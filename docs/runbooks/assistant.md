# Use Simon now

## Start

From the project root in PowerShell, with Docker Desktop running:

```powershell
.\venv\Scripts\python.exe -m pip install -e ".[dev,postgres]"
.\scripts\start-dev.ps1 -DevelopmentLogin
```

Open http://localhost:8000/login, expand **Local development login**, and enter the token printed
by the launcher. Select **Open conversations** and ask a question; the first message creates and
titles the conversation. Use the sidebar to search or revisit saved conversations, or start a new one.
For passkey setup use `-Enroll` instead; subsequent passkey launches need no flags.

SIMON stands for **Smart Interactive Memory and Orchestration Network**. The repository name remains
Jarvis; the Python package is now `simon`. Reinstall the editable package once after updating.
Existing database names, volumes, applied migrations, and saved snapshots retain their original
identifiers. Passkeys still work on the same origin; sign in again because the session cookie is now
named for Simon. New settings use `SIMON_`; legacy `JARVIS_` names remain accepted. At the same source
priority, the new name wins; environment variables take precedence over `.env` values.

The server uses `SIMON_OPENAI_API_KEY` (or the legacy name) from the private `.env` file. Never enter that API
key in the chat or login-token field. The launcher now selects OpenAI mode by default. For direct
uvicorn launches, set `SIMON_MODEL_PROVIDER=openai` explicitly. `-TestRunner` selects offline echo.

Quick and Balanced default to `gpt-5.4-mini` (`SIMON_OPENAI_MODEL`); Deep defaults to `gpt-5.4`
(`SIMON_DEEP_MODEL`). Approved model choices are currently those two models. The installed adapter
uses the official [Responses API](https://developers.openai.com/api/docs/guides/text).
[Model details](https://developers.openai.com/api/docs/models/gpt-5.4-mini) describe its support and limits.
Model availability and billing depend on the configured API account.

## Things to try

- Ask for help drafting an email, explaining code, or making a practical plan.
- Save a shared memory such as a household food preference, then ask for dinner ideas.
- Ask a follow-up in the same conversation to test recent-history use.
- Use Shift+Enter for multiple lines; Enter sends. Reload to recover saved answers.
- Expand the run label below the last answer to see the selected history, excerpts, and memories.
- Search conversations in the sidebar; use New conversation or Alt+N for a fresh thread.
- Switch light/dark theme in the header; on mobile, open the sidebar with the menu button.
- Copy an answer or code block. Markdown headings, lists, tables, and safe links are rendered locally.
- Compare Quick, Balanced, and Deep on the same question; vary answer length independently.
- Watch the live answer, use **Stop**, and confirm your draft remains available.
- Click **Think deeper** on a saved answer, then reload to see both turns and the latest profile.
- In the composer Auto menu, choose defaults, set **Allow Auto to use Deep**, and select
  **Save as my defaults**. Reload to verify they persist for you in this household.
- Mark any saved answer **Helpful**, **Too slow**, or **Needs more depth**. Select another label to
  change your feedback, or click the active label again to clear it. Reload to verify it persists.

## Response controls

| Mode | Default model | Reasoning | Output token cap | Generation timeout |
| --- | --- | --- | --- | --- |
| Quick | gpt-5.4-mini | none | 2,048 (configurable) | 30 seconds |
| Balanced | gpt-5.4-mini | low; Auto can select medium | 8,192 | 60 seconds |
| Deep | gpt-5.4 | high | 16,384 | 120 seconds |
| Auto | Selects one of these profiles | Recorded in the run | Selected profile | Selected profile |

The chat defaults to Auto for both response mode and answer length. Routing v2 considers direct-answer
and transformation requests, comparisons, analysis/debugging cues, explicit depth requests, multiple
constraints, code, and bounded recent user context for short follow-ups. Simple tasks use no reasoning;
everyday tasks use low; comparisons use medium; complex tasks can use the larger model with high
reasoning. There is no extra classification API call. The run records the decision and policy version.
This is a deterministic heuristic with representative regression cases, not a learned difficulty
classifier. Briefness and detailed-output cues select answer length independently of thinking effort.
The API retains its previous Normal length default for older clients; send `answer_length=auto` to
enable automatic length selection.

`SIMON_AUTO_DEEP_ENABLED` now defaults to true. Set it to false to keep automatic requests on the
base model, with up to medium reasoning; manual Deep and Think deeper remain available. Manual mode
selection wins. The composer Auto menu exposes optional Brief/Normal/Detailed length overrides.
The saved personal Auto Deep setting can further restrict automatic Deep. It applies to future
requests across your sessions in the active household; changes do not alter existing runs or replay
results. Explicit Deep and Think deeper remain available when automatic Deep is off. Mode and answer
length defaults initialize the chat controls; API clients still choose those fields in each request.

Preferences and feedback are private to your account within each household. Saving them requires
the normal authenticated session and CSRF checks. Stale edits from another tab return a conflict:
reload before trying again. Feedback is stored separately from immutable answers and is not sent to
the model or used to change routing yet. Spending limits and cost estimates are still planned.

Deep allows more API usage
and time even with Brief selected. Output caps include reasoning tokens, not only visible text.
The model capabilities are documented for [mini](https://developers.openai.com/api/docs/models/gpt-5.4-mini)
and [gpt-5.4](https://developers.openai.com/api/docs/models/gpt-5.4).

Think deeper submits the original question as a linked new run with the current bounded thread
context and current accepted memories. The previous answer and its original context remain unchanged.
It makes another API request. The profile label and context inspector show the routing reason,
requested model, reasoning setting, token usage, token-count time, first-text time, and total API time.
First-text and total time include input-token counting; total time excludes final database publication.

Simon can search and open public web pages with source citations. Connect Google to read your
primary calendar and prepare emails or calendar events for confirmation in chat. See
[Google setup and capability tests](google.md). Reservations support official booking links and
email requests; general website form automation is not implemented. Device control, code execution,
and background tasks are not connected. Shared memory changes use the explicit memory controls.

## What is sent and stored

Selected messages, household memories, excerpts, provenance IDs, and a timestamp are sent to OpenAI.
Only the active household's authorized context is selected. No identity secrets or API key are put
in the prompt. The request uses `store=false`; this disables response storage for later retrieval,
not every form of provider retention. Provider handling follows its applicable API data policy.
Simon remains the canonical store for conversation history.

Every completed run records the exact instructions/input/model request, requested and returned model,
provider response ID, token usage, and selected context. Existing snapshots remain readable. Future
requests send local context rather than using a provider conversation ID or previous-response chain.
The executable tool list is recorded in each run: public web search plus the Google functions
available to the requesting account. Google functions can read calendar events or prepare previews;
only explicit card confirmation can send email or create events. Function rounds carry encrypted
reasoning items with `store=false`, with a four-round bound and a shared output-token budget.
Web search has its own tool charges. Requests with connected tools have at least a 90-second
wall-clock budget, including token counting and tool rounds; Deep retains 120 seconds.
Set `SIMON_WEB_SEARCH_ENABLED=false` to disable web search. Source links and action previews
remain available after reloading older answers.

Context v1 still bounds selection using its byte proxy. Before generation, the adapter calls the
official [input-token counting endpoint](https://developers.openai.com/api/reference/python/resources/responses/subresources/input_tokens/methods/count)
with the same instructions/input/model/reasoning/verbosity. The default input ceiling is 20,000 tokens;
the Quick output cap is configurable via `SIMON_MODEL_MAX_OUTPUT_TOKENS` (maximum 4,096).
Balanced and Deep use the fixed caps above. Provider-side truncation is disabled.
These request limits bound individual requests, not aggregate account spending.

## Retries and failures

Generation runs outside database transactions. A durable attempt reserves one active generation
per thread. The model client has no automatic retries, a 30-second token-count timeout, and the
profile's generation timeout. A completed answer, both messages, final snapshot, events, and audit/outbox
intent commit together. The browser uses live Responses API text deltas through
`POST /v1/threads/{id}/runs/stream`. Partial text is provisional and is removed on failure or Stop.
Completed-run event replay remains available separately through the existing GET event endpoint;
provisional deltas have no replay cursor. See the official
[streaming guide](https://developers.openai.com/api/docs/guides/streaming-responses).

- Replaying a successful request key returns the saved answer without generating again.
- An active duplicate returns 409. Retry the same request shortly or reload to see the completed turn.
- A provider error is recorded and returned as a sanitized 503 for ordinary requests, or a
  `run.error` event after streaming headers have been sent. No partial assistant answer enters
  history. The browser retains your draft; clicking Send again starts a new attempt with a new key.
- Stop or disconnect marks an unfinished attempt failed and prevents later publication. The provider
  stream closes on its next event or timeout; work already performed may still be billed. If completion
  won the race, its saved answer remains available after reload.
- A crashed or stalled attempt expires after the profile timeout plus 60 seconds (90/120/180 seconds),
  recognized on the next submission. Its
  provider outcome is considered unknown and it is not automatically reexecuted.
- A database failure after provider completion can also leave an unknown attempt. Retrying the same
  key cannot recover the uncommitted answer. Starting a new request can cause another provider charge.
- Session/membership permissions are checked before each live event and again before publication.
  A revoked or changed session
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
$env:SIMON_TEST_DATABASE_URL = 'postgresql://jarvis:local-development-only@127.0.0.1:5432/jarvis_test'
$env:SIMON_BROWSER_TESTS = '1'
$env:SIMON_BROWSER_CHANNEL = 'msedge'
$env:SIMON_LIVE_MODEL_TESTS = '1'
.\venv\Scripts\python.exe -m pytest -q --cov=simon --cov-report=term-missing
```

The live test verifies automatic Quick/Brief selection, a saved synthetic memory, persistence after reload,
and a linked Think deeper response using Deep. It makes two paid generation requests.
Clear `SIMON_LIVE_MODEL_TESTS` to keep subsequent runs offline.

Verified September 13, 2026: 195 offline tests passed with 96.68% coverage; the separate live browser
test passed using the configured API key. Auto chose Quick/Brief for the saved synthetic planning codename,
the answer/profile persisted after reload, and Think deeper completed a linked Deep run. The offline
browser checks also passed: passkeys, Markdown safety, copying, themes, mobile navigation, memory,
Stop, draft recovery, and reload. Screenshots are saved under `.local/screenshots/simon-*.png`.
Ruff lint, strict mypy, and Simon wheel packaging (including UI assets and migrations) passed.
Migration validation passed; no schema change was needed. A formatted copy of migration 0001 was
preserved under `.local/migration-copies/20260914T005147Z_0001_foundation.sql` before restoring its
committed, previously applied bytes. Existing formatting-only issues in unrelated Python files remain.
Migration 0005 is applied locally; the September 11 backup/restore matched 21 public tables, with artifacts at
`.local/backups/jarvis_20260911T151106Z_8da011de.*`. The existing test-client deprecation warning is non-failing.

September 14 update: the personal preferences/feedback slice passed 207 offline tests with 96.89%
coverage, including a real browser test for saving defaults, toggling Auto Deep, changing/clearing
feedback, and reload. A separate process-restart test verified preference and feedback persistence.
No paid model calls were needed for this slice. Migration 0006 is applied locally; it adds
`response_preferences` and `run_feedback`. Ruff, strict mypy, and wheel packaging passed.
Backup/restore matched 23 public tables; artifacts are
`.local/backups/simon_20260914T162733Z_140d6516.dump` and its `.json` report.
