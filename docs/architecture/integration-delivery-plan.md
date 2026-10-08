# Integration and deliverable production plan

Status: proposed implementation plan, researched October 7, 2026. This document describes target capabilities; it does not certify current integrations or authorize external actions.

Read with [the platform architecture plan](autonomous-work-platform-plan.md). The platform plan owns orchestration, project/team design, policy, model routing, deployment, and rollout. This companion defines the connector and output work needed to make those workflows useful.

Confirmed direction: eventual public SaaS with configurable full admin control; local development first; hosted operation with optional local workers; and the stdout clothing-brand pilot. Complete/test the core before implementing real tools one at a time, as defined in the [pilot charter](clothing-brand-pilot-charter.md). Text, image, transcription, and other model backends must be configurable, including local/open-source runtimes where the required capabilities are available. Credentials and runtime connections belong in the product's connection settings, with secret references passed to workers.

Paid OpenAI use is authorized for stdout after secure key enrollment and selection of a project spending cap. Future SaaS projects retain the free-default policy, with bounded platform resources and no silent paid fallback. The AI platform owns the delivery priority; stdout's potential brand, collection, design and operating outputs provide real acceptance workloads for its tools. Existing manually prepared briefs are inputs, not successful Simon tool runs. Unresolved business choices remain project tasks and do not delay core architecture. The source-art and authorship policy remains open until owner review. Preserve the existing external brand archive independently of any Simon rebuild; private source details and board URLs stay in the pilot workspace.

## Current integration capabilities

Evidence below comes from the retained source and runbooks after authority retirement. Worker adapters are libraries, not enrolled native project tools. These are implementation observations, not a fresh live-account acceptance run. Existing mock/contract coverage is useful but cannot establish provider access, application fidelity, or production reliability.

| Area                        | Existing evidence                                                                                                                                 | Capability and practical limit                                                                                                                                                                                                                                                              |
| --------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Google                      | [Google runbook](../runbooks/google.md), [Google adapter](../../src/simon/adapters/google.py), [Drive adapter](../../src/simon/adapters/drive.py) | OAuth, Gmail search/read/reviewed send, primary-calendar read/create and bounded account Drive reads. Native project file binding and writes remain to be implemented. Calendar lacks attendee invitations, recurrence, update/delete, and arbitrary-calendar management.                   |
| GitHub                      | [GitHub runbook](../runbooks/runtime-adapters.md), [adapter](../../src/simon/adapters/github_tools.py)                                            | UI-managed personal access token, repository allowlist, read operations, issue creation, draft PR creation between existing remote branches. Does not provide the remote code push/merge lifecycle.                                                                                         |
| Local Git                   | [Git tools](../../src/simon/adapters/git_tools.py), [runner](../../src/simon/adapters/_git_runner.py)                                             | Structured local init/status/diff/log/branch/switch/add/commit inside a worker lease. A remote repository lifecycle still needs implementation.                                                                                                                                             |
| Browser                     | [Browser runbook](../runbooks/runtime-adapters.md), [runner](../../src/simon/adapters/_browser_runner.py)                                         | Public static HTTPS reads/screenshots and local HTML previews. Scripts, credentials, clicks, forms, downloads, and persistent login are deliberately absent.                                                                                                                                |
| Research                    | [Search adapter](../../src/simon/adapters/web_research.py)                                                                                        | Existing search/research and source handling can seed the research workflow. Search access alone does not establish exhaustive research or claim verification.                                                                                                                              |
| Documents                   | [Report renderer](../../src/simon/services/document_rendering.py), [processing runner](../../src/simon/adapters/_processing_runner.py)            | Deterministic styled Markdown-to-DOCX reports already exist. Processing supports PDF text extraction, OCR, and bounded Markdown/text/DOCX conversion. No dedicated XLSX/PPTX authoring-and-render-validation pipeline was found in this pass.                                               |
| Images/audio                | [Generative runbook](../runbooks/runtime-adapters.md), [adapter](../../src/simon/adapters/generative_tools.py)                                    | Optional provider-specific image generation and bounded WAV transcription. No unified multi-provider edit/reference-image pipeline. External charges currently report `null` rather than feeding a complete preflight budget.                                                               |
| Media/creative applications | [Processing runbook](../runbooks/runtime-adapters.md), [native connectors](../runbooks/runtime-adapters.md)                                       | Bounded media conversions exist. Native creative connectors mainly inspect/save/export selected open applications; licensed-host acceptance and the machine runner server remain outstanding in the runbook. They do not yet constitute timeline editing or a general creative workstation. |
| Output/review persistence   | [Artifact library](../../src/simon/services/artifacts.py), [completion review](../../src/simon/services/worker_completion.py)                     | Bounded worker artifact storage and reference-based evidence checks remain reusable. Native project artifact versions and human review records still need implementation.                                                                                                                   |
| Business operations         | [External providers](../../src/simon/adapters/external_action_providers.py)                                                                       | Fixed optional commitment gateways and prerecorded Twilio calls exist. No dedicated Microsoft Graph, Shopify, or YouTube operations adapter was found in the inspected adapter tree. A configured gateway is not proof of a functioning merchant integration.                               |

Preserve the existing bounded I/O, resource grants, revision checks, durable receipts, and distinction between a known failure and an uncertain external result. Extend them into a common connector contract instead of replacing them with arbitrary HTTP access controlled only by prompts.

## Shared connector contract and lifecycle

Implement this once before multiplying providers. Use native provider APIs and official SDKs where they fit; keep business policy, tenancy, receipts, and tests in Jarvis-owned code. A provider SDK handles protocol details, not authorization to act for a project. MCP may expose a connector but should not become the system of record or bypass this lifecycle.

### Registry and authentication

Each connector version declares typed read/write operations, supported account types, auth method, API version, required permissions, resource selector, pagination, sync mechanism, rate accounting, retry class, evidence format, and tested capabilities. Mark each operation as `implemented`, `contract-tested`, `live-validated`, `pilot-accepted`, or `disabled`; show validation date/account type in the UI.

Connection setup must provide:

1. OAuth/install flow or encrypted API-key/runtime setup, followed by an inexpensive capability check with explicit account identity.
2. Resource grants for the organization, project, and acting role: particular repositories, folders/files, calendars, mailboxes, stores, channels, and domains.
3. Incremental consent for new permissions; a grant must never silently broaden when an agent gains a role or another project is created.
4. Token refresh/rotation, revoked-grant detection, disconnect, tenant/account rebind handling, and cancellation of pending actions bound to superseded credentials.
5. Secret storage outside model prompts and project documents. Logs, screenshots, traces, and error bodies receive redaction and access controls.
6. A connection dashboard showing granted operations, account, scopes, sync freshness, token health, quota headroom, last receipt, and remediation.

For local model and media runtimes, the equivalent grant is a configured runtime plus its allowed models, directories, network policy, and resource budget. Do not infer vision, tool calls, structured output, editing, streaming, or embeddings merely from an OpenAI-compatible endpoint label. The main plan defines model conformance tests and routing.

### External action state machine

Use `draft -> validated -> awaiting_review or authorized -> dispatching -> succeeded, failed_known or outcome_unknown`, with rejected and cancelled paths. Reconciliation records evidence resolving an unknown outcome; it is not a generic success state. A draft can be revised freely. Authorization applies to the exact destination, operation, artifact version/hash, material arguments, cost ceiling, expiry, and account binding. A material change invalidates it.

Before dispatch, recheck the project grant, current identity, current policy, target revision, budget reservation, and approval validity. Claim the operation durably and record its idempotency key before leaving the database transaction. Provider calls occur outside that transaction; completion attaches the provider receipt and observed remote state.

Exactly-once external effects are not a general API guarantee. Classify each operation separately:

| Class                               | Dispatch/recovery strategy                                                                                                                        |
| ----------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------- |
| Read-only                           | Retry transient errors with bounded exponential backoff, jitter, deadlines, and provider rate guidance.                                           |
| Provider-supported idempotent write | Persist the provider key and scope; retry only within that provider's documented semantics and retention period.                                  |
| Conditional mutation                | Use the provider's revision/ETag/precondition support; on conflict, reload and produce a new change proposal.                                     |
| Resumable upload/job                | Persist session/job identity and content hash; query status and resume the same operation.                                                        |
| Non-idempotent write                | On ambiguous timeout or lost response, retain `outcome_unknown`; reconcile using provider state and correlation evidence before any new dispatch. |

The action ledger needs request digest, operation identity, run/task/agent/user IDs, account/resource binding, approval reference, attempt timestamps, provider correlation IDs, estimated/reserved/actual cost, status evidence, and reconciliation history. A user clicking retry must not bypass duplicate protection. If reconciliation cannot decide, create an assigned human task rather than claiming success or automatically repeating the effect.

### Sync, events, and cost

Validate webhook authenticity, persist a minimal event envelope, acknowledge promptly, and process asynchronously. Deduplicate deliveries; tolerate missing and out-of-order events. Keep provider sync cursors and resource versions in connector-owned records, not model memory. Renew expiring subscriptions. Run periodic reconciliation and expose stale data. Never delete canonical project evidence just because a provider's cache must be rebuilt.

Separate observed external state from planned changes. Represent deletions/tombstones, moved resources, lost access, and human edits explicitly. Make the source of truth clear per field; avoid two-way synchronization without conflict ownership. Cache reads by connection/resource/revision and coalesce repeated team requests.

Track calls, quota units, bytes, storage, rendering time, local GPU time, and paid tool usage in addition to language-model tokens. Reserve a conservative estimate before a paid job and settle actual usage afterward. Unknown usage remains an unresolved reservation; it is not zero. Scheduling uses per-provider/account limits plus project priority and deadlines.

### Shared acceptance suite

Every production write operation must pass expired/revoked credentials, cross-project access denial, duplicate invocation, process crash before/after provider dispatch, response loss, quota exhaustion, concurrent human edit, stale approval, and reconciliation tests. Sync tests include duplicate/missed events, expired cursor, subscription renewal, pagination, and restart midway through a full import. Live tests use dedicated test resources and verify the actual remote result.

## Delivery order and planning estimates

These are rough engineering person-weeks for an experienced engineer, including focused UI integration and acceptance work after the common platform exists. They are not calendar commitments, do not include the core restructuring, broad design work, app-store/security reviews, legal review, procurement, or sustained support. Confidence is low until a representative workflow is prototyped. Shared infrastructure makes rows overlap; do not simply add them to produce a promised launch date. The earlier main-plan estimate is retired for the revised SaaS/core-first scope. This catalog is a work breakdown and expansion menu; re-estimate each selected package after the core contracts exist.

| Priority        | Work package                                   | First useful outcome                                           | Rough effort                                |
| --------------- | ---------------------------------------------- | -------------------------------------------------------------- | ------------------------------------------- |
| P0              | Shared connection/action/sync framework        | Observable, resumable, scoped integrations                     | 4-7 weeks                                   |
| P0              | Artifact production and review foundation      | Editable source + preview + quality evidence                   | 3-5 weeks                                   |
| P1              | Research + DOCX/PDF                            | Cited business brief with verified sources and readable export | 3-5 weeks                                   |
| P1              | Existing Google hardening + calendar expansion | Reliable project files, reviewed mail, working calendar        | 4-7 weeks                                   |
| Early post-core | Local Git + isolated generator execution       | Reproducible procedural artwork and versioned storefront code  | Estimate after one real generator fixture   |
| P1              | GitHub App + isolated development              | Issue-to-tested-draft-PR workflow                              | 3-6 weeks                                   |
| P1              | XLSX production                                | Recalculated, validated operating model                        | 2-4 weeks                                   |
| P1/P2           | PPTX production                                | Editable, visually checked branded deck                        | 2-4 weeks                                   |
| P1/P2           | Configurable image generation/editing          | Reviewed visual variants with reproducible provenance          | 2-4 weeks per initial backend family        |
| Pilot-dependent | Illustrator + optional garment CAD             | Editable artwork; measured pattern workflow when required      | Separate application/license prototypes     |
| P2              | Interactive browser                            | Two or three supported website workflows with takeover         | 4-8 weeks; additional sites vary            |
| P2              | Microsoft 365                                  | Mail/calendar/files for selected tenant type                   | 4-7 weeks                                   |
| Pilot-dependent | Shopify                                        | Catalog preparation and approved store changes                 | 4-7 weeks                                   |
| Pilot-dependent | YouTube + basic media                          | Private upload, review, scheduled publication, analytics       | 4-7 weeks, excluding advanced video editing |
| Later           | Additional business systems                    | One narrowly defined operational workflow per connector        | 2-6 weeks each; investigate first           |

The selected pilot is stdout. Core connection/artifact contracts and reference tools come first. After core acceptance, prioritize local file handling, local Git and isolated execution, then select image/design and generator tools against the reviewed brand briefs. Git directly supports the algorithmic artwork tool and storefront; it is not an unrelated later software-project feature. Remote GitHub, shared storage/boards, garment CAD and commerce integrations follow their actual dependencies. Complete each tool's real-output/account acceptance before beginning the next. Table priority labels are comparison guidance, not authorization for parallel tool development.

## Google Workspace and calendars

**Build on:** the current OAuth, encrypted credentials, account binding, Drive folder boundary, mutation receipts, calendar duplicate protection, and Gmail review flow. Upgrade behavior in small operations with migration tests for existing grants and pending previews.

**Files/documents:** add selected-resource onboarding, complete folder import with explicit permission inventory, pagination, supported binary extraction, shared-drive handling where selected, revision/conflict handling, large resumable transfers, change tracking, native Docs/Sheets/Slides authoring, and export receipts. Keep extracted text separate from originals and retain page/slide/cell locations for provenance. Slides has a native API for presentation structure and batch operations; use it when Google-native collaboration is the output requirement. [Google Slides API](https://developers.google.com/workspace/slides/api/guides/overview)

Prefer selected-file access using `drive.file` and Picker when the workflow permits it; the scope grants files created/opened/shared with the app. Do not claim selecting a folder automatically gives access to all existing descendants. A whole-company import may require broader access and its own consent/verification decision. [Drive authorization](https://developers.google.com/workspace/drive/api/guides/api-specific-auth)

**Gmail:** add optional attachment ingestion, thread-aware drafting/reply, draft updates, recipient and attachment preview, history cursor synchronization, and configured sender/recipient policies. Preserve the existing distinction between draft generation and sending. Readonly and compose Gmail scopes are restricted; send is sensitive. Confirm the applicable verification/security-assessment path for the actual deployment before offering public onboarding. History expiration can require a full resync. [Gmail scopes](https://developers.google.com/workspace/gmail/api/auth/scopes), [Gmail synchronization](https://developers.google.com/workspace/gmail/api/guides/sync)

**Calendar:** make calendars selectable, then add free/busy, event updates/cancellations, attendees, recurrence/exceptions, and conference links as independent operations. Persist original timezone, UTC instants, all-day semantics, recurrence rules, event revision, and organizer identity. Distinguish editing one occurrence, the series, and future occurrences in the review UI. A task due date, workflow wake-up timer, publication schedule, and human meeting are different records.

Google supports caller-supplied event IDs. Retain stable operation-derived IDs and verify collisions against expected content. Attendees and notification behavior are part of the approved payload; disabling notifications is not a reliable way to make an invitation a harmless draft. [Calendar event creation](https://developers.google.com/workspace/calendar/api/v3/reference/events/insert)

Use per-calendar incremental cursors, watch subscriptions, renewal, and periodic reconciliation. Calendar `410` invalidates the sync cache and requires a full rebuild. Push messages contain change notifications rather than complete event data, channels expire, and delivery is not fully reliable. [Calendar sync](https://developers.google.com/workspace/calendar/api/guides/sync), [Calendar push](https://developers.google.com/workspace/calendar/api/guides/push)

**Quota/cost:** batch Sheets reads/writes and avoid one call per cell or agent thought. Respect per-user/project limits and backoff. Enforce project limits on imported bytes and retention; documents need bounded extraction, not unlimited context insertion. [Sheets usage limits](https://developers.google.com/workspace/sheets/api/limits)

**Acceptance:** connect two accounts without cross-account writes; decline a scope without breaking others; import a real representative folder; handle revoked access and concurrent edits; send one reviewed test email; create/update/cancel disposable events; test DST transitions, all-day boundaries, recurrence exceptions, duplicate dispatch, and lost webhook recovery. Acceptance must include real shared-drive/account types if promised to users.

## Microsoft 365

**Proposed connector:** Microsoft Graph with an Entra app registration, account-type support declared up front, OAuth consent, encrypted token cache, and resource grants for specific mailboxes, calendars, drives, and sites. Start with delegated permissions for the owner's selected resources. Evaluate tenant-admin-approved application permissions separately for unattended organizational needs; do not assume every Graph endpoint supports them.

Implement mail read/draft/send, calendar operations, OneDrive/SharePoint file transfers, metadata/version checks, and the required Excel operations as separate capability modules. Use the same internal document contracts as Google, while preserving provider-specific features and IDs. Do not equate file upload with fully programmable Word/PowerPoint editing; produce editable Office files locally when the cloud service does not expose the needed authoring API.

Graph delta links are opaque state, support replay, and can expire; store and follow them without reconstructing their parameters. Combine change notifications with delta reads and periodic reconciliation where supported. Subscriptions have finite lifetimes and need renewal. [Graph delta query](https://learn.microsoft.com/en-us/graph/delta-query-overview), [Graph change notifications](https://learn.microsoft.com/en-us/graph/change-notifications-overview)

Calendar creation exposes `transactionId` to reduce duplicate retries. Mail `202 Accepted` means accepted for processing, not confirmed delivery. Display these distinctions in receipts. [Graph event creation](https://learn.microsoft.com/en-us/graph/api/user-post-events?view=graph-rest-1.0), [Graph sendMail](https://learn.microsoft.com/en-us/graph/api/user-sendmail?view=graph-rest-1.0)

A concrete constraint: the documented Excel `createSession` endpoint supports delegated work/school `Files.ReadWrite`; personal delegated accounts and application permissions are unsupported there. Account-type capability checks must prevent promising an unattended workbook workflow that cannot run under the chosen identity. Use a tested local workbook route when suitable. [Excel session permissions](https://learn.microsoft.com/en-us/graph/api/workbook-createsession?view=graph-rest-1.0)

**Acceptance:** representative real tenant/account types, admin consent refusal, revoked user access, token renewal, shared mailbox/site behavior, delta reset, concurrent file replacement, mail processing receipt, DST/recurrence cases, workbook session expiration, and endpoint-specific throttling. Record required licensing and account support from the tenant acceptance test.

## GitHub and code development

**Pilot dependency:** implement and accept local Git plus isolated execution early after core acceptance for separate artwork-generator and storefront repositories. Preserve commits, dependency locks, input hashes, generator parameters and output artifacts. Prove a reproducible local generation/edit/review cycle before adding remote repository operations. Existing brand originals are external source assets and must not be removed during checkout cleanup or application teardown.

**Proposed integration:** retain the existing PAT path for development/migration, and add a GitHub App for long-lived organization use. GitHub recommends Apps for this use case; they provide repository selection, granular permissions, short-lived installation credentials, and centralized webhooks. [Choosing a GitHub App](https://docs.github.com/en/apps/creating-github-apps/about-creating-github-apps/deciding-when-to-build-a-github-app)

Build installation onboarding and permission mapping; repository clone/fetch; per-task worktree/checkout; branch push; draft PR/update; CI/check collection; review comments; human-requested revisions; and separately authorized merge/release/deploy actions. Use short-lived credentials only in a trusted Git transport path; never write them into repository remotes, model context, or artifacts. [Installation authentication](https://docs.github.com/en/apps/creating-github-apps/authenticating-with-a-github-app/authenticating-as-a-github-app-installation)

Bind code work to a base commit and store diff, final commit SHA, tests, build output, environment image, and review result. One checkout has one writing agent at a time; cross-agent work uses independent branches and explicit integration tasks. Resolve conflict with new tests/review, and invalidate approval if the relevant commit changes. Respect branch protections and required checks; the agent should not grant itself exceptions.

Webhook processing verifies signatures and deduplicates `X-GitHub-Delivery`; redeliveries reuse that identifier. Acknowledge quickly and queue processing. Handle both primary and secondary rate limits using response guidance; do not assume an App has unlimited capacity. [Webhook practices](https://docs.github.com/en/webhooks/using-webhooks/best-practices-for-using-webhooks), [REST limits](https://docs.github.com/en/rest/using-the-rest-api/rate-limits-for-the-rest-api)

**Approval surface:** goal, linked issue, changed files/diff, preview if visual, checks with links, independent code review, migration risks, and explicit merge/deploy target. Draft branches/PRs can follow project policy; externally visible releases, production deployment, and changes to protected branches need their own action authorization.

**Acceptance:** fix a seeded bug from issue to tested draft PR; duplicate PR request; missing installation permission; repository removed from App access; crash after push; CI failure; concurrent human push; conflicted branch; revoked approval; protected-branch rejection; clean environment teardown. Never run untrusted repository workflows with unrelated production credentials.

## Research and interactive browser work

**Research target:** a reusable workflow of question decomposition, source discovery, primary-source reading, evidence extraction, contradiction review, synthesis, and factual verification. Keep a claim/evidence ledger containing source URL/document ID, title, author/publisher when available, publication and retrieval dates, exact location, bounded supporting excerpt, content revision/hash, and confidence/reason for exclusion. A search snippet is discovery evidence, not proof of a claim.

Separate factual claims, inferences, proposed choices, and unknowns. Require source coverage appropriate to the question, independent corroboration for consequential or contested facts, and an explicit stopping rule based on coverage/time/budget. A reviewer opens sampled source passages and verifies that they support the final text. Track broken citations, stale facts, contradictions, and unsupported numbers as defects.

Use configurable search/retrieval backends and direct official pages. Preserve existing public-fetch bounds, extraction isolation, origin checks, and source attribution. Ingested pages, messages, documents, and repository text are data, not authority to change agent policy or disclose secrets. Private research stays within the configured local/provider data boundary.

**Browser target:** Playwright remains the deterministic browser foundation. Add a distinct authenticated interactive worker with isolated per-account sessions, encrypted session storage, semantic element targeting, screenshots, downloads, explicit form field/action descriptions, and user takeover. Stored authentication state can impersonate an account and must be treated as a credential. [Playwright authentication](https://playwright.dev/docs/auth)

Keep static research and interactive operations as separate capabilities. A general browser session can perform writes via clicks, JavaScript, or even navigation; a DOM observer alone cannot guarantee safe effects. Restrict authenticated automation to tested site workflows with explicit write boundaries and bounded destinations. Stage the form and show the exact target/data before a committing action when required by policy. Pause for MFA, CAPTCHA, unavailable API features, or uncertain page state and create a clear human task.

Record redacted before/after screenshots and action traces; Playwright trace tooling can inspect actions, page snapshots, and network activity. Do not expose raw traces as public artifacts. On a timeout after submission, inspect the remote account/receipt instead of clicking submit again. [Playwright traces](https://playwright.dev/docs/trace-viewer)

**Acceptance:** research benchmark with missing/conflicting/stale sources and injected instructions; two or three selected real browser workflows; selector drift; expired session; login challenge; blocked asset; ambiguous submission; user takeover and resume; download quarantine; cross-project cookie isolation; demonstrated refusal to treat a loaded page as authorization. “Any online task” remains a long-term goal bounded by credentials, site support, and what the remote service permits.

## Editable deliverables and quality gates

### Common artifact pipeline

Every deliverable begins with an acceptance brief: audience, intended use, format, editable-source requirement, brand/template, factual sources, calculation rules, dimensions, accessibility, reviewer, and publication destination. Save source inputs, generator/template version, provider/model/runtime where used, run parameters, artifact hashes, and validation evidence.

Use `draft -> automated checks -> independent agent review -> revision -> human review when required -> accepted -> authorized distribution`. Limit revision loops and escalate persistent defects. Review independence means a separate evaluation task with the brief and artifacts; self-reported success from the producer is insufficient. A second model does not replace deterministic checks or human judgment.

Deliver an immutable original, editable source, preview, machine-readable validation results, and concise reviewer notes together. Bind acceptance to the artifact hash/version; editing a live document after approval requires a new review before that version is distributed. A delivery bundle records its source and rendered-preview hashes so an old thumbnail cannot approve a newer file.

### DOCX and PDF

Extend the existing renderer using reusable brand templates and semantic styles. `python-docx` creates/updates DOCX; preserve editable headings, tables, captions, headers/footers, page layout, and links. Add numbering, references, and charts as supported by the actual template contract. [python-docx](https://python-docx.readthedocs.io/en/latest/)

Validate package structure and required content, then render every page using a pinned LibreOffice worker with pinned fonts. LibreOffice supports headless command-line conversion. An Open XML schema check catches a different class of error from visual rendering; neither guarantees layout fidelity in every Office version. [LibreOffice CLI](https://help.libreoffice.org/latest/en-US/text/shared/guide/start_parameters.html), [Open XML validation](https://learn.microsoft.com/en-us/office/open-xml/word/how-to-validate-a-word-processing-document)

Check page count, missing text/images, clipped tables, headings stranded at page bottoms, links, page numbers, reading order, and content/preview consistency. Offer PDF plus DOCX when useful. For formal documents, maintain template ownership, version history, and identified human approvers; document generation does not itself execute an agreement, sign, or file it.

**Acceptance:** representative long report, wide table, mixed list nesting, Unicode, landscape section, image/caption, hyperlinks, page fields, and brand template render/reopen in the declared target applications. Preserve the user's original when importing an existing complex document and report unsupported features before editing it.

### XLSX and analytical workbooks

Use XlsxWriter for generated workbooks and openpyxl for bounded reading/modification of supported existing workbooks. Require explicit workbook structure: inputs/assumptions, calculations, outputs, units/currencies, source dates, number formats, named ranges where helpful, validation, charts, print ranges, and protected formula cells where appropriate. [openpyxl](https://openpyxl.readthedocs.io/en/stable/), [XlsxWriter formulas](https://xlsxwriter.readthedocs.io/working_with_formulas.html)

XlsxWriter does not calculate formulas; cached values can otherwise appear as zero in consumers without a calculation engine. Recalculate using a tested spreadsheet engine, reopen, inspect cached values and formula errors, and verify key totals against independent Python calculations. Excel/LibreOffice/Sheets function differences need an explicitly tested compatibility subset. Test financial or operational models with scenario changes and invariants; do not validate only that the file opens.

Treat imported CSV text as untrusted cell data and control formula interpretation. Preserve existing formulas/styles/objects only where supported; do not promise lossless editing of every workbook feature. The review UI shows assumptions, key outputs, changed cells, formula checks, and worksheet previews.

**Acceptance:** clothing margin/inventory or channel operating-budget model with editable inputs, consistent currencies, validated totals, a scenario change, chart and print preview, no unexpected external links, and no formula errors after recalculation in the promised application.

### PPTX and presentation design

Use python-pptx for the Python-first implementation, with controlled templates/master layouts, editable text and charts, embedded assets, citations, and speaker notes as supported. The library supports creation and modification but does not implement every PowerPoint feature; choose the design system within its tested capabilities. [python-pptx](https://python-pptx.readthedocs.io/en/latest/)

Generate from a slide outline and content/data contract; keep design and data separate. Render all slides, compare text extents to shape bounds, check off-slide/overlapping objects and font substitution, and visually inspect every slide. Provide a contact sheet for rapid review and individual slides for comments. Charts must trace to the same validated source data as companion spreadsheets.

**Acceptance:** a real 10-15 slide pilot deck with editable charts, consistent brand typography, images, source notes, supported notes, readable contrast, no overflow, and successful reopen/render in target PowerPoint. A deck made of flattened slide images does not meet the editable-deliverable requirement.

### Images, audio, and video

Define vendor-neutral operations such as image generate/edit, reference conditioning, optional masks/transparency, transcription, speech synthesis, and media render. Capability negotiation selects a configured cloud or local backend; unavailable features are explicit. API keys and local runtime connections are UI-managed. Preserve provider-specific parameters and returned provenance without forcing all backends into the lowest common feature set.

The existing image adapter can be one implementation, extended with image references/edits and cost accounting. For example, the official OpenAI image API documents distinct generation/editing operations and output controls; confirm model/account access during connector validation rather than hard-coding a model recommendation in this plan. [Official OpenAI image guide](https://developers.openai.com/api/docs/guides/image-generation)

Store prompt/brief, references with rights/source information, model/version, seed when supported, generation parameters, original bytes, dimensions/color profile, edit lineage, and costs. Produce bounded variants and apply the project's configured creative checkpoints before expensive finishing passes. Direction, draft, final and publication review are separately configurable, as requested by the owner. Keep logos/text/layout in editable vector or design layers when precision matters; a generated raster is not a complete brand system or clothing production file.

Check visual coherence, requested composition, spelling/text by OCR plus inspection, artifact defects, brand fit, reference fidelity, output dimensions, and transparency. For apparel add print area, supplier format, color/process requirements, and actual sample review; a mockup must not be presented as a verified manufactured product.

For video, extend existing FFmpeg-based processing with an editable timeline/project specification, source assets, voice/music provenance, subtitles, motion/layout templates, and rendering manifests. Advanced Premiere or other desktop editing needs a separate licensed-runner work package and installed-host acceptance. Check duration, frames, black/frozen segments, audio clipping/loudness, caption timing, spelling, safe areas, aspect variants, and a complete human preview before public release.

**Acceptance:** one approved image brief through variants/edit/export and one short video through source project, render, captions, QC, revision, and approval. Record local compute usage and cloud charges. No claim of “highest quality” passes without review against an explicit exemplar and brief.

### Apparel artwork and garment development

Keep three capabilities distinct: a procedural graphics compositor creates editable prints, motifs and placement maps; garment CAD develops measured patterns, construction, grading and fit; image generation creates visual concepts. The HUMAN ERROR source-art and authorship rule is open until the owner reviews the brand direction. Store creator, source, license, algorithmic transformations and model assistance without inventing a public authorship claim.

For the compositor, record schema version, seed and random-number algorithm, code commit, dependency lock, input hashes, dimensions and units, palette, print constraints, output hashes, cost and review version. A unique seed or variation ID does not prove visual uniqueness. Compare exact hashes and normalized render similarity, flag likely collisions and preserve reviewer decisions; do not promise duplicate prevention is infallible. Accept a representative design only after repeat generation, dimension/print checks and independent visual review.

Use Illustrator for existing native artwork where appropriate. Its documented desktop scripting/extensions can support a narrow local adapter; validate installed-host licensing, fonts, links, export fidelity and original preservation. Inkscape command-line export is an established option for new SVG-based output. [Illustrator developer interface](https://developer.adobe.com/illustrator/), [Inkscape command line](https://wiki.inkscape.org/wiki/Using_the_Command_Line).

Treat CLO as a separate garment capability with Python scripts running inside its application and an SDK. Browzwear additionally offers licensed Open Platform access and a UI-less VStitcher Server Engine. Select either only after a real garment proves import scale, bounded parameter changes, technical export, simulation/rendering and the required licensing. Neither graphical previews nor imported fashion flats establish production-ready patterns; technical review and physical sampling remain part of garment acceptance. [CLO Python API](https://developer.clo3d.com/python.html), [CLO SDK](https://developer.clo3d.com/), [Browzwear Server Engine](https://help.browzwear.com/en/articles/13065059-open-platform-and-vstitcher-server-engine).

## Shopify clothing operations pilot

**Website scope:** the pilot also needs versioned theme/site code, an unpublished review preview, checks and a separately authorized deployment. Shopify CLI supports local theme development, checks and unpublished themes. Keep theme release separate from catalog, inventory and order operations, and preserve another existing storefront if pilot discovery justifies it. [Shopify theme development](https://shopify.dev/docs/storefronts/themes/tools/cli).

**Scope first:** product/variant catalog, sizes/colors/SKUs, inventory by location, collection/content drafts, asset library, order-status reporting, and operational tasks. Purchasing, supplier commitments, refunds, price changes, customer messaging, and fulfillment instructions require separately modeled operations and policies.

Build against the GraphQL Admin API. Shopify identifies REST Admin as legacy and GraphQL as the direction for new apps; use production webhooks rather than introducing a preview event mechanism into the first release. [Shopify APIs](https://shopify.dev/docs/apps/build/apis)

**Authentication/resources:** app install and shop binding, minimal operation-specific scopes, offline/background credentials where needed, token lifecycle, uninstall cleanup, and explicit store/location grants. Customer/order data access must be included only when needed and accepted for the chosen app distribution. [Shopify authentication](https://shopify.dev/docs/apps/build/authentication-authorization)

**Build:** initial catalog import, typed product/variant/inventory mapping, current price/stock evidence, draft product/media changes, field-level diff preview, mutation dispatch, read-after-write verification, webhook receiver, periodic reconciliation, and quota-aware bulk import. Inventory operations need expected-state checks appropriate to the chosen mutation so two agents cannot independently apply stale stock decisions.

Verify webhook HMAC and deduplicate delivery IDs; Shopify specifically recommends reconciliation because webhook delivery is not guaranteed. GraphQL throttling is based on query cost and the app/store pair, so inspect returned cost/throttle data and queue work accordingly. [Shopify webhooks](https://shopify.dev/docs/apps/build/webhooks), [GraphQL rate limits](https://shopify.dev/docs/apps/build/apis/graphql-admin/rate-limits)

**Review:** storefront preview, exact variants/SKUs, image/copy versions, price/currency, inventory/location effects, publication destinations, effective date, and rollback limits. Separate approval of creative assets from approval to publish/change commerce state. Production purchase orders and refunds are business commitments, even if an API makes them easy.

**Acceptance:** development/test store with realistic variants and a full draft-to-reviewed-publication loop; duplicate event, concurrent inventory change, ambiguous mutation, wrong-store prevention, uninstall, token failure, API version change, and catalog rebuild. Production clothing operation also requires supplier/fulfillment integrations and physical sample/quality processes; Shopify alone cannot provide them.

## YouTube channel operations pilot

**Scope first:** channel/content inventory, research and editorial board, briefs/scripts, asset/timeline preparation, rendered video QC, thumbnails, captions, private upload, metadata review, publication scheduling, and performance reporting. Treat editing/rendering and channel publication as distinct workflows.

**Build:** Google OAuth with operation-specific YouTube permissions and explicit channel selection; resumable upload session persistence; upload progress/recovery; remote processing-status checks; metadata/thumbnail/caption operations; private review links; exact publication proposal; and analytics ingestion with its own scopes and metric definitions. Do not imply all channel Studio features are available through the API.

Use one persisted resumable session per artifact hash/action and query that session after interruption. The protocol supports checking received bytes and resuming rather than starting a duplicate upload. [YouTube resumable uploads](https://developers.google.com/youtube/v3/guides/using_resumable_upload_protocol)

A material launch dependency: videos uploaded through `videos.insert` by unverified API projects created after July 28, 2020 are restricted to private viewing until the required audit. Quotas are endpoint/project-specific and can change; read current project limits during setup instead of copying historical upload-cost numbers into code. [YouTube video insertion](https://developers.google.com/youtube/v3/docs/videos/insert)

**Review:** full playable video, thumbnail variants, title, description, chapters/captions, audience/designation fields, channel, visibility, sponsorship/disclosure fields where applicable, publish time in the user's timezone, and relevant asset rights records. Publication approval binds the complete bundle; changed thumbnail/title/video requires a new applicable review.

**Acceptance:** private test upload interrupted/recovered, processing failure, expired upload session, wrong-channel prevention, timezone scheduling, quota exhaustion, revoked authorization, duplicate publish request, and post-publication state verification. Complete an approved publication test only after the project's audit/access status permits it. Analytics should inform new board tasks without automatically rewriting approved creative direction.

## Later business operations and bespoke adapters

Select products already used by the pilot company. Prefer established native APIs and existing business records over replacing accounting, CRM, helpdesk, payroll, or inventory systems. The following are candidate investigations, not verified commitments to provider capability or scope in this pass.

| Domain                  | Candidate established systems                                  | First custom adapter package                                                                                                                                                   |
| ----------------------- | -------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| Accounting/reporting    | QuickBooks Online, Xero, existing ERP                          | Read chart of accounts/transactions; reconcile imports; draft categorized reports; implement reviewed writes only after accountant-defined rules and test-tenant validation.   |
| CRM/sales               | Salesforce, HubSpot                                            | Contact/deal sync, duplicate identity resolution, activity summaries, proposed updates, reviewed outreach; map ownership and source-of-truth fields.                           |
| Support                 | Zendesk, existing helpdesk                                     | Ingest assigned tickets, retrieve policy/product context, draft responses, escalation and SLA tasks, reviewed send; prevent agent-generated replies triggering infinite loops. |
| Messaging               | Slack, Microsoft Teams, existing email                         | Selected channel/thread intake, linked board tasks, authorized outbound updates, edits/retractions where supported, notification deduplication and retention mapping.          |
| Payments/commerce       | Existing payment processor, shipping/fulfillment provider      | Quote/estimate or status first; explicit charge/refund/shipment operations with monetary limits, provider idempotency semantics, receipts, and reconciliation.                 |
| Marketing/social        | Company's actual social and advertising platforms              | Verify app access first; content drafts, preview, schedule, publish/status, analytics; budgets and paid campaign changes require separate authorization.                       |
| Documents/signatures    | Existing e-signature and document management provider          | Template selection, party identity, reviewed envelope/document creation, status callbacks, audit record; approval to draft is not approval to sign.                            |
| Suppliers/manufacturing | Supplier API, EDI, SFTP, structured email, or supported portal | Import catalogs/quotes; map units, SKUs and lead times; draft purchase orders and proofs; reconcile acknowledgements; human resolution for unmatched data.                     |

For every missing connector, write a short specification before coding: actual user workflow; API/SDK and license; account/test environment; permission map; data model and ownership; read/write operations; rate and monetary costs; expected revisions; idempotency/reconciliation; webhook/polling; preview/approval; audit/retention; fixtures; live acceptance; ongoing owner. Budget separate discovery when API access or contracts are uncertain.

If no API exists, evaluate supported file exchange before browser automation. A narrow, tested import/export adapter is often cheaper and more reliable than permanent UI automation. When a portal is necessary, implement a named workflow with takeover and maintenance ownership; do not claim universal online coverage.

## Tool implementation gate and sequence

1. Finish the core connector registry, grants, operation ledger, artifact/review contract, cost reservations and deterministic reference provider. Test these through the [core acceptance matrix](clothing-brand-pilot-charter.md#core-acceptance-matrix).
2. After core acceptance, select one actual tool from stdout's priorities. Local file handling and Git/isolated execution support the first design and storefront work; image/design tools, the procedural generator, optional storage/board sync, garment CAD and commerce follow individually as needed. Reuse existing code when it passes the new contracts; catalog presence alone is not acceptance.
3. Specify its exact operations, output types, data/credential grants, quota/cost policy, preview, failure/recovery paths, live test resources and quality criteria.
4. Implement that package, including UI setup and remediation. Test duplicate/lost responses, revoked credentials, concurrent edits, stale approvals and quota failures as applicable.
5. Complete a real pilot task, inspect actual output/remote state, accept the owner's revision cycle and record support limits. Enable the tool only at its achieved readiness level.
6. Choose the next tool based on demonstrated business need. Keep the existing accepted tools under regression checks while adding it.

Release gate for each tool: the owner sees what the agent is doing, inspects the exact output/evidence, requests revisions, approves a specific public effect when required, interrupts work and recovers from a worker/provider failure. A core fixture proves framework behavior; real provider and artifact acceptance prove that tool's supported workflow.

Public SaaS release is a separate gate covering hosted isolation/capacity, operations, billing/account lifecycle and the supported tool set. This plan does not require implementing every listed integration before a useful clothing-brand pilot can operate.
