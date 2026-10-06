"""Background project refreshes preserve the focused editor and its caret."""

import pytest

from tests.integration.test_project_command_browser import create_project
from tests.integration.test_project_command_browser import project_ui as project_ui

pytestmark = pytest.mark.browser


@pytest.mark.parametrize("field_id", ["pc-command", "project-work-instructions", "sa-message"])
def test_project_refresh_does_not_interrupt_typing(project_ui, field_id):
    from playwright.sync_api import expect

    page, *_ = project_ui
    project_id = create_project(page, "Uninterrupted writing")
    if field_id == "project-work-instructions":
        page.locator("#pc-tab-sessions").click()
        expect(page.locator("#project-work-instructions")).to_be_visible()
        # Settle the navigation request before testing independent background refreshes.
        page.evaluate("() => SimonWork.refreshProject()")
    elif field_id == "sa-message":
        page.locator("#pc-settings").click()
        page.get_by_role("button", name="Describe a team", exact=True).click()
        expect(page.locator("#sa-send")).to_be_enabled()
    else:
        page.evaluate("() => SimonWork.refreshProject()")

    field = page.locator("#" + field_id)
    draft = "Research our brand and write the launch brief."
    field.fill(draft)
    field.evaluate("""input => {
        input.setSelectionRange(9, 18, 'backward');
        window.typingBlurCount = 0;
        input.addEventListener('blur', () => window.typingBlurCount++);
    }""")
    page.evaluate(
        """id => api('/v1/projects/' + id + '/activity', {
            entry: {kind: 'progress', text: 'New progress while the user is typing.'},
            idempotency_key: 'typing-focus-progress-update'
        })""",
        project_id,
    )
    for _ in range(3):
        # This is the same refresh used by Work's 15-second background timer.
        page.evaluate("() => SimonWork.refreshProject()")
        page.evaluate("() => SimonProjectCommand.refresh(true)")
        expect(field).to_be_focused()
        expect(field).to_have_value(draft)
        assert field.evaluate(
            "input => [input.selectionStart, input.selectionEnd, input.selectionDirection]"
        ) == [9, 18, "backward"]
        assert page.evaluate("window.typingBlurCount") == 0

    assert page.evaluate("""() => SimonProjectCommand.getSnapshot().detail.activity.items.some(
        entry => entry.text === 'New progress while the user is typing.'
    )""")

    # Actual keyboard input goes to the existing editor, without clicking it again.
    page.keyboard.type("the collection")
    expect(field).to_have_value(draft[:9] + "the collection" + draft[18:])
    assert project_id in page.url
    assert page.evaluate("() => SimonWork.getProject().id") == project_id
