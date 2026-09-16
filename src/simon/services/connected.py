import base64
import hashlib
import json
import secrets
from collections.abc import Callable
from datetime import timedelta
from time import time
from urllib.parse import urlencode
from uuid import UUID

from cryptography.fernet import Fernet, InvalidToken
from pydantic import HttpUrl
from pydantic import ValidationError as PydanticError

from simon.adapters.google import (
    CALENDAR_SCOPE,
    EMAIL_SCOPE,
    SCOPES,
    ConnectedError,
    GoogleAPI,
    GoogleTokens,
)
from simon.config import Settings
from simon.domain.connected_tools import (
    ActionProposal,
    CalendarDraft,
    CalendarQuery,
    EmailDraft,
    GoogleConnection,
    GoogleOAuthState,
    ToolName,
)
from simon.domain.errors import AuthorizationError, DomainError, NotFoundError, ValidationError
from simon.domain.home import (
    HomeControl,
    HomeOrganization,
    HomeOutletSetup,
    HomeQuery,
    HomeRename,
    OutletSetup,
)
from simon.domain.models import ActorContext, utc_now
from simon.domain.ports import Store
from simon.services.audit import AuditService
from simon.services.canonical import digest
from simon.services.conversations import ConversationService
from simon.services.home import HomeService
from simon.services.identity import IDENTITY_LOCK, IdentityService, token_hash


class ConnectedService:
    def __init__(
        self, store: Store, audit: AuditService, settings: Settings, identity: IdentityService
    ) -> None:
        self.store, self.audit, self.settings, self.identity = store, audit, settings, identity
        self.api = GoogleAPI(settings)
        self.conversations = ConversationService(store, audit)
        self.home = HomeService(store, audit, settings)

    @property
    def configured(self) -> bool:
        return bool(
            self.settings.google_client_id
            and self.settings.google_client_secret
            and self.settings.google_token_key
        )

    @property
    def cipher(self) -> Fernet:
        if not self.configured:
            raise ConnectedError(
                "Google is not configured. Follow the Google setup guide on this server."
            )
        assert self.settings.google_token_key
        return Fernet(self.settings.google_token_key.get_secret_value().encode())

    @property
    def redirect_uri(self) -> str:
        return self.settings.public_origin + self.settings.public_path + "/auth/google/callback"

    def encrypt(self, data: str) -> str:
        return self.cipher.encrypt(data.encode()).decode()

    def decrypt(self, data: str) -> str:
        try:
            return self.cipher.decrypt(data.encode()).decode()
        except InvalidToken:
            raise ConnectedError(
                "Google credentials cannot be read. Restore the token key or reconnect."
            ) from None

    def status(self, actor: ActorContext) -> dict[str, object]:
        self.conversations.authorize(actor, "threads:read")
        connection = self.store.google_connection(actor.household_id, actor.actor_id)
        return {
            "configured": self.configured,
            "connected": bool(connection),
            "email": connection.email if connection else None,
            "calendar": bool(connection and CALENDAR_SCOPE in connection.scopes),
            "email_send": bool(connection and EMAIL_SCOPE in connection.scopes),
            "redirect_uri": self.redirect_uri,
        }

    def available(self, actor: ActorContext) -> tuple[ToolName, ...]:
        tools: list[ToolName] = ["web_search"] if self.settings.web_search_enabled else []
        connection = self.store.google_connection(actor.household_id, actor.actor_id)
        if self.configured and connection:
            if CALENDAR_SCOPE in connection.scopes:
                tools.extend(("calendar_list_events", "propose_calendar_event"))
            if EMAIL_SCOPE in connection.scopes:
                tools.append("propose_email")
        if "home:read" in actor.scopes and (
            any(self.home.catalog.configured(actor.household_id).values())
            or self.home.inventory(actor)
        ):
            tools.extend(("home_list_devices", "home_get_status", "home_refresh_devices"))
            if "home:control" in actor.scopes:
                tools.append("home_control")
            if "home:organize" in actor.scopes:
                tools.extend(("home_organize_devices", "home_rename_device"))
            if "identity:manage" in actor.scopes:
                tools.append("home_setup_outlet")
        return tuple(tools)

    def start(self, actor: ActorContext, session_token: str) -> tuple[str, str]:
        self.conversations.authorize(actor, "threads:write")
        state, binding, verifier = (secrets.token_urlsafe(32) for _ in range(3))
        encrypted = self.encrypt(json.dumps({"session": session_token, "verifier": verifier}))
        self.store.save_google_state(
            GoogleOAuthState(
                state_hash=token_hash(state),
                binding_hash=token_hash(binding),
                encrypted_data=encrypted,
                expires_at=utc_now() + timedelta(minutes=5),
            )
        )
        challenge = (
            base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
            .rstrip(b"=")
            .decode()
        )
        return "https://accounts.google.com/o/oauth2/v2/auth?" + urlencode(
            {
                "client_id": self.settings.google_client_id,
                "redirect_uri": self.redirect_uri,
                "response_type": "code",
                "scope": " ".join(SCOPES),
                "state": state,
                "access_type": "offline",
                "prompt": "consent",
                "code_challenge": challenge,
                "code_challenge_method": "S256",
            }
        ), binding

    def callback(self, state: str, binding: str, code: str) -> None:
        record = self.store.take_google_state(token_hash(state), token_hash(binding))
        if not record or record.expires_at <= utc_now():
            raise ValidationError(
                "Google connection expired or belongs to another browser. Connect again."
            )
        data = json.loads(self.decrypt(record.encrypted_data))
        _, actor = self.identity.resolve(data["session"])
        self.conversations.authorize(actor, "threads:write")
        tokens, scopes = self.api.exchange(code, data["verifier"], self.redirect_uri)
        email = self.api.account_email(tokens.access_token)
        if not {CALENDAR_SCOPE, EMAIL_SCOPE}.intersection(scopes):
            raise ConnectedError(
                "No Calendar or Gmail permission was granted. Connect again and select a tool."
            )
        with self.store.transaction(IDENTITY_LOCK), self.store.transaction(actor.household_id):
            _, current = self.identity.resolve(data["session"])
            self.conversations.authorize(current, "threads:write")
            self.store.save_google_connection(
                GoogleConnection(
                    actor_id=actor.actor_id,
                    household_id=actor.household_id,
                    email=email,
                    scopes=scopes,
                    encrypted_tokens=self.encrypt(tokens.model_dump_json()),
                )
            )
            self.audit.record(
                event_type="google.connected",
                actor=actor,
                resource_type="google_connection",
                resource_id=str(actor.actor_id),
                payload={"scopes": list(scopes)},
            )

    def disconnect(self, actor: ActorContext) -> None:
        self.conversations.authorize(actor, "threads:write")
        with self.store.transaction(actor.household_id):
            self.store.delete_google_connection(actor.household_id, actor.actor_id)
            self.audit.record(
                event_type="google.disconnected",
                actor=actor,
                resource_type="google_connection",
                resource_id=str(actor.actor_id),
                payload={},
            )

    def connection(self, actor: ActorContext, expected: UUID | None = None) -> GoogleConnection:
        connection = self.store.google_connection(actor.household_id, actor.actor_id)
        if not connection or (expected and connection.id != expected):
            raise ConnectedError(
                "The Google connection changed. Connect again and request a new preview."
            )
        return connection

    def access_token(self, actor: ActorContext, connection: GoogleConnection) -> str:
        tokens = GoogleTokens.model_validate_json(self.decrypt(connection.encrypted_tokens))
        if tokens.expires_at <= time() + 60:
            tokens = self.api.refresh(tokens)
            with self.store.transaction(actor.household_id):
                self.connection(actor, connection.id)
                self.store.save_google_connection(
                    connection.model_copy(
                        update={
                            "encrypted_tokens": self.encrypt(tokens.model_dump_json()),
                        }
                    )
                )
        return tokens.access_token

    def edit_home(
        self,
        actor: ActorContext,
        run_id: UUID,
        change: HomeRename | HomeOutletSetup,
        revalidate: Callable[[], ActorContext],
    ) -> dict[str, object]:
        """Persist chat configuration once, while the originating request is still active."""
        with self.store.transaction(IDENTITY_LOCK), self.store.transaction(actor.household_id):
            current = revalidate()
            if (current.actor_id, current.household_id) != (actor.actor_id, actor.household_id):
                raise AuthorizationError("Home access changed")
            self.conversations.authorize(current, "threads:write")
            self.conversations.authorize(current, "home:read")
            self.conversations.authorize(
                current,
                "identity:manage" if isinstance(change, HomeOutletSetup) else "home:organize",
            )
            attempt = self.store.attempt(run_id)
            if (
                not attempt
                or attempt.household_id != actor.household_id
                or attempt.run.actor_id != actor.actor_id
                or attempt.run.parent_run_id is not None
                or attempt.status != "pending"
                or attempt.expires_at <= utc_now()
            ):
                raise AuthorizationError("The chat request is no longer active.")
            self.conversations.get(current, attempt.run.thread_id)

            def apply() -> dict[str, object]:
                if isinstance(change, HomeRename):
                    return self.home.catalog.rename(current, change, self.home.devices)
                device = self.home.setup_outlet(
                    current,
                    change.device_id,
                    OutletSetup.model_validate(change.model_dump(exclude={"device_id"})),
                )
                return {
                    "device_id": device.id,
                    "name": device.name,
                    "room": device.room,
                    "load_type": device.load_type,
                    "control_enabled": device.control_enabled,
                    "scope": "Simon inventory",
                    "device_commands_sent": False,
                }

            request_digest = digest(change.model_dump(mode="json"))
            result, _ = self.store.execute_once(
                f"{actor.household_id}:home.edit:{actor.actor_id}",
                f"{run_id}:{type(change).__name__}:{request_digest}",
                request_digest,
                apply,
            )
            return result

    def executor(
        self,
        actor: ActorContext,
        run_id: UUID,
        proposals: list[ActionProposal],
        revalidate: Callable[[], ActorContext],
    ) -> Callable[[str, str], str]:
        def execute(name: str, arguments: str) -> str:
            checked = revalidate()
            if (checked.actor_id, checked.household_id) != (actor.actor_id, actor.household_id):
                raise AuthorizationError("Tool access changed")
            self.conversations.authorize(checked, "threads:write")
            if name not in self.available(checked) or name == "web_search":
                raise AuthorizationError("tool unavailable")
            try:
                if len(arguments) > 20000:
                    raise ValueError("arguments too long")
                if name == "home_list_devices":
                    if json.loads(arguments) != {}:
                        raise ValueError("no arguments expected")
                    self.home.catalog.sync(checked)
                    return json.dumps(self.home.inventory(checked))
                if name == "home_refresh_devices":
                    if json.loads(arguments) != {}:
                        raise ValueError("no arguments expected")
                    return json.dumps(
                        {
                            "providers": self.home.catalog.sync(checked, force=True),
                            "devices": self.home.inventory(checked),
                        }
                    )
                if name == "home_organize_devices":
                    change = HomeOrganization.model_validate_json(arguments)
                    with self.store.transaction(IDENTITY_LOCK):
                        current = revalidate()
                        if (current.actor_id, current.household_id) != (
                            actor.actor_id,
                            actor.household_id,
                        ):
                            raise AuthorizationError("Home access changed")
                        return json.dumps(
                            self.home.catalog.organize(
                                current,
                                change,
                                self.home.devices,
                                operation_key=str(run_id)
                                + ":"
                                + hashlib.sha256(change.model_dump_json().encode()).hexdigest(),
                            )
                        )
                if name == "home_get_status":
                    query_home = HomeQuery.model_validate_json(arguments)
                    return self.home.read(checked, query_home.device_id).model_dump_json()
                if name in {"home_rename_device", "home_setup_outlet"}:
                    edit = (
                        HomeRename.model_validate_json(arguments)
                        if name == "home_rename_device"
                        else HomeOutletSetup.model_validate_json(arguments)
                    )
                    return json.dumps(self.edit_home(checked, run_id, edit, revalidate))
                if name == "home_control":
                    commands = self.home.control(
                        checked, run_id, HomeControl.model_validate_json(arguments), revalidate
                    )
                    return json.dumps(
                        {
                            "requires_confirmation": False,
                            "commands": [command.model_dump(mode="json") for command in commands],
                            "instruction": "Report each result accurately. Never retry unknown or "
                            "executing commands without a fresh user request.",
                        }
                    )
                connection = self.connection(checked)
                if name == "calendar_list_events":
                    query = CalendarQuery.model_validate_json(arguments)
                    result = self.api.events(self.access_token(checked, connection), query)
                    self.connection(checked, connection.id)
                    return json.dumps(result, ensure_ascii=False)
                if len(proposals) >= 3:
                    return (
                        '{"error":"At most three previews per answer. Ask the user to continue."}'
                    )
                proposal = ActionProposal(
                    actor_id=actor.actor_id,
                    household_id=actor.household_id,
                    run_id=run_id,
                    connection_id=connection.id,
                    account_email=connection.email,
                    kind="calendar.create" if name == "propose_calendar_event" else "email.send",
                    calendar=CalendarDraft.model_validate_json(arguments)
                    if name == "propose_calendar_event"
                    else None,
                    email=EmailDraft.model_validate_json(arguments)
                    if name == "propose_email"
                    else None,
                )
                proposals.append(proposal)
                return json.dumps(
                    {
                        "preview_id": str(proposal.id),
                        "requires_confirmation": True,
                        "executed": False,
                        "instruction": "Ask the user to review the card and confirm.",
                    }
                )
            except (PydanticError, ValueError):
                return json.dumps(
                    {"error": "Invalid tool arguments. Check the schema, target, and values."}
                )
            except DomainError as error:
                return json.dumps({"error": str(error)})

        return execute

    def get_action(self, actor: ActorContext, action_id: UUID) -> ActionProposal:
        self.conversations.authorize(actor, "threads:read")
        action = self.store.action(action_id)
        if not action or (action.actor_id, action.household_id) != (
            actor.actor_id,
            actor.household_id,
        ):
            raise NotFoundError("action not found")
        self.conversations.run(actor, action.run_id)
        return action

    def decide(
        self,
        actor: ActorContext,
        action_id: UUID,
        *,
        confirm: bool,
        revalidate: Callable[[], ActorContext],
    ) -> ActionProposal:
        self.conversations.authorize(actor, "threads:write")
        existing = self.get_action(actor, action_id)
        if existing.kind == "home.set":
            return self.home.decide(actor, action_id, confirm=confirm, revalidate=revalidate)
        with self.store.transaction(IDENTITY_LOCK), self.store.transaction(actor.household_id):
            current_actor = revalidate()
            if (current_actor.actor_id, current_actor.household_id) != (
                actor.actor_id,
                actor.household_id,
            ):
                raise AuthorizationError("Google access changed")
            self.conversations.authorize(current_actor, "threads:write")
            action = self.get_action(current_actor, action_id)
            if action.status != "pending":
                return action  # A retry can never send or create twice.
            if not confirm or action.expires_at <= utc_now():
                action = action.model_copy(update={"status": "cancelled"})
                self.store.save_action(action)
                self.audit.record(
                    event_type="action.cancelled",
                    actor=actor,
                    resource_type="action",
                    resource_id=str(action.id),
                    payload={"kind": action.kind},
                )
                return action
            connection = self.connection(actor, action.connection_id)
            action = action.model_copy(update={"status": "executing"})
            self.store.save_action(action)
            self.audit.record(
                event_type="action.confirmed",
                actor=actor,
                resource_type="action",
                resource_id=str(action.id),
                payload={"kind": action.kind},
            )
        # Refresh and external effects never hold a database transaction open.
        dispatched = False
        try:
            token = self.access_token(actor, connection)
            checked = revalidate()
            if (checked.actor_id, checked.household_id) != (actor.actor_id, actor.household_id):
                raise AuthorizationError("Google access changed")
            self.conversations.authorize(checked, "threads:write")
            self.connection(actor, action.connection_id)
            dispatched = True
            identifier, url = self.api.execute(token, action, connection.email)
            action = action.model_copy(
                update={
                    "status": "succeeded",
                    "provider_id": identifier,
                    "result_url": HttpUrl(url) if url else None,
                }
            )
        except Exception as error:
            unknown = dispatched and (not isinstance(error, ConnectedError) or error.unknown)
            action = action.model_copy(
                update={
                    "status": "unknown" if unknown else "failed",
                    "error": str(error)
                    if isinstance(error, DomainError)
                    else "Google action could not be completed.",
                }
            )
        with self.store.transaction(actor.household_id):
            self.store.save_action(action)
            self.audit.record(
                event_type="action." + action.status,
                actor=actor,
                resource_type="action",
                resource_id=str(action.id),
                payload={"kind": action.kind},
            )
        return action
