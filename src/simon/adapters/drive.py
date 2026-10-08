"""Bounded Drive/Docs/Sheets calls. No arbitrary hosts, redirects, or automatic write retries."""

import hashlib
import io
import json
import mimetypes
import re
import stat
import zipfile
import zlib
from time import monotonic
from typing import Any, NoReturn
from urllib.parse import quote
from uuid import uuid4
from xml.etree import ElementTree
from xml.parsers import expat

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
DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
SHEET = "application/vnd.google-apps.spreadsheet"
MAX_FILE = 10 * 1024 * 1024
MAX_TEXT = 200000
MAX_DOCX_XML = 4 * 1024 * 1024
MAX_DOCX_EXPANDED = 20 * 1024 * 1024
MAX_DOCX_ENTRIES = 512
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


def _docx_xml(raw: bytes) -> ElementTree.Element:
    """Parse bounded package XML without DTDs, entities, or external resolution."""
    builder = ElementTree.TreeBuilder()
    parser = expat.ParserCreate(namespace_separator="}")
    depth, nodes = 0, 0

    def qualified(name: str) -> str:
        return "{" + name if "}" in name else name

    def start(name: str, attributes: dict[str, str]) -> None:
        nonlocal depth, nodes
        depth += 1
        nodes += 1
        if depth > 64 or nodes > 50000:
            raise ValidationError("DOCX XML exceeds the document complexity limit.")
        builder.start(qualified(name), {qualified(key): value for key, value in attributes.items()})

    def end(name: str) -> None:
        nonlocal depth
        builder.end(qualified(name))
        depth -= 1

    def reject_declaration(*_: Any) -> NoReturn:
        raise ValidationError("DOCX XML declarations and entities are unsupported.")

    parser.StartElementHandler = start
    parser.EndElementHandler = end
    parser.CharacterDataHandler = builder.data
    parser.StartDoctypeDeclHandler = reject_declaration
    parser.EntityDeclHandler = reject_declaration
    parser.ExternalEntityRefHandler = reject_declaration
    parser.SetParamEntityParsing(expat.XML_PARAM_ENTITY_PARSING_NEVER)
    try:
        parser.Parse(raw, True)
        return builder.close()
    except (expat.ExpatError, ValueError):
        raise ValidationError("The DOCX contains invalid document XML.") from None


def docx_text(raw: bytes) -> str:
    """Read only the main document's paragraphs/tables; never extract files or follow links."""
    if len(raw) > MAX_FILE:
        raise ValidationError("The DOCX exceeds the 10 MB file limit.")
    if not raw.startswith(b"PK\x03\x04"):
        raise ValidationError("This file is not a readable, uncorrupted DOCX document.")
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            members = archive.infolist()
            if len(members) > MAX_DOCX_ENTRIES:
                raise ValidationError("The DOCX contains too many package entries.")
            seen: set[str] = set()
            expanded = 0
            for member in members:
                name = member.orig_filename.rstrip("/")
                key = name.casefold()
                if (
                    not name
                    or "\\" in name
                    or ":" in name
                    or any(ord(char) < 32 for char in name)
                    or any(part in {"", ".", ".."} for part in name.split("/"))
                    or key in seen
                ):
                    raise ValidationError("The DOCX contains unsafe or duplicate package paths.")
                seen.add(key)
                if (
                    member.flag_bits & 1
                    or stat.S_IFMT(member.external_attr >> 16)
                    not in {0, stat.S_IFREG, stat.S_IFDIR}
                    or member.compress_type not in {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}
                ):
                    raise ValidationError("Encrypted or unsupported DOCX entries cannot be read.")
                if (
                    "vba" in key.rsplit("/", 1)[-1]
                    or key.startswith(("word/activex/", "word/embeddings/"))
                    or key.endswith(
                        (".exe", ".dll", ".com", ".scr", ".js", ".vbs", ".ps1", ".bat", ".cmd")
                    )
                ):
                    raise ValidationError(
                        "DOCX macros, executable or embedded objects are unsupported."
                    )
                expanded += member.file_size
                if (
                    expanded > MAX_DOCX_EXPANDED
                    or member.file_size > MAX_FILE
                    or member.file_size > max(1024 * 1024, member.compress_size * 200)
                ):
                    raise ValidationError(
                        "DOCX exceeds extraction size or compression-ratio limits."
                    )

            def part(name: str) -> ElementTree.Element:
                member = archive.getinfo(name)
                if member.file_size > MAX_DOCX_XML:
                    raise ValidationError("DOCX XML exceeds the document size limit.")
                with archive.open(member) as stream:
                    content = stream.read(MAX_DOCX_XML + 1)
                if len(content) > MAX_DOCX_XML:
                    raise ValidationError("DOCX XML exceeds the document size limit.")
                return _docx_xml(content)

            types = part("[Content_Types].xml")
            package = "{http://schemas.openxmlformats.org/package/2006/content-types}"
            main_types = [
                item.get("ContentType")
                for item in types.findall(package + "Override")
                if item.get("PartName") == "/word/document.xml"
            ]
            if types.tag != package + "Types" or main_types != [DOCX + ".main+xml"]:
                raise ValidationError("Only standard, non-macro DOCX documents can be read.")
            if any(
                marker in item.get("ContentType", "").casefold()
                for item in types
                for marker in ("macroenabled", "vba", "activex", "oleobject")
            ):
                raise ValidationError(
                    "DOCX macros, executable or embedded objects are unsupported."
                )
            document = part("word/document.xml")
    except (
        zipfile.BadZipFile,
        zipfile.LargeZipFile,
        KeyError,
        OSError,
        RuntimeError,
        EOFError,
        zlib.error,
    ):
        raise ValidationError("This file is not a readable, uncorrupted DOCX document.") from None

    namespace = document.tag.removesuffix("document")
    if (
        namespace
        not in {
            "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}",
            "{http://purl.oclc.org/ooxml/wordprocessingml/main}",
        }
        or document.tag != namespace + "document"
    ):
        raise ValidationError("The DOCX has an unsupported document structure.")
    body = document.find(namespace + "body")
    if body is None:
        raise ValidationError("The DOCX has no main document body.")
    parts: list[str] = []
    characters = 0

    def append(text: str) -> None:
        nonlocal characters
        characters += len(text)
        if characters > MAX_TEXT:
            raise ValidationError("Document text exceeds 200,000 characters. Use a smaller file.")
        if text:
            parts.append(text)

    def visit(element: ElementTree.Element) -> None:
        nonlocal characters
        kind = element.tag.removeprefix(namespace) if element.tag.startswith(namespace) else ""
        if element.tag in {namespace + "del", namespace + "moveFrom"}:
            return
        if element.tag == namespace + "t":
            append(element.text or "")
        elif element.tag == namespace + "tab":
            append("\t")
        elif element.tag in {namespace + "br", namespace + "cr"}:
            append("\n")
        else:
            for child in element:
                visit(child)
            if kind in {"tc", "tr"} and parts and parts[-1] in {"\n", "\t"}:
                characters -= len(parts.pop())
            if kind in {"p", "tr"}:
                append("\n")
            elif kind == "tc":
                append("\t")

    visit(body)
    return "".join(parts).rstrip()


class DriveAPI:
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
                    DriveAPI.doc_text(cell.get("content", [])) for cell in row.get("tableCells", [])
                )
        return "".join(parts)

    def read(self, token: str, metadata: dict[str, Any]) -> dict[str, Any]:
        identifier, mime = file_id(metadata["id"]), metadata["mimeType"]
        result = {**metadata, "revision": str(metadata.get("version", ""))}
        if mime == DOC:
            try:
                document = self.json(
                    "GET",
                    f"{DOCS}/{identifier}",
                    token,
                    params={"includeTabsContent": "true"},
                    limit=2 * 1024 * 1024,
                )
            except DriveError as error:
                if error.status != 403:
                    raise
                # Drive may permit a read-only export when the Docs API is disabled.
                # Reuse the authorized file ID and token; exports provide no Docs revision.
                raw, _ = self.request(
                    "GET",
                    f"{FILES}/{identifier}/export",
                    token,
                    params={"mimeType": "text/plain"},
                    limit=MAX_FILE,
                )
                try:
                    text = raw.decode("utf-8-sig")
                except UnicodeError:
                    raise ValidationError(
                        "The exported document is not UTF-8 text. Open it in Drive."
                    ) from None
                if len(text) > MAX_TEXT:
                    raise ValidationError(
                        "Document text exceeds 200,000 characters. Use a smaller file."
                    ) from None
                result.update(
                    text=text,
                    tabs=[],
                    revision="",
                    exported=True,
                    read_only=True,
                    content_note="Read-only text exported from Drive. Document editing requires "
                    "Docs API access and a fresh document read.",
                )
                return result
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
                content_note="Read a specific sheet range.",
            )
        elif mime == DOCX or metadata.get("name", "").lower().endswith(".docx"):
            result.update(
                text=docx_text(self.download(token, identifier)),
                extracted=True,
                read_only=True,
                revision="",
                content_note="Read-only DOCX main-document text, including paragraphs and tables. "
                "Images, headers, footers, comments and separate notes are not included. "
                "Binary document editing is unsupported.",
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
        if current.get("extracted"):
            raise ValidationError(
                "Extracted DOCX text is read-only. Binary document editing is unsupported."
            )
        if current.get("read_only") or current.get("exported"):
            raise ValidationError(
                "This read-only export cannot be edited. Enable Docs API access and read "
                "the document again before editing."
            )
        identifier = file_id(current["id"])
        mime = current["mimeType"]
        if mime == DOC:
            tabs = current["tabs"]
            selected = [tab for tab in tabs if tab["tab_id"] == tab_id] if tab_id else tabs
            if len(selected) != 1:
                raise ValidationError("Select a document tab from the read result.")
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
                "Use the Sheets operation for spreadsheet cells. Binary editing is unsupported."
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
