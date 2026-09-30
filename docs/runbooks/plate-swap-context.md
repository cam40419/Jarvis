# Plate swap context investigation — 2026-09-17

## Revert

The operator reported that extending the backward move after the second
forward-max broke the swap. Restored `G1 Y-212.2 F3000` in the sibling
`autoswap_rip/eject_sequence.gcode` and `bed_swap.gcode`, and restored the
matching assertion in `test_swaptool.py`.

The active sequence is again the existing verified trial
`ec6cba66a9cc49e6bef1054555234b3b`, SHA-256
`33267e5f2c5a981e9154e34c3088ccd7fc867377474ac71ddaa19a04ccb644e3`.
The reported failed 250.3 mm trial `e6d0cd9e0cc4414697c856f5d1dffa5e`
was recorded as failed. Its original artifact remains available as history.
The queue contained no batches at the time of the revert.

The restored command requests Y47.8 after Y260. The reverted revision
requested Y9.7. These are calculated coordinates, not measured bed positions.

## Evidence from the printer

Retrieved these existing files from the printer using read-only FTPS:

- `autoswap_5615d300_4735ff280c17.gcode.3mf` — first print of the earlier
  completed two-print batch.
- `autoswap_f601d885_21b90775ad96.gcode.3mf` — first print of the later
  repeated batch.

Both downloads are 67,001 bytes. Their extracted `Metadata/plate_1.gcode`
contents are identical. Each contains exactly one swap block, and each block
exactly matches the restored verified trial, including all 15 moves.

Local evidence is retained under the sibling project:
`autoswap_rip/spool/diagnostics/swap-context-20260917/`.
`comparison.json` records hashes, relevant source commands with line numbers,
and calculated endpoints for every swap move. The directory also contains
the downloaded archives, extracted G-code, end-of-print excerpts, and a
database backup taken before reverting the active trial.

No printer motion, heating, homing, upload, or print-start command was sent
during this investigation. The restored swap passed all 10 `test_swaptool`
tests using mocked printer transport. Those tests do not verify physical motion.

## What differs between execution paths

`swaptool.send_over_lan` submits only the validated movement commands through
MQTT `gcode_line`. It checks readiness and homing flags but does not establish
acceleration, software travel limits, motor current, or leveling state.

`workflow.patch_gcode` inserts those same commands after the sliced print's
normal shutdown routine and before its final `M18 X Y Z`. The uploaded files
have this surrounding context:

| Setting or position | Last relevant command before the embedded swap | Source line |
| --- | --- | --- |
| Software travel limits | `M211 X0 Y0 Z0 ;turn off soft endstop` | 1208 |
| Bed leveling | `G29.2 S1`, followed later by `G29.1 Z-0.02` and `G29.4` | 1086, 1194, 1211 |
| Units | `G21` | 1219 |
| Acceleration | `M204 S10000` | 11188 |
| Z position after shutdown lift | `G1 Z108` | 11297 |
| Z motor current | Temporary `M17 Z0.4`, then `M17 R` | 11294, 11300 |
| XY parking position | `G1 X-48 Y180 F3600` | 11303 |
| Speed multiplier | `M220 S100` | 11305 |
| Acceleration multiplier | `M201.2 K1.0` | 11306 |
| Motion barrier | `M400` | 11335 |
| Swap begins | `; AUTOSWAP_BEGIN`, followed by `G90` | 11336 |

These are source commands, not a readback of effective firmware settings.
Standalone settings at the time of the successful test were not captured;
their values cannot be inferred solely from a homing acknowledgment.
Bambu's [official A1 startup template](https://github.com/bambulab/BambuStudio/blob/master/resources/profiles/BBL/machine/Bambu%20Lab%20A1%200.4%20nozzle%20template%20machine_start_gcode.json)
also labels `M211 X0 Y0 Z0` as disabling software travel limits.

The print contains no XYZ `G92` offset change and no homing command after
printing starts. The reduced-current line immediately before the swap is a
comment, `;M17 X0.8 Y0.8 Z0.5`; it does not execute. The swap explicitly starts
in absolute positioning and switches modes around every relative move.

## Remaining diagnosis

The inspected files rule out different stored swap moves for these two jobs.
They do not establish the physical cause of the differing outcome.

1. Determine whether the bed itself reaches a different endpoint, or the
   removable plate slips/fails to catch while the bed reaches the same endpoint.
2. If the endpoint differs, investigate effective travel limits and position
   tracking first. The sequence requests Y260, Y-3 and Z260; firmware handling
   of those boundary requests must be observed. A calculated coordinate alone
   cannot demonstrate the actual position.
3. If the bed endpoint is the same, compare acceleration/reversals and the
   plate's initial seating, temperature and catch engagement. The print's
   acceleration command is known, but the standalone test's is not.
4. Use the same original distances for that comparison. Any later change to
   execution settings should be isolated and assessed in both execution paths.

The cause remains unconfirmed. No acceleration, limit, insertion-point or
homing change has been applied to the active sequence as a speculative fix.

## Travel-limit follow-up

The operator requested that we investigate the limits during printing and
prepare to keep the swap within them.

### Confirmed bounds and limitations

The actual uploaded print declares `printable_area = 0x0,256x0,256x256,0x256`
and `printable_height = 256` at lines 395-396. Bambu's
[common machine profile](https://github.com/bambulab/BambuStudio/blob/master/resources/profiles/BBL/machine/fdm_bbl_3dp_001_common.json)
defines that XY area, and the
[A1 profile](https://github.com/bambulab/BambuStudio/blob/master/resources/profiles/BBL/machine/Bambu%20Lab%20A1%200.4%20nozzle.json)
sets the 256 mm height. This provides a documented print-area envelope, not
the firmware's exact soft-endstop or physical rail coordinates.

The A1 startup routine also uses Y261 and Y262.5 for nozzle cleaning. Those
commands demonstrate why a printable-area number must not be represented as
the complete physical travel range.

The uploaded print explicitly disables software limits with `M211 X0 Y0 Z0`
before printing and does not explicitly re-enable them before the swap.
Bambu Studio's [manual axis control implementation](https://github.com/bambulab/BambuStudio/blob/master/src/slic3r/GUI/DeviceCore/DevAxisCtrl.cpp)
uses `M211 S` to save the state, `M211 X1 Y1 Z1` for a manual jog, and `M211 R`
to restore the saved state. Simon's standalone swap is a custom `gcode_line`
payload and does not use that jog wrapper. Therefore, Studio's behavior does
not prove which limit state the earlier Simon test inherited.

Read-only `pushing.pushall` and `info.get_version` requests identified firmware
01.08.01.00 (motion-controller version 00.01.30.58). The returned status fields
did not expose XYZ positions or software-limit bounds/state. The selected
status fields and report-key inventory are saved in `travel-limit-status.json`.
No speculative `M211` query or state-changing command was sent.

| Original requested target | Declared print-area boundary | Difference |
| --- | --- | --- |
| Y260 | Y256 | 4 mm above |
| Y-3 | Y0 | 3 mm below |
| Z260 | Z256 | 4 mm above |

These differences could matter to a plate catch. They do not establish that
the printer clamps at those exact coordinates, nor do they by themselves
explain a 38.1 mm discrepancy. The source shows limits being disabled during
printing; it does not show a tighter print-mode limit shortening the swap.

### Prepared candidate

Added `autoswap_rip/eject_sequence_print_area_candidate.gcode` and a new
validated draft in Simon's swap-test list, labelled **Print-area candidate**.
The draft is not a verified motion result. The original verified trial remains
active for direct Swap and batch preparation.

The candidate uses 15 absolute moves with targets inside 0-256 mm. It retains
the move order and feedrates, replaces boundary requests with Y0/Y256/Z256,
and preserves the original interior endpoints where possible:

| Segment | Candidate target | Change in commanded distance |
| --- | --- | --- |
| Back after second forward max | Y47.8 | 208.2 mm instead of 212.2 mm, because forward max is 4 mm lower |
| Small forward nudge after back max | Y16.05 | 16.05 mm instead of 19.05 mm, because back max is 3 mm higher |
| Forward before lowering | Y149.4 | 149.4 mm instead of 152.4 mm, for the same reason |
| Lowered Z | Z183.8 | 72.2 mm down instead of 76.2 mm, because starting Z is 4 mm lower |
| Final back/forward pair | Y205.2, then Y256 | Still exactly 50.8 mm in each direction |
| Final backward move | Y103.6 | Still exactly 152.4 mm; final Y is 4 mm lower than the original |

Local checks verified every commanded endpoint, the retained interior
coordinates, the exact final two-inch pair and six-inch retreat, and insertion
of this candidate into the actual sliced G-code without changing the surrounding
print code or adding homing. The results are in
`print-area-candidate-check.json`, with a local embedded preview in
`print-area-candidate-preview.gcode.txt`.

At preparation time, the candidate had not been sent to the printer. The
operator subsequently reported failure for both the original candidate trial
`ce06351e83df4399b6f922250e7b84e7` and its identical repeat
`1fcc4d3cbcca468192d34646d9903713`. Both are now recorded as failed. The repeat
was resolved from the operator's explicit report that the reduced limits did
not work.

## Explicit software-limit state for embedded swaps

The operator asked whether the limits could be removed when running the swap
within a print. Updated `autoswap_rip/workflow.py::patch_gcode` so newly
prepared prints use this wrapper at the existing insertion point:

```gcode
M400 ; wait before plate swap
M211 S ; save software endstop state
M211 X0 Y0 Z0 ; disable software endstops for plate swap
; AUTOSWAP_BEGIN
; exact movement commands from the active verified sequence
; AUTOSWAP_END
M400 ; wait for swap motion
M211 R ; restore software endstop state
```

The active movement sequence remains `ec6cba66a9cc49e6bef1054555234b3b`, with
the original Y260/Y-3/Z260 targets and 212.2 mm retreat. No homing was added.
The limit commands sit outside the immutable movement markers, so the
sequence hash continues to identify the exact verified movement commands;
the prepared archive's own hash covers the wrapper as well.

The previous limit state is restored after the motion barrier. This does not
change physical rail limits, remove other firmware constraints, or guarantee
that the physical swap issue is fixed. The original sliced startup already
disabled software endstops; this change asserts the setting again at the
swap itself instead of relying on the inherited state.

The standalone swap sender retains its existing behavior. The bounded
print-area candidate remains failed history. Existing prepared files would
retain their prior contents; the queue contained no batches when this change
was applied, so the next batch preparation will include the wrapper.

Verification: all 29 `test_workflow` and `test_swaptool` tests passed. A new
regression checks that either inherited limit setting is saved, limits are
disabled immediately before the unchanged movement block, the saved setting
is restored after the motion barrier and before motor-off, and no additional
homing is inserted. A local archive preview prepared from the actual retrieved
print also passed movement-hash and plate-MD5 checks. That preview is stored
as `explicit-swap-limits-preview.gcode.3mf` in the diagnostics directory and
has not been uploaded or dispatched.

## Actual batch after explicit limit commands

The operator reported that the movement still appeared limited and asked us
to confirm the full movement code. Inspected batch
`135333218e1f45ee8b83dee0ed02327d`, first job `cf5c50683da5`, and then downloaded
the actual uploaded file `autoswap_43c5b70d_cf5c50683da5.gcode.3mf` from the
printer over read-only FTPS.

- Downloaded size: 67,070 bytes.
- Archive SHA-256: `7a50878b75bf3731e4e6e92d0c5d8fc6c5bafe7c1f9135586353b461bd08c9f9`.
- The downloaded archive exactly matches the locally prepared archive and
  the job's recorded archive hash.
- Its movement block exactly matches the active original verified sequence,
  SHA-256 `33267e5f2c5a981e9154e34c3088ccd7fc867377474ac71ddaa19a04ccb644e3`.
- All 15 moves are present, including Y260/Y-3/Z260, the 212.2 mm retreat,
  the final 50.8 mm backward/forward pair and 152.4 mm backward finish.
- The explicit `M211 X0 Y0 Z0` command is immediately before the movement
  block, with the motion barriers and save/restore commands intact.
- The swap contains no homing.

This rules out a stale upload or accidentally using the reduced 0-256 mm
candidate for this job. It verifies file contents, not runtime execution of
each command or the effective firmware limit state.

The runner stopped on `FAILED (0x0300400C)`. The installed Bambu Studio error
catalog `hms/hms_en_039.json` maps that code to "The task was canceled."
It is not evidence that a software travel limit was hit. A later read-only
status sample showed FAILED, no current error/HMS alert, and a cleared XYZ
homing mask; its line-number field was zero, so it did not locate the move
at which execution stopped. The second print remains queued and the batch
remains `needs_attention`.

Evidence: `latest-uploaded-full-movement-check.json`,
`latest-uploaded-full-movement.gcode.txt`, and
`explicit-limits-failed-status.json` in the diagnostics directory. The next
needed observation is which move actually stopped short. No further motion
or limit changes were made on the assumption that another command was missing.

## Clearing motion during the full-range swap (September 17)

The operator reported that the first up/forward/back/forward moves looked
correct, followed by a backward pull about 2.5 inches too long. The subsequent
back-max move did not reach the expected physical rear position. The operator
then located an unexpected nozzle-clearing motion immediately before that
long backward pull (move 5).

Inspected job `d302cfd8e9ce` in batch `5990b79f3c9f4563aa04966cbb1a0147`.
Downloaded its actual uploaded archive
`autoswap_57516666_d302cfd8e9ce.gcode.3mf` from the printer. It is 67,070 bytes,
matches the local prepared archive byte for byte, has a valid plate MD5, and
contains the original 15-move block with SHA-256
`33267e5f2c5a981e9154e34c3088ccd7fc867377474ac71ddaa19a04ccb644e3`.

### Confirmed command order

| Move | Commanded target or displacement | Ideal endpoint |
| --- | --- | --- |
| 1 | Absolute X0 Z260 | Z260; Y inherited |
| 2 | Absolute Y260 | Y260 |
| 3 | Absolute Y-3 | Y-3 |
| 4 | Absolute Y260 | Y260 |
| 5 | Relative Y-212.2 | Y47.8 |
| 6 | Absolute Y260 | Y260 |
| 7 | Absolute Y-3 | Y-3 |

The 212.2 mm pull is 8.35 inches. Its endpoint assumes the previous forward
move actually established Y260. A routine changing position or mode could
affect a relative move. The later back-max already explicitly uses G90 and
Y-3, so a wrong relative displacement alone does not explain that later
physical shortfall. Lost motion, changed coordinate interpretation, or
firmware constraints remain possibilities, not established causes.

There is no G28, XYZ G92, wipe, purge, AMS unload, or G39 command inside the
swap. Stock AMS unloading and X-axis wiping occur before the swap, separated
from it by M400 barriers. The final layer contains conditional G39.3 checks,
but those also precede the swap. We have not identified what triggered the
operator-observed clearing motion; it must not be attributed to clump
detection, AMS behavior, or a reset without further evidence.

### Motion context and evidence limits

- The last explicit acceleration command before the swap is `M204 S10000`.
  The swap does not set its own acceleration. The standalone test's effective
  acceleration was not captured, so its numerical value cannot be compared.
- All Y moves explicitly use F3000 (50 mm/s); Z uses F900 (15 mm/s). The
  preceding print's F42000 is not inherited by these moves. Abrupt acceleration
  is a plausible contributor to the reported speed, not proof of skipped steps.
- The original sequence mixes G90/G91 in both standalone and embedded runs.
  Relative positioning is a shared vulnerability, not itself a difference
  between their movement files.
- The embedded wrapper explicitly saves/disables/restores software endstops;
  the standalone sender still sends the original movement file directly.
  A standalone success therefore does not verify identical execution settings.
- The runner logged one project-file publish attempt and a correlated printer
  acknowledgment for this retry. It reported layer 50, then finish; the second
  job was canceled without starting. No reset was identified from these logs.
- `mc_print_line_number` remained zero, and available telemetry did not give
  executed XYZ positions. File equality cannot establish the physical path.
- The camera index identified recording 79. Its first frames were readable,
  but FTPS does not support REST (502), and the 115 MB download was stopped
  after about 3.3 MB because throughput was too low to obtain the relevant
  end segment promptly. The partial recording is not evidence of the swap.

Evidence is in `autoswap_rip/spool/diagnostics/swap-interference-20260917/`,
particularly `findings.json`, the downloaded archive, and the extracted G-code.
No production motion, distances, detection settings, or active-trial state
were changed during this investigation; no physical command was sent.

The next diagnostic should identify the unexpected nozzle path (far-left
purge/wiper versus a corner dip) and capture the affected moves in video.
A controlled comparison should explicitly set a modest acceleration, retain
all original full-range targets, use absolute endpoints for the relative
segments, and use motion barriers between steps. Use the same execution
wrapper in standalone and embedded tests. Do not mark such a variant verified
from endpoint calculations alone: absolute targets do not correct lost steps
or a changed coordinate origin, and an idle test does not reproduce every
in-print firmware behavior.

## Cold print-mode diagnostic control

Added **Automations > Swap tests > Test as a print > Run swap as print**.
The button snapshots the current editor content, stages an independent
`swap_test` batch/job, then requests its start. There is no object/file picker.
The record provides download, start/retry when staged, pause/resume/stop while
running, resolution after an uncertain result, and deletion when inactive.
Normal print batches and their repeat behavior remain separate. A completed
test file does not verify or activate a motion sequence.

The builder uses a sanitized copy of the previously accepted A1 3MF container,
replaces all executable plates, updates the plate MD5, and starts the file via
the existing upload-verification and `project_file` runner. It sets both known
bed-leveling flag spellings false for tests, along with flow/vibration
calibration, timelapse, and AMS. No automatic homing is inserted. The server
requires XYZ homed before launch and the runner checks a fresh homing mask
again after upload. The prepared archive hash is checked before transfer.

The default **Current print settings** preserves movement commands and uses
M204 S10000 to reproduce the last explicit acceleration found before the
problematic print's swap. **Slow observation** sets M204 S500, caps XY feedrate
at 1500 mm/min and moves containing Z at 600 mm/min, and adds M400 P500 after
each move. Both use the same software-limit wrapper as production embedded
swaps. Absolute/relative modes and distances are unchanged in both choices.

These are cold motion tests: heater targets are zero, there are no E moves,
tool changes, purge/wipe, or stock startup/shutdown routines. They isolate the
print-file execution path and motion settings; a successful cold test does
not rule out interference from firmware state established by real printing.
The file ends with XYZ motor release, so another home may be needed before
repeating it. No hardware test was dispatched during implementation.

Verification: 37 AutoSwap unit tests, 9 API integration tests, and 2 browser
tests passed (headless Edge). The browser test exercises editable moves,
homing readiness, prepare/start, running status, and stop without contacting
the printer. A physical firmware acceptance/motion check remains for the user.

### Print-mode startup failure and correction

The first three cold tests (`3771ceb15bbf`, `c0466e66c8d4`, `724f50b060ae`)
uploaded and verified successfully at 29,272 bytes. Each received a correlated
successful `project_file` acknowledgment, but only IDLE was observed. The first
and third timed out without observing execution; the second was stopped by
the operator and then lost MQTT connectivity. Acknowledgment did not prove
the motion file had begun.

Found a concrete file-format defect: the initial cold builder replaced the
entire G-code with a short custom header and omitted CONFIG_BLOCK_START/END.
Keeping `project_settings.config` in the ZIP was insufficient to preserve
this G-code structure. BambuStudio's own GCode.cpp, around its configuration
serialization, explicitly says the printer needs this information:
https://raw.githubusercontent.com/bambulab/BambuStudio/master/src/libslic3r/GCode.cpp

Rebuilt the sanitized template using the comment-only header/config preamble
from the actual successful `d302cfd8e9ce` file. The builder now preserves that
serialized configuration verbatim, validates its marker order, and rejects
any executable command in the preamble. Only the generated cold body is
executed. The header reports one motion-only layer and zero filament, and a
layer progress marker is emitted without extrusion. All 15 original movement
commands remain in the current-settings variant. The revised local previews
are 39,506 bytes (current) and 39,512 bytes (slow).

Also corrected premature status reporting: the job remains `starting` after
an acknowledgment until matching PREPARE/RUNNING/PAUSE is observed. An IDLE
report cannot copy an old print's 100% into the new job before execution.
The UI distinguishes starting from running and offers only stop while starting.

After the operator reported no execution and a new read-only sample confirmed
IDLE/no alerts, closed only the latest failed diagnostic record as canceled.
Its immutable old file was retained; the next **Run swap as print** prepares
the corrected file. That sample still reported XYZ homed. No new start,
upload, homing, or motion command was sent by the assistant.

Evidence: `autoswap_rip/spool/diagnostics/cold-print-start-20260917/` contains
the old template, failed row, status at closure, and corrected file previews.
39 AutoSwap tests, the cold-test API integration, and the browser run/stop test
passed. Firmware execution of the corrected cold file still requires an
operator test; the missing configuration is a confirmed generation defect,
not a firmware-proven explanation for every idle acknowledgment.

### Separate swap after a print

The current sliced end code releases XYZ motors with `M18 X Y Z`. Saved
commanded coordinates cannot replace a valid physical homing reference after
motor release, manual movement, skipped steps, or restart. Re-enabling motors
or assigning coordinates would not re-establish that reference.

A separate post-print swap is a possible design if the print is prepared to
retain motor holding and the actual A1 preserves its homing state through
FINISH. Verify that behavior on hardware before changing the batch handoff;
do not infer it solely from removing M18. The separate sender would require a
fresh idle/alert-free/XYZ-homed report and must stop if the reference is lost.
The operator's no-homing-between-prints requirement remains in force. No
production batch handoff or motor-release command was changed in this fix.

### 2026-09-17: operator requested the separate post-print swap approach

Supersedes the previous handoff design for newly staged Simon batches. The operator
reported the cold-test A1 screen remained on its normal home screen, then requested:
keep motors enabled after the print, run the swap separately, release them afterward.

Implemented in `autoswap_rip/workflow.py` and `simon_bridge.py`:

- Intermediate prepared prints replace their final `M18 X Y Z` with a hold marker
  and `M400`. Normal print startup, printing, shutdown and park commands remain.
  No swap is embedded. The last print remains byte-for-byte unchanged.
- Each new batch freezes normalized moves in `spool/uploads/<batch>/batch-swap.gcode`
  with its SHA-256. The active 15 moves still hash to
  `33267e5f2c5a981e9154e34c3088ccd7fc867377474ac71ddaa19a04ccb644e3`.
- After observed activity and matching FINISH, the runner waits for fresh
  IDLE/FINISH, no alerts/errors, and explicitly reported XYZ-homed flags.
  Lost/missing homing stops the handoff; it does not add homing or reset coordinates.
- One LAN `gcode_line` contains `M400`, the exact standalone movement sequence,
  `M400`, then `M18 X Y Z`. There are no added cleaning or software-limit commands.
- Persistent swap state is claimed before publication. Unknown dispatches are
  never automatically retried. Correlated command acknowledgment is honored even
  if MQTT PUBACK is missing; acknowledgment is not proof of completed motion.
- Print completion displays 100%, while the swap remains `accepted_unverified`.
  The existing plate check now explicitly requires waiting for motion before
  continuing. Checking it marks the separate swap verified by the operator.
  Read-only recovery cannot execute or certify a missing separate swap.
- Existing prepared jobs retain their embedded method. Prepare or repeat a batch
  for this change. The UI labels the method and separate swap state.

At 02:14 UTC Sep 18, a read-only LAN query reported IDLE, no error/HMS alert,
and home_flag 847201687. No queue runner was active. Closed only diagnostic job
`45edf9483a7c` / batch `3acd27809526471fb3cfd360cb06bfeb` as canceled, preserving
the file, original row snapshot, and failure evidence. This cold test had also
received a successful start acknowledgment but never showed execution; its root
cause remains unproven. The closure removes its block on staging another batch.

Verification: all 48 AutoSwap tests, nine API integration tests, and the sequence
browser test passed. Tests simulate printer reports and commands. A local preview
was also built from the retained real print `d302cfd8e9ce`, reconstructing its
unpatched body by removing the one exact known swap block. All other archive
members were retained and the plate checksum verified. Evidence is under
`autoswap_rip/spool/diagnostics/separate-swap-20260917/`.

No upload, print start, homing, motion or motor-release command was sent by the
assistant. The first supervised hardware run still must establish whether A1
firmware retains motor holding and the physical reference through FINISH.

### 2026-09-17: separate swap did not follow the first print

Batch `edd2f9f3798d452980c640ab0961829f`, first job `5a5a8ceae938`, printed
successfully. The runner then failed at 02:34:55 UTC Sep 18 with
`printer did not report status`. There was no `gcode_line` publish attempt.
The failure came from the extra readiness read inside `run_separate_swap`:
the runner had already consumed the matching FINISH report, discarded it
instead of passing it to the handoff, and demanded another report within
up to ten seconds (fifteen seconds overall). Earlier matching progress
reports in this run were typically twenty seconds apart.

The old recovery action could also turn a still-pending, never-sent swap
into `verified` when the operator selected "Print and swap finished".
The second job `b66b7af65352` was uploaded afterward but the operator's
cancel request was honored before its print-start command. It remains canceled.

Corrections:

- Pass the fresh matching FINISH report directly to the swap readiness check.
  If a full fresh homing/state/identity report is still needed, poll in bounded
  windows for up to sixty seconds. A short `StatusTimeout` does not terminate
  that wait. Disconnects, alert/error states and lost homing still stop it.
- Persist `print_completed_at` and 100% print progress before attempting the
  swap. Record `print_finished` and `separate_swap_waiting` log events so the
  next failure identifies its phase.
- Both API and runner block a later print behind an incomplete earlier print
  or an unverified separate swap. Recovery cannot mark a pending swap finished.
- UI shows "Print finished" and "Pending — not sent" separately. It offers
  **Retry pending swap only**, using `/batches/{id}/retry-swap` and the bridge's
  `run-pending-swap` command. This requires recorded completion, pending swap,
  fresh matching print identity and XYZ homing. It sends only the stored swap;
  no upload, reprint or homing. Sending/unknown/accepted swaps cannot be retried.
  Retry does not resurrect canceled print jobs.

At 02:42 UTC, a new read-only sample identified the first job as FINISH,
with home_flag 847201687, no error and no HMS alerts. Corrected that job's
erroneous `verified` recovery mark back to `pending`, retaining print completion
and 100% progress. Its batch and second job remain canceled; the retry control
is available for the unsent swap only. Saved the previous rows and live status
under `autoswap_rip/spool/diagnostics/separate-swap-handoff-20260917/`.

Verification: 37 targeted AutoSwap tests, ten API tests and two browser tests
passed. Regression tests cover a twenty-second report delay, reusing FINISH,
complete status timeout with pending swap retained, no repeated dispatch,
retry without upload/reprint, canceled jobs remaining canceled, and preventing
the recovery action from skipping an unsent swap. Simon was restarted.
No physical printer command was sent by the assistant.

### 2026-09-17: reverted to the embedded swap

The separate post-print handoff failed on hardware twice more after the
correction above. Batch `a72d45405fd44bd2bf631250065ace43` (job
`db8fe8af8026`) and batch `683ff45606a74c86bf7300012b3c0e91` (job
`99e6a7d5eeb5`) each printed to a matching FINISH with a fresh XYZ-homed
report, the runner published the `gcode_line` swap, and the A1 returned no
acknowledgment within fifteen seconds. Both batches were resolved as canceled.
The operator reported that the separate swap does not work and asked for the
previous setup, where the swap G-code is appended to the intermediate print.

Reverted in `autoswap_rip/workflow.py`, `simon_bridge.py`, Simon's print
batch API (`src/simon/api/print_batches.py`) and the Automations page:

- `simon_bridge.stage` again prepares every print except the last with
  `prepare_print`, which inserts the active verified sequence before the
  sliced file's final `M18 X Y Z` inside the `M211` save/disable/restore
  wrapper. FINISH arrives after the swap; the batch then waits for the plate
  check. The last print is copied unchanged.
- Removed the hold marker and `hold_motors_gcode`, the per-batch
  `batch-swap.gcode` snapshot, `run_separate_swap`, `finish_separate_swap`,
  `run_pending_swap`, the bridge's `run-pending-swap` command, the API route
  `/batches/{id}/retry-swap`, the swap-state gating in `start`, `resolve`,
  `run_queue` and recovery, and the "Retry pending swap only" control.
- New jobs no longer record `swap_mode`, `swap_path`, `swap_sha256`,
  `swap_state` or `print_completed_at`; the schema migration list no longer
  adds those columns. The live `queue.sqlite3` keeps the columns, unused.
- The Automations page describes the swap as inside the print file again and
  shows the plain job state instead of "Print finished".
- Batches prepared during the separate period are all canceled. Their spool
  files still carry the hold marker; delete them from the page or repeat them,
  which prepares fresh embedded files from the retained sliced sources.
- Tests: deleted `test_separate_swap.py` (its generic acknowledgment test
  moved into `test_workflow.py`); the staging, repeat, trial, API and browser
  tests now assert the embedded block sits after the park move and before
  `M18 X Y Z`, and that no snapshot file is written.

Verification: 40 AutoSwap tests, 9 API integration tests and the sequence
browser test passed. A dry run of `prepare_print` on the retained Test_Cube2
sliced file with the active sequence produced one embedded block and a
matching plate MD5, written only to a temporary folder. No upload, print
start, homing, motion or motor-release command was sent by the assistant.
Simon must be restarted to load the API change; the bridge and runner are
separate processes and use the reverted code on their next start.

### 2026-09-18: confirm embedded setup and inspect reported extra backward motion

The operator requested the original embedded setup again and reported an extra
backward movement during the latest run. On inspection, staging, runner, API and
UI had already been reverted to embedded swaps. The latest log shows job
`6f78cb91b88e` in batch `2c447c971bd144fda585ea2647c76a3e`, started at 13:29 UTC
and FINISH at 13:41:55 UTC. Its batch rows and local prepared file had been
deleted, so downloaded the actual retained printer file read-only via FTPS:
`autoswap_17752112_6f78cb91b88e.gcode.3mf` (67,070 bytes).

The actual uploaded plate contains exactly one embedded block. Its normalized
15 moves exactly match active trial `ec6cba66a9cc49e6bef1054555234b3b`, SHA-256
`33267e5f2c5a981e9154e34c3088ccd7fc867377474ac71ddaa19a04ccb644e3`.
The entire plate G-code is also identical to the retained earlier embedded
job `d302cfd8e9ce`. Plate MD5 is valid. There is no G0/G1/G28 after the swap:
only M400, M211 R, M18 X Y Z and final progress. Before the swap are the sliced
filament-unload/wipe/park commands, including final `G1 X-48 Y180 F3600`.

This confirms no movement commands were added to this file versus that earlier
embedded artifact; it does not explain the operator's observed physical motion.
The operator identified the backward movement after UP, forward max, back max,
forward max: move 5, `G91` / `G1 Y-212.2 F3000` / `G90`. Its commanded travel is
212.2 mm (8.3543 inches), ending at Y47.8 from Y260. That distance was chosen
when the earlier forward 4.5-inch / back 2.5-inch pair became forward-max with
a compensated backward move, retaining the earlier Y47.8 endpoint. Asked for
the desired backward travel before changing it. Do not infer a replacement
distance or remove either adjacent absolute back-max command.
No active sequence or movement was changed. Evidence and the extracted swap
are in `autoswap_rip/spool/diagnostics/embedded-swap-review-20260918/`.

Verification: 20 queue tests and eight API integration tests passed. No print,
homing or motion was started. Restarted Simon to ensure the API uses the reverted
code; new batches embed the swap in every intermediate print and preserve the
final print unchanged.

### 2026-09-18: move 5 shortened to 6.35 inches at operator request

The operator specified **6.35 inches** for the backward move after the second
forward-max. Changed only `G1 Y-212.2 F3000` to `G1 Y-161.29 F3000`. Exact
conversion is 6.35 × 25.4 = 161.29 mm; the commanded endpoint from Y260 is
now Y98.71. All other positioning modes, movement commands and feedrates are
unchanged, including the following absolute forward/back-max moves and the
final back-six-inch command. There are still 15 movement commands.

Updated `eject_sequence.gcode` and `bed_swap.gcode`, and created a new immutable
active revision `edc4cddb3ca642539bf5d009dd8a6c0e`, SHA-256
`f117d4845b31d038b5800740803ce87a93f08d35d95ce4140a56ff6ba7f0f330`.
The old `ec6cba66...` file and its historical verification remain untouched.
The new revision is `configured`: operator requested and syntax checked, not
physically verified. Bridge/API permit configured revisions for direct control
and batch preparation without inventing a motion-test result. UI calls the
selected revision active. Trial syntax checks and immutable hash checks remain.

New batches embed the revision in each intermediate print file; existing
prepared files retain their earlier movements and must be prepared again.
Twenty queue tests and eight API tests passed, including configured revisions.
A local real-file preview contains exactly the active block and a valid plate
MD5, with no added homing and all three Y-3 back-max commands retained. Evidence:
`spool/diagnostics/embedded-swap-review-20260918/revision-6.35-inches.json` and
`revised-6.35-preview.gcode.3mf`. No printer motion was sent. Simon restarted.

### 2026-09-18: controlled motion after repeated overshoot and excessive speed

The operator reported excessive backward travel and a sudden increase in speed
again. The latest batch was `d2c9201fa2ac4f80b5c4d72d5193994e`; its first job
`a63c63a15d5b` finished, and the operator canceled the second. Read-only FTPS
download of `autoswap_9f2988fd_a63c63a15d5b.gcode.3mf` matched the local prepared
archive byte for byte (67,070 bytes; SHA-256
`673ecfad84cc9285ada2e7477d7892cd66fcbff86a11c5baa084951f40993370`).
It contains the requested `G1 Y-161.29 F3000`, so the printer received the
6.35-inch revision. This does not prove which motions firmware executed.

The last explicit acceleration before the swap is `M204 S10000`. Every swap
move specifies its own feedrate, but there was no acceleration reset or wait
between individual moves. Native unload/wipe/park commands occur before the
block; no homing, XYZ coordinate reset, wipe, or AMS command occurs inside it.
There is no useful live XYZ trace. Inherited acceleration and queued motion
are plausible contributors to abrupt movement, not a confirmed explanation
of the physical overshoot.

Activated immutable configured revision `48519c5ba3074d1384dfa0660e8aac16`,
SHA-256 `7ca8aed162b24515b9968cc65af4186a460814f0bd689b13faf141c6837c8104`:

- Preserve all 15 intended endpoints from the prior 6.35-inch revision.
- Use G90 and an absolute destination for every move; no G91 or G92.
- Before every move, set M220 S100, M201.2 K1.0, and M204 S500.
- Set XY feedrate to F1500 (25 mm/s), Z to F600 (10 mm/s).
- Wait for completion and pause 250 ms after each move using M400 P250.
- Move 5 is Y260 to Y98.71; all three Y-3 back-max anchors remain, and the
  final backward move remains six inches, Y260 to Y107.6.

Updated `eject_sequence.gcode` and `bed_swap.gcode` to match. Earlier immutable
trials, verification history, and prepared files remain unchanged. Newly
prepared intermediate prints embed this exact active file inside the existing
software-limit wrapper, before final motor release. The final print remains
unchanged. Direct swap uses the same active movement file. No homing was added.

The validator now permits a narrow, explicit set of motion controls (M400,
M400 P250/P500, M204 S500, M220 S100, M201.2 K1.0). It continues to reject
homing, heat, extrusion, offsets, limit changes, and arbitrary M-codes in the
editable movement file. Cold print tests default to acceleration 500; the UI
labels distinguish the active sequence's saved settings from extra observation
pauses. Active state is `configured`, not a fabricated physical verification.

Verification: 42 AutoSwap tests and nine API integration tests passed. A preview
made from the actual failed run's original sliced source has exactly one active
block, valid plate MD5, identical intended endpoints, unchanged surrounding
G-code and unchanged other archive members. Evidence, downloaded file, endpoint
trace, preview, and activation report are under
`autoswap_rip/spool/diagnostics/swap-speed-20260918/`.
No upload, print, homing, motion, or motor-release command was sent. The physical
behavior still needs observation in a newly prepared batch; old prepared files
retain their prior motion settings.

### 2026-09-18: restore verified master, enforce identical blocks, repair cold file start

The operator explicitly selected **Earlier verified test: 8.35-inch backward
move** as the master for regular tests and real prints. Restored the exact
normalized commands from verified trial `ec6cba66a9cc49e6bef1054555234b3b`:
SHA-256 `33267e5f2c5a981e9154e34c3088ccd7fc867377474ac71ddaa19a04ccb644e3`.
Move 5 is precisely 212.2 mm (8.3543 inches), not a freshly rounded distance.
All 15 G0/G1 commands, G90/G91 modes (including redundant ones), and feedrates
are unchanged. The prior 6.35-inch/slow absolute revision is no longer active.

New annotated immutable active trial: `d371787cd75e4937a92d65d08a0d6b24`, state
`configured`. It records selection of the verified commands without inventing
a new physical test of the execution wrapper. Previous trials remain intact.
`eject_sequence.gcode` and `bed_swap.gcode` match the restored commands.

`swaptool.annotate_swap` describes each individual move, including direction,
distance, destination when known, and feedrate. Comments do not affect the
canonical command hash. Trial saving and the API activation check use this same
command identity. Comments appear in the editor, active file, real print block,
and cold print file. The regular LAN payload also includes them.

There is now one `swaptool.swap_block` used by regular LAN swaps, saved swap
tests, exact cold tests, and real-print preparation. It includes the existing
M400 / M211 save-disable-restore wrapper, now also used by direct tests. It adds
no homing or axis movements. Block SHA-256:
`e8a1aa7bd0615a762272ce3ff1e25a89cce55d870a7c4ac6b669600eee1b92ce`.
The wrapper changes software-limit state consistently across paths; historical
physical verification of the movements alone does not prove identical physical
behavior under every inherited firmware state.

Prepared real files are checked for exactly one complete shared block and the
selected command hash. The runner checks the full archive hash, compares the
embedded command hash to the batch and active master, and rejects stale
sequences before uploading. The API also rejects batches whose master changed.
FTPS verification now downloads and hashes the actual uploaded bytes; matching
file size alone is insufficient. No print-start request follows a content
mismatch. A complete matching download tolerates missing TLS close_notify.

Print-mode tests default to **Active sequence / Exact commands and speeds**,
snapshotting the same database-selected master as print batches. **Editor
draft** and **Slower with pauses** are explicit alternatives. The slow test's
recorded hash now describes its actual modified commands. Saved regular tests
show whether their commands match the active master. A button reloads the
annotated active master into the editor, and active changes refresh an
unmodified editor without overwriting unsaved edits.

Cold-print builder changes: preserve the known printable template's full
comment-only header, configuration, and archive metadata together; replace
only executable G-code and its MD5. Removed zero-time/zero-height/zero-material
header rewriting and synthetic layer markers. Exact mode adds no speed or
acceleration overrides. No source print startup, homing, extrusion, AMS, or
heating commands are executed. Template estimates can still appear on the
printer. Research reference for a motion-only file retaining a sliced header:
https://github.com/unrelatedlabs/bambu-cuts (README, 3MF benchmark; observed on A1).
This was investigated as a format difference, not assumed to be the sole cause
of every prior failure.

Hardware verification: sent one **motion-free diagnostic** after checking idle
state and queue exclusion. Its executable body contained only M73 progress,
M73 C0, M400 S12, and final M73 progress. No swap, axis motion, homing, motor
control, heating, extrusion, AMS, or calibration command was sent. Uploaded file
`autoswap_d2702371_no-motion-format-probe.gcode.3mf` was read back with matching
SHA-256 `f70cd2619f506c327446c252176009bf23677638b1b731b305f16d621d5cc15f`.
The A1 reported RUNNING for `autoswap_probe_96969628`, then FINISH at 100%.
Its pre-existing print_error value remained unchanged. This confirms file-start
acceptance and completion on this A1; the complete physical swap remains for
operator observation.

Verification: 45 AutoSwap tests, ten API integration tests, and two browser
tests passed. Parity tests capture the actual LAN payload and compare it byte
for byte with the cold and real-print blocks. Tests cover annotation identity,
stale master rejection, corrupt uploaded bytes of equal length, active-master
selection despite a stale editor, and no added homing. A preview from the real
retained sliced source preserves all surrounding executable G-code and other
archive members. Evidence is in
`autoswap_rip/spool/diagnostics/swap-parity-20260918/`, including both print
previews, the regular payload, hardware probe, and parity/activation report.
Prepare fresh batches; historical prepared files are unchanged.

### 2026-09-18: promote operator-confirmed slow print test and enforce shutdown boundary

The operator reported that the most recent swap-as-print worked perfectly and
requested that real swaps use that slow, controlled execution. The identified
test is job `fad83f946f44`, batch `c68f1c7a70dd45cbbeba4790ae82e915`, selected
with `source=active` and `motion_profile=slow`. It reported RUNNING and FINISH
at 100% on 2026-09-18 at 14:42:52 UTC. Its prepared/uploaded archive SHA-256 is
`64997bb6b684053e65ba8e0d105e493ebbb46e1914715fa8cfeef3b74883f8cc`.
The upload log records a matching hash from a download of the printer's actual
39,700-byte file before print start.

The executed test used F1500 XY (25 mm/s), F600 Z (10 mm/s), M204 S500
acceleration, and M400 P500 after each of the 15 moves. Its normalized inner
block hash was `7b352be46cb65e17a349847f03191cc36d241aa4936c4f26d7a3100df2674298`,
but M204 S500 was outside that block. Promoted every exact move/mode/feedrate/
pause and included that tested acceleration setting after the initial G90 in
the immutable sequence. This prevents real prints from inheriting M204 S10000
instead. Apart from relocating this setting into the sequence, the extracted
commands are identical to the successful test. All comments remain available.

New active trial: `f95169f3483543f8ba3299bb1d1c12e0`; command SHA-256
`024181fae008384f026cb982e0698004027a740ace607cc93af3f44609e706b5`.
State is `verified`, based on the operator's explicit physical success report
for the identified cold print test. The note distinguishes this from software
verification of the real-print transition; no new real print was run.
Updated `eject_sequence.gcode` and `bed_swap.gcode` to the same annotated
profile. Previous immutable trials and prepared jobs are unchanged.

The shared slow-profile builder now keeps acceleration inside the sequence and
does not duplicate M400 P500 waits when applied to an already-slow sequence.
For this active profile, exact/current and slow cold-test generation produce
identical files. Regular LAN controls and real embedded swaps use the same
complete block, whose SHA-256 is
`83fb000726c083e58224009be9950b9637c8216e15b54a53f03dda21d156937b`.

Strengthened print-boundary validation in `workflow.py`:

- Native unload, wipe, park, and finish-sound commands stay before insertion.
- AMS M620 S.../M621 S... and conditional M622/M623 blocks must be balanced
  and properly nested before the swap. M620 remap controls and dotted M620.x
  parameter commands are not mistaken for branches.
- A pending M1006 melody must be closed by M1006 W before the swap.
- Existing M400 barriers bracket the complete shared block. The first waits
  for prior motion, and the final barrier precedes restoring limits/release.
- After the block, only the final motor release and M73 progress commands are
  accepted. Later homing, AMS/tool changes, wipe/motion macros, acceleration
  overrides, coordinate resets, and other commands cause preparation failure.
  Executable commands after EXECUTABLE_BLOCK_END are also rejected.
- These checks run both during preparation and when the runner extracts the
  embedded sequence before upload. The application does not silently reorder
  unknown routines or send a second swap after FINISH.

Audited the retained original sliced print: all 209 AMS/conditional blocks
close; its unload/wipe ends with M621 S255, its finish sound ends with M1006 W,
and its final native M400 precedes insertion. Its nozzle-clog-disable command
already occurs at the start of native end G-code. The official A1 end profile
matches this native ordering:
https://github.com/bambulab/BambuStudio/blob/master/resources/profiles/BBL/machine/Bambu%20Lab%20A1%200.4%20nozzle%20template%20machine_end_gcode.json
This establishes command ordering and barriers, not an unconditional guarantee
against undocumented firmware behavior or unrelated external control clients.

Verification: 50 AutoSwap tests and ten API integration tests passed. A real
print preview from the actual retained source preserves all original commands
around one inserted block and all other archive members. Direct, exact cold,
and real-print blocks are byte-identical. Compared each move's command,
effective acceleration, and following wait to the successful test file; all
15 match. Added rejection tests for unfinished/misnested firmware routines,
late commands, and missing barriers. Evidence and activation provenance are
under `autoswap_rip/spool/diagnostics/swap-promote-slow-20260918/`.
No printer command was sent during this promotion. New batches use the active
verified slow profile; previously prepared batches must be prepared again.
