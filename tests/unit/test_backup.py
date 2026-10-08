import json
from pathlib import Path

import pytest

from simon.services.backup import create_bundle, restore_files, verify_bundle


def sources(tmp_path: Path) -> dict[str, Path]:
    roots = {"files": tmp_path / "files"}
    for root in roots.values():
        root.mkdir()
    (roots["files"] / "empty").mkdir()
    (roots["files"] / "report.txt").write_text("local workspace", encoding="utf-8")
    (roots["files"] / "output.bin").write_bytes(bytes(range(256)))
    return roots


def dump(path: Path) -> None:
    path.write_bytes(b"synthetic database archive")


def test_bundle_roundtrip_and_existing_destination(tmp_path: Path) -> None:
    roots = sources(tmp_path)
    bundle = create_bundle(
        tmp_path / "backup",
        roots=roots,
        dump_database=dump,
        database_snapshot=lambda: {"jobs": "hash"},
    )
    manifest = verify_bundle(bundle)
    assert manifest.database_tables == {"jobs": "hash"}
    assert not manifest.includes_secrets
    destination = tmp_path / "recovered"
    restore_files(bundle, destination)
    assert (destination / "files/empty").is_dir()
    assert (destination / "files/output.bin").read_bytes() == bytes(range(256))
    assert verify_bundle(destination) == manifest
    with pytest.raises(ValueError, match="new directory"):
        restore_files(bundle, destination)
    assert (roots["files"] / "report.txt").read_text() == "local workspace"


@pytest.mark.parametrize("change", ["modified", "missing", "extra"])
def test_corrupt_bundle_never_restores(tmp_path: Path, change: str) -> None:
    bundle = create_bundle(
        tmp_path / "backup",
        roots=sources(tmp_path),
        dump_database=dump,
        database_snapshot=lambda: {},
    )
    file = bundle / "files/report.txt"
    if change == "modified":
        file.write_bytes(b"corrupt")
    elif change == "missing":
        file.unlink()
    else:
        (bundle / "unexpected").write_bytes(b"extra")
    with pytest.raises(ValueError, match="differ"):
        restore_files(bundle, tmp_path / "restored")
    assert not (tmp_path / "restored").exists()


def test_source_mutation_and_database_mutation_block_publication(tmp_path: Path) -> None:
    roots = sources(tmp_path)

    def mutating_dump(path: Path) -> None:
        dump(path)
        (roots["files"] / "report.txt").write_bytes(b"concurrent edit")

    with pytest.raises(ValueError, match="changed"):
        create_bundle(
            tmp_path / "backup",
            roots=roots,
            dump_database=mutating_dump,
            database_snapshot=lambda: {},
        )
    assert not (tmp_path / "backup").exists()
    snapshots = iter([{"jobs": "old"}, {"jobs": "new"}])
    with pytest.raises(ValueError, match="Database changed"):
        create_bundle(
            tmp_path / "backup2",
            roots=roots,
            dump_database=dump,
            database_snapshot=lambda: next(snapshots),
        )
    assert not (tmp_path / "backup2").exists()


def test_missing_and_overlapping_roots_fail(tmp_path: Path) -> None:
    roots = sources(tmp_path)
    with pytest.raises(ValueError, match="overlaps"):
        create_bundle(
            roots["files"] / "backup", roots=roots, dump_database=dump, database_snapshot=lambda: {}
        )
    roots["files"] = tmp_path / "missing"
    with pytest.raises(ValueError, match="missing"):
        create_bundle(
            tmp_path / "backup", roots=roots, dump_database=dump, database_snapshot=lambda: {}
        )


def test_configuration_and_secret_opt_in(tmp_path: Path) -> None:
    roots = sources(tmp_path)
    env = tmp_path / "secret.env"
    env.write_text("SYNTHETIC_SECRET=example", encoding="utf-8")
    key = tmp_path / "credentials.key"
    key.write_text("synthetic encryption key", encoding="utf-8")
    for configuration in ({"server.env": env}, {"credentials.key": key}):
        with pytest.raises(ValueError, match="explicit"):
            create_bundle(
                tmp_path / "rejected",
                roots=roots,
                dump_database=dump,
                database_snapshot=lambda: {},
                configuration=configuration,
            )
    bundle = create_bundle(
        tmp_path / "backup",
        roots=roots,
        dump_database=dump,
        database_snapshot=lambda: {},
        configuration={"server.env": env, "credentials.key": key},
        includes_secrets=True,
    )
    assert verify_bundle(bundle).includes_secrets
    assert (bundle / "configuration/server.env").read_bytes() == env.read_bytes()
    assert (bundle / "configuration/credentials.key").read_bytes() == key.read_bytes()


@pytest.mark.parametrize("name", ["agent-platform.json", "project-boards.json"])
def test_retired_configuration_is_rejected(tmp_path: Path, name: str) -> None:
    config = tmp_path / name
    config.write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError, match="Unsupported configuration"):
        create_bundle(
            tmp_path / "backup",
            roots=sources(tmp_path),
            dump_database=dump,
            database_snapshot=lambda: {},
            configuration={name: config},
        )


def test_symlink_is_rejected(tmp_path: Path) -> None:
    roots = sources(tmp_path)
    try:
        (roots["files"] / "redirect").symlink_to(roots["files"], target_is_directory=True)
    except OSError:
        pytest.skip("Host does not permit symlink creation")
    with pytest.raises(ValueError, match="redirects"):
        create_bundle(
            tmp_path / "backup", roots=roots, dump_database=dump, database_snapshot=lambda: {}
        )


@pytest.mark.parametrize("interrupted", [False, True])
def test_incomplete_database_dump_never_publishes_and_preserves_partial_bytes(
    tmp_path, interrupted
):
    roots = sources(tmp_path)

    def incomplete(path):
        path.write_bytes(b"partial archive" if interrupted else b"")
        if interrupted:
            raise OSError("Synthetic interrupted dump")

    with pytest.raises(OSError if interrupted else ValueError, match=r"interrupted|Empty"):
        create_bundle(
            tmp_path / "backup", roots=roots, dump_database=incomplete, database_snapshot=lambda: {}
        )
    assert not (tmp_path / "backup").exists()
    partial = list(tmp_path.glob(".backup.*.partial"))
    assert len(partial) == 1
    assert (partial[0] / "database.dump").read_bytes() == (
        b"partial archive" if interrupted else b""
    )
    assert (roots["files"] / "report.txt").read_text() == "local workspace"


def test_self_consistent_bundle_without_database_is_not_a_recovery_bundle(tmp_path):
    bundle = create_bundle(
        tmp_path / "backup",
        roots=sources(tmp_path),
        dump_database=dump,
        database_snapshot=lambda: {},
    )
    (bundle / "database.dump").unlink()
    manifest = json.loads((bundle / "manifest.json").read_text(encoding="utf-8"))
    del manifest["files"]["database.dump"]
    (bundle / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ValueError, match="no database archive"):
        restore_files(bundle, tmp_path / "restored")
    assert not (tmp_path / "restored").exists()


@pytest.mark.parametrize("invalid", ["existing_destination", "missing_root", "directory_config"])
def test_invalid_backup_inputs_do_not_start_a_database_dump(tmp_path, invalid):
    roots = sources(tmp_path)
    destination = tmp_path / "backup"
    configuration = {}
    if invalid == "existing_destination":
        destination.mkdir()
        (destination / "precious.txt").write_text("keep", encoding="utf-8")
    elif invalid == "missing_root":
        roots = {}
    else:
        configuration = {"external-providers.json": roots["files"]}
    with pytest.raises(ValueError, match=r"already exists|files root|regular file"):
        create_bundle(
            destination,
            roots=roots,
            configuration=configuration,
            dump_database=lambda _: pytest.fail("Rejected input must not start a dump"),
            database_snapshot=lambda: {},
        )
    if destination.exists():
        assert (destination / "precious.txt").read_text() == "keep"
    assert not list(tmp_path.glob(".backup.*.partial"))


@pytest.mark.parametrize("change", ["configuration_during_dump", "file_after_copy"])
def test_source_changes_at_different_backup_stages_prevent_publication(tmp_path, change):
    roots = sources(tmp_path)
    configuration = tmp_path / "external-providers.json"
    configuration.write_text("{}", encoding="utf-8")
    snapshots = []

    def dumping(path):
        dump(path)
        if change == "configuration_during_dump":
            configuration.write_text('{"changed":true}', encoding="utf-8")

    def snapshot():
        snapshots.append(True)
        if change == "file_after_copy" and len(snapshots) == 2:
            (roots["files"] / "report.txt").write_text("new version", encoding="utf-8")
        return {"jobs": "unchanged"}

    with pytest.raises(ValueError, match=r"Configuration changed|Source changed"):
        create_bundle(
            tmp_path / "backup",
            roots=roots,
            configuration={"external-providers.json": configuration},
            dump_database=dumping,
            database_snapshot=snapshot,
        )
    assert not (tmp_path / "backup").exists()
    assert len(list(tmp_path.glob(".backup.*.partial"))) == 1


@pytest.mark.parametrize("operation", ["create", "restore"])
def test_destination_created_during_io_is_never_overwritten(tmp_path, monkeypatch, operation):
    from simon.services import backup

    roots = sources(tmp_path)
    destination = tmp_path / "published"

    def claim_destination():
        destination.mkdir(exist_ok=True)
        (destination / "precious.txt").write_text("another operation", encoding="utf-8")

    if operation == "create":

        def dump_and_claim(path):
            dump(path)
            claim_destination()

        with pytest.raises(ValueError, match="destination appeared"):
            create_bundle(
                destination, roots=roots, dump_database=dump_and_claim, database_snapshot=lambda: {}
            )
    else:
        bundle = create_bundle(
            tmp_path / "backup", roots=roots, dump_database=dump, database_snapshot=lambda: {}
        )
        copyfile = backup.shutil.copyfile

        def copy_and_claim(source, target):
            result = copyfile(source, target)
            claim_destination()
            return result

        monkeypatch.setattr(backup.shutil, "copyfile", copy_and_claim)
        with pytest.raises(ValueError, match="destination appeared"):
            restore_files(bundle, destination)
        verify_bundle(bundle)
    assert list(destination.iterdir()) == [destination / "precious.txt"]
    assert (destination / "precious.txt").read_text() == "another operation"
    assert len(list(tmp_path.glob(".published.*.partial"))) == 1
