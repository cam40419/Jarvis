# Raspberry Pi idle display

The idle display is a thin Chromium kiosk. Simon owns its slideshow, layout, widgets, and live
data; the Pi stores no application state and needs no repository checkout. A screen polls for
changes every 15 seconds and keeps retrying through network or server restarts.

## Requirements

- Raspberry Pi 3 or newer with Raspberry Pi OS Desktop. A Pi 3 works well for this thin client;
  use optimized slideshow images and avoid keeping unrelated Chromium tabs open.
- A monitor and an account configured to log into the desktop automatically
- Network access to the configured `SIMON_PUBLIC_ORIGIN` (HTTPS is required for non-loopback
  production deployments)
- Chromium: `sudo apt update && sudo apt install -y chromium`

## Provision

1. Sign into Simon as an owner and open **Home → Displays**.
2. Choose **Add a display**, give the Pi a room-oriented name, and copy the one-time provisioning
   URL. It contains a device secret and should not be posted or committed.
3. On the Pi, open that URL in Chromium once. Simon validates it, stores the secret in an
   HTTP-only cookie, and redirects to a URL without the secret.
4. Confirm the display reports **Online** on the management page. Upload JPEG, PNG, WebP, or GIF
   files there (15 MB maximum each).

The generated URL remains valid for reinstalling that device, so keep it in a password manager.
Deleting browser data removes the device cookie and requires using that URL again.

## Start Chromium automatically

On the Pi, create the desktop autostart directory and copy the supplied template:

```bash
mkdir -p ~/.config/autostart
cp deploy/pi/simeon-display.desktop.example ~/.config/autostart/simeon-display.desktop
chmod 600 ~/.config/autostart/simeon-display.desktop
```

If the Pi does not have this repository, create the same file from the template on the Simon
server. Replace `PASTE_PROVISIONING_URL_HERE` with the URL from the management page. Log out and
back in, or reboot, to test autostart. The first launch consumes the URL into the browser cookie;
subsequent launches safely revisit it and refresh that cookie.

To prevent the monitor from sleeping, use Raspberry Pi Configuration to disable screen blanking.
Avoid embedding the URL in a world-readable system service because it contains the device token.

## Raspberry Pi 3 and portrait screens

Portrait orientation is detected automatically with CSS; there is no separate Simon setting.
For a physically rotated 16:9 panel, configure Raspberry Pi OS for a 90-degree rotation so the
browser reports a 9:16 viewport. Overlay widgets stay in the four corners and center. Split layout
places the photograph in the upper portion and stacks widgets over a dark lower panel.

For smooth operation on a Pi 3, prepare photos at the panel's native portrait resolution—normally
1080×1920—or smaller. JPEG/WebP files around 2 MB or less reduce decoding pauses and memory use;
the 15 MB upload limit is a safety ceiling, not a recommended target. The portrait stylesheet
also disables the relatively expensive widget blur effect.

## Manage through chat

Simon's model receives two display tools for owners. It first reads the full configuration and
can then change the layout, image timing, dimming, or complete widget set. Supported widgets are:

- `clock`
- `power` (current total and monitored-device count)
- `running_jobs`
- `printer`
- `message` (short custom text)

Layouts are `overlay`, `split`, and `focus`. Widget positions are top-left, top-right,
bottom-left, bottom-right, and center, with one widget per position. Image upload and removal stay
in the authenticated Displays page; chat cannot invent or retrieve local image files.

## Storage and security

`SIMON_DISPLAY_DATA_DIR` defaults to `.local/displays`. It contains the manifest, hashed device
tokens, and uploaded originals. Include it in host backups. Device endpoints are read-only and
use a separate HTTP-only cookie, so the kiosk never receives an owner session or CSRF credential.
Use HTTPS whenever the Pi reaches Simeon over anything other than a trusted private tunnel.

If a provisioning URL is exposed, remove that display's record from the manifest while Simeon is
stopped and provision a replacement. A UI revoke/rotate action is not yet exposed.

## Troubleshooting

- **Display unavailable / retrying:** verify the Pi can open Simon's origin and that its clock is
  correct. Reopen the provisioning URL if browser data was cleared.
- **Offline in management:** a screen is considered online after it has polled during the last 90
  seconds.
- **No power values:** power widgets only include currently sampled Shelly meters.
- **No running jobs:** completed and cancelled jobs are intentionally hidden.
- **Black bars or cropping:** images use center-crop (`background-size: cover`); prepare them at
  9:16 for a portrait 16:9 panel when the crop matters.
