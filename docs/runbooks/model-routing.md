# Configure worker model routing

The worker foundation selects configured models through `ModelRouter`. It is separate
from Simon's existing conversation model settings. Routing previews perform no network
requests. Calling `ModelEndpointClient.generate` executes one bounded text request.

## Inventory and credentials

Start with [model-endpoints.example.json](../../examples/agents/model-endpoints.example.json).
This is a standalone JSON array of `ModelEndpoint` objects; copy its entries into your
platform manifest's model collection when configuring the platform. Every example is
disabled until configured. `local-instruct` is a generic alias to replace with an
installed model's exact identifier, not a model download name. The Claude and Gemini
identifiers are explicit placeholders.

The OpenAI examples use documented IDs and reasoning settings:
[GPT-5.4 Mini](https://developers.openai.com/api/docs/models/gpt-5.4-mini) and
[GPT-6 Astra](https://developers.openai.com/api/docs/models/gpt-6-astra).
They demonstrate standard/frontier configuration; these assignments are deployment
policy, not comparative benchmark results or promises of account access. Example
context/output limits are conservative application caps, not provider maxima.

The generated starter manifest uses the documented 400,000-token context window
for the exact default `gpt-5.4-mini` model, allowing expanded project instructions,
tool definitions, and planning schemas to fit. Its output cap remains 4,096 tokens;
agent input, output, and spending limits still apply. This context capacity is
documented in the [official model details](https://developers.openai.com/api/docs/models/gpt-5.4-mini).
Other model IDs retain a conservative 32,768-token starter context limit until
their administrator verifies the deployment and updates the manifest. Existing
manifests retain their configured limits when starter defaults change.

Set `model`, `base_url`, `capabilities`, `context_window_tokens`, `max_output_tokens`,
and supported `reasoning_efforts` from the actual deployment. Unknown fields are
rejected. Set `enabled` only after verifying the account or local server. Model IDs
are unrestricted strings, so updates require configuration changes rather than an enum
change. Evaluate representative tasks before assigning `tier`.

`api_key_env` names an environment variable, never its secret value. Make that variable
available to the worker process through its service/container configuration. Direct use
of this library reads process environment variables; it does not load `.env` itself.
Cloud endpoints require a credential reference. Empty or missing referenced keys make
an endpoint ineligible. URLs require HTTPS, except loopback HTTP; credentials, query
strings, and fragments cannot be embedded in URLs.

## Selection policy

Both inputs use a 1–5 scale. Depth describes reasoning difficulty; importance describes
the consequences of a weak result. Start with user-provided values or explicit team
defaults; this foundation does not yet infer those scores from natural language.

| Maximum of depth and importance | Minimum tier | Preferred supported effort |
| ------------------------------- | ------------ | -------------------------- |
| 1–2                             | economy      | low                        |
| 3                               | standard     | medium                     |
| 4                               | frontier     | high                       |
| 5                               | frontier     | highest configured effort  |

The router first checks enabled state, credentials, required capabilities, privacy,
input-plus-output context capacity, output limits, minimum tier, and estimated budget.
It ranks eligible endpoints by lowest adequate tier, local execution, lower `priority`,
known cost, then ID. A larger adequate tier can run work when lower tiers are unavailable.
Important work never silently falls back below its quality floor.

`privacy="local_only"` excludes cloud endpoints. `local=true` is an administrator's
assertion about the deployment's data boundary. `model_override` names an inventory
endpoint and still obeys every constraint. Decisions include alternatives, rejection
reasons, and the original request. Unsupported reasoning settings are never invented;
an empty list leaves reasoning configuration to the provider.

Check the selected model's default before leaving `reasoning_efforts` empty.
For example, [GPT-5.4 Mini](https://developers.openai.com/api/docs/models/gpt-5.4-mini)
defaults to no reasoning and supports explicit reasoning effort.
[Reasoning tokens](https://developers.openai.com/api/docs/guides/reasoning)
share the response-token limit with visible output, so a report or completion
review needs room for both. Configure supported efforts and suitable endpoint and
profile ceilings; generated project tasks still obey routing and budget checks.
The provider recommends an initial allowance of at least 25,000 tokens for
reasoning plus output while measuring a workload. A reasoning-only response can
consume its entire allowance without producing visible text. Simon records that
as `model_output_truncated`, retaining usage and reasoning-token counts; explicit
provider refusals are recorded as `model_refused` without retaining their bodies.
Neither outcome triggers an automatic retry or replays a completed tool action.
These are known generation failures, not accepted final answers.

Run this offline preview from the repository root in PowerShell:

```powershell
@'
import json
from pathlib import Path
from simon.domain.model_routing import ModelEndpoint, RoutingRequest
from simon.services.model_router import ModelRouter

items = json.loads(Path("examples/agents/model-endpoints.example.json").read_text())
models = [ModelEndpoint.model_validate(item) for item in items]
local = models[0].model_copy(update={"enabled": True})
decision = ModelRouter([local], environ={}).route(RoutingRequest(
    depth=2, importance=2, privacy="local_only",
    input_tokens=1000, output_tokens=512, budget_usd=0,
))
print(decision.model_dump_json(indent=2))
'@ | .\venv\Scripts\python.exe -
```

## Local servers and execution limits

Install and run a model server separately. Ollama, vLLM, or another compatible server
must expose `/v1/chat/completions` and the configured model. Provide enough RAM/VRAM,
the model files, and an appropriate model license. A container's loopback address refers
to that container; use an authenticated HTTPS endpoint for other machines or containers.
See [Ollama compatibility](https://docs.ollama.com/api/openai-compatibility).

After configuring a real local endpoint, the Python dispatch call is:

```python
from simon.adapters.model_endpoints import ModelEndpointClient
from simon.domain.model_routing import TextGenerationRequest

result = ModelEndpointClient([local]).generate(
    decision, TextGenerationRequest(prompt="Draft a project checklist.", max_output_tokens=512)
)
print(result.text)
```

This text adapter supports [OpenAI Responses](https://developers.openai.com/api/reference/resources/responses/methods/create),
[Anthropic Messages](https://platform.claude.com/docs/en/api/messages/create), and
[Gemini generateContent](https://ai.google.dev/api/generate-content), plus compatible
local chat endpoints. It does not execute tool loops, images, video, or desktops;
those require the corresponding capability runner. Capability declarations alone do
not implement a tool. Truncated responses are marked, and failed requests are never
automatically retried.

For dispatched agents, the model-request transport timeout is at most 120 seconds,
or 300 seconds when the profile permits more than 8,192 output tokens. It is also
bounded by the profile's `timeout_seconds`. The overall worker deadline remains
in force between calls; cancellation cannot interrupt an in-flight synchronous
request. Tool transport timeouts are unchanged, and a timed-out model request is
never automatically retried.

Pricing fields are USD per million input/output tokens. Unknown cloud pricing blocks
budgeted routing. Unpriced local execution estimates zero API charges, excluding hardware
and electricity. Estimates use caller-supplied token counts; they are not a hard spending
cap or a token-count guarantee. The [dispatcher](agent-execution.md) now reserves the
aggregate estimated model cost of each run's permitted task loops before queueing.
Company-wide quotas, actual billing reconciliation and provider-specific pricing
adjustments remain future work. A timeout may incur cost even without a returned result.
