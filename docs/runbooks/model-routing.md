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
| --- | --- | --- |
| 1–2 | economy | low |
| 3 | standard | medium |
| 4 | frontier | high |
| 5 | frontier | highest configured effort |

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

Pricing fields are USD per million input/output tokens. Unknown cloud pricing blocks
budgeted routing. Unpriced local execution estimates zero API charges, excluding hardware
and electricity. Estimates use caller-supplied token counts; they are not a hard spending
cap or a token-count guarantee. The [dispatcher](agent-execution.md) now reserves the
aggregate estimated model cost of each run's permitted task loops before queueing.
Company-wide quotas, actual billing reconciliation and provider-specific pricing
adjustments remain future work. A timeout may incur cost even without a returned result.
