"""Research compaction keeps source substance alongside bounded provenance."""

import json

import pytest

from simon.services.worker_context import ToolEvidenceBuffer, _preview, render_history
from tests.unit.test_worker_context import capture, entry


def fields(value):
    return value.get("retained_fields", value)


def sources(count):
    return [
        {
            "url": f"https://sources.example.test/manufacturing/item-{index}",
            "title": f"Source {index}: factory setup and production costs",
        }
        for index in range(count)
    ]


def web_result(index=0):
    return {
        "query": "Find suppliers for one-of-one clothing with unique patterns",
        "text": (
            f"Finding {index}: unique patterns incur individual setup costs. "
            + "Factory quotations must separate pattern work, material waste and sewing labor. "
            * 120
            + "This is planning evidence, not an agreed factory quotation."
        ),
        "text_truncated": False,
        "citations": sources(40),
        "sources": sources(80),
        "sources_truncated": False,
        "retrieved_at": "2026-10-02T05:00:00Z",
    }


def browser_result(index=0):
    return {
        "title": f"Factory {index}: manufacturing capabilities",
        "url": f"https://factory.example.test/capabilities/{index}",
        "status": 200,
        "text": (
            f"Factory {index} offers sampling and small-batch production. "
            + "The published page describes fabric sourcing, pattern preparation and assembly. "
            * 80
            + "Contact the factory for an itemized quotation."
        ),
        "text_truncated": False,
        "blocked_requests": 2,
        "links": sources(40),
        "links_truncated": True,
        "screenshot_path": None,
    }


@pytest.mark.parametrize("factory", [web_result, browser_result])
@pytest.mark.parametrize("chars,items", [(500, 8), (200, 4), (32, 1)])
def test_semantic_text_and_truncation_flags_survive_every_compaction_level(factory, chars, items):
    original = factory()
    preview = fields(_preview(original, chars, items, "/output"))
    assert preview["text_truncated"] is False
    assert preview["text"]["head"] == original["text"][: chars * 3 // 4]
    assert preview["text"]["tail"] == original["text"][-(chars - chars * 3 // 4) :]
    assert preview["text"]["evidence_pointer"] == "/output/text"
    assert preview["text"]["omitted_chars"] == len(original["text"]) - chars
    if "url" in original:
        assert preview["url"] == original["url"]
        assert preview["title"] == original["title"]
        assert preview["status"] == 200
    for name in {"citations", "sources", "links"} & original.keys():
        assert preview[name]["evidence_pointer"] == "/output/" + name
        assert preview[name]["total_items"] == len(original[name])
        assert len(preview[name]["first_items"]) <= (1 if chars >= 200 else 0)
        assert preview[name]["omitted_items"] >= len(original[name]) - 1


def test_link_inventories_have_separate_budget_from_source_body():
    original = web_result()
    preview = fields(_preview(original, 500, 8, "/output"))
    metadata_chars = sum(len(json.dumps(preview[key])) for key in ("citations", "sources"))
    body_chars = len(json.dumps(preview["text"]))
    assert metadata_chars < body_chars
    assert len(preview["citations"]["first_items"]) == 1
    assert len(preview["sources"]["first_items"]) == 1


@pytest.mark.parametrize("max_chars", [20000, 12000])
def test_twelve_large_research_results_keep_real_body_evidence_under_tight_budget(max_chars):
    buffer = ToolEvidenceBuffer()
    history = [
        capture(
            buffer,
            entry(web_result(index), tool_id="web.search", arguments={"query": "Manufacturers"}),
        )[1]
        for index in range(6)
    ] + [
        capture(
            buffer,
            entry(
                browser_result(index),
                tool_id="browser.read",
                arguments={"url": browser_result(index)["url"]},
            ),
        )[1]
        for index in range(6)
    ]
    before = json.dumps(history)
    view = render_history(history, max_chars)
    assert view.compacted and len(view.text) <= max_chars < view.original_chars
    projected = json.loads(view.text)["calls"]
    assert len(projected) == len(history) == 12
    for original, reduced in zip(history, projected, strict=True):
        assert reduced["invocation_id"] == original["invocation_id"]
        assert reduced["status"] == "succeeded" and reduced["side_effect"] is False
        result = fields(reduced["output"])
        assert result["text_truncated"] is False
        assert result["text"]["head"] and original["output"]["text"].startswith(
            result["text"]["head"]
        )
        page = buffer.read(
            {
                "invocation_id": reduced["invocation_id"],
                "pointer": result["text"]["evidence_pointer"],
                "offset": 300,
                "limit": 700,
            }
        )
        assert page["text"] == original["output"]["text"][300:1000]
    assert json.dumps(history) == before


def test_omitted_citations_remain_exactly_retrievable_without_new_source_call():
    buffer = ToolEvidenceBuffer()
    original = entry(web_result(), tool_id="web.search")
    buffer.capture(original)
    preview = fields(_preview(original["output"], 200, 4, "/output"))
    pointer = preview["citations"]["evidence_pointer"] + "/30"
    page = buffer.read(
        {"invocation_id": original["invocation_id"], "pointer": pointer, "offset": 0, "limit": 1000}
    )
    assert json.loads(page["text"]) == original["output"]["citations"][30]
    assert not page["partial"]


@pytest.mark.parametrize("key", ["content", "body", "summary", "findings", "markdown"])
def test_other_semantic_text_fields_keep_bounded_evidence_alongside_reference_metadata(key):
    original = {
        "id": "source-id",
        "revision": "r1",
        "status": "succeeded",
        key: "Actual evidence " * 1000,
        "truncated": False,
    }
    preview = fields(_preview(original, 32, 1, "/output"))
    assert preview[key]["head"] == original[key][:24]
    assert preview[key]["evidence_pointer"] == "/output/" + key
    assert preview["id"] == "source-id" and preview["revision"] == "r1"
    assert preview["truncated"] is False
