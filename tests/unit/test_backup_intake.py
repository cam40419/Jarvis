"""Recovery retains native intake originals and model catalog without implicit secrets."""

import json
import sys
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from simon.services.backup import restore_files, verify_bundle
from simon.services.intake_sources import IntakeSourceBytes


@pytest.fixture
def backup_command(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[2] / "scripts"))
    from scripts import backup_bundle

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(backup_bundle, "ROOT", tmp_path)
    files = tmp_path / "files"
    files.mkdir()
    catalog = tmp_path / "private-model-catalog.json"
    catalog.write_text(
        json.dumps(
            [
                {
                    "id": "approved",
                    "name": "Approved model",
                    "credential_required": True,
                    "workspace_ids": [str(uuid4())],
                    "endpoint": {
                        "id": "approved",
                        "provider": "openai_compatible",
                        "model": "tested-model",
                        "base_url": "https://model.example.invalid/v1",
                        "api_key_env": "MODEL_PROJECT_KEY",
                    },
                }
            ]
        ),
        encoding="utf-8",
    )
    settings = SimpleNamespace(
        local_files_dir=files,
        external_providers_file=None,
        model_catalog_file=catalog,
        integration_key_file=tmp_path / "credentials.key",
    )
    (tmp_path / ".env").write_text("PILOT_MODEL_KEY=synthetic-test-secret", encoding="utf-8")
    settings.integration_key_file.write_bytes(b"synthetic-encryption-key")
    monkeypatch.setattr(backup_bundle, "Settings", lambda: settings)
    calls = []

    def dumping(database, target):
        calls.append(database)
        target.write_bytes(b"synthetic database archive")

    monkeypatch.setattr(backup_bundle, "dump_database", dumping)
    monkeypatch.setattr(backup_bundle, "snapshot", lambda _: {"native_intakes": "table-hash"})
    return SimpleNamespace(command=backup_bundle, settings=settings, calls=calls, root=tmp_path)


@pytest.mark.parametrize("include_secrets", [False, True])
def test_backup_roundtrip_preserves_catalog_and_immutable_source_tree(
    backup_command, monkeypatch, include_secrets
):
    h = backup_command
    workspace, project = uuid4(), uuid4()
    original = b"Original project source, preserved independently from extracted previews."
    blobs = IntakeSourceBytes(h.settings.local_files_dir / ".project-sources")
    source_digest = blobs.put(workspace, project, original)
    destination = h.root / "backup"
    args = ["backup_bundle.py", "create", str(destination), "--writers-stopped"]
    if include_secrets:
        args.append("--include-secrets")
    monkeypatch.setattr(sys, "argv", args)
    h.command.main()
    manifest = verify_bundle(destination)
    assert h.calls == ["jarvis"]
    assert manifest.includes_secrets is include_secrets
    assert (
        destination / "configuration/model-catalog.json"
    ).read_bytes() == h.settings.model_catalog_file.read_bytes()
    assert (destination / "configuration/server.env").exists() is include_secrets
    assert (destination / "configuration/credentials.key").exists() is include_secrets
    assert "MODEL_PROJECT_KEY" in (destination / "configuration/model-catalog.json").read_text()
    assert (
        "synthetic-test-secret"
        not in (destination / "configuration/model-catalog.json").read_text()
    )
    recovered = h.root / "recovered"
    restore_files(destination, recovered)
    restored_blobs = IntakeSourceBytes(recovered / "files/.project-sources")
    assert restored_blobs.read(workspace, project, source_digest, len(original)) == original
    assert verify_bundle(recovered) == manifest


def test_configured_missing_catalog_prevents_publishing_incomplete_recovery(
    backup_command, monkeypatch
):
    h = backup_command
    h.settings.model_catalog_file = h.root / "missing-models.json"
    monkeypatch.setattr(
        sys, "argv", ["backup_bundle.py", "create", str(h.root / "backup"), "--writers-stopped"]
    )
    with pytest.raises(FileNotFoundError):
        h.command.main()
    assert h.calls == []
    assert not (h.root / "backup").exists()


def test_unconfigured_catalog_is_optional_and_does_not_invent_model_configuration(
    backup_command, monkeypatch
):
    h = backup_command
    h.settings.model_catalog_file = None
    destination = h.root / "backup"
    monkeypatch.setattr(
        sys, "argv", ["backup_bundle.py", "create", str(destination), "--writers-stopped"]
    )
    h.command.main()
    assert "configuration/model-catalog.json" not in verify_bundle(destination).files
