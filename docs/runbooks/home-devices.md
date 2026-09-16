# Home devices

The first integration supports the office LIFX Beam, two office Smart Life hexagon sets, and
planned Shelly Plug US Gen4 outlets for the bedroom lamp, living-room lamp, and office air purifier.
LIFX and Tuya devices are discovered automatically from the credentials in `.env`. You do not
need an inventory file or individual cloud device IDs. On September 15, live read-only checks
found the Beam and the two Tuya controllers, **Up Arrow** and **Down Arrow**. Both hexagon
controllers report the lighting category `dd`. The adapter now supports standard whole-fixture
color and brightness in color mode. Live actuation has not been tested. After restoring the local Tuya credentials, registration
was verified again: both controllers are present in Office with color control advertised.

## Automatic setup

1. Add the required provider credentials below to `.env`, then restart Simon. If Smart Life is
   already linked to the Tuya project, no further linking or per-device setup is needed.
2. Simon imports devices in the background on startup, then refreshes every five minutes.
   **Connections > Home devices > Refresh devices** or "Refresh my home devices" requests a
   refresh immediately. Duplicate requests have a short 15-second cooldown. Listing devices in
   chat also refreshes stale discovery. Background discovery can be disabled with
   `SIMON_HOME_AUTO_DISCOVERY=false`; explicit chat/UI refresh remains available.
3. Ask "Put Beam, Up Arrow and Down Arrow in the Office room" or "Add both arrows to a group
   called Accent lights". Simon saves the organization immediately without changing lights.
   You can also edit **Room** and **Groups** directly under each device in Connections.
4. Ask for power, brightness, or color changes. Simon executes them immediately and reports
   the result for each device. Discovered LIFX lights and recognized Tuya lighting categories
   are enabled for supported controls. Other discovered types are listed read-only.

Rooms and groups belong to Simon and can span brands. They do not rename rooms/groups in the
vendor apps. Each device has one room and multiple groups. User assignments override provider
defaults on future refreshes; LIFX's group name seeds the initial room and group. Tuya devices
initially have no Simon room. Blank room or an empty group list clears that assignment.

The normal launcher uses PostgreSQL, which preserves inventory and organization across restarts.
`-Memory` mode is temporary and loses saved organization when stopped. Migration 0008 adds the
two inventory/sync tables. Migration 0009 adds durable device command receipts; the launcher
applies both. Provider errors preserve previous
inventory and appear in Connections; a successful complete discovery marks missing devices
unavailable for control. No partial page result is committed, and discovery sends no commands.

The standard local development household is selected automatically. A deployment with another
household must set `SIMON_HOME_HOUSEHOLD_ID` to that household's ID (visible at `/auth/session`).
Production discovery has no implicit household. Environment credentials are assigned only to this
household, never to whichever user signs in first. Owners and members can organize devices;
guests cannot access them.

Keep credentials in `.env`, not the inventory, chat, screenshots, or Git. The initial adapter uses
one LIFX account token, one Tuya project, and one shared Shelly admin password per Simon server.
Imported inventory is household-scoped. Provider IDs/IPs, Tuya local keys, and credentials are
excluded from the model's device listing. Account credential changes trigger fresh discovery
and invalidate pending control previews; stale account records are hidden.

## LIFX Beam

Create a personal access token through the [LIFX account settings](https://cloud.lifx.com/settings)
and store it as `SIMON_LIFX_TOKEN` in `.env`. LIFX documents bearer authentication in its
[authentication reference](https://api.developer.lifx.com/reference/authentication).

Simon calls **GET** `https://api.lifx.com/v1/lights/all` itself, imports the lights and their
names/group defaults, and subsequently controls only the exact selected light ID.
See [List Lights](https://api.developer.lifx.com/reference/list-lights).

The adapter sends whole-fixture power, brightness, and color changes through
[Set State](https://api.developer.lifx.com/reference/set-state), with `fast=false`, checks the
individual device result, and reads status afterward. [Color](https://api.developer.lifx.com/reference/colors)
is sent as hue/saturation to preserve brightness unless a new intensity is requested. Segment colors, animation, transitions,
and effects are not exposed in this slice. The API requires internet access and a cloud-connected
Beam; offline lights cannot be controlled.

## Smart Life / Tuya hexagons

Create a Smart Home cloud project in the Tuya Developer Platform and link your existing Smart Life
account using **Devices > Link App Account** and its QR authorization flow. Choose a data center
that serves the app account and authorize the project APIs required for device specification,
status, and commands. Follow Tuya's [cloud project/account linking guide](https://developer.tuya.com/en/docs/developer/apply-cloud-api-key?id=Kff30z8sv62ah).

Add these values to the private `.env`:

```dotenv
SIMON_TUYA_CLIENT_ID=your-project-access-id
SIMON_TUYA_CLIENT_SECRET=your-project-access-secret
SIMON_TUYA_REGION=us
```

Use the **project's** data center, not your time zone: `us` (also accepted as `us-west`) is Western America and `us-east` is
Eastern America. Other supported settings are `eu` (Central Europe), `eu-west` (Western Europe),
`cn` (China), and `in` (India). See Tuya's [endpoint table](https://developer.tuya.com/en/docs/iot/api-request?id=Ka4a8uuo1j4t4).
Check the project's current IoT Core entitlement, quotas, and expiration in the console; do not
assume the initial account setup provides permanent API access.

Simon pages through the [linked app accounts' device list](https://developer.tuya.com/en/docs/cloud/fc19523d18?id=Kakr4p8nq5xsc)
at `/v1.0/iot-01/associated-users/devices`, including all linked controllers without needing a
manually supplied user or device ID. Requests use sorted, encoded query strings when signing.
Pagination is bounded to ten pages of 100 devices. Each controller appears as one Simon device
regardless of its number of panels. Start with **Check status** for each set. The adapter queries the
[device specification and status APIs](https://developer.tuya.com/en/docs/cloud/device-connection-service?id=Kb0b8geg6o761)
and only uses recognized lighting instructions. It does not guess datapoint numbers.

Power requires `switch_led` with Boolean type. Brightness requires `bright_value_v2` or
`bright_value` with an advertised integer range. Simon maps 1-100% into that device's min/max/step.
White-mode brightness uses the advertised integer range. In color mode, brightness updates
the HSV value while preserving hue/saturation. Color requests use advertised standard JSON
`colour_data` or `colour_data_v2`, switch to `colour` mode when necessary, and preserve current
brightness unless specified. If current intensity is unavailable (as with the hexagons in white
mode), Simon reuses the saved color intensity. Legacy `dj` uses 0?255 saturation/value; v2 and supported modern
lighting categories use 0?1000. Explicit advertised HSV bounds take precedence. Unknown layouts
are rejected; when neither current nor saved color intensity is available, specify brightness. Scenes and
individual panel control remain future work.
If a controller exposes proprietary instructions, status may work while controls are unavailable.
See the [lighting instruction set](https://developer.tuya.com/en/docs/iot/dj?id=K9i5ql3v98hn3)
and [command API](https://developer.tuya.com/en/docs/cloud/e2512fb901?id=Kag2yag3tiqn5).

Tuya uses signed requests and a cached access token. Simon does not retry commands on token or
network errors. Tuya status is cloud-reported and can lag the physical fixture; an accepted command
alone is not proof the light changed.

If Tuya reports a suspended data center (code `28841107`), enable the configured data center in
the project console or correct the region to the project's enabled center, then restart Simon
and refresh. The local account was verified on Western America (`us-west`/`us`).

## Shelly Plug US Gen4

Simon discovers Plug US Gen4 devices automatically on the local Wi-Fi/Ethernet network using
[mDNS](https://shelly-api-docs.shelly.cloud/gen2/General/mDNS/). It browses `_shelly._tcp` and
`_http._tcp`, accepts private IPv4 addresses on port 80, and verifies each device's exact ID,
model `S4PL-00116US`, and generation 4 through `Shelly.GetDeviceInfo`. It does not sweep subnets.
A changed DHCP address updates the saved destination; a missed broadcast does not delete a plug.
Simon keeps checking identity before every command and meter read. LAN discovery can be disabled
without hiding already registered plugs. Existing manual Shelly entries remain supported and are
deduplicated against discovery by device ID.

**The password is the plug's local device authentication password.** Open the plug's IP address
in a browser on the same LAN, find **Authentication** in its device settings, and set a password.
The username is always `admin`. Put that password in `SIMON_SHELLY_PASSWORD` in the private `.env`.
It is separate from the Wi-Fi/AP password and your Shelly cloud login. If local authentication is
disabled, leave the setting blank. Currently all plugs use this shared password; per-device
credentials are future work. See the [authentication reference](https://shelly-api-docs.shelly.cloud/gen2/General/Authentication/).
The authenticated `GET /v1/home/outlets` endpoint lists discovered IP addresses and hardware IDs.

The plug must be on Wi-Fi with local HTTP available; Zigbee-only operation cannot use this API.
Multicast must reach the Simon machine (normally the same subnet). If discovery finds nothing,
check Windows firewall permissions for Python/mDNS UDP 5353 and whether Wi-Fi client isolation,
a VPN, or a container network blocks multicast. A manual address remains an alternative through
the optional legacy inventory file. No public port forwarding is required.

### Chat naming, setup, and control

Ask Simon to list devices, then give any LIFX, Tuya, or Shelly device a name:

- "Rename Up Arrow to Office ceiling lights."
- "Name the plug ending 6789ab Reading lamp."
- "The plug ending 6789ab powers a lamp in my bedroom. Name it Bedroom lamp and enable control."
- "Turn off Bedroom lamp."
- "Move Bedroom lamp to the Bedroom room and add it to Evening lights."

Use the actual suffix shown by Simon or the Shelly app, not the example suffix above.
Simon saves names immediately and preserves them through refreshes and restarts. These are
Simon names; vendor app names are unchanged. Room/group organization remains available in chat.
If multiple devices match, Simon asks which one you mean. Naming alone never enables control
or changes the connected load. Setup can enable lamp or air-purifier control after the owner
identifies the load. It does not switch power unless you also request a power change.
These tools do not create confirmation cards. "Think deeper" cannot repeat naming, setup,
or switching. Device names also appear in inventory, power responses, and new command receipts.

### Outlet controls in the UI

Open **Connections → Home devices**. Each discovered Shelly outlet shows its ID suffix and,
for household owners, editable **Device name**, **Outlet room**, and **Powers** fields.
Choose **Light / lamp** or **Air purifier**, leave **Allow control here and in chat** checked,
and click **Save outlet**. Choosing **Not assigned** disables control. Saving only changes
configuration; it does not switch the plug.

Use **Turn on** or **Turn off** to switch an enabled outlet immediately, or **Check status**
to read its power state. Each command displays its result. An uncertain command disables the
power buttons until you check status; it is never automatically retried. After saving a lamp,
chat requests such as "Turn off Bedroom lamp", "Turn off the bedroom lights", and "All off"
use the same saved inventory. Air purifiers require an explicit device target.

### Backend setup and control

New plugs are registered read-only because their connected load is unknown. An owner can assign
a name, room, and load through the UI, chat, or setup endpoint. This saves metadata without switching power.
Supported controllable loads are `lighting` and `air_purifier`; use `unclassified` with control
disabled for other/unknown loads. Simon must not infer a connected load from a naming request alone.

All routes use Simon's existing authenticated session. POST requests require the session's
`X-CSRF-Token` and exact `Origin`, as with the rest of the API. Inspect and try schemas at `/docs`.
These are backend endpoints; no consumption dashboard has been added yet.

| Method and path | Purpose |
| --- | --- |
| `POST /v1/home/discovery/refresh` | Discover all providers; LAN browsing has a short cooldown |
| `GET /v1/home/outlets` | List plug IDs, names, rooms, IP addresses, and load configuration |
| `POST /v1/home/outlets/{id}/setup` | Owner assigns load and enables control |
| `POST /v1/home/outlets/{id}/power` | Set on/off immediately, with an idempotency key |
| `GET /v1/home/devices/{id}/status` | Live switch state and electrical readings |
| `GET /v1/home/commands` | Read the requesting user's durable command receipts |
| `GET /v1/home/power` | Latest stored meter samples and stale/unavailable flags |
| `POST /v1/home/power/refresh` | Poll meters if their next sample is due |
| `GET /v1/home/devices/{id}/power?start=...&end=...` | Samples and measured consumption for a window |

Example setup body (replace `{id}` in the URL with a discovered Simon ID):

```json
{"name":"Bedroom lamp","room":"Bedroom","load_type":"lighting","control_enabled":true}
```

Example immediate switch body:

```json
{"on":false,"idempotency_key":"bedroom-lamp-off-001"}
```

Reuse the key only to retry the same request. A fresh intentional command needs a new key.
Requests claim a durable receipt before LAN I/O; retries, including after restart, cannot resend
an uncertain or completed command. Commands require no chat/model run and no confirmation card.
After setup, normal chat device controls can also target the plug. Room/all-lights commands
include lighting loads only; an air purifier must be named explicitly. Plugs cannot dim lamps or
change purifier fan speed. Verify separately that the purifier resumes when outlet power returns.
See the [Plug US Gen4 API](https://shelly-api-docs.shelly.cloud/gen2/Devices/Gen4/ShellyPlugUSG4/).

### Metering and consumption data

```dotenv
SIMON_SHELLY_LAN_DISCOVERY=true
SIMON_POWER_MONITORING_ENABLED=true
SIMON_POWER_POLL_SECONDS=60
SIMON_POWER_RETENTION_DAYS=90
```

The app polls all inventoried Shelly plugs, including read-only ones, every 60 seconds by default.
Discovery runs at startup and every five minutes. A newly discovered plug is picked up on the
next polling cycle. Monitoring runs independently of the chat model and sends no switching
commands. Migration 0010 adds the power sample table; the launcher applies it automatically.
Install updated dependencies with `python -m pip install -e ".[dev,postgres]"` for mDNS support.

Samples persist current power (W), the device energy counter (Wh), voltage, current, frequency,
temperature, on/off state, and uptime when reported. Collection failures become explicit unavailable
samples. Database claims prevent concurrent workers from polling the same plug twice per interval.
Raw samples older than the retention period are removed. Persistence requires PostgreSQL; memory
mode loses history when stopped. See [Switch status fields](https://shelly-api-docs.shelly.cloud/gen2/ComponentsAndServices/Switch/).

History defaults to the past 24 hours. Explicit start/end values require timezone offsets and a
window of at most 24 hours; query multiple windows for longer retained history. At most 3,000
samples are returned per window, ordered oldest first. Window endpoints are start-inclusive,
end-exclusive. The summary reports observed Wh/kWh and covered seconds, plus reset/gap counts.
It sums adjacent valid counter differences only. It excludes meter resets (also detected using
uptime), failed samples, long gaps, and unsampled edges; an empty window returns null consumption.
These are observed intervals, not an estimate of a complete billing period. Meter counters may
reset on reboot and are never reset by Simon. The future smart-home dashboard can consume these
endpoints without changing collection or command execution.

## Try it in chat

Examples (commands execute immediately):

- "Which home devices can you control?"
- "Is my office Beam on?"
- "Turn on my office Beam and set it to 40%."
- "Turn off Up Arrow."
- "Make the office lights blue at 40%."
- "All off."
- "Put Up Arrow and Down Arrow in Office and add them to Accent lights."
- Once installed: "Turn on the bedroom lamp" or "How much power is the office air purifier using?"

No confirmation card is required for home controls. Room/group/all-lights commands affect
lighting only; ask explicitly to control the air purifier. A single command supports up to 32
devices with independent results. Other supported home commands, including a named plug,
also execute directly. Google email/calendar previews still require confirmation.

Migration 0009 commits one command record per device per answer before network dispatch.
Duplicate tool calls cannot resend it. Receipts survive cancelled/failed answers and restarts;
find them in **Connections > Home devices > Recent device commands**. Stop prevents further
dispatch once observed; it cannot undo commands already sent. Old pending home previews are
not automatically executed?ask Simon again for the current desired state.

"Reported device state matches the request" means the immediate status readback matched (with a
small tolerance for brightness rounding). "Command accepted" means readback did not verify the
request; use **Check status** and observe the fixture. An unknown outcome or an action stuck at
"Command started" is never automatically resent, including after restart. Check the device before
making a fresh request. Do not infer failure merely from a slow cloud update.

## Repeatable tests

```powershell
.\venv\Scripts\python.exe -m pytest -q -m "not postgres" tests/unit/test_home_adapters.py tests/unit/test_home_color.py tests/contract/test_home_control.py tests/unit/test_discovery_adapters.py tests/contract/test_home_tools.py tests/contract/test_home_discovery.py tests/integration/test_home_api.py
```

Provider HTTP responses are simulated; tests do not operate real hardware. They exercise exact
targeting, signing, Digest auth, per-device capabilities, unknown outcomes, access revocation,
concurrent command retries, color encoding, batch results, cancellation, complete pagination, discovery error recovery, and room/group persistence.
The complete suite also runs persistence contracts on PostgreSQL
and renders the home controls in a real browser when browser tests are enabled. Follow the
README's full-suite settings for those checks. Live discovery and status have been verified for
the three office lights. Physical switching and dimming remain untested.
