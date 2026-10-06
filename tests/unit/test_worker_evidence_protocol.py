"""Paging mistakes stay bounded without inventing data or replaying a source action."""

import hashlib
import json
from uuid import uuid4

import pytest

from simon.services.worker_context import (
    MAX_EVIDENCE_CHARS,
    EvidenceReadError,
    ToolEvidenceBuffer,
)


def captured(text="First page. Remaining report."):
    buffer = ToolEvidenceBuffer()
    identifier = str(uuid4())
    buffer.capture(
        {
            "invocation_id": identifier,
            "tool_id": "project.output_read",
            "side_effect": False,
            "status": "succeeded",
            "arguments": {"offset": 0},
            "output": {"text": text, "next_offset": None},
        }
    )
    return buffer, {
        "invocation_id": identifier,
        "pointer": "/output/text",
        "offset": 0,
        "limit": 8000,
    }


@pytest.mark.parametrize("offset", [11750, 11751, 24000, MAX_EVIDENCE_CHARS])
def test_known_value_at_or_beyond_end_returns_explicit_bounded_eof(offset):
    text = "x" * 11750
    buffer, arguments = captured(text)
    page = buffer.read({**arguments, "offset": offset})
    assert page["status"] == "eof" and page["eof"] is True
    assert page["offset"] == page["total_chars"] == len(text)
    assert page["next_offset"] is None and page["text"] == ""
    assert page.get("requested_offset", offset) == offset
    assert page["value_sha256"] == hashlib.sha256(text.encode()).hexdigest()
    # The record is unchanged, so reading the right range remains exact.
    assert buffer.read({**arguments, "offset": 6109})["text"] == text[6109:]


def test_actual_v7_shaped_two_pages_cover_full_captured_report_without_gap():
    text = "Manufacturing cost and supplier evidence. " * 300
    text = text[:11750]
    buffer, arguments = captured(text)
    first = buffer.read({**arguments, "limit": 6109})
    assert first["next_offset"] == 6109 and first["eof"] is False
    second = buffer.read({**arguments, "offset": first["next_offset"]})
    assert second["next_offset"] is None and second["eof"] is True
    assert first["text"] + second["text"] == text
    assert first["value_sha256"] == second["value_sha256"]
    eof = buffer.read({**arguments, "offset": second["total_chars"]})
    assert eof["status"] == "eof" and eof["text"] == ""


def test_empty_captured_text_is_eof_not_missing_evidence():
    buffer, arguments = captured("")
    page = buffer.read(arguments)
    assert page["status"] == "eof" and page["eof"] is True
    assert page["text"] == "" and page["total_chars"] == 0


@pytest.mark.parametrize(
    ("changes", "reason"),
    [
        ({"pointer": "/output/private-path-that-does-not-exist"}, "invalid_pointer"),
        ({"pointer": "C:/private/secret.txt"}, "invalid_pointer"),
        ({"pointer": "/output/~invalid"}, "invalid_pointer"),
        ({"offset": -1}, "invalid_page_range"),
        ({"offset": True}, "invalid_page_range"),
        ({"offset": MAX_EVIDENCE_CHARS + 1}, "invalid_page_range"),
        ({"offset": None}, "invalid_page_range"),
        ({"limit": 8001}, "invalid_page_range"),
        ({"limit": 0}, "invalid_page_range"),
        ({"limit": "secret-limit"}, "invalid_page_range"),
        ({"path": "C:/private/secret.txt"}, "invalid_request"),
    ],
)
def test_known_capture_mistakes_have_static_non_reflecting_feedback(changes, reason):
    buffer, arguments = captured()
    with pytest.raises(EvidenceReadError) as raised:
        buffer.read({**arguments, **changes})
    error = raised.value
    assert error.reason == reason and error.recoverable is True
    assert error.feedback["status"] == "not_read"
    assert error.feedback["error"] == "invalid_evidence_request"
    serialized = json.dumps(error.feedback)
    assert len(serialized) < 600
    for forbidden in ("secret", "private", arguments["invocation_id"]):
        assert forbidden not in serialized
    assert buffer.read(arguments)["text"] == "First page. Remaining report."


def test_foreign_invocation_and_random_typo_are_indistinguishable_and_never_read():
    foreign, foreign_arguments = captured("PRIVATE FOREIGN EVIDENCE")
    local, local_arguments = captured("Current task evidence")
    responses = []
    for identifier in (foreign_arguments["invocation_id"], str(uuid4())):
        with pytest.raises(EvidenceReadError) as raised:
            local.read({**local_arguments, "invocation_id": identifier})
        assert raised.value.recoverable is True
        assert raised.value.reason == "unavailable_evidence"
        responses.append(raised.value.feedback)
    assert responses[0] == responses[1]
    assert foreign_arguments["invocation_id"] not in json.dumps(responses)
    assert "PRIVATE FOREIGN EVIDENCE" not in json.dumps(responses)
    assert foreign.read(foreign_arguments)["text"] == "PRIVATE FOREIGN EVIDENCE"
    assert local.read(local_arguments)["text"] == "Current task evidence"


def test_provider_next_page_is_distinct_from_captured_result_end():
    buffer, arguments = captured()
    identifier = str(uuid4())
    buffer.capture(
        {
            "invocation_id": identifier,
            "output": {"text": "Document first part", "next_offset": 19000, "characters": 30000},
        }
    )
    page = buffer.read({**arguments, "invocation_id": identifier})
    assert page["text"] == "Document first part"
    assert page["eof"] is True and page["next_offset"] is None
    assert page["total_chars"] == len("Document first part")
    provider_offset = buffer.read(
        {**arguments, "invocation_id": identifier, "pointer": "/output/next_offset"}
    )
    assert provider_offset["text"] == "19000"


@pytest.mark.parametrize("corrupt", [None, {"output": object()}])
def test_internal_capture_corruption_remains_terminal(corrupt):
    buffer, arguments = captured()
    buffer._records[arguments["invocation_id"]] = corrupt
    with pytest.raises(EvidenceReadError) as raised:
        buffer.read({**arguments, "pointer": ""})
    assert raised.value.reason == "invalid_evidence"
    assert raised.value.recoverable is False
    assert "object" not in json.dumps(raised.value.feedback)
