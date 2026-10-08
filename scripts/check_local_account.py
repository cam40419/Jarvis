"""Fail local startup if the configured administrator cannot use the persistent account."""

from __future__ import annotations

import argparse
from collections.abc import Sequence

from simon.adapters.postgres import PostgresStore
from simon.config import Settings
from simon.domain.models import ActorContext, Channel
from simon.services.accounts import AccountService
from simon.services.identity import IdentityService


def main(arguments: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config-only", action="store_true")
    args = parser.parse_args(arguments)
    store = None
    try:
        settings = Settings()
        if settings.storage_backend != "postgres" or settings.account_workspace_id is None:
            raise ValueError("Configure PostgreSQL and the administrator workspace.")
        if args.config_only:
            print("Persistent configuration valid; database and provider were not contacted.")
            return 0
        store = PostgresStore(settings.database_url.get_secret_value())
        identity = IdentityService(store, settings)
        actor = ActorContext(
            actor_id=settings.account_admin_actor_id,
            workspace_id=settings.account_workspace_id,
            channel=Channel.API,
            scopes=frozenset({"identity:manage"}),
        )
        if not AccountService(identity).is_admin(actor):
            raise ValueError("The configured administrator must own the account workspace.")
        if store.password_for_actor(actor.actor_id) is None and not store.passkeys(actor.actor_id):
            raise ValueError("The configured administrator needs a sign-in credential.")
        print("Configured administrator account is ready.")
        return 0
    except Exception:
        # Validation and database exceptions can include credentials or private account data.
        print("Administrator preflight failed; check configuration, membership and sign-in setup.")
        return 2
    finally:
        if store is not None:
            store.close()


if __name__ == "__main__":
    raise SystemExit(main())
