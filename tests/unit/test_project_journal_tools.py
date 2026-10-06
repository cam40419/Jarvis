"""An explicit read grant reuses exact earlier evidence without any provider access."""

from uuid import uuid4

import pytest

from simon.adapters.memory import InMemoryStore
from simon.adapters.project_journal_tools import (
    ProjectJournalToolTransport,
    project_journal_definitions,
)
from simon.domain.errors import AuthorizationError, NotFoundError
from simon.domain.tool_catalog import ToolExecutionContext
from tests.contract.test_project_outputs import create_output, output_setup
from tests.contract.test_run_journal import append, running


def setup(tmp_path):
    h = output_setup(InMemoryStore(), tmp_path)
    prior, task, executor = running(h)
    entry = append(
        h, prior, task, executor, "candidate", 1, text="A saved, incomplete supplier comparison."
    )
    current = create_output(h, number=2)
    tool = next(item for item in project_journal_definitions() if item.id == "project.journal_read")
    context = ToolExecutionContext(
        actor_id=h.actor.actor_id,
        workspace_id=h.actor.workspace_id,
        run_id=current.id,
        agent_id="writer",
        invocation_id=uuid4(),
        allowed_tool_ids=frozenset({tool.id}),
        scopes=h.actor.scopes,
        authorized_action="read",
    )
    transport = ProjectJournalToolTransport(
        h.service, actor=h.actor, run_id=current.id, revalidate=lambda: h.actor
    )
    return h, prior, current, entry, tool, context, transport


def test_explicit_grant_reads_prior_saved_draft_as_untrusted_reference(tmp_path):
    h, prior, _, entry, tool, context, transport = setup(tmp_path)
    result = transport(tool, {"run_id": str(prior.id), "entry_id": str(entry.id)}, context)
    assert result["text"] == "A saved, incomplete supplier comparison."
    assert result["entry"]["status"] == "draft"
    assert result["untrusted_source"] and result["historical_record"]
    assert len(h.service.journal.list_run(h.actor, h.projects[0].id, prior.id)) == 1


@pytest.mark.parametrize("change", ["grant", "scope", "actor", "run", "agent"])
def test_journal_reuse_requires_exact_current_assignment_and_permissions(tmp_path, change):
    _, prior, _, entry, tool, context, transport = setup(tmp_path)
    changes = {
        "grant": {"allowed_tool_ids": frozenset()},
        "scope": {"scopes": frozenset()},
        "actor": {"actor_id": uuid4()},
        "run": {"run_id": uuid4()},
        "agent": {"agent_id": "unassigned"},
    }
    with pytest.raises(AuthorizationError):
        transport(
            tool,
            {"run_id": str(prior.id), "entry_id": str(entry.id)},
            context.model_copy(update=changes[change]),
        )


def test_journal_tool_cannot_read_other_project_even_for_same_owner(tmp_path):
    h, _, _, _, tool, context, transport = setup(tmp_path)
    foreign = create_output(h, number=3, project=1)
    with pytest.raises(NotFoundError):
        transport(tool, {"run_id": str(foreign.id), "entry_id": str(uuid4())}, context)
