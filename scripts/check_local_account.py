"""Fail local startup if the configured administrator cannot use the persistent account."""

from __future__ import annotations

from simon.adapters.postgres import PostgresStore
from simon.config import Settings
from simon.domain.models import ActorContext, Channel
from simon.services.accounts import AccountService
from simon.services.identity import IdentityService


def main() -> None:
    settings = Settings()
    if settings.account_household_id is None:
        raise RuntimeError("SIMON_ACCOUNT_HOUSEHOLD_ID must identify the local workspace.")
    store = PostgresStore(settings.database_url.get_secret_value())
    identity = IdentityService(store, settings)
    membership = identity.membership(
        settings.account_admin_actor_id, settings.account_household_id
    )
    if membership.role != "owner":
        raise RuntimeError("The configured administrator must own the account workspace.")
    actor = ActorContext(
        actor_id=settings.account_admin_actor_id,
        household_id=settings.account_household_id,
        channel=Channel.API,
        scopes=frozenset({"identity:manage"}),
    )
    if not AccountService(identity).is_admin(actor):
        raise RuntimeError("The configured actor cannot manage Simon accounts.")
    password = store.password_for_actor(settings.account_admin_actor_id)
    if password is None or password.username != "cam40419":
        raise RuntimeError("The cam40419 password credential is missing.")
    print(f"Administrator {password.username} owns {membership.household_name}.")


if __name__ == "__main__":
    main()
