# Project models, keys and resource limits

Open **Models & usage** on a project to enroll a model, qualify it, choose planning and
review routes, set resource limits and inspect usage. These controls currently govern
native model qualification and [project intake](project-intake.md). They do not yet govern
independent chat, business tools or native task execution.

The administrator approves provider/model templates and their server addresses. Project
owners choose from those templates and supply their own encrypted credentials where
required. Local, open-weight and hosted models use the same enrollment and accounting
contracts. No template, model server, paid key or inference call is supplied automatically.

## Start with a local model

1. Install and run a suitable text model server separately. Verify its license, hardware
   needs, API compatibility, model identifier, context capacity and output limit.
2. Save the administrator catalog below in a private file outside the repository. Replace
   the workspace UUID and deployment-specific values with the actual installation.
3. Set `SIMON_MODEL_CATALOG_FILE` to that absolute path in the service configuration.
4. Open a project, choose **Models & usage → Add model**, select the approved template
   and save. Enrollment makes no provider call.
5. Choose **Qualify model**, inspect the reservation, then deliberately run the check.
   A zero-fee local template can qualify with the default zero spending limits and no
   cloud or paid permission. It still consumes a concurrent-call slot.
6. Leave planning and review on **Automatic qualified selection**, or pin each to an
   enrolled model. Return to intake to see the effective routes and generate a plan.

```json
[
  {
    "id": "local-text",
    "name": "Local text model",
    "workspace_ids": ["11111111-1111-4111-8111-111111111111"],
    "credential_required": false,
    "data_policy": "Runs on the approved local server without forwarding project data.",
    "license_note": "Replace with the selected model's license and usage constraints.",
    "endpoint": {
      "id": "local-text",
      "provider": "openai_compatible",
      "model": "your-installed-model-id",
      "base_url": "http://127.0.0.1:1234/v1",
      "local": true,
      "capabilities": ["text"],
      "tier": "standard",
      "priority": 100,
      "context_window_tokens": 131072,
      "max_output_tokens": 8192,
      "input_cost_per_million_usd": 0,
      "output_cost_per_million_usd": 0
    }
  }
]
```

This is an example declaration, not a measured capacity or model recommendation. The
`local` flag is an administrator assertion about data processing, not proof that the
server avoids cloud forwarding. Loopback refers to the Simon server's host; it does not
connect to an arbitrary browser user's computer. Optional enrolled local workers remain
a later execution slice. `SIMON_MODEL_PROVIDER=local` controls the separate offline chat
provider and does not start an intake model or provide a canned intake result.

## Administrator catalog and hosted enrollment

`SIMON_MODEL_CATALOG_FILE` contains at most 100 template rows in a 1 MiB JSON file.
Template identifiers are unique; each endpoint identifier must equal its template
identifier. Workspace bindings determine which templates a project owner can select.
The current project limit is 100 enrolled model connections. An absent or invalid catalog
blocks model use while retaining saved projects, intake and usage history.

Supported text transports are `openai_responses`, `openai_compatible`, `anthropic` and
`gemini`. An administrator can declare other model identifiers behind a compatible
transport. Templates declare capability names, tier, priority, token limits and prices;
they do not qualify a deployment. Ordinary project users cannot submit arbitrary server
URLs, change provider prices, declare an endpoint local, or enroll a new transport.

For a hosted template, configure its verified provider, model identifier and HTTPS API
base URL, set `local: false`, set `credential_required: true`, and set the endpoint's
`api_key_env` to the literal `MODEL_PROJECT_KEY`. This value is an internal credential
placeholder. Both token prices must be declared accurately in USD per million tokens.
Use explicit zero prices only when the provider configuration actually has no token fees;
free-tier quotas and provider-side overage behavior are not discovered automatically.

The project owner then supplies the key in **Add model**, allows hosted processing in
**Project settings**, and, for positive prices, allows paid models. Both project and
workspace ceilings must cover the qualification request before it can dispatch. Saving
a key does not test it, spend money or turn on cloud consent. A local endpoint that
requires authentication can also use a project key through a credential-required template.

There is no process-provider-key fallback for native project models. In particular,
`SIMON_OPENAI_API_KEY`, chat configuration, connected-account credentials and the former
intake catalog cannot implicitly fund or authorize a project call. Each required key must
be enrolled for its project model. Administrator catalog updates are read when resolving
models; changing the template or its workspace bindings invalidates affected readiness.

## Credential storage and recovery

Provider keys are write-only in the browser and API. Responses, operation receipts and
audit events expose safe metadata, such as credential revision and key presence; they
do not return plaintext or ciphertext. The browser clears key fields on submission,
closing and access loss, and does not store them in local or session storage. Unknown
key-write responses are recovered through their operation receipt. If no saved receipt
is found, the owner can re-enter the same key and retry the original command identifier.

Credentials are encrypted using Fernet with an authenticated envelope containing the
workspace, project, model and credential revision. Moving ciphertext between those
scopes fails decryption. Rotation stores a new immutable credential revision and
invalidates qualification. Disabling a connection blocks new use and retains its
encrypted history and incurred usage. It does not revoke the key at the provider or
promise cancellation of a request already sent there.

The application currently shares its deployment encryption-key facility with connected
accounts: `SIMON_GOOGLE_TOKEN_KEY`, if configured, otherwise the key at
`SIMON_INTEGRATION_KEY_FILE` (default `%LOCALAPPDATA%/Simon/credentials.key` on Windows).
This is the encryption master key, not a provider credential. Protect and recover the
same key with the matching database; a new master key cannot decrypt old project keys.
Master-key rotation and external secret-manager/KMS integration remain future work.

Follow [storage recovery](storage-recovery.md). Bundles include the administrator catalog
as `configuration/model-catalog.json`; encrypted model credentials reside in the database.
`--include-secrets` additionally includes the available encryption-key file and `.env`.
An encryption key supplied only through a process environment or external secret store
requires separate recovery. Bundles are private and are not encrypted by the backup
command. Verify a restoration into a disposable destination before accepting traffic.

## Qualification and routing

Qualification sends one synthetic request asking for a fixed JSON object, without
project documents. Its conservative input estimate is the prompt's UTF-8 byte length
plus 2,048 framing tokens. Output is bounded by the lower of 512 tokens and the endpoint
limit; limits below 32 cannot qualify. The UI shows the maximum monetary reservation
calculated from that request and the configured rates. There is no automatic probe on
enrollment, provider retry or repair loop.

Passing records a dated fingerprint of the template and current credential revision.
It demonstrates this basic text/JSON exchange only. It does not certify research quality,
long-context reliability, image understanding, tool use, embeddings, streaming or creative
work. A provider error, invalid result, unresolved usage, changed authority or stale
configuration prevents qualification. Known charges remain recorded even if the result
cannot be accepted. Template, credential and enablement changes require a new check.

Planning and review can use the same or different qualified project models. Automatic
selection considers eligible declared text models under current privacy and paid grants;
pinning a model blocks the route if that selection becomes unavailable. There is no paid
fallback when paid permission is off and no fallback after a call has been dispatched.
Default policy disallows hosted and paid processing, so an enrolled, qualified zero-fee
local model can be selected without enlarging monetary limits. The router does not
install a free model or infer that an untested free service is suitable.

Intake checks its actual bounded context against the selected model before reservation.
Its separate review call uses a fresh assessment prompt. Current enrollment, template,
credential, project authority and policy are rechecked before dispatch; changed context
or revoked sources also fence late proposal application.

## Limits, usage and reconciliation

Both the workspace and project enforce lifetime, UTC daily, UTC monthly and per-operation
spending ceilings, a pause switch and a concurrent-call allowance. Amounts are stored as
integer micro-USD; 1 USD equals 1,000,000 micro-USD. Each configured ceiling ranges from
zero to 1,000,000 USD. Concurrency defaults to two and accepts one through 32. Zero
monetary limits permit requests with zero configured fees only.

Planning reserves generation and review together in one transaction. Both reservations
count toward concurrency even while review is waiting, so the two-call intake operation
requires at least two slots in both scopes. Qualification reserves one slot. Independent
projects compete for the same workspace capacity; parallel reservations cannot spend
the same remaining allowance. Reservation and dispatch recheck both scopes. Pausing
prevents new dispatch; it does not erase already-incurred charges. Limits cannot be
lowered below current accounted charges, holds or occupied call slots. If a provider
reports an overrun, owners can still retain existing ceilings while pausing calls or
withdrawing paid/cloud permission; the overrun does not prevent an emergency stop.

Each call records the chosen model/configuration revision, configured rates, estimated
reservation, status, reported token usage and settled charge. Known usage settles at the
frozen rates, rounded upward. Generation and review settle independently: an unknown
call cannot erase the other's known charge. Unexpected reported usage above reservation
is still recorded and can exhaust the ceiling. This ledger limits Simon's own requests;
it cannot control spending elsewhere on the same provider account or guarantee the
provider's final invoice. Local CPU/GPU time, electricity and storage are not metered here.

| Status       | Accounting and recovery                                                                                                                       |
| ------------ | --------------------------------------------------------------------------------------------------------------------------------------------- |
| `reserved`   | Admitted but not sent; holds money and a call slot. An expired unsent reservation can be released.                                            |
| `dispatched` | Dispatch was durably recorded before sending; holds money and a slot until resolved or its deadline expires.                                  |
| `settled`    | Definitive usage recorded; the remaining reservation is released.                                                                             |
| `unknown`    | Dispatch or usage is uncertain; both the monetary hold and call slot remain until settlement or reconciliation, including for zero-fee calls. |
| `released`   | An unsent call's reservation was released with no charge.                                                                                     |
| `reconciled` | A workspace owner recorded provider evidence and the verified total charge; the hold is released.                                             |

Current qualification and intake attempts have a five-minute durable deadline. Reads and
admission recover expired records; they never automatically redispatch them. Charges are
attributed to the reservation's original UTC day/month, while unresolved holds count against
current periods. Unknown calls also retain their concurrency slot because an expired
local or hosted request may still be running. Cancellation releases only unsent work, retains possible provider
liability and prevents cancelled intake results from staffing the project.

After an unknown call's deadline, a workspace owner with project access can choose
**Reconcile charge**. Supply the verified total, a reason and provider evidence/reference.
Reconciliation cannot erase a charge already known. It releases the hold and records the
actor, time and evidence. Late provider results do not overwrite that decision; an
additional verified charge is recorded as a separate adjustment with an audit event.
Reconciliation is an accounting action, not a provider refund.

## API and authority

Paths below are relative to `/v2/projects/{project_id}/models`. Reads require current
human project access. Writes require same-origin CSRF, a current human session and the
relevant owner authority; agent bearer credentials cannot manage keys, budgets or probes.
`X-Workspace-ID` is an expectation guard, not permission to choose another workspace.

| Method and suffix                    | Behavior                                                                                  |
| ------------------------------------ | ----------------------------------------------------------------------------------------- |
| `GET` base                           | Safe models/templates, effective routing, policies, usage totals and current permissions. |
| `POST /connections`                  | Enroll a project model with an optional write-only credential.                            |
| `PUT /connections/{model_id}`        | Rename, enable/disable or rotate a credential using the model version.                    |
| `POST /connections/{model_id}/probe` | Reserve and run one deliberate qualification request.                                     |
| `GET /operations/{key}`              | Recover a model-enrollment/update receipt without resending its secret.                   |
| `PUT /policy`                        | Replace project routes, cloud/paid grants and limits using the policy version.            |
| `PUT /workspace-policy`              | Replace shared limits; requires workspace-owner authority.                                |
| `GET /usage?offset=0&limit=50`       | Project usage without private endpoint snapshots; API page limit is 100.                  |
| `POST /usage/{usage_id}/reconcile`   | Reconcile eligible unknown usage using its current version and evidence.                  |

All writes carry an idempotency key. Enrollment receipts include a scoped credential
digest in request identity, so different secret re-entry cannot silently rotate or
duplicate an earlier write. Current authority is checked before returning a receipt.
Project readers can inspect safe project settings/usage; workspace totals and limits
are visible to project or workspace owners. Only a workspace owner can change shared
limits or reconcile an unknown charge. Project owners can manage their project's models
and policy while the project is active.

Migration `0033_project_models.sql` adds scoped model/credential records, separate
workspace/project policies and the per-call ledger. Existing intake charges and holds
are imported once as historical liabilities. The three former intake model-setting
fields are removed; there is no second catalog or allowance authority at runtime.
Historical planning evidence remains readable. Apply migrations only through the normal
chosen installation workflow; acceptance tests use disposable databases.

## Verification and remaining scope

The phase uses synthetic provider transport for domain, service, API, memory/PostgreSQL
contract and browser scenarios. Coverage includes credential isolation, qualification,
route fencing, concurrency, settlement, uncertain writes, reconciliation, source/context
changes and access loss. The [roadmap](../next-phases.md#verification-and-remaining-limits)
owns consolidated verification results from the completed non-live regression and
focused browser checks. Local database acceptance uses PostgreSQL 16.15 with pgvector 0.8.6;
PostgreSQL 17 remains the CI/container target requiring recorded acceptance.

```powershell
.\venv\Scripts\python.exe -m pytest tests/unit/test_native_models.py tests/unit/test_project_models.py tests/unit/test_model_usage.py tests/contract/test_native_model_store.py tests/api/test_native_models_api.py -q -m "not postgres"
.\venv\Scripts\python.exe scripts/test_postgres.py --bin-dir .local/db-phase/pg16/Library/bin -- tests/contract/test_native_model_store.py tests/integration/test_native_model_migration.py tests/api/test_native_models_postgres_api.py -q
$env:SIMON_BROWSER_TESTS = "1"
$env:SIMON_BROWSER_CHANNEL = "msedge"
.\venv\Scripts\python.exe -m pytest tests/integration/test_native_models_browser.py tests/integration/test_native_intake_browser.py -q
```

With those browser settings enabled, run the complete non-live gate against a fresh
database. The extended timeout accommodates the full Windows database/browser suite:

```powershell
.\venv\Scripts\python.exe scripts/test_postgres.py --bin-dir .local/db-phase/pg16/Library/bin --test-timeout 3600 -- -q -m "not live" --cov=simon --cov-report=term-missing
```

No paid live qualification, actual provider enrollment or pilot output-quality evaluation
was performed during implementation. Native task execution, specialist/tool calls,
artifact review, all-tool billing, subscription entitlements, compute/storage admission,
administrator catalog editing in the browser and broader capability/quality evaluation
remain later core work. Configure and accept each enforcing service before presenting
those capabilities as available.
