from simon.services.report_layout import prepare_report


def test_plain_report_labels_get_hierarchy_without_changing_prose():
    source = (
        "Competitive comparison report for Example\n\n"
        "Scope and method\nReviewed retained evidence.\n\n"
        "1) Supplier A\nSource basis: retained evidence.\n\nProducts and pricing\n"
        "Price: ₹599. Source: https://example.com/catalog\n\n"
        "Evidence strength\n\nMedium.\n\n"
        "Cross-brand comparison summary\n\n- Exact finding\n"
    )
    result = prepare_report(source)
    assert result.startswith("# Competitive comparison report for Example\n")
    assert "## Scope and method\n" in result
    assert "## 1) Supplier A\n" in result
    assert "### Products and pricing\n" in result
    assert "## Cross-brand comparison summary\n" in result
    assert "\n".join(line.lstrip("# ") for line in result.splitlines()) == source.rstrip("\n")
    assert prepare_report(result) == result


def test_existing_markdown_only_gets_its_plain_title_marked():
    source = (
        "Manufacturing Strategy Memo — Example\nEvidence basis: retained sources.\n\n"
        "## Findings\nText."
    )
    assert prepare_report(source) == "# " + source


def test_regular_text_code_lists_and_urls_are_not_promoted_to_headings():
    assert prepare_report("A short message\n\nThanks") == "A short message\n\nThanks"
    assert prepare_report("The report is ready.\n\nPlease read it.") == (
        "The report is ready.\n\nPlease read it."
    )
    source = (
        "Report for Example\n\n```text\n\nEvidence strength\n\n```\n\n"
        "1. Keep the exact numbered list\n\nhttps://example.com/report\n"
    )
    assert prepare_report(source) == "# " + source
