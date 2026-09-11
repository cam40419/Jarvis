"""Create the explicit local development identity: python -m jarvis.seed."""

import psycopg

from jarvis.config import get_settings
from jarvis.domain.identity import DEV_ACTOR_ID, DEV_HOUSEHOLD_ID


def seed_development_identity(database_url: str) -> None:
    with psycopg.connect(database_url, connect_timeout=5) as connection:
        connection.execute(
            "INSERT INTO households (id, name) VALUES (%s, 'Development household') "
            "ON CONFLICT (id) DO NOTHING",
            (DEV_HOUSEHOLD_ID,),
        )
        connection.execute(
            "INSERT INTO users (id, display_name) VALUES (%s, 'Development user') "
            "ON CONFLICT (id) DO NOTHING",
            (DEV_ACTOR_ID,),
        )
        connection.execute(
            "INSERT INTO memberships (household_id, user_id, role) VALUES (%s, %s, 'owner') "
            "ON CONFLICT (household_id, user_id) DO NOTHING",
            (DEV_HOUSEHOLD_ID, DEV_ACTOR_ID),
        )


def main() -> None:
    settings = get_settings()
    if settings.environment != "development":
        raise RuntimeError("development seeding requires JARVIS_ENVIRONMENT=development")
    seed_development_identity(settings.database_url.get_secret_value())
    print(f"Development user: {DEV_ACTOR_ID}\nDevelopment household: {DEV_HOUSEHOLD_ID}")


if __name__ == "__main__":
    main()
