from __future__ import annotations

from typing import Annotated, Any
from uuid import UUID

from fastapi import Depends, FastAPI, Header, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from jarvis.adapters.memory import InMemoryStore
from jarvis.config import Settings, get_settings
from jarvis.domain.errors import DomainError
from jarvis.domain.models import (
    ActorContext,
    CapabilityDefinition,
    CapabilityInvocation,
    Channel,
    Job,
    RiskClass,
)
from jarvis.services.audit import AuditService
from jarvis.services.capabilities import CapabilityBroker
from jarvis.services.jobs import JobService
from jarvis.services.policy import PolicyEngine


class EchoInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    message: str = Field(min_length=1, max_length=1_000)


class SubmitJobRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: str
    input: dict[str, Any]
    idempotency_key: str = Field(min_length=8, max_length=200)


class AppContainer:
    def __init__(
        self,
        store: InMemoryStore | None = None,
        settings: Settings | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        if self.settings.environment == "production":
            raise RuntimeError(
                "production authentication is not implemented; refusing unsafe startup"
            )
        self.store = store or InMemoryStore()
        self.audit = AuditService(self.store)
        self.policy = PolicyEngine()
        self.capabilities = CapabilityBroker(
            self.store, self.store, self.policy, self.audit
        )
        self.jobs = JobService(self.store, self.audit)
        self.store.register(
            CapabilityDefinition(
                name="system.echo",
                description="Return validated text for end-to-end health checks.",
                risk=RiskClass.READ,
                required_scopes=frozenset({"system:read"}),
                input_schema=EchoInput.model_json_schema(),
                output_schema={
                    "type": "object",
                    "properties": {"message": {"type": "string"}},
                    "required": ["message"],
                    "additionalProperties": False,
                },
            ),
            EchoInput,
            echo,
        )


def echo(value: BaseModel) -> dict[str, Any]:
    validated = EchoInput.model_validate(value)
    return {"message": validated.message}


def actor_from_headers(
    x_actor_id: Annotated[UUID, Header()],
    x_household_id: Annotated[UUID, Header()],
    x_scopes: Annotated[str, Header()] = "",
    x_channel: Annotated[Channel, Header()] = Channel.API,
    x_correlation_id: Annotated[UUID | None, Header()] = None,
) -> ActorContext:
    return ActorContext(
        actor_id=x_actor_id,
        household_id=x_household_id,
        scopes=frozenset(item for item in x_scopes.split() if item),
        channel=x_channel,
        **({"correlation_id": x_correlation_id} if x_correlation_id else {}),
    )


def create_app(container: AppContainer | None = None) -> FastAPI:
    services = container or AppContainer()
    app = FastAPI(title="Jarvis API", version="0.1.0")
    app.state.container = services

    @app.exception_handler(DomainError)
    async def domain_error_handler(_request: Request, exc: DomainError) -> JSONResponse:
        status_by_code = {
            "not_found": 404,
            "forbidden": 403,
            "confirmation_required": 409,
            "idempotency_conflict": 409,
            "invalid_transition": 409,
            "validation_error": 422,
        }
        return JSONResponse(
            status_code=status_by_code.get(exc.code, 400),
            content={"error": {"code": exc.code, "message": str(exc)}},
        )

    @app.get("/health/live")
    def live() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/v1/capabilities", response_model=list[CapabilityDefinition])
    def list_capabilities(
        actor: Annotated[ActorContext, Depends(actor_from_headers)],
    ) -> list[CapabilityDefinition]:
        return [
            definition
            for definition in services.store.list()
            if definition.required_scopes <= actor.scopes
        ]

    @app.post("/v1/capabilities/invoke")
    def invoke_capability(
        invocation: CapabilityInvocation,
        actor: Annotated[ActorContext, Depends(actor_from_headers)],
    ) -> dict[str, Any]:
        return services.capabilities.invoke(actor, invocation).model_dump(mode="json")

    @app.post("/v1/jobs", response_model=Job, status_code=202)
    def submit_job(
        request: SubmitJobRequest,
        actor: Annotated[ActorContext, Depends(actor_from_headers)],
    ) -> Job:
        job, _created = services.jobs.submit(
            actor,
            kind=request.kind,
            input=request.input,
            idempotency_key=request.idempotency_key,
        )
        return job

    return app


app = create_app()
