# Reviewed bookings, orders and phone messages

The generic external-action API stores proposals, exact human review decisions and
durable provider outcomes. It remains an independent library/API boundary. Native
project agents are not enrolled in this runtime, and the Projects UI does not yet
provide its review flow. A reviewed board status alone cannot authorize a purchase,
booking or phone call.

The optional providers are disabled and unconfigured by default. There is no
general website checkout automation, interactive telephone conversation, stored
card entry, or standing authorization in this implementation. A call plays only
the exact reviewed text, then hangs up. No real transaction is part of the test
suite.

## Configure a provider

Open **Connections**. Select
**Twilio phone messages** or **Booking and purchase gateway**, enter the provider
credentials and connection fields, and choose **Connect account**. Simon saves
these settings in the backend and encrypts the credential. Connections belong
to the signed-in account and workspace. Use **Reconnect** to update credentials
or fields, and **Disconnect** to remove the stored connection. API and worker
processes read current settings without a restart; users do not edit files.

Twilio fields include an account SID, originating number, and optional allowed
recipient numbers. Gateway fields include an HTTPS service address and named
merchant identifiers. An operator may also configure these provider adapters on the server. Changing provider identity, destination or allowlists still
requires a fresh proposal under the existing review contract.

For **Twilio**, configure an actual account SID, its server-side auth token and
an authorized originating telephone number. Restrict `call_recipients` to exact
E.164 destinations when desired. The fixed official Calls endpoint accepts only
the reviewed `To`, configured `From`, escaped inline TwiML, a 30-second ringing
timeout and a reviewed call duration of 10–300 seconds. Recording is off. The
message is limited to 1,500 characters and escaped TwiML to 4,000 characters.
Charges and destination permissions depend on the Twilio account. Twilio
`completed` does not prove that a person answered or understood a message; it can
include an IVR or voicemail. See the official [Call resource](https://www.twilio.com/docs/voice/api/call-resource),
[Say verb](https://www.twilio.com/docs/voice/twiml/say), and
[credential guidance](https://www.twilio.com/docs/usage/security).

For **bookings and purchases**, the gateway adapter requires an actual service
that implements the contract below. Pointing it at a retailer's public website
does not implement ordering. `endpoint` is an exact HTTPS base address, with no
credentials, query, fragment or relative path segments. `merchant_names` maps
exact permitted merchant IDs to their displayed names. HTTP redirects and
automatic retries are disabled; credentials stay on the configured endpoint.

## Gateway contract

All operations use the operator's bearer token. Prices use integer minor units
and an ISO currency code, for example `{"amount_minor":1299,"currency":"USD"}`.
The gateway must enforce its own merchant/account authorization and immutable
quote binding; Simon cannot establish that an arbitrary external service
honors its contract.

1. `POST {endpoint}/quotes` is a **nonbinding** lookup. Its request follows
   `ExternalQuoteRequest` in `/openapi.json`: provider, kind, merchant ID, items
   or schedule, party size and fulfillment preferences. Return HTTP 200 with an
   `ExternalActionDraft` containing an immutable `quote_id`, exact merchant name,
   complete terms and total including fees, tax and delivery. Bookings and
   reservations include timezone-aware start/end times, location and party size.
   A quote request must never itself place an order or reserve capacity.
2. After user review, `POST {endpoint}/commitments` receives
   `{action_id, review_digest, workspace_id, actor_id, draft}` and
   `Idempotency-Key: <action_id>`. Verify that the entire submitted draft matches
   the immutable quote and its current validity. Reject changed prices, expired
   availability or substitutions with a definitive rejection such as HTTP 409;
   do not silently accept different terms. Deduplicate the action ID durably
   before making a commitment at the underlying provider.
3. HTTP 200, 201 or 202 returns this exact envelope:

   ```json
   {
     "action_id": "the-submitted-UUID",
     "review_digest": "the-submitted-64-character-SHA256",
     "receipt": {
       "id": "safe_provider_reference",
       "status": "accepted",
       "provider_status": "awaiting_confirmation",
       "outcome": "The provider accepted the exact reviewed request."
     }
   }
   ```

   Receipt status is `accepted`, `succeeded` or `failed`. Its ID contains only
   ASCII letters, digits, underscore or hyphen. `succeeded` must mean that the
   provider confirms the reviewed commitment. The action ID and review digest
   must match the request exactly.

4. `GET {endpoint}/commitments/{receipt_id}` returns the same envelope with the
   latest status. This read is available only after a known accepted receipt.
   There is no implicit cancellation/refund operation.

## Review, durability and recovery

Proposals are durable `platform.external_action` Jobs in the existing Store;
PostgreSQL deployments use the same transactions, optimistic versions, tenant
boundaries, audit journal and backups as other work. An initial proposal lives
in `Job.input`, with later state in `Job.result`. A per-run secondary Job index
is created in the same transaction as its proposal. Idempotency is scoped to the
actor, input details and run. Reusing a key with different details is rejected.

The API verifies the exact review digest and current identity before claiming
the submission. The `executing` claim commits **before** provider I/O. Repeated
confirmation never redispatches an executing, accepted or terminal action.
Timeouts, server errors, invalid receipts and process interruption can leave an
unknown outcome. Neither the worker nor UI automatically retries these actions.

For an interrupted claim older than five minutes, explicitly mark it unknown,
then investigate the provider's records. The authenticated API can record a manual
resolution only for an unknown outcome. It requires the exact review digest,
`reported_outcome` (`completed` or `not_completed`), a provider evidence/reference
description and a user note. This produces distinct status `resolved`, records
the actor and timestamp, and is labelled **User reconciled**. It sends no new
provider request and does not claim machine-verified provider confirmation.
Canceling a pending proposal only withdraws local approval; it does not cancel
an order or call already submitted to a provider.

Authenticated endpoints use `/v1/external-actions`: GET list and `/providers`,
POST proposal and `/quotes`, GET `/{id}`, and POST `/{id}/confirm`, `/cancel`,
`/refresh`, `/mark-interrupted`, `/reconcile`. Confirmation and cancellation
accept `{review_digest}`. Session writes retain the application's CSRF checks.
Reading requires `jobs:read`; proposal, decisions and reconciliation require
`jobs:write`. Decisions and reconciliation additionally require the authenticated
API channel and account ownership.

The standalone worker-library transport `external_actions` exposes only `propose`, `status`, `providers`
and optional network `quote`. There is no agent confirmation or reconciliation
tool. Canonical schemas, required scopes, run identity and fresh authorization
are checked in the transport, in addition to profile tool grants. Quote lookup
requires explicit network permission and a configured gateway.

## Validation

Run `python -m pytest tests/unit/test_external_actions.py
tests/integration/test_external_actions_api.py -q`. Tests use synthetic HTTP
responses, including concurrent approval, timeout/unknown outcomes, credentials
redaction, immutable receipt binding, ownership, review digests and restart
durability. Set `SIMON_TEST_DATABASE_URL` to an isolated database ending in
`_test` to exercise PostgreSQL persistence; tests create a temporary schema.

Native project integration and a review UI require a later bounded connector phase;
these tests do not certify a live provider account or a completed booking.
