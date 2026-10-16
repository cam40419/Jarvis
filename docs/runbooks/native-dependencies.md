# Typed dependencies and selective repair

Project **Facts & dependencies** provides typed fact/requirement revisions, source
provenance, exact output dependency declarations, impact assessments and bounded repair
previews. The artifact dialog also exposes the declaration for its current revision.
Project owners manage this work; authorized project readers can inspect available evidence.
Persistent installations require migration **0039** after the existing migration sequence.

## Supported workflow

1. Save a fact or requirement with a stable key, explicit type, value and optional unit.
   Optionally bind original source revisions and hashes. A JSON Pointer must select the
   same value from complete, unredacted JSON source text. Whole-file evidence is an owner
   attestation; Simon does not infer truth from an arbitrary document.
2. Declare the inputs of each exact artifact revision and their affected output regions.
   Inputs can be typed facts or other artifact revisions. Mark coverage complete, partial
   or unknown. Complete with no edges explicitly declares independence. Missing declarations
   and partial coverage remain uncertainty, including through upstream relationships.
3. Change a fact and assess its old/current revisions. The assessment lists direct and
   transitive affected outputs, selected regions, unaffected outputs and uncertainty.
   Cycles terminate at a fixed point. Already-stale declared bindings remain affected even
   when the selected comparison revisions have equal values.
4. Select affected outputs and supply replacements with exact before-values. Inspect the
   immutable before/after preview, changed regions and remaining stale or uncertain outputs.
   Previewing creates no artifact revisions and makes no model calls.
5. Commit the exact proposal hash. All selected outputs publish in one transaction, with
   integrity, nonempty and declared-repair-scope checks and declarations on the new revisions.
   The scope check links the assessment and immutable proposal for later inspection; it does
   not certify semantic correctness. They require the
   configured independent review and owner acceptance. Unrepaired stale bindings are retained.

Repair supports whole-file UTF-8 text and existing JSON Pointer values. JSON patches preserve
unselected data values and serialize the repaired file with consistent indentation; they do
not preserve that file's original whitespace. Unrelated artifacts retain their exact bytes,
revision IDs and approval records. JSON pointers support escaped keys, root values and
existing array indices; ambiguous duplicate-key JSON, nonfinite numbers, nonexistent paths,
overlapping replacements and edits outside the declared affected region are rejected.

## Authority and recovery

Fact value, type and unit determine semantic authority separately from provenance. A numeric
rendering change such as `20` to `20.0`, a same-value fact revision, or an unrelated JSON
source-field change preserves a complete declaration's existing approval. A changed selected
source value makes dependent work stale immediately, before its fact is refreshed. Whole-file
source bindings require refresh after content changes. Review prompts for complete coverage
contain only selected typed fact values and their citations, with no project-wide source
excerpts. Partial and unknown coverage retain conservative project source and fact checks.

Changing the root declaration invalidates its prior approval. Source revocation still blocks
disclosure through historical declarations, reviews, decisions, repair plans and captured
packets. Removing an edge cannot erase the earlier evidence relationship. Captured approved
packet components with complete declarations use scoped authority; draft, legacy and partial
packets retain conservative source checks. Capturing a packet does not accept the package.

Any concurrent output, task, fact, declaration, project, review policy or source change after
assessment requires a fresh assessment and preview. This check is intentionally project-wide:
even an unrelated concurrent change may force a new preview. A failed publication or storage
quota admission rolls back the entire batch, including task changes and receipts. Retrying
the same proposal, even with another request key or a restarted service, returns the durable
commit receipt without publishing duplicates. Saved previews can be reopened from the project
after closing or reloading the browser. Recovery quarantine blocks repair publication.

All writes are owner-authorized and rechecked under the identity/workspace transaction locks.
Ordinary member access, model/worker credentials and historical receipts confer no write
authority. The browser preserves the original command and key across uncertain responses and
uses the operation receipt to recover it. Browser numeric editing rejects integers that cannot
be represented exactly by JavaScript; the API retains Python's supported JSON integer precision.

## Bounds and remaining work

The first slice supports 500 facts, 2,000 retained fact revisions, 200 project outputs, 32
dependencies per declaration, 100 declaration versions per artifact revision, 100 assessments
and 100 proposals per project. A proposal can repair 10 outputs with 16 disjoint replacements
each, within a 256 KiB immutable plan. Fact values are bounded to 8 KiB and review dependency
context to 96 KiB. Exceeding a bound rejects the operation; there is no silent truncation.
Graph traversal has a 6,400-visit bound. Existing artifact size/revision and storage limits apply.
The supported artifact lifecycle has one output per maker/repair task. Imported or malformed
records that share those tasks across outputs are rejected before repair or review; changing
a shared task version could otherwise invalidate another output's approval. General multi-output
task acceptance needs a separate lifecycle contract.

These fact/dependency records and repair snapshots are bounded metadata; the current logical
storage meter charges published artifact bytes, sources and delivery content, not all database
metadata/index/WAL/backup bytes. Physical capacity and metadata admission remain step 4 work.
The project-level assessment/proposal/history caps need a retention or archival policy before
long-lived production use. Narrower concurrency invalidation and graph-query optimization also
need measured capacity tests.

Coverage is explicitly declared, not automatically proven. Hidden dependencies, arbitrary
binary/native formats, semantic truth, automatic repair generation, application reopen/render
validation and competitive superiority are not established by these tests. The registered
text/JSON fixtures exercise the selective-repair contract; package acceptance, measured review
effort and authorized takeover are the next step. PostgreSQL 17 and comparative CORE-ACCEPT
evidence remain required.

The API is rooted at `/v2/projects/{project_id}/dependencies`: read the project view, fact
history, assessment, repair or operation receipt; post facts, output declarations, assessments,
repair previews and `/repairs/{proposal_id}/commit`. Commands carry idempotency keys and exact
versions/hashes. See the Pydantic commands for the complete request schemas.
