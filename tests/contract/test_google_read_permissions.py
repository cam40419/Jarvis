"""Google read capability isolation and OAuth upgrades."""

import json
from time import time
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

import pytest

from simon.adapters.google import (
    DRIVE_READ_SCOPE,
    DRIVE_WRITE_SCOPE,
    GMAIL_READ_SCOPE,
    GoogleTokens,
)
from simon.domain.errors import AuthorizationError
from tests.contract.test_connected import connected_setup


def grant(service, actor, scopes):
    connection = service.connection(actor)
    service.store.save_google_connection(connection.model_copy(update={"scopes": scopes}))


@pytest.mark.parametrize("scope,tools,flag", [
    (GMAIL_READ_SCOPE, ("gmail_search_messages", "gmail_read_message"), "gmail_read"),
    (DRIVE_READ_SCOPE, ("drive_search_files", "drive_read_file"), "drive_read"),
])
def test_read_only_oauth_grants_and_independent_tools(store, scope, tools, flag):
    service, actor, session = connected_setup(store)
    assert service.status(actor)["needs_reconnect"]
    url, binding = service.start(actor, session)
    params = parse_qs(urlsplit(url).query)
    assert GMAIL_READ_SCOPE in params["scope"][0] and DRIVE_WRITE_SCOPE in params["scope"][0]
    tokens = GoogleTokens(access_token="access", refresh_token="refresh", expires_at=time() + 3600)
    service.api.exchange = lambda *args: (tokens, (scope,))
    service.callback(params["state"][0], binding, "code")
    assert service.status(actor)[flag]
    assert set(tools) <= set(service.available(actor))
    assert "propose_email" not in service.available(actor)
    assert "calendar_list_events" not in service.available(actor)


@pytest.mark.parametrize("name,method,args,scope", [
    ("gmail_search_messages", "gmail_search", {"query": "is:unread"}, GMAIL_READ_SCOPE),
    ("gmail_read_message", "gmail_message", {"id": "abc"}, GMAIL_READ_SCOPE),
    ("drive_search_files", "drive_search", {"query": "notes"}, DRIVE_READ_SCOPE),
    ("drive_read_file", "drive_file", {"id": "file"}, DRIVE_READ_SCOPE),
])
def test_read_dispatch_scope_validation_and_disconnect(store, name, method, args, scope):
    service, actor, _ = connected_setup(store)
    execute = service.executor(actor, uuid4(), [], lambda: actor)
    with pytest.raises(AuthorizationError):
        execute(name, json.dumps(args))
    grant(service, actor, (scope,))
    calls = []

    def read(token, query):
        calls.append(query)
        assert token == "access-secret"
        return {"content": "synthetic private result"}

    setattr(service.api, method, read)
    assert json.loads(execute(name, json.dumps(args)))["content"] == "synthetic private result"
    assert len(calls) == 1
    assert "error" in json.loads(execute(name, '{"id":"../token","query":"bad"}'))
    assert len(calls) == 1

    def disconnect_during_read(token, query):
        service.disconnect(actor)
        return {"content": "must not be returned"}

    setattr(service.api, method, disconnect_during_read)
    result = execute(name, json.dumps(args))
    assert "error" in result and "must not be returned" not in result


def test_google_read_cannot_cross_accounts(store):
    service, actor, _ = connected_setup(store)
    grant(service, actor, (GMAIL_READ_SCOPE, DRIVE_READ_SCOPE))
    other = actor.model_copy(update={"actor_id": uuid4()})
    assert "gmail_read_message" not in service.available(other)
    execute = service.executor(actor, uuid4(), [], lambda: other)
    with pytest.raises(AuthorizationError):
        execute("gmail_read_message", '{"id":"abc"}')
