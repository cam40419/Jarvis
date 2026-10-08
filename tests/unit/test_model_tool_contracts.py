"""Every advertised chat tool has a bounded, identity-free provider request contract."""

from types import SimpleNamespace
from typing import get_args

import pytest
from jsonschema import Draft202012Validator
from pydantic import TypeAdapter, ValidationError

from simon.adapters.model_tools import cited_text, definitions
from simon.domain.connected_tools import ToolName


@pytest.mark.parametrize("name", get_args(ToolName))
def test_advertised_tools_have_strict_schemas_without_identity_or_secret_inputs(name):
    tool = definitions((name,))[0]
    if name == "web_search":
        assert tool == {"type": "web_search"}
        return
    schema = tool["parameters"]
    Draft202012Validator.check_schema(schema)
    assert tool["name"] == name and tool["strict"]
    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == set(schema["properties"])
    assert not set(schema["properties"]) & {
        "actor_id",
        "workspace_id",
        "scopes",
        "credential",
        "api_key",
        "token",
    }
    assert all("default" not in value for value in schema["properties"].values())
    assert not Draft202012Validator(schema).is_valid({"workspace_id": "forged"})
    assert definitions((name,))[0] == tool  # Building another schema cannot mutate a prior one.


@pytest.mark.parametrize("name", ["propose_home_change", "project_create", "task_create"])
def test_removed_tool_names_are_not_accepted_in_request_snapshots_or_definitions(name):
    with pytest.raises(ValidationError):
        TypeAdapter(ToolName).validate_python(name)
    with pytest.raises(ValueError, match="unsupported tool"):
        definitions((name,))


@pytest.mark.parametrize("name", ["gmail_search_messages", "drive_search_files"])
def test_google_search_contract_requires_explicit_account_and_bounded_paging(name):
    validator = Draft202012Validator(definitions((name,))[0]["parameters"])
    request = {"query": "brand", "page_token": "", "limit": 20, "account": "source@example.test"}
    assert validator.is_valid(request)
    assert not validator.is_valid({k: v for k, v in request.items() if k != "account"})
    for field, value in [
        ("limit", 21),
        ("limit", 0),
        ("query", "x" * 501),
        ("page_token", "x" * 2049),
    ]:
        assert not validator.is_valid(request | {field: value})


def test_display_widget_contract_rejects_unknown_actions_and_unbounded_updates():
    validator = Draft202012Validator(definitions(("display_configure",))[0]["parameters"])
    widget = {
        "id": "message",
        "kind": "message",
        "title": "Work",
        "position": "center",
        "message": "Ready",
    }
    request = {
        "display_id": None,
        "layout": None,
        "slide_seconds": None,
        "dim_percent": None,
        "widgets": [widget],
    }
    assert validator.is_valid(request)
    assert not validator.is_valid(request | {"widgets": [widget | {"command": "execute"}]})
    assert not validator.is_valid(request | {"widgets": [widget | {"kind": "shell"}]})
    assert not validator.is_valid(request | {"widgets": [widget] * 6})
    assert not validator.is_valid(request | {"dim_percent": 101})


def test_citations_ignore_unsafe_annotations_and_preserve_valid_evidence():
    def citation(url, start=1, end=2):
        return SimpleNamespace(
            type="url_citation", title="Source", url=url, start_index=start, end_index=end
        )

    annotations = [
        SimpleNamespace(type="file_citation"),
        citation("javascript:alert(1)"),
        citation("https://example.test/(source)"),
        citation("https://example.test/(source)"),
        citation("https://example.test/metadata", start=99, end=100),
    ]
    response = SimpleNamespace(
        output=[
            SimpleNamespace(type="reasoning"),
            SimpleNamespace(
                type="message",
                content=[
                    SimpleNamespace(type="refusal"),
                    SimpleNamespace(type="output_text", text="ABC", annotations=annotations),
                ],
            ),
        ]
    )
    text, sources = cited_text(response)
    assert text == "A[1](https://example.test/%28source%29)C"
    assert [str(source.url) for source in sources] == [
        "https://example.test/(source)",
        "https://example.test/metadata",
    ]
