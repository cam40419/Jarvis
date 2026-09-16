# Simon: practical-use roadmap

## September 16 phase: Home, voice, and remote access preparation

Implemented:

- A dedicated Home dashboard groups existing inventory by room. Light cards support power, brightness, and color according to device capabilities; outlet cards link to existing naming/load setup.
- Direct dashboard changes reuse permission checks, idempotent receipts, live preflight, and readback verification. Uncertain outcomes require a status check before another UI command.
- Shelly charts show 1/6/24-hour power histories and observed kWh, coverage, missing readings, and counter resets. The UI never treats unavailable samples as zero consumption. Status is refreshed explicitly; the existing backend continues sampling power.
- Live OpenAI WebRTC voice, independent of the text model, with server-side delegation into Simon's existing Auto routing and tools. Captions, recent call transcripts, mute, end call, and Stop task are available in chat.
- Session ownership, CSRF, single-call admission, duration/start limits, heartbeat expiry, cancellation revalidation, server-only delegation results, and cumulative usage persistence.
- `/simon` URL prefix support for pages, assets, auth, chat, streaming, and Google callbacks. Vercel external rewrite and named-tunnel examples, a production launcher, and passkey enrollment instructions.

The live synthetic WebRTC test connected and received final usage confirmation. Mocked integration tests exercise delegation and cancellation. Human speech quality, interruption timing on the user's phone, and physical home-device behavior still need acceptance testing. The public tunnel, Vercel portfolio rewrite, and Windows startup tasks have not been deployed.

Verification: 447 tests passed with PostgreSQL and browser checks enabled, at 95.23% coverage. Four opt-in paid tests were skipped in that suite; the new live WebRTC test passed separately. Ruff, strict mypy, and the production configuration check passed. Migration 0011 was applied to the local development database. No household devices were switched during these checks.

See [remote and voice setup](runbooks/remote-voice.md). Migration 0011 stores voice-session records. The first version requires one server worker and a foreground browser tab; it has no background wake word or mobile app service.

## Next: finish the personal deployment

1. Determine the DNS provider, provision the tunnel hostname, and merge the prepared rewrites into the existing Vercel portfolio project.
2. Enroll a domain passkey and test sign-in and voice using a phone's cellular connection. Check the portfolio root still works and Google returns through its updated callback.
3. Verify the actual office lights and named lamp/purifier outlets: off/on, brightness/color where supported, readback, meter samples, and device-unavailable handling.
4. Enable restart tasks after this test. Verify reboot recovery, a PostgreSQL backup/restore, and call cleanup after network loss. Move to an always-on host if keeping the Windows computer awake is inconvenient.

## Next: scenes and schedules

- Persist named scenes such as Work, Wind down, and All lights off; edit their device membership and settings from Home and chat. Capture per-device receipts and partial failures.
- Add an actual scheduling worker with timezone-aware routines, repeat rules, retry/idempotency policy, and an execution history. Existing job records alone do not provide scheduled automation.
- Extend power views with daily aggregates before offering weekly/monthly charts; account for missing coverage and meter resets. Add tariff/cost estimates only after the user provides a rate.

## Next: faster and more capable assistance

- Measure first speech, first text, delegation latency, and interruption latency on realistic personal tasks. Tune routing with those measurements rather than a claimed speed target.
- Add configurable budgets and usage visibility across speech and delegated reasoning. The existing call limits do not provide an account-wide spending cap.
- Evaluate direct handling of well-defined device commands and reusable scene actions. Retain ambiguity checks and permissions.
- Extend Google with calendar edits/recurrence and inbox reading, then evaluate reservation-specific integrations. Arbitrary website form completion is still unavailable.

Keep [model management](model-management-plan.md) and [home integration](home-integration-plan.md) as the detailed subsystem plans.
