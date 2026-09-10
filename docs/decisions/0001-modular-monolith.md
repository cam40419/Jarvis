# ADR 0001: Modular monolith with isolated Home OS and workers

- Status: accepted
- Date: 2026-09-09

## Decision

Jarvis begins as a modular monolith, while Home OS and machine-bound workers are separately
deployable processes in the same monorepo. Cross-process interactions use versioned contracts.
Home OS owns physical workflows and actuator safety. Jarvis owns interaction, context, tools, and
general jobs.

## Consequences

The system avoids premature operational complexity while preserving the failure, privilege, and
hardware-placement boundaries that matter. Domain packages cannot import API frameworks or device
adapters. Home OS cannot depend on a conversation or model session.
