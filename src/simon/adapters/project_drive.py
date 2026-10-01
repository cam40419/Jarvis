"""Bounded Drive/Docs/Sheets calls. No arbitrary hosts, redirects, or automatic write retries."""

import hashlib
import json
import mimetypes
import re
from time import monotonic
from typing import Any
from urllib.parse import quote
from uuid import uuid4

import httpx

from simon.adapters.google import ConnectedError
from simon.domain.connected_tools import GoogleItem
from simon.domain.errors import ValidationError

FILES = "https://www.googleapis.com/drive/v3/files"
UPLOAD = "https://www.googleapis.com/upload/drive/v3/files"
DOCS = "https://docs.googleapis.com/v1/documents"
SHEETS = "https://sheets.googleapis.com/v4/spreadsheets"
FOLDER = "application/vnd.google-apps.folder"
DOC = "application/vnd.google-apps.document"
SHEET = "application/vnd.google-apps.spreadsheet"
MAX_FILE = 10 * 1024 * 1024
MAX_TEXT = 200000
FIELDS = (
    "id,name,mimeType,parents,trashed,version,modifiedTime,size,md5Checksum,"
    "capabilities(canEdit,canTrash,canAddChildren)"
)


class DriveError(ConnectedError):
    def __init__(self, message: str, *, status: int = 0, unknown: bool = False) -> None:
        super().__init__(message, unknown=unknown)
        self.status = status


def file_id(value: str) -> str:
    return GoogleItem(id=value).id


def text_type(mime: str, name: str = "") -> bool:
    return (
        mime.startswith("text/")
        or mime
        in {
            "application/json",
            "application/xml",
            "application/javascript",
            "application/x-python",
            "application/yaml",
            "application/toml",
        }
        or name.lower().endswith((".md", ".py", ".js", ".ts", ".json", ".yaml", ".yml", ".toml"))
    )


def text_revision(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


class ProjectDriveAPI:
    def request(
        self,
        method: str,
        url: str,
        token: str,
        *,
        params: dict[str, str] | None = None,
        body: dict[str, Any] | None = None,
        content: bytes | None = None,
        headers: dict[str, str] | None = None,
        limit: int = MAX_FILE,
    ) -> tuple[bytes, httpx.Headers]:
        write = method not in {"GET", "HEAD"}
        try:
            started = monotonic()
            with (
                httpx.Client(timeout=20, follow_redirects=False, trust_env=False) as client,
                client.stream(
                    method,
                    url,
                    params=params,
                    json=body,
                    content=content,
                    headers={"Authorization": f"Bearer {token}", **(headers or {})},
                ) as response,
            ):
                status = response.status_code
                if not 200 <= status < 300:
                    message = {
                        401: "Reconnect Google in Connections to renew project file access.",
                        403: "Google denied access. Enable Drive, Docs and Sheets APIs and "
                        "reconnect Google with Drive editing permission.",
                        404: "The Drive file or folder is unavailable or no longer shared.",
                        409: "Drive reported a conflict. Refresh before retrying.",
                        412: "File changed in Drive. Read it again before editing.",
                        429: "Google's request limit was reached. Try again later.",
                    }.get(status, "Google could not complete the project file request.")
                    raise DriveError(
                        message,
                        status=status,
                        unknown=write and (status >= 500 or status == 408),
                    )
                data = bytearray()
                for chunk in response.iter_bytes():
                    data.extend(chunk)
                    if len(data) > limit or monotonic() - started > 30:
                        raise DriveError("The file exceeds Simon's transfer limit.", unknown=write)
                return bytes(data), response.headers
        except httpx.HTTPError:
            raise DriveError(
                "Google could not be reached. Check the file before repeating a write.",
                unknown=write,
            ) from None

    def json(self, method: str, url: str, token: str, **kwargs: Any) -> dict[str, Any]:
        data, headers = self.request(method, url, token, **kwargs)
        try:
            result = json.loads(data)
            if not isinstance(result, dict):
                raise ValueError("object required")
        except (ValueError, TypeError):
            raise DriveError(
                "Google returned an invalid file response.", unknown=method != "GET"
            ) from None
        if headers.get("etag"):
            result["_etag"] = headers["etag"]
        return result

    def metadata(self, token: str, identifier: str) -> dict[str, Any]:
        result = self.json(
            "GET",
            f"{FILES}/{file_id(identifier)}",
            token,
            params={"fields": FIELDS, "supportsAllDrives": "true"},
        )
        if result.get("trashed"):
            raise DriveError("The Drive file is in trash.", status=404)
        result["url"] = (
            f"https://drive.google.com/drive/folders/{identifier}"
            if result.get("mimeType") == FOLDER
            else f"https://drive.google.com/file/d/{identifier}/view"
        )
        return result

    def list_files(
        self,
        token: str,
        folder: str,
        query: str = "",
        page: str = "",
        *,
        folders_only: bool = False,
        search_all: bool = False,
    ) -> dict[str, Any]:
        escaped = query.replace("\\", "\\\\").replace("'", "\\'")
        result = self.json(
            "GET",
            FILES,
            token,
            params={
                "q": (
                    "trashed = false"
                    if search_all
                    else f"'{file_id(folder)}' in parents and trashed = false"
                )
                + (f" and mimeType = '{FOLDER}'" if folders_only else "")
                + (f" and name contains '{escaped}'" if escaped else ""),
                "fields": "nextPageToken,files(" + FIELDS + ")",
                "pageSize": "100",
                "pageToken": page,
                "orderBy": "folder,name",
                "supportsAllDrives": "true",
                "includeItemsFromAllDrives": "true",
            },
        )
        for item in result.get("files", []):
            identifier = file_id(item["id"])
            item["url"] = (
                f"https://drive.google.com/drive/folders/{identifier}"
                if item.get("mimeType") == FOLDER
                else f"https://drive.google.com/file/d/{identifier}/view"
            )
        return {
            "files": result.get("files", []),
            "next_page_token": result.get("nextPageToken", ""),
        }

    def trash(self, token: str, current: dict[str, Any]) -> dict[str, Any]:
        if not current.get("capabilities", {}).get("canTrash"):
            raise ValidationError("Google does not allow this item to be moved to trash.")
        result = self.json(
            "PATCH",
            f"{FILES}/{file_id(current['id'])}",
            token,
            params={"fields": FIELDS, "supportsAllDrives": "true"},
            body={"trashed": True},
            headers={"If-Match": current["_etag"]} if current.get("_etag") else None,
        )
        if not result.get("trashed"):
            raise DriveError("Drive did not confirm the item was moved to trash.", unknown=True)
        return result

    def generate_id(self, token: str) -> str:
        result = self.json(
            "GET",
            FILES + "/generateIds",
            token,
            params={"count": "1", "space": "drive", "type": "files"},
        )
        return file_id(result["ids"][0])

    def create(
        self,
        token: str,
        name: str,
        parent: str | None,
        content: bytes | None,
        mime: str,
        identifier: str | None,
        operation: str,
    ) -> dict[str, Any]:
        metadata: dict[str, Any] = {
            "name": name,
            "mimeType": mime,
            "appProperties": {"simonOperation": operation},
        }
        if parent:
            metadata["parents"] = [file_id(parent)]
        if identifier:
            metadata["id"] = file_id(identifier)
        params = {"fields": FIELDS, "supportsAllDrives": "true"}
        if content is None or (not content and mime in {DOC, SHEET}):
            return self.json("POST", FILES, token, body=metadata, params=params)
        if len(content) > MAX_FILE:
            raise ValidationError("Files must be at most 10 MB.")
        boundary = "simon_" + uuid4().hex
        media = "text/plain" if mime == DOC else "text/csv" if mime == SHEET else mime
        data = (
            f"--{boundary}\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n".encode()
            + json.dumps(metadata).encode()
            + f"\r\n--{boundary}\r\nContent-Type: {media}\r\n\r\n".encode()
            + content
            + f"\r\n--{boundary}--\r\n".encode()
        )
        return self.json(
            "POST",
            UPLOAD,
            token,
            params={**params, "uploadType": "multipart"},
            content=data,
            headers={"Content-Type": f"multipart/related; boundary={boundary}"},
        )

    def download(self, token: str, identifier: str) -> bytes:
        return self.request(
            "GET",
            f"{FILES}/{file_id(identifier)}",
            token,
            params={"alt": "media", "supportsAllDrives": "true"},
        )[0]

    @staticmethod
    def doc_text(elements: list[dict[str, Any]]) -> str:
        parts: list[str] = []
        for element in elements:
            paragraph = element.get("paragraph", {})
            parts.extend(
                item.get("textRun", {}).get("content", "") for item in paragraph.get("elements", [])
            )
            for row in element.get("table", {}).get("tableRows", []):
                parts.extend(
                    ProjectDriveAPI.doc_text(cell.get("content", []))
                    for cell in row.get("tableCells", [])
                )
        return "".join(parts)

    def read(self, token: str, metadata: dict[str, Any]) -> dict[str, Any]:
        identifier, mime = file_id(metadata["id"]), metadata["mimeType"]
        result = {**metadata, "revision": str(metadata.get("version", ""))}
        if mime == DOC:
            document = self.json(
                "GET",
                f"{DOCS}/{identifier}",
                token,
                params={"includeTabsContent": "true"},
                limit=2 * 1024 * 1024,
            )
            tabs: list[dict[str, str]] = []

            def collect(values: list[dict[str, Any]], depth: int = 0) -> None:
                if depth > 10:
                    raise ValidationError("This document has too many nested tabs.")
                for tab in values:
                    tabs.append(
                        {
                            "tab_id": tab["documentTab"]["body"].get("tabId", "")
                            or tab.get("tabProperties", {}).get("tabId", ""),
                            "title": tab.get("tabProperties", {}).get("title", ""),
                            "text": self.doc_text(tab["documentTab"]["body"].get("content", [])),
                        }
                    )
                    collect(tab.get("childTabs", []), depth + 1)

            collect(document.get("tabs", []))
            if not tabs:
                tabs = [
                    {
                        "tab_id": "",
                        "title": "",
                        "text": self.doc_text(
                            document.get("body", {}).get("content", []),
                        ),
                    }
                ]
            text = "\n".join(tab["text"] for tab in tabs)
            if len(text) > MAX_TEXT:
                raise ValidationError(
                    "Document text exceeds 200,000 characters. Use a smaller file."
                )
            result.update(text=text, tabs=tabs, revision=document.get("revisionId", ""))
        elif mime == SHEET:
            book = self.json(
                "GET", f"{SHEETS}/{identifier}", token, params={"fields": "sheets.properties"}
            )
            result.update(
                sheets=book.get("sheets", []),
                content_note="Use project_sheet_read for a specific sheet range.",
            )
        elif text_type(mime, metadata.get("name", "")):
            raw = self.download(token, identifier)
            try:
                text = raw.decode("utf-8-sig")
            except UnicodeError:
                raise ValidationError("This file is not UTF-8 text. Open it in Drive.") from None
            if len(text) > MAX_TEXT:
                raise ValidationError("File text exceeds 200,000 characters.")
            result.update(text=text, revision=text_revision(text))
        else:
            result["content_note"] = (
                "Stored in Drive. This binary file can be opened or downloaded."
            )
        return result

    def edit(
        self,
        token: str,
        current: dict[str, Any],
        old_text: str,
        new_text: str,
        tab_id: str | None,
    ) -> dict[str, Any]:
        identifier = file_id(current["id"])
        mime = current["mimeType"]
        if mime == DOC:
            tabs = current["tabs"]
            selected = [tab for tab in tabs if tab["tab_id"] == tab_id] if tab_id else tabs
            if len(selected) != 1:
                raise ValidationError("Select the document tab returned by project_file_read.")
            tab = selected[0]
            if old_text and tab["text"].count(old_text) != 1:
                raise ValidationError("The exact old text must occur once in the selected tab.")
            if old_text:
                update: dict[str, Any] = {
                    "replaceAllText": {
                        "containsText": {"text": old_text, "matchCase": True},
                        "replaceText": new_text,
                        **({"tabsCriteria": {"tabIds": [tab["tab_id"]]}} if tab["tab_id"] else {}),
                    }
                }
            else:
                update = {
                    "insertText": {
                        "text": new_text,
                        "endOfSegmentLocation": {
                            **({"tabId": tab["tab_id"]} if tab["tab_id"] else {}),
                        },
                    }
                }
            self.json(
                "POST",
                f"{DOCS}/{identifier}:batchUpdate",
                token,
                body={
                    "writeControl": {"requiredRevisionId": current["revision"]},
                    "requests": [update],
                },
            )
        elif "text" in current and text_type(mime, current.get("name", "")):
            if old_text and current["text"].count(old_text) != 1:
                raise ValidationError("The exact old text must occur once. Read the file again.")
            text = (
                current["text"].replace(old_text, new_text, 1)
                if old_text
                else current["text"] + new_text
            )
            if len(text) > MAX_TEXT:
                raise ValidationError("Edited file exceeds 200,000 characters.")
            self.json(
                "PATCH",
                f"{UPLOAD}/{identifier}",
                token,
                params={"uploadType": "media", "fields": FIELDS, "supportsAllDrives": "true"},
                content=text.encode(),
                headers={
                    "Content-Type": mime,
                    **({"If-Match": current["_etag"]} if current.get("_etag") else {}),
                },
            )
        else:
            raise ValidationError(
                "Use project_sheet_write for Sheets. Binary editing is unsupported."
            )
        return {"id": identifier, "url": f"https://drive.google.com/file/d/{identifier}/view"}

    def sheet_read(self, token: str, identifier: str, cell_range: str) -> dict[str, Any]:
        result = self.json(
            "GET",
            f"{SHEETS}/{file_id(identifier)}/values/{quote(cell_range, safe='')}",
            token,
            params={"valueRenderOption": "FORMULA"},
            limit=1024 * 1024,
        )
        values = result.get("values", [])
        if len(values) > 100 or sum(len(row) for row in values) > 2000:
            raise ValidationError("Read at most 100 rows and 2,000 cells at once.")
        return {
            "range": result.get("range", cell_range),
            "values": values,
            "revision": text_revision(json.dumps(values, ensure_ascii=False)),
        }

    def sheet_write(
        self,
        token: str,
        identifier: str,
        cell_range: str,
        values: list[list[Any]],
    ) -> dict[str, Any]:
        match = re.fullmatch(
            r"(?:.+!)?([A-Za-z]+)([1-9][0-9]*)(?::([A-Za-z]+)([1-9][0-9]*))?", cell_range
        )
        if not match:
            raise ValidationError("Write an explicit bounded A1 range, such as Sheet1!A1:C10.")

        def column(value: str) -> int:
            number = 0
            for char in value.upper():
                number = number * 26 + ord(char) - 64
            return number

        first_col, first_row, last_col, last_row = match.groups()
        rows = int(last_row or first_row) - int(first_row) + 1
        columns = column(last_col or first_col) - column(first_col) + 1
        if len(values) > rows or any(len(row) > columns for row in values):
            raise ValidationError(
                "Values extend beyond the range that was read. Read a larger range first."
            )
        if sum(len(row) for row in values) > 2000:
            raise ValidationError("Write at most 2,000 cells at once.")
        return self.json(
            "PUT",
            f"{SHEETS}/{file_id(identifier)}/values/{quote(cell_range, safe='')}",
            token,
            params={"valueInputOption": "RAW"},
            body={"range": cell_range, "majorDimension": "ROWS", "values": values},
        )

    def rename(self, token: str, current: dict[str, Any], name: str) -> dict[str, Any]:
        return self.json(
            "PATCH",
            f"{FILES}/{file_id(current['id'])}",
            token,
            params={"fields": FIELDS, "supportsAllDrives": "true"},
            body={"name": name},
            headers={"If-Match": current["_etag"]} if current.get("_etag") else {},
        )

    @staticmethod
    def media(name: str, format: str) -> str:
        return {"google_doc": DOC, "google_sheet": SHEET, "folder": FOLDER}.get(
            format,
            mimetypes.guess_type(name)[0] or "text/plain",
        )
