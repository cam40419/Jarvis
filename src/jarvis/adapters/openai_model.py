from openai import (
    APIConnectionError,
    APIError,
    APITimeoutError,
    AuthenticationError,
    OpenAI,
    PermissionDeniedError,
    RateLimitError,
)

from jarvis.domain.errors import ModelError
from jarvis.domain.model import ModelAnswer, ModelRequest


class OpenAIModel:
    def __init__(self, api_key: str) -> None:
        # Fixed official endpoint, finite timeouts, and no automatic generation retries.
        self.api_key = api_key

    def generate(self, request: ModelRequest) -> ModelAnswer:
        try:
            with OpenAI(
                api_key=self.api_key,
                base_url="https://api.openai.com/v1",
                timeout=30,
                max_retries=0,
            ) as client:
                count = client.responses.input_tokens.count(
                    model=request.model,
                    instructions=request.instructions,
                    input=request.input_text,
                )
                if count.input_tokens > request.input_token_limit:
                    raise ModelError("model_input_limit")
                response = client.responses.create(
                    model=request.model,
                    instructions=request.instructions,
                    input=request.input_text,
                    store=False,
                    max_output_tokens=request.max_output_tokens,
                    tools=[],
                    truncation="disabled",
                )
                if response.status != "completed" or not response.output_text.strip():
                    raise ModelError("model_incomplete")
                if response.usage is None or len(response.output_text) > 60000:
                    raise ModelError("model_incomplete")
                return ModelAnswer(
                    text=response.output_text,
                    response_id=response.id,
                    model=response.model,
                    input_tokens=response.usage.input_tokens,
                    output_tokens=response.usage.output_tokens,
                )
        except (AuthenticationError, PermissionDeniedError):
            raise ModelError("model_credentials") from None
        except RateLimitError:
            raise ModelError("model_rate_limit") from None
        except APITimeoutError:
            raise ModelError("model_timeout") from None
        except (APIConnectionError, APIError):
            raise ModelError() from None
