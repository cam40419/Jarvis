# Home integration plan

Requested September 14, 2026. Household inventory: office LIFX Beam; two office Smart Life hexagon
sets (exact model still unknown); planned Shelly Plug US Gen4 outlets for bedroom/living-room lamps
and the office air purifier. See the [setup runbook](runbooks/home-devices.md).

| Provider | Initial transport | Implemented controls | Dependency |
| --- | --- | --- | --- |
| LIFX Beam | Cloud HTTP with bearer token | Whole-fixture on/off, brightness, status | LIFX token and exact light ID |
| Tuya / Smart Life | Region-specific signed cloud HTTP | Recognized lighting power and white-mode brightness, cloud status | Linked cloud project, API entitlement, two device IDs and compatible instructions |
| Shelly Plug US Gen4 | Local Wi-Fi HTTP RPC and Digest auth | Outlet power, watts, accumulated Wh | Installed plug, fixed private IPv4, device ID, optional shared admin password |

## First slice: implemented

- Version 1 strict home contracts, household scopes, named operator inventory, no arbitrary URLs.
- Chat device listing/status tools and durable power/brightness preview cards.
- Exact action confirmation, access revalidation, audit receipts, readback and unknown outcomes.
- Single dispatch per action even with concurrent clicks; no automatic retries after uncertainty.
- Connections panel with explicit status reads. Credentials remain local to the server.
- Legacy six-device inventory example, still useful for planned Shelly plugs.

The initial adapter runs in Simon's process. The architecture's separately deployable Home OS
gateway remains future work. The generic synchronous capability broker is not used for hardware
I/O because it holds a database transaction during handler execution. Home capabilities use the
same deterministic policy checks and a durable claim-before-network dispatcher instead.

`home.list_devices` and `home.get_status` are `read`; `home.set` is `write_soft` for recognized
discovered lighting or explicitly configured lighting/air purifier loads. The static manifest lives in `domain/connected_tools.py`,
with device schemas in `domain/home.py`.
The adapter recognizes documented lighting categories; the operator classifies legacy plug loads.
The model cannot change classification or enable controls. Broader appliance, hard, and dangerous
writes remain blocked by ADR 0002. State-setting
commands are idempotent in intent, but Simon still avoids repeated dispatch because physical
conditions or user intent can change between attempts. Preview creation is not actuation.

## Next: commission the office

1. [x] Discover the LIFX Beam and verify its reported state. Manual switching remains pending.
2. [x] Discover Smart Life's Up Arrow and Down Arrow and inspect their status/instructions.
   Both report category `dd`; power is supported, brightness is unavailable in their current mode.
   Add explicit mappings for further controls only after inspecting actual output.
3. [x] Add whole-fixture color and brightness in color mode, plus immediate batch controls.
   White temperature, Beam zones, and hexagon effects remain future work.

## Then: outlets and routines

1. Install the three Shelly plugs, reserve LAN IPs, verify the connected loads and purifier restart
   behavior, and commission one plug at a time.
2. Add per-device credentials and easier local setup/discovery. Consider a Home OS gateway for
   independent device connectivity and a LIFX LAN adapter for reduced cloud dependence.
3. [x] Add persistent room/group membership and chat organization. Scene execution with explicit
   per-device results and partial-failure handling is still future work.
4. Design schedules with timezone, persisted intent, cancellation, bounded execution and an
   appropriate authorization lifetime. Calendar events do not execute device commands.
5. Evaluate local Tuya or a home-automation gateway if cloud entitlement or vendor-specific
   capabilities limit the hexagons. Do not assume undocumented LAN protocols are universal.

Provider references were checked against official documentation on September 14, 2026; the setup
runbook links the API, authentication, device, and instruction references. Real read-only discovery
and status were verified September 15. No devices have been operated during verification.

Verification: 325 tests passed with 95.59% coverage, including PostgreSQL contracts and desktop/mobile
browser checks. Three opt-in paid model checks were skipped. Ruff, mypy, wheel build, isolated
package dependency validation, and the running server's health/UI checks passed. After correcting
the capability metadata to describe the complete action receipt, the focused home suite was rerun.
That first slice needed no schema migration; home previews use existing durable action snapshots.

## Automatic inventory and organization: September 15, 2026

- LIFX lists all account lights. Tuya pages the linked app-account endpoint, with no device/UID entry.
- Startup and five-minute background discovery, plus chat/UI refresh with duplicate suppression.
- Migration 0008 stores device inventory and per-provider sync claims/results in PostgreSQL.
- Atomic per-provider imports; failed or incomplete discovery preserves the last inventory.
- Named rooms and multiple groups are writable through chat or Connections, with audit records.
- Household isolation, repeat-safe chat edits, unknown types read-only, missing devices disabled.
- Tuya `us-west` aliases `us`; suspended data centers get an actionable error in Connections.
- Simon organization is shared across brands and does not modify vendor app organization.

The local credentials now discover Beam, Up Arrow and Down Arrow. Cloud discovery requires no
inventory file; Shelly keeps its explicit LAN configuration until its setup/discovery phase.

Verification: 356 tests passed with 95.88% coverage, including automatic startup discovery,
PostgreSQL persistence, pagination, failed sync recovery, household isolation, repeat-safe edits,
and desktop/mobile UI checks. Three opt-in paid model checks were skipped. Ruff, mypy, wheel build
and isolated package validation passed. Migration 0008 is applied locally and bundled in the wheel.
Live discovery/status checked all three office lights, and their Simon room is saved as Office.


## Direct home commands and color: September 15, 2026

- Replaced the chat home preview tool with `home_control`: power, brightness, and #RRGGBB color.
- Exact devices, room, group, or all lights in one call; implicit targets exclude air purifiers.
- No chat confirmation. Capability/scoping checks, per-device readback, and partial results remain.
- Migration 0009 saves each dispatch before network I/O, independently of answer completion.
- One command per device per answer; uncertain outcomes are never silently retried.
- Color uses LIFX hue/saturation and Tuya advertised standard JSON HSV formats, preserving intensity.
- Chat shows device result cards; Connections exposes receipts even when an answer is cancelled.
- Tuya credentials restored locally; discovery registered Up Arrow and Down Arrow in Office.
  Both advertise `colour_data` HSV with saturation/value 0?1000. Read-only live checks and
  intercepted command preparation verified blue requests for both; no commands were sent.
- The Beam also advertises color support. Its Office room remains saved.
- Physical color/switch testing remains pending; automated provider tests use simulated responses.

Verification: the full suite passed 390 tests (three paid live-model checks skipped), including
PostgreSQL persistence and desktop/mobile browser checks. A subsequent focused 20-test run
verified the final Think deeper dispatch restriction. Ruff, mypy, wheel build, and isolated
package dependency checks passed. Migration 0009 is applied locally and packaged. The running
server health check passed. Live discovery/read-only checks verified all three office fixtures;
Tuya color commands were prepared against live specifications with dispatch intercepted.


## Shelly LAN and backend metering: September 15, 2026

- Automatic mDNS discovery and verified Plug US Gen4 registration; stable IDs across address changes.
- Owner-only backend setup identifies lighting/air-purifier loads before enabling control.
- Immediate backend on/off commands with durable idempotency, independent of a chat/model run.
- Background read-only power polling, configurable cadence/retention, persisted samples in migration 0010.
- Latest readings, stale/error state, timeseries, and observed Wh/kWh summaries with reset/gap handling.
- No consumption dashboard yet. Next UI phase should use the existing power endpoints for current
  readings and historical charts, clearly showing incomplete coverage and stale samples.
- Per-device credentials, cross-subnet discovery configuration, longer-period aggregation, and
  utility-rate/cost calculations remain future extensions.

Verification: 410 tests passed with 95.24% coverage, including PostgreSQL contracts and
browser checks; three opt-in paid model checks were skipped. Ruff, mypy, wheel packaging,
isolated package dependency validation, and server health checks passed. Migration 0010 is
applied locally. Live mDNS discovered two Plug US Gen4 outlets (IDs ending f76154 and f767b4);
both returned electrical readings and accumulated multiple persisted samples without any
switch commands. They remain unassigned/read-only until their connected loads are identified.

## Chat device naming and outlet setup: September 15, 2026

- `home_rename_device` saves names for LIFX, Tuya, and Shelly devices in Simon, including legacy
  configured inventory. Names survive provider refreshes and service restarts; vendor names
  remain unchanged. Renaming preserves room/group membership, load type, and control enablement.
- `home_setup_outlet` lets the owner identify a discovered plug's connected lamp or air purifier,
  set its name/room, and enable control from chat. A naming request alone does not enable control.
- Shelly inventory includes an identifier suffix to distinguish unnamed plugs without requiring
  inventory-file edits. Simon asks for a target when names or descriptions are ambiguous.
- Setup and naming save immediately without confirmation cards or switch commands. A requested
  power change can follow setup in the same answer through existing durable command receipts.
- Metadata edits are audited, household-scoped, and replay-safe. Revoked, expired, cancelled,
  and Think deeper requests cannot execute the new tools. Discovery merges names under the same
  inventory transaction as room/group overrides.
- The bounded model tool sequence permits five tool steps plus a final response for discovery,
  naming, setup, organization, and control; existing token/time/call limits still apply.
- No database migration or consumption dashboard is needed for this phase.

Verification: 423 tests passed with 95.35% coverage, including PostgreSQL persistence and
browser checks; three paid live-model checks were skipped. New tests cover setup followed by
control in one chat run, persistent names across providers/restarts, replay protection, scope
revocation, invalid loads, expired requests, Think deeper, and the bounded model tool loop.
Ruff, mypy, wheel build, isolated package dependency validation, and the running server health
check passed. Physical plugs were not renamed, reconfigured, or switched during this phase.

## Outlet setup and power controls in Connections

- Discovered Shelly outlets now show editable names, rooms, and load types for owners. Selecting
  Light / lamp or Air purifier enables the control checkbox by default; Not assigned clears it.
- Saving uses the existing setup API and persists the same inventory used by chat. Lamps join
  room/all-lights commands immediately. Configuration saves never switch power.
- Direct Turn on / Turn off controls use explicit desired states and fresh idempotency keys.
  Buttons stay disabled during requests; uncertain results require a status check before another
  command. Results distinguish verified, accepted, failed, and uncertain changes.
- Chat instructions resolve "toggle" against a live status read, without guessing unknown state.
- Connections waits for session initialization, fixing an empty-panel race after page reload.
- Desktop/mobile browser coverage includes real API setup persistence, subsequent chat control,
  simulated switching, uncertain outcomes, reload, and disabling an unassigned load.

Verification: 424 tests passed with 95.35% coverage, including PostgreSQL and desktop/mobile
browser checks; three paid model checks were skipped. Ruff, mypy, and whitespace checks passed.
Physical outlets were not switched. Docker and the existing local database were restarted for
testing; the development Simon server was not running on port 8000 at verification time.

## September 16: dashboard and voice phase

The Home dashboard now exposes room-based controls for all three providers, plus 1/6/24-hour
Shelly power charts with observed consumption and coverage. Voice delegates device requests
to the same existing tools and receipts. The deployment and subsequent scenes/schedules work
is tracked in [next phases](next-phases.md); see [remote voice setup](runbooks/remote-voice.md).
Physical switching and phone speech acceptance tests remain user-device checks.
