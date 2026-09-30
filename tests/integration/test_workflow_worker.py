import os
import subprocess
import sys
from uuid import uuid4

from simon.adapters.postgres import PostgresStore
from simon.domain.workflows import SaveWorkflow, StartWorkflow, WorkflowSpec
from simon.services.workflows import WorkflowService
from tests.contract.test_connected import connected_setup


def test_separate_worker_process_resumes_saved_run(postgres_url, tmp_path):
    connected, actor, _ = connected_setup(PostgresStore(postgres_url))
    service = WorkflowService(connected.store, connected.identity)
    saved = service.save(
        actor,
        SaveWorkflow(
            spec=WorkflowSpec(
                name="Process restart",
                steps=[
                    {"id": "before", "action": "system.echo", "inputs": {"message": "once"}},
                    {"id": "checkpoint", "kind": "wait", "depends_on": ["before"]},
                    {"id": "after", "action": "system.echo", "depends_on": ["checkpoint"]},
                ],
            ),
            idempotency_key=str(uuid4()),
        ),
    )
    run = service.start(
        actor, saved.id, StartWorkflow(definition_version=1, idempotency_key=str(uuid4()))
    )
    environment = os.environ | {
        "SIMON_DATABASE_URL": postgres_url,
        "SIMON_STORAGE_BACKEND": "postgres",
    }
    for _ in range(4):
        with (tmp_path / "worker.log").open("a") as log:
            result = subprocess.run(
                [sys.executable, "-m", "simon.workflow_worker", "--once"],
                env=environment,
                stdout=log,
                stderr=log,
                timeout=20,
                creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
            )
        assert result.returncode == 0
    final = service.get(actor, run.id)
    assert final.status == "succeeded"
    assert len(final.steps[0].attempts) == 1
    assert len(service.events(actor, run.id, 0)) == final.version
