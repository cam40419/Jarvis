"""Shared configuration for managed boards in the API and standalone dispatcher."""

from simon.adapters.clickup import ClickUpAdapter, load_board_connections
from simon.adapters.optional_http import BoundedHTTP
from simon.config import Settings
from simon.domain.ports import Store
from simon.services.integrations import IntegrationService
from simon.services.project_boards import ProjectBoardService
from simon.services.project_work import ProjectWorkService


def project_board_service(
    settings: Settings,
    store: Store,
    work: ProjectWorkService,
    *,
    integrations: IntegrationService | None = None,
) -> ProjectBoardService:
    integrations = integrations or IntegrationService(store, settings)
    return ProjectBoardService(
        store,
        work,
        ClickUpAdapter(BoundedHTTP(environ=integrations.credentials)),
        load_board_connections(settings.project_boards_file),
        integrations=integrations,
    )
