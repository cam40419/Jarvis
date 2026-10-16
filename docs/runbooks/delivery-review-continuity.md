# Delivery acceptance, review inbox and authorized handoff

The October 12 step 3 implementation extends immutable delivery packets with a registered
editable text/JSON profile, computed package checks, exact owner acceptance, a project
review inbox and native worker succession. These are bounded baseline capabilities.
Comparative quality, measured review effort and CORE-ACCEPT still require the registered
[evaluation program](../architecture/core-baseline-fixtures.md).

## Deliver an editable text/JSON package

Upload three separate artifacts, each with its own task, declare their complete dependencies
and obtain their current approvals. An explicit empty declaration means the owner claims
the output has no fact or upstream-output dependencies; missing or partial declarations
cannot establish this profile's package completeness.
Capture a delivery packet with all three components assigned the existing `source` role.
Then assign `editable-text-json`, version 1, and bind these profile roles:

| Required role             | File and purpose                                                |
| ------------------------- | --------------------------------------------------------------- |
| `json_specification`      | One `.json` file with media type `application/json`.            |
| `markdown_rationale`      | One `.md` file with media type `text/markdown` or `text/plain`. |
| `schema_and_check_report` | One `.json` file containing `schema_version: 1` and `schema`.   |
| `derived_safe_preview`    | Three automatically derived complete escaped source previews.   |

Every original must be nonempty valid UTF-8, at most 48,000 bytes, and match the captured
revision and SHA-256. The report provides local schema constraints; Simon computes the
check results. Caller-supplied claims that checks passed cannot establish acceptance.
For example, a specification `{"price":10,"currency":"USD"}` can use this report:

```json
{
  "schema_version": 1,
  "schema": {
    "type": "object",
    "properties": {
      "price": { "type": "number", "minimum": 0 },
      "currency": { "type": "string", "const": "USD" }
    },
    "required": ["price", "currency"],
    "additionalProperties": false
  }
}
```

Declare an existing JSON Pointer field and an actual edit, such as `/price`, before `10`,
after `11`. Validation reads the exact originals, checks the bounded local JSON Schema,
performs the edit in an isolated copy, serializes and reopens it, checks the changed value,
restores it to verify unrelated data, and renders the edited safe preview. It records the
original, edited, reopened and preview hashes. This exercise does not publish a new revision.
References, remote schema resolution and unregistered schema keywords are rejected.

The owner then accepts the exact packet, profile assignment and successful validation.
Component approval, package validation and package acceptance are separate records.
Changed authority, a newer profile assignment, a newer packet, failed checks, stale component
decisions or revoked evidence prevent current package acceptance. Historical records remain
immutable. Each packet permits 20 records of each evidence kind; exported package evidence
is bounded at 4 MiB. Original manifests retain their canonical bytes and hashes; additional
profile, validation and acceptance records live under `package/` in the ZIP, alongside a
fresh applicability report. Assignments retain their original profile definition and
validations retain their original validator definition so later runtime upgrades do not
replace the definitions used for historical evidence.

This profile validates generic text/JSON and escaped source previews. CAD, office,
creative-application rendering, layout quality and semantic truth require their own
validators and acceptance evidence. The profile catalog identifies the actual validator
and runtime versions used.

## Review only the decisions that need attention

Open the project's review inbox. It groups blocked or uncertain outputs, repairs, owner
decisions, independent review and accepted work. Artifact and packet pagination are
independent, and counts explicitly cover the loaded pages. Each row links back to the exact
artifact revision or immutable packet for the existing authoritative decision controls.

Current checks, concrete findings, dependency coverage and omitted evidence remain visible.
Text differences are capped at 4,000 characters. JSON summaries show at most 20 changed
fields and 500 characters per value, distinguish removal from JSON `null`, and label
truncation. Incomplete coverage is not treated as evidence of correctness. Revoked evidence
withholds derived findings, checks and differences as well as original content.

The owner review policy adds `max_pending_reviews` (default 20, range 1–100). Before another
independent model review creates work or reserves usage, the service counts other active
reviews, pending approval/repair decisions and unresolved review liabilities under the project
lock. Resolve a decision or reconcile an uncertain effect to release capacity. Replacing
an artifact's head does not hide historical unknown charges. Admission supports at most
1,000 project outputs and fails closed above that inventory. This is a per-project review
limit; global owner scheduling and tenant fairness remain step 4 work.

An exhausted repair allowance is shown as an owner escalation. The inbox cannot silently
change policy, approve a batch or create another correction loop. Meaningful quality and
human-effort improvements still require measured trials.

## Hand work to another authorized person and worker

Create a handoff for an active person explicitly shared into the same project. The retained
canonical manifest identifies objective, exact artifact history, decisions, pending work,
checkpoint/effect references, known costs, held liabilities, current grants and supported
continuation, including current facts, dependency assessments, repair proposals and package decisions.
Export includes exact editable originals; source bodies, provider prompts
and credentials are not transferred. The creator and named recipient can inspect it while
current membership and evidence access permit disclosure.

The recipient must independently hold current project-owner authority to continue work and
select a currently enrolled worker issued under their own authority. The existing run's
policy, issuer, model, budget, lease and recovery-epoch gates still apply. This supports
same-project native workers; foreign runtime import is explicitly blocked. Replacing the
original issuer's grant is a separate authorization operation.

Expired or queued work can continue through existing checkpoints. Completed steps are
reused without redispatch. Unknown effects require reconciliation; an active worker lease
cannot be stolen. The durable succession binding is rechecked at claim, effect dispatch
and publication, including recipient revocation after takeover. A completed run produces
an already-completed receipt without another effect. Exact manifest/file verification
rejects altered originals or immutable manifests and unsupported contents. The separate
`current-status.json` report is informational and excluded from trusted bundle verification;
fresh authority is always read from Simon at continuation time.

Changed handoff context blocks continuation until a new handoff is captured; reading the
execution does not discard an otherwise authorized checkpoint. If context changes while a
provider request is in flight, a confirmed result retains its unapplied checkpoint and known
charge without publishing. A refreshed handoff can reuse that checkpoint. A lost
acknowledgement retains the unknown effect and held cost; recapture cannot bypass its
reconciliation requirement. Live resource status follows the storage page's access rules;
ordinary member recipients do not receive private administrator notes in either the API
response or the exported status report.

Handoff inventories are bounded: 500 facts, 100 artifacts, 200 exact revisions, 100 runs, 200 tasks,
500 usage entries, 2,000 source references, 100 assessments, 100 repair proposals and
20 packets; excess inventory is rejected explicitly.
Exports are bounded at 32 MiB. Storage admission charges new retained evidence and manifests
atomically; existing artifact bytes are referenced without charging another retained copy.
A downgrade preserves retained work but may block new capture or later publication.

Use the existing [artifact](native-artifacts.md), [execution](native-execution.md),
[dependency](native-dependencies.md), [storage](workspace-storage.md) and
[recovery](storage-recovery.md) runbooks for their underlying authority and reconciliation
controls. Apply the repository's pending migrations through the documented
[persistence procedure](persistence.md) before using this slice in a persistent installation.
This slice adds `0040_native_acceptance_continuity.sql`. Development verification does
not migrate or restart an operator installation.

## Development verification

October 12 verification used synthetic providers and disposable test state. The broad
`pytest -m 'not postgres and not browser and not live'` run recorded 2,823 passes,
16 platform-capability skips and 485 deselections. One existing Windows process-cleanup
test encountered sandbox process-termination restrictions and passed its isolated rerun
with the required process access. Later refinements received focused regression runs.

The final acceptance domain/service/API and memory-store selection passed 37 cases.
Existing execution and recovery families passed 117 cases. Focused inbox checks cover
revocation, bounded differences, concurrent queue admission and unresolved review effects
across replacement revisions. The 25 continuity unit/API regressions cover two people and workers,
checkpoint reuse, permission changes, storage downgrades, archive tampering, changed context,
and both confirmed and uncertain in-flight provider results.

Twelve distinct PostgreSQL 16.15 cases passed across the new stores, API restart and
revocation tests and historical migration test. The final profile-snapshot change also
passed repeated restart and direct-storage checks. All disposable clusters were stopped.
PostgreSQL 17 validation remains required.

Fifteen headless Edge browser cases passed: six package-acceptance cases, five handoff
cases and four inbox cases. These exercise real local HTTP sessions, both public-path
prefixes, owner/member access, exact receipt recovery, stale drafts, context switching,
exports and desktop/mobile layouts. Ruff, mypy, ESLint and Prettier checks passed.

These results establish the tested bounded contracts. Application-specific reopen/render
validation, measured human review and takeover effort, paid-model quality, resource/fairness
trials and the comparative CORE-ACCEPT gate remain unaccepted.
