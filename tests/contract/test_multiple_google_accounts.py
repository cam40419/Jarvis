import json
from time import time
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

import pytest

from simon.adapters.google import (
    CALENDAR_SCOPE,
    DRIVE_WRITE_SCOPE,
    EMAIL_SCOPE,
    GMAIL_READ_SCOPE,
    ConnectedError,
    GoogleTokens,
)
from simon.domain.connected_tools import CalendarDraft
from simon.domain.project_files import ProjectBind, ProjectCreate, ProjectFiles
from tests.contract.test_calendar_immediate import pending_calendar
from tests.contract.test_connected import EVENT, connected_setup, make_proposal
from tests.contract.test_project_files import setup_project


def add(
    service,
    actor,
    email="second@example.com",
    scopes=(CALENDAR_SCOPE, EMAIL_SCOPE, GMAIL_READ_SCOPE, DRIVE_WRITE_SCOPE),
):
    first = service.connection(actor)
    tokens = GoogleTokens(
        access_token="second-access", refresh_token="second-refresh", expires_at=time() + 3600
    )
    second = first.model_copy(
        update={
            "id": uuid4(),
            "email": email,
            "scopes": scopes,
            "encrypted_tokens": service.encrypt(tokens.model_dump_json()),
            "is_default": False,
        }
    )
    service.store.save_google_connection(second)
    return service.connection(actor, account=email)


def test_oauth_add_reconnect_default_disconnect_and_isolation(store):
    service, actor, session = connected_setup(store)
    original = service.connection(actor)
    service.api.account_email = lambda token: "SECOND@example.com"
    for _ in range(2):
        url, binding = service.start(actor, session)
        params = parse_qs(urlsplit(url).query)
        assert "select_account" in params["prompt"][0]
        service.callback(params["state"][0], binding, "code")
        assert service.connection(actor).id == original.id
        assert len(service.status(actor)["accounts"]) == 2
    second = service.connection(actor, account="second@example.com")
    other = actor.model_copy(update={"actor_id": uuid4()})
    with pytest.raises(ConnectedError):
        service.connection(other, second.id)
    service.set_default(actor, str(second.id))
    assert service.connection(actor).id == second.id
    # A stale token refresh must not restore the previous default.
    service.store.save_google_connection(original)
    assert service.connection(actor).id == second.id
    service.disconnect(actor, str(original.id))
    assert len(service.status(actor)["accounts"]) == 1
    assert service.connection(actor).id == second.id
    third = add(service, actor, "third@example.com")
    service.disconnect(actor, str(second.id))
    assert service.connection(actor).id == third.id
    assert service.connection(actor).is_default
    assert "encrypted_tokens" not in json.dumps(service.status(actor))


def test_tools_select_account_scopes_and_preserve_read_binding(store):
    service, actor, _ = connected_setup(store)
    second = add(service, actor, scopes=(GMAIL_READ_SCOPE,))
    calls = []
    service.api.gmail_search = lambda token, query: (
        calls.append(token) or {"messages": [], "next_page_token": ""}
    )
    execute = service.executor(actor, uuid4(), [], lambda: actor)
    accounts = json.loads(execute("google_accounts_list", "{}"))["accounts"]
    assert len(accounts) == 2
    assert "error" in json.loads(execute("gmail_search_messages", "{}"))
    result = json.loads(execute("gmail_search_messages", json.dumps({"account": second.email})))
    assert result["account_email"] == second.email and calls == ["second-access"]
    assert "error" in json.loads(
        execute("gmail_search_messages", '{"account":"unknown@example.com"}')
    )

    def disconnect(token, query):
        service.disconnect(actor, second.email)
        return {"private": "must not escape"}

    service.api.gmail_search = disconnect
    result = execute("gmail_search_messages", json.dumps({"account": second.email}))
    assert "error" in result and "must not escape" not in result


def test_calendar_and_email_stay_on_selected_account_after_default_change(store):
    service, actor, _ = connected_setup(store)
    original = service.connection(actor)
    second = add(service, actor)
    calls = []
    service.api.execute = lambda token, action, sender: (
        calls.append((token, sender)) or "receipt",
        None,
    )
    attempt = pending_calendar(service, actor)
    result = service.create_calendar_event(
        actor, attempt.run.id, CalendarDraft(**EVENT, account=second.email), lambda: actor
    )
    assert result.status == "succeeded" and result.account_email == second.email
    assert calls == [("second-access", second.email)]
    preview, _, _ = make_proposal(service, actor)
    service.set_default(actor, second.email)
    result = service.decide(actor, preview.id, confirm=True, revalidate=lambda: actor)
    assert result.status == "succeeded"
    assert calls[-1] == ("access-secret", original.email)


def test_project_keeps_its_account_and_can_explicitly_relink(store):
    connected, actor, files, project = setup_project(store)
    original = connected.connection(actor)
    second = add(connected, actor)
    connected.set_default(actor, second.email)
    tokens = []
    original_list = files.api.list_files
    files.api.list_files = lambda token, *args: tokens.append(token) or original_list(token, *args)
    files.files(actor, ProjectFiles(project_id=project))
    assert tokens == ["access-secret"]
    assert files.view(actor, files.binding(actor, project))["status"] == "ready"
    created = files.create(
        actor,
        ProjectCreate(
            name="Second project",
            description="In account two",
            account=second.email,
            idempotency_key="second-project",
        ),
    )
    assert created["drive"]["google_email"] == second.email
    binding = files.binding(actor, project)
    files.bind(
        actor,
        ProjectBind(
            project_id=project,
            folder_id=created["drive"]["folder_id"],
            expected_version=binding.version,
            account=second.email,
        ),
    )
    files.files(actor, ProjectFiles(project_id=project))
    assert tokens[-1] == "second-access"
    connected.disconnect(actor, original.email)
    assert files.sync(actor, project, force=True)["status"] == "ready"
    connected.disconnect(actor, second.email)
    with pytest.raises(ConnectedError):
        files.files(actor, ProjectFiles(project_id=project))


def test_reconnect_hint_cannot_replace_another_account(store):
    service, actor, session = connected_setup(store)
    original = service.connection(actor)
    second = add(service, actor)
    url, binding = service.start(actor, session, second.email)
    params = parse_qs(urlsplit(url).query)
    assert params["login_hint"] == [second.email]
    with pytest.raises(ConnectedError, match="same Google account"):
        service.callback(params["state"][0], binding, "wrong-account-code")
    assert service.connection(actor).id == original.id
    assert service.connection(actor, account=second.email).id == second.id
