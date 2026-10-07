# Hosting capacity and subscription plan

Prepared October 7, 2026. Status: researched planning proposal; no resources purchased or deployed.

The owner has selected local development first, eventual hosted public SaaS, and optional local workers. This document translates those decisions into deployment options and a capacity contract for the [platform plan](autonomous-work-platform-plan.md). Hardware, monthly spending limits, launch geography, and service availability targets still need owner input.

**Start with one locally operated installation and design its tenant boundaries immediately.** Move to a small hosted private alpha when remote access and availability justify its cost. Select a larger managed deployment from measured workload data. Subscriptions should allocate shared capacity and usage budgets; ordinary customers should not require separate servers.

## Separate the costs before choosing a host

| Cost category              | What consumes it                                                                  | Planning rule                                                                            |
| -------------------------- | --------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------- |
| Control plane              | Web/API, authentication, project boards, scheduling, database, audit records      | Small always-on CPU services; no GPU required.                                           |
| Execution                  | Agent runtime, file processing, browser sessions, code sandboxes, media rendering | Queue and allocate bounded worker slots by workload class.                               |
| Model inference            | Text, embeddings, images, audio, video                                            | Account separately for each provider, model, project, and payer.                         |
| Storage and delivery       | Originals, revisions, previews, logs, backups, download traffic                   | Measure stored bytes and transfer independently; retain selected revisions deliberately. |
| Reliability and operations | Backups, monitoring, orchestration, incident handling, upgrades                   | Include labor and recovery work when comparing unmanaged and managed hosts.              |
| Business tools             | Future commerce, creative, social, and productivity integrations                  | Separate subscriptions, usage charges, and business spending from Simon infrastructure.  |

An agent role is an identity and configuration, not a permanently running machine. Fifty employees can have fifty durable role records while only two or three execution attempts run concurrently. Human review waits and scheduled starts should release compute rather than keep model sessions or browser processes open.

Calling a hosted model API does not require renting a GPU. Open-weight model inference on a local GPU uses the owner's hardware, power, memory, and available time. Renting a GPU is a separate optional choice that needs its own utilization estimate and budget.

All amounts below are illustrative USD before taxes, currency conversion, support upgrades, and labor. Listed rates were checked against the linked official sources on October 7, 2026. Monthly hourly-service arithmetic uses 730 hours; fixed monthly plans use their published monthly price. Promotional credits are excluded. GB and GiB retain the source's units. Recheck prices and regional availability before deployment.

## Option A existing local hardware

Use the current Windows workstation initially, with Linux containers through a supported local container environment where practical. Keep Windows-specific automation on an explicit worker profile. Do not require that every eventual customer install a worker.

Proposed development composition:

- Web/API and a separate worker process, built from the same versioned repository.
- PostgreSQL for tenant, project, board, review, usage, and workflow-facing business records.
- Filesystem artifact storage behind the same contract used by hosted object storage.
- A local durable workflow development service if Temporal is selected, followed by recovery tests against a production-style deployment before any production claim.
- A model endpoint configured through the provider interface: eligible free API, an installed local runtime, or an explicitly enabled paid API.
- Synthetic tool adapters that produce fixtures, delays, failures, duplicate completions, and bounded side effects without business accounts.

Suggested initial resource envelope to measure, not a vendor minimum: reserve 4 CPU cores, 8-12 GB RAM, and 50-100 GB free SSD space for the application, development database, and tests. Browser concurrency and local inference need additional memory. A system with 16 GB total RAM may need very low concurrency while an IDE and other applications are open.

Incremental hosting rental is $0 if suitable hardware already exists. Power is not free: an assumed additional 50 W for 200 hours at an assumed $0.20/kWh costs `0.050 kW x 200 h x $0.20 = $2/month`. At the same draw for 730 hours it costs `$7.30/month`. These are arithmetic examples, not measured consumption or the owner's electricity tariff.

Exclude hardware purchases, disk wear, internet upgrades, backup media, model API charges, and GPU power from that example. Record the actual CPU, RAM, GPU/VRAM, free disk, and acceptable operating hours before choosing local model sizes or concurrency.

One local machine is a development environment with a single failure point. Verify a backup can restore into a clean installation. Keep the workstation private during development; public SaaS exposure is a later deployment gate.

## Option B inexpensive VPS for a private alpha

DigitalOcean provides a straightforward reference for comparison: its Basic bundled plans list 2 vCPU/4 GiB/80 GiB SSD at $24/month and 4 vCPU/8 GiB/160 GiB SSD at $48/month. Bundled plans include transfer allowances and a monthly cap. Select an available US region near the pilot, provisionally NYC, and confirm availability before ordering. These are shared CPU plans. [DigitalOcean Droplet pricing](https://www.digitalocean.com/pricing/droplets)

Use a single container host for the web/API, a bounded worker, and PostgreSQL during a low-volume private alpha. Restrict execution to trusted internal tasks at first. Public customer code and untrusted browser workloads require separately isolated execution capacity; a shared application host is not the sandbox design.

Spaces provides an S3-compatible object-storage option at $5/month including 250 GiB storage and 1 TiB outbound transfer; excess storage is $0.02/GiB-month and excess transfer $0.01/GiB. [DigitalOcean Spaces pricing](https://www.digitalocean.com/pricing/spaces-object-storage)

For predictable arithmetic, use the weekly backup option at 20% of the Droplet price; daily is 30%. A machine snapshot does not replace application-consistent database backup and restore verification. [DigitalOcean backup pricing](https://docs.digitalocean.com/products/backups/details/pricing/)

| Private-alpha illustration            | Monthly arithmetic       | Subtotal |
| ------------------------------------- | ------------------------ | -------- |
| Constrained 4 GiB application host    | `$24 + ($24 x 20%) + $5` | $33.80   |
| More practical 8 GiB application host | `$48 + ($48 x 20%) + $5` | $62.60   |

Both examples assume the stated storage and transfer allowances are sufficient. They exclude managed databases, extra worker hosts, hosted workflow orchestration, API inference, domain registration, email delivery, and paid monitoring. Their PostgreSQL process shares the rented host; neither subtotal buys high availability. A production Temporal cluster is not included or assumed to fit on either machine.

Recommendation: use the 8 GiB option as the initial hosted sizing hypothesis if and when a private alpha is needed. Measure the application first; a 4 GiB host may be adequate for a very small control plane but should not be promised as a combined database/browser/workflow host.

## Option C managed PaaS

Render is a useful managed comparison: a `1c-2g` service or background worker is $25/month, `0.5c-1g` PostgreSQL compute is $19/month, expandable database storage is $0.30/GB, and a Pro workspace is $25/month plus compute. Example deployment region: Virginia. [Render pricing](https://render.com/pricing), [available regions](https://render.com/docs/regions)

| Managed-alpha illustration           | Monthly arithmetic          | Cost    |
| ------------------------------------ | --------------------------- | ------- |
| API and separate worker              | `2 x $25`                   | $50.00  |
| Database compute                     | `1 x $19`                   | $19.00  |
| Database storage allowance           | `20 billable GB x $0.30`    | $6.00   |
| Pro workspace                        | `1 x $25`                   | $25.00  |
| Object storage in the Spaces example | `1 x $5`                    | $5.00   |
| Infrastructure subtotal              | `$50 + $19 + $6 + $25 + $5` | $105.00 |

The storage row budgets 20 billable GB conservatively; verify included storage and allocation increments in the chosen configuration. This is a small non-HA database, one worker, and one API instance, not a service availability commitment. Add workflow service usage, paid model calls, bandwidth/build overages, independent backup retention, and extra instances separately. PaaS and external object storage introduce cross-provider traffic that must be measured.

The benefit is less host patching and simpler deployments. The tradeoff is a higher recurring bill and a need to fit the platform's service and execution constraints. Do not adopt a platform-specific agent runtime merely because it is bundled with hosting. The core contracts must also run locally.

## Option D scalable AWS deployment

The candidate architecture is ECS/Fargate for the API and suitable worker pools, RDS PostgreSQL, S3 artifacts, a load balancer, centralized secrets, and observability. Dedicated EC2 execution pools remain an option when sandbox or local-model requirements exceed Fargate's fit. Kubernetes is unnecessary for the initial release.

For Linux/x86 Fargate in US East (N. Virginia), AWS's published example uses $0.000011244 per vCPU-second and $0.000001235 per GB-second. That is approximately $0.0404784/vCPU-hour and $0.004446/GB-hour; the first 20 GB of task ephemeral storage is included. [AWS Fargate pricing](https://aws.amazon.com/fargate/pricing/)

The following is an explicit **compute and ingress subtotal**, not a complete AWS bill:

| Component and assumption                              | Monthly arithmetic                        | Cost    |
| ----------------------------------------------------- | ----------------------------------------- | ------- |
| Two API tasks, each 1 vCPU/2 GB, running continuously | `2 x 730 x ($0.0404784 + 2 x $0.004446)`  | $72.08  |
| One 2 vCPU/4 GB worker for 100 aggregate task-hours   | `100 x (2 x $0.0404784 + 4 x $0.004446)`  | $9.87   |
| One application load balancer                         | `730 x $0.0225`                           | $16.43  |
| Assumed average one LCU                               | `730 x $0.008`                            | $5.84   |
| Assumed two load-balancer public IPv4 addresses       | `2 x 730 x $0.005`                        | $7.30   |
| Rounded subtotal                                      | `$72.08 + $9.87 + $16.43 + $5.84 + $7.30` | $111.52 |

Load balancer rates are from AWS's US-East example; public IPv4 is charged per address-hour. LCU use and IP counts are workload/configuration assumptions. [AWS load balancer pricing](https://aws.amazon.com/elasticloadbalancing/pricing/), [AWS VPC pricing](https://aws.amazon.com/vpc/pricing/)

The subtotal excludes RDS and its storage/backup/HA costs, S3 requests and storage, worker startup overhead, private-network egress, NAT gateways or endpoints, internet/cross-zone transfer, logs, tracing, secrets, authentication, image registry/builds, durable orchestration, and model usage. A private worker calling external model APIs needs a deliberately costed egress route. Those omissions make the subtotal unsuitable as a quoted production budget.

For an initial planning envelope only, add provisional allowances of $80 for database capacity/storage, $90 for private egress/network overhead, and $20 for artifacts/logs/secrets: `$111.52 + $80 + $90 + $20 = $301.52/month`, before workflow and model charges. These three allowances are internal placeholders, not verified AWS quotes or a guarantee that HA fits. Replace each with a regional calculator bill of materials after workload and availability targets are known.

AWS becomes attractive when tenancy, recovery, separate execution pools, audit controls, or scale justify the operating complexity. It is not the recommended development starting point. Reserved commitments and Spot capacity should wait for measurements; use interruptible capacity only for jobs whose retry behavior is tested.

## Durable orchestration is a real line item

Temporal is the proposed candidate, subject to the architecture decision. Its documentation distinguishes a local development server from production deployment and requires application workers on your infrastructure even when using Temporal Cloud. [Temporal production deployment](https://docs.temporal.io/production-deployment)

The current Cloud page advertises no base monthly fee, $50 per million actions, active history at $0.042/GB-hour, retained history at $0.00105/GB-hour, and developer support at 10% of usage. This replaces older assumptions about a flat entry fee; validate the selected plan before purchase. [Temporal pricing](https://temporal.io/pricing)

Small-workload illustration, using assumed consumption rather than a Simon benchmark:

- 100,000 actions: `0.1 x $50 = $5.00`.
- Average 0.01 GB active history for 730 hours: `0.01 x 730 x $0.042 = $0.3066`.
- Average 1 GB retained history for 730 hours: `1 x 730 x $0.00105 = $0.7665`.
- Usage plus assumed 10% developer support: `($5 + $0.3066 + $0.7665) x 1.10 = $6.68`, rounded.

Thus the 8 GiB VPS example plus this Cloud workload is `$62.60 + $6.68 = $69.28/month`; the managed PaaS example is `$105 + $6.68 = $111.68/month`, both before inference and the stated exclusions. Workflow workers still consume their allocated host capacity. A task is not one billable action; retries, timers, signals, and activity calls affect action counts.

Self-hosting removes that vendor usage bill but adds service/database resources, upgrades, visibility retention, backups, monitoring, and recovery responsibility. Benchmark it before sizing a host. Keep large artifacts and full model traces out of workflow history; store authorized artifact references. The example excludes HA, provisioned-capacity commitments, and optional add-ons. Temporal's own namespace Fairness feature adds 0.1 billable action per action while enabled; distinguish it from Simon's tenant admission scheduler. [Temporal billing details](https://docs.temporal.io/cloud/pricing)

## Model costs and the free starting experience

The owner's requirement is a free-tier default with optional project API keys and limits. Implement a configurable free provider profile, with availability, eligibility, privacy terms, rate limits, and capabilities checked during setup. No particular free model is selected by this document.

If the free provider is unavailable or its quota is exhausted, offer an eligible local endpoint, a deferred queue, or a configuration action. Do not silently route to a paid model. Free promotional/API capacity is not a production throughput or quality guarantee, and open-weight models are not free infrastructure.

Store paid provider keys in an encrypted server-side secret store, scoped to the owning tenant and project grant. Agents receive a secret reference or controlled provider access; prompts, logs, browser clients, and review packages must not expose the key. Provider adapters should preserve usage, model identity, pricing version, and the actual payer.

Maintain separate ledgers for platform-billed usage and bring-your-own-key usage. With BYOK, the provider bills the customer's account; Simon should still reserve and track estimated project spend. Simon can stop its own new requests but cannot prevent other applications from spending on the same provider account.

Cost estimation must include planning, execution, reviews, revisions, embeddings, retries, and multimedia. Illustrative fictional model rates of $1/million input tokens and $4/million output tokens would make 5 million input plus 1 million output tokens cost `$5 + $4 = $9`. At three times that total token usage, cost is `$27`; those are arithmetic assumptions, not a provider recommendation or quote.

Reserve the estimated maximum cost before starting each bounded call; reconcile measured usage afterward. Unknown-price models require an administrator-entered estimate or a zero-paid-use block. Include in-flight reservations in project, tenant, and platform limits. Notify at configurable thresholds and stop new paid work at the hard limit; completion, cancellation, refunds, and delayed provider usage must reconcile without double charging.

Budget limits are controls on authorized calls, not a guarantee that an external bill cannot differ. Bound maximum output tokens, iterations, parallelism, timeouts, and retries, and leave a configurable reserve for outstanding requests. Preserve work state when a limit is reached.

## Optional local workers and shared hosted capacity

Use one worker contract in local-only, hosted-only, and hybrid modes. An enrolled worker advertises platform, tool capabilities, CPU/RAM/GPU capacity, trust class, tenant scope, and scheduling windows. The server issues short-lived job grants and bounded leases; the worker initiates outbound authenticated connections so no inbound home-network port is required.

Minimum lifecycle:

1. An authorized owner creates a short-lived enrollment request for one tenant and named device.
2. The worker registers its key and declared capabilities; the owner can inspect and revoke it.
3. Each claim checks tenant, project policy, tool permission, locality requirement, and resource availability.
4. The worker executes in a bounded workspace, reports progress/heartbeats, and uploads authorized artifacts and usage.
5. Expiry, interruption, replacement, and late results follow durable lease and idempotency rules; reconnecting does not duplicate an external action.
6. Revocation removes future access and cancels/reconciles in-flight work. Local cleanup and credential expiry are explicit lifecycle operations.

A personal device handles only its authorized tenant's jobs by default. It is not spare public compute. Hosted workers must not inherit platform administrator credentials. Separate hostile code/browser jobs from trusted application processing and from other tenants; a container alone is not a complete hostile-workload boundary.

Support project policies for `hosted allowed`, `local preferred`, and `local required`. If a required local worker is offline, retain the task with a visible reason and next retry. Cloud fallback requires the project's existing permission and budget; it must not bypass data-locality settings.

Keep core hosted operations functional without any local worker. Moving a project between eligible worker pools changes placement, not its task identities, reviews, model configuration, or artifact history.

## Subscription and dynamic allocation design

Define entitlement records now and delay final retail pricing until workload measurements exist. A plan grants limits; a separate metering record describes actual use. Platform administration can configure grants and exceptions, but every override remains auditable and the infrastructure still has finite capacity.

| Entitlement                 | Allocation behavior                                                                                     |
| --------------------------- | ------------------------------------------------------------------------------------------------------- |
| Active execution slots      | Concurrent work across the tenant, further constrained per project and worker class.                    |
| CPU time and memory class   | Measured bounded jobs; larger jobs require a suitable pool and sufficient allowance.                    |
| GPU time                    | Explicit opt-in allowance, separate from CPU and external model API usage.                              |
| Storage                     | Meter originals, revisions, previews, and retained deleted files; define export and retention behavior. |
| Transfer                    | Meter downloads and uploads where the platform incurs transfer charges.                                 |
| Included model credits      | Monetary/provider-rated allowance with explicit overage policy; independent of BYOK.                    |
| Local workers               | Enrollment and concurrency limits if product pricing requires them; always optional.                    |
| Priority                    | Weighted queue service with fairness and aging; avoid starvation of lower plans.                        |
| Collaboration and retention | Seats, history retention, audit/export features, and later enterprise requirements.                     |

Use tenant-aware queues and a scheduler that checks entitlements, reservations, global headroom, and capable-worker availability before dispatch. Autoscale from queue age and resource pressure, with cooldowns and hard fleet ceilings. Adding an agent identity does not automatically buy another server or enlarge a budget.

Prefer prepaid included usage or explicitly enabled bounded overage for the first paid release. Separate platform operations spending from customer ad budgets, inventory orders, subscriptions, and other future business-tool actions. Users should see an understandable estimate before unusually expensive work and the actual cost afterward.

Paid plans must not promise unlimited autonomous computation. Estimate margin from infrastructure, provider costs, storage/transfer, support, payment processing, and failure/revision overhead. A local worker can reduce hosted execution usage but does not eliminate coordination, storage, support, or model API costs.

## Capacity evidence required before hosting selection

Use the same small workload suite for local and candidate hosted environments:

- Interactive board/project operations during background jobs; record p50/p95 latency and memory pressure.
- A complete synthetic project with intake, staffing, claiming, model calls, artifact creation, review, revision, and completion.
- A human review wait lasting across process restarts without occupying execution capacity.
- Queue fairness for multiple tenants, including one tenant exhausting its quota.
- Worker loss, duplicate results, retry exhaustion, and budget exhaustion without duplicate external effects.
- Artifact upload/download, preview retention, backup restore, and tenant isolation failures.
- Optional local-worker disconnect and revocation, with the hosted-only path still succeeding.

Track cost per completed accepted deliverable, revision rate, successful completion rate, queue delay, and operator time. Tokens per call alone do not reveal whether inexpensive models are producing economical accepted work.

Before public release, establish availability and recovery objectives, validate database and object-storage restoration, verify access separation, and test the actual production-style workflow deployment. The locally tested core is the first gate; live business integrations then enter one at a time through the [integration delivery plan](integration-delivery-plan.md).

## Inputs still needed

1. Development hardware: CPU, total RAM, GPU/VRAM, available SSD space, and whether the machine can stay on.
2. Separate monthly caps for platform hosting, paid model experimentation, and later image/video generation.
3. First hosted audience geography and whether any project data must remain on local hardware.
4. Expected initial users, concurrent active jobs, artifact volume, and acceptable wait time.
5. Private-alpha and public-release availability/recovery expectations.

These inputs refine sizing and budget decisions. They do not block the tenant model, worker protocol, entitlement schema, usage ledger, free-default behavior, or synthetic core acceptance plan.
