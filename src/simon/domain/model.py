from collections.abc import Callable
from typing import Literal, Protocol

from pydantic import Field

from simon.domain.connected_tools import ToolName, WebSource
from simon.domain.models import StrictModel

ProfileName = Literal["auto", "quick", "balanced", "deep"]
AnswerLength = Literal["brief", "normal", "detailed"]
ReasoningEffort = Literal["none", "low", "medium", "high"]


class ProfileSelection(StrictModel):
    version: Literal["profiles-v1", "profiles-v2"] = "profiles-v1"
    requested: ProfileName
    selected: Literal["quick", "balanced", "deep"]
    reason: str
    answer_length: AnswerLength
    reasoning_effort: ReasoningEffort | None = None


class ModelRequest(StrictModel):
    model: str
    instructions: str
    input_text: str
    max_output_tokens: int = 2048
    input_token_limit: int = 20000
    store: Literal[False] = False
    reasoning_effort: Literal["none", "low", "medium", "high"] = "none"
    verbosity: Literal["low", "medium", "high"] = "medium"
    timeout_seconds: int = 30
    tools: tuple[ToolName, ...] = ()


class ModelAnswer(StrictModel):
    text: str = Field(min_length=1, max_length=60000)
    response_id: str
    model: str
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    reasoning_tokens: int = 0
    token_count_ms: int = 0
    first_text_ms: int | None = None
    total_ms: int = 0
    web_sources: tuple[WebSource, ...] = ()
    tool_calls: tuple[str, ...] = ()


class LanguageModel(Protocol):
    def generate(self, request: ModelRequest) -> ModelAnswer: ...


class StreamingLanguageModel(LanguageModel, Protocol):
    def generate_stream(
        self, request: ModelRequest, on_delta: Callable[[str], None]
    ) -> ModelAnswer: ...


class ToolLanguageModel(LanguageModel, Protocol):
    def generate_with_tools(
        self,
        request: ModelRequest,
        on_delta: Callable[[str], None] | None,
        execute: Callable[[str, str], str],
    ) -> ModelAnswer: ...
