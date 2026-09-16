import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from datetime import timedelta
from pathlib import Path
from typing import Annotated, Any
from urllib.parse import urlsplit
from uuid import UUID, uuid4

from fastapi import Depends, FastAPI, Header, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import (
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
    Response,
    StreamingResponse,
)
from fastapi.staticfiles import StaticFiles
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field
from starlette.middleware.trustedhost import TrustedHostMiddleware

from simon.adapters.memory import InMemoryStore
from simon.api.auth import auth_router, require_csrf, session_cookie
from simon.api.model_stream import model_stream
from simon.config import Settings, get_settings
from simon.domain.connected_tools import ActionProposal, GoogleStart
from simon.domain.context import CreateMemory, ExplicitMemory
from simon.domain.conversations import CreateThread, Message, Run, SubmitRun, Thread
from simon.domain.errors import AuthorizationError, DomainError, ModelError
from simon.domain.home import DirectHomeControl, HomeOrganization, OutletPower, OutletSetup
from simon.domain.identity import DEV_ACTOR_ID, DEV_HOUSEHOLD_ID, Membership
from simon.domain.interaction import (
    AnswerReference,
    ResponsePreferences,
    RunFeedback,
    SaveFeedback,
    SavePreferences,
)
from simon.domain.models import (
    ActorContext,
    CapabilityDefinition,
    CapabilityInvocation,
    Channel,
    Job,
    RiskClass,
    utc_now,
)
from simon.domain.ports import Store
from simon.domain.voice import VoiceOffer
from simon.services.audit import AuditService
from simon.services.capabilities import CapabilityBroker
from simon.services.connected import ConnectedService
from simon.services.conversations import ConversationService
from simon.services.identity import IdentityService
from simon.services.interaction import InteractionService
from simon.services.jobs import JobService
from simon.services.memory import MemoryService
from simon.services.model_conversations import ModelConversationService
from simon.services.policy import PolicyEngine
from simon.services.power import PowerMonitor
from simon.services.voice import VoiceService


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
            from simon.adapters.postgres import PostgresStore

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
        self.connected = ConnectedService(self.store, self.audit, self.settings, self.identity)
        self.power = PowerMonitor(self.connected.home)
        self.interaction = InteractionService(self.store, self.audit)
        self.policy = PolicyEngine()
        self.capabilities = CapabilityBroker(self.store, self.store, self.policy, self.audit)
        self.jobs = JobService(self.store, self.audit)
        self.conversations = ConversationService(self.store, self.audit)
        if self.settings.model_provider == "openai":
            from simon.adapters.openai_model import OpenAIModel

            assert self.settings.openai_api_key is not None
            self.conversations = ModelConversationService(
                self.store,
                self.audit,
                OpenAIModel(self.settings.openai_api_key.get_secret_value()),
                self.settings,
                self.connected,
            )
        self.memories = MemoryService(self.store, self.audit)
        self.voice = VoiceService(
            self.connected,
            self.conversations
            if isinstance(self.conversations, ModelConversationService)
            else None,
        )
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

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        async def discovery() -> None:
            while True:
                try:
                    await asyncio.to_thread(services.connected.home.catalog.sync_target)
                except Exception:
                    logging.getLogger(__name__).warning(
                        "Home inventory refresh unavailable; retrying later."
                    )
                await asyncio.sleep(300)

        task = asyncio.create_task(discovery()) if services.settings.home_auto_discovery else None

        async def metering() -> None:
            target = services.connected.home.catalog.target
            if not target:
                return
            actor = ActorContext(
                actor_id=UUID("00000000-0000-4000-8000-000000000002"),
                household_id=target,
                channel=Channel.WORKER,
                scopes=frozenset({"home:read"}),
            )
            while True:
                try:
                    await asyncio.to_thread(services.power.poll, actor)
                except Exception:
                    logging.getLogger(__name__).warning(
                        "Outlet metering unavailable; retrying later."
                    )
                await asyncio.sleep(services.settings.power_poll_seconds)

        meter_task = (
            asyncio.create_task(metering()) if services.settings.power_monitoring_enabled else None
        )
        try:
            yield
        finally:
            await services.voice.shutdown()
            if task:
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task
            if meter_task:
                meter_task.cancel()
                with suppress(asyncio.CancelledError):
                    await meter_task

    app = FastAPI(title="Simon API", version="0.1.0", lifespan=lifespan)
    app.state.container = services

    hostname = urlsplit(services.settings.public_origin).hostname
    assert hostname is not None
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=[hostname])
    app.include_router(auth_router(services.identity))
    static = Path(__file__).parent / "static"
    app.mount("/assets", StaticFiles(directory=static), name="assets")

    @app.get("/", include_in_schema=False)
    def home() -> RedirectResponse:
        return RedirectResponse(services.settings.public_path + "/login")

    def page(name: str) -> HTMLResponse:
        base = services.settings.public_path
        markup = (static / name).read_text(encoding="utf-8")
        markup = markup.replace("<head>", '<head><meta name="simon-base" content="' + base + '">')
        for attribute in ('href="/', 'src="/'):
            markup = markup.replace(attribute, attribute[:-1] + base + "/")
        return HTMLResponse(markup)

    @app.get("/login", include_in_schema=False)
    def login_page() -> HTMLResponse:
        return page("login.html")

    @app.get("/chat", include_in_schema=False)
    def chat_page() -> HTMLResponse:
        return page("chat.html")

    @app.middleware("http")
    async def security_headers(request: Request, call_next: Any) -> Any:
        path = request.url.path.removeprefix(services.settings.public_path)
        callback = path == "/auth/google/callback"
        try:
            response = await call_next(request)
        finally:
            if callback:
                # OAuth codes must not appear in Uvicorn access logs, even on errors.
                request.scope["query_string"] = b""
        response.headers["Cache-Control"] = "no-store"
        response.headers["CDN-Cache-Control"] = "no-store"
        response.headers["Vercel-CDN-Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Frame-Options"] = "DENY"
        if path in {"/login", "/chat"} or path.startswith("/assets/"):
            response.headers["Content-Security-Policy"] = (
                "default-src 'self'; frame-ancestors 'none'; object-src 'none'; base-uri 'none'"
                "; media-src 'self' blob:; connect-src 'self'"
            )
        response.headers["Permissions-Policy"] = "microphone=(self), camera=(), geolocation=()"
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

    @app.get("/v1/voice")
    def voice_config(actor: Annotated[ActorContext, Depends(checked_actor)]) -> dict[str, Any]:
        services.conversations.authorize(actor, "threads:read")
        records = services.store.voice_sessions(actor.household_id, actor.actor_id)
        return {
            "enabled": services.voice.enabled,
            "max_seconds": services.settings.voice_max_seconds,
            "sessions": [
                r.model_dump(mode="json", exclude={"provider_id", "fragments", "request_key"})
                for r in records
            ],
        }

    @app.post("/v1/voice/sessions")
    async def voice_start(
        body: VoiceOffer, request: Request, actor: Annotated[ActorContext, Depends(checked_actor)]
    ) -> dict[str, Any]:
        token = request.cookies.get(session_cookie(services.identity), "")
        return await services.voice.start(actor, token, body)

    @app.get("/v1/voice/sessions/{identifier}")
    def voice_session(
        identifier: UUID, actor: Annotated[ActorContext, Depends(checked_actor)]
    ) -> dict[str, Any]:
        record = services.voice.get(actor, identifier)
        return record.model_dump(mode="json", exclude={"provider_id", "request_key"})

    @app.post("/v1/voice/sessions/{identifier}/heartbeat")
    async def voice_heartbeat(
        identifier: UUID, actor: Annotated[ActorContext, Depends(checked_actor)]
    ) -> dict[str, str]:
        services.voice.get(actor, identifier)
        if running := services.voice.active.get(identifier):
            from time import monotonic

            running.last_heartbeat = monotonic()
        return {"status": "ok"}

    @app.post("/v1/voice/sessions/{identifier}/stop-task")
    async def voice_stop_task(
        identifier: UUID, actor: Annotated[ActorContext, Depends(checked_actor)]
    ) -> dict[str, str]:
        services.voice.get(actor, identifier)
        if running := services.voice.active.get(identifier):
            await services.voice.stop_work(running)
            services.voice.save(
                running, backend_status="Task stopped; already sent actions may have completed."
            )
        return {"status": "stopped"}

    @app.post("/v1/voice/sessions/{identifier}/close")
    async def voice_close(
        identifier: UUID, actor: Annotated[ActorContext, Depends(checked_actor)]
    ) -> dict[str, str]:
        services.voice.get(actor, identifier)
        if running := services.voice.active.get(identifier):
            await services.voice.close(running)
        return {"status": "closed"}

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

    @app.get("/v1/threads/{thread_id}/latest-run", response_model=Run | None)
    def latest_run(
        thread_id: UUID, actor: Annotated[ActorContext, Depends(checked_actor)]
    ) -> Run | None:
        services.conversations.get(actor, thread_id)
        latest = services.store.latest_run(thread_id)
        return services.conversations.run(actor, latest.id) if latest else None

    @app.post("/v1/threads/{thread_id}/runs/stream", response_class=StreamingResponse)
    def stream_run(
        thread_id: UUID,
        request: SubmitRun,
        http_request: Request,
        actor: Annotated[ActorContext, Depends(checked_actor)],
    ) -> StreamingResponse:
        services.conversations.get(actor, thread_id)
        services.conversations.authorize(actor, "threads:write")
        if not isinstance(services.conversations, ModelConversationService):
            raise AuthorizationError("live streaming requires OpenAI mode")
        return model_stream(
            services.conversations, actor, thread_id, request, lambda: checked_actor(http_request)
        )

    @app.post("/v1/runs/{run_id}/cancel", status_code=204)
    def cancel_run(
        run_id: UUID, actor: Annotated[ActorContext, Depends(checked_actor)]
    ) -> Response:
        if not isinstance(services.conversations, ModelConversationService):
            raise AuthorizationError("cancellation requires OpenAI mode")
        services.conversations.cancel(actor, run_id)
        return Response(status_code=204)

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

    @app.get("/v1/preferences", response_model=ResponsePreferences)
    def response_preferences(
        actor: Annotated[ActorContext, Depends(checked_actor)],
    ) -> ResponsePreferences:
        return services.interaction.preferences(actor)

    @app.post("/v1/preferences", response_model=ResponsePreferences)
    def save_preferences(
        request: SavePreferences, actor: Annotated[ActorContext, Depends(checked_actor)]
    ) -> ResponsePreferences:
        return services.interaction.save_preferences(actor, request)

    @app.get("/v1/threads/{thread_id}/answers", response_model=list[AnswerReference])
    def answer_references(
        thread_id: UUID,
        actor: Annotated[ActorContext, Depends(checked_actor)],
        offset: Annotated[int, Query(ge=0)] = 0,
        limit: Annotated[int, Query(ge=1, le=100)] = 100,
    ) -> tuple[AnswerReference, ...]:
        return services.interaction.answers(actor, thread_id, offset, limit)

    @app.post("/v1/runs/{run_id}/feedback", response_model=RunFeedback)
    def save_feedback(
        run_id: UUID, request: SaveFeedback, actor: Annotated[ActorContext, Depends(checked_actor)]
    ) -> RunFeedback:
        return services.interaction.save_feedback(actor, run_id, request)

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
            "profiles": ["auto", "quick", "balanced", "deep"],
            "answer_lengths": ["auto", "brief", "normal", "detailed"],
            "auto_deep_enabled": services.settings.auto_deep_enabled,
            "web_search": services.settings.model_provider == "openai"
            and services.settings.web_search_enabled,
        }

    @app.get("/v1/connections/google")
    def google_status(actor: Annotated[ActorContext, Depends(checked_actor)]) -> dict[str, object]:
        return services.connected.status(actor)

    @app.get("/v1/home/commands")
    def home_commands(
        actor: Annotated[ActorContext, Depends(checked_actor)],
    ) -> list[dict[str, object]]:
        return [c.model_dump(mode="json") for c in services.connected.home.commands(actor)]

    @app.get("/v1/home/devices")
    def home_devices(
        actor: Annotated[ActorContext, Depends(checked_actor)],
    ) -> list[dict[str, object]]:
        return services.connected.home.inventory(actor)

    @app.post("/v1/home/outlets/{device_id}/setup")
    def setup_outlet(
        device_id: str,
        body: OutletSetup,
        request: Request,
        actor: Annotated[ActorContext, Depends(checked_actor)],
    ) -> dict[str, object]:
        from simon.services.identity import IDENTITY_LOCK

        with (
            services.store.transaction(IDENTITY_LOCK),
            services.store.transaction(actor.household_id),
        ):
            current = checked_actor(request)
            if (current.actor_id, current.household_id) != (actor.actor_id, actor.household_id):
                raise AuthorizationError("Home access changed")
            device = services.connected.home.setup_outlet(current, device_id, body)
            return {
                "device_id": device.id,
                "name": device.name,
                "room": device.room,
                "load_type": device.load_type,
                "control_enabled": device.control_enabled,
            }

    @app.get("/v1/home/outlets")
    def outlets(actor: Annotated[ActorContext, Depends(checked_actor)]) -> list[dict[str, object]]:
        return [
            {
                "id": d.id,
                "name": d.name,
                "room": d.room,
                "groups": list(d.groups),
                "address": str(d.address),
                "remote_id": d.remote_id,
                "load_type": d.load_type,
                "control_enabled": d.control_enabled,
            }
            for d in services.power.devices(actor)
        ]

    @app.post("/v1/home/outlets/{device_id}/power")
    def outlet_power(
        device_id: str,
        body: OutletPower,
        request: Request,
        actor: Annotated[ActorContext, Depends(checked_actor)],
    ) -> dict[str, object]:
        return services.connected.home.outlet_power(
            actor, device_id, body, lambda: checked_actor(request)
        ).model_dump(mode="json")

    @app.post("/v1/home/devices/{device_id}/control")
    def home_control(
        device_id: str,
        body: DirectHomeControl,
        request: Request,
        actor: Annotated[ActorContext, Depends(checked_actor)],
    ) -> dict[str, object]:
        return services.connected.home.direct_control(
            actor, device_id, body, lambda: checked_actor(request)
        ).model_dump(mode="json")

    @app.get("/v1/home/power")
    def power_overview(actor: Annotated[ActorContext, Depends(checked_actor)]) -> dict[str, object]:
        return services.power.overview(actor)

    @app.post("/v1/home/power/refresh")
    def refresh_power(actor: Annotated[ActorContext, Depends(checked_actor)]) -> dict[str, object]:
        services.power.poll(actor)
        return services.power.overview(actor)

    @app.get("/v1/home/devices/{device_id}/power")
    def power_history(
        device_id: str,
        actor: Annotated[ActorContext, Depends(checked_actor)],
        start: AwareDatetime | None = None,
        end: AwareDatetime | None = None,
    ) -> dict[str, object]:
        finish = end or utc_now()
        return services.power.history(
            actor, device_id, start or finish - timedelta(hours=24), finish
        )

    @app.get("/v1/home/discovery")
    def home_discovery(
        actor: Annotated[ActorContext, Depends(checked_actor)],
    ) -> list[dict[str, object]]:
        return services.connected.home.catalog.status(actor)

    @app.post("/v1/home/discovery/refresh")
    def refresh_home(actor: Annotated[ActorContext, Depends(checked_actor)]) -> dict[str, object]:
        return {
            "providers": services.connected.home.catalog.sync(actor, force=True),
            "devices": services.connected.home.inventory(actor),
        }

    @app.post("/v1/home/organize")
    def organize_home(
        body: HomeOrganization,
        request: Request,
        actor: Annotated[ActorContext, Depends(checked_actor)],
    ) -> dict[str, object]:
        from simon.services.identity import IDENTITY_LOCK

        with services.store.transaction(IDENTITY_LOCK):
            current = checked_actor(request)
            if (current.actor_id, current.household_id) != (actor.actor_id, actor.household_id):
                raise AuthorizationError("Home access changed")
            return services.connected.home.catalog.organize(
                current,
                body,
                services.connected.home.devices,
                operation_key=str(uuid4()),
            )

    @app.get("/v1/home/devices/{device_id}/status")
    def home_status(
        device_id: str, actor: Annotated[ActorContext, Depends(checked_actor)]
    ) -> dict[str, object]:
        return services.connected.home.read(actor, device_id).model_dump(mode="json")

    @app.post("/v1/connections/google/start")
    def google_start(
        body: GoogleStart,
        request: Request,
        actor: Annotated[ActorContext, Depends(checked_actor)],
    ) -> JSONResponse:
        url, binding = services.connected.start(
            actor,
            request.cookies.get(session_cookie(services.identity), ""),
        )
        response = JSONResponse({"url": url})
        response.set_cookie(
            "simon_google_binding",
            binding,
            max_age=300,
            httponly=True,
            secure=services.settings.secure_cookies,
            samesite="lax",
            path=services.settings.public_path + "/auth/google/callback",
        )
        return response

    @app.get("/auth/google/callback")
    def google_callback(
        request: Request,
        state: Annotated[str, Query(max_length=200)] = "",
        code: Annotated[str, Query(max_length=4096)] = "",
    ) -> RedirectResponse:
        outcome = "failed"
        try:
            if state and code:
                services.connected.callback(
                    state, request.cookies.get("simon_google_binding", ""), code
                )
                outcome = "connected"
        except DomainError:
            pass  # Do not reflect provider data or credentials into browser history.
        response = RedirectResponse(
            services.settings.public_path + "/chat?google=" + outcome, status_code=303
        )
        response.delete_cookie(
            "simon_google_binding",
            path=services.settings.public_path + "/auth/google/callback",
            secure=services.settings.secure_cookies,
            httponly=True,
            samesite="lax",
        )
        return response

    @app.post("/v1/connections/google/disconnect", status_code=204)
    def google_disconnect(actor: Annotated[ActorContext, Depends(checked_actor)]) -> Response:
        services.connected.disconnect(actor)
        return Response(status_code=204)

    @app.get("/v1/actions/{action_id}", response_model=ActionProposal)
    def action_status(
        action_id: UUID, actor: Annotated[ActorContext, Depends(checked_actor)]
    ) -> ActionProposal:
        return services.connected.get_action(actor, action_id)

    @app.post("/v1/actions/{action_id}/confirm", response_model=ActionProposal)
    def confirm_action(
        action_id: UUID, request: Request, actor: Annotated[ActorContext, Depends(checked_actor)]
    ) -> ActionProposal:
        return services.connected.decide(
            actor, action_id, confirm=True, revalidate=lambda: checked_actor(request)
        )

    @app.post("/v1/actions/{action_id}/cancel", response_model=ActionProposal)
    def cancel_action(
        action_id: UUID, request: Request, actor: Annotated[ActorContext, Depends(checked_actor)]
    ) -> ActionProposal:
        return services.connected.decide(
            actor, action_id, confirm=False, revalidate=lambda: checked_actor(request)
        )

    if services.settings.public_path:
        root = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
        root.state.container = services
        root.mount(services.settings.public_path, app)
        return root
    return app


app = create_app()
