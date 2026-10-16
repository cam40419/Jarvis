# Current platform state and next implementation

The milestone remains an accepted platform core. Simon supports project intake,
staffing, native execution, artifacts, independent review, repair and delivery
snapshots. Workspace storage entitlements now enforce atomic logical-content
admission and retain an audited administrator history.

The October 12 correctness follow-up addresses three concrete findings:

- Intake and artifact authority share a 2,000 retained-source-revision bound;
  complete historical and revoked metadata still participates in authority checks.
- Workspace existence survives removal of its last member, preserving platform
  storage administration without granting former members access.
- Supported native-core restores require an external recovery witness. Quarantine
  blocks dispatch and publication; old runners, grants, schedules and reservations
  stay fenced after explicit reconciliation and completion. Retained costs and
  unknown liabilities are not cleared by automatic retry or expiry assumptions.

Persistent installations need migration 0038 and the documented
[recovery procedure](runbooks/storage-recovery.md#enforced-native-core-restoration-quarantine).
Implementation does not migrate, restart or change a deployed installation.

[Typed dependencies and selective repair](runbooks/native-dependencies.md) now support this workflow:
version a fact or requirement, declare exact output dependencies, assess direct/transitive
impact, preview a bounded text/JSON repair and commit the batch atomically. Repaired outputs
require fresh review; unrelated bytes and approval identities are preserved. Missing coverage,
concurrent changes and revocation remain explicit. The
[registered fixtures](architecture/core-baseline-fixtures.md) now execute the apparel and
equipment repair workflows. Persistent installations also require migration 0039.

**Step 3 is implemented for the registered text/JSON and native-worker scope:**
[delivery acceptance, review inbox and authorized handoff](runbooks/delivery-review-continuity.md)
add required component roles, computed reopen/edit/render evidence, exact owner package
decisions, bounded review admission and permission-checked checkpoint succession.
Application-specific formats and measured review/takeover quality remain acceptance work.
Persistent installations require migration `0040_native_acceptance_continuity.sql`.
**Step 4:** physical capacity, compute isolation/admission, tenant
fairness, model-catalog administration and model-quality trials remain parallel
core work. Selective repair also exposes follow-up needs for metadata accounting,
history retention and graph/concurrency capacity measurements. Recovery still requires
truthful operator/provider reconciliation and
does not claim automatic detection of privileged database rewinds that bypass the
supported procedure.

The [roadmap](next-phases.md) and
[baseline quality plan](architecture/platform-baseline-quality-plan.md) govern the
remaining work. CORE-01 through CORE-24, PostgreSQL 17 validation and registered
comparative trials remain acceptance requirements. Business integrations follow
CORE-ACCEPT; historical coverage and synthetic tests do not establish that gate.
