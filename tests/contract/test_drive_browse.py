"""Generic Drive browsing keeps live account boundaries without project state."""

import json
from uuid import uuid4

import pytest

from simon.adapters.drive import FOLDER
from simon.adapters.google import DRIVE_READ_SCOPE
from simon.domain.drive import DriveBrowse
from simon.domain.errors import AuthorizationError
from tests.contract.test_connected import connected_setup
from tests.contract.test_google_read_permissions import grant


def test_drive_browse_is_account_scoped_and_revalidates_after_read(store):
    connected, actor, _ = connected_setup(store)
    grant(connected, actor, (DRIVE_READ_SCOPE,))
    calls = []

    def metadata(token, identifier):
        assert token == "access-secret" and identifier == "root"
        return {"id": "owned-root", "mimeType": FOLDER}

    def listing(token, folder, query, page, **flags):
        calls.append((token, folder, query, page, flags))
        return {"files": [{"id": "source", "name": "notes.txt"}], "next_page_token": "next"}

    connected.drive.api.metadata = metadata
    connected.drive.api.list_files = listing
    execute = connected.executor(actor, uuid4(), [], lambda: actor)
    result = json.loads(execute("drive_list_folder", '{"folder_id":"root","query":"notes"}'))
    assert result["account_email"] == "owner@example.com"
    assert result["next_page_token"] == "next"
    assert calls[0][1:4] == ("owned-root", "notes", "")
    assert not store.explicit_memories(actor.workspace_id, 0, 100, actor.actor_id)

    def revoke_after_read(*args, **kwargs):
        grant(connected, actor, ())
        return listing(*args, **kwargs)

    connected.drive.api.list_files = revoke_after_read
    with pytest.raises(AuthorizationError, match="Drive access changed"):
        connected.drive.browse(actor, DriveBrowse(), lambda: actor)
