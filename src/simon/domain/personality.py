from typing import Literal

from pydantic import Field

from simon.domain.models import StrictModel

VoiceName = Literal["marin", "cedar", "meridian", "vesper"]


class AssistantPersona(StrictModel):
    preset: Literal["simon", "jarvis", "custom"] = "simon"
    address_as: str = Field(default="", max_length=60, pattern=r"^[^\r\n\x00-\x1f]*$")
    voice: VoiceName | None = None
    instructions: str = Field(default="", max_length=2000)
