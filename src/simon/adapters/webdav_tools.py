"""Root-confined WebDAV listing and bounded files with conditional writes."""

from __future__ import annotations

import base64
import binascii
import re
from collections.abc import Sequence
from typing import Any
from urllib.parse import quote, unquote, urlsplit
from uuid import UUID
from xml.etree import ElementTree

from simon.adapters.cloud_storage_tools import ConnectedStorageTransport
from simon.adapters.optional_http import (
    authorize_operation,
    connection_endpoint,
    credential,
    redact,
    redact_bytes,
    relative_path,
)
from simon.domain.tool_catalog import (
    ToolCatalogError,
    ToolDefinition,
    ToolExecutionContext,
    ToolExecutionError,
)

MAX_FILE_BYTES = 262144
_DESCRIPTIONS = {
    "list": "List immediate children under an explicitly granted WebDAV storage root.",
    "read": "Read a WebDAV file up to 256 KiB as UTF-8 text or base64, including its ETag.",
    "write": "Create or update a WebDAV file using an expected ETag; never overwrite blindly.",
}
_PROPFIND = (
    b'<?xml version="1.0" encoding="utf-8"?>'
    b'<d:propfind xmlns:d="DAV:"><d:prop><d:resourcetype/><d:getetag/>'
    b"<d:getcontentlength/><d:getcontenttype/><d:getlastmodified/></d:prop></d:propfind>"
)


def _schema(operation: str) -> dict[str, Any]:
    properties: dict[str, Any] = {"path": {"type": "string", "maxLength": 1000}}
    required = [] if operation == "list" else ["path"]
    if operation != "list":
        properties["path"]["minLength"] = 1
        properties["encoding"] = {"enum": ["text", "base64"]}
    if operation == "write":
        properties.update(
            {
                "content": {"type": "string", "maxLength": 349528},
                "expected_etag": {"type": "string", "maxLength": 512},
            }
        )
        required.extend(["content", "expected_etag"])
    return {
        "type": "object",
        "properties": properties,
        "required": required,
        "additionalProperties": False,
    }


def webdav_tool_definitions(
    *,
    enabled: bool = False,
    workspace_id: UUID | None = None,
    actor_ids: Sequence[UUID] = (),
    endpoint: str | None = None,
    username: str = "",
    credential_env: str = "SIMON_WEBDAV_PASSWORD",
) -> tuple[ToolDefinition, ...]:
    configured = bool(workspace_id and actor_ids and endpoint and username)
    return tuple(
        ToolDefinition(
            id="webdav." + operation,
            description=description,
            categories=frozenset({"files", "storage"}),
            capabilities=frozenset({"webdav." + operation}),
            transport="webdav",
            enabled=enabled,
            configured=configured,
            endpoint=endpoint,
            credential_env=credential_env,
            required_scopes=frozenset({"jobs:write" if operation == "write" else "jobs:read"}),
            side_effect=operation == "write",
            action_policy="write" if operation == "write" else "read",
            input_schema=_schema(operation),
            output_schema={"type": "object"},
            settings={
                "operation": operation,
                "workspace_id": str(workspace_id) if workspace_id else "",
                "actor_ids": [str(item) for item in actor_ids],
                "username": username,
                "network": True,
            },
        )
        for operation, description in _DESCRIPTIONS.items()
    )


def _etag(value: str, *, writing: bool = False) -> str:
    if (
        len(value) > 512
        or any(ord(char) < 32 or ord(char) == 127 for char in value)
        or (writing and re.fullmatch(r'"[^"\r\n]*"', value) is None)
    ):
        raise ToolCatalogError("Expected revision must be one strong quoted ETag")
    return value


def _listing(content: bytes, endpoint: str, path: str) -> list[dict[str, Any]]:
    try:
        text = content.decode("utf-8-sig")
        if "<!DOCTYPE" in text.upper() or "<!ENTITY" in text.upper() or "\x00" in text:
            raise ValueError
        tree = ElementTree.fromstring(text)
        if tree.tag != "{DAV:}multistatus":
            raise ValueError
    except (ValueError, ElementTree.ParseError):
        raise ToolExecutionError("WebDAV returned invalid or unsupported XML") from None
    root = urlsplit(endpoint)
    root_path = unquote(root.path).rstrip("/")
    requested_path = root_path + ("/" + path if path else "")
    entries: list[dict[str, Any]] = []
    responses = tree.findall("{DAV:}response")
    if len(responses) > 1001:
        raise ToolExecutionError("WebDAV directories are limited to 1000 entries; use a subfolder")
    for item in responses:
        href = item.findtext("{DAV:}href")
        if not href:
            raise ToolExecutionError("WebDAV returned an entry without a path")
        location = urlsplit(href)
        if (
            location.query
            or location.fragment
            or location.username
            or location.password
            or (location.scheme and location.scheme != root.scheme)
            or (location.netloc and location.netloc != root.netloc)
            or not location.path.startswith("/")
        ):
            raise ToolExecutionError("WebDAV returned a path outside the requested directory")
        full_path = unquote(location.path).rstrip("/")
        if full_path == requested_path:
            continue
        if not full_path.startswith(requested_path + "/"):
            raise ToolExecutionError("WebDAV returned a path outside the requested directory")
        child = full_path[len(requested_path) + 1 :]
        if "/" in child:
            raise ToolExecutionError("WebDAV returned recursive entries for a shallow listing")
        child = relative_path(child)
        properties = None
        for propstat in item.findall("{DAV:}propstat"):
            if re.fullmatch(r"HTTP/\d(?:\.\d)? 200(?: .*)?", propstat.findtext("{DAV:}status", "")):
                properties = propstat.find("{DAV:}prop")
                break
        if properties is None:
            continue
        raw_size = properties.findtext("{DAV:}getcontentlength", "0")
        if not raw_size.isdigit() or len(raw_size) > 16:
            raise ToolExecutionError("WebDAV returned an invalid file size")
        entries.append(
            {
                "name": child,
                "path": (path + "/" if path else "") + child,
                "kind": "folder"
                if properties.find("{DAV:}resourcetype/{DAV:}collection") is not None
                else "file",
                "bytes": int(raw_size),
                "etag": _etag(properties.findtext("{DAV:}getetag", "")),
                "media_type": properties.findtext("{DAV:}getcontenttype", "")[:200],
                "modified_at": properties.findtext("{DAV:}getlastmodified", "")[:100],
            }
        )
    return entries


class WebDAVTransport(ConnectedStorageTransport):
    def __call__(
        self,
        definition: ToolDefinition,
        arguments: dict[str, Any],
        context: ToolExecutionContext,
    ) -> dict[str, Any]:
        definition = self.resolve(definition, context)
        operation = definition.settings.get("operation")
        if not isinstance(operation, str) or operation not in _DESCRIPTIONS:
            raise ToolCatalogError("Unsupported WebDAV operation")
        write = operation == "write"
        authorize_operation(
            definition,
            arguments,
            context,
            transport="webdav",
            write=write,
            schema=_schema(operation),
        )
        endpoint = connection_endpoint(definition)
        secret = credential(definition, self.environ)
        username = definition.settings.get("username")
        if (
            not isinstance(username, str)
            or not username
            or len(username) > 200
            or any(ord(char) < 32 or ord(char) == 127 or char == ":" for char in username)
        ):
            raise ToolCatalogError("Configure a WebDAV service account username")
        encoded_auth = base64.b64encode((username + ":" + secret).encode("utf-8")).decode("ascii")
        secrets = (secret, encoded_auth)
        headers = {"Authorization": "Basic " + encoded_auth, "X-Requested-With": "XMLHttpRequest"}
        path = relative_path(arguments.get("path", ""), empty=operation == "list")
        url = endpoint + "/" + quote(path, safe="/")
        if operation == "list":
            headers.update({"Depth": "1", "Content-Type": "application/xml; charset=utf-8"})
            response = self.request(
                "PROPFIND", url, headers=headers, content=_PROPFIND, expected=frozenset({207})
            )
            return dict(
                redact(
                    {"path": path, "entries": _listing(response.content, endpoint, path)}, secrets
                )
            )
        encoding = arguments.get("encoding", "text")
        if operation == "read":
            response = self.request("GET", url, headers=headers, expected=frozenset({200}))
            raw = response.content
            if len(raw) > MAX_FILE_BYTES:
                raise ToolExecutionError("WebDAV files are limited to 256 KiB")
            raw, redacted = redact_bytes(raw, secrets)
            try:
                content = (
                    raw.decode("utf-8")
                    if encoding == "text"
                    else base64.b64encode(raw).decode("ascii")
                )
            except UnicodeError:
                raise ToolExecutionError(
                    "File is not UTF-8 text; request base64 encoding"
                ) from None
            return dict(
                redact(
                    {
                        "path": path,
                        "content": content,
                        "encoding": encoding,
                        "bytes": len(raw),
                        "etag": _etag(response.headers.get("etag", "")),
                        "redacted": redacted,
                        "media_type": response.headers.get(
                            "content-type", "application/octet-stream"
                        )[:200],
                    },
                    secrets,
                )
            )
        try:
            raw = (
                arguments["content"].encode("utf-8")
                if encoding == "text"
                else base64.b64decode(arguments["content"], validate=True)
            )
        except (binascii.Error, ValueError):
            raise ToolCatalogError("The file content is not valid base64") from None
        if len(raw) > MAX_FILE_BYTES:
            raise ToolCatalogError("WebDAV files are limited to 256 KiB")
        expected_etag = arguments["expected_etag"]
        if expected_etag:
            headers["If-Match"] = _etag(expected_etag, writing=True)
        else:
            headers["If-None-Match"] = "*"
        headers["Content-Type"] = (
            "text/plain; charset=utf-8" if encoding == "text" else ("application/octet-stream")
        )
        response = self.request(
            "PUT",
            url,
            headers=headers,
            content=raw,
            write=True,
            expected=frozenset({200, 201, 204}),
        )
        revision = response.headers.get("etag", response.headers.get("oc-etag", ""))
        try:
            revision = _etag(revision)
        except ToolCatalogError:
            raise ToolExecutionError(
                "WebDAV write succeeded but its ETag is invalid", unknown=True
            ) from None
        return dict(
            redact(
                {
                    "path": path,
                    "written": True,
                    "bytes": len(raw),
                    "etag": revision,
                    "created": response.status == 201,
                    "note": "Read the file to obtain an ETag before updating it"
                    if not revision
                    else "",
                },
                secrets,
            )
        )
