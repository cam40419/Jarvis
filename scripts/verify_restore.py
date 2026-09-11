"""Back up the quiescent Compose database and verify a restore into a temporary database.

Run with the API/workers stopped. The source database is read only throughout.
Binary dumps are written by Python, avoiding Windows PowerShell redirection corruption.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
COMPOSE = ROOT / "deploy" / "compose" / "compose.yaml"


def docker(*args: str, data: bytes | None = None) -> bytes:
    result = subprocess.run(
        ["docker", "compose", "-f", str(COMPOSE), "exec", "-T", "postgres", *args],
        input=data,
        capture_output=True,
        check=True,
    )
    return result.stdout


def query(database: str, sql: str) -> str:
    return (
        docker(
            "psql", "-X", "-U", "jarvis", "-d", database, "-v", "ON_ERROR_STOP=1", "-At", "-c", sql
        )
        .decode()
        .strip()
    )


def snapshot(database: str) -> dict[str, str]:
    tables = query(
        database, "SELECT tablename FROM pg_tables WHERE schemaname = 'public' ORDER BY tablename"
    ).splitlines()
    result = {}
    for table in tables:
        quoted = '"' + table.replace('"', '""') + '"'
        records = query(
            database,
            "SELECT coalesce(jsonb_agg(to_jsonb(t) ORDER BY "
            f"to_jsonb(t)::text), '[]'::jsonb)::text FROM public.{quoted} AS t",
        )
        result[table] = hashlib.sha256(records.encode()).hexdigest()
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", default="jarvis")
    parser.add_argument("--output-directory", type=Path, default=ROOT / ".local" / "backups")
    args = parser.parse_args()
    args.output_directory.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ") + "_" + uuid4().hex[:8]
    archive = args.output_directory / f"jarvis_{stamp}.dump"
    report = archive.with_suffix(".json")
    restored = "jarvis_restore_" + uuid4().hex
    before = snapshot(args.database)
    archive.write_bytes(
        docker(
            "pg_dump",
            "-U",
            "jarvis",
            "-d",
            args.database,
            "--format=custom",
            "--no-owner",
            "--no-privileges",
        )
    )
    docker("createdb", "-U", "jarvis", restored)
    try:
        docker(
            "pg_restore",
            "-U",
            "jarvis",
            "-d",
            restored,
            "--exit-on-error",
            "--no-owner",
            "--no-privileges",
            data=archive.read_bytes(),
        )
        after = snapshot(restored)
        if before != after or before != snapshot(args.database):
            raise RuntimeError("restore differs from source, or source changed during verification")
        result = {
            "verified_at": datetime.now(UTC).isoformat(),
            "source_database": args.database,
            "archive": archive.name,
            "archive_sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
            "tables_verified": len(before),
            "table_sha256": before,
            "result": "passed",
        }
        report.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
        print(f"Restore verified: {len(before)} tables match.\nBackup: {archive}\nReport: {report}")
    finally:
        # Only this generated temporary database is removed, never the source database.
        docker("dropdb", "-U", "jarvis", restored)


if __name__ == "__main__":
    main()
