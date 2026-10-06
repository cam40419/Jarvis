"""Copy stopped installation storage, verify it, then atomically change its bindings.

This is an operator maintenance operation, not a live snapshot. Original directories
are deliberately retained: older environment leases can contain absolute paths there.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from dotenv.parser import parse_stream

from simon.services.backup import FileDigest, checked_path, inventory
from simon.services.safe_files import open_regular_nofollow

STORAGE_BINDINGS = {"files": "SIMON_LOCAL_FILES_DIR", "agents": "SIMON_AGENT_STATE_DIR"}


@dataclass(frozen=True)
class StorageMigrationResult:
    configuration_updated: bool
    configuration_backup: str
    receipt_path: str
    data_root: str
    source_roots: dict[str, str]
    destination_roots: dict[str, str]
    file_counts: dict[str, int]

    def public_dict(self) -> dict[str, object]:
        """Paths and counts only; never configuration contents."""
        return asdict(self)


def _path(path: Path) -> Path:
    if any(character in str(path) for character in ("\x00", "\r", "\n", "${")):
        raise ValueError("Storage paths contain unsupported characters")
    if os.name == "nt":
        if path.drive.startswith("\\\\"):
            raise ValueError("Storage migration requires a local drive")
        if any(_reserved_windows_component(part) for part in path.parts[1:]):
            raise ValueError("Storage paths contain ambiguous Windows components")
    # Reject every existing redirect before resolving dot components/aliases.
    return checked_path(checked_path(path).resolve())


def _reserved_windows_component(part: str) -> bool:
    is_reserved = getattr(os.path, "isreserved", None)
    reserved = bool(is_reserved(part)) if is_reserved is not None else Path(part).is_reserved()
    return part.endswith((" ", ".")) or ":" in part or reserved


def _overlaps(first: Path, second: Path) -> bool:
    return first.is_relative_to(second) or second.is_relative_to(first)


def _read(path: Path) -> bytes:
    with open_regular_nofollow(checked_path(path)) as source:
        return source.read()


def storage_environment(original: bytes, targets: dict[str, Path]) -> bytes:
    """Keep unrelated bindings byte-for-byte, including multiline quoted secrets."""
    bom = original.startswith(b"\xef\xbb\xbf")
    text = original.decode("utf-8-sig")
    newline = "\r\n" if "\r\n" in text else "\n"
    values = {STORAGE_BINDINGS[name]: _path(path).as_posix() for name, path in targets.items()}
    seen: set[str] = set()
    output: list[str] = []
    for binding in parse_stream(io.StringIO(text)):
        if binding.error:
            raise ValueError("Configuration has an invalid environment binding")
        if binding.key not in values:
            output.append(binding.original.string)
            continue
        # parse_stream includes preceding blank lines in each binding.
        prefix = re.match(r"\s*", binding.original.string)
        output.append(prefix.group() if prefix else "")
        if binding.key not in seen:
            output.append(
                f"{binding.key}={json.dumps(values[binding.key], ensure_ascii=False)}{newline}"
            )
            seen.add(binding.key)
    rendered = "".join(output)
    for key, value in values.items():
        if key not in seen:
            if rendered and not rendered.endswith(("\r", "\n")):
                rendered += newline
            rendered += f"{key}={json.dumps(value, ensure_ascii=False)}{newline}"
    return (b"\xef\xbb\xbf" if bom else b"") + rendered.encode("utf-8")


def _private_directory(path: Path) -> None:
    """Remove inherited Windows grants before writing the first secret byte."""
    checked_path(path.parent).mkdir(parents=True, exist_ok=True)
    checked_path(path).mkdir(mode=0o700)
    if os.name != "nt":
        path.chmod(0o700)
        return
    system = Path(os.environ.get("SYSTEMROOT", r"C:\Windows")) / "System32"
    identity = subprocess.run(
        [str(system / "whoami.exe"), "/user", "/fo", "csv", "/nh"],
        capture_output=True,
        check=True,
        timeout=15,
    )
    row = next(csv.reader(io.StringIO(identity.stdout.decode(errors="replace").strip())))
    sid = row[-1]
    if re.fullmatch(r"S-\d+(?:-\d+)+", sid) is None:
        raise ValueError("Could not determine the private configuration owner")
    subprocess.run(
        [str(system / "icacls.exe"), str(path), "/inheritance:r", "/grant:r", f"*{sid}:(OI)(CI)F"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        check=True,
        timeout=15,
    )


def _write_private(path: Path, content: bytes) -> None:
    # The parent already has owner-only ACLs; opening first and chmod later is
    # insufficient on Windows and could briefly expose an environment backup.
    with checked_path(path).open("xb") as destination:
        if os.name != "nt":
            path.chmod(0o600)
        destination.write(content)
        destination.flush()
        os.fsync(destination.fileno())


def _compatible_destination(
    target: Path, expected: tuple[dict[str, FileDigest], list[str]]
) -> None:
    if not target.exists():
        return
    files, directories = inventory(target)
    expected_files, expected_directories = expected
    if not set(directories).issubset(expected_directories) or any(
        name not in expected_files or value != expected_files[name] for name, value in files.items()
    ):
        raise ValueError("Destination contains conflicting or unexpected data")
    if any((target / name).stat().st_nlink != 1 for name in files):
        raise ValueError("Destination contains a linked file")


def _copy_file(source: Path, destination: Path) -> None:
    """Copy a missing file only, never overwrite an existing destination."""
    checked_path(destination.parent)
    with open_regular_nofollow(checked_path(source)) as original, destination.open("xb") as copied:
        shutil.copyfileobj(original, copied, length=1024 * 1024)
        copied.flush()
        os.fsync(copied.fileno())


def migrate_storage(
    data_root: Path,
    *,
    checkout: Path,
    roots: dict[str, Path],
    writers_stopped: bool,
) -> StorageMigrationResult:
    """Retain sources; do not publish .env until both copies and sources verify.

    Interrupted attempts may leave copies at the destination. A subsequent run
    accepts only matching subsets, so it cannot overwrite conflicting user data.
    The last operation is atomic configuration publication; earlier errors leave
    the original configuration intact, allowing the caller to restart old services.
    """
    if not writers_stopped:
        raise ValueError("All writers must be stopped before migration")
    if not data_root.is_absolute():
        raise ValueError("Storage root must be an absolute path")
    if set(roots) != set(STORAGE_BINDINGS):
        raise ValueError("Both files and agents source roots are required")
    checkout, data_root = _path(checkout), _path(data_root)
    roots = {
        name: _path(path if path.is_absolute() else checkout / path) for name, path in roots.items()
    }
    targets = {name: _path(data_root / name) for name in STORAGE_BINDINGS}
    if data_root == Path(data_root.anchor) or _overlaps(checkout, data_root):
        raise ValueError("Storage root must be outside the checkout and not a filesystem root")
    if _overlaps(roots["files"], roots["agents"]):
        raise ValueError("Storage source roots overlap")
    for name, source in roots.items():
        if source != targets[name] and _overlaps(source, data_root):
            raise ValueError("Storage destination overlaps a source")
        if source == Path(source.anchor) or checkout.is_relative_to(source):
            raise ValueError("Storage source is too broad")
    environment = _path(checkout / ".env")
    original = _read(environment)
    changed = storage_environment(original, targets)
    before = {name: inventory(path) for name, path in roots.items()}
    for name, target in targets.items():
        _compatible_destination(target, before[name])
    # A dedicated existing parent is okay; unrelated root-level content is not.
    if data_root.exists() and any(child.name not in targets for child in data_root.iterdir()):
        raise ValueError("Storage root contains unexpected entries")
    for name, target in targets.items():
        target.mkdir(parents=True, exist_ok=True)
        files, directories = before[name]
        for directory in directories:
            checked_path(target / directory).mkdir(parents=True, exist_ok=True)
        for relative in files:
            destination = checked_path(target / relative)
            if not destination.exists():
                _copy_file(roots[name] / relative, destination)
    if any(inventory(path) != before[name] for name, path in roots.items()):
        raise ValueError("Source changed during migration; configuration was not changed")
    if any(inventory(path) != before[name] for name, path in targets.items()):
        raise ValueError("Destination verification failed; configuration was not changed")
    if _read(environment) != original:
        raise ValueError("Configuration changed during migration")
    backup = _path(checkout / ".local/storage-migrations" / uuid4().hex)
    _private_directory(backup)
    backup_env = backup / "prechange.env"
    _write_private(backup_env, original)
    staged_env = backup / "replacement.env"
    _write_private(staged_env, changed)
    result = StorageMigrationResult(
        configuration_updated=changed != original,
        configuration_backup=str(backup_env),
        receipt_path=str(backup / "receipt.json"),
        data_root=str(data_root),
        source_roots={name: str(path) for name, path in roots.items()},
        destination_roots={name: str(path) for name, path in targets.items()},
        file_counts={name: len(snapshot[0]) for name, snapshot in before.items()},
    )
    receipt = {
        **result.public_dict(),
        "created_at": datetime.now(UTC).isoformat(),
        "consistency": "operator-stopped-writers",
        "original_environment_sha256": hashlib.sha256(original).hexdigest(),
        "replacement_environment_sha256": hashlib.sha256(changed).hexdigest(),
        "inventories": {
            name: {
                "files": {path: digest.model_dump() for path, digest in snapshot[0].items()},
                "directories": snapshot[1],
            }
            for name, snapshot in before.items()
        },
    }
    _write_private(backup / "receipt.json", json.dumps(receipt, indent=2).encode("utf-8"))
    # Recheck after private backup creation as well; do no fallible work after
    # replace. Even a failed replacement leaves the complete original untouched.
    if _read(environment) != original or any(
        inventory(path) != before[name] for name, path in roots.items()
    ):
        raise ValueError("Source or configuration changed before publication")
    if any(inventory(path) != before[name] for name, path in targets.items()):
        raise ValueError("Destination changed before publication")
    if result.configuration_updated:
        os.replace(checked_path(staged_env), checked_path(environment))
    return result
