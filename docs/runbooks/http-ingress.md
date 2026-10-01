# HTTP request limits

`simon.api.request_ingress.RequestIngressMiddleware` bounds request bodies before
any endpoint executes. It checks both advertised Content-Length and actual
received bytes, including chunked uploads. Invalid framing returns 400, an
oversized request returns 413, and the entire request body must arrive within
60 seconds or the middleware returns 408. Sending occasional chunks does not
restart that deadline. Rejected or disconnected bodies never reach handlers,
so they cannot leave a partially applied endpoint mutation.

| Route | Maximum request body |
| --- | --- |
| `/auth` and its subpaths | 64 KiB |
| `/v1/local-files/upload` | 50 MiB |
| `/v1/projects/{project-id}/upload` | 16 MiB, allowing the existing 10 MiB base64 file |
| `/v1/local-files/action` | 32 MiB, allowing the existing editor's Unicode JSON payload |
| `/v1/agent-platform/plans` | 32 MiB, allowing the existing 100-task batch |
| Other routes | 2 MiB |

The configured public path and ASGI mount root are removed before route matching;
`/simon/auth/password/login` therefore has the same cap as `/auth/password/login`.
Bodies above 1 MiB spool to the operating system's temporary directory. Downloads
and response streams, including long-lived SSE, are passed through without
response buffering or a response timeout.

POST requests to password, passkey, and development-login endpoints share a
token bucket for each actual socket peer. The defaults allow a burst of 60
requests, replenished at 60 requests per minute. Rejections return 429 with a
Retry-After header. `Settings.auth_rate_limit` and
`Settings.auth_rate_window_seconds` configure these values; a rate of zero
explicitly disables this application limit. The middleware also accepts
`auth_max_sources` (default 1024), `body_timeout_seconds`, and body-cap overrides
for controlled deployments and tests. Active peer buckets are never evicted to
give new sources unlimited fresh allowances; excess sources share one bounded
overflow bucket.

Run Uvicorn with `proxy_headers=False`. The limiter deliberately ignores
Forwarded, X-Forwarded-For, CF-Connecting-IP, and similar request headers. A local
tunnel therefore presents its loopback peer and shares one authentication bucket
among remote users. Configure the public edge's per-client rate limit as well
when exposing a multi-user service. Do not trust arbitrary forwarded addresses
to distinguish users. The application limiter is process-local; multiple server
processes each have their own allowance. An edge limit also covers aggregate
connections and traffic before the local server receives them.

Register the middleware on the outermost application, including deployments
mounted below a public path. Keep security-header handling outside it so 400,
408, 413, and 429 responses receive the same HTTPS protections as ordinary
responses. The existing configured public origin supplies the canonical HTTPS
scheme; client-supplied forwarding headers do not set it.

The HTTPS launch preflight also checks the current Cloudflare credential file
against the selected tunnel. The read rejects links and non-regular or oversized
files, and failures do not echo credential contents. An explicit tunnel restart
clears an older stop marker before preflight; a new stop or maintenance request
arriving during preflight prevents launch and remains in place for recovery.
