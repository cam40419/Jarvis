# Use Jarvis now

## Start

From the project root in PowerShell, with Docker Desktop running:

```powershell
.\venv\Scripts\python.exe -m pip install -e ".[dev,postgres]"
.\scripts\start-dev.ps1 -DevelopmentLogin
```

Open http://localhost:8000/login, expand **Local development login**, and enter the token printed
by the launcher. Select **Open conversations**, create a conversation, and ask a question.
For passkey setup use `-Enroll` instead; subsequent passkey launches need no flags.

The server uses `JARVIS_OPENAI_API_KEY` from the existing private `.env` file. Never enter that API
key in the chat or login-token field. The launcher now selects OpenAI mode by default. For direct
uvicorn launches, set `JARVIS_MODEL_PROVIDER=openai` explicitly. `-TestRunner` selects offline echo.

The initial model is `gpt-5.4-mini`, configurable via `JARVIS_OPENAI_MODEL`. The installed adapter
uses the official [Responses API](https://developers.openai.com/api/docs/guides/text).
[Model details](https://developers.openai.com/api/docs/models/gpt-5.4-mini) describe its support and limits.
Model availability and billing depend on the configured API account.

## Things to try

- Ask for help drafting an email, explaining code, or making a practical plan.
- Save a shared memory such as a household food preference, then ask for dinner ideas.
- Ask a follow-up in the same conversation to test recent-history use.
- Use Shift+Enter for multiple lines; Enter sends. Reload to recover saved answers.
- Expand **Last run context** to see which history, excerpts, and memories were selected.

Jarvis can respond using text context, but cannot send email, read your calendar, browse current
web content, control devices, run code, or execute background tasks yet. It should not claim those
actions were performed. Shared memory changes still require the explicit memory controls.

## What is sent and stored

Selected messages, household memories, excerpts, provenance IDs, and a timestamp are sent to OpenAI.
Only the active household's authorized context is selected. No identity secrets or API key are put
in the prompt. The request uses `store=false`; this disables response storage for later retrieval,
not every form of provider retention. Provider handling follows its applicable API data policy.
Jarvis remains the canonical store for conversation history.

Every completed run records the exact instructions/input/model request, requested and returned model,
provider response ID, token usage, and selected context. Existing snapshots remain readable. Future
requests send local context rather than using a provider conversation ID or previous-response chain.
The executable tool list is empty.

Context v1 still bounds selection using its byte proxy. Before generation, the adapter calls the
official [input-token counting endpoint](https://developers.openai.com/api/reference/python/resources/responses/subresources/input_tokens/methods/count)
with the same instructions/input/model. The default input ceiling is 20,000 tokens; output is capped
at 2,048 (`JARVIS_MODEL_MAX_OUTPUT_TOKENS`, maximum 4,096). Provider-side truncation is disabled.
These request limits bound individual requests, not aggregate account spending.

## Retries and failures

Generation runs outside database transactions. A durable attempt reserves one active generation
per thread. The model client has no automatic retries, with 30-second timeouts for each of the token
count and response requests. A completed answer, both messages, final snapshot, events, and audit/outbox
intent commit together. Events stream after completion; this phase does not stream live model tokens.

- Replaying a successful request key returns the saved answer without generating again.
- An active duplicate returns 409. Retry the same request shortly or reload to see the completed turn.
- A provider error is recorded and returned as a sanitized 503. No partial assistant answer enters
  history. The browser retains your draft; clicking Send again starts a new attempt with a new key.
- A crashed or stalled attempt expires after 90 seconds, recognized on the next submission. Its
  provider outcome is considered unknown and it is not automatically reexecuted.
- A database failure after provider completion can also leave an unknown attempt. Retrying the same
  key cannot recover the uncommitted answer. Starting a new request can cause another provider charge.
- Session/membership permissions are checked again before publication. A revoked or changed session
  cannot publish the pending answer.

Generation cannot be rolled back at the provider. Local idempotency prevents ordinary duplicate
generation, but does not promise exactly-once provider billing across crashes or lost responses.
The test runner and OpenAI modes both preserve successful replay records when switching modes.

Credential errors mean the server key/model access needs checking. Quota/rate errors mean API billing
or limits need attention. Timeout errors leave the provider outcome unknown. Incomplete/empty answers
are not published; ask a shorter question or adjust the output cap.

## Verify

Ordinary tests use fake-model contracts and the real SDK with mocked HTTP transport. They do not
call a paid API. The optional live browser test uses only synthetic data in a disposable database
schema and an isolated browser profile:

```powershell
$env:JARVIS_TEST_DATABASE_URL = 'postgresql://jarvis:local-development-only@127.0.0.1:5432/jarvis_test'
$env:JARVIS_BROWSER_TESTS = '1'
$env:JARVIS_BROWSER_CHANNEL = 'msedge'
$env:JARVIS_LIVE_MODEL_TESTS = '1'
.\venv\Scripts\python.exe -m pytest -q --cov=jarvis --cov-report=term-missing
```

The live test verifies that Jarvis answers from a saved synthetic memory and preserves the answer
after reload. Clear `JARVIS_LIVE_MODEL_TESTS` to keep subsequent runs offline.

Verified September 11, 2026: 163 offline tests passed with 97.23% coverage; the separate live browser
test also passed using the configured API key. Jarvis answered the saved synthetic planning codename
correctly, and the answer persisted after reload. Ruff, strict mypy, and wheel packaging passed.
Migration 0005 is applied locally; backup/restore matched 21 public tables, with artifacts at
`.local/backups/jarvis_20260911T151106Z_8da011de.*`. The existing test-client deprecation warning is non-failing.
