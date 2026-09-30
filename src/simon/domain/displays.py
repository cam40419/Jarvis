from typing import Literal
from uuid import UUID, uuid4

from pydantic import AwareDatetime, Field, model_validator

from simon.domain.models import StrictModel, utc_now

DisplayLayout = Literal["overlay", "split", "focus"]
WidgetKind = Literal["clock", "power", "running_jobs", "printer", "message"]
WidgetPosition = Literal["top_left", "top_right", "bottom_left", "bottom_right", "center"]


class DisplayWidget(StrictModel):
    id: str = Field(pattern=r"^[a-z][a-z0-9_-]{1,39}$")
    kind: WidgetKind
    title: str = Field(default="", max_length=80)
    position: WidgetPosition
    message: str = Field(default="", max_length=500)

    @model_validator(mode="after")
    def message_only_for_message_widget(self) -> "DisplayWidget":
        if self.kind == "message" and not self.message.strip():
            raise ValueError("message widgets require message text")
        if self.kind != "message" and self.message:
            raise ValueError("message text is only valid for message widgets")
        return self


class DisplayConfiguration(StrictModel):
    layout: DisplayLayout = "overlay"
    slide_seconds: int = Field(default=30, ge=5, le=3600)
    dim_percent: int = Field(default=20, ge=0, le=80)
    widgets: tuple[DisplayWidget, ...] = (
        DisplayWidget(id="clock", kind="clock", position="top_left"),
        DisplayWidget(id="power", kind="power", title="Power", position="top_right"),
        DisplayWidget(
            id="jobs", kind="running_jobs", title="Running jobs", position="bottom_left"
        ),
    )

    @model_validator(mode="after")
    def unique_slots(self) -> "DisplayConfiguration":
        if len(self.widgets) > 5:
            raise ValueError("at most five widgets are supported")
        ids = [widget.id for widget in self.widgets]
        positions = [widget.position for widget in self.widgets]
        if len(set(ids)) != len(ids):
            raise ValueError("widget IDs must be unique")
        if len(set(positions)) != len(positions):
            raise ValueError("widget positions must be unique")
        return self


class ConfigureDisplay(StrictModel):
    display_id: UUID | None = None
    layout: DisplayLayout | None = None
    slide_seconds: int | None = Field(default=None, ge=5, le=3600)
    dim_percent: int | None = Field(default=None, ge=0, le=80)
    widgets: tuple[DisplayWidget, ...] | None = None


class CreateDisplay(StrictModel):
    name: str = Field(min_length=1, max_length=80)


class DisplayImage(StrictModel):
    id: UUID = Field(default_factory=uuid4)
    filename: str = Field(min_length=1, max_length=180)
    media_type: Literal["image/jpeg", "image/png", "image/webp", "image/gif"]
    byte_count: int = Field(ge=1, le=15 * 1024 * 1024)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    created_at: AwareDatetime = Field(default_factory=utc_now)


class DisplayDevice(StrictModel):
    id: UUID = Field(default_factory=uuid4)
    household_id: UUID
    created_by: UUID
    name: str = Field(min_length=1, max_length=80)
    token_hash: str = Field(pattern=r"^[0-9a-f]{64}$", repr=False)
    configuration: DisplayConfiguration = Field(default_factory=DisplayConfiguration)
    images: tuple[DisplayImage, ...] = ()
    revision: int = Field(default=1, ge=1)
    created_at: AwareDatetime = Field(default_factory=utc_now)
    updated_at: AwareDatetime = Field(default_factory=utc_now)
    last_seen_at: AwareDatetime | None = None
