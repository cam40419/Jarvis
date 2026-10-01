"""Wait for the configured database before migrating and starting a local worker."""

from __future__ import annotations

import argparse
import logging
import time
from collections.abc import Sequence
from logging.handlers import RotatingFileHandler
from pathlib import Path

import psycopg

from simon.config import Settings
from simon.migrate import migrate

logger = logging.getLogger(__name__)
STOPPED = 3


def prepare(
    database_url: str,
    *,
    timeout: float = 300,
    poll_seconds: float = 2,
    stop_file: Path | None = None,
    maintenance_file: Path | None = None,
) -> int:
    """Retry connection startup, never replay work or conceal a migration failure."""
    deadline = time.monotonic() + timeout
    waiting = False

    def stopped() -> bool:
        return any(path is not None and path.exists() for path in (stop_file, maintenance_file))

    while True:
        if stopped():
            logger.info("Worker startup stopped by operator request")
            return STOPPED
        try:
            with psycopg.connect(database_url, connect_timeout=2) as connection:
                connection.execute("SET statement_timeout = '2s'")
                connection.execute("SELECT 1")
            break
        except psycopg.OperationalError as error:
            # Missing SQLSTATE usually means the server/socket is not yet reachable.
            # Authentication and other configuration failures require operator action.
            state = error.sqlstate
            if state is not None and not (state.startswith("08") or state in {"57P03", "53300"}):
                logger.error("Database readiness rejected (%s); check configuration", state)
                return 1
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                logger.error("Database readiness timed out; recovery will retry startup")
                return 1
            if not waiting:
                logger.info("Waiting for the configured PostgreSQL database")
                waiting = True
            time.sleep(min(poll_seconds, remaining))
        except Exception as error:
            logger.error("Database readiness failed (%s)", type(error).__name__)
            return 1
    if stopped():
        logger.info("Worker startup stopped before migrations")
        return STOPPED
    try:
        applied = migrate(database_url, lock_timeout_ms=5000, statement_timeout_ms=30000)
    except Exception as error:
        # Errors may contain credentials or SQL. Log only the phase and error class.
        logger.error(
            "Worker migration failed (%s); inspect migration compatibility", type(error).__name__
        )
        if stopped():
            logger.info("Worker startup stopped during migrations")
            return STOPPED
        return 1
    if stopped():
        logger.info("Worker startup stopped after migrations")
        return STOPPED
    logger.info("Database ready; migrations complete (%d applied)", len(applied))
    return 0


def main(arguments: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stop-file", type=Path)
    parser.add_argument("--maintenance-file", type=Path)
    parser.add_argument("--log-file", type=Path)
    parser.add_argument("--timeout", type=float, default=300)
    parser.add_argument("--poll-seconds", type=float, default=2)
    args = parser.parse_args(arguments)
    if not 0 < args.timeout <= 600 or not 0 < args.poll_seconds <= 10:
        parser.error("timeout must be between 0 and 600; poll-seconds between 0 and 10")
    logging.basicConfig(level=logging.INFO)
    handler = None
    try:
        if args.log_file is not None:
            args.log_file.parent.mkdir(parents=True, exist_ok=True)
            handler = RotatingFileHandler(
                args.log_file, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
            )
            handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
            logger.addHandler(handler)
        settings = Settings()
        if settings.storage_backend != "postgres":
            logger.error("Worker startup requires PostgreSQL configuration")
            return 2
        return prepare(
            settings.database_url.get_secret_value(),
            timeout=args.timeout,
            poll_seconds=args.poll_seconds,
            stop_file=args.stop_file,
            maintenance_file=args.maintenance_file,
        )
    except Exception as error:
        logger.error("Worker preparation failed (%s); check configuration", type(error).__name__)
        return 1
    finally:
        if handler is not None:
            logger.removeHandler(handler)
            handler.close()


if __name__ == "__main__":
    raise SystemExit(main())
