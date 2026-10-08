"""Immutable project source bytes and bounded, non-executing text extraction."""

import hashlib
import os
import re
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Literal
from uuid import UUID

from simon.adapters.drive import docx_text
from simon.domain.errors import ValidationError
from simon.services.safe_files import open_regular_nofollow

MAX_SOURCE_BYTES = 5 * 1024 * 1024
TEXT_LIMIT = 20000
_PRIVATE_KEY_MARKER = re.compile(r"-----(BEGIN|END) [A-Z ]*PRIVATE KEY-----")
_SECRETS = (
    re.compile(r"\b(?:sk-[A-Za-z0-9_-]{16,}|AIza[A-Za-z0-9_-]{30,}|gh[pousr]_[A-Za-z0-9_]{20,})"),
    re.compile(r"(?i)\bBearer\s+[A-Za-z0-9_.-]{16,}"),
    re.compile(
        r"(?im)\b(?:[A-Z_]*(?:API_KEY|PASSWORD|SECRET|ACCESS_TOKEN|PRIVATE_KEY)|api[- ]?key)"
        r"[\"']?\s*[:=]\s*[\"']?[^\s,\"']{8,}"
    ),
)


def redact(text: str) -> tuple[str, int]:
    # Scan markers once: a non-greedy block regex becomes quadratic on many
    # unterminated BEGIN markers. An incomplete key also stays private.
    fragments: list[str] = []
    start: int | None = None
    cursor = 0
    count = 0
    for marker in _PRIVATE_KEY_MARKER.finditer(text):
        if marker.group(1) == "BEGIN" and start is None:
            start = marker.start()
        elif marker.group(1) == "END" and start is not None:
            fragments.extend((text[cursor:start], "[REDACTED CREDENTIAL]"))
            cursor, start = marker.end(), None
            count += 1
    if start is not None:
        fragments.extend((text[cursor:start], "[REDACTED CREDENTIAL]"))
        count += 1
    else:
        fragments.append(text[cursor:])
    text = "".join(fragments)
    for pattern in _SECRETS:
        text, found = pattern.subn("[REDACTED CREDENTIAL]", text)
        count += found
    return text, count


@dataclass(frozen=True)
class SourceExtraction:
    text: str
    status: Literal["text", "unparsed"]
    redactions: int
    truncated: bool


def extract_source(raw: bytes, filename: str, media_type: str) -> SourceExtraction:
    """Unknown/malformed formats remain inventoried, never interpreted as code."""
    suffix = Path(filename).suffix.lower()
    try:
        if suffix == ".docx":
            text = docx_text(raw)
        elif media_type.startswith("text/") or suffix in {
            ".txt",
            ".md",
            ".csv",
            ".json",
            ".yaml",
            ".yml",
            ".toml",
            ".xml",
            ".html",
        }:
            text = raw.decode("utf-8-sig")
        else:
            return SourceExtraction("", "unparsed", 0, False)
    except (UnicodeError, ValidationError):
        return SourceExtraction("", "unparsed", 0, False)
    if "\x00" in text:
        return SourceExtraction("", "unparsed", 0, False)
    text, redactions = redact(text)
    return SourceExtraction(text[:TEXT_LIMIT], "text", redactions, len(text) > TEXT_LIMIT)


class IntakeSourceBytes:
    def __init__(self, root: Path) -> None:
        self.root = root.absolute()

    def _path(self, workspace_id: UUID, project_id: UUID, sha256: str) -> Path:
        if not re.fullmatch(r"[0-9a-f]{64}", sha256):
            raise ValidationError("Invalid source digest.")
        path = self.root / str(workspace_id) / str(project_id) / sha256
        # This tree is server-owned. User filenames never become filesystem paths.
        for part in (path, *path.parents):
            if part.exists() or part.is_symlink():
                info = part.lstat()
                if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
                    raise ValidationError("Source storage cannot contain filesystem redirects.")
        return path

    def put(self, workspace_id: UUID, project_id: UUID, raw: bytes) -> str:
        if len(raw) > MAX_SOURCE_BYTES:
            raise ValidationError("Source exceeds the 5 MiB limit.")
        sha256 = hashlib.sha256(raw).hexdigest()
        path = self._path(workspace_id, project_id, sha256)
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            if self.read(workspace_id, project_id, sha256, len(raw)) != raw:
                raise ValidationError("Stored source does not match its digest.")
            return sha256
        descriptor, name = tempfile.mkstemp(prefix=".upload-", dir=path.parent)
        temporary = Path(name)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            try:
                os.link(temporary, path)
            except FileExistsError:
                self.read(workspace_id, project_id, sha256, len(raw))
        finally:
            temporary.unlink(missing_ok=True)
        return sha256

    def read(self, workspace_id: UUID, project_id: UUID, sha256: str, size: int) -> bytes:
        path = self._path(workspace_id, project_id, sha256)
        try:
            with open_regular_nofollow(path) as stream:
                raw = stream.read(MAX_SOURCE_BYTES + 1)
        except OSError:
            raise ValidationError("Original source is unavailable.") from None
        if len(raw) != size or hashlib.sha256(raw).hexdigest() != sha256:
            raise ValidationError("Original source failed integrity verification.")
        return raw
