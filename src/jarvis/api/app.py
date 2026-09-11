from pathlib import Path
from typing import Annotated, Any
from urllib.parse import urlsplit
from uuid import UUID

from fastapi import Depends, FastAPI, Header, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import (
    FileResponse,
    JSONResponse,
    RedirectResponse,
    Response,
    StreamingResponse,
)
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field
from starlette.middleware.trustedhost import TrustedHostMiddleware

from jarvis.adapters.memory import InMemoryStore
from jarvis.api.auth import auth_router, require_csrf, session_cookie
from jarvis.config import Settings, get_settings
from jarvis.domain.context import CreateMemory, ExplicitMemory
from jarvis.domain.conversations import CreateThread, Message, Run, SubmitRun, Thread
from jarvis.domain.errors import AuthorizationError, DomainError, ModelError
from jarvis.domain.identity import DEV_ACTOR_ID, DEV_HOUSEHOLD_ID, Membership
from jarvis.domain.models import (
    ActorContext,
    CapabilityDefinition,
    CapabilityInvocation,
    Job,
    RiskClass,
)
from jarvis.domain.ports import Store
from jarvis.services.audit import AuditService
from jarvis.services.capabilities import CapabilityBroker
from jarvis.services.conversations import ConversationService
from jarvis.services.identity import IdentityService
from jarvis.services.jobs import JobService
from jarvis.services.memory import MemoryService
from jarvis.services.model_conversations import ModelConversationService
from jarvis.services.policy import PolicyEngine


class EchoInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    message: str = Field(min_length=1, max_length=1_000)


class SubmitJobRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: str = Field(pattern=r"^[a-z][a-z0-9_.-]{2,127}$")
    input: dict[str, Any]
    idempotency_key: str = Field(min_length=8, max_length=200)


class AppContainer:
    def __init__(
        self,
        store: Store | None = None,
        settings: Settings | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.store: Store
        if store is not None:
            self.store = store
        elif self.settings.storage_backend == "postgres":
            from jarvis.adapters.postgres import PostgresStore

            self.store = PostgresStore(self.settings.database_url.get_secret_value())
        else:
            self.store = InMemoryStore()
        if self.settings.dev_login_enabled and self.settings.storage_backend == "memory":
            self.store.put_membership(
                Membership(
                    actor_id=DEV_ACTOR_ID,
                    household_id=DEV_HOUSEHOLD_ID,
                    role="owner",
                    display_name="Development user",
                    household_name="Development household",
                )
            )
        self.identity = IdentityService(self.store, self.settings)
        self.audit = AuditService(self.store)
        self.policy = PolicyEngine()
        self.capabilities = CapabilityBroker(self.store, self.store, self.policy, self.audit)
        self.jobs = JobService(self.store, self.audit)
        self.conversations = ConversationService(self.store, self.audit)
        if self.settings.model_provider == "openai":
            from jarvis.adapters.openai_model import OpenAIModel

            assert self.settings.openai_api_key is not None
            self.conversations = ModelConversationService(
                self.store,
                self.audit,
                OpenAIModel(self.settings.openai_api_key.get_secret_value()),
                self.settings,
            )
        self.memories = MemoryService(self.store, self.audit)
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


def create_app(container: AppContainer | None = None) -> FastAPI:
    services = container or AppContainer()
    app = FastAPI(title="Jarvis API", version="0.1.0")
    app.state.container = services

    hostname = urlsplit(services.settings.public_origin).hostname
    assert hostname is not None
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=[hostname])
    app.include_router(auth_router(services.identity))
    static = Path(__file__).parent / "static"
    app.mount("/assets", StaticFiles(directory=static), name="assets")

    @app.get("/", include_in_schema=False)
    def home() -> RedirectResponse:
        return RedirectResponse("/login")

    @app.get("/login", include_in_schema=False)
    def login_page() -> FileResponse:
        return FileResponse(static / "login.html")

    @app.get("/chat", include_in_schema=False)
    def chat_page() -> FileResponse:
        return FileResponse(static / "chat.html")

    @app.middleware("http")
    async def security_headers(request: Request, call_next: Any) -> Any:
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Frame-Options"] = "DENY"
        if request.url.path in {"/login", "/chat"} or request.url.path.startswith("/assets/"):
            response.headers["Content-Security-Policy"] = (
                "default-src 'self'; frame-ancestors 'none'; object-src 'none'; base-uri 'none'"
            )
        return response

    def checked_actor(
        request: Request,
        x_csrf_token: Annotated[str | None, Header()] = None,
    ) -> ActorContext:
        token = request.cookies.get(session_cookie(services.identity), "")
        _session, actor = services.identity.resolve(token)
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            require_csrf(request, services.identity, token)
        return actor

    @app.exception_handler(RequestValidationError)
    async def request_validation_error(
        _request: Request, _exc: RequestValidationError
    ) -> JSONResponse:
        return JSONResponse(
            status_code=422,
            content={
                "error": {"code": "validation_error", "message": "request fields are invalid"}
            },
        )

    @app.exception_handler(DomainError)
    async def domain_error_handler(_request: Request, exc: DomainError) -> JSONResponse:
        status_by_code = {
            "unauthenticated": 401,
            "not_found": 404,
            "forbidden": 403,
            "confirmation_required": 409,
            "idempotency_conflict": 409,
            "invalid_transition": 409,
            "validation_error": 422,
            "model_error": 503,
            "model_busy": 409,
        }
        return JSONResponse(
            status_code=status_by_code.get(exc.code, 400),
            content={
                "error": {
                    "code": exc.code,
                    "message": str(exc),
                    **({"reason": exc.reason} if isinstance(exc, ModelError) else {}),
                }
            },
        )

    @app.get("/health/live")
    def live() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/v1/capabilities", response_model=list[CapabilityDefinition])
    def list_capabilities(
        actor: Annotated[ActorContext, Depends(checked_actor)],
    ) -> list[CapabilityDefinition]:
        return [
            definition
            for definition in services.store.list()
            if definition.required_scopes <= actor.scopes
        ]

    @app.post("/v1/capabilities/invoke")
    def invoke_capability(
        invocation: CapabilityInvocation,
        actor: Annotated[ActorContext, Depends(checked_actor)],
    ) -> dict[str, Any]:
        return services.capabilities.invoke(actor, invocation).model_dump(mode="json")

    @app.post("/v1/jobs", response_model=Job, status_code=202)
    def submit_job(
        request: SubmitJobRequest,
        actor: Annotated[ActorContext, Depends(checked_actor)],
    ) -> Job:
        if "jobs:write" not in actor.scopes:
            raise AuthorizationError("missing required scopes: jobs:write")
        job, _created = services.jobs.submit(
            actor,
            kind=request.kind,
            input=request.input,
            idempotency_key=request.idempotency_key,
        )
        return job

    @app.get("/v1/jobs/{job_id}", response_model=Job)
    def get_job(
        job_id: UUID,
        actor: Annotated[ActorContext, Depends(checked_actor)],
    ) -> Job:
        if "jobs:read" not in actor.scopes:
            raise AuthorizationError("missing required scopes: jobs:read")
        return services.jobs.get(actor, job_id)

    @app.post("/v1/threads", response_model=Thread, status_code=201)
    def create_thread(
        request: CreateThread, actor: Annotated[ActorContext, Depends(checked_actor)]
    ) -> Thread:
        return services.conversations.create(actor, request)

    @app.get("/v1/threads", response_model=list[Thread])
    def threads(
        actor: Annotated[ActorContext, Depends(checked_actor)],
        offset: Annotated[int, Query(ge=0)] = 0,
        limit: Annotated[int, Query(ge=1, le=100)] = 50,
    ) -> tuple[Thread, ...]:
        return services.conversations.list(actor, offset, limit)

    @app.get("/v1/threads/{thread_id}", response_model=Thread)
    def thread(thread_id: UUID, actor: Annotated[ActorContext, Depends(checked_actor)]) -> Thread:
        return services.conversations.get(actor, thread_id)

    @app.get("/v1/threads/{thread_id}/messages", response_model=list[Message])
    def messages(
        thread_id: UUID,
        actor: Annotated[ActorContext, Depends(checked_actor)],
        after: Annotated[int, Query(ge=0)] = 0,
        limit: Annotated[int, Query(ge=1, le=100)] = 100,
    ) -> tuple[Message, ...]:
        return services.conversations.messages(actor, thread_id, after, limit)

    @app.post("/v1/threads/{thread_id}/runs", response_model=Run, status_code=201)
    def submit_run(
        thread_id: UUID,
        request: SubmitRun,
        actor: Annotated[ActorContext, Depends(checked_actor)],
        http_request: Request,
    ) -> Run:
        if isinstance(services.conversations, ModelConversationService):
            return services.conversations.submit(
                actor, thread_id, request, revalidate=lambda: checked_actor(http_request)
            )
        return services.conversations.submit(actor, thread_id, request)

    @app.get("/v1/runs/{run_id}", response_model=Run)
    def run(run_id: UUID, actor: Annotated[ActorContext, Depends(checked_actor)]) -> Run:
        return services.conversations.run(actor, run_id)

    @app.get("/v1/runs/{run_id}/events", response_class=StreamingResponse)
    def run_events(
        run_id: UUID,
        actor: Annotated[ActorContext, Depends(checked_actor)],
        after: Annotated[int, Query(ge=0)] = 0,
        last_event_id: Annotated[int | None, Header(ge=0)] = None,
    ) -> Response:
        events = services.conversations.events(
            actor, run_id, last_event_id if last_event_id is not None else after
        )
        if not events:
            return Response(status_code=204)
        return StreamingResponse(
            iter(
                f"id: {e.sequence}\nevent: {e.event_type}\ndata: {e.model_dump_json()}\n\n"
                for e in events
            ),
            media_type="text/event-stream",
            headers={"X-Accel-Buffering": "no"},
        )

    @app.get("/v1/memories", response_model=list[ExplicitMemory])
    def memories(
        actor: Annotated[ActorContext, Depends(checked_actor)],
        offset: Annotated[int, Query(ge=0)] = 0,
        limit: Annotated[int, Query(ge=1, le=100)] = 100,
    ) -> tuple[ExplicitMemory, ...]:
        return services.memories.list(actor, offset, limit)

    @app.post("/v1/memories", response_model=ExplicitMemory, status_code=201)
    def create_memory(
        request: CreateMemory, actor: Annotated[ActorContext, Depends(checked_actor)]
    ) -> ExplicitMemory:
        return services.memories.create(actor, request)

    @app.post("/v1/memories/{memory_id}/retract", response_model=ExplicitMemory)
    def retract_memory(
        memory_id: UUID, actor: Annotated[ActorContext, Depends(checked_actor)]
    ) -> ExplicitMemory:
        return services.memories.retract(actor, memory_id)

    @app.get("/v1/assistant")
    def assistant_status(
        actor: Annotated[ActorContext, Depends(checked_actor)],
    ) -> dict[str, object]:
        services.conversations.authorize(actor, "threads:read")
        return {
            "provider": services.settings.model_provider,
            "model": services.settings.openai_model
            if services.settings.model_provider == "openai"
            else "deterministic-echo-v1",
        }

    return app


app = create_app()
