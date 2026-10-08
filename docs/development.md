# Development standards

Keep changes readable, bounded, and verifiable. The [roadmap](next-phases.md) owns
delivery priority; runbooks describe actual capabilities and operating limits.

## Account connections

Users link, reconnect and disconnect accounts through **Connections**. Persist
connection settings in the backend and encrypt credentials; never require users
to edit JSON configuration, `.env`, resource IDs or process launch settings.
Discover provider resources through the connected account's permissions and let
users select destinations by name. Keep connections scoped to the signed-in
actor and workspace, protect mutations with session authentication and CSRF,
and resolve current connections in both API and worker processes. Account
changes take effect without a restart. Project tool enrollment must use these
current account bindings and explicit project permissions.

Use the shared integration connection store and explicit public views. Secrets
must not appear in API responses, agent context, logs or validation errors.
Keep the encryption key in private recovery bundles alongside the database.

## Formatting and checks

Use Python 3.11 or later and the repository virtual environment. Node 22.13 or later
is needed only for development checks; the application serves its browser assets directly.

```powershell
.\venv\Scripts\python.exe -m pip install -e '.[dev,postgres,browser]'
npm.cmd ci --ignore-scripts
.\venv\Scripts\python.exe -m ruff format src tests scripts examples
npm.cmd run format
.\venv\Scripts\python.exe -m ruff check src tests scripts examples
.\venv\Scripts\python.exe -m ruff format --check src tests scripts examples
npm.cmd run format:check
npm.cmd run lint
.\venv\Scripts\python.exe -m mypy src
.\venv\Scripts\python.exe -m pytest -q -m "not postgres and not browser and not live"
```

On other operating systems use the corresponding virtual-environment Python and `npm`.
Ruff formats Python; Prettier formats browser assets, documentation, configuration
examples, and CI YAML. ESLint checks browser-script correctness. `.editorconfig`
defines UTF-8, final newlines, and indentation for editors, including PowerShell.
Do not reformat applied SQL migrations or generated engineering evidence: their bytes
are part of migration history or recorded artifact provenance. Format the generators.

The browser uses classic deferred scripts. Their shared page globals are declared in
`eslint.config.mjs`, with script order in `chat.html` and `login.html`. A global
declaration must correspond to an actual provider script; do not silence an undefined
variable by adding a fictitious global. Keep login and chat contracts separate.

## Comments and documentation

- Explain the reason, invariant, trust boundary, or unusual constraint. Avoid narrating
  an assignment, loop, or obvious type declaration.
- Use full sentences for explanatory comments, with consistent capitalization and
  punctuation. Preserve tool directives, URLs, and executable examples verbatim.
- Use module and public-contract docstrings when they explain ownership, side effects,
  failure behavior, or limits. Do not add repetitive docstrings to simple accessors.
- Keep comments next to the behavior they explain. Update or remove them when the
  implementation changes. Historical implementation status belongs in documentation.
- Document broad exception handling where it intentionally records an uncertain
  external outcome, preserves cancellation, redacts errors, or performs cleanup.
  Narrow exception handling when those guarantees do not require a broad boundary.
- Keep provider output and retrieved files as untrusted data. Comments and prompts
  describe policy; ordinary code must enforce authorization and admission.

## Maintainability and efficiency

Reuse typed contracts and shared validation helpers. Keep external I/O out of broad
transactions when practical, bound reads and queues, and retain explicit cancellation
and reconciliation boundaries. Hoist invariant work out of loops, but measure before
changing architecture or adding caches with new invalidation requirements.

Preserve idempotency, tenant isolation, current permission checks, and unknown-outcome
handling during refactors. Add regression tests for behavior fixes; formatting changes
use existing tests. Keep generated source bundles reproducible so collecting the same
files does not create a new identity solely because the clock or input order changed.

## Verification boundaries

`tests/conftest.py` applies isolated runtime settings in `pytest_configure`, before test modules can import the application. Runtime app imports in fixtures occur after that setup. Keep this ordering: an autouse fixture alone runs too late to protect import-time application construction from operator configuration.

PostgreSQL tests require an explicitly configured disposable database ending in `_test`.
Alternatively, the [isolated database runner](runbooks/database-testing.md) creates and
stops its own local cluster using a supplied PostgreSQL installation.
Browser tests require Playwright and `SIMON_BROWSER_TESTS=1`; optionally select installed
Edge with `SIMON_BROWSER_CHANNEL=msedge`. Run live/paid checks only with explicit opt-in.
Container smoke tests require their declared worker images. Skipped integration tests
must remain visible in review notes and must not be described as verified behavior.

CI checks formatting, browser lint, Python lint, strict types, and the test suite with
PostgreSQL and Chromium. Its coverage threshold remains 90 percent.
