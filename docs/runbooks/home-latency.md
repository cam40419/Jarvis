# Home command latency investigation

September 16, 2026: investigated phone chat/device delays after the domain deployment.

## Observations

- Local health requests took 1–2 ms; public-domain health requests took 184–719 ms
  across four samples. These are samples from the home computer, not phone cellular measurements.
- A recent completed command took 31 seconds. A subsequent status answer took 46 seconds,
  used four individual status tool calls, and spent 8.4 seconds on model input-token counting.
- Direct Shelly reads were intermittent. Four pings to the Back Lamp lost one packet;
  successful replies took 104–994 ms. The host uses a Realtek USB Wi-Fi adapter on 2.4 GHz.
  Strong reported signal does not rule out packet loss or contention.
- The Air Purifier's saved address (`192.168.1.32`) was unreachable. LIFX reported the Beam
  offline on one check and online on later checks. A successful discovery does not establish
  that all previously saved devices are reachable.

## Changes

- `home_get_statuses` checks up to 32 authorized device IDs with four concurrent workers,
  preserving per-device errors. The prompt prefers one batch over repeated model round trips.
- `home_list_devices` reads saved inventory without blocking on discovery. Automatic startup/
  five-minute discovery and explicit refresh remain available.
- Simple home requests select Quick automatically. Voice routing uses the latest transcript;
  the full context wrapper remains in the model input, idempotency, and memory evidence.
- Each provider adapter uses a bounded HTTP client. Connection and response timeouts are eight
  seconds, with one retry for read-only LAN connection failures. An initial two-second LAN
  connection limit was reverted after the controlled restart investigation below.
  Device writes are never automatically retried. Identity validation and live
  preflight/readback remain required.
- Tuya device specifications are cached for five minutes (128-device bound). Actual status is
  fetched fresh. Home now displays unknown connectivity accurately instead of falsely marking
  Tuya offline and disabling its controls.
- Authentication, connection, timeout, and rate-limit errors have distinct messages.

The live read-only model probe selected inventory plus one status batch; six device reads took
9.54 seconds and the complete model answer took 27.56 seconds. This probe used a shorter context
than the earlier production requests, so it is not a controlled speedup measurement. Network
delays and provider/model latency still remain. No physical device commands were issued by
the investigation.

Regression verification: 499 tests passed with PostgreSQL/browser checks, five optional paid
tests skipped. After the final voice-routing and timeout adjustments, 47 voice/adapter tests
passed. Ruff and strict mypy passed. The separate live probe used the configured model and
read-only tools. Simon was restarted to serve these changes through the existing domain route.

## Remaining checks

Reload Simon on the phone to obtain the updated Home script. Recheck a named device and a
multi-device status request. Compare timestamps with `runs.snapshot.total_ms`,
`token_count_ms`, and `tool_calls`; do not infer device availability from discovery alone.

Check the Air Purifier plug's power and current Wi-Fi address in Shelly, then refresh devices
if its address changed. Compare vendor-app behavior while Simon reports an error. Ethernet
is planned for the future home server; current operation remains supported on Wi-Fi.

Runtime logs after the latest restart: `.local/server-restart.stdout.log` and
`.local/server-restart.stderr.log`; tunnel logs use `.local/tunnel-restart.*.log`.
The running launcher PIDs are in `.local/server.pid` and `.local/tunnel.pid`.
The Cloudflare tunnel configuration did not change during this investigation.

Reducing tool round trips follows [OpenAI's latency guidance](https://developers.openai.com/api/docs/guides/latency-optimization).

## Controlled restart follow-up (September 16)

Stopped the Uvicorn server and Cloudflare tunnel, restarted PostgreSQL without deleting its
volume, then tested with Simon stopped, with discovery/metering disabled, and with normal
discovery/metering and the tunnel restored. Simon is left running normally on this PC.

Confirmed software contributor: the recently shortened two-second LAN connect timeout was
too aggressive. With Simon and the tunnel stopped, successful TCP connections to `.30` and
`.31` took 4.385 and 3.626 seconds. Restored the original eight-second connect budget and
updated the adapter regression test. The longer limit tolerates delayed connections; it
does not make the network faster. An unreachable device can take roughly 16 seconds to
fail a read because there is one connection retry.

| Condition | `.30` direct status | `.31` direct status | `.33` direct status | `.32` Air Purifier |
| --- | --- | --- | --- | --- |
| Simon/tunnel stopped | 3.979 s | 1.139 s | 2.056 s | Connect timeout |
| Simon running, background home jobs disabled | 7.056 s | 6.360 s | 2.231 s | Connect timeout |
| Normal server and tunnel restored | 6.083 s | 3.922 s | 1.754 s | Connect timeout |

These are individual samples from the same direct HTTP probe, not statistically controlled
performance estimates. They establish that slow local responses persist without Simon or
Vercel in the request path; they do not identify the faulty radio, access point, or firmware.
With Simon stopped, four pings to `.31` lost three packets, while four router pings succeeded
in 23-66 ms. Strong RSSI alone does not explain or exclude these symptoms.

Fresh mDNS browsing still advertised all four plugs at their saved IPs. The Air Purifier's
neighbor entry later became reachable with the expected MAC, but TCP port 80 still timed
out. It is therefore more precise to say its local API is unreachable than to declare the
whole plug offline or assume its address changed. Vendor-app status is still needed.

After restoration, background metering successfully read the three other plugs. A separate
adapter check still had an intermittent response timeout from `.30`; the issue is not fully
resolved. LIFX Beam and both Tuya reads succeeded. Sixteen alternating pooled/unpooled RPCs
to `.30` and `.31` all succeeded, with the devices returning `Connection: close` in both modes;
this did not reproduce a stale pooled-connection failure.

Post-restart public health returned HTTP 200 in 0.633 and 0.803 seconds; local health took
0.024 and 0.001 seconds. The tunnel established four connections. No outlet power states
were changed. Ignored `.local/connectivity-{stopped,isolated,restored}.json`,
`.local/restart-adapter-readings.json`, and `.local/keepalive-comparison.json` retain the
sanitized observations. The initial short-timeout baseline is not directly comparable to
the later eight-second HTTP probes.
