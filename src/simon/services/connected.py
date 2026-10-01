"""Account-bound connected tools, provider credentials, and durable action receipts."""

import base64
import hashlib
import json
import secrets
from collections.abc import Callable
from datetime import UTC, timedelta
from time import time
from typing import TYPE_CHECKING, cast
from urllib.parse import urlencode
from uuid import UUID, uuid5

from cryptography.fernet import Fernet, InvalidToken
from pydantic import HttpUrl
from pydantic import ValidationError as PydanticError

from simon.adapters.google import (
    CALENDAR_SCOPE,
    DRIVE_READ_SCOPE,
    DRIVE_WRITE_SCOPE,
    EMAIL_SCOPE,
    GMAIL_READ_SCOPE,
    SCOPES,
    ConnectedError,
    GoogleAPI,
    GoogleTokens,
)
from simon.adapters.home_client import HomeClient
from simon.config import Settings
from simon.domain.connected_tools import (
    ActionProposal,
    CalendarDraft,
    CalendarQuery,
    EmailDraft,
    GoogleAccountSelect,
    GoogleConnection,
    GoogleItem,
    GoogleOAuthState,
    GoogleSearch,
    ToolName,
)
from simon.domain.context import ForgetFact, RecallQuery, RememberFact
from simon.domain.errors import AuthorizationError, DomainError, NotFoundError, ValidationError
from simon.domain.models import ActorContext, utc_now
from simon.domain.ports import Store
from simon.domain.tasks import ControlAssistantTask, CreateAssistantTask, SteerAssistantTask
from simon.services.audit import AuditService
from simon.services.canonical import digest
from simon.services.conversations import ConversationService
from simon.services.identity import IDENTITY_LOCK, IdentityService, token_hash
from simon.services.memory import MemoryService
from simon.services.recall import RecallService

if TYPE_CHECKING:
    from simon.services.tasks import AssistantTaskService


class ConnectedService:
    def __init__(
        self, store: Store, audit: AuditService, settings: Settings, identity: IdentityService
    ) -> None:
        self.store, self.audit, self.settings, self.identity = store, audit, settings, identity
        self.api = GoogleAPI(settings)
        self.conversations = ConversationService(store, audit)
        self.home = HomeClient(settings)
        self.memories = MemoryService(store, audit)
        self.recall = RecallService(store)
        from simon.services.project_files import ProjectFileService

        self.projects = ProjectFileService(self)
        from simon.services.local_files import LocalFileService

        self.local_files = LocalFileService(self)
        self.tasks: AssistantTaskService | None = None

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

    @staticmethod
    def account_status(connection: GoogleConnection) -> dict[str, object]:
        return {
            "id": str(connection.id),
            "email": connection.email,
            "is_default": connection.is_default,
            "calendar": CALENDAR_SCOPE in connection.scopes,
            "email_send": EMAIL_SCOPE in connection.scopes,
            "gmail_read": GMAIL_READ_SCOPE in connection.scopes,
            "drive_read": bool({DRIVE_READ_SCOPE, DRIVE_WRITE_SCOPE} & set(connection.scopes)),
            "drive_write": DRIVE_WRITE_SCOPE in connection.scopes,
            "needs_reconnect": not {
                CALENDAR_SCOPE,
                EMAIL_SCOPE,
                GMAIL_READ_SCOPE,
                DRIVE_WRITE_SCOPE,
            }.issubset(connection.scopes),
        }

    def status(self, actor: ActorContext) -> dict[str, object]:
        self.conversations.authorize(actor, "threads:read")
        connections = self.store.google_connections(actor.household_id, actor.actor_id)
        return {
            "configured": self.configured,
            "connected": bool(connections),
            "email": None,
            "calendar": False,
            "email_send": False,
            "gmail_read": False,
            "drive_read": False,
            "drive_write": False,
            "needs_reconnect": False,
            **(self.account_status(connections[0]) if connections else {}),
            "accounts": [self.account_status(c) for c in connections],
            "redirect_uri": self.redirect_uri,
        }

    def set_default(self, actor: ActorContext, account: str) -> None:
        self.conversations.authorize(actor, "threads:write")
        with self.store.transaction(actor.household_id):
            connection = self.connection(actor, account=account)
            self.store.set_default_google_connection(
                actor.household_id, actor.actor_id, connection.id
            )
            self.audit.record(
                event_type="google.default_changed",
                actor=actor,
                resource_type="google_connection",
                resource_id=str(connection.id),
                payload={},
            )

    def available(self, actor: ActorContext) -> tuple[ToolName, ...]:
        tools: list[ToolName] = ["web_search"] if self.settings.web_search_enabled else []
        if "threads:read" in actor.scopes:
            tools.append("context_search")
        if {"memories:read", "memories:write"} <= actor.scopes:
            tools.extend(("memory_remember", "memory_forget"))
        if self.tasks and {"jobs:read", "jobs:write"} <= actor.scopes:
            tools.extend(("task_create", "task_list", "task_control", "task_steer"))
        connections = self.store.google_connections(actor.household_id, actor.actor_id)
        scopes = {scope for c in connections for scope in c.scopes}
        if self.configured and connections:
            tools.append("google_accounts_list")
            if CALENDAR_SCOPE in scopes:
                tools.extend(
                    (
                        "calendar_list_events",
                        "calendar_create_event",
                        "propose_calendar_event",
                    )
                )
            if EMAIL_SCOPE in scopes:
                tools.append("propose_email")
            if GMAIL_READ_SCOPE in scopes:
                tools.extend(("gmail_search_messages", "gmail_read_message"))
            if {DRIVE_READ_SCOPE, DRIVE_WRITE_SCOPE} & set(scopes):
                tools.extend(("drive_search_files", "drive_read_file", "drive_list_folder"))
        if self.home.configured:
            tools.extend(self.home.available(actor))
        if {"jobs:read", "jobs:write", "memories:read", "memories:write"} <= actor.scopes:
            tools.extend(("project_list", "project_create", "project_unlink_drive"))
            if self.configured and connections and DRIVE_WRITE_SCOPE in scopes:
                tools.extend(
                    (
                        "project_link_drive",
                        "project_drive_trash",
                        "project_sync",
                        "project_files_list",
                        "project_file_read",
                        "project_file_create",
                        "project_file_edit",
                        "project_sheet_read",
                        "project_sheet_write",
                        "project_file_rename",
                    )
                )
        if self.settings.local_files_enabled and {"jobs:read", "jobs:write"} <= actor.scopes:
            tools.extend(
                (
                    "local_files_roots",
                    "local_files_list",
                    "local_files_search",
                    "local_file_read",
                    "local_file_write",
                    "local_file_edit",
                    "local_file_move",
                    "local_folder_create",
                    "local_zip_inspect",
                    "local_zip_extract",
                    "local_zip_create",
                    "local_file_import_drive",
                    "local_file_export_drive",
                )
            )
        return tuple(tools)

    def start(self, actor: ActorContext, session_token: str, account: str = "") -> tuple[str, str]:
        self.conversations.authorize(actor, "threads:write")
        state, binding, verifier = (secrets.token_urlsafe(32) for _ in range(3))
        email = self.connection(actor, account=account).email if account else ""
        encrypted = self.encrypt(
            json.dumps({"session": session_token, "verifier": verifier, "email": email})
        )
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
                "prompt": "consent select_account",
                **({"login_hint": email} if email else {}),
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
        email = self.api.account_email(tokens.access_token).strip().lower()
        if data.get("email") and data["email"].casefold() != email.casefold():
            raise ConnectedError("Choose the same Google account when reconnecting it.")
        granted_tools = {
            CALENDAR_SCOPE,
            EMAIL_SCOPE,
            GMAIL_READ_SCOPE,
            DRIVE_READ_SCOPE,
            DRIVE_WRITE_SCOPE,
        }
        if not granted_tools.intersection(scopes):
            raise ConnectedError(
                "No Calendar, Gmail or Drive permission was granted. "
                "Connect again and select a tool."
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

    def disconnect(self, actor: ActorContext, account: str = "") -> None:
        self.conversations.authorize(actor, "threads:write")
        with self.store.transaction(actor.household_id):
            connection = self.connection(actor, account=account)
            self.store.delete_google_connection(actor.household_id, actor.actor_id, connection.id)
            self.audit.record(
                event_type="google.disconnected",
                actor=actor,
                resource_type="google_connection",
                resource_id=str(actor.actor_id),
                payload={},
            )

    def connection(
        self, actor: ActorContext, expected: UUID | None = None, *, account: str = ""
    ) -> GoogleConnection:
        connections = self.store.google_connections(actor.household_id, actor.actor_id)
        if expected:
            connection = next((c for c in connections if c.id == expected), None)
        elif account:
            connection = next(
                (c for c in connections if account.casefold() in {c.email.casefold(), str(c.id)}),
                None,
            )
        else:
            connection = connections[0] if connections else None
        if not connection:
            raise ConnectedError(
                "Google account is not connected or its connection changed. "
                "Open Connections or list the connected accounts."
            )
        return connection

    @staticmethod
    def require_google_scope(connection: GoogleConnection, name: str) -> None:
        required = (
            {CALENDAR_SCOPE}
            if name.startswith("calendar_") or name == "propose_calendar_event"
            else {EMAIL_SCOPE}
            if name == "propose_email"
            else {GMAIL_READ_SCOPE}
            if name.startswith("gmail_")
            else {DRIVE_READ_SCOPE, DRIVE_WRITE_SCOPE}
        )
        if not required.intersection(connection.scopes):
            raise ConnectedError(
                "This Google account lacks permission for this tool. Reconnect it in Connections."
            )

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

    def create_calendar_event(
        self,
        actor: ActorContext,
        run_id: UUID,
        draft: CalendarDraft,
        revalidate: Callable[[], ActorContext],
    ) -> ActionProposal:
        # Normalize equivalent offsets so a repeated tool call has one provider event ID.
        canonical = draft.model_dump(mode="json")
        canonical.update(
            start=draft.start.astimezone(UTC).isoformat(),
            end=draft.end.astimezone(UTC).isoformat(),
        )
        action_id = uuid5(run_id, "calendar.create:" + digest(canonical))
        with self.store.transaction(IDENTITY_LOCK), self.store.transaction(actor.household_id):
            current = revalidate()
            self._calendar_access(actor, current, run_id)
            existing = self.store.action(action_id)
            if existing:
                return existing
            attempt = self.store.attempt(run_id)
            assert attempt is not None
            actions = self.store.recent_actions(attempt.run.thread_id, actor.actor_id, 100)
            if sum(action.run_id == run_id for action in actions) >= 3:
                raise ValidationError("At most three calendar creations per request.")
            connection = self.connection(current, account=draft.account)
            self.require_google_scope(connection, "calendar_create_event")
            action = ActionProposal(
                id=action_id,
                actor_id=actor.actor_id,
                household_id=actor.household_id,
                run_id=run_id,
                connection_id=connection.id,
                account_email=connection.email,
                kind="calendar.create",
                calendar=draft,
                status="executing",
                immediate=True,
            )
            self.store.save_action(action)
            self.audit.record(
                event_type="action.requested",
                actor=current,
                resource_type="action",
                resource_id=str(action.id),
                payload={"kind": action.kind, "immediate": True},
            )

        def check_active() -> ActorContext:
            checked = revalidate()
            self._calendar_access(actor, checked, run_id)
            return checked

        return self._dispatch(actor, action, connection, check_active)

    def _calendar_access(self, actor: ActorContext, current: ActorContext, run_id: UUID) -> None:
        if (current.actor_id, current.household_id) != (actor.actor_id, actor.household_id):
            raise AuthorizationError("Google access changed")
        self.conversations.authorize(current, "threads:write")
        attempt = self.store.attempt(run_id)
        if (
            not attempt
            or attempt.household_id != actor.household_id
            or attempt.run.actor_id != actor.actor_id
            or attempt.status != "pending"
            or attempt.expires_at <= utc_now()
            or attempt.run.parent_run_id is not None
            or "calendar_create_event" not in attempt.run.capability_manifest
        ):
            raise AuthorizationError("Calendar creation requires an active original request.")
        self.conversations.get(current, attempt.run.thread_id)
        if "calendar_create_event" not in self.available(current):
            raise AuthorizationError("Calendar creation permission is unavailable.")

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
                if len(arguments) > (450000 if name.startswith(("project_", "local_")) else 20000):
                    raise ValueError("arguments too long")
                if name.startswith("local_"):
                    return self.local_files.execute(checked, run_id, name, arguments, revalidate)
                if name == "drive_list_folder":
                    from simon.domain.project_files import DriveBrowse

                    return json.dumps(
                        self.projects.browse(
                            checked, DriveBrowse.model_validate_json(arguments), revalidate
                        ),
                        ensure_ascii=False,
                    )
                if name.startswith("project_"):
                    return self.projects.execute(checked, run_id, name, arguments, revalidate)
                if name in {
                    "context_search",
                    "memory_remember",
                    "memory_forget",
                    "task_create",
                    "task_list",
                    "task_control",
                    "task_steer",
                }:
                    with (
                        self.store.transaction(IDENTITY_LOCK),
                        self.store.transaction(actor.household_id),
                    ):
                        current = revalidate()
                        if (current.actor_id, current.household_id) != (
                            actor.actor_id,
                            actor.household_id,
                        ):
                            raise AuthorizationError("Context access changed")
                        attempt = self.store.attempt(run_id)
                        if (
                            not attempt
                            or attempt.status != "pending"
                            or attempt.expires_at <= utc_now()
                            or attempt.run.actor_id != actor.actor_id
                            or attempt.household_id != actor.household_id
                        ):
                            raise NotFoundError("active request not found")
                        thread = self.conversations.get(current, attempt.run.thread_id)
                        if thread.visibility != "personal":
                            raise AuthorizationError(
                                "Personal context requires a new private conversation"
                            )
                        if name == "context_search":
                            return json.dumps(
                                self.recall.search(
                                    current,
                                    RecallQuery.model_validate_json(arguments),
                                ),
                                ensure_ascii=False,
                            )
                        if name == "memory_remember":
                            return self.memories.remember(
                                current, run_id, RememberFact.model_validate_json(arguments)
                            ).model_dump_json()
                        if name == "task_list":
                            task_service = cast("AssistantTaskService", self.tasks)
                            if json.loads(arguments) != {}:
                                raise ValueError("no arguments expected")
                            return json.dumps(
                                [
                                    task.model_dump(mode="json")
                                    for task in task_service.list(current)
                                ],
                                ensure_ascii=False,
                            )
                        if name == "task_create":
                            task_service = cast("AssistantTaskService", self.tasks)
                            values = json.loads(arguments)
                            values["idempotency_key"] = (
                                "chat-task:"
                                + str(run_id)
                                + ":"
                                + hashlib.sha256(arguments.encode()).hexdigest()
                            )
                            return task_service.create(
                                current,
                                CreateAssistantTask.model_validate(values),
                                origin_thread_id=thread.id,
                            ).model_dump_json()
                        if name == "task_control":
                            task_service = cast("AssistantTaskService", self.tasks)
                            values = json.loads(arguments)
                            identifier = UUID(values.pop("task_id"))
                            return task_service.control(
                                current, identifier, ControlAssistantTask.model_validate(values)
                            ).model_dump_json()
                        if name == "task_steer":
                            task_service = cast("AssistantTaskService", self.tasks)
                            values = json.loads(arguments)
                            identifier = UUID(values.pop("task_id"))
                            return task_service.steer(
                                current, identifier, SteerAssistantTask.model_validate(values)
                            ).model_dump_json()
                        return self.memories.retract(
                            current, ForgetFact.model_validate_json(arguments).memory_id
                        ).model_dump_json()
                if name.startswith(("home_", "display_")):
                    current = revalidate()
                    if (current.actor_id, current.household_id) != (
                        actor.actor_id,
                        actor.household_id,
                    ):
                        raise AuthorizationError("Tool access changed")
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
                    return json.dumps(
                        self.home.execute(
                            current,
                            name,
                            json.loads(arguments),
                            run_id=run_id,
                            thread_id=attempt.run.thread_id,
                        )
                    )
                if name == "google_accounts_list":
                    if json.loads(arguments):
                        raise ValueError("No arguments expected")
                    return json.dumps({"accounts": self.status(checked)["accounts"]})
                values = json.loads(arguments)
                account = GoogleAccountSelect.model_validate(
                    {"account": values.get("account", "")}
                ).account
                connection = self.connection(checked, account=account)
                self.require_google_scope(connection, name)
                if name in {
                    "gmail_search_messages",
                    "gmail_read_message",
                    "drive_search_files",
                    "drive_read_file",
                }:
                    if name in {"gmail_search_messages", "drive_search_files"}:
                        search = GoogleSearch.model_validate_json(arguments)
                        search_method = (
                            self.api.gmail_search
                            if name == "gmail_search_messages"
                            else self.api.drive_search
                        )
                        result = search_method(self.access_token(checked, connection), search)
                    else:
                        item = GoogleItem.model_validate_json(arguments)
                        read_method = (
                            self.api.gmail_message
                            if name == "gmail_read_message"
                            else self.api.drive_file
                        )
                        result = read_method(self.access_token(checked, connection), item)
                    # Do not release private results after access changes during network I/O.
                    current = revalidate()
                    if (current.actor_id, current.household_id) != (
                        actor.actor_id,
                        actor.household_id,
                    ):
                        raise AuthorizationError("Google access changed")
                    self.conversations.authorize(current, "threads:write")
                    if name not in self.available(current):
                        raise AuthorizationError("Google tool access changed")
                    self.require_google_scope(self.connection(current, connection.id), name)
                    return json.dumps(
                        {**result, "account_email": connection.email}, ensure_ascii=False
                    )
                if name == "calendar_list_events":
                    query = CalendarQuery.model_validate_json(arguments)
                    result = self.api.events(self.access_token(checked, connection), query)
                    current = revalidate()
                    if (current.actor_id, current.household_id) != (
                        actor.actor_id,
                        actor.household_id,
                    ):
                        raise AuthorizationError("Google access changed")
                    self.conversations.authorize(current, "threads:write")
                    self.require_google_scope(self.connection(current, connection.id), name)
                    return json.dumps(
                        {**result, "account_email": connection.email}, ensure_ascii=False
                    )
                if name == "calendar_create_event":
                    receipt = self.create_calendar_event(
                        checked,
                        run_id,
                        CalendarDraft.model_validate_json(arguments),
                        revalidate,
                    )
                    proposals[:] = [p for p in proposals if p.id != receipt.id]
                    proposals.append(receipt)
                    return json.dumps(
                        {
                            "action_id": str(receipt.id),
                            "status": receipt.status,
                            "account_email": receipt.account_email,
                            "requires_confirmation": False,
                            "created": receipt.status == "succeeded",
                            "calendar": receipt.calendar.model_dump(mode="json")
                            if receipt.calendar
                            else None,
                            "url": str(receipt.result_url) if receipt.result_url else None,
                            "error": receipt.error,
                            "instruction": (
                                "Report this receipt accurately. Succeeded means created. "
                                "Never ask for confirmation. "
                                "Do not retry unknown or executing actions."
                            ),
                        },
                        ensure_ascii=False,
                    )
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
        if self.store.run(action.run_id):
            self.conversations.run(actor, action.run_id)
        else:
            attempt = self.store.attempt(action.run_id)
            if not attempt or attempt.household_id != actor.household_id:
                raise NotFoundError("action request not found")
            self.conversations.get(actor, attempt.run.thread_id)
        return action

    def actions(self, actor: ActorContext, thread_id: UUID) -> tuple[ActionProposal, ...]:
        self.conversations.get(actor, thread_id)
        return tuple(self.store.recent_actions(thread_id, actor.actor_id, 100))

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
            raise AuthorizationError("This legacy preview is retired. Request a new home action.")
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
        return self._dispatch(actor, action, connection, revalidate)

    def _dispatch(
        self,
        actor: ActorContext,
        action: ActionProposal,
        connection: GoogleConnection,
        revalidate: Callable[[], ActorContext],
    ) -> ActionProposal:
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
