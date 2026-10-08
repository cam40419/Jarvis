"""Validate launcher configuration without opening stores or exposing secret values."""

from __future__ import annotations

import argparse
from collections.abc import Sequence

from simon.config import Settings


def main(arguments: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("service", choices=("server",))
    args = parser.parse_args(arguments)
    try:
        settings = Settings()
        if settings.storage_backend != "postgres":
            raise ValueError("Persistent PostgreSQL configuration required")
        if settings.environment != "production":
            raise ValueError("Production settings required")
        print("Server configuration validated")
    except Exception:
        # Pydantic and provider errors can contain raw configuration values.
        print(f"Invalid {args.service} configuration; review settings and the startup runbook.")
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
