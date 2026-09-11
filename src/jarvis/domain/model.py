from typing import Literal, Protocol

from pydantic import Field

from jarvis.domain.models import StrictModel


class ModelRequest(StrictModel):
    model: str
    instructions: str
    input_text: str
    max_output_tokens: int = 2048
    input_token_limit: int = 20000
    store: Literal[False] = False


class ModelAnswer(StrictModel):
    text: str = Field(min_length=1, max_length=60000)
    response_id: str
    model: str
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)


class LanguageModel(Protocol):
    def generate(self, request: ModelRequest) -> ModelAnswer: ...
