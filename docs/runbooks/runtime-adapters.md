# Runtime adapter libraries

The [native project platform](native-projects.md) owns project, task and staffing records.
The modules below are retained, independently tested building blocks. Creating a native
agent or issuing a board credential does not start a model, mount files, acquire a
machine, or authorize an external action. Native project execution and tool enrollment
must connect these components through the project permission and review policies in
the [platform plan](../architecture/autonomous-work-platform-plan.md).

## Models and bounded work

| Component                                                          | Current library contract                                                                                                                                                                |
| ------------------------------------------------------------------ | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| [Worker assignment](../../src/simon/domain/worker_assignment.py)   | Immutable prompt, model, tool and output requirements for a single bounded invocation. These are execution inputs, not a staffing catalog.                                              |
| [Model router](../../src/simon/services/model_router.py)           | Capability, privacy and budget checks against explicit endpoint configuration. A routing estimate is not account billing or a project spending ledger.                                  |
| [Endpoint client](../../src/simon/adapters/model_endpoints.py)     | OpenAI Responses, compatible HTTP APIs, Anthropic and Gemini transports; local endpoints use the compatible API contract. No model provider owns business state.                        |
| [Agent worker](../../src/simon/services/agent_worker.py)           | Bounded model/tool loop, checkpoints, cancellation, evidence paging, explicit output schema and completion review. Tool effects are not automatically retried after an unknown outcome. |
| [Completion review](../../src/simon/services/worker_completion.py) | Controller-numbered source passages, grounded references, one bounded repair and structured format correction. A review cannot grant tools or change execution limits.                  |

[Endpoint examples](../../examples/agents/model-endpoints.example.json) demonstrate library
inputs. They are not loaded as project settings or a team manifest by the application.
Chat still has its own configured provider/profile path; unifying it with project model
policy is a later delivery step. Local inference requires a separately operated model
server and its own capacity controls.

## Tools and external services

| Library                                                                                                                 | Existing operations and boundary                                                                                                                                |
| ----------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| [Transport registry](../../src/simon/adapters/tool_transports.py), [catalog](../../src/simon/services/tool_catalog.py)  | Validate schema, explicit tool grant, action policy, scopes and bounded outcomes. Models do not supply identity or credentials.                                 |
| [Git](../../src/simon/adapters/git_tools.py), [GitHub](../../src/simon/adapters/github_tools.py)                        | Isolated repository operations and account-scoped repository API calls. Remote publishing still needs an explicit policy and review path.                       |
| [Browser](../../src/simon/adapters/browser_tools.py), [research](../../src/simon/adapters/web_research.py)              | Bounded origin-controlled reads/screenshots and sourced search. They are not a logged-in general browser automation product.                                    |
| [Processing](../../src/simon/adapters/processing_tools.py), [documents](../../src/simon/services/document_rendering.py) | Bounded media operations and report rendering. A rendered document still needs artifact admission and review.                                                   |
| [Generative tools](../../src/simon/adapters/generative_tools.py)                                                        | Provider image generation/audio operations. Provider charges, reference images and creative iteration need project-level integration.                           |
| [CAD](../../src/simon/adapters/cad_tools.py), [PCB](../../src/simon/adapters/pcb_tools.py)                              | Isolated engineering operations with file/output limits. Design correctness and manufacturability remain separate acceptance checks.                            |
| [Application tools](../../src/simon/adapters/application_tools.py)                                                      | Host protocol for selected creative applications. Licensed application availability and connector-host acceptance are deployment prerequisites.                 |
| [Cloud storage](../../src/simon/adapters/cloud_storage_tools.py), [WebDAV](../../src/simon/adapters/webdav_tools.py)    | Provider adapters with explicit credentials/resource grants. These are not a project file authority or automatic replication service.                           |
| [Google reads](../../src/simon/services/drive.py), [native tools](../../src/simon/adapters/native_tools.py)             | Current-account Drive/mail/calendar reads and account workspace file tools. Native board credentials do not authorize these personal-account tools.             |
| [ClickUp](../../src/simon/adapters/clickup.py)                                                                          | Bounded provider calls and independently scoped credentials. Native boards remain the application task authority; no mirror or bidirectional board sync exists. |

The [integration delivery plan](../architecture/integration-delivery-plan.md) defines
the work needed to turn these libraries into supported project workflows. Add one
tool after core acceptance, with an explicit account binding, cost ceiling, revision
checks, safe retry semantics, observable receipts and user review where required.

## Files and environments

[EnvironmentManager](../../src/simon/services/execution.py) keeps durable leases and
capacity checks; [Docker and machine backends](../../src/simon/adapters/execution_backends.py)
enforce environment ownership. The machine protocol requires an independently operated
trusted host. Native staffing does not provision or register that host.

[ArtifactStore](../../src/simon/services/artifacts.py) preserves immutable bytes and
checks provenance/hashes. [Dependency handoff](../../src/simon/services/artifact_handoff.py)
checks selected successful task outputs before staging bounded copies. These primitives
require an explicit controller-owned storage directory and authorization callbacks.
They have no application-wide manifest or autonomous dispatcher entrypoint.

[Workspace import](../../src/simon/adapters/workspace_files.py) accepts an account
workspace file and writes a new controlled filename inside an active Docker lease.
It cannot import a project root or fetch another run through a retired project API.
Generic account file API behavior is documented in [local files](local-files.md).

## Verification

Run unit/contract coverage before configuring real providers. Tool and model test
fixtures are synthetic and do not establish host application, container or paid
provider acceptance. Docker examples require their named image; licensed application
bridges require the application and current OS permission model. State omitted checks
explicitly when describing support. Never turn a skipped integration into a capability claim.
