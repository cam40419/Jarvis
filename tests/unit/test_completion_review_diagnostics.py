"""Unmet checks can omit bad quotes; positive claims always need grounded evidence."""

import json

import pytest

from simon.services.worker_completion import CompletionReviewError, parse_completion_review
from tests.unit.test_worker_completion import check, verdict


@pytest.mark.parametrize("status", ["missing", "unverified"])
@pytest.mark.parametrize("source", ["candidate", "task_context"])
def test_unsupported_negative_quote_is_removed_without_completion_credit(status, source):
    diagnostics = {}
    result = parse_completion_review(
        verdict(
            check("Deliver the report", status=status, source=source, text="Invented quote"),
            status="not_delivered",
        ),
        candidate="I will write the report later.",
        task_context="Read recorded sources.",
        diagnostics=diagnostics,
    )
    assert result.status == "not_delivered"
    assert result.checks[0].status == status
    assert result.checks[0].evidence == ()
    assert diagnostics == {
        "omitted_evidence_count": 1,
        "omitted_evidence": [
            {
                "check_index": 0,
                "evidence_index": 0,
                "kind": "deliverable",
                "status": status,
                "source": source,
                "reason": "excerpt_not_found",
            }
        ],
    }
    assert "Invented quote" not in json.dumps(diagnostics)


def test_partial_review_preserves_grounded_positive_checks_and_only_drops_bad_negative_quote():
    candidate = "Partial report: sampled production needs an itemized factory quote."
    result = parse_completion_review(
        verdict(
            check("Provide a manufacturing assessment", text="sampled production"),
            check("Verify quoted pricing", kind="source_support", status="unverified", text="oops"),
            status="partial",
        ),
        candidate=candidate,
        task_context="Public source estimates are not a binding factory quote.",
    )
    assert result.status == "partial"
    assert result.checks[0].evidence[0].excerpt == "sampled production"
    assert result.checks[1].status == "unverified"
    assert result.checks[1].evidence == ()


@pytest.mark.parametrize("kind", ["deliverable", "source_support", "saved_result", "verification"])
@pytest.mark.parametrize("invalid_first", [False, True])
def test_satisfied_check_omits_extra_bad_quote_only_when_its_required_proof_remains(
    kind, invalid_first
):
    candidate = "Research memo: numbered editions differ from a unique seed per garment."
    context = "Verified source read and saved report revision 2: numbered editions of 20."
    source = "candidate" if kind == "deliverable" else "task_context"
    exact = candidate if source == "candidate" else context
    supported = check("Support this requirement", kind=kind, source=source, text=exact)
    bad = {"source": source, "excerpt": "PRIVATE invented additional quotation"}
    supported["evidence"].insert(0 if invalid_first else 1, bad)
    checks = [supported]
    if kind != "deliverable":
        checks.insert(0, check("Deliver the research memo", text=candidate))
    diagnostics = {}
    result = parse_completion_review(
        verdict(*checks), candidate=candidate, task_context=context, diagnostics=diagnostics
    )
    assert result.status == "complete"
    assert len(result.checks[-1].evidence) == 1
    assert result.checks[-1].evidence[0].source == source
    assert result.checks[-1].evidence[0].excerpt == exact
    assert diagnostics == {
        "omitted_evidence_count": 1,
        "omitted_evidence": [
            {
                "check_index": len(checks) - 1,
                "evidence_index": 0 if invalid_first else 1,
                "kind": kind,
                "status": "satisfied",
                "source": source,
                "reason": "excerpt_not_found",
            }
        ],
    }
    assert "PRIVATE" not in result.model_dump_json() + json.dumps(diagnostics)


@pytest.mark.parametrize("kind", ["source_support", "saved_result", "verification"])
def test_grounded_candidate_quote_cannot_replace_missing_required_execution_proof(kind):
    candidate = "I saved and verified the entire research report."
    unsupported = check("Verify the required action", kind=kind, text=candidate)
    unsupported["evidence"].append({"source": "task_context", "excerpt": "Invented receipt"})
    with pytest.raises(CompletionReviewError) as caught:
        parse_completion_review(
            verdict(check("Provide a report", text=candidate), unsupported),
            candidate=candidate,
            task_context="Only a folder inventory was returned.",
        )
    assert caught.value.diagnostics["reason"] == "unsupported_satisfied_check"
    assert caught.value.diagnostics["issues"][0]["check_index"] == 1
    assert caught.value.diagnostics["issues"][0]["source"] == "task_context"


def test_a_grounded_check_does_not_rescue_a_different_unsupported_satisfied_check():
    candidate = "The report compares numbered editions with unique generated garments."
    with pytest.raises(CompletionReviewError) as caught:
        parse_completion_review(
            verdict(
                check("Explain product differentiation", text=candidate),
                check("Provide supplier pricing", text="Factory quotes prove a 40 percent margin"),
            ),
            candidate=candidate,
            task_context="No supplier quote was received.",
        )
    assert caught.value.diagnostics["reason"] == "unsupported_satisfied_check"
    assert caught.value.diagnostics["issues"][0]["check_index"] == 1


def test_extra_quote_cleanup_preserves_partial_status_and_unmet_requirements():
    candidate = "Partial memo: one-of-one production needs per-garment pattern control."
    delivered = check("Assess production workflow", text=candidate)
    delivered["evidence"].append({"source": "candidate", "excerpt": "Invented elaboration"})
    result = parse_completion_review(
        verdict(
            delivered,
            check(
                "Verify supplier quotes and minimums", kind="source_support", status="unverified"
            ),
            status="partial",
        ),
        candidate=candidate,
        task_context="No quotes received.",
    )
    assert result.status == "partial"
    assert result.checks[1].requirement == "Verify supplier quotes and minimums"
    assert result.checks[1].status == "unverified"


def test_grounded_quote_does_not_permit_malformed_extra_evidence():
    candidate = "Report with a grounded comparison."
    malformed = check("Deliver the report", text=candidate)
    malformed["evidence"].append({"source": "private invalid source", "excerpt": "private body"})
    with pytest.raises(CompletionReviewError) as caught:
        parse_completion_review(verdict(malformed), candidate=candidate, task_context="Context")
    assert caught.value.diagnostics["stage"] == "schema"
    assert "private" not in json.dumps(caught.value.diagnostics)


@pytest.mark.parametrize("status", ["complete", "partial", "not_delivered"])
def test_satisfied_ungrounded_check_still_rejects_whole_review(status):
    checks = [check("Actually save the output", text="Private invented receipt")]
    if status != "complete":
        checks.append(check("Remaining comparison", status="missing"))
    with pytest.raises(CompletionReviewError) as caught:
        parse_completion_review(
            verdict(*checks, status=status), candidate="I will write it.", task_context="No write."
        )
    assert caught.value.diagnostics["reason"] == "unsupported_satisfied_check"
    issue = caught.value.diagnostics["issues"][0]
    assert issue["check_index"] == 0 and issue["evidence_index"] == 0
    assert issue["source"] == "candidate" and issue["status"] == "satisfied"
    assert "Private invented receipt" not in str(caught.value)
    assert "Private invented receipt" not in json.dumps(caught.value.diagnostics)


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


def test_diagnostics_container_is_cleared_between_reviews():
    diagnostics = {"old_result": "must disappear"}
    result = parse_completion_review(
        verdict(check("Deliver report", text="Full report")),
        candidate="Full report",
        task_context="Context",
        diagnostics=diagnostics,
    )
    assert result.status == "complete"
    assert diagnostics == {}
