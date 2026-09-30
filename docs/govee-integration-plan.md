# Govee integration investigation

Investigated September 29, 2026. Goal: two light strips and one lamp with power and color
control through Home, text chat, and voice. Implementation and hardware compatibility remain
pending connection setup and the lamp model. Both strips are confirmed by the user as H6147.

## H6147 finding

Govee lists H6147 as Bluetooth, with two 10 m rolls per kit. It is absent from the current
cloud API supported-model list. The Homebridge Govee project's own compatibility table lists
Bluetooth support for H6147 but not API or LAN support. The strip integration therefore needs
a Bluetooth path; the cloud adapter proposed below will not connect these strips.
[Govee specifications](https://community.govee.com/support/faqs/specs),
[Homebridge device compatibility](https://github.com/homebridge-plugins/homebridge-govee/wiki/Supported-Devices).

Use a Bluetooth-capable Simon host within range, or a nearby always-on Bluetooth bridge with
a network API. Windows returned no present Bluetooth devices during the read-only hardware
check; this does not establish that the computer lacks hardware (it may be disabled or lack
drivers). Confirm adapter availability before selecting a direct Windows BLE implementation.
Power, brightness and solid-color commands must be tested against the actual controllers.
Do not promise reliable readback based only on a successful BLE write.

Clarify whether "two strips" means two independent H6147 controllers or the two rolls from
one kit. Base device identity and separate Home cards on controllers, not roll count.
The lamp's model remains needed to choose Bluetooth, LAN or cloud for that device.

## Findings

Simon currently supports LIFX, Tuya and Shelly. No Govee integration or Govee configuration
variable was found. Home already offers on/off buttons, a brightness slider and a color picker.
Text and voice use the same home tools and durable command receipts. A new provider can reuse
this path; it does not need a separate assistant or voice integration.

The current Govee cloud API exposes device discovery, state and capability-based control.
Power, brightness and RGB color are advertised per device. Discovery returns a model/SKU
alongside a device ID, both of which must be retained. Check the actual account inventory
against the requested capabilities instead of assuming every strip/lamp is supported.
[Discovery and capabilities](https://developer.govee.com/reference/get-you-devices),
[control](https://developer.govee.com/reference/control-you-devices),
[supported models](https://developer.govee.com/docs/support-product-model).

Cloud integration needs a Govee API key requested through Govee Home Settings → Apply for API
Key. It does not use Google's OAuth setup. Keep the key in server secret configuration.
If a key already exists, reuse it: Govee states that generating a new key invalidates previous
active keys for that account.
[Key setup](https://developer.govee.com/reference/apply-you-govee-api-key),
[key rotation policy](https://developer.govee.com/changelog/important-policy-update-important-notice-regarding-api-key-security-management).

Govee's official desktop guide requires LAN Control enabled and devices on the same LAN for
its local features. Local support must be checked per model and firmware; Desktop compatibility
alone is not proof that every public LAN API operation is available. Prefer direct local
control where verified; use the cloud adapter for supported devices without local control.
[Official LAN setup guidance](https://desktop.govee.com/user-manual/user-guide).

## Proposed implementation

1. Add `GoveeAPI` with `discover`, `status`, and `set`, initially using the current cloud API
   at `https://openapi.api.govee.com/router/api/v1`. Add `SIMON_GOVEE_API_KEY` as a secret setting.
   A later/parallel LAN transport should map to the same device identity to avoid duplicate cards.
2. Extend `HomeDevice`, `DiscoveredDevice` and `HomeSync` for Govee and its SKU. Current remote-ID
   validation rejects Govee's colon-separated IDs; add provider-specific validation without
   weakening LIFX/Shelly validation. Extend `home_syncs_provider_check` on `home_syncs`
   through a new migration.
3. Register Govee in `HomeInventory` and `HomeService`, including account binding, discovery,
   configuration status, command dispatch, readback and transport cleanup. Preserve Simon-owned
   names, rooms and groups. Enable control only for discovered lighting with supported capabilities.
4. Reuse the Home cards and `home_control` tool. Examples: “Turn off both strips,” “Make the
   lamp blue,” and “Set the living-room lights to purple at 30 percent.” Essential ambiguities
   should resolve by device name/room, with normal lighting commands executing directly.
5. Translate Simon's color representation to Govee RGB, keeping brightness separate. A combined
   power/brightness/color request can need multiple provider calls: track partial execution and
   uncertain results accurately rather than claiming the whole command succeeded.
6. Add rate limiting and shared status caching. Published cloud limits include two control
   requests/second/device and 30 state reads/minute/device. Do not copy Simon's rapid readback
   polling unchanged. Honor 429 responses and never automatically repeat an uncertain write.
   [Published limits](https://developer.govee.com/reference/get-you-devices).
7. Handle offline or unqueryable state distinctly. Govee documents offline state as historical
   and empty state values as unsupported queries. Report accepted commands separately from
   verified device changes.
   [State semantics](https://developer.govee.com/reference/get-devices-status).

## Verification before enabling the three devices

- Adapter tests: discovery, RGB/brightness conversion, advertised capabilities, malformed replies,
  throttling, stale state, timeouts and partial multi-setting writes.
- Service/database tests: provider migration, household isolation, credential rotation, naming,
  groups, cancellation and duplicate command receipts.
- Browser tests: discovery, power buttons, color/brightness, offline and uncertain states.
- Assistant tests: UI/text/voice resolve the same device IDs and report actual receipts.
- Hardware check for each strip and lamp after model identification and connection setup.

Initial scope is whole-device power, solid color and brightness. Segmented RGBIC effects,
animations, music sync and DreamView require separate capability handling and testing.

No application behavior, credentials, firewall rules or device settings were changed during
this investigation. No device-control commands were sent.
