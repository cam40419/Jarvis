"""Exercise link resolution and rejection in the actual browser DOM, offline."""

import os
from pathlib import Path

import pytest

pytestmark = pytest.mark.browser


@pytest.mark.parametrize("base", ["", "/simon", "/simon/"])
def test_markdown_saved_file_links_respect_mount_and_reject_unsafe_urls(base):
    if os.environ.get("SIMON_BROWSER_TESTS") != "1":
        pytest.skip("set SIMON_BROWSER_TESTS=1 to run browser checks")
    from playwright.sync_api import expect, sync_playwright

    root = Path(__file__).resolve().parents[2]
    mount = base.rstrip("/")
    file_path = "/v1/local-files/download?root=workspace&path=research%2Freport.md"
    origin = "https://simon.example.test"
    with sync_playwright() as playwright:
        options = {"headless": True}
        if os.environ.get("SIMON_BROWSER_CHANNEL"):
            options["channel"] = os.environ["SIMON_BROWSER_CHANNEL"]
        browser = playwright.chromium.launch(**options)
        try:
            context = browser.new_context()
            context.route("**/*", lambda route: route.fulfill(body="Saved report"))
            page = context.new_page()
            page.goto(origin + mount + "/")
            page.set_content(f'<meta name="simon-base" content="{base}"><main></main>')
            # No app-path.js dependency: snapshots and previews load this renderer alone.
            page.add_script_tag(path=str(root / "src/simon/api/static/markdown.js"))
            unsafe = [
                "javascript:alert",
                "data:text/html,test",
                "file:///report.md",
                "sandbox:/mnt/data/report.md",
                "sandbox:/v1/other?file=report.md",
                "sandbox://outside.example/report.md",
                "sandbox:/v1/local-files/download/other?file=report.md",
                "sandbox:/v1/local-files/download?path=bad%0aname",
                "//outside.example/report",
                "/\\outside.example/report",
                "https://good.example\\@outside.example/report",
                "/%2foutside.example/report",
                "/%5coutside.example/report",
                "/v1/../outside",
                "/%2e%2e/outside",
                "/v1/file%0d%0aLocation:test",
                "/v1/file\x00name",
                "https://outside.example/\tpage",
            ]
            markdown = (
                f"[Report]({file_path}) [Already mounted]({mount + file_path}) "
                f"[Actual saved answer](sandbox:{file_path}) "
                "[External](https://example.com/source) [HTTP](http://example.com/source)\n\n"
                + "\n\n".join(f"[Unsafe {index}]({value})" for index, value in enumerate(unsafe))
            )
            page.evaluate(
                "text => document.querySelector('main').append(SimonMarkdown.render(text))",
                markdown,
            )
            expect(page.locator("a")).to_have_count(5)
            for label in ["Report", "Already mounted", "Actual saved answer"]:
                link = page.get_by_role("link", name=label, exact=True)
                expect(link).to_have_attribute("href", mount + file_path)
                expect(link).to_have_attribute("target", "_blank")
                expect(link).to_have_attribute("rel", "noopener noreferrer")
                assert link.evaluate("link => link.href") == origin + mount + file_path
            expect(page.get_by_role("link", name="External", exact=True)).to_have_attribute(
                "href", "https://example.com/source"
            )
            expect(page.get_by_role("link", name="HTTP", exact=True)).to_have_attribute(
                "href", "http://example.com/source"
            )
            with page.expect_popup() as popup:
                page.get_by_role("link", name="Report", exact=True).click()
            expect(popup.value).to_have_url(origin + mount + file_path)
            expect(popup.value.locator("body")).to_contain_text("Saved report")
            with page.expect_popup() as actual_popup:
                page.get_by_role("link", name="Actual saved answer", exact=True).click()
            expect(actual_popup.value).to_have_url(origin + mount + file_path)
        finally:
            browser.close()
