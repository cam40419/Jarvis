"""Create, verify, or stage recovery of a local Compose installation.

Run from the repository root. Creation requires all API/worker/external file
writers stopped. This command never stops services or overwrites the live database.
"""

from __future__ import annotations

import argparse
import subprocess
from pathlib import Path
from uuid import uuid4

from verify_restore import docker, snapshot

from simon.config import Settings
from simon.services.backup import create_bundle, restore_files, verify_bundle

ROOT = Path(__file__).resolve().parents[1]


def command(*args: str) -> list[str]:
    return [
        "docker",
        "compose",
        "-f",
        str(ROOT / "deploy/compose/compose.yaml"),
        "exec",
        "-T",
        "postgres",
        *args,
    ]


def dump_database(database: str, target: Path) -> None:
    with target.open("xb") as output:
        subprocess.run(
            command(
                "pg_dump",
                "-U",
                "jarvis",
                "-d",
                database,
                "--format=custom",
                "--no-owner",
                "--no-privileges",
            ),
            stdout=output,
            stderr=subprocess.PIPE,
            check=True,
        )
    with target.open("rb") as source:
        subprocess.run(
            command("pg_restore", "--list"),
            stdin=source,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            check=True,
        )


def verify_database(bundle: Path) -> None:
    manifest = verify_bundle(bundle)
    temporary = "simon_restore_" + uuid4().hex
    docker("createdb", "-U", "jarvis", temporary)
    try:
        with (bundle / "database.dump").open("rb") as source:
            subprocess.run(
                command(
                    "pg_restore",
                    "-U",
                    "jarvis",
                    "-d",
                    temporary,
                    "--exit-on-error",
                    "--no-owner",
                    "--no-privileges",
                ),
                stdin=source,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                check=True,
            )
        if snapshot(temporary) != manifest.database_tables:
            raise ValueError("Restored database differs from the recorded table hashes")
        print(f"Database restore verified: {len(manifest.database_tables)} tables match")
    finally:
        # This generated database is the only database this script removes.
        docker("dropdb", "-U", "jarvis", temporary)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="action", required=True)
    create = commands.add_parser("create")
    create.add_argument("destination", type=Path)
    create.add_argument("--writers-stopped", action="store_true", required=True)
    create.add_argument("--database", default="jarvis", help="Compose database name")
    create.add_argument(
        "--include-secrets",
        action="store_true",
        help="Include .env in this PRIVATE, UNENCRYPTED bundle",
    )
    verify = commands.add_parser("verify")
    verify.add_argument("bundle", type=Path)
    verify.add_argument("--database", action="store_true", help="Also perform a database restore")
    restore = commands.add_parser("restore-files")
    restore.add_argument("bundle", type=Path)
    restore.add_argument("destination", type=Path)
    args = parser.parse_args()
    if args.action == "create":
        if Path.cwd().resolve() != ROOT:
            parser.error("Run creation from the repository root to resolve configuration paths")
        settings = Settings()
        configuration = {}
        if settings.external_providers_file is not None:
            configuration["external-providers.json"] = settings.external_providers_file
        if settings.model_catalog_file is not None:
            configuration["model-catalog.json"] = settings.model_catalog_file
        if args.include_secrets:
            configuration["server.env"] = ROOT / ".env"
            if settings.integration_key_file.is_file():
                configuration["credentials.key"] = settings.integration_key_file
        result = create_bundle(
            args.destination,
            roots={"files": settings.local_files_dir},
            configuration=configuration,
            includes_secrets=args.include_secrets,
            dump_database=lambda target: dump_database(args.database, target),
            database_snapshot=lambda: snapshot(args.database),
        )
        print(f"Recovery bundle created and checksum-verified: {result}")
        print("Run verify --database to prove database restoration. Bundle is not encrypted.")
    elif args.action == "verify":
        manifest = verify_bundle(args.bundle)
        if args.database:
            verify_database(args.bundle)
        print(f"Bundle verified: {len(manifest.files)} files; no live data changed")
    else:
        restore_files(args.bundle, args.destination)
        print(f"Files staged: {args.destination}; live database and configuration unchanged")


if __name__ == "__main__":
    main()
