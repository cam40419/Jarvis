"""Scoped Dropbox writes and read-only Box / Microsoft Graph storage adapters."""

from __future__ import annotations

import base64
import binascii
import json
import re
import time
from collections.abc import Callable, Mapping, Sequence
from typing import Any
from urllib.parse import quote, urlsplit
from uuid import UUID

from simon.adapters.optional_http import (
    BoundedHTTP,
    HTTPResult,
    authorize_operation,
    credential,
    optional_tool_status,
    redact,
    redact_bytes,
    relative_path,
)
from simon.domain.errors import AuthorizationError
from simon.domain.models import ActorContext
from simon.domain.tool_catalog import (
    ToolCatalogError,
    ToolDefinition,
    ToolExecutionContext,
    ToolExecutionError,
)

MAX_FILE_BYTES = 262144
ENDPOINTS = {
    "dropbox": "https://api.dropboxapi.com/2",
    "box": "https://api.box.com/2.0",
    "onedrive": "https://graph.microsoft.com/v1.0",
}
_DESCRIPTIONS = {
    "dropbox": {
        "list": "List a scoped Dropbox folder; pages are fetched from a fresh scoped cursor.",
        "search": "Search file names under the configured Dropbox folder.",
        "metadata": "Read metadata for a relative path under the configured Dropbox folder.",
        "read": "Read a Dropbox file up to 256 KiB as text or base64.",
        "upload": "Create or revise a small Dropbox file using strict revision conflict checks.",
    },
    "box": {
        "list": "List a Box folder after verifying it belongs to the configured folder tree.",
        "metadata": "Read Box file or folder metadata after checking server-side ancestry.",
        "read": "Read a small Box file, validating ancestry and a fixed download host.",
    },
    "onedrive": {
        "list": "List a OneDrive or SharePoint folder inside a configured drive and folder tree.",
        "metadata": "Read OneDrive or SharePoint item metadata with parent-chain verification.",
        "read": "Read a small OneDrive or SharePoint file from an allowed download host.",
    },
}


def _schema(provider: str, operation: str) -> dict[str, Any]:
    text = {"type": "string", "maxLength": 1000}
    properties: dict[str, Any] = {}
    required = []
    if provider == "box":
        properties["item_id"] = {"type": "string", "pattern": r"^[0-9]{1,30}$"}
        if operation != "list":
            required.append("item_id")
        if operation == "metadata":
            properties["kind"] = {"enum": ["file", "folder"]}
        if operation == "list":
            properties["offset"] = {"type": "integer", "minimum": 0, "maximum": 10000}
    else:
        properties["path"] = text
        if operation in {"read", "upload"}:
            properties["path"] = {**text, "minLength": 1}
            required.append("path")
        if operation in {"list", "search"}:
            properties["page"] = {"type": "integer", "minimum": 1, "maximum": 10}
    if operation == "search":
        properties["query"] = {"type": "string", "minLength": 1, "maxLength": 200}
        required.append("query")
    if operation in {"read", "upload"}:
        properties["encoding"] = {"enum": ["text", "base64"]}
    if operation == "upload":
        properties.update(
            {
                "content": {"type": "string", "maxLength": 349528},
                "expected_revision": {"type": "string", "maxLength": 128},
            }
        )
        required.extend(["content", "expected_revision"])
    return {
        "type": "object",
        "properties": properties,
        "required": required,
        "additionalProperties": False,
    }


def _definitions(
    provider: str,
    *,
    enabled: bool,
    workspace_id: UUID | None,
    actor_ids: Sequence[UUID],
    credential_env: str,
    configured: bool,
    settings: dict[str, Any],
) -> tuple[ToolDefinition, ...]:
    return tuple(
        ToolDefinition(
            id=provider + "." + operation,
            description=description,
            transport=provider,
            categories=frozenset({"files", "storage", provider}),
            capabilities=frozenset({provider + "." + operation}),
            enabled=enabled,
            configured=configured and bool(workspace_id and actor_ids),
            endpoint=ENDPOINTS[provider],
            credential_env=credential_env,
            required_scopes=frozenset({"jobs:write" if operation == "upload" else "jobs:read"}),
            side_effect=operation == "upload",
            action_policy="write" if operation == "upload" else "read",
            input_schema=_schema(provider, operation),
            output_schema={"type": "object"},
            settings={
                "operation": operation,
                "network": True,
                "workspace_id": str(workspace_id) if workspace_id else "",
                "actor_ids": [str(item) for item in actor_ids],
                **settings,
            },
        )
        for operation, description in _DESCRIPTIONS[provider].items()
    )


def dropbox_tool_definitions(
    *,
    enabled: bool = False,
    workspace_id: UUID | None = None,
    actor_ids: Sequence[UUID] = (),
    root_path: str = "",
    credential_env: str = "SIMON_DROPBOX_TOKEN",
) -> tuple[ToolDefinition, ...]:
    return _definitions(
        "dropbox",
        enabled=enabled,
        workspace_id=workspace_id,
        actor_ids=actor_ids,
        credential_env=credential_env,
        configured=bool(root_path),
        settings={"root_path": root_path},
    )


def box_tool_definitions(
    *,
    enabled: bool = False,
    workspace_id: UUID | None = None,
    actor_ids: Sequence[UUID] = (),
    root_folder_id: str = "",
    credential_env: str = "SIMON_BOX_TOKEN",
) -> tuple[ToolDefinition, ...]:
    return _definitions(
        "box",
        enabled=enabled,
        workspace_id=workspace_id,
        actor_ids=actor_ids,
        credential_env=credential_env,
        configured=bool(root_folder_id),
        settings={"root_folder_id": root_folder_id, "download_hosts": ["dl.boxcloud.com"]},
    )


def onedrive_tool_definitions(
    *,
    enabled: bool = False,
    workspace_id: UUID | None = None,
    actor_ids: Sequence[UUID] = (),
    drive_id: str = "",
    root_item_id: str = "",
    download_hosts: Sequence[str] = (),
    credential_env: str = "SIMON_ONEDRIVE_TOKEN",
) -> tuple[ToolDefinition, ...]:
    return _definitions(
        "onedrive",
        enabled=enabled,
        workspace_id=workspace_id,
        actor_ids=actor_ids,
        credential_env=credential_env,
        configured=bool(drive_id and root_item_id),
        settings={
            "drive_id": drive_id,
            "root_item_id": root_item_id,
            "download_hosts": list(download_hosts),
        },
    )


def _identifier(value: Any, *, numeric: bool = False) -> str:
    pattern = r"[0-9]{1,30}" if numeric else r"[A-Za-z0-9_!.-]{1,250}"
    if not isinstance(value, str) or re.fullmatch(pattern, value) is None or value in {".", ".."}:
        raise ToolCatalogError("Storage identifiers must be valid configured item identifiers")
    return value


def _settings(definition: ToolDefinition) -> None:
    provider = definition.transport
    if provider not in ENDPOINTS or definition.endpoint != ENDPOINTS[provider]:
        raise ToolCatalogError("Cloud storage tools require their fixed provider API endpoint")
    if definition.settings.get("operation") not in _DESCRIPTIONS[provider]:
        raise ToolCatalogError("Unsupported cloud storage operation")
    if provider == "dropbox":
        root = definition.settings.get("root_path")
        if not isinstance(root, str) or not root.startswith("/") or root == "/":
            raise ToolCatalogError("Configure a Dropbox folder path, such as /Simon")
        relative_path(root[1:])
    if provider == "box":
        _identifier(definition.settings.get("root_folder_id"), numeric=True)
    if provider == "onedrive":
        _identifier(definition.settings.get("drive_id"))
        _identifier(definition.settings.get("root_item_id"))
    if provider in {"box", "onedrive"}:
        hosts = definition.settings.get("download_hosts", [])
        if not isinstance(hosts, list) or len(hosts) > 20:
            raise ToolCatalogError("Configure exact provider download hostnames")
        for host in hosts:
            if (
                not isinstance(host, str)
                or host != host.lower()
                or re.fullmatch(r"[a-z0-9]+(?:[a-z0-9.-]*[a-z0-9])?", host) is None
                or (provider == "box" and host != "dl.boxcloud.com")
                or (
                    provider == "onedrive"
                    and not host.endswith((".sharepoint.com", ".files.1drv.com"))
                )
            ):
                raise ToolCatalogError("Download hosts must be exact supported provider hostnames")
        if definition.settings.get("operation") == "read" and not hosts:
            raise ToolCatalogError("Configure exact download hosts before enabling file reads")


def cloud_storage_tool_status(
    definition: ToolDefinition,
    actor: ActorContext,
    environ: Mapping[str, str],
) -> tuple[str, tuple[str, ...]]:
    state, reasons = optional_tool_status(definition, actor, environ)
    if state != "configured":
        return state, reasons
    try:
        _settings(definition)
    except ToolCatalogError as error:
        return "unconfigured", (str(error),)
    return "configured", ()


def _object(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ToolExecutionError("Storage provider returned an invalid object")
    return value


def _items(value: Any, *, maximum: int = 2000) -> list[dict[str, Any]]:
    if not isinstance(value, list) or len(value) > maximum:
        raise ToolExecutionError("Storage provider returned an invalid result page")
    return [_object(item) for item in value]


def _projection(item: dict[str, Any], fields: Sequence[str]) -> dict[str, Any]:
    return {
        key: (item[key][:2000] if isinstance(item[key], str) else item[key])
        for key in fields
        if isinstance(item.get(key), str | int | bool) or (key in item and item[key] is None)
    }


def _content(raw: bytes, encoding: str, secret: str) -> dict[str, Any]:
    if len(raw) > MAX_FILE_BYTES:
        raise ToolExecutionError("Cloud file reads are limited to 256 KiB")
    raw, redacted = redact_bytes(raw, (secret,))
    try:
        content = (
            raw.decode("utf-8") if encoding == "text" else base64.b64encode(raw).decode("ascii")
        )
    except UnicodeError:
        raise ToolExecutionError("File is not UTF-8 text; request base64 encoding") from None
    return {"content": content, "encoding": encoding, "bytes": len(raw), "redacted": redacted}


class _Session:
    def __init__(self, transport: BoundedHTTP) -> None:
        self.transport = transport
        self.deadline = time.monotonic() + transport.timeout_seconds
        self.calls = 0

    def request(self, method: str, url: str, **kwargs: Any) -> HTTPResult:
        self.calls += 1
        remaining = self.deadline - time.monotonic()
        if self.calls > 32 or remaining <= 0:
            raise ToolExecutionError("Cloud storage operation exceeded its request or time budget")
        client = BoundedHTTP(
            transport=self.transport.transport,
            environ=self.transport.environ,
            timeout_seconds=min(remaining, 60),
            max_response_bytes=self.transport.max_response_bytes,
        )
        return client.request(method, url, **kwargs)

    def json(self, method: str, url: str, **kwargs: Any) -> dict[str, Any]:
        return _object(self.request(method, url, **kwargs).json(write=kwargs.get("write", False)))

    def download(self, response: HTTPResult, hosts: list[str], secret: str) -> bytes:
        for _ in range(3):
            if response.status == 200:
                return response.content
            location = response.headers.get("location", "")
            try:
                parsed = urlsplit(location)
                permitted = (
                    parsed.scheme == "https"
                    and parsed.hostname in hosts
                    and parsed.port in {None, 443}
                    and not parsed.username
                    and not parsed.password
                    and not parsed.fragment
                    and not any(ord(char) < 32 or char == "\\" for char in location)
                    and secret not in location
                )
            except ValueError:
                permitted = False
            if not permitted:
                raise ToolExecutionError("Provider download host is not explicitly permitted")
            # Signed download URLs are credentials. Never expose them or forward the API bearer.
            response = self.request(
                "GET", location, headers={}, expected=frozenset({200, 302, 307})
            )
        raise ToolExecutionError("Provider download exceeded the redirect limit")


class ConnectedStorageTransport(BoundedHTTP):
    def __init__(
        self,
        *,
        definition_resolver: Callable[[ToolDefinition, ToolExecutionContext], ToolDefinition]
        | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self.definition_resolver = definition_resolver

    def resolve(self, definition: ToolDefinition, context: ToolExecutionContext) -> ToolDefinition:
        return (
            self.definition_resolver(definition, context)
            if self.definition_resolver
            else definition
        )


class DropboxTransport(ConnectedStorageTransport):
    def __call__(
        self,
        definition: ToolDefinition,
        arguments: dict[str, Any],
        context: ToolExecutionContext,
    ) -> dict[str, Any]:
        definition = self.resolve(definition, context)
        _settings(definition)
        operation = str(definition.settings["operation"])
        authorize_operation(
            definition,
            arguments,
            context,
            transport="dropbox",
            write=operation == "upload",
            schema=_schema("dropbox", operation),
        )
        secret = credential(definition, self.environ)
        session = _Session(self)
        headers = {"Authorization": "Bearer " + secret, "Content-Type": "application/json"}
        root = str(definition.settings["root_path"])
        normalized_root = root.lower()
        path = relative_path(arguments.get("path", ""), empty=operation not in {"read", "upload"})
        full_path = root + ("/" + path if path else "")

        def metadata(
            value: Any,
            *,
            exact: bool = False,
            file_only: bool = False,
        ) -> dict[str, Any]:
            item = _object(value)
            actual = item.get("path_lower")
            if not isinstance(actual, str) or (
                not actual.startswith(normalized_root + "/") and actual != normalized_root
            ):
                raise ToolExecutionError("Dropbox returned an item outside the configured root")
            if exact and actual != full_path.lower():
                raise ToolExecutionError("Dropbox returned a different item than requested")
            relative = actual[len(normalized_root) :].lstrip("/")
            relative_path(relative, empty=True)
            kind = item.get(".tag", "file" if file_only else None)
            if kind not in {"file", "folder"} or (file_only and kind != "file"):
                raise ToolExecutionError("Dropbox returned an unsupported item type")
            result = _projection(
                item,
                ("name", "id", "rev", "size", "server_modified", "is_downloadable", "content_hash"),
            )
            result.update({"path": relative, "kind": kind})
            return result

        def rpc(name: str, payload: dict[str, Any]) -> dict[str, Any]:
            return session.json(
                "POST",
                ENDPOINTS["dropbox"] + "/files/" + name,
                headers=headers,
                content=json.dumps(payload).encode(),
                expected=frozenset({200}),
            )

        if operation in {"list", "search"}:
            page = arguments.get("page", 1)
            if operation == "list":
                data = rpc(
                    "list_folder",
                    {"path": full_path, "recursive": False, "include_deleted": False, "limit": 100},
                )
            else:
                data = rpc(
                    "search_v2",
                    {
                        "query": arguments["query"],
                        "options": {"path": full_path, "max_results": 100, "filename_only": True},
                    },
                )
            for index in range(1, page + 1):
                values = _items(data.get("entries" if operation == "list" else "matches"))
                entries = [
                    metadata(
                        item
                        if operation == "list"
                        else _object(item.get("metadata")).get("metadata")
                    )
                    for item in values
                ]
                for entry in entries:
                    scoped = root.lower() + ("/" + entry["path"] if entry["path"] else "")
                    if not scoped.startswith(full_path.lower() + "/"):
                        raise ToolExecutionError(
                            "Dropbox returned content outside the requested folder"
                        )
                has_more = data.get("has_more") is True
                if index == page or not has_more:
                    result = {
                        "entries": entries if index == page else [],
                        "page": page,
                        "next_page": page + 1 if has_more and page < 10 else None,
                        "more_available": has_more,
                    }
                    return dict(redact(result, (secret,)))
                cursor = data.get("cursor")
                if not isinstance(cursor, str) or not 1 <= len(cursor) <= 10000:
                    raise ToolExecutionError("Dropbox returned an invalid continuation cursor")
                data = rpc(
                    "list_folder/continue" if operation == "list" else "search/continue_v2",
                    {"cursor": cursor},
                )
        if operation == "metadata":
            return dict(
                redact(metadata(rpc("get_metadata", {"path": full_path}), exact=True), (secret,))
            )
        content_headers = {
            "Authorization": "Bearer " + secret,
            "Content-Type": "application/octet-stream",
        }
        if operation == "read":
            content_headers["Dropbox-API-Arg"] = json.dumps({"path": full_path})
            response = session.request(
                "POST",
                "https://content.dropboxapi.com/2/files/download",
                headers=content_headers,
                expected=frozenset({200}),
            )
            try:
                item = metadata(
                    json.loads(response.headers.get("dropbox-api-result", "")),
                    exact=True,
                    file_only=True,
                )
            except (ValueError, ToolExecutionError):
                raise ToolExecutionError(
                    "Dropbox download metadata is invalid or outside its root"
                ) from None
            result = {
                **item,
                **_content(response.content, arguments.get("encoding", "text"), secret),
            }
            return dict(redact(result, (secret,)))
        revision = arguments["expected_revision"]
        if revision and re.fullmatch(r"[A-Za-z0-9]{1,128}", revision) is None:
            raise ToolCatalogError("Dropbox expected revision is invalid")
        try:
            raw = (
                arguments["content"].encode("utf-8")
                if arguments.get("encoding", "text") == "text"
                else base64.b64decode(arguments["content"], validate=True)
            )
        except (ValueError, binascii.Error):
            raise ToolCatalogError("Upload content is not valid base64") from None
        if len(raw) > MAX_FILE_BYTES:
            raise ToolCatalogError("Cloud uploads are limited to 256 KiB")
        content_headers["Dropbox-API-Arg"] = json.dumps(
            {
                "path": full_path,
                "mode": {".tag": "update", "update": revision} if revision else "add",
                "autorename": False,
                "strict_conflict": True,
            }
        )
        response = session.request(
            "POST",
            "https://content.dropboxapi.com/2/files/upload",
            headers=content_headers,
            content=raw,
            write=True,
            expected=frozenset({200}),
        )
        try:
            item = metadata(response.json(write=True), exact=True, file_only=True)
            if not isinstance(item.get("rev"), str) or not item["rev"]:
                raise ToolExecutionError("Missing upload revision")
        except (ToolExecutionError, ToolCatalogError, ValueError):
            raise ToolExecutionError(
                "Dropbox upload outcome could not be verified", unknown=True
            ) from None
        return dict(redact({**item, "written": True}, (secret,)))


class BoxTransport(ConnectedStorageTransport):
    def __call__(
        self,
        definition: ToolDefinition,
        arguments: dict[str, Any],
        context: ToolExecutionContext,
    ) -> dict[str, Any]:
        definition = self.resolve(definition, context)
        _settings(definition)
        operation = str(definition.settings["operation"])
        authorize_operation(
            definition,
            arguments,
            context,
            transport="box",
            write=False,
            schema=_schema("box", operation),
        )
        secret = credential(definition, self.environ)
        session = _Session(self)
        headers = {"Authorization": "Bearer " + secret}
        root = str(definition.settings["root_folder_id"])
        identifier = arguments.get("item_id", root)
        kind = "folder" if operation == "list" else arguments.get("kind", "file")
        fields = "id,type,name,size,etag,path_collection,parent,file_version,modified_at"

        def get(item_id: str, item_kind: str) -> dict[str, Any]:
            item = session.json(
                "GET",
                ENDPOINTS["box"] + f"/{item_kind}s/{item_id}",
                headers=headers,
                params={"fields": fields},
                expected=frozenset({200}),
            )
            if item.get("id") != item_id or item.get("type") != item_kind:
                raise ToolExecutionError("Box returned a different item than requested")
            if item_id != root or item_kind != "folder":
                parents = _items(_object(item.get("path_collection")).get("entries"), maximum=100)
                if root not in {
                    parent.get("id") for parent in parents if parent.get("type") == "folder"
                }:
                    raise AuthorizationError("Box item is outside the configured folder tree")
            return item

        item = get(identifier, kind)
        if operation == "metadata":
            return dict(
                redact(
                    _projection(item, ("id", "type", "name", "size", "etag", "modified_at")),
                    (secret,),
                )
            )
        if operation == "list":
            offset = arguments.get("offset", 0)
            data = session.json(
                "GET",
                ENDPOINTS["box"] + f"/folders/{identifier}/items",
                headers=headers,
                params={"fields": fields, "offset": offset, "limit": 100},
                expected=frozenset({200}),
            )
            entries = _items(data.get("entries"), maximum=100)
            for child in entries:
                if _object(child.get("parent")).get("id") != identifier:
                    raise ToolExecutionError("Box returned an item outside the requested folder")
                if child.get("type") not in {"file", "folder"}:
                    raise ToolExecutionError("Box web links are not supported by this storage tool")
            get(identifier, "folder")
            total = data.get("total_count")
            next_offset = offset + len(entries)
            result = {
                "entries": [
                    _projection(child, ("id", "type", "name", "size", "etag")) for child in entries
                ],
                "next_offset": next_offset
                if isinstance(total, int) and total > next_offset and entries
                else None,
            }
            return dict(redact(result, (secret,)))
        if not isinstance(item.get("size"), int) or not 0 <= item["size"] <= MAX_FILE_BYTES:
            raise ToolExecutionError("Cloud file reads require a known size up to 256 KiB")
        version = _identifier(_object(item.get("file_version")).get("id"), numeric=True)
        response = session.request(
            "GET",
            ENDPOINTS["box"] + f"/files/{identifier}/content",
            headers=headers,
            params={"version": version},
            expected=frozenset({200, 302}),
        )
        raw = session.download(response, definition.settings["download_hosts"], secret)
        latest = get(identifier, "file")
        if _object(latest.get("file_version")).get("id") != version:
            raise ToolExecutionError("Box file changed while reading; read it again")
        result = {
            **_projection(item, ("id", "name", "etag")),
            **_content(raw, arguments.get("encoding", "text"), secret),
        }
        return dict(redact(result, (secret,)))


class OneDriveTransport(ConnectedStorageTransport):
    def __call__(
        self,
        definition: ToolDefinition,
        arguments: dict[str, Any],
        context: ToolExecutionContext,
    ) -> dict[str, Any]:
        definition = self.resolve(definition, context)
        _settings(definition)
        operation = str(definition.settings["operation"])
        authorize_operation(
            definition,
            arguments,
            context,
            transport="onedrive",
            write=False,
            schema=_schema("onedrive", operation),
        )
        secret = credential(definition, self.environ)
        session = _Session(self)
        headers = {"Authorization": "Bearer " + secret}
        root = str(definition.settings["root_item_id"])
        drive = str(definition.settings["drive_id"])
        base = ENDPOINTS["onedrive"] + "/drives/" + quote(drive, safe="") + "/items/"
        fields = "id,name,size,eTag,parentReference,file,folder,remoteItem,lastModifiedDateTime"
        path = relative_path(arguments.get("path", ""), empty=operation != "read")
        url = base + quote(root, safe="") + (":/" + quote(path, safe="/") if path else "")

        def fetch(target: str) -> dict[str, Any]:
            return session.json(
                "GET",
                target,
                headers=headers,
                params={"$select": fields},
                expected=frozenset({200}),
            )

        def verify(item: dict[str, Any]) -> None:
            visited: set[str] = set()
            current = item
            for _ in range(20):
                identifier = _identifier(current.get("id"))
                reference = _object(current.get("parentReference"))
                if (
                    "remoteItem" in current
                    or reference.get("driveId") != drive
                    or identifier in visited
                ):
                    raise AuthorizationError("OneDrive item is not inside the permitted drive tree")
                if identifier == root:
                    if "folder" not in current:
                        raise AuthorizationError("OneDrive configured root must be a folder")
                    return
                visited.add(identifier)
                parent_id = _identifier(reference.get("id"))
                current = fetch(base + quote(parent_id, safe=""))
                if current.get("id") != parent_id:
                    raise ToolExecutionError("OneDrive returned an unexpected parent item")
            raise AuthorizationError("OneDrive folder ancestry exceeded the configured depth limit")

        item = fetch(url)
        verify(item)
        identifier = _identifier(item.get("id"))
        if operation == "metadata":
            result = _projection(item, ("id", "name", "size", "eTag", "lastModifiedDateTime"))
            result["kind"] = "folder" if "folder" in item else "file"
            return dict(redact(result, (secret,)))
        if operation == "list":
            if "folder" not in item:
                raise ToolCatalogError("OneDrive listing requires a folder")
            page = arguments.get("page", 1)
            initial = base + quote(identifier, safe="") + "/children"
            current_url = initial
            for index in range(1, page + 1):
                data = session.json(
                    "GET",
                    current_url,
                    headers=headers,
                    params={"$select": fields, "$top": 100} if index == 1 else None,
                    expected=frozenset({200}),
                )
                entries = _items(data.get("value"), maximum=100)
                for child in entries:
                    parent = _object(child.get("parentReference"))
                    if (
                        "remoteItem" in child
                        or parent.get("driveId") != drive
                        or parent.get("id") != identifier
                    ):
                        raise AuthorizationError(
                            "OneDrive listing includes an out-of-root shortcut"
                        )
                next_link = data.get("@odata.nextLink")
                if index == page or not next_link:
                    verify(fetch(base + quote(identifier, safe="")))
                    result = {
                        "entries": [
                            _projection(child, ("id", "name", "size", "eTag"))
                            | {"kind": "folder" if "folder" in child else "file"}
                            for child in entries
                        ]
                        if index == page
                        else [],
                        "page": page,
                        "next_page": page + 1 if next_link and page < 10 else None,
                        "more_available": bool(next_link),
                    }
                    return dict(redact(result, (secret,)))
                if not isinstance(next_link, str):
                    raise ToolExecutionError("OneDrive returned an invalid page link")
                parsed = urlsplit(next_link)
                if (
                    parsed.scheme != "https"
                    or parsed.netloc != "graph.microsoft.com"
                    or parsed.path != urlsplit(initial).path
                    or parsed.fragment
                    or any(ord(char) < 32 or char == "\\" for char in next_link)
                ):
                    raise ToolExecutionError("OneDrive page link escaped the requested folder")
                current_url = next_link
        if (
            "file" not in item
            or not isinstance(item.get("size"), int)
            or not 0 <= item["size"] <= MAX_FILE_BYTES
            or not isinstance(item.get("eTag"), str)
        ):
            raise ToolExecutionError(
                "OneDrive reads require a regular file up to 256 KiB with an ETag"
            )
        response = session.request(
            "GET",
            base + quote(identifier, safe="") + "/content",
            headers=headers,
            expected=frozenset({200, 302}),
        )
        raw = session.download(response, definition.settings["download_hosts"], secret)
        latest = fetch(base + quote(identifier, safe=""))
        verify(latest)
        if latest.get("id") != identifier or latest.get("eTag") != item["eTag"]:
            raise ToolExecutionError("OneDrive file changed while reading; read it again")
        result = {
            **_projection(item, ("id", "name", "eTag")),
            **_content(raw, arguments.get("encoding", "text"), secret),
        }
        return dict(redact(result, (secret,)))
