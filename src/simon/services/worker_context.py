"""Bounded views over immutable, task-local tool evidence; never a tool replay."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, ClassVar
from uuid import UUID

from simon.domain.artifacts import Artifact

EvidenceWriter = Callable[[UUID, str], Artifact]
MAX_EVIDENCE_CHARS = 8 * 1024 * 1024
MAX_TASK_EVIDENCE_CHARS = 32 * 1024 * 1024
MAX_EVIDENCE_PAGE = 8000


class ContextLimitError(ValueError):
    """Even explicit evidence summaries cannot fit without dropping required context."""


class EvidenceReadError(ValueError):
    """An internal read must name an existing result and a bounded JSON pointer/page."""

    _MESSAGES: ClassVar[dict[str, str]] = {
        "invalid_request": "Use exactly invocation_id, pointer, offset and limit.",
        "unavailable_evidence": (
            "The requested evidence is unavailable in this task. Choose an exact invocation_id "
            "from this task's recorded tool results, not an artifact_id or run_id. "
            "Evidence only contains previously returned results; reading a different document "
            "requires its granted read tool. No value was read."
        ),
        "invalid_evidence": "The captured evidence could not be read safely.",
        "invalid_pointer": (
            "Use an exact JSON pointer from this call's evidence preview, or an empty "
            "pointer to inspect the captured record. No value was read."
        ),
        "invalid_page_range": (
            f"Use integer offset from 0 to {MAX_EVIDENCE_CHARS} and integer limit "
            f"from 1 to {MAX_EVIDENCE_PAGE}. "
            "Offsets apply to this captured value, not the original source document. "
            "Use next_offset to continue; null next_offset means this value has ended."
        ),
    }

    def __init__(self, reason: str) -> None:
        self.reason = reason
        self.recoverable = reason != "invalid_evidence"
        super().__init__(self._MESSAGES[reason])

    @property
    def feedback(self) -> dict[str, Any]:
        # Never echo a guessed invocation, pointer, path or arbitrary provider text.
        return {
            "status": "not_read",
            "error": "invalid_evidence_request",
            "reason": self.reason,
            "message": str(self),
            "max_page_chars": MAX_EVIDENCE_PAGE,
        }


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=True, separators=(",", ":"), allow_nan=False)


@dataclass(frozen=True)
class HistoryView:
    text: str
    compacted: bool
    original_chars: int


class ToolEvidenceBuffer:
    """Each execute() creates its own buffer; there is no cross-run or global lookup."""

    def __init__(self) -> None:
        self._records: dict[str, dict[str, Any]] = {}
        self._size = 0

    def capture(self, entry: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        identifier = str(UUID(entry["invocation_id"]))
        if identifier in self._records:
            raise ValueError("Evidence invocation already recorded")
        serialized = _json(entry)
        if (
            len(serialized) > MAX_EVIDENCE_CHARS
            or self._size + len(serialized) > MAX_TASK_EVIDENCE_CHARS
        ):
            raise ContextLimitError("Tool evidence exceeds the task storage bound")
        # Detach provider-owned mutable objects before later reads or compaction.
        self._records[identifier] = json.loads(serialized)
        self._size += len(serialized)
        return serialized, {
            "invocation_id": identifier,
            "sha256": hashlib.sha256(serialized.encode("utf-8")).hexdigest(),
            "original_chars": len(serialized),
            "durable": False,
        }

    def read(self, arguments: dict[str, Any]) -> dict[str, Any]:
        if set(arguments) != {"invocation_id", "pointer", "offset", "limit"}:
            raise EvidenceReadError("invalid_request")
        identifier, pointer = arguments["invocation_id"], arguments["pointer"]
        offset, limit = arguments["offset"], arguments["limit"]
        if not isinstance(identifier, str) or identifier not in self._records:
            raise EvidenceReadError("unavailable_evidence")
        if (
            not isinstance(pointer, str)
            or len(pointer) > 1000
            or (pointer and not pointer.startswith("/"))
        ):
            raise EvidenceReadError("invalid_pointer")
        if (
            type(offset) is not int
            or not 0 <= offset <= MAX_EVIDENCE_CHARS
            or type(limit) is not int
            or not 1 <= limit <= MAX_EVIDENCE_PAGE
        ):
            raise EvidenceReadError("invalid_page_range")
        value: Any = self._records[identifier]
        if not isinstance(value, dict):
            raise EvidenceReadError("invalid_evidence")
        try:
            for encoded in pointer.split("/")[1:] if pointer else ():
                # RFC 6901 escaping; reject malformed escapes rather than guess.
                remainder = encoded.replace("~0", "").replace("~1", "")
                if "~" in remainder:
                    raise ValueError
                part = encoded.replace("~1", "/").replace("~0", "~")
                if isinstance(value, dict):
                    value = value[part]
                elif isinstance(value, list) and part.isascii() and part.isdecimal():
                    value = value[int(part)]
                else:
                    raise ValueError
        except (KeyError, IndexError, ValueError):
            raise EvidenceReadError("invalid_pointer") from None
        try:
            text = value if isinstance(value, str) else _json(value)
            digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        except (TypeError, ValueError, UnicodeError, RecursionError):
            raise EvidenceReadError("invalid_evidence") from None
        # Paging an already captured value is a local read, not a provider action.
        # A plausible overshoot is an explicit EOF, never a reason to repeat the
        # original tool. The actual extent/hash remain those of this exact value.
        requested_offset = offset
        offset = min(offset, len(text))
        end = min(len(text), offset + limit)
        return {
            "status": "eof" if offset == len(text) else "ok",
            "invocation_id": identifier,
            "pointer": pointer,
            "format": "text" if isinstance(value, str) else "json",
            "offset": offset,
            **({"requested_offset": requested_offset} if requested_offset != offset else {}),
            "next_offset": end if end < len(text) else None,
            "total_chars": len(text),
            "value_sha256": digest,
            "text": text[offset:end],
            "eof": end == len(text),
            "partial": offset != 0 or end != len(text),
        }


_REFERENCE_KEYS = {
    "id",
    "file_id",
    "project_id",
    "run_id",
    "task_id",
    "artifact_id",
    "operation_id",
    "action_id",
    "invocation_id",
    "source_account_id",
    "account_id",
    "read_tool",
    "read_context",
    "revision",
    "version",
    "tab_id",
    "url",
    "webViewLink",
    "path",
    "name",
    "title",
    "status",
    "error",
    "side_effect",
    "next_page_token",
    "sha256",
}
_CONTENT_KEYS = {
    "text",
    "content",
    "body",
    "summary",
    "findings",
    "answer",
    "markdown",
    "plain_text",
    "snippet",
    "description",
}
_CONTENT_FLAGS = {
    "text_truncated",
    "content_truncated",
    "body_truncated",
    "sources_truncated",
    "links_truncated",
    "truncated",
    "characters",
    "total_chars",
    "offset",
    "next_offset",
}
_SOURCE_COLLECTIONS = {"citations", "sources", "links"}
_ESSENTIAL_KEYS = _REFERENCE_KEYS | _CONTENT_KEYS | _CONTENT_FLAGS | _SOURCE_COLLECTIONS


def _pointer(parent: str, key: str) -> str:
    return parent + "/" + key.replace("~", "~0").replace("/", "~1")


def _preview(
    value: Any,
    chars: int,
    items: int,
    path: str,
    depth: int = 0,
    *,
    reference_chars: int = 1000,
) -> Any:
    if isinstance(value, str):
        if len(value) <= chars:
            return value
        # Both ends are explicitly excerpts; the middle is available via evidence action.
        head = chars * 3 // 4
        tail = chars - head
        return {
            "evidence_pointer": path,
            "original_chars": len(value),
            "head": value[:head],
            "tail": value[-tail:] if tail else "",
            "omitted_chars": len(value) - chars,
        }
    if isinstance(value, (dict, list)) and depth >= 12:
        return {"evidence_pointer": path, "omitted": True, "reason": "nested value"}
    if isinstance(value, dict):
        # A source's identity is not its contents. Keep bounded body excerpts and
        # provider truncation flags even when other metadata fills the item budget.
        ordered = sorted(value, key=lambda key: (key not in _ESSENTIAL_KEYS, key))
        retained = ordered[: max(items, sum(key in _ESSENTIAL_KEYS for key in ordered))]
        result = {}
        for key in retained:
            child = value[key]
            child_path = _pointer(path, key)
            if key in _SOURCE_COLLECTIONS and isinstance(child, list):
                # URL inventories must not consume the source-body budget. At the
                # smallest level retain their exact pointer/count, not navigation
                # links instead of research. Full entries remain available by page.
                source_items = 3 if chars >= 1000 else 1 if chars >= 200 else 0
                result[key] = _preview(
                    child,
                    min(chars, 120),
                    min(items, source_items),
                    child_path,
                    depth + 1,
                    reference_chars=min(chars, 256),
                )
                continue
            limit = max(chars, reference_chars) if key in _REFERENCE_KEYS else chars
            result[key] = _preview(
                child,
                limit,
                items,
                child_path,
                depth + 1,
                reference_chars=reference_chars,
            )
        if len(retained) < len(value):
            return {
                "evidence_pointer": path,
                "retained_fields": result,
                "omitted_field_count": len(value) - len(retained),
            }
        return result
    if isinstance(value, list):
        if len(value) <= items:
            return [
                _preview(
                    item,
                    chars,
                    items,
                    _pointer(path, str(index)),
                    depth + 1,
                    reference_chars=reference_chars,
                )
                for index, item in enumerate(value)
            ]
        return {
            "evidence_pointer": path,
            "total_items": len(value),
            "first_items": [
                _preview(
                    item,
                    chars,
                    items,
                    _pointer(path, str(index)),
                    depth + 1,
                    reference_chars=reference_chars,
                )
                for index, item in enumerate(value[:items])
            ],
            "omitted_items": len(value) - items,
        }
    return value


def render_history(history: list[dict[str, Any]], max_chars: int) -> HistoryView:
    """Never alter instructions or drop a call outcome; disclose every shortened value."""
    full = _json(history)
    if len(full) <= max_chars:
        return HistoryView(full, False, len(full))
    for chars, items in ((4000, 32), (2000, 24), (1000, 16), (500, 8), (200, 4), (32, 1)):
        entries = [
            {
                key: entry[key]
                for key in ("tool_id", "invocation_id", "status", "side_effect", "evidence")
                if key in entry
            }
            | {
                "arguments": _preview(entry.get("arguments", {}), chars, items, "/arguments"),
                "output": _preview(entry.get("output", {}), chars, items, "/output"),
            }
            for entry in history
        ]
        serialized = _json(
            {
                "context_compacted": True,
                "notice": (
                    "These are explicit excerpts, not complete source documents. Every call's "
                    "outcome is retained. Use an evidence action with invocation_id and the "
                    "indicated JSON pointer to read omitted text without repeating a tool. "
                    "Never repeat a completed write just to recover its response."
                ),
                "calls": entries,
            }
        )
        if len(serialized) <= max_chars:
            return HistoryView(serialized, True, len(full))
    raise ContextLimitError("Required tool receipts do not fit the remaining context")
