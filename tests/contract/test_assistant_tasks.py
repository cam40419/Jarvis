from uuid import uuid4

from simon.config import Settings
from simon.domain.context import CreateMemory
from simon.domain.identity import DEV_ACTOR_ID, DEV_HOUSEHOLD_ID, Membership
from simon.domain.models import ActorContext, Channel
from simon.domain.tasks import CreateAssistantTask
from simon.services.audit import AuditService
from simon.services.identity import ROLE_SCOPES, IdentityService
from simon.services.memory import MemoryService
from simon.services.model_conversations import ModelConversationService
from simon.services.tasks import AssistantTaskService
from tests.contract.test_model_runs import FakeModel


def test_worker_completes_a_durable_task_and_saves_its_private_thread(store):
    store.put_membership(
        Membership(
            actor_id=DEV_ACTOR_ID,
            household_id=DEV_HOUSEHOLD_ID,
            role="owner",
        )
    )
    actor = ActorContext(
        actor_id=DEV_ACTOR_ID,
        household_id=DEV_HOUSEHOLD_ID,
        channel=Channel.API,
        scopes=ROLE_SCOPES["owner"],
    )
    model = FakeModel()
    conversations = ModelConversationService(
        store, AuditService(store), model, Settings(_env_file=None)
    )
    tasks = AssistantTaskService(store, IdentityService(store, Settings()), conversations)
    created = tasks.create(
        actor,
        CreateAssistantTask(
            title="Evaluate insulation",
            instructions="Compare the options and make a recommendation.",
            task_type="research",
            priority=4,
            idempotency_key=str(uuid4()),
        ),
    )

    assert tasks.tick() == 1
    completed = tasks.get(actor, created.id)
    assert completed.status == "succeeded"
    assert completed.progress == 100
    assert completed.result == "A useful answer."
    assert completed.thread_id is not None and completed.run_id is not None
    assert "Evaluate insulation" in model.requests[0].input_text
    assert model.requests[0].reasoning_effort == "high"


def test_linked_task_publishes_result_and_generated_files_to_project(store):
    store.put_membership(
        Membership(
            actor_id=DEV_ACTOR_ID,
            household_id=DEV_HOUSEHOLD_ID,
            role="owner",
        )
    )
    actor = ActorContext(
        actor_id=DEV_ACTOR_ID,
        household_id=DEV_HOUSEHOLD_ID,
        channel=Channel.API,
        scopes=ROLE_SCOPES["owner"],
    )
    audit = AuditService(store)
    project = MemoryService(store, audit).create(
        actor,
        CreateMemory(
            subject="Solar",
            content="Compare proposals",
            scope="personal",
            category="project",
            idempotency_key=str(uuid4()),
        ),
    )
    model = FakeModel()
    original = model.generate

    def generate(request):
        answer = original(request)
        return answer.model_copy(
            update={
                "text": "# Recommendation\nUse option A.\n\n"
                "```file:comparison.csv\noption,cost\nA,100\nB,120\n```\n\n"
                "```file:notes.json\n{\"winner\": \"A\"}\n```\n\n"
                "```file:../unsafe.txt\nnope\n```"
            }
        )

    model.generate = generate
    conversations = ModelConversationService(
        store, audit, model, Settings(_env_file=None)
    )
    tasks = AssistantTaskService(store, IdentityService(store, Settings()), conversations)
    created = tasks.create(
        actor,
        CreateAssistantTask(
            title="Compare solar bids",
            instructions="Return a comparison CSV and JSON summary.",
            project_id=project.id,
            task_type="research",
            idempotency_key=str(uuid4()),
        ),
    )

    assert tasks.tick() == 1
    completed = tasks.get(actor, created.id)
    assert completed.status == "succeeded" and completed.artifact_count == 3
    artifacts = tasks.task_artifacts(actor, created.id)
    assert {artifact.name for artifact in artifacts} == {
        "result.md",
        "comparison.csv",
        "notes.json",
    }
    csv = next(artifact for artifact in artifacts if artifact.name == "comparison.csv")
    assert tasks.artifact(actor, csv.id)[1] == b"option,cost\nA,100\nB,120"
