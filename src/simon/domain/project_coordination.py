"""Small model-authored plans, validated before they acquire execution grants."""

from typing import Literal

from pydantic import Field, model_validator

from simon.domain.models import StrictModel


class DelegatedTask(StrictModel):
    id: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,39}$")
    title: str = Field(min_length=1, max_length=160)
    agent_id: str = Field(min_length=1, max_length=63)
    objective: str = Field(min_length=1, max_length=6000)
    depends_on: tuple[str, ...] = Field(default=(), max_length=8)
    tool_ids: tuple[str, ...] = Field(default=(), max_length=30)
    todo_id: str | None = Field(default=None, max_length=100)


class LeadDecision(StrictModel):
    status: Literal["plan", "waiting", "complete"]
    summary: str = Field(min_length=1, max_length=4000)
    tasks: tuple[DelegatedTask, ...] = Field(default=(), max_length=8)

    @model_validator(mode="after")
    def valid_graph(self) -> "LeadDecision":
        if bool(self.tasks) != (self.status == "plan"):
            raise ValueError("Only a plan has tasks, and a plan must contain tasks")
        graph = {task.id: set(task.depends_on) for task in self.tasks}
        if len(graph) != len(self.tasks) or "lead-summary" in graph:
            raise ValueError("Task IDs must be unique; lead-summary is reserved")
        if any(not dependencies <= graph.keys() for dependencies in graph.values()):
            raise ValueError("Unknown task dependency")
        while graph:
            ready = {key for key, dependencies in graph.items() if not dependencies}
            if not ready:
                raise ValueError("Task dependencies must be acyclic")
            graph = {
                key: dependencies - ready for key, dependencies in graph.items() if key not in ready
            }
        return self
