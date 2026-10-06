"""Project-selected file locations; credentials stay in account connections."""

from typing import Any, Literal

from pydantic import Field, model_validator

from simon.domain.models import StrictModel

StorageProvider = Literal["local", "google_drive", "dropbox", "box", "onedrive", "webdav"]


class ProjectStorageLocation(StrictModel):
    id: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,63}$")
    name: str = Field(min_length=1, max_length=120)
    provider: StorageProvider
    connection_id: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9_.-]{1,79}$")
    account: str = Field(default="", max_length=254)
    folder_id: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_-]{1,256}$")
    path: str = Field(default="", max_length=1000)
    writable: bool = False
    linked_drive: bool = False

    @model_validator(mode="after")
    def provider_fields(self) -> "ProjectStorageLocation":
        if self.linked_drive and (self.provider != "google_drive" or self.id != "linked-drive"):
            raise ValueError("Only the existing project Drive link can follow its linked folder")
        if self.provider in {"local", "google_drive"} and self.connection_id:
            raise ValueError("Select a Google account or project-local folder")
        if self.provider != "google_drive" and (self.account or self.linked_drive):
            raise ValueError("Google account settings apply only to Google Drive")
        if self.provider != "google_drive" and self.folder_id:
            raise ValueError("Use the folder configured in Connections for this provider")
        if self.provider in {"google_drive", "box", "onedrive"} and self.path:
            raise ValueError("Use the selected folder for this provider")
        return self


class SaveProjectStorage(StrictModel):
    expected_version: int = Field(ge=0)
    locations: tuple[ProjectStorageLocation, ...] = Field(default=(), max_length=32)

    @model_validator(mode="after")
    def unique_locations(self) -> "SaveProjectStorage":
        if len({location.id for location in self.locations}) != len(self.locations):
            raise ValueError("Storage location identifiers must be unique")
        return self


class ProjectStorageRead(StrictModel):
    location_id: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,63}$")
    operation: Literal["list", "search", "metadata", "read", "sheet_read"] = "list"
    arguments: dict[str, Any] = Field(default_factory=dict)


class ProjectStorageWrite(StrictModel):
    location_id: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,63}$")
    operation: Literal["write", "create", "edit", "rename", "sheet_write", "folder_create"]
    arguments: dict[str, Any] = Field(default_factory=dict)
