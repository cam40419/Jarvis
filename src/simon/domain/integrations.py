"""Private persisted connections; only explicit public views cross the API boundary."""

from typing import Any, Literal
from uuid import UUID

from pydantic import Field, SecretStr

from simon.domain.email_identity import EmailAddress
from simon.domain.models import StrictModel

IntegrationProvider = Literal[
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
]


class IntegrationConnection(StrictModel):
    id: str = Field(pattern=r"^[a-z][a-z0-9_.-]{1,79}$")
    workspace_id: UUID
    actor_id: UUID
    provider: IntegrationProvider
    name: str = Field(min_length=1, max_length=100)
    settings: dict[str, Any] = Field(default_factory=dict)
    encrypted_secret: str = Field(repr=False)


class ConnectIntegration(StrictModel):
    name: str = Field(default="", max_length=100)
    credential: SecretStr = Field(min_length=1, max_length=4096)
    account_sid: str | None = Field(default=None, max_length=100)
    client_id: str | None = Field(default=None, max_length=300)
    from_number: str | None = Field(default=None, max_length=30)
    endpoint: str | None = Field(default=None, max_length=1000)
    merchant_names: dict[str, str] = Field(default_factory=dict, max_length=100)
    call_recipients: frozenset[str] = Field(default=frozenset(), max_length=100)
    email_transport: Literal["resend", "smtp"] = "resend"
    from_email: EmailAddress | None = None
    smtp_host: str | None = Field(default=None, max_length=253, pattern=r"^[A-Za-z0-9.-]+$")
    smtp_port: Literal[465, 587] = 587
    smtp_username: str | None = Field(default=None, max_length=254, pattern=r"^[^\r\n]+$")
    repositories: list[str] = Field(default_factory=list, max_length=100)
    github_write_enabled: bool = False
    username: str | None = Field(default=None, max_length=200, pattern=r"^[^:\r\n]+$")
    root_path: str | None = Field(default=None, max_length=1000)
    root_folder_id: str | None = Field(default=None, max_length=30)
    drive_id: str | None = Field(default=None, max_length=250)
    root_item_id: str | None = Field(default=None, max_length=250)
    download_hosts: list[str] = Field(default_factory=list, max_length=20)
    storage_write_enabled: bool = False
