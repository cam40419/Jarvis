"""Real browser/API review flow with an isolated synthetic provider transport."""

import httpx
import pytest

from simon.domain.external_actions import CallDetails
from tests.integration.test_agent_work_browser import agent_ui  # noqa: F401
from tests.unit.test_external_actions import actor, call_draft, setup

pytestmark = pytest.mark.browser


def test_phone_review_exact_details_confirmation_and_mobile(agent_ui, tmp_path):  # noqa: F811
    from playwright.sync_api import expect

    page, _, container = agent_ui
    service, requests = setup(store=container.store)
    container.external_actions.__dict__.update(service.__dict__)
    draft = call_draft(call=CallDetails(message='<img src=x onerror="window.bad=1"> Test message.'))
    proposal = container.external_actions.propose(actor(), draft, "browser-call-review")
    page.evaluate("id => window.simonExternalActions.open(id)", str(proposal.id))
    review = page.locator("#external-actions .ea-detail")
    expect(review).to_contain_text("+12025550123")
    expect(review).to_contain_text("+12025550199")
    expect(review).to_contain_text(draft.call.message)
    assert review.locator("img").count() == 0
    assert page.evaluate("window.bad") is None
    submit = review.get_by_role("button", name="Place reviewed call", exact=True)
    expect(submit).to_be_disabled()
    review.get_by_role("checkbox").check()
    expect(submit).to_be_enabled()
    submit.click()
    expect(review).to_contain_text("Provider accepted")
    expect(review.get_by_role("button", name="Place reviewed call")).to_have_count(0)
    assert len(requests) == 1
    page.reload()
    page.get_by_role("button", name="Work", exact=True).click()
    expect(review).to_contain_text("Provider accepted")
    assert len(requests) == 1
    page.locator("#external-actions").screenshot(
        path=str(tmp_path / "external-actions-desktop.png"), animations="disabled"
    )
    page.set_viewport_size({"width": 390, "height": 844})
    expect(review).to_be_visible()
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    page.locator("#external-actions").screenshot(
        path=str(tmp_path / "external-actions-mobile.png"), animations="disabled"
    )


def test_unconfigured_is_blocked_and_unknown_has_no_retry(agent_ui):  # noqa: F811
    from playwright.sync_api import expect

    page, _, container = agent_ui
    service, requests = setup(store=container.store, definitions=[])
    container.external_actions.__dict__.update(service.__dict__)
    proposal = container.external_actions.propose(actor(), call_draft(), "browser-blocked-call")
    page.evaluate("id => window.simonExternalActions.open(id)", str(proposal.id))
    review = page.locator("#external-actions .ea-detail")
    expect(review).to_contain_text("No provider is configured")
    expect(review.get_by_role("checkbox")).to_be_disabled()
    expect(review.get_by_role("button", name="Place reviewed call")).to_be_disabled()
    review.get_by_role("button", name="Cancel proposal").click()
    expect(review).to_contain_text("Proposal cancelled")
    assert not requests

    def timeout(request):
        raise httpx.ReadTimeout("synthetic timeout", request=request)

    service, requests = setup(timeout, store=container.store)
    container.external_actions.__dict__.update(service.__dict__)
    proposal = container.external_actions.propose(actor(), call_draft(), "browser-unknown-call")
    page.evaluate("id => window.simonExternalActions.open(id)", str(proposal.id))
    review.get_by_role("checkbox").check()
    review.get_by_role("button", name="Place reviewed call").click()
    expect(review).to_contain_text("Outcome needs review")
    expect(review).to_contain_text("will not be automatically repeated")
    expect(review.get_by_role("button", name="Place reviewed call")).to_have_count(0)
    review.get_by_label("Verified outcome").select_option("not_completed")
    review.get_by_label("Provider evidence or reference").fill(
        "Synthetic provider log has no call."
    )
    review.get_by_label("Reconciliation note").fill("I checked the reviewed destination and time.")
    review.get_by_role("button", name="Record user reconciliation").click()
    expect(review).to_contain_text("User reconciled")
    expect(review).to_contain_text("not provider confirmation")
    assert len(requests) == 1
