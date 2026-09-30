# Printer batch startup investigation — 2026-09-17

## Latest incident

The operator reported that a batch froze before its first print. The newest
entry in `.local/logs/autoswap-batches.log` identifies job `1ebcb3e8e4f3` in
batch `90243090abc2416e965fdd0756ded9ff` and ends with:

```text
queue stopped on 1ebcb3e8e4f3: printer MQTT connection lost: Unspecified error
```

The log file was last modified at 20:39:52 local time. Its older entries also
contain unrelated upload failures and an earlier canceled print; those are
not the latest incident.

The batch/job rows had already been deleted when inspected. The old log did
not record the upload-verification or print-start acknowledgment phases, so
it cannot establish whether the latest start command was accepted before the
disconnect. No queue runner process remained active.

A read-only FTPS listing found the corresponding file on the printer:
`autoswap_18f6d416_1ebcb3e8e4f3.gcode.3mf`, 67,070 bytes. This demonstrates
that the upload reached storage; it does not establish execution of the file.

A fresh read-only status request succeeded and reported IDLE, no current
job name, zero progress, heaters off, and no HMS alerts. The selected report
is saved in `autoswap_rip/spool/diagnostics/batch-start-20260917/current-status.json`.
The reported wireless signal was -46 dBm at that later observation; this
does not diagnose the earlier disconnect or measure network throughput.

## Concrete code issue found and corrected

While awaiting `project_file` acceptance, `Printer.command()` requested a
full status update after every unrelated event. A response describing the
previous idle print could therefore trigger another request immediately,
creating a feedback loop while the new print was preparing. Recovery had a
similar unbounded request path. This was a real polling defect, but the
retained incident evidence does not prove it caused this particular disconnect.

Updated the shared `Printer.refresh()` method to allow at most one request
every five seconds. The check is protected by a lock because both the network
callback and the runner can request status. Polling also checks connection
state. `wait_status()` continues requesting when due so a suppressed immediate
request cannot leave it waiting indefinitely on an idle printer.

The runner now writes timestamped events for:

- Upload start, transfer completion, and verified remote size.
- The transition to the printer-readiness check after uploading.
- Start-command publish attempt and transport acknowledgment.
- Printer acknowledgment or fresh matching print activity.
- Print state/progress changes and the job-stop reason.
- Disconnect reason and selected last-observed state, stage, progress and
  G-code line number.

Diagnostics go to stderr, preserving structured command output on stdout.
Only selected status fields are logged; printer credentials and full raw
status payloads are excluded. Existing batch logs capture stderr and stdout.

The application still sends the print-start command once and stops the queue
on an uncertain disconnect. This change does not restart the printer, resend
an uncertain job, or change any swap movement/limit commands. The next runner
process uses the updated code without a Simon server restart.

## Verification

All 33 `test_printer_startup`, `test_workflow`, and `test_swaptool` tests passed.
New regressions cover a burst of 100 unrelated reports, continued polling at
bounded intervals, acceptance without a transport acknowledgment, an idle
status wait during the polling cooldown, and disconnect logging without
another application-level start command.

A subsequent read-only check stayed connected for 13.3 seconds and received
three status reports, ending in IDLE. No print-start or movement command was
sent. A physical print is still needed to establish whether this change
resolves the observed freeze.
