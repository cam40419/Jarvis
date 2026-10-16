"""Typed values and selectors preserve semantics without weakening snapshot bounds."""

from uuid import uuid4

import pytest
from pydantic import ValidationError

from simon.domain.native_dependencies import (
    DependencySelector,
    NativeFactRevision,
    NativeImpactAssessment,
    semantic_hash,
    snapshot_hash,
)


def revision(**changes):
    values = {
        "workspace_id": uuid4(),
        "project_id": uuid4(),
        "fact_id": uuid4(),
        "version": 1,
        "value_type": "number",
        "value": 1,
        "created_by": uuid4(),
        **changes,
    }
    values["semantic_sha256"] = changes.get(
        "semantic_sha256",
        semantic_hash(values["value_type"], values["value"], values.get("unit", "")),
    )
    values["sha256"] = changes.get(
        "sha256",
        snapshot_hash(
            {
                "value_type": values["value_type"],
                "value": values["value"],
                "unit": values.get("unit", ""),
                "sources": [],
            }
        ),
    )
    return NativeFactRevision(**values)


def test_semantic_numbers_normalize_without_rounding_or_boolean_aliases():
    assert semantic_hash("number", 1) == semantic_hash("number", 1.0)
    assert semantic_hash("number", -0.0) == semantic_hash("number", 0)
    assert semantic_hash("number", 10**40 + 1) != semantic_hash("number", 10**40)
    assert semantic_hash("boolean", True) != semantic_hash("number", 1)
    assert semantic_hash("json", {"a": 1, "b": 2.0}) == semantic_hash("json", {"b": 2, "a": 1.0})
    assert semantic_hash("json", ["number", "1e0"]) != semantic_hash("json", 1)
    assert semantic_hash("number", 1, "kg") != semantic_hash("number", 1, "g")


@pytest.mark.parametrize("path", ["", "/a~1b/~0", "/label ", "/ ", "/a//b"])
def test_json_pointer_keeps_valid_escaped_keys_and_significant_whitespace(path):
    selector = DependencySelector(kind="json_pointer", path=path)
    assert selector.path == path


@pytest.mark.parametrize("path", ["a", "/x~", "/x~2", "/x~00~x"])
def test_json_pointer_rejects_invalid_escapes(path):
    with pytest.raises(ValidationError):
        DependencySelector(kind="json_pointer", path=path)
    with pytest.raises(ValidationError):
        DependencySelector(path="/a")


@pytest.mark.parametrize(
    "changes",
    [
        {"value": True},
        {"value": float("nan")},
        {"value": float("inf")},
        {"value_type": "boolean", "value": 1},
        {"value_type": "string", "value": 1},
        {"value_type": "string", "value": "😀" * 3000},
        {"value_type": "json", "value": {"bad\x00key": 1}},
        {"version": 2},
        {"semantic_sha256": "f" * 64},
        {"sha256": "f" * 64},
    ],
)
def test_fact_types_bounds_unicode_and_ancestry_are_validated(changes):
    with pytest.raises((ValidationError, ValueError)):
        revision(**changes)


def test_fact_values_keep_whitespace_and_normalize_owned_nested_snapshots():
    assert revision(value_type="string", value="  exact  ").value == "  exact  "
    value = {"a": [" exact "]}
    record = revision(value_type="json", value=value)
    value["a"].clear()
    assert record.value == {"a": [" exact "]}


def test_impact_snapshot_enforces_utf8_byte_bound():
    with pytest.raises(ValidationError):
        NativeImpactAssessment(
            workspace_id=uuid4(),
            project_id=uuid4(),
            fact_id=uuid4(),
            from_revision_id=uuid4(),
            to_revision_id=uuid4(),
            graph_fingerprint="a" * 64,
            source_fingerprint="b" * 64,
            outputs=({"reasons": ["😀" * 70000]},),
            created_by=uuid4(),
        )
