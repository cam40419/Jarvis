"""Unmet checks can omit bad quotes; positive claims always need grounded evidence."""

import json

import pytest

from simon.services.worker_completion import CompletionReviewError, parse_completion_review
from tests.unit.test_worker_completion import check, verdict


def test_grounded_quote_does_not_permit_malformed_extra_evidence():
    candidate = "Report with a grounded comparison."
    malformed = check("Deliver the report", text=candidate)
    malformed["evidence"].append({"source": "private invalid source", "excerpt": "private body"})
    with pytest.raises(CompletionReviewError) as caught:
        parse_completion_review(verdict(malformed), candidate=candidate, task_context="Context")
    assert caught.value.diagnostics["stage"] == "schema"
    assert "private" not in json.dumps(caught.value.diagnostics)


def test_negative_status_does_not_permit_malformed_or_inconsistent_review():
    malformed = json.loads(
        verdict(check("Missing report", status="missing"), status="not_delivered")
    )
    malformed["checks"][0]["evidence"] = [{"source": "unknown", "excerpt": "private body"}]
    with pytest.raises(CompletionReviewError) as caught:
        parse_completion_review(json.dumps(malformed), candidate="Promise", task_context="Context")
    assert caught.value.diagnostics["stage"] == "schema"
    assert caught.value.diagnostics["issues"][0]["field"] == ["checks", 0, "evidence", 0, "source"]
    assert "private body" not in json.dumps(caught.value.diagnostics)
    with pytest.raises(CompletionReviewError):
        parse_completion_review(
            verdict(check("Missing report", status="missing"), status="complete"),
            candidate="Promise",
            task_context="Context",
        )


def test_schema_diagnostics_redact_unknown_field_names_and_values():
    malformed = json.loads(
        verdict(check("Missing report", status="missing"), status="not_delivered")
    )
    malformed["private user content field"] = "Private raw output"
    with pytest.raises(CompletionReviewError) as caught:
        parse_completion_review(json.dumps(malformed), candidate="Promise", task_context="Context")
    assert caught.value.diagnostics == {
        "stage": "schema",
        "reason": "schema_validation",
        "issues": [{"field": ["unknown_field"], "reason": "extra_forbidden"}],
    }
    assert "Private" not in str(caught.value) + json.dumps(caught.value.diagnostics)


@pytest.mark.parametrize(
    "text,reason",
    [
        ("private invalid provider body", "invalid_json"),
        ('{"status":"complete","status":"not_delivered"}', "invalid_json"),
        ('{"status":NaN}', "invalid_json"),
        ("x" * 64001, "review_too_large"),
    ],
    ids=["invalid-body", "duplicate-field", "nonfinite", "oversized"],
)
def test_invalid_json_diagnostics_contain_only_static_reason(text, reason):
    with pytest.raises(CompletionReviewError) as caught:
        parse_completion_review(text, candidate="Promise", task_context="Context")
    assert caught.value.diagnostics == {"stage": "json", "reason": reason}
