# Static browser tools

The browser worker reads public HTTPS pages, captures viewport screenshots, and
renders local HTML previews. It has no login, form submission, clicking, desktop
control, or page-script execution. It is useful for static research pages and
previewing generated HTML; applications that require JavaScript need a separately
authorized future browser workflow.

## Provisioning

```powershell
docker build --file deploy/Dockerfile.browser-worker --tag simon-browser:local .
python examples/agents/browser_smoke.py
# Also exercise the explicitly allowed public example.com page:
python examples/agents/browser_smoke.py --online
```

The image pins Playwright 1.62.0 and installs its matching Chromium. Run it with
the existing dedicated workspace, read-only root filesystem, worker UID/GID,
resource limits, and no extra host mounts. Use capabilities `browser` and `python`.
The normal [environment provisioning requirements](agent-environments.md) apply.

Use two separate environment definitions:

| Use                              | Network  | Tools                                |
| -------------------------------- | -------- | ------------------------------------ |
| Generated HTML previews          | `none`   | `browser.render_html`                |
| Public HTTPS reading/screenshots | `bridge` | `browser.read`, `browser.screenshot` |

Copy selected [tool definitions](../../examples/agents/browser-tools.example.json)
into the platform manifest. Enable/configure them only after provisioning the
image. The factory is `browser_tool_definitions()` in `simon.adapters.browser_tools`.
Grant the profile its explicit tool IDs/environment IDs and existing
`jobs:read`/`jobs:write` scopes. Screenshots and previews require the write action.

Online tools also need a nonempty operator-controlled `settings.allowed_origins`,
for example `["https://example.com"]`. The default empty list grants no online
destinations. Grants are exact HTTPS origins on port 443; wildcard hosts,
credentials, paths, query strings, and private/loopback destinations are rejected.
Add a page's asset/CDN origins explicitly if its styling requires them.

## Arguments and results

- `browser.read`: `url`, optional `max_text_chars`.
- `browser.screenshot`: `url`, new PNG `output`, optional `max_text_chars`.
- `browser.render_html`: local `.html`/`.htm` `input`, new PNG `output`, optional
  `max_text_chars`. Use inline CSS/images for self-contained previews.

Paths are relative to the current lease workspace and cannot traverse directories,
use symlinks, or target Git metadata. Existing screenshots are preserved. The
viewport is fixed at 1365 × 900; screenshots capture that viewport, not an
unbounded full page.

The normal execution result contains `exit_code`, `stdout`, `stderr`, and
`truncated`. Successful stdout is JSON containing `title`, final `url`, HTTP
`status`, bounded `text`, `text_truncated`, `blocked_requests`, and optional
`screenshot_path`. Include the screenshot's relative path in the agent's final
artifact list to publish it to Simon's durable library.

## Retrieval limits

Every network request uses a bounded HTTPS fetcher. It validates each redirect,
resolves the hostname only to public addresses, connects to that checked address,
and verifies TLS against the original hostname. It ignores environment proxies
and never sends cookies, authorization headers, or credentials. It permits only
GET requests, up to 5 redirects, 40 total requests, 2 MiB per response, and 10 MiB
combined content. Compressed responses are rejected to preserve those byte bounds.
The text budget defaults to 8,000 characters and can reach 16,000.

Chromium receives fetched content through intercepted responses; requests are
never continued onto Chromium's own network stack. Resource origins face the
same checks as page origins. Page scripts, service workers, frames, objects, forms,
and downloads are disabled. Browser contexts are fresh per invocation and do not
reuse browsing state. Offline rendering blocks all external resources.

The command timeout defaults to 60 seconds. A blocked asset can yield an incomplete
visual with an increased `blocked_requests` count; an unavailable or disallowed
main page fails the operation. Redirected stylesheets can have relative-resource
differences from a normal interactive browser. This limited mode intentionally
does not promise full fidelity for dynamic sites.

Reference: [Playwright request routing](https://playwright.dev/python/docs/api/class-page#page-route)
documents why raw route continuation is insufficient for validating redirects.
The worker fetches redirects itself and uses
[isolated contexts](https://playwright.dev/python/docs/api/class-browsercontext).
