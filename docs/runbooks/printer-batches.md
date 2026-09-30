# A1 print batches

## Start a sequence

On Automations, prepare up to eight sliced print steps. Repeating a file or an
entire sequence creates independent jobs. Choose **Continue automatically** under
**Between prints**, check that the initial plate and supplies are ready, and click
**Start batch**. **Wait for a plate check** retains manual handoff.

Every intermediate file contains the active, annotated swap before final motor
release. Native shutdown routines finish before the swap. The last file has no
swap. Starting the next print uses its normal sliced startup, including homing;
there is no homing inside the preceding swap.

## Completion and recovery

- A print advances only after a fresh FINISH report identifies its unique job,
  and PREPARE/RUNNING/PAUSE has previously been observed and saved. Percent or
  estimated remaining time cannot complete a job.
- The next upload/start requires an idle, alert-free printer still identifying
  the previous completed job. Prepared files and embedded swap hashes are checked.
- Every upload is downloaded again and its bytes hashed before print start.
  Automatic batches retry upload failures up to three times. No start command
  was sent for those attempts.
  The FTPS connection timeout is 30 seconds and the data socket timeout is
  120 seconds per stalled operation, with 64 KiB transfer blocks. Data streams
  close without waiting for the A1's missing TLS close-notify response.
  Upload and readback progress appear in MB on the job, with continuing runner
  heartbeats. Verification retries have a size-dependent budget of at least
  ten minutes. Slow progress alone is not a print failure.
- The unique remote file name and dispatch intent are committed before sending
  a start. A lost acknowledgment triggers observation, never a second start.
- Temporary MQTT loss holds the queue on the current job while the client
  reconnects. There is no maximum running-print duration. Pauses hold the queue.
- `Simon-Printer-Recovery` runs every minute and at sign-in. It uses the same OS
  lock as the runner and direct printer controls. A surviving runner is left
  alone; an exited runner is replaced using saved dispatch/observation state.
  Staged, canceled, manual-check, and needs-attention batches are never started
  by this task. An explicit CLI stop request is respected.
- Unknown dispatch outcomes, changed job identity, changed files, and printer
  failures stop advancement for inspection. Old jobs without durable dispatch
  metadata cannot be automatically restarted.

Install recovery with `scripts/install-printer-recovery.ps1`. The task has no
execution time limit and uses the existing user-session account. Keep this PC
powered and signed in. A reboot requires sign-in before monitoring can resume.
The active runner holds a Windows execution request to prevent automatic sleep.

Logs: `.local/logs/autoswap-batches.log` for API-launched runners and
`.local/logs/autoswap-recovery.log` for recovered runners. Queue state lives in
the sibling `autoswap_rip/queue.sqlite3` database.

## Saved physical reference — 2026-09-18

Sibling folder:
`autoswap_rip/references/2026-09-18-validated-slow-batch-8732259f/`.

It contains original and prepared print files, the annotated swap, the successful
cold print test, checksums, implementation snapshots, and actual FINISH reports
for both real prints. It survives deletion of the original batch in the UI.

Active command SHA-256:
`024181fae008384f026cb982e0698004027a740ace607cc93af3f44609e706b5`.

The sequence retains all 15 validated moves, including the 212.2 mm backward
move and final 152.4 mm backward move. XY feed is F1500, Z feed is F600, acceleration
is M204 S500, and every move has M400 P500. Queue reliability changes do not alter
these moves.

Software fault-injection tests cover two consecutive simulated 14-hour prints,
connection outages, lost acknowledgments, crash recovery, stale FINISH reports,
manual handoff, cancellation during upload, failure/alert stops, and duplicate
runner exclusion. These are simulated duration tests, not a physical weekend
soak test. Printer completion reports do not detect mechanical plate seating;
the fixture, supply capacity, and physical swap remain separate constraints.
