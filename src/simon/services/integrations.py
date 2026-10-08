"""UI-managed account connections shared by API and worker processes."""

import json
import os
import re
import smtplib
from collections.abc import Iterator, Mapping
from contextlib import suppress
from hashlib import sha256
from threading import RLock
from time import monotonic
from typing import Any, cast
from uuid import UUID, uuid4

from cryptography.fernet import Fernet, InvalidToken
from pydantic import SecretStr

from simon.adapters.clickup import ClickUpAdapter, _identifier, _rows
from simon.adapters.external_action_providers import provider_configuration_reasons
from simon.adapters.optional_http import BoundedHTTP
from simon.config import Settings
from simon.domain.clickup import BoardConnection
from simon.domain.errors import AuthorizationError, NotFoundError, ValidationError
from simon.domain.external_actions import ExternalProviderDefinition
from simon.domain.integrations import ConnectIntegration, IntegrationConnection, IntegrationProvider
from simon.domain.models import ActorContext, Channel
from simon.domain.ports import Store
from simon.domain.tool_catalog import ToolCatalogError, ToolDefinition


class IntegrationCredentials(Mapping[str, str]):
    """Resolve secrets per request so disconnects and rotations reach other processes."""

    def __init__(self, service: "IntegrationService", fallback: Mapping[str, str]) -> None:
        self.service, self.fallback = service, fallback

    def __getitem__(self, key: str) -> str:
        if key == "SIMON_OPENAI_API_KEY":
            record = self.service.openai_connection()
            if record:
                return self.service.decrypt(record.encrypted_secret)
        if not key.startswith("SIMON_LINK_"):
            return self.fallback[key]
        try:
            _, _, workspace, actor, identifier = key.split("_", 4)
            record = next(
                (
                    row
                    for row in self.service.store.integration_connections(
                        UUID(hex=workspace), UUID(hex=actor)
                    )
                    if row.id == identifier.lower()
                ),
                None,
            )
            if record is None:
                raise KeyError(key)
            return self.service.decrypt(record.encrypted_secret)
        except (ValueError, KeyError):
            raise KeyError(key) from None

    def __iter__(self) -> Iterator[str]:
        return iter(self.fallback)

    def __len__(self) -> int:
        return len(self.fallback)


class IntegrationService:
    def can_configure_email(self, actor: ActorContext) -> bool:
        return (
            actor.actor_id == self.settings.account_admin_actor_id
            and "identity:manage" in actor.scopes
        )

    def openai_connection(self) -> IntegrationConnection | None:
        admin = self.settings.account_admin_actor_id
        workspaces: tuple[UUID, ...]
        if self.settings.account_workspace_id:
            workspaces = (self.settings.account_workspace_id,)
        else:
            workspaces = tuple(
                membership.workspace_id for membership in self.store.memberships(admin)
            )
        return next(
            (
                record
                for workspace in workspaces
                for record in self.store.integration_connections(workspace, admin)
                if record.provider == "openai"
            ),
            None,
        )

    def email_connection(self) -> IntegrationConnection | None:
        admin = self.settings.account_admin_actor_id
        for membership in self.store.memberships(admin):
            record = next(
                (
                    row
                    for row in self.store.integration_connections(membership.workspace_id, admin)
                    if row.provider == "email"
                ),
                None,
            )
            if record:
                return record
        return None

    def __init__(
        self, store: Store, settings: Settings, *, http: BoundedHTTP | None = None
    ) -> None:
        self.store, self.settings = store, settings
        from simon.services.credentials import runtime_credentials

        self.credentials = IntegrationCredentials(self, runtime_credentials())
        if http is not None:
            http.environ = self.credentials
        self.clickup = ClickUpAdapter(http or BoundedHTTP(environ=self.credentials))
        self._workspace_cache: dict[str, tuple[float, str, tuple[BoardConnection, ...]]] = {}
        self._cache_lock = RLock()

    @property
    def cipher(self) -> Fernet:
        if self.settings.google_token_key:
            return Fernet(self.settings.google_token_key.get_secret_value().encode())
        path = self.settings.integration_key_file
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            # Write a complete key before making it visible to concurrent processes.
            temporary = path.with_name(path.name + "." + uuid4().hex)
            try:
                with temporary.open("xb") as stream:
                    os.chmod(temporary, 0o600)
                    stream.write(Fernet.generate_key())
                    stream.flush()
                    os.fsync(stream.fileno())
                with suppress(FileExistsError):
                    os.link(temporary, path)
            finally:
                temporary.unlink(missing_ok=True)
        try:
            return Fernet(path.read_bytes())
        except (ValueError, OSError):
            raise ValidationError("Connection encryption key is unavailable") from None

    def decrypt(self, value: str) -> str:
        try:
            return self.cipher.decrypt(value.encode()).decode()
        except InvalidToken:
            raise ValidationError(
                "Restore the connection encryption key or reconnect this account"
            ) from None

    @staticmethod
    def authorize(actor: ActorContext, *, write: bool = False) -> None:
        if ("jobs:write" if write else "jobs:read") not in actor.scopes:
            raise AuthorizationError("Connection access is not authorized")
        if write and actor.channel != Channel.API:
            raise AuthorizationError("Connect accounts through the authenticated UI")

    @staticmethod
    def credential_key(record: IntegrationConnection) -> str:
        return f"SIMON_LINK_{record.workspace_id.hex}_{record.actor_id.hex}_{record.id}".upper()

    @staticmethod
    def public(record: IntegrationConnection) -> dict[str, Any]:
        return {
            "id": record.id,
            "provider": record.provider,
            "name": record.name,
            "settings": record.settings,
        }

    def list(self, actor: ActorContext) -> list[dict[str, Any]]:
        self.authorize(actor)
        return [
            self.public(row)
            for row in self.store.integration_connections(actor.workspace_id, actor.actor_id)
        ]

    def disconnect(self, actor: ActorContext, identifier: str) -> None:
        self.authorize(actor, write=True)
        if not any(
            row.id == identifier
            for row in self.store.integration_connections(actor.workspace_id, actor.actor_id)
        ):
            raise NotFoundError("Connection not found")
        self.store.delete_integration_connection(actor.workspace_id, actor.actor_id, identifier)
        with self._cache_lock:
            self._workspace_cache.pop(identifier, None)

    def connect(
        self,
        actor: ActorContext,
        provider: str,
        body: ConnectIntegration,
        identifier: str | None = None,
    ) -> dict[str, Any]:
        self.authorize(actor, write=True)
        if provider not in {
            "clickup",
            "twilio",
            "gateway",
            "home",
            "google_app",
            "email",
            "github",
            "dropbox",
            "box",
            "onedrive",
            "webdav",
            "openai",
        }:
            raise ValidationError("Unsupported connection provider")
        if (
            provider in {"google_app", "openai"}
            and actor.actor_id != self.settings.account_admin_actor_id
        ):
            raise AuthorizationError(
                "Only the site administrator can configure shared application credentials"
            )
        if provider == "email" and not self.can_configure_email(actor):
            raise AuthorizationError("Only the site administrator can configure email delivery")
        secret = body.credential.get_secret_value().strip()
        if not secret or any(ord(char) <= 32 or ord(char) == 127 for char in secret):
            raise ValidationError("Enter a valid provider credential")
        if provider == "clickup" and not secret.startswith("pk_"):
            raise ValidationError("Enter your ClickUp personal API token")
        records = self.store.integration_connections(actor.workspace_id, actor.actor_id)
        if identifier is not None and not any(
            row.id == identifier and row.provider == provider for row in records
        ):
            raise NotFoundError("Connection not found")
        if identifier is None and len(records) >= 100:
            raise ValidationError("Disconnect an unused account before adding another")
        record = IntegrationConnection(
            id=identifier or "link_" + uuid4().hex,
            workspace_id=actor.workspace_id,
            actor_id=actor.actor_id,
            provider=cast(IntegrationProvider, provider),
            name=body.name.strip() or provider.title(),
            encrypted_secret=self.cipher.encrypt(secret.encode()).decode(),
        )
        key = self.credential_key(record)
        if provider == "openai":
            previous = next((row for row in records if row.provider == "openai"), None)
            if identifier is None and previous:
                record = record.model_copy(update={"id": previous.id})
        elif provider == "email":
            if not body.from_email:
                raise ValidationError("Enter the verified sender email address")
            if body.email_transport == "smtp" and not (body.smtp_host and body.smtp_username):
                raise ValidationError("Enter the SMTP server and username")
            record = record.model_copy(
                update={
                    "settings": {
                        "transport": body.email_transport,
                        "from_email": body.from_email.casefold(),
                        "smtp_host": body.smtp_host,
                        "smtp_port": body.smtp_port,
                        "smtp_username": body.smtp_username,
                    }
                }
            )
            previous = next((row for row in records if row.provider == "email"), None)
            if identifier is None and previous:
                record = record.model_copy(update={"id": previous.id})
        elif provider == "github":
            repositories = sorted({value.strip() for value in body.repositories})
            if not repositories or any(
                not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", value)
                or any(part in {".", ".."} for part in value.split("/"))
                for value in repositories
            ):
                raise ValidationError("Enter permitted GitHub repositories as owner/name")
            from simon.domain.tool_catalog import ToolExecutionError

            try:
                account = (
                    BoundedHTTP(transport=self.clickup.http.transport)
                    .request(
                        "GET",
                        "https://api.github.com/user",
                        headers={
                            "Authorization": "Bearer " + secret,
                            "Accept": "application/vnd.github+json",
                            "X-GitHub-Api-Version": "2026-03-10",
                        },
                        expected=frozenset({200}),
                    )
                    .json(write=False)
                )
            except ToolExecutionError:
                raise ValidationError(
                    "GitHub rejected the connection; check your access token"
                ) from None
            if not isinstance(account, dict) or not isinstance(account.get("login"), str):
                raise ValidationError("GitHub returned an invalid account")
            record = record.model_copy(
                update={
                    "settings": {
                        "repositories": repositories,
                        "write_enabled": body.github_write_enabled,
                        "login": account["login"][:100],
                    }
                }
            )
            previous = next((row for row in records if row.provider == "github"), None)
            if identifier is None and previous:
                record = record.model_copy(update={"id": previous.id})
        elif provider in {"dropbox", "box", "onedrive", "webdav"}:
            record = record.model_copy(
                update={
                    "settings": {
                        "endpoint": body.endpoint,
                        "username": body.username,
                        "root_path": body.root_path,
                        "root_folder_id": body.root_folder_id,
                        "drive_id": body.drive_id,
                        "root_item_id": body.root_item_id,
                        "download_hosts": body.download_hosts,
                        "write_enabled": body.storage_write_enabled,
                    }
                }
            )
            previous = next((row for row in records if row.provider == provider), None)
            if identifier is None and previous:
                record = record.model_copy(update={"id": previous.id})
            from simon.adapters.cloud_storage_tools import _settings

            try:
                if provider == "webdav":
                    from simon.adapters.optional_http import connection_endpoint

                    if not body.username:
                        raise ToolCatalogError("Enter a WebDAV username")
                    connection_endpoint(self._storage_tools(record)[0])
                else:
                    for definition in self._storage_tools(record):
                        _settings(definition)
            except ToolCatalogError as error:
                raise ValidationError(str(error)) from None
            # Read-only scope and credential check; no files are written while linking.
            self._probe_storage(record, secret)
        elif provider == "clickup":
            probe = ClickUpAdapter(
                BoundedHTTP(environ={key: secret}, transport=self.clickup.http.transport)
            )
            connection = self._board(record, "1")
            teams = _rows(probe._request(connection, "GET", "/team"), "teams", maximum=1000)
            workspaces = [
                {
                    "id": _identifier(str(team.get("id")), numeric=True),
                    "name": str(team.get("name", "ClickUp workspace"))[:100],
                }
                for team in teams
            ]
            if not workspaces:
                raise ValidationError("This ClickUp account has no accessible Workspaces")
            record = record.model_copy(update={"settings": {"workspaces": workspaces}})
        elif provider == "home":
            from simon.adapters.home_client import HomeClient

            try:
                HomeClient(
                    self.settings.model_copy(
                        update={
                            "home_api_url": body.endpoint or "",
                            "home_api_token": SecretStr(secret),
                        }
                    )
                )
            except ValueError:
                raise ValidationError("Enter an HTTPS or local RobbinsHome address") from None
            if not body.endpoint:
                raise ValidationError("Enter the RobbinsHome address")
            record = record.model_copy(update={"settings": {"endpoint": body.endpoint}})
            previous = next((row for row in records if row.provider == "home"), None)
            if identifier is None and previous:
                record = record.model_copy(update={"id": previous.id})
        elif provider == "google_app":
            if not body.client_id or not re.fullmatch(
                r"[A-Za-z0-9_.-]+\.apps\.googleusercontent\.com", body.client_id
            ):
                raise ValidationError("Enter the Google OAuth client ID")
            record = record.model_copy(update={"settings": {"client_id": body.client_id}})
            previous = next((row for row in records if row.provider == "google_app"), None)
            if identifier is None and previous:
                record = record.model_copy(update={"id": previous.id})
        else:
            options = {
                "account_sid": body.account_sid,
                "from_number": body.from_number,
                "endpoint": body.endpoint,
                "merchant_names": body.merchant_names,
                "call_recipients": sorted(body.call_recipients),
            }
            record = record.model_copy(update={"settings": options})
            reasons = provider_configuration_reasons(self._external(record), {key: secret})
            if reasons:
                raise ValidationError("; ".join(reasons))
            if any(
                not re.fullmatch(r"\+[1-9][0-9]{7,14}", number) for number in body.call_recipients
            ):
                raise ValidationError("Enter complete international recipient phone numbers")
        self.store.save_integration_connection(record)
        with self._cache_lock:
            self._workspace_cache.pop(record.id, None)
        return self.public(record)

    def _storage_tools(self, record: IntegrationConnection) -> tuple[ToolDefinition, ...]:
        from simon.adapters.cloud_storage_tools import (
            box_tool_definitions,
            dropbox_tool_definitions,
            onedrive_tool_definitions,
        )
        from simon.adapters.webdav_tools import webdav_tool_definitions

        options: dict[str, Any] = {
            "enabled": True,
            "workspace_id": record.workspace_id,
            "actor_ids": (record.actor_id,),
            "credential_env": self.credential_key(record),
        }
        if record.provider == "webdav":
            return webdav_tool_definitions(
                **options,
                endpoint=record.settings.get("endpoint"),
                username=record.settings.get("username") or "",
            )
        if record.provider == "dropbox":
            return dropbox_tool_definitions(
                **options, root_path=record.settings.get("root_path") or ""
            )
        if record.provider == "box":
            return box_tool_definitions(
                **options, root_folder_id=record.settings.get("root_folder_id") or ""
            )
        return onedrive_tool_definitions(
            **options,
            drive_id=record.settings.get("drive_id") or "",
            root_item_id=record.settings.get("root_item_id") or "",
            download_hosts=record.settings.get("download_hosts") or [],
        )

    def _probe_storage(self, record: IntegrationConnection, secret: str) -> None:
        from urllib.parse import quote

        from simon.domain.tool_catalog import ToolExecutionError

        http = BoundedHTTP(transport=self.clickup.http.transport)
        headers = {"Authorization": "Bearer " + secret}
        try:
            if record.provider == "webdav":
                import base64

                from simon.adapters.webdav_tools import _PROPFIND

                encoded = base64.b64encode(
                    (record.settings["username"] + ":" + secret).encode()
                ).decode()
                http.request(
                    "PROPFIND",
                    record.settings["endpoint"].rstrip("/") + "/",
                    headers={
                        "Authorization": "Basic " + encoded,
                        "Depth": "0",
                        "Content-Type": "application/xml",
                    },
                    content=_PROPFIND,
                    expected=frozenset({207}),
                )
            elif record.provider == "dropbox":
                http.request(
                    "POST",
                    "https://api.dropboxapi.com/2/files/get_metadata",
                    headers={**headers, "Content-Type": "application/json"},
                    content=json.dumps({"path": record.settings["root_path"]}).encode(),
                    expected=frozenset({200}),
                )
            elif record.provider == "box":
                http.request(
                    "GET",
                    "https://api.box.com/2.0/folders/" + str(record.settings["root_folder_id"]),
                    headers=headers,
                    expected=frozenset({200}),
                )
            else:
                http.request(
                    "GET",
                    "https://graph.microsoft.com/v1.0/drives/"
                    + quote(str(record.settings["drive_id"]), safe="")
                    + "/items/"
                    + quote(str(record.settings["root_item_id"]), safe=""),
                    headers=headers,
                    expected=frozenset({200}),
                )
        except ToolExecutionError:
            raise ValidationError(
                "The storage connection test failed; check the token and selected folder"
            ) from None

    def test(self, actor: ActorContext, identifier: str) -> dict[str, Any]:
        """Explicit read-only probes never send a message, upload or place an order."""
        self.authorize(actor, write=True)
        record = next(
            (
                row
                for row in self.store.integration_connections(actor.workspace_id, actor.actor_id)
                if row.id == identifier
            ),
            None,
        )
        if record is None:
            raise NotFoundError("Connection not found")
        if record.provider == "email" and not self.can_configure_email(actor):
            raise AuthorizationError("Only the site administrator can test email delivery")
        import base64
        from urllib.parse import quote

        from simon.domain.models import utc_now
        from simon.domain.tool_catalog import ToolExecutionError

        secret = self.decrypt(record.encrypted_secret)
        http = BoundedHTTP(transport=self.clickup.http.transport)
        headers = {"Authorization": "Bearer " + secret}
        status, message = "passed", "Connection test passed."
        try:
            if record.provider in {"dropbox", "box", "onedrive", "webdav"}:
                self._probe_storage(record, secret)
            elif record.provider == "openai":
                http.request(
                    "GET",
                    "https://api.openai.com/v1/models",
                    headers=headers,
                    expected=frozenset({200}),
                )
                message = "API authentication passed. No model generation was requested."
            elif record.provider == "clickup":
                self.clickup._request(self._board(record, "1"), "GET", "/team")
            elif record.provider == "github":
                for repository in record.settings["repositories"]:
                    http.request(
                        "GET",
                        "https://api.github.com/repos/" + quote(repository, safe="/"),
                        headers={**headers, "Accept": "application/vnd.github+json"},
                        expected=frozenset({200}),
                    )
                message = (
                    "Repository read access passed. Writes follow the saved connection permissions."
                )
            elif record.provider == "twilio":
                sid = record.settings["account_sid"]
                http.request(
                    "GET",
                    "https://api.twilio.com/2010-04-01/Accounts/" + sid + ".json",
                    headers={
                        "Authorization": "Basic "
                        + base64.b64encode((sid + ":" + secret).encode()).decode()
                    },
                    expected=frozenset({200}),
                )
            elif record.provider == "home":
                http.request(
                    "GET",
                    str(record.settings["endpoint"]).rstrip("/") + "/health",
                    headers=headers,
                    expected=frozenset({200}),
                )
            elif record.provider == "gateway":
                http.request(
                    "GET",
                    str(record.settings["endpoint"]).rstrip("/") + "/health",
                    headers=headers,
                    expected=frozenset({200}),
                )
                message = (
                    "Gateway health check passed; merchant operations still depend on its API."
                )
            elif record.provider == "email" and record.settings["transport"] == "smtp":
                import ssl

                options = record.settings
                connection: smtplib.SMTP
                if options["smtp_port"] == 465:
                    connection = smtplib.SMTP_SSL(
                        options["smtp_host"], 465, timeout=10, context=ssl.create_default_context()
                    )
                else:
                    connection = smtplib.SMTP(options["smtp_host"], 587, timeout=10)
                with connection:
                    if options["smtp_port"] == 587:
                        connection.starttls(context=ssl.create_default_context())
                    connection.login(options["smtp_username"], secret)
                message = "SMTP authentication passed. No email was sent."
            else:
                status = "configuration_only"
                message = (
                    "Settings saved; authorize Google or verify your recovery email to test access."
                )
        except (ToolExecutionError, ValidationError, OSError, ValueError, smtplib.SMTPException):
            status, message = (
                "failed",
                "Test failed. Check the credential, permissions and service address.",
            )
        checked = {"status": status, "message": message, "checked_at": utc_now().isoformat()}
        with self.store.transaction(actor.workspace_id):
            latest = next(
                (
                    row
                    for row in self.store.integration_connections(
                        actor.workspace_id, actor.actor_id
                    )
                    if row.id == record.id
                ),
                None,
            )
            if latest is None:
                raise NotFoundError("This account was disconnected while its test was running")
            if latest.encrypted_secret != record.encrypted_secret or {
                key: value for key, value in latest.settings.items() if key != "connection_test"
            } != {key: value for key, value in record.settings.items() if key != "connection_test"}:
                return {
                    "status": "configuration_changed",
                    "message": "Connection changed during the test; test its new settings again.",
                    "checked_at": checked["checked_at"],
                }
            self.store.save_integration_connection(
                latest.model_copy(
                    update={"settings": {**latest.settings, "connection_test": checked}}
                )
            )
        return checked

    def bind_tool(self, definition: ToolDefinition, actor: ActorContext) -> ToolDefinition:
        if definition.settings.get("ui_managed") is not True:
            return definition
        if definition.transport == "github":
            return self.bind_github(definition, actor)
        self.authorize(actor)
        record = next(
            (
                row
                for row in self.store.integration_connections(actor.workspace_id, actor.actor_id)
                if row.provider == definition.transport
            ),
            None,
        )
        if record is None:
            raise ToolCatalogError(
                "Connect " + definition.transport.title() + " in Connections to use this tool"
            )
        if record.settings.get("connection_test", {}).get("status") == "failed":
            raise ToolCatalogError(
                "This connection failed its last test; reconnect and test it in Connections"
            )
        if definition.side_effect and record.settings.get("write_enabled") is not True:
            raise ToolCatalogError("Enable file writes in your storage connection")
        return next(tool for tool in self._storage_tools(record) if tool.id == definition.id)

    def bind_github(self, definition: ToolDefinition, actor: ActorContext) -> ToolDefinition:
        """Resolve the caller's current grant and encrypted credential for every invocation."""
        if definition.settings.get("ui_managed") is not True:
            return definition
        self.authorize(actor)
        record = next(
            (
                row
                for row in self.store.integration_connections(actor.workspace_id, actor.actor_id)
                if row.provider == "github"
            ),
            None,
        )
        if record is None:
            raise ToolCatalogError("Connect GitHub in Connections to use this skill")
        if record.settings.get("connection_test", {}).get("status") == "failed":
            raise ToolCatalogError(
                "GitHub failed its last test; reconnect and test it in Connections"
            )
        if definition.side_effect and record.settings.get("write_enabled") is not True:
            raise ToolCatalogError("Enable issue and draft PR creation in your GitHub connection")
        from simon.adapters.github_tools import github_tool_definitions

        tools = github_tool_definitions(
            enabled=True,
            workspace_id=actor.workspace_id,
            actor_ids=(actor.actor_id,),
            repositories=record.settings["repositories"],
            credential_env=self.credential_key(record),
        )
        return next(tool for tool in tools if tool.id == definition.id)

    def _board(
        self, record: IntegrationConnection, workspace: str, name: str = ""
    ) -> BoardConnection:
        return BoardConnection(
            id=record.id + "_" + workspace,
            name=(record.name + " · " + name)[:100],
            enabled=True,
            workspace_id=record.workspace_id,
            actor_ids=frozenset({record.actor_id}),
            credential_env=self.credential_key(record),
            clickup_workspace_id=workspace,
            discover_lists=True,
        )

    def board_connections(self, actor: ActorContext) -> tuple[BoardConnection, ...]:
        self.authorize(actor)
        connections: list[BoardConnection] = []
        for record in self.store.integration_connections(actor.workspace_id, actor.actor_id):
            if record.provider == "clickup":
                token_hash = sha256(self.decrypt(record.encrypted_secret).encode()).hexdigest()
                with self._cache_lock:
                    cached = self._workspace_cache.get(record.id)
                if cached and cached[0] > monotonic() and cached[1] == token_hash:
                    connections.extend(cached[2])
                    continue
                teams = _rows(
                    self.clickup._request(self._board(record, "1"), "GET", "/team"),
                    "teams",
                    maximum=1000,
                )
                discovered = tuple(
                    self._board(
                        record,
                        _identifier(str(team.get("id")), numeric=True),
                        str(team.get("name", "Workspace")),
                    )
                    for team in teams
                )
                with self._cache_lock:
                    if len(self._workspace_cache) >= 100:
                        self._workspace_cache.clear()
                    self._workspace_cache[record.id] = (monotonic() + 30, token_hash, discovered)
                connections.extend(discovered)
        return tuple(connections)

    def board_connection(self, actor: ActorContext, identifier: str) -> BoardConnection | None:
        for record in self.store.integration_connections(actor.workspace_id, actor.actor_id):
            if record.provider == "clickup" and identifier.startswith(record.id + "_"):
                return self._board(
                    record, _identifier(identifier[len(record.id) + 1 :], numeric=True)
                )
        return None

    def _external(self, record: IntegrationConnection) -> ExternalProviderDefinition:
        assert record.provider in {"twilio", "gateway"}
        return ExternalProviderDefinition(
            id=record.id,
            name=record.name,
            kind="twilio" if record.provider == "twilio" else "gateway",
            enabled=True,
            workspace_id=record.workspace_id,
            actor_ids=frozenset({record.actor_id}),
            credential_env=self.credential_key(record),
            **record.settings,
        )

    def external_connections(self, actor: ActorContext) -> tuple[ExternalProviderDefinition, ...]:
        return tuple(
            self._external(record)
            for record in self.store.integration_connections(actor.workspace_id, actor.actor_id)
            if record.provider in {"twilio", "gateway"}
        )

    def home_settings(self, actor: ActorContext) -> Settings:
        record = next(
            (
                row
                for row in self.store.integration_connections(actor.workspace_id, actor.actor_id)
                if row.provider == "home"
            ),
            None,
        )
        if record is None:
            return self.settings
        return self.settings.model_copy(
            update={
                "home_api_url": record.settings["endpoint"],
                "home_api_token": SecretStr(self.decrypt(record.encrypted_secret)),
            }
        )

    def google_settings(self) -> Settings:
        admin = self.settings.account_admin_actor_id
        for membership in self.store.memberships(admin):
            record = next(
                (
                    row
                    for row in self.store.integration_connections(membership.workspace_id, admin)
                    if row.provider == "google_app"
                ),
                None,
            )
            if record:
                return self.settings.model_copy(
                    update={
                        "google_client_id": record.settings["client_id"],
                        "google_client_secret": SecretStr(self.decrypt(record.encrypted_secret)),
                        "google_token_key": self.settings.google_token_key
                        or SecretStr(self.settings.integration_key_file.read_text().strip()),
                    }
                )
        return self.settings
