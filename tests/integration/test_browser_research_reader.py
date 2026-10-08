"""Actual static-browser extraction with synthetic pages; no provider requests."""

import os
from types import SimpleNamespace

import pytest

from simon.adapters import _browser_runner as runner

pytestmark = pytest.mark.browser


@pytest.fixture
def runner_browser(monkeypatch):
    if os.environ.get("SIMON_BROWSER_TESTS") != "1":
        pytest.skip("set SIMON_BROWSER_TESTS=1")
    from playwright.sync_api import BrowserType

    original_launch = BrowserType.launch

    def launch(browser, **kwargs):
        # Production workers are Linux-only; retain the DNS isolation flag when testing on Windows.
        kwargs.pop("env", None)
        if channel := os.environ.get("SIMON_BROWSER_CHANNEL"):
            kwargs["channel"] = channel
        return original_launch(browser, **kwargs)

    monkeypatch.setattr(BrowserType, "launch", launch)


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
    monkeypatch, tmp_path, runner_browser
):
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


def test_offline_renderer_blocks_network_scripts_and_preserves_existing_output(
    monkeypatch, tmp_path, runner_browser
):
    source = tmp_path / "board.html"
    source.write_text(
        "<title>Collection board</title><style>body{background:#e8d8c5}</style>"
        "<script>document.title='UNSAFE SCRIPT'</script>"
        '<img src="https://unapproved.example/logo.png"><main>'
        + "Essentials in natural cotton. " * 20
        + "</main>",
        encoding="utf-8",
    )

    def no_network(*args):
        pytest.fail("Offline HTML rendering attempted a network fetch")

    monkeypatch.setattr(runner.Fetcher, "get", no_network)
    request = {
        "operation": "browser.render_html",
        "arguments": {"input": "board.html", "output": "drafts/board.png", "max_text_chars": 100},
        "allowed_origins": [],
        "timeout_seconds": 30,
    }
    result = runner.run(request, tmp_path)
    image = (tmp_path / "drafts" / "board.png").read_bytes()
    assert result["title"] == "Collection board"
    assert result["url"] == "local:board.html"
    assert result["screenshot_path"] == "drafts/board.png"
    assert result["text"].startswith("Essentials in natural cotton.")
    assert len(result["text"]) == 100 and result["text_truncated"]
    assert result["blocked_requests"] >= 1
    assert image.startswith(b"\x89PNG\r\n\x1a\n") and len(image) > 100
    with pytest.raises(ValueError, match="already exists"):
        runner.run(request, tmp_path)
    assert (tmp_path / "drafts" / "board.png").read_bytes() == image


def test_remote_screenshot_fetches_only_permitted_resources_without_credentials(
    monkeypatch, tmp_path, runner_browser
):
    responses = {
        "/lookbook": (
            "text/html",
            b"<title>Lookbook</title><link rel='stylesheet' href='/style.css'>"
            b"<img src='https://unapproved.example/tracker.png'><main>Autumn collection</main>",
        ),
        "/style.css": ("text/css", b"body{background:#eadfca;color:#283625}"),
    }
    calls, resolved = [], []

    def resolve(hostname):
        resolved.append(hostname)
        return "93.184.216.34"

    class Connection:
        def __init__(self, hostname, address, *, timeout):
            assert hostname == "supplier.example" and address == "93.184.216.34"
            assert 0 < timeout <= 10
            self.path = None

        def request(self, method, path, *, headers):
            assert method == "GET"
            assert "Cookie" not in headers and "Authorization" not in headers
            calls.append(path)
            self.path = path

        def getresponse(self):
            media_type, body = responses[self.path]
            return SimpleNamespace(
                status=200,
                getheader=lambda name, default=None: (
                    media_type if name == "Content-Type" else default
                ),
                read=lambda limit: body[:limit],
            )

        def close(self):
            pass

    monkeypatch.setattr(runner, "public_address", resolve)
    monkeypatch.setattr(runner, "PinnedHTTPSConnection", Connection)
    result = runner.run(
        {
            "operation": "browser.screenshot",
            "arguments": {"url": "https://supplier.example/lookbook", "output": "lookbook.png"},
            "allowed_origins": ["https://supplier.example"],
            "timeout_seconds": 30,
        },
        tmp_path,
    )
    assert result["title"] == "Lookbook"
    assert result["text"] == "Autumn collection"
    assert result["url"] == "https://supplier.example/lookbook"
    assert result["blocked_requests"] >= 1
    assert result["screenshot_path"] == "lookbook.png"
    assert (tmp_path / "lookbook.png").read_bytes().startswith(b"\x89PNG\r\n\x1a\n")
    assert calls == ["/lookbook", "/style.css"]
    assert resolved == ["supplier.example", "supplier.example"]
