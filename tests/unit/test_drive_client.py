import io
import json
import stat
import zipfile

import httpx
import pytest

from simon.adapters.drive import DOC, DOCX, FOLDER, DriveAPI, DriveError
from simon.domain.errors import ValidationError


@pytest.fixture
def transport(monkeypatch):
    calls = []
    replies = []
    original = httpx.Client

    def handle(request):
        calls.append(request)
        status, data = replies.pop(0)
        if isinstance(data, bytes):
            return httpx.Response(status, content=data)
        return httpx.Response(status, json=data)

    monkeypatch.setattr(
        httpx, "Client", lambda **kwargs: original(transport=httpx.MockTransport(handle), **kwargs)
    )
    return DriveAPI(), calls, replies


def test_docs_edits_use_revision_and_selected_tab(transport):
    api, calls, replies = transport
    replies.append((200, {"replies": [{}]}))
    result = api.edit(
        "secret",
        {
            "id": "doc",
            "mimeType": DOC,
            "revision": "revision-one",
            "tabs": [{"tab_id": "tab1", "text": "Original text"}],
        },
        "Original",
        "Edited",
        "tab1",
    )
    assert result["id"] == "doc"
    body = json.loads(calls[0].content)
    assert body["writeControl"] == {"requiredRevisionId": "revision-one"}
    assert body["requests"][0]["replaceAllText"]["tabsCriteria"] == {"tabIds": ["tab1"]}
    assert calls[0].url.host == "docs.googleapis.com"
    with pytest.raises(ValidationError, match="occur once"):
        api.edit(
            "secret",
            {
                "id": "doc",
                "mimeType": DOC,
                "revision": "rev",
                "tabs": [{"tab_id": "tab1", "text": "twice twice"}],
            },
            "twice",
            "once",
            "tab1",
        )
    assert len(calls) == 1


def test_docs_forbidden_read_exports_same_file_as_read_only_utf8(transport):
    api, calls, replies = transport
    replies.extend([(403, {}), (200, "\ufeffProject café notes\n".encode())])
    result = api.read(
        "secret",
        {
            "id": "doc",
            "mimeType": DOC,
            "version": "drive-version",
            "capabilities": {"canEdit": True},
        },
    )
    assert result["text"] == "Project café notes\n"
    assert result["exported"] is True and result["read_only"] is True
    assert result["tabs"] == [] and result["revision"] == ""
    assert [(call.method, call.url.host, call.url.path) for call in calls] == [
        ("GET", "docs.googleapis.com", "/v1/documents/doc"),
        ("GET", "www.googleapis.com", "/drive/v3/files/doc/export"),
    ]
    assert dict(calls[1].url.params) == {"mimeType": "text/plain"}
    assert all(call.headers["authorization"] == "Bearer secret" for call in calls)
    with pytest.raises(ValidationError, match="read-only export cannot be edited"):
        api.edit("secret", result, "Project", "Changed", None)
    assert len(calls) == 2


def test_docs_success_keeps_revision_and_tabs_without_export(transport):
    api, calls, replies = transport
    replies.append(
        (
            200,
            {
                "revisionId": "docs-revision",
                "body": {
                    "content": [
                        {"paragraph": {"elements": [{"textRun": {"content": "Existing text"}}]}}
                    ]
                },
            },
        )
    )
    result = api.read("secret", {"id": "doc", "mimeType": DOC, "version": "drive-version"})
    assert result["revision"] == "docs-revision"
    assert result["tabs"] == [{"tab_id": "", "title": "", "text": "Existing text"}]
    assert not result.get("exported") and not result.get("read_only")
    assert len(calls) == 1


@pytest.mark.parametrize("status", [401, 404, 429, 503])
def test_docs_other_read_errors_do_not_attempt_export(transport, status):
    api, calls, replies = transport
    replies.append((status, {}))
    with pytest.raises(DriveError) as caught:
        api.read("secret", {"id": "doc", "mimeType": DOC})
    assert caught.value.status == status and not caught.value.unknown
    assert len(calls) == 1


@pytest.mark.parametrize("status", [302, 403, 404])
def test_docs_export_denial_or_redirect_is_not_retried(transport, status):
    api, calls, replies = transport
    replies.extend([(403, {}), (status, {"error": "secret provider detail"})])
    with pytest.raises(DriveError) as caught:
        api.read("secret", {"id": "doc", "mimeType": DOC})
    assert caught.value.status == status and not caught.value.unknown
    assert "secret" not in str(caught.value)
    assert len(calls) == 2


@pytest.mark.parametrize(
    "content,message",
    [(b"\xff", "not UTF-8"), (("é" * 200001).encode(), "200,000 characters")],
    ids=["invalid-utf8", "oversized-text"],
)
def test_docs_export_rejects_invalid_or_excessive_text(transport, content, message):
    api, calls, replies = transport
    replies.extend([(403, {}), (200, content)])
    with pytest.raises(ValidationError, match=message):
        api.read("secret", {"id": "doc", "mimeType": DOC})
    assert len(calls) == 2


def test_docs_export_enforces_transfer_limit(transport, monkeypatch):
    api, calls, replies = transport
    monkeypatch.setattr("simon.adapters.drive.MAX_FILE", 32)
    replies.extend([(403, {}), (200, b"x" * 33)])
    with pytest.raises(DriveError, match="transfer limit") as caught:
        api.read("secret", {"id": "doc", "mimeType": DOC})
    assert not caught.value.unknown and len(calls) == 2


def test_sheet_bounds_and_literal_values(transport):
    api, calls, replies = transport
    with pytest.raises(ValidationError, match="beyond"):
        api.sheet_write("secret", "sheet", "Sheet1!A1", [["one", "two"]])
    with pytest.raises(ValidationError, match="explicit"):
        api.sheet_write("secret", "sheet", "Sheet1!A:A", [["one"]])
    assert not calls
    replies.append((200, {"updatedCells": 2}))
    api.sheet_write("secret", "sheet", "Sheet1!A1:B1", [["=literal", "two"]])
    assert calls[0].url.params["valueInputOption"] == "RAW"
    assert json.loads(calls[0].content)["values"] == [["=literal", "two"]]


@pytest.mark.parametrize("status,unknown", [(403, False), (412, False), (408, True), (503, True)])
def test_write_failures_distinguish_uncertain_results(transport, status, unknown):
    api, calls, replies = transport
    replies.append((status, {"error": {"message": "secret provider detail"}}))
    with pytest.raises(DriveError) as caught:
        api.create("secret", "Folder", None, None, FOLDER, "generated", "operation")
    assert caught.value.unknown is unknown
    assert "secret" not in str(caught.value)
    assert len(calls) == 1


def test_multipart_create_keeps_id_parent_and_operation(transport):
    api, calls, replies = transport
    replies.append((200, {"id": "generated"}))
    api.create("secret", "notes.md", "folder", b"file content", "text/markdown", "generated", "op")
    request = calls[0]
    assert request.url.params["uploadType"] == "multipart"
    assert b'"parents": ["folder"]' in request.content
    assert b'"id": "generated"' in request.content
    assert b'"simonOperation": "op"' in request.content
    assert b"file content" in request.content


def test_folder_search_and_trash_use_drive_api_without_permanent_delete(transport):
    api, calls, replies = transport
    replies.append((200, {"files": [], "nextPageToken": "page-two"}))
    result = api.list_files("secret", "root", "Client's files", folders_only=True, search_all=True)
    assert result["next_page_token"] == "page-two"
    assert "mimeType" in calls[0].url.params["q"] and "in parents" not in calls[0].url.params["q"]
    assert "Client\\'s files" in calls[0].url.params["q"]
    replies.append((200, {"id": "old", "trashed": True}))
    api.trash("secret", {"id": "old", "_etag": "revision-etag", "capabilities": {"canTrash": True}})
    assert calls[1].method == "PATCH"
    assert json.loads(calls[1].content) == {"trashed": True}
    assert calls[1].headers["if-match"] == "revision-etag"


WORD_NAMESPACE = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"


def docx_package(body="<w:p><w:r><w:t>Brand strategy</w:t></w:r></w:p>", **options):
    document = options.get(
        "document",
        f'<w:document xmlns:w="{WORD_NAMESPACE}"><w:body>{body}</w:body></w:document>'.encode(),
    )
    content_types = options.get(
        "content_types",
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        f'<Override PartName="/word/document.xml" ContentType="{DOCX}.main+xml"/>'
        "</Types>",
    )
    destination = io.BytesIO()
    with zipfile.ZipFile(
        destination, "w", compression=options.get("compression", zipfile.ZIP_DEFLATED)
    ) as archive:
        archive.writestr("[Content_Types].xml", content_types)
        archive.writestr("word/document.xml", document)
        for name, content in options.get("extra", []):
            archive.writestr(name, content)
    return destination.getvalue()


def read_docx(transport, content, *, mime=DOCX, name="Brand strategy.docx"):
    api, _, replies = transport
    replies.append((200, content))
    return api.read(
        "linked-project-token",
        {"id": "brand-strategy", "name": name, "mimeType": mime, "version": "drive-version"},
    )


def test_docx_reads_paragraphs_tables_and_unicode_without_following_external_links(transport):
    api, calls, _ = transport
    content = docx_package(
        "<w:p><w:r><w:t>Brand café</w:t><w:tab/><w:t>Strategy</w:t></w:r></w:p>"
        "<w:p><w:r><w:t>First</w:t><w:br/><w:t>Second</w:t></w:r></w:p>"
        "<w:tbl><w:tr><w:tc><w:p><w:r><w:t>Audience</w:t></w:r></w:p></w:tc>"
        "<w:tc><w:p><w:r><w:t>Independent founders</w:t></w:r></w:p></w:tc></w:tr></w:tbl>"
        "<w:p><w:del><w:r><w:delText>Outdated brand</w:delText></w:r></w:del>"
        "<w:hyperlink><w:r><w:t>Research source</w:t></w:r></w:hyperlink>"
        "<w:r><w:instrText>DDE remote command</w:instrText></w:r></w:p>",
        extra=[
            (
                "word/_rels/document.xml.rels",
                '<Relationships><Relationship TargetMode="External" '
                'Target="https://example.invalid/do-not-fetch"/></Relationships>',
            ),
        ],
    )
    result = read_docx(transport, content)
    assert result["text"] == (
        "Brand café\tStrategy\nFirst\nSecond\nAudience\tIndependent founders\nResearch source"
    )
    assert result["read_only"] and result["extracted"]
    assert result["revision"] == ""
    assert "not included" in result["content_note"]
    assert len(calls) == 1
    assert calls[0].method == "GET"
    assert calls[0].url.host == "www.googleapis.com"
    assert calls[0].url.path == "/drive/v3/files/brand-strategy"
    assert dict(calls[0].url.params) == {"alt": "media", "supportsAllDrives": "true"}
    assert calls[0].headers["authorization"] == "Bearer linked-project-token"
    with pytest.raises(ValidationError, match=r"read-only.*Binary document editing"):
        api.edit("linked-project-token", result, "Brand", "New brand", None)
    assert len(calls) == 1


@pytest.mark.parametrize("encoding", ["utf-8", "utf-16"])
def test_docx_supports_encoded_xml_and_generic_drive_mime(transport, encoding):
    document = (
        f'<?xml version="1.0" encoding="{encoding}"?>'
        f'<w:document xmlns:w="{WORD_NAMESPACE}"><w:body>'
        "<w:p><w:r><w:t>Positioning café</w:t></w:r></w:p></w:body></w:document>"
    ).encode(encoding)
    result = read_docx(
        transport,
        docx_package(document=document),
        mime="application/octet-stream",
        name="BRAND.DOCX",
    )
    assert result["text"] == "Positioning café"


@pytest.mark.parametrize("encoding", ["utf-8", "utf-16"])
@pytest.mark.parametrize("source", ['SYSTEM "file:///secret"', '"expanded text"'])
def test_docx_rejects_external_and_internal_entities_before_expansion(transport, encoding, source):
    document = (
        f'<?xml version="1.0" encoding="{encoding}"?>'
        f"<!DOCTYPE document [<!ENTITY forbidden {source}>]>"
        f'<w:document xmlns:w="{WORD_NAMESPACE}"><w:body>'
        "<w:p><w:r><w:t>&forbidden;</w:t></w:r></w:p></w:body></w:document>"
    ).encode(encoding)
    with pytest.raises(ValidationError, match="declarations and entities"):
        read_docx(transport, docx_package(document=document))
    assert len(transport[1]) == 1


@pytest.mark.parametrize(
    "content",
    [b"not a ZIP", docx_package(document=b"<malformed"), docx_package(document=b"<wrong/>")],
    ids=["invalid-archive", "invalid-xml", "invalid-root"],
)
def test_docx_rejects_corrupt_or_invalid_documents(transport, content):
    with pytest.raises(ValidationError):
        read_docx(transport, content)
    assert len(transport[1]) == 1


def test_docx_rejects_bad_crc(transport):
    content = docx_package(compression=zipfile.ZIP_STORED)
    content = content.replace(b"Brand strategy", b"Wrong strategy", 1)
    with pytest.raises(ValidationError, match="uncorrupted DOCX"):
        read_docx(transport, content)


@pytest.mark.parametrize(
    "path",
    ["../outside.xml", "/absolute.xml", "word\\bad.xml", "WORD/DOCUMENT.XML"],
)
def test_docx_rejects_unsafe_or_duplicate_package_paths(transport, path):
    content = docx_package(extra=[(path, "ignored")])
    if "\\" in path:
        # Windows' ZIP writer normalizes separators; retain the malicious on-wire spelling.
        content = content.replace(path.replace("\\", "/").encode(), path.encode())
    with pytest.raises(ValidationError, match="unsafe or duplicate"):
        read_docx(transport, content)


@pytest.mark.parametrize("path", ["word/vbaProject.bin", "word/embeddings/ole.bin", "script.exe"])
def test_docx_rejects_macro_and_executable_payloads(transport, path):
    with pytest.raises(ValidationError, match="macros, executable or embedded"):
        read_docx(transport, docx_package(extra=[(path, b"not executed")]))


def test_docx_rejects_macro_document_content_type(transport):
    types = (
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Override PartName="/word/document.xml" '
        'ContentType="application/vnd.ms-word.document.macroEnabled.main+xml"/></Types>'
    )
    with pytest.raises(ValidationError, match="non-macro"):
        read_docx(transport, docx_package(content_types=types))


def test_docx_rejects_links_inside_package(transport):
    link = zipfile.ZipInfo("word/link.xml")
    link.create_system = 3
    link.external_attr = (stat.S_IFLNK | 0o777) << 16
    with pytest.raises(ValidationError, match="unsupported DOCX entries"):
        read_docx(transport, docx_package(extra=[(link, "/outside")]))


@pytest.mark.parametrize(
    "setting,value,message",
    [
        ("MAX_DOCX_XML", 40, "document size limit"),
        ("MAX_DOCX_ENTRIES", 1, "too many package entries"),
        ("MAX_DOCX_EXPANDED", 40, "extraction size"),
    ],
)
def test_docx_enforces_package_limits(transport, monkeypatch, setting, value, message):
    monkeypatch.setattr("simon.adapters.drive." + setting, value)
    with pytest.raises(ValidationError, match=message):
        read_docx(transport, docx_package())


def test_docx_rejects_compression_bomb_before_reading_xml(transport):
    content = docx_package(extra=[("word/oversized.xml", b"x" * (2 * 1024 * 1024))])
    with pytest.raises(ValidationError, match="compression-ratio"):
        read_docx(transport, content)


def test_docx_enforces_text_limit(transport):
    content = docx_package(f"<w:p><w:r><w:t>{'x' * 200001}</w:t></w:r></w:p>")
    with pytest.raises(ValidationError, match="200,000 characters"):
        read_docx(transport, content)


def test_docx_enforces_xml_depth_limit(transport):
    content = docx_package("<w:p>" * 65 + "<w:r><w:t>Deep</w:t></w:r>" + "</w:p>" * 65)
    with pytest.raises(ValidationError, match="complexity limit"):
        read_docx(transport, content)


def test_docx_download_keeps_existing_transfer_limit(transport):
    with pytest.raises(DriveError, match="transfer limit") as caught:
        read_docx(transport, b"x" * (10 * 1024 * 1024 + 1))
    assert not caught.value.unknown and len(transport[1]) == 1


def test_other_binary_documents_still_return_honest_metadata_without_download(transport):
    api, calls, _ = transport
    result = api.read(
        "secret", {"id": "pdf", "name": "Strategy.pdf", "mimeType": "application/pdf"}
    )
    assert "text" not in result and "opened or downloaded" in result["content_note"]
    assert not calls
