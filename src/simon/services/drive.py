"""Read connected Drive folders using current account permissions."""

from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from simon.adapters.drive import FOLDER, DriveAPI
from simon.adapters.google import DRIVE_READ_SCOPE, DRIVE_WRITE_SCOPE, ConnectedError
from simon.domain.drive import DriveBrowse
from simon.domain.errors import AuthorizationError, ValidationError
from simon.domain.models import ActorContext

if TYPE_CHECKING:
    from simon.services.connected import ConnectedService


class DriveService:
    def __init__(self, connected: "ConnectedService") -> None:
        self.connected = connected
        self.api = DriveAPI()

    def browse(
        self,
        actor: ActorContext,
        request: DriveBrowse,
        revalidate: Callable[[], ActorContext],
    ) -> dict[str, Any]:
        self.connected.conversations.authorize(actor, "threads:read")
        connection = self.connected.connection(actor, account=request.account)
        if not {DRIVE_READ_SCOPE, DRIVE_WRITE_SCOPE}.intersection(connection.scopes):
            raise ConnectedError("Reconnect this Google account with Drive access.")
        token = self.connected.access_token(actor, connection)
        folder = self.api.metadata(token, request.folder_id)
        if folder["mimeType"] != FOLDER:
            raise ValidationError("Select a Drive folder to browse.")
        result = self.api.list_files(
            token,
            folder["id"],
            request.query,
            request.page_token,
            folders_only=request.folders_only,
            search_all=request.search_all,
        )
        current = revalidate()
        if (current.actor_id, current.workspace_id) != (actor.actor_id, actor.workspace_id):
            raise AuthorizationError("Drive access changed.")
        self.connected.conversations.authorize(current, "threads:read")
        latest = self.connected.connection(current, connection.id)
        if not {DRIVE_READ_SCOPE, DRIVE_WRITE_SCOPE}.intersection(latest.scopes):
            raise AuthorizationError("Drive access changed.")
        return {"account_email": connection.email, "folder": folder, **result}
