"""Compose scoped services, HTTP routes, and process-owned background resources."""

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from typing import Annotated, Any
from urllib.parse import urlsplit
from uuid import UUID

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
from pydantic import BaseModel, ConfigDict, Field
from starlette.middleware.trustedhost import TrustedHostMiddleware

from simon.adapters.account_email import AccountEmail
from simon.adapters.external_action_binding import (
    external_action_service,
    external_tool_status,
    external_transport_factory,
)
from simon.adapters.memory import InMemoryStore
from simon.adapters.native_tools import (
    native_tool_status,
    native_transport_factory,
    with_native_tools,
)
from simon.adapters.project_board_binding import project_board_service
from simon.adapters.project_board_tools import (
    project_board_tool_status,
    project_board_transport_factory,
)
from simon.adapters.project_journal_tools import project_journal_transport_factory
from simon.adapters.project_output_tools import project_output_transport_factory
from simon.adapters.project_runtime_tools import with_project_runtime_tools
from simon.adapters.project_storage_tools import project_storage_transport_factory
from simon.adapters.project_work_tools import project_transport_factory
from simon.adapters.tool_preflight import INSTALLED_TRANSPORTS
from simon.api.agent_platform import agent_platform_router
from simon.api.agent_setup_assistant import agent_setup_assistant_router
from simon.api.auth import auth_router, require_csrf, session_cookie
from simon.api.external_actions import external_actions_router
from simon.api.integrations import integrations_router
from simon.api.local_files import local_file_router
from simon.api.model_stream import model_stream
from simon.api.project_boards import project_boards_router
from simon.api.project_command import project_command_router
from simon.api.project_files import project_router
from simon.api.project_outputs import project_outputs_router
from simon.api.project_workspace import project_workspace_router
from simon.api.request_ingress import RequestIngressMiddleware
from simon.api.tasks import task_router
from simon.api.work_sessions import session_router
from simon.config import Settings, get_settings
from simon.domain.accounts import AccountAccess, InviteAccount
from simon.domain.connected_tools import ActionProposal, GoogleAccountSelect, GoogleStart
from simon.domain.context import CreateMemory, ExplicitMemory, RecallQuery
from simon.domain.conversations import CreateThread, Message, Run, SubmitRun, Thread
from simon.domain.errors import AuthorizationError, DomainError, ModelError
from simon.domain.identity import DEV_ACTOR_ID, DEV_WORKSPACE_ID, Membership
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
    Job,
    RiskClass,
)
from simon.domain.ports import Store
from simon.domain.voice import VoiceOffer
from simon.services.accounts import AccountService
from simon.services.agent_calendar import AgentCalendarService
from simon.services.agent_platform import AgentPlatformService, load_manifest
from simon.services.agent_runs import AgentRunService
from simon.services.agent_setup_assistant import AgentSetupAssistantService
from simon.services.audit import AuditService
from simon.services.capabilities import CapabilityBroker
from simon.services.connected import ConnectedService
from simon.services.conversations import ConversationService
from simon.services.email_identity import EmailIdentityService
from simon.services.identity import IdentityService
from simon.services.interaction import InteractionService
from simon.services.jobs import JobService
from simon.services.memory import MemoryService
from simon.services.model_conversations import ModelConversationService
from simon.services.policy import PolicyEngine
from simon.services.project_autonomy import ProjectAutonomyService
from simon.services.project_coordinator import ProjectCoordinator
from simon.services.project_output_replication import ProjectOutputReplicationService
from simon.services.project_outputs import ProjectOutputService
from simon.services.project_storage import ProjectStorageService
from simon.services.project_work import ProjectWorkService
from simon.services.project_workspace import ProjectWorkspaceService
from simon.services.tasks import AssistantTaskService
from simon.services.voice import VoiceService
from simon.services.work_sessions import WorkSessionService


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

            self.store = PostgresStore(self.settings.database_url.get_secret_value(), pool_size=16)
        else:
            self.store = InMemoryStore()
        if self.settings.dev_login_enabled and self.settings.storage_backend == "memory":
            self.store.put_membership(
                Membership(
                    actor_id=DEV_ACTOR_ID,
                    workspace_id=DEV_WORKSPACE_ID,
                    role="owner",
                    display_name="Development user",
                    workspace_name="Development workspace",
                )
            )
        self.identity = IdentityService(self.store, self.settings)
        self.accounts = AccountService(self.identity)
        self.audit = AuditService(self.store)
        self.connected = ConnectedService(self.store, self.audit, self.settings, self.identity)
        self.email_identity = EmailIdentityService(
            self.identity, AccountEmail(self.connected.integrations)
        )
        self.external_actions = external_action_service(self.settings, self.store)
        self.interaction = InteractionService(self.store, self.audit, self.connected.home)
        self.policy = PolicyEngine()
        self.capabilities = CapabilityBroker(self.store, self.store, self.policy, self.audit)
        self.jobs = JobService(self.store, self.audit)
        self.conversations = ConversationService(self.store, self.audit)
        if self.settings.model_provider == "openai":
            from simon.adapters.openai_model import OpenAIModel

            assert self.settings.openai_api_key is not None
            fallback_openai_key = self.settings.openai_api_key.get_secret_value()
            self.conversations = ModelConversationService(
                self.store,
                self.audit,
                OpenAIModel(
                    self.settings.openai_api_key.get_secret_value(),
                    credential_provider=lambda: self.connected.integrations.credentials.get(
                        "SIMON_OPENAI_API_KEY", fallback_openai_key
                    ),
                ),
                self.settings,
                self.connected,
            )
        self.tasks = AssistantTaskService(
            self.store,
            self.identity,
            self.conversations
            if isinstance(self.conversations, ModelConversationService)
            else None,
        )
        self.connected.tasks = self.tasks
        self.work_sessions = WorkSessionService(self.tasks)
        self.agent_platform = AgentPlatformService(
            self.store,
            with_native_tools(
                with_project_runtime_tools(load_manifest(self.settings.agent_manifest_file)),
                self.connected,
            ),
            state_dir=self.settings.agent_state_dir,
            integrations=self.connected.integrations,
            available_transports=INSTALLED_TRANSPORTS,
            tool_availability=lambda actor, tool_id: (
                native_tool_status(
                    self.connected,
                    actor,
                    tool_id,
                )
                if tool_id.startswith("native.")
                else project_board_tool_status(self.project_boards, actor, tool_id)
                if tool_id.startswith("clickup.")
                else external_tool_status(
                    self.external_actions,
                    actor,
                    tool_id,
                )
            ),
        )
        self.agent_transport_factory = native_transport_factory(self.connected)
        self.agent_runs = AgentRunService(
            self.agent_platform,
            enabled=self.settings.agent_execution_enabled,
        )
        self.agent_calendar = AgentCalendarService(self.connected)
        self.project_outputs = ProjectOutputService(self.agent_runs, self.connected.local_files)
        self.project_output_replication = ProjectOutputReplicationService(
            self.project_outputs, self.connected.projects
        )
        self.connected.projects.output_sync = self.project_output_replication.sync
        self.agent_transport_factory = project_output_transport_factory(
            self.agent_transport_factory,
            self.project_outputs,
        )
        self.agent_transport_factory = project_journal_transport_factory(
            self.agent_transport_factory,
            self.project_outputs,
        )
        self.project_work = ProjectWorkService(
            self.store,
            project_resolver=self.tasks.project,
        )
        self.project_workspace = ProjectWorkspaceService(self.project_work)
        self.project_storage = ProjectStorageService(self.project_work, self.connected)
        self.agent_platform.project_tool_filter_factory = self.project_storage.tool_filter
        self.project_output_replication.destination_allowed = self.project_storage.primary_selected
        self.agent_transport_factory = project_storage_transport_factory(
            self.agent_transport_factory,
            self.project_storage,
            self.agent_runs,
        )
        self.project_boards = project_board_service(
            self.settings, self.store, self.project_work, integrations=self.connected.integrations
        )
        self.agent_platform.project_tool_availability = lambda actor, project_id, tool_id: (
            project_board_tool_status(self.project_boards, actor, tool_id, project_id)
        )
        self.agent_transport_factory = project_board_transport_factory(
            self.agent_transport_factory,
            self.project_boards,
            self.agent_runs,
        )
        self.project_coordinator = ProjectCoordinator(
            self.project_work,
            self.agent_runs,
            external_actions=self.external_actions,
            boards=self.project_boards,
        )
        self.project_work.team_validator = self.project_coordinator.validate_team
        self.agent_setup_assistant = AgentSetupAssistantService(self.agent_platform)
        self.project_autonomy = ProjectAutonomyService(
            self.project_work,
            self.project_coordinator,
            enabled=self.settings.agent_execution_enabled,
        )
        self.agent_transport_factory = project_transport_factory(
            self.agent_transport_factory,
            self.project_work,
            self.agent_runs,
        )
        self.agent_transport_factory = external_transport_factory(
            self.agent_transport_factory,
            self.external_actions,
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
        async def project_sync() -> None:
            while True:
                try:
                    await asyncio.to_thread(services.connected.projects.tick)
                except Exception:
                    logging.getLogger(__name__).warning(
                        "Project Drive sync unavailable; retrying later."
                    )
                await asyncio.sleep(60)

        project_task = (
            asyncio.create_task(project_sync())
            if services.settings.project_drive_sync_enabled
            else None
        )
        try:
            yield
        finally:
            await services.voice.shutdown()
            if project_task:
                project_task.cancel()
                with suppress(asyncio.CancelledError):
                    await project_task
            services.store.close()

    app = FastAPI(title="Simon API", version="0.1.0", lifespan=lifespan)
    app.state.container = services

    hostname = urlsplit(services.settings.public_origin).hostname
    assert hostname is not None
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=[hostname])
    app.include_router(auth_router(services.identity, services.email_identity))
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

    async def security_headers(request: Request, call_next: Any) -> Any:
        # One configured public origin controls redirects. Client-supplied proxy
        # headers never control origin, identity or auth throttling source keys.
        public_origin = urlsplit(services.settings.public_origin)
        request.scope["scheme"] = public_origin.scheme
        supplied_host = request.headers.get("host", "")
        valid_host = False
        try:
            incoming = urlsplit("//" + supplied_host)
            if (
                incoming.hostname == public_origin.hostname
                and incoming.netloc == supplied_host
                and incoming.username is None
                and incoming.password is None
                and (incoming.port is None or 1 <= incoming.port <= 65535)
            ):
                valid_host = True
                # TrustedHost checks the hostname but ignores a supplied port.
                # Accepted hosts always use our configured port in generated URLs.
                request.scope["headers"] = [
                    (key, public_origin.netloc.encode("ascii") if key == b"host" else value)
                    for key, value in request.scope["headers"]
                ]
        except ValueError:
            pass
        path = str(request.scope.get("path", "")).removeprefix(services.settings.public_path)
        callback = path == "/auth/google/callback"
        try:
            response = (
                await call_next(request)
                if valid_host
                else JSONResponse({"detail": "Invalid host header"}, status_code=400)
            )
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
        if services.settings.secure_cookies:
            response.headers["Strict-Transport-Security"] = "max-age=31536000"
        if path.startswith("/display/"):
            response.headers["Content-Security-Policy"] = (
                "default-src 'self'; frame-ancestors 'none'; object-src 'none'; base-uri 'none'"
                "; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'"
            )
        elif path in {"/login", "/chat", "/automations", "/displays"} or path.startswith(
            "/assets/"
        ):
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

    app.include_router(task_router(services.tasks, checked_actor))
    app.include_router(
        agent_platform_router(
            services.agent_platform,
            checked_actor,
            services.agent_runs,
            services.agent_calendar,
        )
    )
    app.include_router(agent_setup_assistant_router(services.agent_setup_assistant, checked_actor))
    app.include_router(project_router(services.connected.projects, checked_actor))
    app.include_router(project_command_router(services.project_coordinator, checked_actor))
    app.include_router(project_outputs_router(services.project_outputs, checked_actor))
    app.include_router(
        project_workspace_router(
            services.project_workspace,
            checked_actor,
            replication=services.project_output_replication,
            locations=services.project_storage,
        )
    )
    app.include_router(external_actions_router(services.external_actions, checked_actor))
    app.include_router(project_boards_router(services.project_boards, checked_actor))
    assert services.project_boards.integrations is not None
    app.include_router(integrations_router(services.project_boards.integrations, checked_actor))
    if services.tasks.conversations:
        app.include_router(session_router(services.work_sessions, checked_actor))
    app.include_router(local_file_router(services.connected.local_files, checked_actor))

    @app.get("/v1/connections/home")
    def home_connection(
        actor: Annotated[ActorContext, Depends(checked_actor)],
    ) -> dict[str, object]:
        return {
            "configured": services.connected.home.for_actor(actor).configured,
            "url": services.connected.home.for_actor(actor).url
            if services.connected.home.for_actor(actor).configured
            else None,
        }

    @app.get("/v1/work/overview")
    def work_overview(
        actor: Annotated[ActorContext, Depends(checked_actor)],
    ) -> dict[str, Any]:
        projects = services.connected.projects.list(actor)
        project_ids = {project["id"] for project in projects}
        return {
            "projects": projects,
            "project_artifacts": [
                artifact.model_dump(mode="json")
                for artifact in services.tasks.artifacts(actor)
                if str(artifact.project_id) in project_ids
            ],
            "tasks": [task.model_dump(mode="json") for task in services.tasks.list(actor)],
        }

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
            "agent_prompt_error": 422,
            "artifact_error": 409,
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

    @app.get("/v1/accounts")
    def accounts(actor: Annotated[ActorContext, Depends(checked_actor)]) -> list[dict[str, Any]]:
        return services.accounts.list(actor)

    @app.post("/v1/accounts/invite")
    def invite_account(
        body: InviteAccount,
        request: Request,
        actor: Annotated[ActorContext, Depends(checked_actor)],
    ) -> dict[str, Any]:
        from simon.services.identity import IDENTITY_LOCK

        with services.store.transaction(IDENTITY_LOCK):
            return services.accounts.invite(checked_actor(request), body)

    @app.post("/v1/accounts/{identifier}/invitation")
    def renew_invitation(
        identifier: UUID, request: Request, actor: Annotated[ActorContext, Depends(checked_actor)]
    ) -> dict[str, Any]:
        from simon.services.identity import IDENTITY_LOCK

        with services.store.transaction(IDENTITY_LOCK):
            return services.accounts.renew(checked_actor(request), identifier)

    @app.post("/v1/accounts/{identifier}/password-recovery")
    def account_password_recovery(
        identifier: UUID, request: Request, actor: Annotated[ActorContext, Depends(checked_actor)]
    ) -> dict[str, Any]:
        from simon.api.auth import local_password_access

        local_password_access(services.identity)
        return services.accounts.recovery(checked_actor(request), identifier)

    @app.post("/v1/accounts/{identifier}/access")
    def account_access(
        identifier: UUID,
        body: AccountAccess,
        request: Request,
        actor: Annotated[ActorContext, Depends(checked_actor)],
    ) -> dict[str, Any]:
        from simon.services.identity import IDENTITY_LOCK

        with services.store.transaction(IDENTITY_LOCK):
            return services.accounts.access(checked_actor(request), identifier, body)

    @app.get("/v1/voice")
    def voice_config(actor: Annotated[ActorContext, Depends(checked_actor)]) -> dict[str, Any]:
        services.conversations.authorize(actor, "threads:read")
        records = services.store.voice_sessions(actor.workspace_id, actor.actor_id)
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

    @app.get("/v1/context/search")
    def search_context(
        actor: Annotated[ActorContext, Depends(checked_actor)],
        query: Annotated[str, Query(max_length=200)] = "",
        offset: Annotated[int, Query(ge=0, le=10000)] = 0,
    ) -> dict[str, object]:
        return services.connected.recall.search(actor, RecallQuery(query=query, offset=offset))

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
            "background_sessions": services.settings.storage_backend == "postgres"
            and services.settings.model_provider == "openai",
            "web_search": services.settings.model_provider == "openai"
            and services.settings.web_search_enabled,
        }

    @app.get("/v1/connections/google")
    def google_status(actor: Annotated[ActorContext, Depends(checked_actor)]) -> dict[str, object]:
        return services.connected.status(actor)

    @app.post("/v1/connections/google/start")
    def google_start(
        body: GoogleStart,
        request: Request,
        actor: Annotated[ActorContext, Depends(checked_actor)],
    ) -> JSONResponse:
        url, binding = services.connected.start(
            actor,
            request.cookies.get(session_cookie(services.identity), ""),
            body.account,
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
    def google_disconnect(
        body: GoogleAccountSelect, actor: Annotated[ActorContext, Depends(checked_actor)]
    ) -> Response:
        services.connected.disconnect(actor, body.account)
        return Response(status_code=204)

    @app.post("/v1/connections/google/default")
    def google_default(
        body: GoogleAccountSelect, actor: Annotated[ActorContext, Depends(checked_actor)]
    ) -> dict[str, object]:
        services.connected.set_default(actor, body.account)
        return services.connected.status(actor)

    @app.post("/v1/connections/google/test")
    def google_test(
        body: GoogleAccountSelect, actor: Annotated[ActorContext, Depends(checked_actor)]
    ) -> dict[str, str]:
        return services.connected.test_connection(actor, body.account)

    @app.get("/v1/actions/{action_id}", response_model=ActionProposal)
    def action_status(
        action_id: UUID, actor: Annotated[ActorContext, Depends(checked_actor)]
    ) -> ActionProposal:
        return services.connected.get_action(actor, action_id)

    @app.get("/v1/threads/{thread_id}/actions", response_model=tuple[ActionProposal, ...])
    def thread_actions(
        thread_id: UUID, actor: Annotated[ActorContext, Depends(checked_actor)]
    ) -> tuple[ActionProposal, ...]:
        return services.connected.actions(actor, thread_id)

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
        root.add_middleware(TrustedHostMiddleware, allowed_hosts=[hostname])
        root.add_middleware(
            RequestIngressMiddleware,
            public_path=services.settings.public_path,
            auth_rate_limit=services.settings.auth_rate_limit,
            auth_rate_window_seconds=services.settings.auth_rate_window_seconds,
        )
        root.middleware("http")(security_headers)
        root.mount(services.settings.public_path, app)
        return root
    app.add_middleware(
        RequestIngressMiddleware,
        auth_rate_limit=services.settings.auth_rate_limit,
        auth_rate_window_seconds=services.settings.auth_rate_window_seconds,
    )
    app.middleware("http")(security_headers)
    return app


app = create_app()
