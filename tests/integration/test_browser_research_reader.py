"""Actual static-browser extraction with synthetic pages; no provider requests."""

import os

import pytest

from simon.adapters import _browser_runner as runner

pytestmark = pytest.mark.browser


@pytest.fixture
def static_page():
    if os.environ.get("SIMON_BROWSER_TESTS") != "1":
        pytest.skip("set SIMON_BROWSER_TESTS=1")
    from playwright.sync_api import sync_playwright

    with sync_playwright() as provider:
        browser = provider.chromium.launch(
            channel=os.environ.get("SIMON_BROWSER_CHANNEL") or None, headless=True
        )
        context = browser.new_context(java_script_enabled=False, service_workers="block")
        context.route("**/*", lambda route: route.abort())
        page = context.new_page()
        try:
            yield page
        finally:
            browser.close()


@pytest.mark.parametrize("container", ["main", 'div role="main"', "article"])
def test_read_extraction_prioritizes_source_content_over_navigation_without_changing_dom(
    static_page, container
):
    tag = container.split()[0]
    markup = (
        "<title>Supplier capabilities</title><header>Global header</header><nav>"
        + "A long navigation menu. " * 500
        + '<a href="/navigation">Navigation link</a></nav>'
        + f"<{container}><header><h1>Custom production</h1>By Jane Smith "
        + '<time datetime="2026-09-30">September 30, 2026</time></header>'
        + '<header role="banner">Repeated site banner</header>'
        + "<p>Minimum order: 25 garments.</p>"
        + "<table><tr><th>Service</th><th>Time</th></tr><tr><td>Sampling</td><td>14 days</td>"
        + '</tr></table><a href="/capabilities">Production capabilities</a>'
        + '<aside id="cookie-banner">Accept cookies<a href="/privacy">Privacy</a></aside>'
        + "<footer>Production figures verified by the factory.</footer>"
        + '<footer role="contentinfo">Repeated site information</footer>'
        + f"</{tag}><footer>Footer material</footer>"
    )
    static_page.set_content(markup)
    before = static_page.content()
    extracted = runner.read_document(static_page, 2000, "https://supplier.example/about")
    assert "Minimum order: 25 garments." in extracted["text"]
    assert "Custom production" in extracted["text"]
    assert "By Jane Smith September 30, 2026" in extracted["text"]
    assert "Production figures verified by the factory." in extracted["text"]
    assert "Sampling\t14 days" in extracted["text"]
    assert not any(
        value in extracted["text"]
        for value in ("navigation", "Footer", "cookies", "Global", "Repeated site")
    )
    assert extracted["text_truncated"] is False
    assert extracted["links"] == [
        {"url": "https://supplier.example/capabilities", "title": "Production capabilities"}
    ]
    assert static_page.content() == before
    assert "A long navigation menu." in static_page.locator("body").inner_text()


def test_empty_or_hidden_main_falls_back_to_useful_body_only_content(static_page):
    static_page.set_content(
        "<header>Navigation heading</header><main hidden>Hidden page placeholder</main>"
        '<div style="display:none"><main>Hidden ancestor placeholder</main></div>'
        "<main><nav>Only navigation</nav></main><div><h1>Body-only supplier page</h1>"
        "<p>Custom patterns require a supplied tech pack.</p>"
        "<p>Our cookie-print collection is available in cotton.</p>"
        '<a href="/quote">Request pricing</a></div>'
        '<div role="dialog" aria-label="Cookie consent">Tracking notice</div><footer>Links</footer>'
    )
    extracted = runner.read_document(static_page, 2000, "https://supplier.example/")
    assert "Custom patterns require a supplied tech pack." in extracted["text"]
    assert "cookie-print collection" in extracted["text"]
    assert "Hidden page placeholder" not in extracted["text"]
    assert "Hidden ancestor placeholder" not in extracted["text"]
    assert "Tracking notice" not in extracted["text"]
    assert "Only navigation" not in extracted["text"]
    assert extracted["links"][0]["url"] == "https://supplier.example/quote"
    assert extracted["text_truncated"] is False


def test_read_text_and_link_limits_are_independent(static_page):
    static_page.set_content(
        "<main><p>Useful text.</p>" + '<a href="/source"></a>' * 250 + "</main>"
    )
    extracted = runner.read_document(static_page, 100, "https://supplier.example/")
    assert extracted["text"] == "Useful text."
    assert extracted["text_truncated"] is False
    assert extracted["links_truncated"] is True
    static_page.set_content("<main><p>" + "Source fact. " * 100 + "</p></main>")
    extracted = runner.read_document(static_page, 100, "https://supplier.example/")
    assert len(extracted["text"]) == 100 and extracted["text_truncated"] is True
    assert extracted["links_truncated"] is False


def test_public_source_reader_returns_content_and_followable_links_without_page_scripts(
    monkeypatch, tmp_path
):
    if os.environ.get("SIMON_BROWSER_TESTS") != "1":
        pytest.skip("set SIMON_BROWSER_TESTS=1")
    from playwright.sync_api import BrowserType

    original_launch = BrowserType.launch

    def launch(browser, **kwargs):
        # Production runner is Linux-only; this test also supports installed Edge
        # on Windows. Keep the runner's DNS-disable flag and isolated context.
        kwargs.pop("env", None)
        if channel := os.environ.get("SIMON_BROWSER_CHANNEL"):
            kwargs["channel"] = channel
        return original_launch(browser, **kwargs)

    monkeypatch.setattr(BrowserType, "launch", launch)
    calls = []
    html = b"""<!doctype html><html><head><title>Supplier production capabilities</title>
    <script>document.title='UNSAFE SCRIPT';fetch('https://not-allowed.example/beacon')</script>
    </head><body><h1>Made to order</h1><p>Minimum order: 25 garments.</p>
    <a href="/production#terms">Production terms</a>
    <a href="https://brand.example/work">Brand work</a>
    <a href="javascript:alert(1)">Bad link</a></body></html>"""

    def get(fetcher, url):
        calls.append(url)
        assert fetcher.public_web is True
        assert url == "https://supplier.example/about"
        return url, 200, "text/html; charset=utf-8", html

    monkeypatch.setattr(runner.Fetcher, "get", get)
    result = runner.run(
        {
            "operation": "browser.read",
            "arguments": {"url": "https://supplier.example/about", "max_text_chars": 1000},
            "allowed_origins": [],
            "public_web": True,
            "timeout_seconds": 30,
        },
        tmp_path,
    )
    assert calls == ["https://supplier.example/about"]
    assert result["title"] == "Supplier production capabilities"
    assert result["url"] == "https://supplier.example/about"
    assert "Minimum order: 25 garments." in result["text"]
    assert result["links"] == [
        {"url": "https://supplier.example/production", "title": "Production terms"},
        {"url": "https://brand.example/work", "title": "Brand work"},
    ]
    assert result["links_truncated"] is False
    assert result["screenshot_path"] is None
