import pytest

from tests.integration.test_agent_work_browser import agent_ui  # noqa: F401
from tests.integration.test_local_files_api import png_bytes

pytestmark = pytest.mark.browser


def test_upload_image_preview_and_pdf_link_use_authenticated_routes(request, tmp_path):
    from playwright.sync_api import expect

    page, _, container = request.getfixturevalue("agent_ui")
    container.connected.settings = container.connected.settings.model_copy(update={
        "local_files_enabled": True, "local_files_dir": tmp_path / "files",
    })
    page.locator("#local-files-open").click()
    panel = page.locator("#local-files-panel")
    expect(panel.locator("#local-file-upload")).to_be_visible()
    panel.locator("#local-file-upload").set_input_files({
        "name": "preview.png", "mimeType": "image/png", "buffer": png_bytes(),
    })
    expect(panel.get_by_role("button", name="Preview image")).to_be_visible()
    panel.get_by_role("button", name="Preview image").click()
    image = panel.get_by_role("img", name="preview.png")
    expect(image).to_be_visible()
    page.wait_for_function("document.querySelector('.local-file-image')?.naturalWidth === 1")
    assert "/v1/local-files/preview?" in image.get_attribute("src")
    panel.locator("#local-file-upload").set_input_files({
        "name": "report.pdf", "mimeType": "application/pdf", "buffer": b"%PDF-1.7\n%%EOF",
    })
    link = panel.get_by_role("link", name="View report.pdf in a new tab")
    expect(link).to_be_visible()
    assert link.get_attribute("target") == "_blank"
    assert "noopener" in link.get_attribute("rel")
    assert "/v1/local-files/preview?" in link.get_attribute("href")
    panel.get_by_label("Filter this page").fill("report")
    expect(panel.get_by_role("article", name="report.pdf", exact=True)).to_be_visible()
    expect(panel.get_by_role("article", name="preview.png", exact=True)).to_be_hidden()
    panel.get_by_label("Filter this page").fill("missing")
    expect(panel.get_by_text("No file names match on this page.")).to_be_visible()
    panel.get_by_label("Filter this page").fill("")
    expect(panel.get_by_role("article", name="preview.png", exact=True)).to_be_visible()
