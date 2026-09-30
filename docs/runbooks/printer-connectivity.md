# Printer connectivity investigation — September 28, 2026

## Findings

The intermittent historical failures are real, but were not reproduced during
this investigation. The evidence does not identify a router, printer radio,
firmware, or PC network driver as their cause.

Simon uses direct LAN MQTT on port 8883 and implicit FTPS on port 990 at
192.168.1.23. Bambu Studio was not running during the tests. Its most recent
logs were September 23 and its main/network logs were encrypted, so its active
LAN versus cloud route could not be compared. Bambu documents separate cloud
and local communication paths in its
[security white paper](https://cdn1.bambulab.com/trust-center/file/bambulab-security-whitepaper-en.pdf).
A stable Studio connection alone does not establish that Simon's LAN path is stable.

## Live read-only measurements

- PC: Killer E2500 Ethernet, link up at 1 Gbps. No wireless interface present.
  Earlier notes about this PC using a USB Wi-Fi adapter are not current evidence.
- Printer ping: 12/12 replies, 0% loss, 1–26 ms, average 4 ms.
- Three TCP connections per port: all succeeded on 8883 and 990, approximately
  15–19 ms for the first two and 1.04 seconds for the third.
- Three calls to the actual dashboard status reader: all returned IDLE in
  0.981, 1.074, and 0.982 seconds.
- One persistent MQTT session for 60 seconds, requesting a full report every
  ten seconds: 24 status reports, six containing gcode_state, no unexpected
  disconnects. Printer reported Wi-Fi signal of -45 to -46 dBm.
- Downloaded an existing 873,174-byte print file without changing it:
  FTPS connection 0.793 seconds, login complete by 0.830 seconds, download
  5.410 seconds (161.4 decimal KB/s). This measures download, not upload speed.
- Queue database contained two completed batches and no running batch.

These small samples cannot rule out intermittent failures or transfer problems
under sustained upload load. No upload, print, motion, or heating command was sent.

## Confirmed implementation contributors

1. `autoswap_rip/workflow.py:upload` downloads the entire uploaded file again
   to verify its SHA-256 before printing. The September 23 job transferred
   10,521,036 bytes in 70.571 seconds including setup; verification added
   55.915 seconds. Total preflight file transfer/verification was 126.487
   seconds. This is a deliberate integrity check and directly adds delay.
   It should not be removed without preserving safe verification of print contents.
2. `PrinterStatusService._read` creates a new MQTT/TLS connection for every
   uncached read, waits eight seconds, then disconnects. Its cache lasts eight
   seconds. Any exception becomes the same generic LAN-connection message;
   the dashboard therefore cannot distinguish timeout, authentication failure,
   transport loss, or malformed telemetry. The three live reads succeeded,
   so connection churn is a design weakness, not a proven cause of these outages.
3. The batch runner has a persistent MQTT client and reconnect handling already.
   During a live batch the dashboard normally reads its SQLite telemetry rather
   than opening another connection. API and workflow-worker status caches are
   process-local, so they do not coordinate idle-printer reads across processes.

## Historical failure evidence

`.local/logs/autoswap-batches.log` contains 25 printer disconnect entries over
September 18–24: 21 generic errors and four keepalive timeouts. Two pairs of
keepalive entries have near-identical timestamps, so entries should not be
treated as 25 independent network outages.

On September 22, a 562,452-byte upload was reset after sending only 65,536
bytes (Windows error 10054). The recovery log records a separate 19,625,907-byte
transfer reset after 2,621,440 bytes on September 23 UTC, after prolonged slow
progress. A remote reset identifies the failure mode, not the component that
caused it. Healthy present-day pings do not explain those historical failures.

## Next discriminating measurements

Compare Studio and Simon simultaneously while the symptom is occurring:
record Studio's actual remote endpoints, MQTT connect/disconnect and report
timestamps, TCP retransmissions/resets, and transfer timing. Compare equivalent
file sizes and account for Simon's full download verification. If Studio uses
cloud transport, repeat the comparison using an explicitly selected direct LAN
path before drawing conclusions about different client implementations.

Useful software follow-up is structured status-failure logging and a coordinated
persistent telemetry owner shared by dashboard/worker/runner. This investigation
does not claim those changes would cure the historical FTPS resets.
