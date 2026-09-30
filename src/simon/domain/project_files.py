"""Account-bound Drive project folders and durable file-operation receipts."""

from typing import Any, Literal
from uuid import UUID, uuid4

from pydantic import AwareDatetime, Field

from simon.domain.models import StrictModel, utc_now

DRIVE_ID = r"^[A-Za-z0-9_-]+$"


class ProjectDrive(StrictModel):
    project_id: UUID
    household_id: UUID
    actor_id: UUID
    google_email: str = ""
    folder_id: str | None = None
    enabled: bool = True
    status: Literal["pending", "ready", "needs_permission", "error", "unlinked"] = "pending"
    error: str | None = None
    version: int = 1
    last_synced_at: AwareDatetime | None = None
    lease_until: AwareDatetime = Field(default_factory=utc_now)
    next_sync_at: AwareDatetime = Field(default_factory=utc_now)


class ProjectFileOperation(StrictModel):
    id: UUID = Field(default_factory=uuid4)
    project_id: UUID
    household_id: UUID
    actor_id: UUID
    google_email: str
    request_digest: str
    kind: str
    status: Literal["executing", "succeeded", "failed", "unknown"] = "executing"
    file_id: str | None = None
    result: dict[str, Any] = Field(default_factory=dict)
    before: dict[str, Any] = Field(default_factory=dict, repr=False)
    error: str | None = None
    created_at: AwareDatetime = Field(default_factory=utc_now)


class ProjectCreate(StrictModel):
    account: str = Field(default="", max_length=254)
    name: str = Field(min_length=1, max_length=200, pattern=r"\S")
    description: str = Field(min_length=1, max_length=1000, pattern=r"\S")
    idempotency_key: str = Field(min_length=8, max_length=200)


class ProjectSelect(StrictModel):
    project_id: UUID


class ProjectBind(ProjectSelect):
    account: str = Field(default="", max_length=254)
    folder_id: str = Field(min_length=1, max_length=256, pattern=DRIVE_ID)
    expected_version: int = Field(ge=0)


class ProjectFiles(ProjectSelect):
    folder_id: str | None = Field(default=None, max_length=256, pattern=DRIVE_ID)
    query: str = Field(default="", max_length=200)
    page_token: str = Field(default="", max_length=2048)


class ProjectFile(ProjectSelect):
    file_id: str = Field(min_length=1, max_length=256, pattern=DRIVE_ID)


class ProjectFileCreate(ProjectSelect):
    name: str = Field(min_length=1, max_length=200, pattern=r"\S")
    content: str = Field(default="", max_length=200000)
    format: Literal["text", "google_doc", "google_sheet", "folder"] = "text"
    folder_id: str | None = Field(default=None, max_length=256, pattern=DRIVE_ID)


class ProjectFileEdit(ProjectFile):
    revision: str = Field(min_length=1, max_length=256)
    old_text: str = Field(default="", max_length=200000)
    new_text: str = Field(max_length=200000)
    tab_id: str | None = Field(default=None, max_length=256, pattern=DRIVE_ID)


class ProjectSheetRead(ProjectFile):
    range: str = Field(min_length=1, max_length=200)


class ProjectSheetWrite(ProjectSheetRead):
    revision: str = Field(min_length=1, max_length=256)
    values: list[list[str | int | float | bool | None]] = Field(min_length=1, max_length=100)


class ProjectFileRename(ProjectFile):
    name: str = Field(min_length=1, max_length=200, pattern=r"\S")
    revision: str = Field(min_length=1, max_length=256)


class DriveBrowse(StrictModel):
    account: str = Field(default="", max_length=254)
    folder_id: str = Field(default="root", min_length=1, max_length=256, pattern=DRIVE_ID)
    query: str = Field(default="", max_length=200)
    page_token: str = Field(default="", max_length=2048)
    folders_only: bool = True
    search_all: bool = False


class ProjectUnlink(ProjectSelect):
    expected_version: int = Field(ge=0)


class ProjectTrash(ProjectFile):
    account: str = Field(default="", max_length=254)
    revision: str = Field(min_length=1, max_length=256)
