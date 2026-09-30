"""Deterministic model selection without network probes or model-name assumptions."""

from __future__ import annotations

import os
from collections.abc import Mapping, Sequence

from simon.domain.model_routing import (
    EndpointRejection,
    ModelEndpoint,
    ModelTier,
    RoutingDecision,
    RoutingRequest,
)

_TIERS = {"economy": 0, "standard": 1, "frontier": 2}
_EFFORTS = ("none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra")


class ModelRoutingError(ValueError):
    def __init__(
        self,
        code: str,
        message: str,
        rejections: tuple[EndpointRejection, ...] = (),
    ) -> None:
        super().__init__(message)
        self.code = code
        self.rejections = rejections


class ModelRouter:
    def __init__(
        self,
        endpoints: Sequence[ModelEndpoint],
        *,
        environ: Mapping[str, str] | None = None,
    ) -> None:
        self.endpoints = tuple(endpoints)
        if len({item.id for item in endpoints}) != len(endpoints):
            raise ValueError("Model endpoint IDs must be unique")
        self._environ = os.environ if environ is None else environ

    def route(self, request: RoutingRequest) -> RoutingDecision:
        required_tier = self.minimum_tier(request)
        rejections: list[EndpointRejection] = []
        eligible: list[ModelEndpoint] = []
        for endpoint in self.endpoints:
            reasons = self._rejections(endpoint, request, required_tier)
            if reasons:
                rejections.append(
                    EndpointRejection(endpoint_id=endpoint.id, reasons=tuple(reasons))
                )
            else:
                eligible.append(endpoint)
        if request.model_override is not None:
            selected = next((item for item in eligible if item.id == request.model_override), None)
            if selected is None:
                raise ModelRoutingError(
                    "model_override_unavailable",
                    "The requested endpoint is unknown or violates the routing constraints",
                    tuple(rejections),
                )
        else:
            selected = min(eligible, key=lambda item: self._rank(item, request), default=None)
        if selected is None:
            raise ModelRoutingError(
                "no_eligible_model",
                f"No configured model satisfies the {required_tier} quality floor and constraints",
                tuple(rejections),
            )
        alternatives = sorted(
            (item for item in eligible if item.id != selected.id),
            key=lambda item: self._rank(item, request),
        )
        mode = "Manual selection" if request.model_override else "Automatic selection"
        return RoutingDecision(
            endpoint_id=selected.id,
            model=selected.model,
            provider=selected.provider,
            tier=selected.tier,
            local=selected.local,
            reasoning_effort=self._effort(selected, request),
            reason=(
                f"{mode}: depth {request.depth}/5 and importance {request.importance}/5 "
                f"require at least {required_tier}; capabilities, privacy, context, credentials, "
                "and estimated budget checked"
            ),
            estimated_cost_usd=self.estimate_cost(selected, request),
            alternative_endpoint_ids=tuple(item.id for item in alternatives),
            rejections=tuple(rejections),
            request=request,
        )

    @staticmethod
    def minimum_tier(request: RoutingRequest) -> ModelTier:
        demand = max(request.depth, request.importance)
        return "frontier" if demand >= 4 else "standard" if demand == 3 else "economy"

    @staticmethod
    def estimate_cost(endpoint: ModelEndpoint, request: RoutingRequest) -> float | None:
        incoming = endpoint.input_cost_per_million_usd
        outgoing = endpoint.output_cost_per_million_usd
        if incoming is None or outgoing is None:
            return 0.0 if endpoint.local and incoming is None and outgoing is None else None
        return (request.input_tokens * incoming + request.output_tokens * outgoing) / 1_000_000

    def _rejections(
        self, endpoint: ModelEndpoint, request: RoutingRequest, minimum_tier: ModelTier
    ) -> list[str]:
        reasons: list[str] = []
        if not endpoint.enabled:
            reasons.append("disabled")
        if endpoint.api_key_env and not self._environ.get(endpoint.api_key_env, "").strip():
            reasons.append("credentials_unavailable")
        if request.privacy == "local_only" and not endpoint.local:
            reasons.append("privacy_requires_local")
        if not request.required_capabilities <= endpoint.capabilities:
            reasons.append("missing_capabilities")
        if request.input_tokens + request.output_tokens > endpoint.context_window_tokens:
            reasons.append("context_window_exceeded")
        if request.output_tokens > endpoint.max_output_tokens:
            reasons.append("output_limit_exceeded")
        if _TIERS[endpoint.tier] < _TIERS[minimum_tier]:
            reasons.append("below_quality_floor")
        cost = self.estimate_cost(endpoint, request)
        if request.budget_usd is not None:
            if cost is None:
                reasons.append("pricing_unknown")
            elif cost > request.budget_usd:
                reasons.append("estimated_budget_exceeded")
        return reasons

    def _rank(
        self, endpoint: ModelEndpoint, request: RoutingRequest
    ) -> tuple[int, int, int, float, str]:
        cost = self.estimate_cost(endpoint, request)
        return (
            _TIERS[endpoint.tier],
            0 if endpoint.local else 1,
            endpoint.priority,
            float("inf") if cost is None else cost,
            endpoint.id,
        )

    @staticmethod
    def _effort(endpoint: ModelEndpoint, request: RoutingRequest) -> str | None:
        if not endpoint.reasoning_efforts:
            return None
        desired = {1: "low", 2: "low", 3: "medium", 4: "high", 5: "ultra"}[
            max(request.depth, request.importance)
        ]
        supported = sorted(endpoint.reasoning_efforts, key=_EFFORTS.index)
        return next(
            (effort for effort in supported if _EFFORTS.index(effort) >= _EFFORTS.index(desired)),
            supported[-1],
        )
