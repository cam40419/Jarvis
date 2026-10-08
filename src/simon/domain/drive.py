"""Bounded account-scoped Google Drive browse input."""

from pydantic import Field

from simon.domain.models import StrictModel

DRIVE_ID = r"^[A-Za-z0-9_-]+$"


class DriveBrowse(StrictModel):
    account: str = Field(default="", max_length=254)
    folder_id: str = Field(default="root", min_length=1, max_length=256, pattern=DRIVE_ID)
    query: str = Field(default="", max_length=200)
    page_token: str = Field(default="", max_length=2048)
    folders_only: bool = True
    search_all: bool = False
