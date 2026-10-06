"""Fixed ClickUp v2 operations with explicit account, workspace and home-list grants."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from simon.adapters.optional_http import BoundedHTTP
from simon.domain.errors import AuthorizationError, InvalidTransitionError
from simon.domain.models import ActorContext
from simon.domain.project_boards import (
    BoardAssignee,
    BoardConnection,
    BoardList,
    BoardStatus,
    BoardTask,
    BoardTaskPage,
)
from simon.domain.tool_catalog import ToolCatalogError, ToolExecutionError

API = "https://api.clickup.com/api/v2"


def load_board_connections(path: Path | None) -> tuple[BoardConnection, ...]:
    if path is None:
        return ()
    try:
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 262144:
            raise ValueError
        with path.open("rb") as stream:
            raw = stream.read(262145)
        if len(raw) > 262144:
            raise ValueError
        data = json.loads(raw)
        if not isinstance(data, list) or len(data) > 100:
            raise ValueError
        result = tuple(BoardConnection.model_validate(item) for item in data)
        if len({item.id for item in result}) != len(result):
            raise ValueError
        return result
    except (OSError, ValueError, UnicodeError):
        raise ToolCatalogError(
            "Board configuration must be a bounded list of valid connections"
        ) from None


def board_configuration_reasons(
    connection: BoardConnection, environ: Mapping[str, str]
) -> tuple[str, ...]:
    reasons = []
    if not connection.enabled:
        reasons.append("ClickUp connection is disabled")
    token = environ.get(connection.credential_env, "")
    if (
        not token
        or len(token) > 4096
        or any(ord(char) <= 32 or ord(char) == 127 for char in token)
        or (connection.auth_type == "personal" and not token.startswith("pk_"))
    ):
        reasons.append("ClickUp credential is not configured")
    return tuple(reasons)


def task_marker(marker: str) -> str:
    if not isinstance(marker, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{8,200}", marker):
        raise ToolCatalogError("A bounded durable operation reference is required")
    return f"[Simon reference: {marker}]"


def _identifier(value: str, *, numeric: bool = False) -> str:
    if not isinstance(value, str) or not re.fullmatch(
        r"[0-9]{1,30}" if numeric else r"[A-Za-z0-9_-]{1,100}", value
    ):
        raise ToolCatalogError("Invalid ClickUp resource identifier")
    return value


def _text(value: str, *, maximum: int, empty: bool = False) -> str:
    if not isinstance(value, str) or len(value) > maximum or (not empty and not value.strip()):
        raise ToolCatalogError("ClickUp text exceeds the supported bounds")
    return value


def _rows(value: Any, key: str, *, maximum: int) -> list[dict[str, Any]]:
    rows = value.get(key) if isinstance(value, dict) else None
    if (
        not isinstance(rows, list)
        or len(rows) > maximum
        or not all(isinstance(row, dict) for row in rows)
    ):
        raise ToolExecutionError("ClickUp returned an invalid or oversized collection")
    return rows


class ClickUpAdapter:
    def __init__(self, http: BoundedHTTP) -> None:
        self.http = http

    def _authorize(
        self,
        connection: BoardConnection,
        actor: ActorContext,
        *,
        write: bool = False,
        list_id: str | None = None,
    ) -> None:
        if (
            actor.workspace_id != connection.workspace_id
            or actor.actor_id not in connection.actor_ids
            or ("jobs:write" if write else "jobs:read") not in actor.scopes
        ):
            raise AuthorizationError("This ClickUp connection is not granted to this account")
        if list_id is not None and (
            _identifier(list_id, numeric=True) not in connection.list_ids
            and not connection.discover_lists
        ):
            raise AuthorizationError("The ClickUp list is outside the configured allowlist")
        reasons = board_configuration_reasons(connection, self.http.environ)
        if reasons:
            raise ToolCatalogError("; ".join(reasons))

    def _request(
        self,
        connection: BoardConnection,
        method: str,
        path: str,
        *,
        write: bool = False,
        body: dict[str, Any] | None = None,
        params: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        token = self.http.environ.get(connection.credential_env, "")
        response = self.http.request(
            method,
            API + path,
            write=write,
            headers={
                "Authorization": ("Bearer " if connection.auth_type == "oauth" else "") + token,
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            expected=frozenset({200}),
            params=params,
            content=json.dumps(body).encode() if body is not None else None,
        )
        value = response.json(write=write)
        if not isinstance(value, dict) or (token and token in json.dumps(value)):
            raise ToolExecutionError("ClickUp returned an invalid response", unknown=write)
        return value

    def _spaces(self, connection: BoardConnection) -> frozenset[str]:
        teams = _rows(self._request(connection, "GET", "/team"), "teams", maximum=1000)
        if not any(str(team.get("id")) == connection.clickup_workspace_id for team in teams):
            raise AuthorizationError(
                "The ClickUp token does not authorize the configured workspace"
            )
        rows = _rows(
            self._request(
                connection,
                "GET",
                f"/team/{connection.clickup_workspace_id}/space",
                params={"archived": "false"},
            ),
            "spaces",
            maximum=1000,
        )
        return frozenset(str(row.get("id")) for row in rows if not row.get("archived", False))

    def _metadata(
        self,
        connection: BoardConnection,
        list_id: str,
        spaces: frozenset[str],
        shared_lists: frozenset[str] = frozenset(),
    ) -> BoardList:
        value = self._request(connection, "GET", f"/list/{list_id}")
        try:
            space_id = str(value["space"]["id"])
            if str(value["id"]) != list_id or (
                space_id not in spaces and list_id not in shared_lists
            ):
                raise AuthorizationError(
                    "The ClickUp list does not belong to the configured workspace"
                )
            statuses = tuple(
                BoardStatus.model_validate(
                    {
                        "status": row["status"],
                        "type": row["type"],
                        "color": row.get("color"),
                    }
                )
                for row in _rows(value, "statuses", maximum=100)
            )
            return BoardList(
                id=list_id,
                workspace_id=connection.clickup_workspace_id,
                name=value["name"],
                space_id=space_id,
                statuses=statuses,
                archived=value.get("archived", False),
                url=f"https://app.clickup.com/{connection.clickup_workspace_id}/v/li/{list_id}",
            )
        except (KeyError, TypeError, ValueError):
            raise ToolExecutionError("ClickUp returned invalid list metadata") from None

    def _checked_list(
        self,
        connection: BoardConnection,
        actor: ActorContext,
        list_id: str,
        *,
        write: bool = False,
    ) -> BoardList:
        self._authorize(connection, actor, list_id=list_id, write=write)
        spaces = self._spaces(connection)
        shared = self._shared_lists(connection) if connection.discover_lists else frozenset()
        result = self._metadata(connection, list_id, spaces, shared)
        if result.archived:
            raise ToolCatalogError("Archived ClickUp lists require operator review")
        return result

    def _shared_lists(self, connection: BoardConnection) -> frozenset[str]:
        shared = self._request(
            connection, "GET", f"/team/{connection.clickup_workspace_id}/shared"
        ).get("shared")
        lists = _rows(shared, "lists", maximum=1000)
        folders = _rows(shared, "folders", maximum=1000)
        for folder in folders:
            folder_id = _identifier(str(folder.get("id")), numeric=True)
            lists.extend(
                _rows(
                    self._request(
                        connection, "GET", f"/folder/{folder_id}/list", params={"archived": "false"}
                    ),
                    "lists",
                    maximum=1000,
                )
            )
        if len(lists) > 10000:
            raise ToolExecutionError("ClickUp shared list discovery exceeds the supported bounds")
        return frozenset(
            _identifier(str(row.get("id")), numeric=True)
            for row in lists
            if not row.get("archived", False)
        )

    def boards(self, connection: BoardConnection, actor: ActorContext) -> tuple[BoardList, ...]:
        self._authorize(connection, actor)
        spaces = self._spaces(connection)
        identifiers = set(connection.list_ids)
        shared: frozenset[str] = frozenset()
        if connection.discover_lists:
            shared = self._shared_lists(connection)
            identifiers.update(shared)
            for space in sorted(spaces):
                space = _identifier(space, numeric=True)
                lists = _rows(
                    self._request(
                        connection, "GET", f"/space/{space}/list", params={"archived": "false"}
                    ),
                    "lists",
                    maximum=1000,
                )
                folders = _rows(
                    self._request(
                        connection, "GET", f"/space/{space}/folder", params={"archived": "false"}
                    ),
                    "folders",
                    maximum=1000,
                )
                for folder in folders:
                    folder_id = _identifier(str(folder.get("id")), numeric=True)
                    lists.extend(
                        _rows(
                            self._request(
                                connection,
                                "GET",
                                f"/folder/{folder_id}/list",
                                params={"archived": "false"},
                            ),
                            "lists",
                            maximum=1000,
                        )
                    )
                identifiers.update(
                    _identifier(str(row.get("id")), numeric=True)
                    for row in lists
                    if not row.get("archived", False)
                )
                if len(identifiers) > 10000:
                    raise ToolExecutionError("ClickUp list discovery exceeds the supported bounds")
        return tuple(
            self._metadata(connection, value, spaces, shared) for value in sorted(identifiers)
        )

    def list_metadata(
        self,
        connection: BoardConnection,
        actor: ActorContext,
        list_id: str,
    ) -> BoardList:
        return self._checked_list(connection, actor, list_id)

    @staticmethod
    def _task(
        connection: BoardConnection,
        value: dict[str, Any],
        metadata: BoardList,
        *,
        expected_id: str | None = None,
        write: bool = False,
    ) -> BoardTask:
        try:
            identifier = _identifier(value["id"])
            if (
                (expected_id is not None and identifier != expected_id)
                or str(value["list"]["id"]) != metadata.id
                or str(value["team_id"]) != connection.clickup_workspace_id
                or str(value["space"]["id"]) != metadata.space_id
            ):
                raise ValueError("Resource boundary mismatch")
            dependencies = []
            for row in _rows(value, "dependencies", maximum=100):
                # ClickUp includes incoming and outgoing relationships. Only prerequisites apply.
                if str(row.get("task_id")) == identifier:
                    if str(row.get("workspace_id")) != connection.clickup_workspace_id:
                        raise ValueError("Dependency workspace mismatch")
                    dependencies.append(_identifier(row["depends_on"]))
            return BoardTask(
                id=identifier,
                list_id=metadata.id,
                workspace_id=connection.clickup_workspace_id,
                name=value["name"],
                description=value.get("markdown_description") or value.get("description") or "",
                status=value["status"]["status"],
                status_type=value["status"]["type"],
                date_updated=str(value["date_updated"]),
                assignees=tuple(
                    sorted(
                        (
                            BoardAssignee(id=str(row["id"]), name=row.get("username") or "")
                            for row in _rows(value, "assignees", maximum=100)
                        ),
                        key=lambda item: item.id,
                    )
                ),
                dependencies=tuple(sorted(set(dependencies))),
                archived=value.get("archived", False),
                url=f"https://app.clickup.com/t/{identifier}",
            )
        except (KeyError, TypeError, ValueError, ToolCatalogError, ToolExecutionError):
            raise ToolExecutionError(
                "ClickUp returned an invalid task or one outside the configured list/workspace",
                unknown=write,
            ) from None

    def tasks(
        self,
        connection: BoardConnection,
        actor: ActorContext,
        list_id: str,
        *,
        page: int = 0,
    ) -> BoardTaskPage:
        if type(page) is not int or not 0 <= page <= 1000:
            raise ToolCatalogError("ClickUp task page must be between 0 and 1000")
        metadata = self._checked_list(connection, actor, list_id)
        value = self._request(
            connection,
            "GET",
            f"/list/{list_id}/task",
            params={
                "page": page,
                "include_closed": "true",
                "subtasks": "true",
                "include_timl": "false",
                "include_markdown_description": "true",
                "order_by": "id",
                "archived": "false",
            },
        )
        tasks = tuple(
            self._task(connection, row, metadata) for row in _rows(value, "tasks", maximum=100)
        )
        if len({task.id for task in tasks}) != len(tasks):
            raise ToolExecutionError("ClickUp returned duplicate tasks in one page")
        more = len(tasks) == 100 and value.get("last_page") is not True
        if more and page == 1000:
            raise ToolExecutionError("ClickUp list exceeds the supported pagination bound")
        return BoardTaskPage(tasks=tasks, page=page, next_page=page + 1 if more else None)

    def _get(self, connection: BoardConnection, task_id: str, metadata: BoardList) -> BoardTask:
        value = self._request(
            connection,
            "GET",
            f"/task/{_identifier(task_id)}",
            params={
                "include_markdown_description": "true",
            },
        )
        return self._task(connection, value, metadata, expected_id=task_id)

    def get_task(
        self,
        connection: BoardConnection,
        actor: ActorContext,
        task_id: str,
        *,
        list_id: str,
    ) -> BoardTask:
        _identifier(task_id)
        metadata = self._checked_list(connection, actor, list_id)
        return self._get(connection, task_id, metadata)

    def accessible_task(
        self, connection: BoardConnection, actor: ActorContext, task_id: str
    ) -> BoardTask:
        """Read individually shared tasks without granting access to their whole List."""
        self._authorize(connection, actor)
        self._spaces(connection)
        task_id = _identifier(task_id)
        value = self._request(
            connection, "GET", f"/task/{task_id}", params={"include_markdown_description": "true"}
        )
        try:
            list_id = _identifier(str(value["list"]["id"]), numeric=True)
            space_id = _identifier(str(value["space"]["id"]), numeric=True)
            self._authorize(connection, actor, list_id=list_id)
            if str(value["team_id"]) != connection.clickup_workspace_id:
                raise AuthorizationError("The ClickUp task belongs to another Workspace")
            metadata = BoardList(
                id=list_id,
                workspace_id=connection.clickup_workspace_id,
                name="Shared task List",
                space_id=space_id,
                statuses=(),
                url=f"https://app.clickup.com/{connection.clickup_workspace_id}/v/li/{list_id}",
            )
        except (KeyError, TypeError, ValueError):
            raise ToolExecutionError("ClickUp returned invalid task metadata") from None
        return self._task(connection, value, metadata, expected_id=task_id)

    def shared_task_ids(self, connection: BoardConnection, actor: ActorContext) -> tuple[str, ...]:
        self._authorize(connection, actor)
        self._spaces(connection)
        shared = self._request(
            connection, "GET", f"/team/{connection.clickup_workspace_id}/shared"
        ).get("shared")
        return tuple(
            _identifier(str(row.get("id"))) for row in _rows(shared, "tasks", maximum=1000)
        )

    def get_tasks(
        self,
        connection: BoardConnection,
        actor: ActorContext,
        task_ids: Sequence[str],
        *,
        list_id: str,
    ) -> tuple[BoardTask, ...]:
        """Reuse verification within one bounded read batch, never across requests or actors."""
        if isinstance(task_ids, str) or not 1 <= len(task_ids) <= 25:
            raise ToolCatalogError("A ClickUp read batch requires between one and 25 task IDs")
        identifiers = tuple(_identifier(value) for value in task_ids)
        if len(set(identifiers)) != len(identifiers):
            raise ToolCatalogError("ClickUp read batches require unique task IDs")
        metadata = self._checked_list(connection, actor, list_id)
        return tuple(self._get(connection, identifier, metadata) for identifier in identifiers)

    @staticmethod
    def _status(metadata: BoardList, status: str) -> None:
        _text(status, maximum=100)
        if status not in {row.status for row in metadata.statuses}:
            raise ToolCatalogError("The requested status is not present in the ClickUp list")

    def create_task(
        self,
        connection: BoardConnection,
        actor: ActorContext,
        list_id: str,
        *,
        name: str,
        description: str,
        status: str,
        marker: str,
    ) -> BoardTask:
        _text(name, maximum=500)
        _text(description, maximum=30_000, empty=True)
        reference = task_marker(marker)
        metadata = self._checked_list(connection, actor, list_id, write=True)
        self._status(metadata, status)
        value = self._request(
            connection,
            "POST",
            f"/list/{list_id}/task",
            write=True,
            body={
                "name": name,
                "description": description + "\n\n" + reference,
                "status": status,
                "check_required_custom_fields": True,
                "notify_all": False,
            },
        )
        result = self._task(connection, value, metadata, write=True)
        if result.name != name or result.status != status or reference not in result.description:
            raise ToolExecutionError(
                "ClickUp did not confirm the created task details", unknown=True
            )
        return result

    def update_status(
        self,
        connection: BoardConnection,
        actor: ActorContext,
        task_id: str,
        *,
        list_id: str,
        status: str,
        expected_updated: str,
    ) -> BoardTask:
        _identifier(task_id)
        if not isinstance(expected_updated, str) or not re.fullmatch(
            r"[0-9]{1,20}", expected_updated
        ):
            raise ToolCatalogError("Status updates require the last observed update timestamp")
        metadata = self._checked_list(connection, actor, list_id, write=True)
        self._status(metadata, status)
        current = self._get(connection, task_id, metadata)
        if current.archived or current.date_updated != expected_updated:
            raise InvalidTransitionError("ClickUp task changed; refresh and review before updating")
        if current.status == status:
            return current
        value = self._request(
            connection, "PUT", f"/task/{task_id}", write=True, body={"status": status}
        )
        result = self._task(connection, value, metadata, expected_id=task_id, write=True)
        if result.status != status:
            raise ToolExecutionError("ClickUp did not confirm the requested status", unknown=True)
        return result

    def add_dependency(
        self,
        connection: BoardConnection,
        actor: ActorContext,
        task_id: str,
        depends_on: str,
        *,
        list_id: str,
    ) -> dict[str, Any]:
        _identifier(task_id)
        _identifier(depends_on)
        if task_id == depends_on:
            raise ToolCatalogError("A ClickUp task cannot depend on itself")
        metadata = self._checked_list(connection, actor, list_id, write=True)
        current, prerequisite = (
            self._get(connection, task_id, metadata),
            self._get(
                connection,
                depends_on,
                metadata,
            ),
        )
        if current.archived or prerequisite.archived or task_id in prerequisite.dependencies:
            raise ToolCatalogError("Archived tasks and direct dependency cycles require review")
        if depends_on in current.dependencies:
            return {"task_id": task_id, "depends_on": depends_on, "already_present": True}
        self._request(
            connection,
            "POST",
            f"/task/{task_id}/dependency",
            write=True,
            body={"depends_on": depends_on},
        )
        return {"task_id": task_id, "depends_on": depends_on, "accepted": True}

    def append_comment(
        self,
        connection: BoardConnection,
        actor: ActorContext,
        task_id: str,
        *,
        list_id: str,
        text: str,
        marker: str,
    ) -> dict[str, Any]:
        _identifier(task_id)
        _text(text, maximum=20_000)
        reference = task_marker(marker)
        metadata = self._checked_list(connection, actor, list_id, write=True)
        if self._get(connection, task_id, metadata).archived:
            raise ToolCatalogError("Archived tasks require operator review")
        value = self._request(
            connection,
            "POST",
            f"/task/{task_id}/comment",
            write=True,
            body={
                "comment_text": text + "\n\n" + reference,
                "notify_all": False,
            },
        )
        try:
            identifier = _identifier(str(value["id"]), numeric=True)
            timestamp = str(value["date"])
            if not re.fullmatch(r"[0-9]{1,20}", timestamp):
                raise ValueError
        except (KeyError, ValueError, ToolCatalogError):
            raise ToolExecutionError(
                "ClickUp returned an invalid comment receipt", unknown=True
            ) from None
        return {"task_id": task_id, "comment_id": identifier, "date": timestamp, "marker": marker}
