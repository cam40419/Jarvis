# Code review and cleanup

Reviewed October 1, 2026 against base commit `5d317fd`. This report records the
repository-wide static/formatting review, manual review of critical execution and
storage paths, fixes, and remaining work. It does not certify live providers or the
production deployment.

## Findings fixed

| Finding                                                                                                                                | Change                                                                                                                                                                        | Evidence                                                                                                                                                                                                                               |
| -------------------------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Identical multi-file publications could receive different artifact identities because ZIP timestamps and entry order changed the bytes | Sort entries and use deterministic ZIP metadata; retain publication time in the artifact descriptor. Reuse the workspace reader and a case-folded name set during collection. | A new regression test failed before the fix and passes afterward. It varies collection time and path order, checks identity reuse, and verifies that changed content produces a new artifact while retaining the original.             |
| Strict typing failed on NumPy, which is a CAD-image dependency rather than an API dependency                                           | Load NumPy at the worker boundary in the same way as the existing optional geometry library                                                                                   | Strict mypy passes for all 136 source modules without adding geometry packages to the API installation.                                                                                                                                |
| Three dispatcher CLI tests had drifted from the actual platform contract                                                               | Supply the real agent-profile service in the platform test fixture                                                                                                            | All dispatcher CLI tests pass; the production startup contract is preserved.                                                                                                                                                           |
| Python and browser source lacked a consistently enforced formatter                                                                     | Apply Ruff and Prettier, including readable embedded static HTML, and add CI checks, EditorConfig, and line-ending attributes                                                 | Formatter checks pass. Python AST comparison identified only the documented fixes, docstrings, and comprehension simplifications; JavaScript changes beyond formatting are removal of unused state and readable static HTML templates. |
| Browser globals were implicit and unused chat state remained                                                                           | Declare existing page-specific script contracts for ESLint; remove unused `latestRun` assignments                                                                             | Browser lint passes with undefined-variable and unused-variable checks enabled; the browser regression suite exercises the page integration.                                                                                           |
| Assistant instructions contradicted implemented inbox access and the extracted home ownership boundary                                 | Limit inbox claims to granted Gmail tools, identify RobbinsHome as the home-data owner, and version the revised prompt. Align the displayed Simon expansion with the README.  | Model/connected-tool contract tests and browser tests pass. Historical run snapshots retain their saved prompt release.                                                                                                                |
| Multiple documents presented stale future work as the active backlog                                                                   | Consolidate delivery status in the roadmap, remove superseded planning documents, mark retained subsystem plans, and correct current runbooks                                 | Local documentation links are checked; the obsolete plate-swap link now points to repository separation.                                                                                                                               |

## Review coverage

- Python source, tests, scripts, and examples: syntax, configured lint, formatting,
  additional performance/security-pattern inspection, and comparison of syntax trees
  before and after the broad formatting pass.
- Browser JavaScript, CSS, HTML, and embedded static markup: formatting, explicit
  classic-script dependencies, unused/undefined-variable checks, and browser regressions.
- Critical paths: task admission and dependencies, cancellation and uncertain outcomes,
  external commitment claims, artifact publication and authorized reads, filesystem
  redirect checks, XML listing guards, bounded process output, backup verification,
  and the transaction boundaries noted below.
- Comments: preserve explanations of authorization, ownership, idempotency, uncertain
  outcomes, and platform constraints; correct stale ownership comments, add concise
  module contracts where useful, and document the convention in [development standards](development.md).
- PowerShell: parse all 16 scripts and normalize authored whitespace/line endings.
  Startup and recovery script tests run without changing scheduled tasks.
- Applied SQL migrations and generated engineering evidence retain their existing bytes.
  Formatting targets the generator source rather than regenerating release artifacts.

Static checks and passing tests do not establish the absence of defects. Manual review
focused on the listed boundaries; this is not an exhaustive security audit of every
dependency, provider, or deployment.

## Remaining engineering finding

**Medium priority: broad workspace transactions include slow I/O.**
[Conversation submission](../src/simon/services/model_conversations.py) still fetches
remote home history while preparing a run inside a household transaction.
[Local file mutations](../src/simon/services/local_files.py) run through a household
transaction around the idempotent operation, including potentially large ZIP/file work.
A slow provider or disk operation can therefore delay unrelated work in that workspace.

Move this work behind short admission claims and version-checked completion. Add
concurrency tests proving that unrelated work progresses, duplicate writes remain
deduplicated, revoked access blocks publication, and interrupted effects remain
reconcilable. Removing these locks without replacing those guarantees would change
correctness. This is explicitly tracked in the roadmap's recoverable-execution milestone.

The existing limits on predecessor-file handoff, shared spending accounting, and
distributed execution are product gaps already consolidated in the
[roadmap](next-phases.md), rather than functionality added by this cleanup.

## Validation

The final run completed with **1,312 passed, 237 skipped, and 10 deselected**, with
browser checks enabled through installed Edge. The earlier regression run selected
only non-PostgreSQL/non-live cases and passed 1,311 tests.

**The local coverage gate fails:** measured coverage is **83.86 percent**, below the
existing 90 percent requirement. The final command exits with status 1 for coverage,
with no test failures. PostgreSQL-backed and platform-specific scenarios were skipped,
so this does not establish the coverage result for the complete Linux/PostgreSQL suite.
The threshold and skipped tests remain unchanged; full CI verification is outstanding.

Passing checks: Python lint and formatting, strict mypy across 136 modules, Prettier,
ESLint, PowerShell parsing, local documentation links, and `git diff --check`.

Final test command, with `SIMON_BROWSER_TESTS=1` and `SIMON_BROWSER_CHANNEL=msedge`:

```powershell
.\venv\Scripts\python.exe -m pytest -q -m "not live" --cov=simon --cov-report=term-missing --tb=short
```

Required integration limits remain visible:

- Docker is unavailable in this environment and no disposable PostgreSQL test URL is
  configured. PostgreSQL-backed scenarios and real worker-container smoke tests are
  not locally verified by this review.
- Windows symlink privileges and POSIX FIFO support are unavailable for their respective
  filesystem cases; those tests are skipped here and remain available to Linux CI.
- The real CAD browser demonstration requires its prebuilt local example artifacts.
- Live/paid model and provider checks are excluded. No production services, accounts,
  scheduled tasks, or external commitments were changed.

Two dependency deprecation warnings originate in the installed FastAPI/Starlette
test-client stack. They do not fail the tests; a dependency upgrade should be checked
separately against the supported test-client combination.

## Follow-up file pruning

Removed the superseded roadmap copy, Phase 1 readiness checklist, combined Simon/Home
architecture, and two templates for the retired portfolio proxy deployment. The voice
runbook retains call setup, limits, recovery, and personal settings, and now points
to the current HTTPS runbook instead of the retired deployment instructions.

A repository reference scan found no unreferenced Python, JavaScript, CSS, or PowerShell
files outside tests and examples. This scan is a pruning aid, not proof of runtime
reachability. Compatibility launchers, worker runner payloads, migrations, tests, and
engineering examples remain in place.
