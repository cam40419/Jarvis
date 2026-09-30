from datetime import timedelta
from typing import Literal
from uuid import UUID, uuid4

from pydantic import AwareDatetime, Field, HttpUrl, field_validator, model_validator

from simon.domain.home import (
    HomeChange,
    HomeCommand,
    HomeControl,
    HomeOrganization,
    HomeOutletSetup,
    HomeQuery,
    HomeRename,
    HomeStatus,
)
from simon.domain.models import CapabilityDefinition, RiskClass, StrictModel, utc_now

ToolName = Literal[
    "google_accounts_list",
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
    "project_list",
    "project_create",
    "project_unlink_drive",
    "project_drive_trash",
    "project_link_drive",
    "project_sync",
    "project_files_list",
    "project_file_read",
    "project_file_create",
    "project_file_edit",
    "project_sheet_read",
    "project_sheet_write",
    "project_file_rename",
    "context_search",
    "memory_remember",
    "memory_forget",
    "task_create",
    "task_list",
    "task_control",
    "task_steer",
    "web_search",
    "calendar_list_events",
    "calendar_create_event",
    "propose_calendar_event",
    "propose_email",
    "gmail_search_messages",
    "gmail_read_message",
    "drive_list_folder",
    "drive_search_files",
    "drive_read_file",
    "home_list_devices",
    "home_get_status",
    "home_get_statuses",
    # Persisted ModelRequest snapshots retain retired names. Accept this for history;
    # ConnectedService.available and model_tools.definitions still forbid executing it.
    "propose_home_change",
    "home_control",
    "home_refresh_devices",
    "home_organize_devices",
    "home_rename_device",
    "home_setup_outlet",
    "display_list",
    "display_configure",
]


class GoogleStart(StrictModel):
    account: str = Field(default="", max_length=254)
    shared_chat_acknowledged: Literal[True]


class WebSource(StrictModel):
    title: str
    url: HttpUrl


class GoogleAccountSelect(StrictModel):
    account: str = Field(default="", max_length=254)


class CalendarQuery(GoogleAccountSelect):
    start: AwareDatetime
    end: AwareDatetime

    @model_validator(mode="after")
    def valid_window(self) -> "CalendarQuery":
        if not self.start < self.end <= self.start + timedelta(days=31):
            raise ValueError("calendar window must be positive and at most 31 days")
        return self


class CalendarDraft(CalendarQuery):
    title: str = Field(min_length=1, max_length=200)
    description: str = Field(default="", max_length=4000)
    location: str = Field(default="", max_length=500)


class GoogleSearch(GoogleAccountSelect):
    query: str = Field(default="", max_length=500)
    page_token: str = Field(default="", max_length=2048)
    limit: int = Field(default=10, ge=1, le=20)


class GoogleItem(GoogleAccountSelect):
    # Provider IDs only; never allow a model-supplied URL or path in API requests.
    id: str = Field(min_length=1, max_length=256, pattern=r"^[A-Za-z0-9_-]+$")


class EmailDraft(GoogleAccountSelect):
    to: str = Field(min_length=3, max_length=254, pattern=r"^[^\s<>@,;]+@[^\s<>@,;]+\.[^\s<>@,;]+$")
    subject: str = Field(min_length=1, max_length=200)
    body: str = Field(min_length=1, max_length=12000)

    @field_validator("to", "subject")
    @classmethod
    def no_header_injection(cls, value: str) -> str:
        if "\r" in value or "\n" in value:
            raise ValueError("email headers must be a single line")
        return value


class ActionProposal(StrictModel):
    id: UUID = Field(default_factory=uuid4)
    household_id: UUID
    actor_id: UUID
    run_id: UUID
    connection_id: UUID
    account_email: str = ""
    kind: Literal["calendar.create", "email.send", "home.set"]
    immediate: bool = False
    home: HomeChange | None = None
    device_name: str | None = None
    device_room: str | None = None
    device_provider: str | None = None
    home_observed: HomeStatus | None = None
    home_verified: bool = False
    calendar: CalendarDraft | None = None
    email: EmailDraft | None = None
    status: Literal["pending", "executing", "succeeded", "failed", "unknown", "cancelled"] = (
        "pending"
    )
    created_at: AwareDatetime = Field(default_factory=utc_now)
    expires_at: AwareDatetime = Field(default_factory=lambda: utc_now() + timedelta(minutes=30))
    provider_id: str | None = None
    result_url: HttpUrl | None = None
    error: str | None = None


class GoogleConnection(StrictModel):
    id: UUID = Field(default_factory=uuid4)
    actor_id: UUID
    household_id: UUID
    email: str
    scopes: tuple[str, ...]
    encrypted_tokens: str = Field(repr=False)
    is_default: bool = False


# The home dispatcher commits the durable action claim before running network I/O.
# Its write result is an action receipt containing observed state, not a bare status.
HOME_CAPABILITIES = {
    "rename": CapabilityDefinition(
        name="home.rename_device",
        version=1,
        description="Save a persistent Simon name for one household device.",
        risk=RiskClass.WRITE_SOFT,
        required_scopes=frozenset({"home:read", "home:organize"}),
        input_schema=HomeRename.model_json_schema(),
        output_schema={"type": "object"},
    ),
    "setup_outlet": CapabilityDefinition(
        name="home.setup_outlet",
        version=1,
        description="Owner identifies a discovered outlet's load and enables control.",
        risk=RiskClass.WRITE_SOFT,
        required_scopes=frozenset({"home:read", "identity:manage"}),
        input_schema=HomeOutletSetup.model_json_schema(),
        output_schema={"type": "object"},
    ),
    "organize": CapabilityDefinition(
        name="home.organize_devices",
        version=1,
        description="Set Simon rooms and groups for explicitly selected household devices.",
        risk=RiskClass.WRITE_SOFT,
        required_scopes=frozenset({"home:read", "home:organize"}),
        input_schema=HomeOrganization.model_json_schema(),
        output_schema={"type": "object"},
    ),
    "refresh": CapabilityDefinition(
        name="home.refresh_devices",
        version=1,
        description="Discover linked cloud devices and merge their inventory without actuation.",
        risk=RiskClass.WRITE_SOFT,
        required_scopes=frozenset({"home:read"}),
        input_schema=StrictModel.model_json_schema(),
        output_schema={"type": "array"},
    ),
    "list": CapabilityDefinition(
        name="home.list_devices",
        version=1,
        description="List configured household devices.",
        risk=RiskClass.READ,
        required_scopes=frozenset({"home:read"}),
        input_schema=StrictModel.model_json_schema(),
        output_schema={"type": "array", "items": {"type": "object"}},
    ),
    "read": CapabilityDefinition(
        name="home.get_status",
        version=1,
        description="Read one configured device's state.",
        risk=RiskClass.READ,
        required_scopes=frozenset({"home:read"}),
        input_schema=HomeQuery.model_json_schema(),
        output_schema=HomeStatus.model_json_schema(),
    ),
    "control": CapabilityDefinition(
        name="home.set",
        version=1,
        description="Immediate power, brightness, and color for enabled household loads.",
        risk=RiskClass.WRITE_SOFT,
        required_scopes=frozenset({"home:read", "home:control"}),
        input_schema=HomeControl.model_json_schema(),
        output_schema={"type": "array", "items": HomeCommand.model_json_schema()},
        idempotent=True,
    ),
}


class GoogleOAuthState(StrictModel):
    state_hash: str
    binding_hash: str
    encrypted_data: str = Field(repr=False)
    expires_at: AwareDatetime
