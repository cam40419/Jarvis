"""Copy verified files/agent state to a permanent data root while all writers are stopped."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from simon.config import Settings
from simon.services.storage_migration import STORAGE_BINDINGS, migrate_storage

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("data_root", type=Path)
    parser.add_argument("--writers-stopped", action="store_true", required=True)
    args = parser.parse_args()
    try:
        if Path.cwd().resolve() != ROOT:
            raise ValueError("Run the migration from the checkout root")
        if any(key.upper() in STORAGE_BINDINGS.values() for key in os.environ):
            raise ValueError("Storage environment overrides must be removed before migration")
        settings = Settings()
        result = migrate_storage(
            args.data_root,
            checkout=ROOT,
            roots={"files": settings.local_files_dir, "agents": settings.agent_state_dir},
            writers_stopped=args.writers_stopped,
        )
    except Exception:
        # Config/model-validation exceptions can include credential values. Never
        # print their text, traceback, environment contents, or process output.
        print(
            "Storage migration refused or failed. Configuration was not replaced; inspect paths "
            "and keep writers stopped before retrying. Original source directories are retained."
        )
        return 2
    print(json.dumps(result.public_dict()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
