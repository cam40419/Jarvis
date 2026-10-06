"""Maintenance is an explicit local command, with inspection as its default."""

import json
from types import SimpleNamespace
from uuid import uuid4

import pytest

from simon import backfill_outputs
from simon.domain.errors import AuthorizationError


@pytest.mark.parametrize("execute", [False, True])
def test_command_requires_execute_for_writes_and_closes_store(monkeypatch, capsys, execute):
    identifiers = {name: uuid4() for name in ("actor", "workspace", "project", "run")}
    actor = object()
    calls = []
    closed = []

    def resolve(actor_id, workspace):
        assert (actor_id, workspace) == (identifiers["actor"], identifiers["workspace"])
        return actor

    def authorize(current, project_id, *, write):
        assert current is actor and project_id == identifiers["project"] and write

    def backfill(current, project_id, run_id, **options):
        assert current is actor and (project_id, run_id) == (
            identifiers["project"],
            identifiers["run"],
        )
        calls.append(options)
        return {"created": 1 if execute else 0, "dry_run": not execute}

    services = SimpleNamespace(
        agent_runs=SimpleNamespace(actor_resolver=resolve),
        project_outputs=SimpleNamespace(
            authorize=authorize, journal=SimpleNamespace(backfill=backfill)
        ),
        store=SimpleNamespace(close=lambda: closed.append(True)),
    )
    monkeypatch.setattr(backfill_outputs, "AppContainer", lambda: services)
    arguments = [
        value
        for name, identifier in identifiers.items()
        for value in ("--" + name, str(identifier))
    ]
    assert backfill_outputs.main(arguments + (["--execute"] if execute else [])) == 0
    assert calls == [{"offset": 0, "limit": 20, "dry_run": not execute}]
    assert json.loads(capsys.readouterr().out)["created"] == (1 if execute else 0)
    assert closed == [True]


def test_command_denied_actor_does_not_call_backfill(monkeypatch, capsys):
    def denied(*_):
        raise AuthorizationError("Workspace membership required")

    closed = []
    services = SimpleNamespace(
        agent_runs=SimpleNamespace(actor_resolver=denied),
        project_outputs=SimpleNamespace(),
        store=SimpleNamespace(close=lambda: closed.append(True)),
    )
    monkeypatch.setattr(backfill_outputs, "AppContainer", lambda: services)
    arguments = [
        value
        for name in ("actor", "workspace", "project", "run")
        for value in ("--" + name, str(uuid4()))
    ]
    assert backfill_outputs.main([*arguments, "--execute"]) == 1
    assert json.loads(capsys.readouterr().out)["error"] == "forbidden"
    assert closed == [True]
