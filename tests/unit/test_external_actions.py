from __future__ import annotations

import json
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from urllib.parse import parse_qs
from uuid import uuid4

import httpx
import pytest
from pydantic import ValidationError

from simon.adapters.external_action_providers import (
    ExternalActionProviders,
    load_external_providers,
)
from simon.adapters.external_action_tools import (
    ExternalActionToolTransport,
    external_action_tool_definitions,
)
from simon.adapters.memory import InMemoryStore
from simon.adapters.optional_http import BoundedHTTP
from simon.adapters.tool_transports import TransportRegistry
from simon.domain.errors import (
    AuthorizationError,
    IdempotencyConflictError,
    InvalidTransitionError,
    NotFoundError,
)
from simon.domain.external_actions import (
    CallDetails,
    ExternalActionDraft,
    ExternalProviderDefinition,
    ExternalQuoteRequest,
    ReconcileExternalAction,
)
from simon.domain.identity import DEV_ACTOR_ID, DEV_WORKSPACE_ID
from simon.domain.models import ActorContext, Channel, JobStatus, utc_now
from simon.domain.tool_catalog import ToolCatalogError, ToolExecutionContext, ToolExecutionError
from simon.services.audit import AuditService
from simon.services.external_actions import ExternalActionService

SECRET = "synthetic-provider-secret"
SID = "AC" + "1" * 32
CALL = "CA" + "2" * 32


def actor(**updates):
    return ActorContext(
        actor_id=DEV_ACTOR_ID,
        workspace_id=DEV_WORKSPACE_ID,
        channel=Channel.API,
        scopes=frozenset({"jobs:read", "jobs:write"}),
    ).model_copy(update=updates)


def call_draft(**updates):
    return ExternalActionDraft(
        kind="call",
        provider_id="phone",
        recipient_id="+12025550123",
        recipient_label="Synthetic recipient",
        summary="Deliver the reviewed message",
        terms="One prerecorded call, up to 120 seconds. Voice charges may apply.",
        call=CallDetails(message="This is Simon, an automated assistant. Test message."),
    ).model_copy(update=updates)


def phone(**updates):
    return ExternalProviderDefinition(
        id="phone",
        name="Configured phone",
        kind="twilio",
        enabled=True,
        workspace_id=DEV_WORKSPACE_ID,
        actor_ids=frozenset({DEV_ACTOR_ID}),
        credential_env="PHONE_SECRET",
        account_sid=SID,
        from_number="+12025550199",
    ).model_copy(update=updates)


def call_response(status="queued", **updates):
    return {
        "sid": CALL,
        "account_sid": SID,
        "to": "+12025550123",
        "from": "+12025550199",
        "status": status,
        **updates,
    }


def setup(handler=None, *, store=None, definitions=None, environ=None):
    store = store or InMemoryStore()
    requests = []

    def send(request):
        requests.append(request)
        return handler(request) if handler else httpx.Response(201, json=call_response())

    providers = ExternalActionProviders(
        BoundedHTTP(
            transport=httpx.MockTransport(send),
            environ={"PHONE_SECRET": SECRET} if environ is None else environ,
        )
    )
    service = ExternalActionService(
        store, AuditService(store), [phone()] if definitions is None else definitions, providers
    )
    return service, requests


def confirm(service, proposal, *, who=None, revalidate=None):
    who = who or actor()
    return service.decide(
        who,
        proposal.id,
        review_digest=proposal.review_digest,
        confirm=True,
        revalidate=revalidate or (lambda: who),
    )


def test_proposal_uses_initial_job_input_and_duplicate_is_durable_across_service_instances(store):
    service, requests = setup(store=store)
    proposed = service.propose(actor(), call_draft(), "same-proposal-key")
    job = store.get_job(proposed.id)
    assert job.result is None  # PostgreSQL create_job persists input, not an initial result.
    assert job.input["proposal"]["status"] == "pending"
    recreated, _ = setup(store=store)
    assert recreated.get(actor(), proposed.id) == proposed
    assert recreated.propose(actor(), call_draft(), "same-proposal-key").id == proposed.id
    assert not requests
    assert len(service.list_actions(actor())) == 1
    with pytest.raises(IdempotencyConflictError):
        service.propose(actor(), call_draft(summary="Changed intent"), "same-proposal-key")


def test_run_index_is_durable_owned_and_returns_current_state(store):
    service, requests = setup(store=store)
    run_id = uuid4()
    proposed = service.propose(actor(), call_draft(), "run-linked-proposal", run_id=run_id)
    service.propose(actor(), call_draft(), "standalone-proposal")
    other = service.propose(actor(), call_draft(), "other-run-proposal", run_id=uuid4())
    recreated, _ = setup(store=store)
    assert recreated.list_for_run(actor(), run_id) == (proposed,)
    assert (
        recreated.propose(actor(), call_draft(), "run-linked-proposal", run_id=run_id).id
        == proposed.id
    )
    assert len(recreated.list_for_run(actor(), run_id)) == 1
    assert recreated.list_for_run(actor(actor_id=uuid4()), run_id) == ()
    assert recreated.list_for_run(actor(workspace_id=uuid4()), run_id) == ()
    assert recreated.list_for_run(actor(), uuid4()) == ()
    cancelled = recreated.decide(
        actor(),
        proposed.id,
        review_digest=proposed.review_digest,
        confirm=False,
        revalidate=actor,
    )
    assert recreated.list_for_run(actor(), run_id) == (cancelled,)
    assert cancelled.status == "cancelled" and other.id != cancelled.id
    assert not requests


def test_manual_reconciliation_requires_unknown_owned_digest_and_evidence_without_network():
    service, requests = setup(lambda _: httpx.Response(503))
    proposal = service.propose(actor(), call_draft(), "reconcile-unknown-call")
    body = ReconcileExternalAction(
        review_digest=proposal.review_digest,
        reported_outcome="not_completed",
        evidence="Provider call log checked at 14:30; no call was created.",
        note="I checked the destination and timestamp with the provider support record.",
    )
    with pytest.raises(InvalidTransitionError):
        service.reconcile(actor(), proposal.id, body, revalidate=actor)
    unknown = confirm(service, proposal)
    assert unknown.status == "unknown" and len(requests) == 1
    with pytest.raises(AuthorizationError):
        service.reconcile(actor(channel=Channel.WORKER), proposal.id, body, revalidate=actor)
    stranger = actor(actor_id=uuid4())
    with pytest.raises(NotFoundError):
        service.reconcile(stranger, proposal.id, body, revalidate=lambda: stranger)
    with pytest.raises(InvalidTransitionError):
        service.reconcile(
            actor(),
            proposal.id,
            body.model_copy(update={"review_digest": "0" * 64}),
            revalidate=actor,
        )
    with pytest.raises(ValidationError):
        ReconcileExternalAction(**{**body.model_dump(), "evidence": " " * 20})
    resolved = service.reconcile(actor(), proposal.id, body, revalidate=actor)
    assert resolved.status == "resolved"
    assert resolved.manual_resolution.actor_id == actor().actor_id
    assert resolved.manual_resolution.evidence == body.evidence
    assert "User reported" in resolved.outcome
    assert resolved.provider_status is None
    assert service.store.audit_events()[-1].event_type == "external_action.resolved"
    assert confirm(service, proposal).status == "resolved"
    assert len(requests) == 1


def test_confirmation_dispatches_once_and_exact_twilio_request_is_bounded():
    service, requests = setup()
    draft = call_draft(call=CallDetails(message="Hello <Dial> & goodbye", max_duration_seconds=45))
    proposal = service.propose(actor(), draft, "call-message-001")
    result = confirm(service, proposal)
    assert result.status == "accepted" and result.provider_id == CALL
    assert confirm(service, proposal) == result
    assert len(requests) == 1
    request = requests[0]
    assert str(request.url) == f"https://api.twilio.com/2010-04-01/Accounts/{SID}/Calls.json"
    body = parse_qs(request.content.decode())
    assert body["To"] == [draft.recipient_id] and body["From"] == [phone().from_number]
    assert body["Record"] == ["false"] and body["TimeLimit"] == ["45"]
    assert "&lt;Dial&gt; &amp;" in body["Twiml"][0]
    assert set(body) == {"To", "From", "Twiml", "Record", "Timeout", "TimeLimit"}
    assert SECRET not in result.model_dump_json()
    assert [event.event_type for event in service.store.audit_events()] == [
        "external_action.proposed",
        "external_action.executing",
        "external_action.accepted",
    ]


def test_concurrent_confirmation_sees_committed_claim_and_does_not_send_twice():
    entered, release = threading.Event(), threading.Event()

    def send(request):
        entered.set()
        assert release.wait(timeout=5)
        return httpx.Response(201, json=call_response())

    service, requests = setup(send)
    proposal = service.propose(actor(), call_draft(), "concurrent-call")
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(confirm, service, proposal)
        assert entered.wait(timeout=5)
        try:
            assert confirm(service, proposal).status == "executing"
        finally:
            release.set()
        assert first.result(timeout=5).status == "accepted"
    assert len(requests) == 1


@pytest.mark.parametrize(
    "failure", ["timeout", "server_error", "invalid_receipt", "wrong_recipient"]
)
def test_ambiguous_provider_outcomes_are_unknown_and_never_replayed(failure):
    def send(request):
        if failure == "timeout":
            raise httpx.ReadTimeout(SECRET, request=request)
        if failure == "server_error":
            return httpx.Response(503, json={"secret": SECRET})
        value = {} if failure == "invalid_receipt" else call_response(to="+12025550000")
        return httpx.Response(201, json=value)

    service, requests = setup(send)
    proposal = service.propose(actor(), call_draft(), "uncertain-outcome")
    result = confirm(service, proposal)
    assert result.status == "unknown" and SECRET not in result.model_dump_json()
    assert confirm(service, proposal).status == "unknown"
    assert len(requests) == 1


def test_explicit_provider_rejection_is_failed_without_retry():
    service, requests = setup(lambda request: httpx.Response(400, json={"message": SECRET}))
    proposal = service.propose(actor(), call_draft(), "rejected-outcome")
    assert confirm(service, proposal).status == "failed"
    assert confirm(service, proposal).status == "failed"
    assert len(requests) == 1


def test_confirmation_binds_digest_actor_scope_and_current_identity():
    service, requests = setup()
    proposal = service.propose(actor(), call_draft(), "authorize-action")
    with pytest.raises(InvalidTransitionError):
        service.decide(actor(), proposal.id, review_digest="0" * 64, confirm=True, revalidate=actor)
    for who in (actor(channel=Channel.WORKER), actor(scopes=frozenset({"jobs:read"}))):
        with pytest.raises(AuthorizationError):
            confirm(service, proposal, who=who)
    with pytest.raises(NotFoundError):
        service.get(actor(actor_id=uuid4()), proposal.id)
    with pytest.raises(NotFoundError):
        service.get(actor(workspace_id=uuid4()), proposal.id)
    with pytest.raises(AuthorizationError):
        confirm(service, proposal, revalidate=lambda: actor(actor_id=uuid4()))
    assert not requests and service.get(actor(), proposal.id).status == "pending"


def test_revocation_after_durable_claim_fails_before_network():
    service, requests = setup()
    proposal = service.propose(actor(), call_draft(), "revoke-after-claim")
    calls = 0

    def revalidate():
        nonlocal calls
        calls += 1
        return actor() if calls == 1 else actor(scopes=frozenset())

    assert confirm(service, proposal, revalidate=revalidate).status == "failed"
    assert not requests


def test_missing_configuration_and_modified_provider_are_visible_blockers():
    for definitions, environ in (([], {}), ([phone(enabled=False)], {}), ([phone()], {})):
        service, requests = setup(definitions=definitions, environ=environ)
        proposal = service.propose(actor(), call_draft(), "unconfigured-call")
        assert proposal.blockers
        assert confirm(service, proposal).status == "pending"
        assert not requests
    service, requests = setup()
    proposal = service.propose(actor(), call_draft(), "changed-provider")
    service.definitions["phone"] = phone(from_number="+12025550188")
    assert "changed" in confirm(service, proposal).blockers[0]
    assert not requests


def test_cancel_expiry_and_interrupted_claims_never_dispatch():
    service, requests = setup()
    proposal = service.propose(actor(), call_draft(), "cancel-proposal")
    result = service.decide(
        actor(), proposal.id, review_digest=proposal.review_digest, confirm=False, revalidate=actor
    )
    assert result.status == "cancelled"
    assert confirm(service, proposal).status == "cancelled"
    expired = service.propose(actor(), call_draft(), "expired-proposal")
    job = service.store.get_job(expired.id)
    expired = expired.model_copy(update={"expires_at": utc_now() - timedelta(seconds=1)})
    service.store.transition_job(
        job.id, job.version, JobStatus.NEEDS_HUMAN, {"proposal": expired.model_dump(mode="json")}
    )
    assert confirm(service, expired).status == "expired"
    interrupted = service.propose(actor(), call_draft(), "interrupted-proposal")
    job = service.store.get_job(interrupted.id)
    interrupted = interrupted.model_copy(
        update={"status": "executing", "claimed_at": utc_now() - timedelta(minutes=10)}
    )
    service.store.transition_job(
        job.id, job.version, JobStatus.RUNNING, {"proposal": interrupted.model_dump(mode="json")}
    )
    assert service.mark_interrupted_unknown(actor(), interrupted.id).status == "unknown"
    assert confirm(service, interrupted).status == "unknown"
    assert not requests


def test_provider_refresh_reads_known_call_without_new_call():
    def send(request):
        return httpx.Response(
            201 if request.method == "POST" else 200,
            json=call_response("queued" if request.method == "POST" else "completed"),
        )

    service, requests = setup(send)
    proposal = service.propose(actor(), call_draft(), "refresh-known-call")
    confirm(service, proposal)
    result = service.refresh(actor(), proposal.id)
    assert result.status == "succeeded" and "not verified" in result.outcome
    assert [request.method for request in requests] == ["POST", "GET"]


def purchase_draft():
    return ExternalActionDraft.model_validate(
        {
            "kind": "purchase",
            "provider_id": "orders",
            "recipient_id": "merchant-1",
            "recipient_label": "Configured merchant",
            "summary": "Buy one example item",
            "terms": "Quoted total includes tax and shipping. No substitutions.",
            "purchase": {
                "quote_id": "quote-123",
                "items": [{"item_id": "item-1", "description": "Example item", "quantity": 1}],
                "total": {"amount_minor": 1234, "currency": "USD"},
                "fulfillment": "Collect at the merchant's stated pickup location",
            },
        }
    )


def gateway():
    return ExternalProviderDefinition(
        id="orders",
        name="Merchant gateway",
        kind="gateway",
        enabled=True,
        workspace_id=DEV_WORKSPACE_ID,
        actor_ids=frozenset({DEV_ACTOR_ID}),
        endpoint="https://merchant.example/api",
        credential_env="PHONE_SECRET",
        merchant_names={"merchant-1": "Configured merchant"},
    )


def test_real_gateway_contract_quotes_then_submits_exact_review_and_idempotency_key():
    def send(request):
        if request.url.path.endswith("/quotes"):
            return httpx.Response(200, json=purchase_draft().model_dump(mode="json"))
        body = json.loads(request.content)
        assert body["draft"]["purchase"]["total"] == {"amount_minor": 1234, "currency": "USD"}
        assert request.headers["Idempotency-Key"] == body["action_id"]
        return httpx.Response(
            201,
            json={
                "action_id": body["action_id"],
                "review_digest": body["review_digest"],
                "receipt": {
                    "id": "order_1",
                    "status": "succeeded",
                    "provider_status": "confirmed",
                    "outcome": "Order confirmed",
                },
            },
        )

    service, requests = setup(send, definitions=[gateway()])
    quote = service.quote(
        actor(),
        ExternalQuoteRequest(
            provider_id="orders",
            kind="purchase",
            recipient_id="merchant-1",
            items=purchase_draft().purchase.items,
        ),
    )
    assert len(requests) == 1 and requests[0].url.path.endswith("/quotes")
    action = service.propose(actor(), quote, "quoted-purchase")
    assert len(requests) == 1
    assert confirm(service, action).status == "succeeded"
    assert requests[1].url.path == "/api/commitments"


def test_gateway_rejects_unapproved_merchants_and_unbound_receipts():
    service, requests = setup(lambda request: httpx.Response(201, json={}), definitions=[gateway()])
    invalid = purchase_draft().model_copy(update={"recipient_label": "Different merchant"})
    proposal = service.propose(actor(), invalid, "wrong-merchant-label")
    assert confirm(service, proposal).blockers and not requests
    action = service.propose(actor(), purchase_draft(), "unbound-provider-receipt")
    assert confirm(service, action).status == "unknown"


@pytest.mark.parametrize("mismatch", ["action_id", "review_digest", "id"])
def test_gateway_receipt_cannot_rebind_a_review_or_inject_receipt_paths(mismatch):
    def send(request):
        submitted = json.loads(request.content)
        response = {
            "action_id": submitted["action_id"],
            "review_digest": submitted["review_digest"],
            "receipt": {
                "id": "receipt_123",
                "status": "accepted",
                "provider_status": "pending",
                "outcome": "Review received",
            },
        }
        if mismatch == "id":
            response["receipt"]["id"] = "receipt:unexpected"
        else:
            response[mismatch] = str(uuid4()) if mismatch == "action_id" else "0" * 64
        return httpx.Response(201, json=response)

    service, requests = setup(send, definitions=[gateway()])
    proposal = service.propose(actor(), purchase_draft(), "bound-receipt-test")
    assert confirm(service, proposal).status == "unknown"
    assert confirm(service, proposal).status == "unknown"
    assert len(requests) == 1


def test_expired_reservation_start_cannot_be_submitted_and_redirects_are_not_followed():
    now = utc_now()
    draft = ExternalActionDraft.model_validate(
        {
            "kind": "reservation",
            "provider_id": "orders",
            "recipient_id": "merchant-1",
            "recipient_label": "Configured merchant",
            "summary": "Reviewed reservation",
            "terms": "A reservation at the stated time, with the stated fee.",
            "reservation": {
                "quote_id": "reservation-1",
                "start_at": now - timedelta(minutes=1),
                "end_at": now + timedelta(hours=1),
                "party_size": 2,
                "location": "Stated location",
                "total": {"amount_minor": 500, "currency": "USD"},
            },
        }
    )
    service, requests = setup(definitions=[gateway()])
    proposal = service.propose(actor(), draft, "past-reservation")
    assert "start time has passed" in confirm(service, proposal).blockers[0]
    assert not requests
    service, requests = setup(
        lambda _: httpx.Response(307, headers={"Location": "https://other.example/steal"}),
        definitions=[gateway()],
    )
    proposal = service.propose(actor(), purchase_draft(), "redirect-prohibited")
    assert confirm(service, proposal).status == "unknown"
    assert len(requests) == 1 and requests[0].url.host == "merchant.example"


def test_provider_file_is_bounded_explicit_and_contains_only_secret_references(tmp_path):
    assert load_external_providers(None) == ()
    path = tmp_path / "providers.json"
    path.write_text(json.dumps([phone().model_dump(mode="json")]))
    assert load_external_providers(path) == (phone(),)
    for data in (
        [{**phone().model_dump(mode="json"), "auth_token": SECRET}],
        [phone().model_dump(mode="json")] * 2,
        {"providers": []},
    ):
        path.write_text(json.dumps(data))
        with pytest.raises(ToolCatalogError):
            load_external_providers(path)
    path.write_bytes(b" " * 262145)
    with pytest.raises(ToolCatalogError):
        load_external_providers(path)


def test_worker_can_only_prepare_and_read_reviews_never_confirm():
    service, requests = setup()
    registry = TransportRegistry()
    run_id = uuid4()
    registry.register(
        "external_actions",
        ExternalActionToolTransport(
            service,
            actor_id=DEV_ACTOR_ID,
            workspace_id=DEV_WORKSPACE_ID,
            run_id=run_id,
            revalidate=actor,
        ),
    )
    definitions = {tool.id: tool for tool in external_action_tool_definitions(enabled=True)}
    assert set(definitions) == {
        "external_actions.propose",
        "external_actions.status",
        "external_actions.providers",
    }
    context = ToolExecutionContext(
        actor_id=DEV_ACTOR_ID,
        workspace_id=DEV_WORKSPACE_ID,
        run_id=run_id,
        agent_id="planner",
        scopes=actor().scopes,
        allowed_tool_ids=frozenset(definitions),
        authorized_action="write",
    )
    output = registry.execute(
        definitions["external_actions.propose"],
        {"draft": call_draft().model_dump(mode="json"), "idempotency_key": "worker-proposal"},
        context,
    ).output
    assert output["status"] == "pending" and output["run_id"] == str(context.run_id)
    assert not requests
    weakened = definitions["external_actions.propose"].model_copy(
        update={"side_effect": False, "action_policy": "read", "required_scopes": frozenset()}
    )
    with pytest.raises(ToolCatalogError):
        registry.execute(
            weakened,
            {"draft": call_draft().model_dump(mode="json"), "idempotency_key": "worker-proposal"},
            context,
        )
    with pytest.raises(AuthorizationError):
        registry.execute(
            definitions["external_actions.status"],
            {"action_id": output["id"]},
            context.model_copy(update={"run_id": uuid4()}),
        )
    handler = ExternalActionToolTransport(
        service,
        actor_id=DEV_ACTOR_ID,
        workspace_id=DEV_WORKSPACE_ID,
        run_id=run_id,
        revalidate=lambda: actor(scopes=frozenset()),
    )
    with pytest.raises(AuthorizationError):
        handler(definitions["external_actions.status"], {"action_id": output["id"]}, context)
    schema_weakened = definitions["external_actions.status"].model_copy(update={"input_schema": {}})
    with pytest.raises(ToolCatalogError):
        handler(schema_weakened, {"action_id": output["id"]}, context)


def test_draft_rejects_payment_fields_wrong_kind_and_invalid_phone():
    for changes in (
        {"recipient_id": "https://attacker.example"},
        {"card_number": "synthetic"},
        {"kind": "purchase"},
    ):
        with pytest.raises(ValidationError):
            ExternalActionDraft.model_validate({**call_draft().model_dump(), **changes})
    with pytest.raises(ToolExecutionError):
        service, _ = setup(lambda request: httpx.Response(200, json={}), definitions=[gateway()])
        service.quote(
            actor(),
            ExternalQuoteRequest(provider_id="orders", kind="purchase", recipient_id="merchant-1"),
        )
