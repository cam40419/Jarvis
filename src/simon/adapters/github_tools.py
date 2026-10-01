"""Allowlisted GitHub REST operations; no push, merge, deletion, or arbitrary HTTP."""

from __future__ import annotations

import base64
import binascii
import json
import re
from collections.abc import Sequence
from typing import Any
from urllib.parse import quote
from uuid import UUID

from simon.adapters.optional_http import (
    BoundedHTTP,
    authorize_operation,
    connection_endpoint,
    credential,
    redact,
    redact_bytes,
    relative_path,
)
from simon.domain.errors import AuthorizationError
from simon.domain.tool_catalog import (
    ToolCatalogError,
    ToolDefinition,
    ToolExecutionContext,
    ToolExecutionError,
)

MAX_FILE_BYTES = 262144
_WRITES = frozenset({"issue_create", "pull_request_draft"})
_DESCRIPTIONS = {
    "repository": "Read metadata for an explicitly permitted GitHub repository.",
    "issues": "List a bounded page of GitHub issues (including pull requests).",
    "issue": "Read one GitHub issue and its Markdown body.",
    "pull_requests": "List a bounded page of GitHub pull requests.",
    "pull_request": "Read a pull request's description and branch references.",
    "file_read": "Read a UTF-8 repository file up to 256 KiB at a branch or commit reference.",
    "issue_create": "Create a GitHub issue in an explicitly permitted repository.",
    "pull_request_draft": "Create a draft pull request between existing repository branches.",
}


def _schema(operation: str) -> dict[str, Any]:
    text = {"type": "string", "minLength": 1, "maxLength": 256}
    properties: dict[str, Any] = {
        "repository": {**text, "pattern": r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$"},
    }
    required = ["repository"]
    if operation in {"issues", "pull_requests"}:
        properties.update(
            {
                "page": {"type": "integer", "minimum": 1, "maximum": 1000},
                "per_page": {"type": "integer", "minimum": 1, "maximum": 100},
                "state": {"enum": ["open", "closed", "all"]},
            }
        )
    if operation in {"issue", "pull_request"}:
        properties["number"] = {"type": "integer", "minimum": 1, "maximum": 1000000000}
        required.append("number")
    if operation == "file_read":
        properties.update({"path": {**text, "maxLength": 1000}, "ref": text})
        required.append("path")
    if operation in _WRITES:
        properties.update({"title": text, "body": {"type": "string", "maxLength": 32000}})
        required.append("title")
    if operation == "pull_request_draft":
        properties.update({"head": text, "base": text})
        required.extend(["head", "base"])
    return {
        "type": "object",
        "properties": properties,
        "required": required,
        "additionalProperties": False,
    }


def github_tool_definitions(
    *,
    enabled: bool = False,
    workspace_id: UUID | None = None,
    actor_ids: Sequence[UUID] = (),
    repositories: Sequence[str] = (),
    credential_env: str = "SIMON_GITHUB_TOKEN",
    endpoint: str = "https://api.github.com",
) -> tuple[ToolDefinition, ...]:
    configured = bool(workspace_id and actor_ids and repositories)
    return tuple(
        ToolDefinition(
            id="github." + operation,
            description=description,
            categories=frozenset({"software", "github"}),
            capabilities=frozenset({"github." + operation}),
            transport="github",
            enabled=enabled,
            configured=configured,
            endpoint=endpoint,
            credential_env=credential_env,
            required_scopes=frozenset({"jobs:write" if operation in _WRITES else "jobs:read"}),
            side_effect=operation in _WRITES,
            action_policy="write" if operation in _WRITES else "read",
            input_schema=_schema(operation),
            output_schema={"type": "object"},
            settings={
                "operation": operation,
                "workspace_id": str(workspace_id) if workspace_id else "",
                "actor_ids": [str(item) for item in actor_ids],
                "repositories": list(repositories),
                "api_version": "2026-03-10",
                "network": True,
            },
        )
        for operation, description in _DESCRIPTIONS.items()
    )


def _object(value: Any, *, write: bool) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ToolExecutionError("GitHub returned an invalid object", unknown=write)
    return value


def _summary(value: dict[str, Any], *, repository: bool = False) -> dict[str, Any]:
    keys = (
        (
            "full_name",
            "description",
            "html_url",
            "default_branch",
            "private",
            "archived",
            "language",
            "open_issues_count",
        )
        if repository
        else (
            "number",
            "title",
            "state",
            "body",
            "html_url",
            "draft",
            "created_at",
            "updated_at",
            "merged",
            "mergeable",
            "additions",
            "deletions",
            "changed_files",
        )
    )
    result = {
        key: value[key]
        for key in keys
        if isinstance(value.get(key), str | int | float | bool)
        or (key in value and value[key] is None)
    }
    for key, item in result.items():
        if isinstance(item, str) and len(item) > 32000:
            result[key] = item[:32000]
            result["body_truncated"] = True
            break
    if not repository:
        result["kind"] = "pull_request" if "pull_request" in value or "head" in value else "issue"
        for key in ("head", "base"):
            if isinstance(value.get(key), dict):
                result[key] = {
                    field: value[key][field]
                    for field in ("ref", "sha")
                    if isinstance(value[key].get(field), str)
                }
    return result


class GitHubTransport(BoundedHTTP):
    def __call__(
        self,
        definition: ToolDefinition,
        arguments: dict[str, Any],
        context: ToolExecutionContext,
    ) -> dict[str, Any]:
        operation = definition.settings.get("operation")
        if not isinstance(operation, str) or operation not in _DESCRIPTIONS:
            raise ToolCatalogError("Unsupported GitHub operation")
        write = operation in _WRITES
        authorize_operation(
            definition,
            arguments,
            context,
            transport="github",
            write=write,
            schema=_schema(operation),
        )
        endpoint = connection_endpoint(definition)
        secret = credential(definition, self.environ)
        repositories = definition.settings.get("repositories")
        if (
            not isinstance(repositories, list)
            or not repositories
            or len(repositories) > 100
            or any(
                not isinstance(item, str)
                or re.fullmatch(
                    r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+",
                    item,
                )
                is None
                for item in repositories
            )
        ):
            raise ToolCatalogError("Configure an explicit GitHub repository allowlist")
        repository = arguments["repository"]
        if repository.lower() not in {item.lower() for item in repositories}:
            raise AuthorizationError("The GitHub repository is not in this tool's allowlist")
        relative_path(repository)
        version = definition.settings.get("api_version", "2026-03-10")
        if not isinstance(version, str) or re.fullmatch(r"\d{4}-\d{2}-\d{2}", version) is None:
            raise ToolCatalogError("The GitHub API version is invalid")
        headers = {
            "Authorization": "Bearer " + secret,
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": version,
            "Content-Type": "application/json",
        }
        url = endpoint + "/repos/" + repository
        params: dict[str, Any] = {}
        body = None
        if operation in {"issues", "issue", "issue_create"}:
            url += "/issues"
        if operation in {"pull_requests", "pull_request", "pull_request_draft"}:
            url += "/pulls"
        if operation in {"issue", "pull_request"}:
            url += "/" + str(arguments["number"])
        if operation in {"issues", "pull_requests"}:
            params = {
                key: arguments.get(key, default)
                for key, default in (
                    ("page", 1),
                    ("per_page", 30),
                    ("state", "open"),
                )
            }
        if operation == "file_read":
            path = relative_path(arguments["path"])
            url += "/contents/" + quote(path, safe="/")
            if "ref" in arguments:
                params["ref"] = arguments["ref"]
        if write:
            payload = {"title": arguments["title"], "body": arguments.get("body", "")}
            if operation == "pull_request_draft":
                for key in ("head", "base"):
                    branch = arguments[key]
                    if (
                        ":" in branch
                        or any(ord(char) < 32 for char in branch)
                        or branch.startswith("-")
                        or branch != branch.strip()
                    ):
                        raise ToolCatalogError(
                            "Draft pull requests require same-repository branches"
                        )
                    payload[key] = branch
                payload["draft"] = True
                payload["maintainer_can_modify"] = False
            body = json.dumps(payload).encode("utf-8")
        response = self.request(
            "POST" if write else "GET",
            url,
            headers=headers,
            params=params,
            content=body,
            write=write,
            expected=frozenset({201 if write else 200}),
        )
        data = response.json(write=write)
        if operation in {"issues", "pull_requests"}:
            if not isinstance(data, list) or len(data) > params["per_page"]:
                raise ToolExecutionError("GitHub returned an invalid result page")
            result = {
                "items": [_summary(_object(item, write=False)) for item in data],
                "page": params["page"],
                "next_page": params["page"] + 1
                if 'rel="next"' in response.headers.get("link", "")
                else None,
            }
        elif operation == "file_read":
            item = _object(data, write=False)
            if item.get("type") != "file" or item.get("encoding") != "base64":
                raise ToolExecutionError("GitHub content is not a supported regular file")
            if not isinstance(item.get("sha"), str) or len(item["sha"]) > 128:
                raise ToolExecutionError("GitHub file has an invalid revision")
            encoded = item.get("content")
            if not isinstance(encoded, str) or len(encoded) > 400000:
                raise ToolExecutionError("GitHub file exceeds the supported size")
            try:
                raw = base64.b64decode("".join(encoded.split()), validate=True)
                if len(raw) > MAX_FILE_BYTES:
                    raise ToolExecutionError("GitHub files are limited to 256 KiB")
                raw, redacted = redact_bytes(raw, (secret,))
                text = raw.decode("utf-8")
            except (binascii.Error, UnicodeError):
                raise ToolExecutionError("GitHub file is not valid UTF-8 text") from None
            result = {
                "path": arguments["path"],
                "text": text,
                "sha": item.get("sha"),
                "bytes": len(raw),
                "redacted": redacted,
            }
        else:
            result = _summary(_object(data, write=write), repository=operation == "repository")
            if write and (
                not isinstance(result.get("number"), int)
                or not isinstance(result.get("html_url"), str)
            ):
                raise ToolExecutionError("GitHub creation outcome is incomplete", unknown=True)
            if operation == "pull_request_draft" and result.get("draft") is not True:
                raise ToolExecutionError(
                    "GitHub did not confirm a draft pull request", unknown=True
                )
        return dict(redact(result, (secret,)))
