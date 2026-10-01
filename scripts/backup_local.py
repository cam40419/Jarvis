"""Make a validated live PostgreSQL archive, retaining the newest 14 automatic copies."""

from __future__ import annotations

import hashlib
import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
COMPOSE = ROOT / "deploy" / "compose" / "compose.yaml"
BACKUPS = ROOT / ".local" / "backups"


def command(*args: str) -> list[str]:
    return ["docker", "compose", "-f", str(COMPOSE), "exec", "-T", "postgres", *args]


def main() -> None:
    BACKUPS.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ") + "_" + uuid4().hex[:8]
    archive = BACKUPS / f"simon_auto_{stamp}.dump"
    partial = archive.with_suffix(".part")
    report = archive.with_suffix(".json")
    try:
        with partial.open("wb") as output:
            subprocess.run(
                command(
                    "pg_dump",
                    "-U",
                    "jarvis",
                    "-d",
                    "jarvis",
                    "--format=custom",
                    "--no-owner",
                    "--no-privileges",
                ),
                stdout=output,
                stderr=subprocess.PIPE,
                check=True,
            )
        if partial.stat().st_size == 0:
            raise RuntimeError("pg_dump produced an empty archive")
        with partial.open("rb") as source:
            subprocess.run(
                command("pg_restore", "--list"),
                stdin=source,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                check=True,
            )
        digest = hashlib.sha256()
        with partial.open("rb") as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(chunk)
        partial.replace(archive)
        report.write_text(
            json.dumps(
                {
                    "created_at": datetime.now(UTC).isoformat(),
                    "database": "jarvis",
                    "archive": archive.name,
                    "bytes": archive.stat().st_size,
                    "sha256": digest.hexdigest(),
                    "format_check": "passed",
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        print(f"Validated backup: {archive}")
        for old in sorted(BACKUPS.glob("simon_auto_*.dump"), reverse=True)[14:]:
            old.unlink()
            old.with_suffix(".json").unlink(missing_ok=True)
    finally:
        partial.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
