from __future__ import annotations

import io
import json
import os
import sys
from pathlib import Path

import pytest
from dotenv.parser import parse_stream

from scripts import migrate_storage as command
from simon.services import storage_migration as migration
from simon.services.backup import inventory


@pytest.fixture
def installation(tmp_path: Path) -> tuple[Path, dict[str, Path], Path]:
    checkout = tmp_path / "checkout"
    roots = {name: checkout / ".local" / name for name in ("files", "agents")}
    for root in roots.values():
        root.mkdir(parents=True)
    (roots["files"] / "empty folder").mkdir()
    (roots["files"] / "report.txt").write_bytes(b"saved original research\x00\xff")
    (roots["agents"] / "leases.sqlite3").write_bytes(
        b"synthetic absolute old path: " + str(roots["agents"]).encode()
    )
    (checkout / ".env").write_bytes(
        b"# existing private configuration\r\n"
        b"SIMON_API_KEY='synthetic-secret'\r\n"
        b"SIMON_LOCAL_FILES_DIR=.local/files\r\n"
        b"SIMON_AGENT_STATE_DIR=.local/agents\r\n"
        b"SIMON_LOG_LEVEL=info\r\n"
    )
    return checkout, roots, tmp_path / "permanent data"


def invoke(installation: tuple[Path, dict[str, Path], Path]) -> migration.StorageMigrationResult:
    checkout, roots, destination = installation
    return migration.migrate_storage(
        destination, checkout=checkout, roots=roots, writers_stopped=True
    )


def test_copy_verified_originals_retained_private_backup_and_exact_bindings(
    installation: tuple[Path, dict[str, Path], Path],
) -> None:
    checkout, roots, destination = installation
    original_env = (checkout / ".env").read_bytes()
    originals = {name: inventory(root) for name, root in roots.items()}
    result = invoke(installation)
    assert result.configuration_updated
    assert result.file_counts == {"files": 1, "agents": 1}
    assert Path(result.configuration_backup).read_bytes() == original_env
    assert "synthetic-secret" not in json.dumps(result.public_dict())
    assert "synthetic-secret" not in Path(result.receipt_path).read_text()
    for name, root in roots.items():
        assert inventory(root) == originals[name] == inventory(destination / name)
    env = (checkout / ".env").read_bytes()
    assert b"SIMON_API_KEY='synthetic-secret'\r\n" in env
    assert b"SIMON_LOG_LEVEL=info\r\n" in env
    values = {binding.key: binding.value for binding in parse_stream(io.StringIO(env.decode()))}
    assert values["SIMON_LOCAL_FILES_DIR"] == (destination / "files").as_posix()
    assert values["SIMON_AGENT_STATE_DIR"] == (destination / "agents").as_posix()
    receipt = json.loads(Path(result.receipt_path).read_bytes())
    assert receipt["inventories"]["files"]["directories"] == ["empty folder"]
    assert receipt["inventories"]["files"]["files"]["report.txt"]["size"] == 25
    if os.name != "nt":
        assert Path(result.configuration_backup).stat().st_mode & 0o777 == 0o600
        assert Path(result.configuration_backup).parent.stat().st_mode & 0o777 == 0o700


def test_resume_matching_partial_copy_and_already_migrated_roots(
    installation: tuple[Path, dict[str, Path], Path],
) -> None:
    checkout, roots, destination = installation
    (destination / "files").mkdir(parents=True)
    copy = destination / "files/report.txt"
    copy.write_bytes((roots["files"] / "report.txt").read_bytes())
    original_time = copy.stat().st_mtime_ns
    invoke(installation)
    assert copy.stat().st_mtime_ns == original_time
    rerun = migration.migrate_storage(
        destination,
        checkout=checkout,
        roots={name: destination / name for name in roots},
        writers_stopped=True,
    )
    assert not rerun.configuration_updated


@pytest.mark.parametrize("conflict", ["wrong_bytes", "extra_file", "extra_dir", "extra_root"])
def test_conflict_preflight_keeps_both_sources_and_env_unchanged(
    installation: tuple[Path, dict[str, Path], Path], conflict: str
) -> None:
    checkout, roots, destination = installation
    env = (checkout / ".env").read_bytes()
    (destination / "agents").mkdir(parents=True)
    if conflict == "wrong_bytes":
        (destination / "agents/leases.sqlite3").write_bytes(b"different")
    elif conflict == "extra_file":
        (destination / "agents/unrelated.txt").write_bytes(b"private")
    elif conflict == "extra_dir":
        (destination / "agents/unrelated").mkdir()
    else:
        (destination / "unrelated").mkdir()
    before = inventory(destination)
    with pytest.raises(ValueError, match=r"conflicting|unexpected"):
        invoke(installation)
    assert (checkout / ".env").read_bytes() == env
    assert inventory(destination) == before
    assert not (destination / "files").exists()
    assert all(root.is_dir() for root in roots.values())


@pytest.mark.parametrize("unsafe", ["relative", "checkout", "ancestor", "root", "overlap"])
def test_unsafe_destination_refused(
    installation: tuple[Path, dict[str, Path], Path], unsafe: str
) -> None:
    checkout, roots, _ = installation
    destinations = {
        "relative": Path("relative"),
        "checkout": checkout / "permanent",
        "ancestor": checkout.parent,
        "root": Path(checkout.anchor),
        "overlap": roots["files"] / "nested",
    }
    original = (checkout / ".env").read_bytes()
    with pytest.raises(ValueError, match=r"absolute|outside|overlaps"):
        migration.migrate_storage(
            destinations[unsafe], checkout=checkout, roots=roots, writers_stopped=True
        )
    assert (checkout / ".env").read_bytes() == original


def test_stopped_writers_required(installation: tuple[Path, dict[str, Path], Path]) -> None:
    checkout, roots, destination = installation
    with pytest.raises(ValueError, match="stopped"):
        migration.migrate_storage(
            destination, checkout=checkout, roots=roots, writers_stopped=False
        )
    assert not destination.exists()


@pytest.mark.parametrize("mutation", ["source", "environment", "destination"])
def test_concurrent_mutation_refuses_publication(
    installation: tuple[Path, dict[str, Path], Path],
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
) -> None:
    checkout, roots, destination = installation
    original = (checkout / ".env").read_bytes()
    copier = migration._copy_file

    def change(source: Path, target: Path) -> None:
        copier(source, target)
        if source.name == "leases.sqlite3":
            if mutation == "source":
                (roots["files"] / "report.txt").write_bytes(b"edited by another writer")
            elif mutation == "destination":
                (destination / "files/report.txt").write_bytes(b"edited destination")
            else:
                (checkout / ".env").write_bytes(original + b"OTHER_SETTING=concurrent\r\n")

    monkeypatch.setattr(migration, "_copy_file", change)
    with pytest.raises(ValueError, match=r"changed|verification failed"):
        invoke(installation)
    expected = original + b"OTHER_SETTING=concurrent\r\n" if mutation == "environment" else original
    assert (checkout / ".env").read_bytes() == expected


def test_replace_failure_preserves_original_configuration_and_backup(
    installation: tuple[Path, dict[str, Path], Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    checkout, _, _ = installation
    original = (checkout / ".env").read_bytes()

    def fail(source: Path, destination: Path) -> None:
        raise OSError("synthetic publication failure")

    monkeypatch.setattr(migration.os, "replace", fail)
    with pytest.raises(OSError, match="publication"):
        invoke(installation)
    assert (checkout / ".env").read_bytes() == original
    backups = list((checkout / ".local/storage-migrations").glob("*/prechange.env"))
    assert len(backups) == 1 and backups[0].read_bytes() == original


def test_multiline_binding_bom_duplicates_and_unrelated_bytes_preserved(tmp_path: Path) -> None:
    unrelated = (
        b"# private\r\n"
        b"SECRET='line one\r\nSIMON_LOCAL_FILES_DIR=not-a-real-binding\r\nlast line'\r\n"
        b"export ANOTHER_SECRET=hello#literal\r\n"
    )
    original = (
        b"\xef\xbb\xbf"
        + unrelated
        + (b"SIMON_LOCAL_FILES_DIR=old\r\nSIMON_LOCAL_FILES_DIR=duplicate\r\nFINAL=value")
    )
    output = migration.storage_environment(
        original, {"files": tmp_path / "files", "agents": tmp_path / "agents"}
    )
    assert output.startswith(b"\xef\xbb\xbf" + unrelated)
    assert b"FINAL=value\r\nSIMON_AGENT_STATE_DIR=" in output
    bindings = list(parse_stream(io.StringIO(output.decode("utf-8-sig"))))
    assert len([entry for entry in bindings if entry.key == "SIMON_LOCAL_FILES_DIR"]) == 1
    assert all(not entry.error for entry in bindings)


def test_invalid_environment_never_copies_or_prints_secret(
    installation: tuple[Path, dict[str, Path], Path],
) -> None:
    checkout, _, destination = installation
    (checkout / ".env").write_bytes(b"SECRET='unterminated-synthetic-private")
    with pytest.raises(ValueError, match="invalid environment") as error:
        invoke(installation)
    assert "synthetic-private" not in str(error.value)
    assert not destination.exists()


@pytest.mark.parametrize("location", ["source_child", "destination_parent"])
def test_redirect_is_rejected(
    installation: tuple[Path, dict[str, Path], Path], location: str
) -> None:
    checkout, roots, destination = installation
    original = (checkout / ".env").read_bytes()
    try:
        if location == "source_child":
            (roots["agents"] / "linked").symlink_to(roots["files"], target_is_directory=True)
        else:
            destination.symlink_to(roots["files"], target_is_directory=True)
    except OSError:
        pytest.skip("Host does not permit symlink creation")
    with pytest.raises(ValueError, match="redirect"):
        invoke(installation)
    assert (checkout / ".env").read_bytes() == original


def test_destination_hardlink_cannot_alias_retained_source(
    installation: tuple[Path, dict[str, Path], Path],
) -> None:
    _, roots, destination = installation
    (destination / "files").mkdir(parents=True)
    (destination / "files/report.txt").hardlink_to(roots["files"] / "report.txt")
    with pytest.raises(ValueError, match="linked file"):
        invoke(installation)


def test_mutation_during_private_backup_still_blocks_environment_publication(
    installation: tuple[Path, dict[str, Path], Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    checkout, roots, _ = installation
    original = (checkout / ".env").read_bytes()
    write = migration._write_private

    def mutate(path: Path, content: bytes) -> None:
        write(path, content)
        if path.name == "receipt.json":
            (roots["files"] / "report.txt").write_bytes(b"late concurrent edit")

    monkeypatch.setattr(migration, "_write_private", mutate)
    with pytest.raises(ValueError, match="changed before publication"):
        invoke(installation)
    assert (checkout / ".env").read_bytes() == original


def test_private_acl_failure_never_writes_configuration_backup(
    installation: tuple[Path, dict[str, Path], Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    checkout, _, _ = installation
    original = (checkout / ".env").read_bytes()

    def reject(path: Path) -> None:
        path.mkdir(parents=True)
        raise OSError("ACL unavailable")

    monkeypatch.setattr(migration, "_private_directory", reject)
    with pytest.raises(OSError, match="ACL unavailable"):
        invoke(installation)
    assert not list((checkout / ".local/storage-migrations").glob("*/*.env"))
    assert (checkout / ".env").read_bytes() == original


def test_cli_never_prints_configuration_exception_contents(
    installation: tuple[Path, dict[str, Path], Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    checkout, _, destination = installation
    monkeypatch.setattr(command, "ROOT", checkout)
    monkeypatch.chdir(checkout)
    monkeypatch.setattr(sys, "argv", ["migrate_storage.py", str(destination), "--writers-stopped"])
    for key in migration.STORAGE_BINDINGS.values():
        monkeypatch.delenv(key, raising=False)

    def invalid_settings() -> None:
        raise ValueError("synthetic-secret-from-configuration")

    monkeypatch.setattr(command, "Settings", invalid_settings)
    assert command.main() == 2
    captured = capsys.readouterr()
    assert "synthetic-secret" not in captured.out + captured.err
    assert "Configuration was not replaced" in captured.out
    assert not destination.exists()
