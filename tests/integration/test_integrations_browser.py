"""Connect and reconnect from the browser against real backend persistence."""

import httpx
import pytest

from tests.integration.test_project_command_browser import project_ui as project_ui

pytestmark = pytest.mark.browser


def test_clickup_connect_reconnect_disconnect_without_files(project_ui, tmp_path):
    from playwright.sync_api import expect

    page, _, _, container, _ = project_ui
    from urllib.parse import urlsplit

    parsed = urlsplit(page.url)
    origin = parsed.scheme + "://" + parsed.netloc
    container.settings.integration_key_file = tmp_path / "credentials.key"
    service = container.project_boards.integrations

    def send(request):
        assert request.url.path == "/api/v2/team"
        return httpx.Response(200, json={"teams": [{"id": "123", "name": "Company"}]})

    service.clickup.http.transport = httpx.MockTransport(send)
    page.locator("#connections-open").click()
    page.locator("#integration-name").fill("My ClickUp")
    page.locator("#integration-credential").fill("pk_browser-token")
    page.locator("#integration-connect").click()
    accounts = page.locator("#integration-accounts")
    expect(accounts).to_contain_text("My ClickUp")
    expect(page.locator("#integration-credential")).to_have_value("")
    original = page.request.get(origin + "/v1/connections/integrations").json()[0]
    assert "pk_browser-token" not in str(original)
    accounts.get_by_role("button", name="Reconnect", exact=True).click()
    page.locator("#integration-credential").fill("pk_rotated-token")
    page.locator("#integration-connect").click()
    expect(page.locator("#integration-status")).to_contain_text("Connected")
    expect(page.locator("#integration-cancel")).to_be_hidden()
    assert (
        page.request.get(origin + "/v1/connections/integrations").json()[0]["id"] == original["id"]
    )
    page.reload()
    page.locator("#connections-open").click()
    expect(accounts).to_contain_text("My ClickUp")
    accounts.get_by_role("button", name="Disconnect", exact=True).click()
    expect(accounts).not_to_contain_text("My ClickUp")
    assert page.request.get(origin + "/v1/connections/integrations").json() == []


def test_connection_center_lists_unlinked_services_and_persists_storage_test(project_ui):
    from playwright.sync_api import expect

    page, _, _, container, _ = project_ui
    requests = []

    def send(request):
        requests.append(request)
        assert request.url.path == "/2/files/get_metadata"
        return httpx.Response(200, json={".tag": "folder", "path_lower": "/simon"})

    container.connected.integrations.clickup.http.transport = httpx.MockTransport(send)
    page.locator("#connections-open").click()
    center = page.locator("#connections-panel")
    expect(center).to_be_visible()
    assert center.bounding_box()["width"] > 420
    expect(page.locator("#connections-catalog")).to_contain_text("Dropbox")
    expect(page.locator("#connections-catalog")).to_contain_text("Box")
    expect(page.locator("#connections-catalog")).to_contain_text("OneDrive")
    expect(page.locator("#connections-catalog")).to_contain_text("WebDAV")
    page.locator("#integration-provider").select_option("dropbox")
    page.locator("#integration-name").fill("Project storage")
    page.locator("#integration-credential").fill("private-storage-token")
    page.locator("#integration-root-path").fill("/Simon")
    page.locator("#integration-connect").click()
    expect(page.locator("#integration-status")).to_contain_text("Connected")
    accounts = page.locator("#integration-accounts")
    accounts.get_by_role("button", name="Test", exact=True).click()
    expect(accounts).to_contain_text("Connection test passed")
    saved = page.evaluate("() => api('/v1/connections/integrations')")[0]
    assert saved["settings"]["connection_test"]["status"] == "passed"
    assert "private-storage-token" not in str(saved)
    assert len(requests) == 2
    page.reload()
    page.locator("#connections-open").click()
    expect(accounts).to_contain_text("Connection test passed")
