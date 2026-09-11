"""Local operator tools for enrollment and revocation; requires database access."""

import argparse
from uuid import UUID

from jarvis.adapters.postgres import PostgresStore
from jarvis.config import get_settings
from jarvis.domain.errors import NotFoundError
from jarvis.domain.identity import Membership
from jarvis.services.identity import IDENTITY_LOCK, IdentityService


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    enroll = commands.add_parser(
        "enroll", help="Issue a one-use enrollment token, valid for 15 minutes"
    )
    enroll.add_argument("--actor-id", type=UUID, required=True)
    enroll.add_argument("--household-id", type=UUID, required=True)
    revoke = commands.add_parser("revoke-sessions")
    revoke.add_argument("--actor-id", type=UUID, required=True)
    remove = commands.add_parser("revoke-passkey")
    remove.add_argument("--credential-id", required=True)
    member = commands.add_parser("membership", help="Create or update a household membership")
    member.add_argument("--actor-id", type=UUID, required=True)
    member.add_argument("--household-id", type=UUID, required=True)
    member.add_argument("--role", choices=["owner", "member", "guest"], required=True)
    member.add_argument("--display-name", default="Jarvis user")
    member.add_argument("--household-name", default="Household")
    remove_member = commands.add_parser("remove-membership")
    remove_member.add_argument("--actor-id", type=UUID, required=True)
    remove_member.add_argument("--household-id", type=UUID, required=True)
    args = parser.parse_args()
    settings = get_settings()
    if settings.storage_backend != "postgres":
        raise RuntimeError("identity administration requires PostgreSQL storage")
    store = PostgresStore(settings.database_url.get_secret_value())
    service = IdentityService(store, settings)
    with store.transaction(IDENTITY_LOCK):
        if args.command == "enroll":
            secret = service.enroll(args.actor_id, args.household_id)
        elif args.command == "revoke-sessions":
            store.revoke_sessions(args.actor_id)
            for membership in store.memberships(args.actor_id):
                service.operator_audit(
                    "identity.sessions_revoked", args.actor_id, membership.household_id
                )
        elif args.command == "revoke-passkey":
            credential = store.get_passkey(args.credential_id)
            if credential is None:
                raise NotFoundError("passkey not found")
            store.delete_passkey(args.credential_id)
            store.revoke_sessions(credential.actor_id)
            for membership in store.memberships(credential.actor_id):
                service.operator_audit(
                    "identity.passkey_revoked", credential.actor_id, membership.household_id
                )
        elif args.command == "remove-membership":
            service.membership(args.actor_id, args.household_id)
            store.delete_membership(args.actor_id, args.household_id)
            store.revoke_sessions(args.actor_id)
            service.operator_audit("identity.membership_removed", args.actor_id, args.household_id)
        else:
            store.put_membership(
                Membership(
                    actor_id=args.actor_id,
                    household_id=args.household_id,
                    role=args.role,
                    display_name=args.display_name,
                    household_name=args.household_name,
                )
            )
            service.operator_audit("identity.membership_updated", args.actor_id, args.household_id)
    if args.command == "enroll":
        print("One-use enrollment token (expires in 15 minutes):\n" + secret)
    else:
        print("Identity records updated.")


if __name__ == "__main__":
    main()
