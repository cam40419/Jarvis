import io
from uuid import uuid4

import pytest
from docx import Document

from simon.domain.artifacts import ArtifactError
from simon.domain.errors import AuthorizationError, NotFoundError, ValidationError
from simon.domain.project_outputs import PromoteProjectOutput
from simon.services import report_documents
from simon.services.document_rendering import DOCX_MEDIA_TYPE
from simon.services.project_outputs import ProjectOutputService
from tests.contract.test_project_output_replication import create_output
from tests.contract.test_project_outputs import output_setup, replace_run


@pytest.fixture
def reports(store, tmp_path):
    h = output_setup(store, tmp_path)
    h.run = create_output(h)
    h.source = h.run.tasks[0].artifacts[1]
    return h


def test_document_cache_is_immutable_authorized_and_keeps_original_run(reports, monkeypatch):
    h = reports
    project = h.projects[0].id
    before = h.runs.get(h.actor, h.run.id).model_dump_json()
    artifact, content = h.service.documents.materialize(h.actor, project, h.run.id, h.source.id)
    assert artifact.name == "Supplier comparison.docx" and artifact.media_type == DOCX_MEDIA_TYPE
    assert "Result" in "\n".join(
        paragraph.text for paragraph in Document(io.BytesIO(content)).paragraphs
    )
    assert h.service.read(h.actor, project, h.run.id, h.source.id)[1] == b"Result"
    assert h.runs.get(h.actor, h.run.id).model_dump_json() == before
    saved = h.store.jobs(
        h.actor.workspace_id, h.actor.actor_id, report_documents.DOCUMENT_KIND, 0, 100
    )
    assert len(saved) == 1
    assert saved[0].input["source"]["source_sha256"] == h.source.sha256
    assert saved[0].input["source"]["renderer_version"] == report_documents.RENDERER_VERSION
    monkeypatch.setattr(
        report_documents,
        "render_report",
        lambda *args, **kwargs: pytest.fail("Cache was rendered again"),
    )
    restored = ProjectOutputService(h.runs, h.files)
    assert restored.documents.materialize(h.actor, project, h.run.id, h.source.id) == (
        artifact,
        content,
    )
    readonly = h.actor.model_copy(update={"scopes": h.actor.scopes - {"jobs:write"}})
    assert restored.documents.materialize(readonly, project, h.run.id, h.source.id) == (
        artifact,
        content,
    )
    with pytest.raises(NotFoundError):
        restored.documents.materialize(h.actor, h.projects[1].id, h.run.id, h.source.id)
    with pytest.raises((AuthorizationError, NotFoundError)):
        restored.documents.materialize(
            h.actor.model_copy(update={"actor_id": uuid4()}), project, h.run.id, h.source.id
        )


def test_response_requires_real_named_unchanged_publication_even_after_cached(reports):
    h = reports
    project, run = h.projects[0].id, h.run
    answer = run.tasks[0].artifacts[0]
    with pytest.raises(ValidationError, match="report deliverables"):
        h.service.documents.materialize(h.actor, project, run.id, answer.id)
    generic = h.service.promote(
        h.actor,
        project,
        run.id,
        answer.id,
        PromoteProjectOutput(idempotency_key="generic-response"),
        lambda: h.actor,
    )
    assert generic.document_url is None
    with pytest.raises(ValidationError):
        h.service.documents.materialize(h.actor, project, run.id, answer.id)
    named = h.service.promote(
        h.actor,
        project,
        run.id,
        answer.id,
        PromoteProjectOutput(
            path="research/Manufacturing strategy.md", idempotency_key="named-report"
        ),
        lambda: h.actor,
    )
    artifact, content = h.service.documents.materialize(h.actor, project, run.id, answer.id)
    assert artifact.name == "Manufacturing strategy.docx"
    assert content.startswith(b"PK")
    item, _ = h.service.source(h.actor, project, run.id, answer.id)
    assert item.document_name == artifact.name and item.document_url.endswith("/document")
    copy = named.project_copy
    h.files.path(h.actor, copy.root, copy.path).write_text("Edited after publication")
    assert h.service.source(h.actor, project, run.id, answer.id)[0].document_url is None
    with pytest.raises(ValidationError):
        h.service.documents.materialize(h.actor, project, run.id, answer.id)


@pytest.mark.parametrize(
    "name,mime",
    [
        ("README.md", "text/markdown"),
        ("readme.en.md", "text/markdown"),
        ("requirements.txt", "text/plain"),
        ("script.py", "text/plain"),
        ("report.pdf", "application/pdf"),
        ("report.docx", DOCX_MEDIA_TYPE),
        ("data.csv", "text/csv"),
        ("binary.txt", "application/octet-stream"),
    ],
)
def test_native_and_technical_files_have_no_word_derivative(reports, name, mime):
    h = reports
    original = h.source
    file = h.service.artifacts.publish_bytes(
        workspace_id=original.workspace_id,
        actor_id=original.actor_id,
        run_id=original.run_id,
        task_id=original.task_id,
        name=name,
        media_type=mime,
        content=b"original native contents",
    )
    replace_run(
        h,
        h.run.model_copy(
            update={
                "tasks": (
                    h.run.tasks[0].model_copy(
                        update={"artifacts": (h.run.tasks[0].artifacts[0], file)}
                    ),
                )
            }
        ),
    )
    item, content = h.service.read(h.actor, h.projects[0].id, h.run.id, file.id)
    assert content == b"original native contents" and item.document_url is None
    with pytest.raises(ValidationError):
        h.service.documents.materialize(h.actor, h.projects[0].id, h.run.id, file.id)


def test_failed_task_and_planning_never_gain_word_export(reports):
    h = reports
    replace_run(
        h,
        h.run.model_copy(
            update={"tasks": (h.run.tasks[0].model_copy(update={"status": "failed"}),)}
        ),
    )
    with pytest.raises(NotFoundError):
        h.service.documents.materialize(h.actor, h.projects[0].id, h.run.id, h.source.id)


def test_tampered_cached_document_fails_checksum_without_regeneration(reports):
    h = reports
    artifact, _ = h.service.documents.materialize(h.actor, h.projects[0].id, h.run.id, h.source.id)
    (h.service.artifacts._directory(artifact) / "content").write_bytes(b"corrupt")
    with pytest.raises(ArtifactError, match="integrity"):
        h.service.documents.materialize(h.actor, h.projects[0].id, h.run.id, h.source.id)


def test_long_report_filename_stays_within_artifact_limit(reports):
    h = reports
    source = h.service.artifacts.publish_bytes(
        workspace_id=h.source.workspace_id,
        actor_id=h.source.actor_id,
        run_id=h.source.run_id,
        task_id=h.source.task_id,
        name="a" * 156 + ".txt",
        media_type="text/plain",
        content=b"Full report text",
    )
    replace_run(
        h,
        h.run.model_copy(
            update={
                "tasks": (
                    h.run.tasks[0].model_copy(
                        update={"artifacts": (h.run.tasks[0].artifacts[0], source)}
                    ),
                )
            }
        ),
    )
    artifact, content = h.service.documents.materialize(
        h.actor, h.projects[0].id, h.run.id, source.id
    )
    assert len(artifact.name) == 160 and artifact.name.endswith(".docx")
    assert content.startswith(b"PK")


def test_renderer_limit_failure_is_readable_and_never_publishes_provenance(reports, monkeypatch):
    h = reports

    def unsupported(*args, **kwargs):
        raise ValueError("Report table exceeds supported layout bounds")

    monkeypatch.setattr(report_documents, "render_report", unsupported)
    with pytest.raises(ValidationError, match="layout bounds"):
        h.service.documents.materialize(h.actor, h.projects[0].id, h.run.id, h.source.id)
    assert not h.store.jobs(
        h.actor.workspace_id, h.actor.actor_id, report_documents.DOCUMENT_KIND, 0, 100
    )
    assert h.service.read(h.actor, h.projects[0].id, h.run.id, h.source.id)[1] == b"Result"


def test_files_filter_pages_verified_named_responses_without_reclassifying_history(reports):
    h = reports
    project = h.projects[0].id
    expected = {h.source.id}
    named_ids = set()
    copies = []
    for number in (2, 3):
        run = create_output(h, number)
        expected.add(run.tasks[0].artifacts[1].id)
        response = run.tasks[0].artifacts[0]
        item = h.service.promote(
            h.actor,
            project,
            run.id,
            response.id,
            PromoteProjectOutput(
                path=f"research/Decision report {number}.md",
                idempotency_key=f"named-decision-report-{number}",
            ),
            lambda: h.actor,
        )
        expected.add(response.id)
        named_ids.add(response.id)
        copies.append(item.project_copy)

    def listed():
        items, cursor, seen = [], None, set()
        while True:
            page = h.service.list(h.actor, project, kind="deliverable", limit=1, cursor=cursor)
            items.extend(page.items)
            cursor = page.next_cursor
            if not cursor:
                return items
            assert cursor not in seen
            seen.add(cursor)

    items = listed()
    assert {item.id for item in items} == expected and len(items) == len(expected)
    assert {item.id for item in items if item.kind == "response"} == named_ids
    assert all(item.document_url for item in items if item.kind == "response")
    history = h.service.list(h.actor, project, kind="response")
    assert len(history.items) == 3 and all(item.kind == "response" for item in history.items)
    # A subsequent user edit removes only the stale formatted-report offer.
    copied = copies[0]
    h.files.path(h.actor, copied.root, copied.path).write_text("New human-authored draft")
    retained = listed()
    assert len(retained) == len(expected) - 1
    assert len(h.service.list(h.actor, project, kind="response").items) == 3
