from pathlib import Path

import pytest

from simon.services.backup import create_bundle, restore_files, verify_bundle


def sources(tmp_path: Path) -> dict[str, Path]:
    roots = {name: tmp_path / name for name in ("files", "agents")}
    for root in roots.values():
        root.mkdir()
    (roots["files"] / "empty").mkdir()
    (roots["files"] / "report.txt").write_text("local project", encoding="utf-8")
    (roots["agents"] / "output.bin").write_bytes(bytes(range(256)))
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
    assert (destination / "agents/output.bin").read_bytes() == bytes(range(256))
    assert verify_bundle(destination) == manifest
    with pytest.raises(ValueError, match="new directory"):
        restore_files(bundle, destination)
    assert (roots["files"] / "report.txt").read_text() == "local project"


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
    roots["agents"] = tmp_path / "missing"
    with pytest.raises(ValueError, match="missing"):
        create_bundle(
            tmp_path / "backup", roots=roots, dump_database=dump, database_snapshot=lambda: {}
        )


def test_configuration_and_secret_opt_in(tmp_path: Path) -> None:
    roots = sources(tmp_path)
    env = tmp_path / "secret.env"
    env.write_text("SYNTHETIC_SECRET=example", encoding="utf-8")
    with pytest.raises(ValueError, match="explicit"):
        create_bundle(
            tmp_path / "rejected",
            roots=roots,
            dump_database=dump,
            database_snapshot=lambda: {},
            configuration={"server.env": env},
        )
    bundle = create_bundle(
        tmp_path / "backup",
        roots=roots,
        dump_database=dump,
        database_snapshot=lambda: {},
        configuration={"server.env": env},
        includes_secrets=True,
    )
    assert verify_bundle(bundle).includes_secrets
    assert (bundle / "configuration/server.env").read_bytes() == env.read_bytes()


def test_symlink_is_rejected(tmp_path: Path) -> None:
    roots = sources(tmp_path)
    try:
        (roots["agents"] / "redirect").symlink_to(roots["files"], target_is_directory=True)
    except OSError:
        pytest.skip("Host does not permit symlink creation")
    with pytest.raises(ValueError, match="redirects"):
        create_bundle(
            tmp_path / "backup", roots=roots, dump_database=dump, database_snapshot=lambda: {}
        )
