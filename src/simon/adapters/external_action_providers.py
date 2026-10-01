"""Fixed optional commitment providers. No browser checkout or arbitrary HTTP tools."""

from __future__ import annotations

import base64
import hmac
import json
import re
import xml.etree.ElementTree as ET
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlencode, urlsplit
from uuid import UUID

from pydantic import Field, ValidationError

from simon.adapters.optional_http import BoundedHTTP
from simon.domain.external_actions import (
    ExternalActionDraft,
    ExternalActionProposal,
    ExternalProviderDefinition,
    ExternalProviderReceipt,
    ExternalQuoteRequest,
)
from simon.domain.models import StrictModel
from simon.domain.tool_catalog import ToolCatalogError, ToolExecutionError
from simon.services.canonical import digest


class GatewayReceiptEnvelope(StrictModel):
    action_id: UUID
    review_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    receipt: ExternalProviderReceipt


def load_external_providers(path: Path | None) -> tuple[ExternalProviderDefinition, ...]:
    if path is None:
        return ()
    try:
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 262144:
            raise ValueError
        with path.open("rb") as stream:
            data = json.loads(stream.read(262145))
        if not isinstance(data, list) or len(data) > 100:
            raise ValueError
        result = tuple(ExternalProviderDefinition.model_validate(value) for value in data)
        if len({value.id for value in result}) != len(result):
            raise ValueError
        return result
    except (OSError, ValueError, UnicodeError):
        raise ToolCatalogError(
            "External provider file must contain at most 100 valid configurations in 256 KiB"
        ) from None


def provider_fingerprint(definition: ExternalProviderDefinition) -> str:
    values = definition.model_dump(mode="json", exclude={"enabled"})
    values["actor_ids"] = sorted(values["actor_ids"])
    values["call_recipients"] = sorted(values["call_recipients"])
    return digest(values)


def _twiml(draft: ExternalActionDraft) -> str:
    assert draft.call is not None
    response = ET.Element("Response")
    ET.SubElement(response, "Say", {"language": "en-US"}).text = draft.call.message
    ET.SubElement(response, "Hangup")
    return ET.tostring(response, encoding="unicode")


def provider_configuration_reasons(
    definition: ExternalProviderDefinition,
    environ: Mapping[str, str],
) -> tuple[str, ...]:
    reasons = []
    if not definition.enabled:
        reasons.append("Provider is disabled")
    secret = environ.get(definition.credential_env, "")
    if (
        not secret
        or len(secret) > 4096
        or any(ord(char) < 32 or ord(char) == 127 for char in secret)
    ):
        reasons.append("Provider credential is not configured")
    if definition.kind == "twilio":
        if not re.fullmatch(r"AC[0-9a-fA-F]{32}", definition.account_sid or ""):
            reasons.append("Twilio account SID is not configured")
        if not re.fullmatch(r"\+[1-9][0-9]{7,14}", definition.from_number or ""):
            reasons.append("Twilio originating phone number is not configured")
        if definition.endpoint is not None:
            reasons.append("Twilio uses its fixed official endpoint")
    else:
        try:
            parsed = urlsplit(definition.endpoint or "")
            if parsed.port is not None and not 1 <= parsed.port <= 65535:
                raise ValueError
        except ValueError:
            reasons.append("Gateway requires an exact HTTPS service endpoint")
            return tuple(reasons)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
            or "\\" in (definition.endpoint or "")
            or any(ord(char) < 32 for char in (definition.endpoint or ""))
            or any(part in {".", ".."} for part in parsed.path.split("/"))
            or "%" in parsed.path
        ):
            reasons.append("Gateway requires an exact HTTPS service endpoint")
        if not definition.merchant_names or any(
            not key or len(key) > 150 or not value or len(value) > 200
            for key, value in definition.merchant_names.items()
        ):
            reasons.append("Gateway merchant identifiers and names must be allowlisted")
    return tuple(reasons)


class ExternalActionProviders:
    def __init__(self, http: BoundedHTTP) -> None:
        self.http = http

    def reasons(
        self,
        definition: ExternalProviderDefinition,
        draft: ExternalActionDraft,
    ) -> tuple[str, ...]:
        reasons = list(provider_configuration_reasons(definition, self.http.environ))
        if definition.kind == "twilio":
            if draft.kind != "call":
                reasons.append("This provider supports prerecorded outbound phone messages only")
            elif len(_twiml(draft)) > 4000:
                reasons.append("Escaped call text exceeds Twilio's TwiML limit")
            elif (
                definition.call_recipients and draft.recipient_id not in definition.call_recipients
            ):
                reasons.append("This phone number is outside the configured recipient allowlist")
        elif (
            draft.kind == "call"
            or definition.merchant_names.get(draft.recipient_id) != draft.recipient_label
        ):
            reasons.append("The exact merchant identifier and name are not allowlisted")
        return tuple(reasons)

    def _headers(self, definition: ExternalProviderDefinition) -> dict[str, str]:
        secret = self.http.environ.get(definition.credential_env, "")
        if definition.kind == "twilio":
            encoded = base64.b64encode(f"{definition.account_sid}:{secret}".encode()).decode()
            return {
                "Authorization": "Basic " + encoded,
                "Content-Type": "application/x-www-form-urlencoded",
            }
        return {"Authorization": "Bearer " + secret, "Content-Type": "application/json"}

    @staticmethod
    def _gateway_endpoint(definition: ExternalProviderDefinition) -> str:
        return (definition.endpoint or "").rstrip("/")

    def quote(
        self,
        definition: ExternalProviderDefinition,
        request: ExternalQuoteRequest,
    ) -> ExternalActionDraft:
        if definition.kind != "gateway" or request.recipient_id not in definition.merchant_names:
            raise ToolCatalogError(
                "Quote lookup requires a configured allowlisted gateway merchant"
            )
        reasons = provider_configuration_reasons(definition, self.http.environ)
        if reasons:
            raise ToolCatalogError("; ".join(reasons))
        response = self.http.request(
            "POST",
            self._gateway_endpoint(definition) + "/quotes",
            headers=self._headers(definition),
            expected=frozenset({200}),
            content=request.model_dump_json().encode(),
        )
        try:
            draft = ExternalActionDraft.model_validate(response.json(write=False))
        except ValidationError:
            raise ToolExecutionError("Gateway returned an invalid quote") from None
        if (draft.kind, draft.provider_id, draft.recipient_id) != (
            request.kind,
            request.provider_id,
            request.recipient_id,
        ) or self.reasons(definition, draft):
            raise ToolExecutionError(
                "Gateway quote did not match the requested provider and merchant"
            )
        self._no_secret_echo(definition, draft.model_dump_json(), write=False)
        return draft

    def _no_secret_echo(
        self,
        definition: ExternalProviderDefinition,
        value: str,
        *,
        write: bool,
    ) -> None:
        secret = self.http.environ.get(definition.credential_env, "")
        if secret and secret in value:
            raise ToolExecutionError("Provider returned an invalid response", unknown=write)

    def execute(
        self,
        definition: ExternalProviderDefinition,
        action: ExternalActionProposal,
    ) -> ExternalProviderReceipt:
        reasons = self.reasons(definition, action.draft)
        if reasons:
            raise ToolCatalogError("; ".join(reasons))
        headers = self._headers(definition)
        if definition.kind == "twilio":
            assert action.draft.call is not None
            payload = {
                "To": action.draft.recipient_id,
                "From": definition.from_number,
                "Twiml": _twiml(action.draft),
                "Record": "false",
                "Timeout": "30",
                "TimeLimit": str(action.draft.call.max_duration_seconds),
            }
            response = self.http.request(
                "POST",
                f"https://api.twilio.com/2010-04-01/Accounts/{definition.account_sid}/Calls.json",
                headers=headers,
                expected=frozenset({201}),
                write=True,
                content=urlencode(payload).encode(),
            )
            return self._call_receipt(definition, action, response.json(write=True), write=True)
        headers["Idempotency-Key"] = str(action.id)
        response = self.http.request(
            "POST",
            self._gateway_endpoint(definition) + "/commitments",
            headers=headers,
            expected=frozenset({200, 201, 202}),
            write=True,
            content=json.dumps(
                {
                    "action_id": str(action.id),
                    "review_digest": action.review_digest,
                    "household_id": str(action.household_id),
                    "actor_id": str(action.actor_id),
                    "draft": action.draft.model_dump(mode="json"),
                }
            ).encode(),
        )
        return self._gateway_receipt(definition, action, response.json(write=True), write=True)

    def refresh(
        self,
        definition: ExternalProviderDefinition,
        action: ExternalActionProposal,
    ) -> ExternalProviderReceipt:
        if not action.provider_id:
            raise ToolCatalogError(
                "No provider receipt exists; reconcile manually without replaying"
            )
        if definition.kind == "twilio":
            if not re.fullmatch(r"CA[0-9a-fA-F]{32}", action.provider_id):
                raise ToolCatalogError("Invalid stored call identifier")
            url = (
                f"https://api.twilio.com/2010-04-01/Accounts/{definition.account_sid}"
                f"/Calls/{action.provider_id}.json"
            )
        else:
            if not re.fullmatch(r"[A-Za-z0-9_-]{1,200}", action.provider_id):
                raise ToolCatalogError("Invalid stored gateway identifier")
            url = self._gateway_endpoint(definition) + "/commitments/" + action.provider_id
        response = self.http.request(
            "GET", url, headers=self._headers(definition), expected=frozenset({200})
        )
        data = response.json(write=False)
        receipt = (
            self._call_receipt(definition, action, data, write=False)
            if definition.kind == "twilio"
            else self._gateway_receipt(definition, action, data, write=False)
        )
        if receipt.id != action.provider_id:
            raise ToolExecutionError("Provider returned a different receipt")
        return receipt

    def _call_receipt(
        self,
        definition: ExternalProviderDefinition,
        action: ExternalActionProposal,
        value: Any,
        *,
        write: bool,
    ) -> ExternalProviderReceipt:
        if (
            not isinstance(value, dict)
            or not re.fullmatch(r"CA[0-9a-fA-F]{32}", str(value.get("sid", "")))
            or value.get("account_sid") != definition.account_sid
            or value.get("to") != action.draft.recipient_id
            or value.get("from") != definition.from_number
        ):
            raise ToolExecutionError("Twilio returned an invalid call receipt", unknown=write)
        status = value.get("status")
        state: Literal["accepted", "succeeded", "failed"]
        if status in {"queued", "ringing", "in-progress"}:
            state, outcome = "accepted", "Call accepted by Twilio; delivery is not yet confirmed."
        elif status == "completed":
            state, outcome = (
                "succeeded",
                ("Call completed; a human answer or understanding of the message is not verified."),
            )
        elif status in {"busy", "failed", "no-answer", "canceled"}:
            state, outcome = (
                "failed",
                "Call ended without confirmed delivery; check provider billing.",
            )
        else:
            raise ToolExecutionError("Twilio returned an unknown call state", unknown=write)
        return ExternalProviderReceipt(
            id=value["sid"], status=state, provider_status=status, outcome=outcome
        )

    def _gateway_receipt(
        self,
        definition: ExternalProviderDefinition,
        action: ExternalActionProposal,
        value: Any,
        *,
        write: bool,
    ) -> ExternalProviderReceipt:
        try:
            envelope = GatewayReceiptEnvelope.model_validate(value)
        except ValidationError:
            raise ToolExecutionError("Gateway returned an invalid receipt", unknown=write) from None
        if (
            envelope.action_id != action.id
            or not hmac.compare_digest(envelope.review_digest, action.review_digest)
            or not re.fullmatch(r"[A-Za-z0-9_-]{1,200}", envelope.receipt.id)
        ):
            raise ToolExecutionError(
                "Gateway receipt did not match the reviewed action",
                unknown=write,
            )
        self._no_secret_echo(definition, envelope.model_dump_json(), write=write)
        return envelope.receipt
