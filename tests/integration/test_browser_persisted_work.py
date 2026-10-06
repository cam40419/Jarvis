import os
import subprocess
import sys
import time

import pytest

from simon.adapters.postgres import PostgresStore
from simon.domain.context import CreateMemory
from tests.contract.test_connected import connected_setup
from tests.integration.test_restart import running_api

pytestmark = [pytest.mark.postgres, pytest.mark.browser]


def test_project_page_and_sessions_survive_closed_browser(postgres_url, tmp_path, monkeypatch):
    if os.environ.get("SIMON_BROWSER_TESTS") != "1":
        pytest.skip("set SIMON_BROWSER_TESTS=1")
    from playwright.sync_api import expect, sync_playwright

    connected, actor, token = connected_setup(PostgresStore(postgres_url))
    project = connected.memories.create(
        actor,
        CreateMemory(
            subject="Background project",
            content="Keep working across sessions",
            scope="personal",
            category="project",
            idempotency_key="background-project",
        ),
    )
    monkeypatch.setenv("SIMON_OPENAI_API_KEY", "synthetic-no-network-key")
    with (
        running_api(postgres_url, tmp_path / "background-api.log", model_provider="openai") as api,
        sync_playwright() as pw,
    ):
        options = {"headless": True}
        if os.environ.get("SIMON_BROWSER_CHANNEL"):
            options["channel"] = os.environ["SIMON_BROWSER_CHANNEL"]
        browser = pw.chromium.launch(**options)
        worker = None
        try:
            context = browser.new_context()
            origin = str(api.base_url).rstrip("/")
            context.add_cookies(
                [{"name": "simon_session", "value": token, "url": origin, "sameSite": "Strict"}]
            )
            page = context.new_page()
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            project_url = origin + "/chat?project=" + str(project.id)
            page.goto(project_url)
            expect(page.locator("#project-page-title")).to_have_text("Background project")
            page.locator("#pc-tab-sessions").click()
            page.locator("#project-work-title").fill("Independent task")
            page.locator("#project-work-instructions").fill("Write a useful result")
            page.get_by_role("button", name="Start background work", exact=True).click()
            expect(page.locator("#project-work-feedback")).to_contain_text("Saved")
            page.get_by_role("button", name="Start a session").click()
            page.locator("#text").fill("Review this project's next steps")
            page.locator("#text").press("Enter")
            expect(page.locator("#status")).to_contain_text("Saved.")
            first_thread = page.url.split("#")[-1]
            expect(page.locator("#new-chat")).to_be_enabled()
            page.locator("#new-chat").click()
            page.locator("#text").fill("A second independent session")
            page.locator("#text").press("Enter")
            expect(page.locator("#status")).to_contain_text("Saved.")
            page.close()
            # Launch the real standalone worker, substituting only its external model adapter.
            code = """
import time
import simon.adapters.openai_model as adapter
from tests.contract.test_model_runs import FakeModel
class Model(FakeModel):
    def __init__(self, *args, **kwargs): super().__init__()
    def generate_with_tools(self, request, delta, execute):
        time.sleep(2)
        return self.generate_stream(request, delta or (lambda text: None))
adapter.OpenAIModel = Model
from simon.workflow_worker import main
main()
"""
            env = os.environ | {
                "SIMON_STORAGE_BACKEND": "postgres",
                "SIMON_DATABASE_URL": postgres_url,
                "SIMON_MODEL_PROVIDER": "openai",
            }
            with (tmp_path / "background-worker.log").open("w") as log:
                worker = subprocess.Popen(
                    [sys.executable, "-c", code],
                    env=env,
                    stdout=log,
                    stderr=log,
                    creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
                )
                deadline = time.monotonic() + 25
                while time.monotonic() < deadline:
                    jobs = connected.store.jobs(
                        actor.workspace_id, actor.actor_id, "assistant.session", 0, 20
                    )
                    tasks = connected.store.jobs(
                        actor.workspace_id, actor.actor_id, "assistant.task", 0, 20
                    )
                    if len(jobs) == 2 and all(job.status == "succeeded" for job in [*jobs, *tasks]):
                        break
                    assert worker.poll() is None, "Worker exited; inspect test log"
                    time.sleep(0.2)
                else:
                    pytest.fail("Background work did not complete after browser closed")
                page = context.new_page()
                page.on("pageerror", lambda error: errors.append(str(error)))
                page.goto(project_url)
                expect(page.locator("#project-page-title")).to_have_text("Background project")
                page.locator("#pc-tab-sessions").click()
                expect(page.locator("#project-page-sessions")).to_contain_text("succeeded")
                expect(page.locator("#project-page-tasks")).to_contain_text("Complete")
                page.get_by_role("button", name="Open session", exact=True).click()
                expect(page.locator("#messages")).to_contain_text("A useful answer.")
                assert page.url.endswith("#" + first_thread)
                page.reload()
                expect(page.locator("#messages")).to_contain_text("A useful answer.")
                assert not errors
        finally:
            if worker:
                worker.terminate()
                worker.wait(timeout=10)
            browser.close()
