# Enterprise agents, automation and durable infrastructure

Research observed on **October 9, 2026**. This is a product and architecture comparison for Simon, not a claim that public documentation proves production quality. Product pages, technical documentation, pricing pages, release material and relevant source licenses were inspected. No vendor account, trial, procurement quote or production workload was exercised.

Coverage: **17 deep profiles**, **56 unique primary comparison URLs** in this note, and **18 structured map entries with 54 unique source URLs** in [enterprise-and-infrastructure.json](enterprise-and-infrastructure.json). The structured map separates Google's employee workspace from its developer agent platform; the prose discusses them together. Capability states preserve documentation scope and uncertainty rather than treating missing evidence as missing functionality.

## Decision

Simon should compete on **turning a changing business objective into accepted, inspectable work with a small amount of owner supervision**. Its existing board, exact artifact revisions, independent reviewer role, linked repairs and durable accounting form a useful starting point. None establishes a market advantage by itself. Enterprise vendors and developer platforms already document multi-agent orchestration, persistent memory, human approval, policy enforcement, recovery, model choice and cost controls. [Google platform overview](https://docs.cloud.google.com/gemini-enterprise-agent-platform/agents), [UiPath Maestro](https://www.uipath.com/product/maestro), [Microsoft Agent Framework](https://learn.microsoft.com/en-us/agent-framework/overview/), [LangGraph persistence](https://docs.langchain.com/oss/python/langgraph/persistence).

The most promising hypothesis is a joined experience: the owner can see **what is accepted, why it is accepted, what changed, what needs attention, what remains authorized and what the entire outcome cost**, across human work and agents from different providers. This should be demonstrated on a real, changing project against the best configured alternatives. It should not be advertised as a feature no other system can implement.

Current Simon evidence comes from the [artifact runbook](../../runbooks/native-artifacts.md), [execution runbook](../../runbooks/native-execution.md), [model controls](../../runbooks/project-models.md) and [roadmap](../../next-phases.md). Current boundaries matter: one logical artifact per task, 1 MiB files, bounded UTF-8 automatic review, a PostgreSQL controller, no accepted production business-tool suite, no object-store artifact backend and no complete SaaS release. Those are substantial gaps against mature platforms.

## Reading the evidence

- **Documented** means an inspected official source describes the capability. It does not mean we tested its failure semantics.
- **Framework primitive** means a developer can compose the behavior; this is different from a finished owner-facing work product.
- **Vendor assertion** means a marketing or announcement source makes the claim, without an independently verified benchmark here.
- **Unresolved** means the sources inspected do not settle the question. It must not be converted to “absent.”
- **Hypothesis** means a proposed Simon strategy, with a test that can disprove it.

Prices below are observations, not quotes or normalized total-cost comparisons. Currency, billing term, base licenses, model inference, storage, connector usage and deployment charges are different. Search snippets were treated as discovery aids; live pages took precedence where they differed. Google, Zapier and Flowise illustrate why this distinction matters.

## Market map

| Layer                                           | Products                                                                                                    | Job the buyer is purchasing                                                                 | Simon relationship                                                                     |
| ----------------------------------------------- | ----------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------- |
| Enterprise work and system-of-record ecosystems | Microsoft 365/Copilot Studio, Salesforce Agentforce, ServiceNow, Gemini Enterprise, IBM watsonx Orchestrate | Work inside existing permissions, data, operational processes and procurement relationships | Strong distribution competitors; integrate where customers already live                |
| Automation and integration platforms            | UiPath, Workato, Zapier/AI by Zapier, n8n, Make                                                             | Cross-system actions, triggers, deterministic workflows, operational visibility             | Prefer reuse of accepted connectors/actions over reproducing the whole catalog         |
| Agent construction, evaluation and deployment   | LangGraph/LangSmith, CrewAI, Dify, Microsoft Agent Framework; historical Flowise                            | Build and operate customizable agent applications                                           | Alternatives for technical buyers and possible execution/evaluation components         |
| Infrastructure for durable work                 | Temporal, Azure Durable Extension, AWS AgentCore, Google Agent Runtime                                      | Hosting, persistent execution, identity, tool policy, observability                         | Infrastructure candidates; not complete replacements for Simon's work/acceptance model |

UiPath spans the automation and durable-orchestration layers. Workato spans integration, agents and owner-facing action dashboards. LangSmith now also offers natural-language agent creation through Fleet and a model gateway, so it should not be reduced to a tracing tool. [UiPath](https://www.uipath.com/product/maestro), [Workato Action Board](https://www.workato.com/the-connector/action-board-go/), [LangSmith pricing and product table](https://www.langchain.com/pricing).

| Product                          | Strongest inspected evidence                                                                     | Human involvement                                                               | What remains unresolved for Simon's exact comparison                                           |
| -------------------------------- | ------------------------------------------------------------------------------------------------ | ------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------- |
| Copilot Studio/M365              | Multiple harnesses, agent flows, external publishing and tenant management                       | Co-authoring; computer-use supervision; separate process approvals can be built | Exact revision/hash acceptance and policy-change invalidation in a deliverable lifecycle       |
| Agentforce                       | Agent Script combines deterministic paths with model reasoning; native CRM actions; governed MCP | Salesforce processes and scripted gates                                         | Restart and ambiguous external-action semantics for arbitrary project deliverables             |
| ServiceNow                       | AI inventory, governance, lifecycle control, agent orchestration and supervised tools            | Human tool authorization and workflow approvals                                 | Small-team setup burden; specific artifact/repair and rollback semantics                       |
| Gemini Enterprise/Agent Platform | Employee experience plus runtime, identity, registry, memory, gateway and evaluation             | Permission-aware actions; governance controls                                   | Current availability of marketed universal-agent functions; exact artifact acceptance          |
| UiPath Maestro                   | Live process control and state across hours/days/weeks, coordinating robots/agents/people        | Durable human steps and cases                                                   | Actual limits and license configuration; artifact acceptance invariant under changes           |
| Workato                          | Managed agents, recipes, MCP, individual user credentials, KPI/approval dashboards               | User confirmation and business approvals                                        | Headless approval limitations; equivalence of a task dashboard and Simon's work graph          |
| AI by Zapier                     | Agentic and deterministic steps in the Zap editor, structured output, task controls              | Per-tool approval and publishing review                                         | Migration completeness; external-action ambiguity and multi-day project work                   |
| n8n                              | AI nodes, MCP, shared workflow projects, execution metering                                      | Explicit tool approval intercepts                                               | Exact deliverable acceptance; source-license fit for a customer-configurable SaaS              |
| Make                             | Visual scenarios, agent tools/knowledge, bounded steps and history compaction                    | Can compose review processes with scenarios                                     | Native enforced approval behavior for each chosen tool; restart semantics of the new agent app |
| LangGraph/LangSmith              | Persistent checkpointers, interrupts, state editing/time travel and managed deployment           | Typed approval/edit interrupts and annotation queues                            | Finished shared business-project lifecycle without custom application work                     |
| CrewAI                           | Crews/Flows, persistent state, asynchronous feedback and governed enterprise deployment          | Review routing; feedback learning; workflow chat                                | Exact-bound acceptance and spend-liability behavior across retries/provider uncertainty        |
| Dify                             | Visual workflows, model/plugin ecosystem and Human Input review/edit/regenerate/escalate         | Native human forms and timeout routes                                           | Native immutable deliverable lifecycle; commercial source-use terms                            |
| Flowise                          | AgentFlow V2 documents restart-safe checkpoints and human input                                  | Input/approval nodes                                                            | Official project is past announced EOL; maintenance responsibility shifts to forks             |
| Microsoft Agent Framework        | Agents, harness, graph workflows, MCP, broad providers, durable extension                        | Workflow approval requests and durable waits                                    | Product-layer work ownership, artifact acceptance and budget settlement remain app concerns    |
| Temporal                         | Durable workflow history, retries/timers/signals, worker versioning, AI integrations             | Durable externally signaled approval                                            | External effects still need application idempotency/authority/acceptance rules                 |
| AWS AgentCore                    | Framework/model-independent hosting, identity, gateway policies, evaluations                     | Policy can require prior approval within a session                              | Finished human project interface; recovery/acceptance across runtime and external services     |
| IBM watsonx Orchestrate          | No/pro-code agents, imported external agents, MCP/API gateway, multicloud/on-prem                | Human workflows depend on configured application                                | Exact revision/repair and ambiguous spend behavior; enterprise implementation burden           |

Evidence for these rows is detailed below. “Shared projects” in an automation platform often means a container of editable workflows, rather than a shared project board with human-assigned work and accepted deliverables. Conversely, a developer can build that board with framework primitives; the relevant comparison is time, correctness and operator burden, not theoretical implementability.

## Inspected products and their implications

### Microsoft Copilot Studio and Microsoft 365

The live pricing page includes Copilot Chat, Standard and GitHub Copilot harnesses; internal M365 agents and separately licensed external channels; co-authoring and agent/flow version rollback. M365 Copilot is listed at $30/user/month, paid yearly, with a qualifying base subscription required. Standalone capacity packs are $200/month for 25,000 Copilot Credits. This installed productivity surface is a distribution advantage Simon cannot match simply by adding another agent builder. [Plans and pricing](https://www.microsoft.com/en-us/copilot/pricing/copilot-studio).

The current billing documentation is unusually specific: generative answers consume two credits, agent actions five, and tenant graph grounding ten; reasoning can add a token meter. Included M365 usage depends on authenticated identity and trigger type. Prepaid custom-agent enforcement occurs at 125% capacity, while new agent-flow runs block at exhaustion; in-flight behavior differs. Individual monthly agent caps are also documented. Simon should explain its hard reservations and uncertain liabilities in terms ordinary owners understand, and test those controls against comparable configured limits. [Billing and enforcement, updated October 1](https://learn.microsoft.com/en-us/microsoft-copilot-studio/requirements-messages-management).

Computer-use supervision is explicitly described as model-triggered and probabilistic: it is not a guaranteed pause before every consequential action. That narrow fact must not be generalized to all Copilot Studio approval mechanisms. A useful Simon experiment is comparing fixed policy gates with escalation chosen by the model. [Computer-use supervision](https://learn.microsoft.com/en-us/microsoft-copilot-studio/human-supervision-computer-use).

### Salesforce Agentforce

Agent Script is an application behavior definition in an authoring bundle, combining model flexibility with scripted actions and branching. Salesforce documents mandatory authentication paths and forced subagent transitions as design patterns. Deterministic orchestration is therefore already a product feature. [Developer authoring documentation](https://developer.salesforce.com/docs/ai/agentforce/guide/agent-dx-nga-author-agent.html), [determinism explanation](https://www.salesforce.com/agentforce/levels-of-determinism/).

MCP can be governed through Agentforce Gateway policies scoped to agents/tools, and Salesforce also exposes native agents as tools to external clients. The implication is an integration path, rather than recreating CRM authority and native transactions inside Simon. [MCP for Agentforce](https://help.salesforce.com/s/articleView?id=ai.agent_mcp.htm&language=en_US&type=5), [hosted Agentforce MCP](https://developer.salesforce.com/docs/platform/hosted-mcp-servers/guide/agentforce.html).

Observed pricing includes $500/100,000 Flex Credits and 20 credits per standard action, equivalent to $0.10 per such action at that list unit. Alternatives include $2/conversation, $5/user/month plus credits, and $125/user/month unmetered employee add-ons. The live page now says Agentforce Max Editions from $550/user/month with 2.75 million credits/org/year; older search results describing Agentforce 1/2.5 million were not used as current truth. These units exclude other Salesforce/data consumption. [Current pricing](https://www.salesforce.com/agentforce/pricing/).

### ServiceNow

AI Control Tower covers AI asset inventory, oversight, lifecycle/governance and external AI ecosystems. The May 2026 announcement describes cross-system discovery and containment/kill-switch capabilities; treat scale and effectiveness as vendor assertions until tested. This is a powerful challenge to a generic “agent control tower” positioning. Simon should pursue the owner-facing work experience and an affordable entry path, while remaining able to export evidence to an enterprise governance system. [Product](https://www.servicenow.com/products/ai-control-tower.html), [2026 expansion announcement](https://newsroom.servicenow.com/press-releases/details/2026/ServiceNow-expands-AI-Control-Tower-to-discover-observe-govern-secure-and-measure-AI-deployed-across-any-system-in-the-enterprise/default.aspx).

The inspected access-control documentation distinguishes invoking and run-as identities, checks ACLs through the agent hierarchy and offers supervised tool execution. It also distinguishes dynamic users from AI users and role masking. These are substantive authority controls, not just approval buttons. Simon must compare its own scoped roles/current-owner checks to an actual configured deployment rather than assuming incumbents ignore identity. [Access-control documentation](https://www.servicenow.com/docs/r/intelligent-experiences/aia-security-implementation.html).

The inspected product page requests a pricing contact; no supported comparable public per-project total was established. Procurement terms and the underlying ServiceNow footprint would need a quote.

### Google: Gemini Enterprise, current business branding and Agent Platform

The platform documentation, updated October 7, describes Studio, Managed Agents API and ADK; Agent Runtime, sessions/Memory Bank, Agent Identity, Registry/Gateway, evaluation and observability. The gateway is a runtime policy enforcement point, and registered metadata includes versions and tool capabilities. These documented components weaken claims that a unified identity/tool/evaluation stack is an unoccupied market. [Agent Platform overview](https://docs.cloud.google.com/gemini-enterprise-agent-platform/agents), [ADK](https://docs.cloud.google.com/gemini-enterprise-agent-platform/build/adk).

Live employee-product branding is in transition: the public Gemini Enterprise URL currently says “Gemini for Business” and markets a universal work agent, project budget caps and cross-session memory. Its FAQ explicitly says background multi-step tasks, mobile/desktop and third-party model choice are early access or not all currently available; it says Gemini models are available today. Platform Model Garden breadth is not proof the employee app can currently choose any model. [Live product page and availability footnote](https://cloud.google.com/gemini-enterprise).

Current edition documentation still uses Gemini Enterprise Business, Standard, Plus, Pay-as-you-go and Frontline. It lists no-code agent tools as preview and restricts full-code external agents to some editions. A current numeric employee-app price was not confirmed on the inspected live page; cached $21/$30 snippets are insufficient. Infrastructure pricing is separate: the current public Gemini platform FAQ describes model usage and agent compute, memory and storage meters. [Editions, updated October 7](https://docs.cloud.google.com/gemini/enterprise/docs/editions), [Gemini platform pricing FAQ](https://cloud.google.com/products/gemini-enterprise-agent-platform).

### UiPath Maestro and the automation estate

Maestro explicitly coordinates agents, robots and people using BPMN, Case or Flow models on a common runtime. The live product page describes process state lasting hours, days or weeks; human waits; and retry, cancellation, pause, resume, migration and runtime control. Long-lived work is not a Simon novelty. [Maestro](https://www.uipath.com/product/maestro).

UiPath's strength is the ability to execute through existing enterprise systems, including robot-mediated tasks. Its pricing matrix distinguishes Community/Basic/Standard/Enterprise capabilities and agent-call entitlements, but no reliable dollar total for a comparable project was exposed in the inspected page. “No limit” listed in an entitlement is not proof of free inference or unlimited capacity. Request a workload-specific quote before calculating total cost. [Plans](https://www.uipath.com/pricing).

Simon should integrate proven robot/process actions where a customer owns UiPath, and build its own lighter workflow only where the setup and operator overhead are materially smaller.

### Workato: Agent Studio, GO and MCP

Agent Studio manages “genies”; Workato GO supplies chat/search, while MCP exposes the same integration skills to outside agent clients. Verified User Access executes eligible skills with end-user OAuth identity instead of a builder's shared connection. The documented stack supports owner-like roles, shared knowledge and external clients, making it a closer competitor than a recipe runner alone. [Agentic FAQ](https://docs.workato.com/en/agentic/faqs).

Action Board adds custom KPI tiles, open events/approvals and conversational genie widgets. The implication is that dashboards plus agent roles plus actions are already sold as one experience. Simon needs acceptance/dependency semantics and lower supervision demonstrated on real deliverables. [Action Board](https://www.workato.com/the-connector/action-board-go/).

User confirmation shows prepared parameters and waits before calling a skill. Documentation states these confirmations are unavailable in headless “Assign task to genie”; business approvals also document restrictions with that action. Such edges are valuable comparative tests, not evidence the entire product lacks approval. [User confirmation](https://docs.workato.com/en/agentic/skills/user-confirmation.html), [business approvals](https://docs.workato.com/en/agentic/agent-studio/business-approvals.html).

Live self-service pricing lists Free at 50,000 one-time credits and Pro at $100/month for 3,500 monthly credits. Enterprise orchestration/agentic editions use contractual usage capacity; availability and capabilities depend on plan. [Self-service plans](https://www.workato.com/free), [billing documentation](https://docs.workato.com/pricing/).

### Zapier Agents migrating to AI by Zapier

October 1 documentation states Agents is moving into AI by Zapier within the Zap editor. Agentic reasoning can be interleaved with deterministic steps, structured outputs and per-tool approval. The migration currently preserves an old agent as one AI step unless restructured. There is no announced shutdown date for Agents in this inspected source. [Migration](https://help.zapier.com/hc/en-us/articles/47402591569805-Migrating-from-Agents-to-AI-by-Zapier).

The new experience uses ordinary Zapier task billing, with per-run task limits and publishing/model administration controls. Model tiers apply multipliers: Standard 1x, Advanced 3x, Premium 5x and BYOK 1x; tool calls also affect task usage. An agent advertised as one step may consume many billable tasks. Existing Agents activity allowances, including Free 400 and Pro 1,500/month, should be labeled legacy transition data rather than assumed to price new AI by Zapier. [Model-tier pricing](https://help.zapier.com/hc/en-us/articles/46425475442829-AI-by-Zapier-model-tier-pricing), [legacy activity metering](https://help.zapier.com/hc/en-us/articles/26559132765325-How-is-Zapier-Agents-usage-measured).

Integration breadth and existing automation habits are distribution strengths. Simon should call an accepted Zap/action through a narrow contract and retain its own project acceptance/authorization evidence, rather than copying the connector catalog.

### n8n

Current pricing observes €20/month billed annually for 2,500 workflow executions on Starter, €50 for 10,000 on Pro, and €667 for 40,000 on self-hosted Business. An execution is the workflow run rather than each step. AI/model/builder allowances and enterprise controls are separate considerations; shared “projects” organize workflows. [Pricing](https://n8n.io/pricing/).

Human review intercepts a tool call, presents its parameters through a configured channel and resumes only following approval. This is stronger than telling a model to ask nicely. The useful Simon contrast is immutable accepted deliverables and live changes to the authorization basis, which require direct testing. [Tool approval documentation](https://docs.n8n.io/build/integrate-ai/ai-examples/human-in-the-loop-for-tools.md), [official implementation guidance](https://github.com/n8n-io/skills/blob/main/skills/n8n-agents-official/references/HUMAN_REVIEW.md).

The October 6 license help clarifies that internal authors may operate workflows for clients, including connected client accounts, if clients cannot edit/configure the logic. External users configuring workflows via another UI, API, MCP or agents require commercial licensing. This source is more current than simplistic “client keys always prohibited” explanations. Simon SaaS must settle its actual usage contract before embedding n8n; connecting to customer-owned licensed instances is a different product design. [Current official license FAQ](https://support.n8n.io/article/can-i-use-your-license-for-my-use-case).

### Make

The live pricing page shows Free 1,000 monthly credits and, at 10,000 credits/month, Core $12, Pro $21 and Teams $38 with the monthly option displayed. Enterprise is custom. Most module actions consume one credit; AI consumption can add credits. Those are not per-accepted-outcome prices. [Pricing](https://www.make.com/en/pricing).

The **new** agent app, rather than the superseded article, is in open beta. It documents tools/knowledge, conversation IDs, limits on calls/history, context compaction, structured outputs and a step timeout capped at 600 seconds. These settings do not establish arbitrary multi-day durable agent behavior. A larger business process can still be assembled from scenarios. [New agent reference](https://help.make.com/make-ai-agent-new-app).

With Make's provider, credits include AI token usage; custom-provider connections on paid plans use operation credits while model usage follows the provider. Any benchmark must count both bills and tool scenarios. [Agent credit usage](https://help.make.com/credit-usage-for-ai-agents).

### LangGraph and LangSmith

LangGraph exposes persistent checkpointers for thread state and stores for cross-thread data. A production database-backed checkpointer matters: the docs explicitly warn RAM-backed savers do not survive restart. Interrupts permit indefinite waits, typed responses, human approval and review/edit. Resuming can re-run pre-interrupt node code, so application side effects require careful design. [Persistence](https://docs.langchain.com/oss/python/langgraph/persistence), [interrupts](https://docs.langchain.com/oss/python/langgraph/interrupts).

LangSmith is now a broader platform: observability/evaluation, deployment, human annotation, Fleet, sandboxes and a model gateway with cost controls, rate limits, fallbacks, redaction and BYOK. Enterprise includes self-hosted/hybrid choices and custom access controls. Developer lists $0 for one seat; Plus $39/seat/month; usage is additional. Current LCU/LSU units are $1.50/$1.00, with service-specific resource meters. This is a serious build-versus-buy comparison for a technical team. [Current product/pricing table](https://www.langchain.com/pricing).

Simon should avoid a second tracing/evaluation platform unless its domain evidence genuinely requires it. An accepted artifact graph and business-owner workflow can remain Simon-owned while exporting traces/evaluation cases.

### CrewAI

CrewAI Crews/Flows support persistent state, event routing and multi-agent execution. The flow docs describe a persistence decorator, SQLite by default, resume/fork behavior and custom persistence. This can support long-running applications, though state persistence alone does not prove external-action idempotency or spend settlement. [Flows](https://docs.crewai.com/en/concepts/flows).

Current Human Feedback documentation supports nonblocking external providers with automatic persisted pause/resume, review outcome routing and optional learned corrections. It also instructs implementers to make resume handlers idempotent. Thus “learn from owner feedback” and async review are existing capabilities; Simon's proposed difference needs versioned, inspectable, reversible approval of learned rules. [Human Feedback](https://docs.crewai.com/en/learn/human-feedback-in-flows).

The inspected commercial pricing now shows Free with 50 workflow executions/month and Enterprise custom. Enterprise lists SSO/RBAC/workload identity, PII redaction, policies and deployment to CrewAI cloud, a customer VPC or customer infrastructure. Do not carry older professional tiers forward without reconfirming. [Pricing](https://crewai.com/pricing).

### Dify

Dify's Human Input node supports review and editing, regeneration with feedback, forwarding, configurable decision branches and timeout routing, through web forms or email. It was introduced with v1.13.0; the official article is dated March 3, 2026. This is close enough that a generic “draft → review → redo” loop is not a differentiator. [Human Input](https://dify.ai/blog/the-human-input-node-bringing-human-judgment-into-automated-workflows).

The August 18 Asqav marketplace integration describes independently verifiable signed receipts binding agent identity, action payload and authorization, with policy gates and multiparty approval. These are vendor/partner assertions about routed actions, not a proof of every downstream effect or artifact quality. Signed evidence alone is also a crowded opportunity. [Dify/Asqav announcement](https://dify.ai/blog/asqav-joins-dify-marketplace-to-make-every-agent-action-verifiable).

Live annual Cloud pricing shows Professional $590/workspace/year and Team $1,590/workspace/year, with different member/message/storage allowances; Community self-hosting and custom Enterprise are separate. The repository license explicitly restricts multitenant source operation without written authorization. Open repository availability should not be mistaken for an unrestricted Simon SaaS foundation. [Pricing](https://www.dify.ai/pricing), [source license](https://github.com/langgenius/dify/blob/main/LICENSE).

### Flowise: useful reference, lifecycle warning

AgentFlow V2 documentation describes explicit visual orchestration, supervisor/worker collaboration, loops, human input and checkpoints that survive restart. Those patterns remain useful reference designs. [AgentFlow V2](https://docs.flowiseai.com/using-flowise/agentflowv2).

The official sunset announcement schedules a July 29, 2026 code freeze, August 10 archival and August 31 EOL. It says the Apache 2.0 source remains available for forks. On the research date this is a legacy/fork alternative, not a current maintained cloud recommendation. Its old product/pricing pages remain online, illustrating the danger of treating an unchanged documentation page as an actively supported product. [Sunset announcement](https://flowiseai.com/sunset).

### Microsoft Agent Framework, replacing the historical framework comparison

Microsoft identifies Agent Framework as the direct successor to AutoGen and Semantic Kernel, with agents/harnesses, functional/graph workflows, sessions, context providers, MCP and multiple model providers including Ollama. Its current overview includes .NET, Python and Go; Go is marked preview. A migration path is documented, so Simon should evaluate the successor for new work rather than compare only obsolete snapshots of AutoGen. [Overview](https://learn.microsoft.com/en-us/agent-framework/overview/), [1.0 announcement](https://devblogs.microsoft.com/agent-framework/microsoft-agent-framework-version-1-0/).

The Durable Extension distinguishes durable workflows from ordinary workflow checkpoint storage and supports Azure Functions or self-hosted workers, human waits, resumption and distributed execution. The SDK is infrastructure; application-specific authorities, artifact contracts and money semantics still need design. Hosting/inference charges depend on deployment; no universal per-project SDK subscription was established. [Durable Extension](https://learn.microsoft.com/en-us/agent-framework/hosting/azure-functions).

### Temporal

Temporal is the strongest challenge to treating recovery as a proprietary invention: workflow history, retries, durable timers, signals/updates, child workflows and worker versioning are documented product capabilities. AI integrations execute model/tool calls as activities. Listed Cloud pricing starts at $50/million actions, with active/retained storage and support charges; it currently says no base monthly fee. Old $100-base-price comparisons are not appropriate without checking the live schedule. [Pricing/capability table](https://temporal.io/pricing).

The official research-app tutorial includes a durable human approval wait. Durability preserves control-flow decisions, but does not prove an external API call can never happen twice or that approval survives arbitrary changes to an artifact, user authority or policy. Those remain application contracts. Simon can retain its domain model while outsourcing increasingly complex execution to Temporal if an engineering/cost benchmark supports it. [Durable human review tutorial](https://learn.temporal.io/tutorials/ai/building-durable-ai-applications/human-in-the-loop/).

### AWS AgentCore and IBM watsonx Orchestrate: gap-closing additions

AgentCore positions itself as model/framework-independent production infrastructure with runtime, identity, gateway/tool access, observation and evaluation. Cedar policies are evaluated per tool invocation, and the current policy docs also describe session-aware Dogwood conditions such as prior approval and cumulative thresholds. Simon cannot claim uniqueness for enforcing permissions outside the model. [Product](https://aws.amazon.com/bedrock/agentcore/), [policy concepts](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/policy-core-concepts.html).

AgentCore's bill is modular. Observed v1 runtime rates are $0.0895/vCPU-hour and $0.00945/GB-hour; memory, gateway, evaluation, browser/code/search and model inference are separate meters. v2 prices and committed-baseline launch wording differ. Use the chosen region/version and observed billing behavior in a benchmark rather than a single advertised “agent cost.” [Pricing](https://aws.amazon.com/bedrock/agentcore/pricing/).

IBM watsonx Orchestrate documents no/pro-code agents, imported external agents, shared context and a gateway for APIs/MCP, with multicloud and on-prem choices. Live list tiers are Essentials from $530/month, Standard from $6,360/month and Premium custom, with separate throughput/capacity and isolation features. It is a relevant enterprise alternative, not evidence that open/local or cross-framework operation is a Simon-exclusive feature. [Pricing/product capabilities](https://www.ibm.com/products/watsonx-orchestrate/pricing), [external-agent control plane example](https://developer.ibm.com/articles/watsonx-orchestrate-agentic-control-plane/).

## Pricing units to use in an actual comparison

| Product                   | Observed list basis                                                                 | Extra variables that prevent a simple sticker-price ranking                            |
| ------------------------- | ----------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------- |
| Copilot Studio            | $200/25,000-credit monthly pack; M365 Copilot $30/user/month yearly                 | Base M365 eligibility, harness/trigger type, feature/token multipliers, Azure/BYOM     |
| Agentforce                | $500/100,000 credits; 20 credits/standard action; alternate user/conversation plans | Salesforce/Data 360 footprint, voice/action type, contract and unmetered license scope |
| ServiceNow                | Quote required on inspected product page                                            | Existing platform tier, AI entitlement, integration/configuration services             |
| Gemini Enterprise         | Editions/PAYG confirmed; current numeric employee price unresolved                  | Location licenses, indexed storage, preview access, platform/model usage               |
| UiPath                    | Plan/agent-call matrix; dollar project price unresolved                             | Robot/runtime/process tier, agent calls, model, existing enterprise licenses           |
| Workato                   | Pro $100/month/3,500 credits; Free one-time 50,000                                  | Agent/MCP/action rates, contracted edition, concurrency and external systems           |
| AI by Zapier              | Ordinary task subscription plus model/tool task multipliers                         | Agent loop/tool count, model tier/BYOK provider bill, existing plan allowances         |
| n8n                       | €20/2,500; €50/10,000 executions, annual billing                                    | Hosted versus self-hosted, model/gateway costs, concurrency, commercial licensing      |
| Make                      | Core $12, Pro $21, Teams $38 at displayed 10,000-credit monthly selection           | Token charges, module/tool scenarios, custom-provider bill, data/log entitlements      |
| LangSmith                 | Plus $39/seat/month plus usage; LCU $1.50, LSU $1                                   | Deployment compute/DB, traces/retention, gateways, evaluations and model bills         |
| CrewAI                    | Free 50 executions/month; Enterprise custom                                         | Execution allowance, deployment infrastructure, models/connectors/services             |
| Dify                      | $590/$1,590 per workspace/year Cloud Professional/Team                              | Provider keys, message/storage/trigger quotas, enterprise/commercial authorization     |
| Flowise                   | Official EOL; old posted prices unsuitable for procurement                          | Fork maintenance, hosting, security updates and provider usage                         |
| Microsoft Agent Framework | SDK plus chosen host/inference                                                      | Durable scheduler/storage, cloud or self-host operations, provider/model bills         |
| Temporal                  | From $50/million actions, storage/support additional                                | Billable operation count, history size/retention, workers and model/tool usage         |
| AWS AgentCore             | Versioned resource and service meters                                               | Region, CPU/memory wait time, memory/gateway/evaluation/search and inference           |
| IBM watsonx Orchestrate   | Essentials from $530/month; Standard from $6,360                                    | Throughput, capacity ceilings, isolation, deployment choice and contract               |

The values are grounded in the linked product sections above. Re-fetch the actual rate card before a purchase or a pricing promise. A **task**, **action**, **credit**, **execution**, **conversation**, **trace** and **accepted deliverable** are different units.

For Simon's experiments, measure total cost per accepted deliverable as:

`(platform + model + tool + storage + runner + human correction time + failure/recovery time) / accepted deliverables`

Also record elapsed delivery time, p90 cost, supervision minutes and disputed/unresolved charges. A cheap run that needs an hour of corrections is a poor comparison with an accepted output. Reserving money conservatively may improve trust while reducing effective capacity; report that tradeoff instead of hiding it.

## Challenger pass: ideas that the evidence already weakened

| Initial attractive claim                | Counterevidence found                                                                                     | Revised Simon question                                                                     |
| --------------------------------------- | --------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------ |
| “Persistent agents are missing”         | UiPath long-lived state, LangGraph checkpoints, Temporal and Microsoft Durable Extension                  | Is recovery understandable and safe for the owner without an engineer?                     |
| “Humans can finally approve AI work”    | Dify review/edit/regenerate; n8n, Workato and Zapier tool approvals                                       | What exact output/action and current authority does each decision bind?                    |
| “Agents can collaborate as a team”      | ADK, Agent Framework, CrewAI and Flowise supervisor/worker patterns                                       | Does creating more roles measurably reduce work, or only multiply coordination/token cost? |
| “A dashboard makes it manageable”       | Workato Action Board and ServiceNow Control Tower                                                         | Does the owner see acceptance, dependency impact and the next decision at a glance?        |
| “Model-neutral/open/local is unique”    | Agent Framework/Ollama, framework provider choice, enterprise VPC/on-prem choices                         | Can a small buyer keep useful continuity while changing models/hosts with little effort?   |
| “Budget control is an empty market”     | Copilot per-agent caps, LangSmith gateway controls, Google project-cap marketing, AWS temporal thresholds | Can Simon expose and reconcile unknown liabilities rather than only known usage?           |
| “Cryptographic receipts create a moat”  | Dify/Asqav signed action/authorization receipts                                                           | Can receipts connect to real acceptance/outcomes and remain useful outside Simon?          |
| “Low-code graphs are the inevitable UI” | Flowise's own sunset explanation cites coding-agent pressure and complexity limits                        | Can Simon express goals/contracts plainly and use graphs only where they help inspection?  |

These revisions are deliberate. Evidence of a primitive does not prove a competitor has the same joined UX; missing public evidence also does not prove it does not.

## Differentiation hypotheses worth testing

All entries below are proposals, not current Simon capabilities beyond what the runbooks establish. They should be judged against configured alternatives using the same model, tools, input corpus and spending boundary where possible.

| Hypothesis                                                                                   | Concrete Simon experiment                                                                                                                                              | Falsifier / decision rule                                                                                                                      |
| -------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------- |
| An accepted-work graph is more useful than a transcript or task-status board                 | Link each accepted artifact to requirements, source snapshots, validation evidence, owner decisions and dependent deliverables; show impact when an assumption changes | Stop calling it differentiation if n8n/Dify/Workato plus an ordinary document/board matches setup and supervision with equal correctness       |
| Changes should invalidate only the work they actually affect                                 | Version cited sources and requirements; display impacted claims/reviews; propose bounded repairs instead of restarting all work                                        | Reject if impact analysis misses material changes or selective repairs cost more than rebuilding on representative projects                    |
| Owners can delegate a result contract rather than design an agent graph                      | Start with required deliverable, acceptance criteria, evidence sources, budget/deadline and prohibited actions; compile a reviewable plan/role/tool contract           | Reject if owners still need a workflow expert or the generated plan performs worse than a simple fixed recipe                                  |
| Review independence should be configurable by risk, not a second identical prompt everywhere | Compare mechanical checks, fresh independent role, different model, source verification and human review at different tiers                                            | Remove unnecessary review calls when they add expense without detecting held-out defects; never equate a second agent with correctness         |
| Unknown spend/action status can be a product advantage                                       | Show reserved/settled/unknown amounts, current evidence and narrow reconciliation options; hold risky retries until external status is known                           | Reject if conservative holds cause excessive stalled work or owners cannot resolve uncertainty faster than competing run history               |
| The owner should spend attention on decisions, not monitor activity                          | One actionable inbox sorted by cost/critical-path/risk, with evidence and options; batch low-risk decisions without weakening their bindings                           | Reject if batching increases harmful approval errors or does not reduce supervision minutes materially                                         |
| Feedback can become an approved, reversible operating rule                                   | Suggest lessons from corrections; record scope, evidence, version, approver and expiry; replay old tasks before applying                                               | Reject if preferences leak across clients or silently overgeneralize; compare directly with CrewAI learning and Google procedural memory       |
| Open work records can make Simon safer to adopt                                              | Export objective/tasks/artifacts/acceptance evidence in a documented format; import external agents/tools through strict adapters                                      | Reject if export does not reproduce the meaningful state or portability imposes too much implementation cost for buyers                        |
| A narrow vertical package can make general infrastructure valuable immediately               | Run the clothing-brand project with defined briefs, spec sheets, supplier/launch research and review rubrics, while retaining generic contracts                        | Reject that wedge if the same buyer gets equivalent accepted work faster with existing tools and an ordinary shared folder                     |
| An execution rehearsal can prevent expensive mistakes                                        | Before granting a write tool, simulate representative actions against fixtures, show intended side effects and test revocation/stale decisions                         | Reject if rehearsal predicts neither actual failure rates nor owner trust; preserve it only as a tool-acceptance test if product usage is weak |

The strongest bundle appears to be **result contracts + accepted-work graph + focused owner inbox + economically honest recovery**, initially for a clear vertical. This remains an inference. A feature pile spanning every category is likely to lose against incumbents on distribution and against frameworks on flexibility.

## Build, integrate or benchmark before choosing

| Surface                                                             | Proposed decision                                                               | Reason and condition                                                                                                                  |
| ------------------------------------------------------------------- | ------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------- |
| Project/task identity, exact revisions, acceptance and repair graph | Keep Simon-owned                                                                | It is the product's core domain; adapters should map into it without creating two authorities                                         |
| Cross-system integrations                                           | Integrate accepted actions/MCP from customer stacks; build narrow tool adapters | Catalog breadth is expensive and already distributed; each action still needs current authority, receipts and idempotency contracts   |
| Existing native PostgreSQL controller                               | Keep for the accepted small-deployment scope; benchmark replacement             | A migration to Temporal/Durable Extension is justified by real long-running/versioning/operational load, not their mere availability  |
| Tracing, evaluation datasets and generic model gateway              | Reuse/export to mature systems where appropriate                                | LangSmith and cloud platforms already cover these; Simon can add domain evaluation and evidence links                                 |
| Hosting/tool identity/network policy                                | Benchmark AgentCore/Google/Azure versus current intended host                   | Each may reduce infrastructure work but add cost, platform boundaries and data-policy complexity                                      |
| Customer-owned automation platforms                                 | Treat as optional execution/tool providers                                      | Avoid forcing migration of workflows and credentials; retain one project authority and make every external invocation attributable    |
| n8n/Dify source embedding in public SaaS                            | Obtain explicit commercial fit before selecting                                 | Current license text is not an unrestricted multitenant permission; technical feasibility alone is insufficient                       |
| Flowise                                                             | Study design or maintained forks; do not depend on official future support      | The primary team announced EOL; a fork adds maintenance ownership                                                                     |
| Cryptographic evidence/signing                                      | Integrate only when customer value warrants                                     | External verification may matter, but signatures attest data/authorship/authorization, not factual quality or actual business success |

## Comparative failure and usefulness protocol

Run an initial benchmark against **n8n + a shared board/folder**, **Dify**, **Workato**, **LangGraph/LangSmith**, and one ecosystem selected by the target buyer. Temporal or Durable Extension should be included as an infrastructure baseline for recovery, not scored as an entire business application. Begin with supported text artifacts and synthetic external tools; use real actions only after their acceptance contracts are established.

Use the same project objective, model quality class, source set and action limits. Permit the rival to use its best documented approach and ordinary integrations; a deliberately naive alternative is not a fair baseline. Record configuration time and custom code as part of cost, without assuming a mature purchased solution starts from zero.

Faults to inject:

1. Produce a candidate, then alter its bytes before approval/external action.
2. Change the objective, cited source or acceptance rubric after review starts.
3. Reassign/cancel the maker or reviewer task before a delayed result arrives.
4. Revoke the owner, agent role, connector grant or model route during a wait.
5. Crash immediately before dispatch, after provider receipt, and after result receipt before persistence.
6. Lose the external action's response while the external system performs the action.
7. Repeat the same mutation/review/approval callback and distinguish a retry from a new intent.
8. Trigger parallel child work against a shared budget and shared external object.
9. Exhaust a budget while known and unknown charges coexist; receive a later extra charge.
10. Deliver contradictory sources and embedded instructions attempting to bypass the required review.
11. Request changes twice, then verify the accepted head and every dependent task use the correction.
12. Leave the work overnight, change device, and have another authorized human take over.

Separate invariant checks from user outcomes. Invariant measures include wrong-revision acceptance, unauthorized writes, duplicate external effects, lost waits, incorrect money settlement and cross-client access. Product measures include time to first accepted result, acceptance rate, rubric defect rate, supervision minutes, recovery time and cost per accepted deliverable.

A sensible first success criterion is **materially less owner time without more defects or authority failures**, and a bounded total cost comparable to the buyer's current tools. Define exact thresholds before testing; proposed starting thresholds might be 30% less supervision and zero failures on required deterministic invariants. These are experimental choices, not market-validated numbers.

## Remaining uncertainties and research stop condition

This pass covers the assigned enterprise/automation/framework/infrastructure category deeply enough to challenge common differentiation claims. It does not claim all products or all possible strategies have been discovered. New announcements, gated documentation, enterprise contracts and configured applications can change conclusions.

The next evidence to acquire is finite and decision-linked:

- Hands-on lifecycle/fault behavior for the benchmark shortlist, particularly approval binding, revocation, delayed provider results and external idempotency.
- Current Google employee-app branding/prices and exact early-access entitlements, separated from Agent Platform availability.
- Workato's headless approval behavior under the intended deployment and the account's actual Agent Studio/MCP entitlement.
- Salesforce/ServiceNow/UiPath total workload quotes where an existing customer footprint makes them credible alternatives.
- Commercial licensing for any proposed n8n/Dify embedding; an actual architecture and proposed user behavior should accompany the query.
- Model/provider reconciliation capabilities and observed cost distribution, including independent-review false acceptance and unnecessary repair rates.
- Customer interviews establishing whether the buyer's main problem is output quality, operational trust, workflow setup, integration access or management attention.

Continue researching an avenue when new evidence can change a build/integrate/positioning decision. Stop a pass when its material questions have a cited answer, a bounded uncertainty with a test, or an explicit reason it is outside the chosen customer/workflow. This produces a maintained map and falsifiable strategy, rather than an impossible assertion that every future avenue has been exhausted.
