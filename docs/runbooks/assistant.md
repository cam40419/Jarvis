# Chat and background conversations

Open `/login`, sign in, and use **Chat** for conversations. Use **Projects** for project
intake, native boards and scoped teams; see [native projects](native-projects.md).
The [development guide](../development.md) documents installation and verification.

Chat supports saved conversation history, response profiles, model streaming, stop,
account-scoped memory and the tools actually enabled for the current account. A local
test provider returns deterministic responses without making model calls. Configure
`SIMON_MODEL_PROVIDER=openai` and `SIMON_OPENAI_API_KEY` for the current OpenAI chat path.
Only `SIMON_` settings are accepted; keys belong in server configuration or supported
encrypted connection enrollment, never in prompts or a login-token field.

Quick, Balanced, Deep and Auto use the configured chat profiles. Inspect the saved run
for actual model and routing decisions. These chat controls are separate from the
planned project-wide provider selection, budgets and local-model routing policy.

Background conversations persist through `/v1/work-sessions`. A submitted message
returns a queued receipt; the separate `simon.assistant_worker` consumes it against
PostgreSQL with a configured model. Reloading the browser reconnects to saved progress.
Stopping a conversation prevents new authorized work. Uncertain interrupted actions
are not replayed automatically. Work sessions are chat jobs, not a second project board.

```powershell
.\venv\Scripts\python.exe -m simon.assistant_worker --check
.\venv\Scripts\python.exe -m simon.assistant_worker
```

`--check` validates configuration without opening the database or calling a provider.
The worker honors its explicit stop file and drains current work. A development
memory store cannot provide durable background execution across process restarts.

[Google connections](google.md), [local files](local-files.md), and optional external
actions have their own permission and review boundaries. Chat does not assemble a
project team, create native board tasks through tools, or launch project workers yet.
