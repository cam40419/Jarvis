import pytest

from tests.integration.test_project_command_browser import create_project
from tests.integration.test_project_command_browser import project_ui as project_ui

pytestmark = pytest.mark.browser


def test_project_records_and_procedures_are_readable_and_versioned(project_ui):
    from playwright.sync_api import expect

    page, *_ = project_ui
    create_project(page, "Business records")
    page.get_by_role("tab", name="Knowledge", exact=True).click()
    panel = page.locator("#project-workspace")
    expect(panel).to_be_visible()
    panel.get_by_role("button", name="New record", exact=True).click()
    dialog = page.locator("#pw-record-dialog")
    dialog.get_by_label("Title", exact=True).fill("Supplier checks")
    dialog.get_by_label("Record type", exact=True).select_option("procedure")
    dialog.get_by_label("Overview", exact=True).fill(
        "Check existing evidence before refreshing quotes."
    )
    dialog.get_by_label("Procedure steps (one per line)").fill(
        "Read supplier records\nCompare current quotes"
    )
    dialog.get_by_label("Completion checks (one per line)").fill(
        "Every quote has a source and date"
    )
    dialog.get_by_role("button", name="Save revision", exact=True).click()
    expect(dialog).not_to_be_visible()
    card = panel.locator(".pw-record").filter(has_text="Supplier checks")
    expect(card).to_contain_text("Version 1")
    card.get_by_role("button", name="Open record").click()
    expect(dialog).to_contain_text("Every quote has a source and date")
    expect(dialog).to_contain_text("Read supplier records")
    dialog.get_by_role("button", name="Edit record").click()
    dialog.get_by_label("Overview", exact=True).fill(
        "Compare current quotes after reading retained sources."
    )
    dialog.get_by_role("button", name="Save revision", exact=True).click()
    expect(dialog).not_to_be_visible()
    expect(card).to_contain_text("Version 2")
    card.get_by_role("button", name="Open record").click()
    dialog.get_by_text("Revision history", exact=True).click()
    expect(dialog).to_contain_text("Version 1")
    dialog.get_by_text("Version 1 ·", exact=False).click()
    expect(dialog).to_contain_text("Check existing evidence before refreshing quotes.")
