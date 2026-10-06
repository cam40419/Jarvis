"""Inspect or explicitly archive one settled run's legacy outputs, without executing tools.

python -m simon.backfill_outputs --actor UUID --workspace UUID --project UUID --run UUID
Add --execute after reviewing the dry-run result; repeat with next_offset if present.
"""

import argparse
import json
from collections.abc import Sequence
from uuid import UUID

from simon.api.app import AppContainer
from simon.domain.errors import DomainError


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("actor", "workspace", "project", "run"):
        parser.add_argument("--" + name, type=UUID, required=True)
    parser.add_argument("--offset", type=int, default=0)
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args(argv)
    services = AppContainer()
    try:
        actor = services.agent_runs.actor_resolver(args.actor, args.workspace)
        services.project_outputs.authorize(actor, args.project, write=True)
        result = services.project_outputs.journal.backfill(
            actor,
            args.project,
            args.run,
            offset=args.offset,
            limit=args.limit,
            dry_run=not args.execute,
        )
        print(json.dumps(result))
        return 0
    except DomainError as error:
        print(json.dumps({"error": error.code, "message": str(error)}))
        return 1
    finally:
        close = getattr(services.store, "close", None)
        if close is not None:
            close()


if __name__ == "__main__":
    raise SystemExit(main())
