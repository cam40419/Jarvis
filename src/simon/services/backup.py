"""Portable, checksum-verified recovery bundles for a stopped Simon installation.

The operator must stop all writers. Hash comparisons detect many accidental writes,
but are not a filesystem snapshot or a substitute for that maintenance window.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field


class FileDigest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    size: int = Field(ge=0)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class BundleManifest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: int = Field(default=1, ge=1, le=1)
    created_at: str
    consistency: str = "operator-stopped-writers"
    files: dict[str, FileDigest]
    directories: list[str]
    source_roots: dict[str, str]
    database_tables: dict[str, str]
    includes_secrets: bool


def checked_path(path: Path) -> Path:
    """Reject redirects in existing ancestors as well as the final path."""
    path = Path(os.path.abspath(path.expanduser()))
    for component in (*reversed(path.parents), path):
        try:
            info = component.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or (
            getattr(info, "st_file_attributes", 0)
            & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
        ):
            raise ValueError(f"Filesystem redirects are unsupported: {component}")
    return path


def file_digest(path: Path) -> FileDigest:
    checked_path(path)
    if not stat.S_ISREG(path.stat().st_mode):
        raise ValueError(f"Expected a regular file: {path}")
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            size += len(chunk)
            digest.update(chunk)
    return FileDigest(size=size, sha256=digest.hexdigest())


def inventory(root: Path) -> tuple[dict[str, FileDigest], list[str]]:
    root = checked_path(root)
    if not root.is_dir():
        raise ValueError(f"Required source directory is missing: {root}")
    files: dict[str, FileDigest] = {}
    directories: list[str] = []

    def failed(error: OSError) -> None:
        raise error

    for parent, folders, names in os.walk(root, onerror=failed, followlinks=False):
        for name in folders:
            path = checked_path(Path(parent) / name)
            directories.append(path.relative_to(root).as_posix())
        for name in names:
            path = checked_path(Path(parent) / name)
            files[path.relative_to(root).as_posix()] = file_digest(path)
    return files, sorted(directories)


def verify_bundle(bundle: Path) -> BundleManifest:
    bundle = checked_path(bundle)
    manifest_path = checked_path(bundle / "manifest.json")
    if manifest_path.stat().st_size > 64 * 1024 * 1024:
        raise ValueError("Backup manifest exceeds 64 MiB")
    manifest = BundleManifest.model_validate_json(manifest_path.read_bytes())
    files, directories = inventory(bundle)
    files.pop("manifest.json", None)
    if files != manifest.files or directories != manifest.directories:
        raise ValueError("Backup contents differ from the manifest")
    if "database.dump" not in files:
        raise ValueError("Backup has no database archive")
    return manifest


def create_bundle(
    destination: Path,
    *,
    roots: dict[str, Path],
    dump_database: Callable[[Path], None],
    database_snapshot: Callable[[], dict[str, str]],
    configuration: dict[str, Path] | None = None,
    includes_secrets: bool = False,
) -> Path:
    """Publish only after database and source-file inventories remain unchanged.

    Callbacks must target the same database; dump_database must format-check its dump.
    Failed attempts stay under a unique .partial directory for operator inspection.
    """
    destination = checked_path(destination)
    if destination.exists():
        raise ValueError("Backup destination already exists")
    roots = {name: checked_path(path) for name, path in roots.items()}
    configuration = {name: checked_path(path) for name, path in (configuration or {}).items()}
    if set(roots) != {"files"}:
        raise ValueError("Exactly the files root is required")
    if not set(configuration).issubset(
        {
            "external-providers.json",
            "intake-models.json",
            "server.env",
            "credentials.key",
        }
    ):
        raise ValueError("Unsupported configuration entry")
    if ("server.env" in configuration) != includes_secrets or (
        "credentials.key" in configuration and not includes_secrets
    ):
        raise ValueError("Secret inclusion must be explicit")
    for source in [*roots.values(), *configuration.values()]:
        if destination.is_relative_to(source) or source.is_relative_to(destination):
            raise ValueError("Backup destination overlaps a source")
    before = {name: inventory(path) for name, path in roots.items()}
    config_before = {name: file_digest(path) for name, path in configuration.items()}
    tables = database_snapshot()
    destination.parent.mkdir(parents=True, exist_ok=True)
    staging = destination.with_name(f".{destination.name}.{uuid4().hex}.partial")
    staging.mkdir(mode=0o700)
    dump_database(staging / "database.dump")
    if not (staging / "database.dump").stat().st_size:
        raise ValueError("Empty database archive")
    for name, source in roots.items():
        files, directories = before[name]
        target = staging / name
        target.mkdir()
        for directory in directories:
            (target / directory).mkdir(parents=True, exist_ok=True)
        for relative in files:
            shutil.copyfile(checked_path(source / relative), target / relative)
        if inventory(target) != before[name] or inventory(source) != before[name]:
            raise ValueError("Files changed during backup; stop all writers and retry")
    if configuration:
        (staging / "configuration").mkdir()
        for name, source in configuration.items():
            target = staging / "configuration" / name
            shutil.copyfile(checked_path(source), target)
            if file_digest(target) != config_before[name]:
                raise ValueError("Configuration changed during backup")
    if database_snapshot() != tables:
        raise ValueError("Database changed during backup; stop all writers and retry")
    # Recheck every source at the end, including roots copied earlier.
    if any(inventory(path) != before[name] for name, path in roots.items()) or any(
        file_digest(path) != config_before[name] for name, path in configuration.items()
    ):
        raise ValueError("Source changed during backup")
    files, directories = inventory(staging)
    manifest = BundleManifest(
        created_at=datetime.now(UTC).isoformat(),
        files=files,
        directories=directories,
        source_roots={name: str(path) for name, path in roots.items()},
        database_tables=tables,
        includes_secrets=includes_secrets,
    )
    (staging / "manifest.json").write_text(
        json.dumps(manifest.model_dump(), indent=2) + "\n",
        encoding="utf-8",
    )
    verify_bundle(staging)
    if destination.exists():
        raise ValueError("Backup destination appeared during publication")
    staging.rename(destination)
    return destination


def restore_files(bundle: Path, destination: Path) -> None:
    """Stage a verified bundle into a NEW directory; never overwrite live data.

    Includes database.dump for a separate, explicit database recovery. Restoring
    bytes does not start services or recreate Docker containers from lease records.
    """
    manifest = verify_bundle(bundle)
    bundle = checked_path(bundle)
    destination = checked_path(destination)
    if (
        destination.exists()
        or destination.is_relative_to(bundle)
        or bundle.is_relative_to(destination)
    ):
        raise ValueError("Restore requires a new directory outside the backup")
    destination.parent.mkdir(parents=True, exist_ok=True)
    staging = destination.with_name(f".{destination.name}.{uuid4().hex}.partial")
    staging.mkdir(mode=0o700)
    # Enumerate verified disk entries instead of treating manifest paths as authority.
    files, directories = inventory(bundle)
    for directory in directories:
        (staging / directory).mkdir(parents=True, exist_ok=True)
    for relative in files:
        shutil.copyfile(checked_path(bundle / relative), staging / relative)
    if verify_bundle(staging) != manifest:
        raise ValueError("Backup changed during restore")
    if destination.exists():
        raise ValueError("Restore destination appeared during publication")
    staging.rename(destination)
