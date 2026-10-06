"""Small model-authored plans, validated before they acquire execution grants."""

from typing import Annotated, Literal

from pydantic import Field, field_validator, model_validator

from simon.domain.models import StrictModel

LocalTaskID = Annotated[str, Field(pattern=r"^[a-z][a-z0-9_-]{0,39}$")]


class DelegatedTask(StrictModel):
    id: LocalTaskID
    title: str = Field(min_length=1, max_length=160)
    agent_id: str = Field(min_length=1, max_length=63)
    objective: str = Field(min_length=1, max_length=6000)
    depends_on: tuple[LocalTaskID, ...] = Field(
        default=(),
        max_length=8,
        description="Local task.id values from this plan only; never saved todo IDs.",
    )
    tool_ids: tuple[str, ...] = Field(default=(), max_length=30)
    todo_id: str | None = Field(default=None, max_length=100)

    @field_validator("tool_ids")
    @classmethod
    def unique_tools(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        # Repeated permissions add no authority; keep the model's first-use order.
        return tuple(dict.fromkeys(value))


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
