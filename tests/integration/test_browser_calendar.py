import json
import os
from uuid import uuid4

import pytest

from simon.adapters.postgres import PostgresStore
from simon.domain.connected_tools import CalendarDraft
from simon.domain.conversations import CreateThread, SubmitRun
from simon.services.model_conversations import ModelConversationService
from tests.contract.test_calendar_immediate import pending_calendar
from tests.contract.test_connected import EVENT, connected_setup
from tests.contract.test_model_runs import FakeModel
from tests.integration.test_restart import running_api

pytestmark = [pytest.mark.postgres, pytest.mark.browser]


@pytest.mark.parametrize("interrupted", [False, True])
def test_direct_calendar_receipt_has_no_confirmation_and_survives_reload(
    postgres_url,
    tmp_path,
    interrupted,
):
    if os.environ.get("SIMON_BROWSER_TESTS") != "1":
        pytest.skip("set SIMON_BROWSER_TESTS=1")
    from playwright.sync_api import expect, sync_playwright

    service, actor, token = connected_setup(PostgresStore(postgres_url))
    writes = []
    service.api.execute = lambda *args: (
        writes.append(1) or "synthetic",
        "https://www.google.com/calendar/event?eid=synthetic",
    )
    if interrupted:
        attempt = pending_calendar(service, actor)
        receipt = service.create_calendar_event(
            actor,
            attempt.run.id,
            CalendarDraft(**EVENT),
            lambda: actor,
        )
        service.store.save_attempt(attempt.model_copy(update={"status": "failed"}))
        thread_id = attempt.run.thread_id
    else:
        model = FakeModel()

        def generate(request, on_delta, execute):
            execute("calendar_create_event", json.dumps(EVENT))
            return model.generate(request)

        model.generate_with_tools = generate
        conversations = ModelConversationService(
            service.store,
            service.audit,
            model,
            service.settings,
            service,
        )
        thread = conversations.create(
            actor,
            CreateThread(title="Calendar", idempotency_key=str(uuid4())),
        )
        run = conversations.submit(
            actor,
            thread.id,
            SubmitRun(text="Create lunch", idempotency_key=str(uuid4())),
        )
        receipt = service.get_action(actor, run.action_ids[0])
        thread_id = thread.id
    assert writes == [1]
    with running_api(postgres_url, tmp_path / "calendar.log") as api, sync_playwright() as pw:
        options = {"headless": True}
        if os.environ.get("SIMON_BROWSER_CHANNEL"):
            options["channel"] = os.environ["SIMON_BROWSER_CHANNEL"]
        browser = pw.chromium.launch(**options)
        try:
            context = browser.new_context(timezone_id="America/New_York")
            origin = str(api.base_url).rstrip("/")
            context.add_cookies(
                [
                    {
                        "name": "simon_session",
                        "value": token,
                        "url": origin,
                        "sameSite": "Strict",
                    }
                ]
            )
            page = context.new_page()
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.goto(origin + "/chat#" + str(thread_id))
            for _ in range(2):
                card = page.get_by_role("region", name="Calendar result")
                expect(card).to_have_count(1)
                expect(card).to_contain_text("Calendar event created")
                expect(card).to_contain_text("Oct 1, 2026")
                expect(card).to_contain_text("12:00 PM")
                expect(card).to_contain_text("America/New_York")
                expect(card.get_by_role("button", name="Confirm & create event")).to_have_count(0)
                expect(card.get_by_role("link", name="Open calendar event")).to_have_attribute(
                    "href",
                    str(receipt.result_url),
                )
                page.reload()
            assert not errors
        finally:
            browser.close()
