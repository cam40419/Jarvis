from collections.abc import Iterator
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from simon.api.app import AppContainer, create_app
from simon.config import Settings, get_settings


@pytest.fixture(autouse=True)
def isolate_runtime_settings(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    for key, value in {
        "SIMON_ENVIRONMENT": "test",
        "SIMON_STORAGE_BACKEND": "memory",
        "SIMON_DEV_LOGIN_ENABLED": "false",
        "SIMON_PUBLIC_ORIGIN": "http://localhost:8000",
        "SIMON_PUBLIC_PATH": "",
        "SIMON_RP_ID": "localhost",
        "SIMON_MODEL_PROVIDER": "local",
        "SIMON_HOME_AUTO_DISCOVERY": "false",
        "SIMON_SHELLY_LAN_DISCOVERY": "false",
        "SIMON_POWER_MONITORING_ENABLED": "false",
        "SIMON_LIFX_TOKEN": "",
        "SIMON_TUYA_CLIENT_ID": "",
        "SIMON_TUYA_CLIENT_SECRET": "",
        "SIMON_TUYA_REGION": "us",
    }.items():
        monkeypatch.setenv(key, value)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def container() -> AppContainer:
    return AppContainer(
        settings=Settings(
            environment="test",
            storage_backend="memory",
            dev_login_enabled=True,
            dev_login_token=SecretStr("test-development-secret-32-characters"),
        )
    )


@pytest.fixture
def client(container: AppContainer) -> Iterator[TestClient]:
    with TestClient(create_app(container), base_url="http://localhost:8000") as test_client:
        yield test_client


@pytest.fixture
def auth_headers(client: TestClient) -> dict[str, str]:
    response = client.post(
        "/auth/dev-login",
        headers={"Origin": "http://localhost:8000"},
        json={"token": "test-development-secret-32-characters"},
    )
    assert response.status_code == 200
    return {"Origin": "http://localhost:8000", "X-CSRF-Token": response.json()["csrf_token"]}


@pytest.fixture(scope="session")
def postgres_base_url() -> str:
    import os

    url = os.environ.get("SIMON_TEST_DATABASE_URL")
    if not url:
        pytest.skip("set SIMON_TEST_DATABASE_URL to enable PostgreSQL tests")
    psycopg = pytest.importorskip("psycopg")
    from psycopg.conninfo import conninfo_to_dict

    if not conninfo_to_dict(url).get("dbname", "").endswith("_test"):
        pytest.fail("SIMON_TEST_DATABASE_URL database name must end with _test")
    with psycopg.connect(url, connect_timeout=5) as connection:
        connection.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")
        connection.execute("CREATE EXTENSION IF NOT EXISTS vector")
    return url


@pytest.fixture
def postgres_url(postgres_base_url: str) -> Iterator[str]:
    import psycopg
    from psycopg import sql
    from psycopg.conninfo import make_conninfo

    from simon.migrate import migrate

    schema = "simon_test_" + uuid4().hex
    with psycopg.connect(postgres_base_url, autocommit=True) as connection:
        connection.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
        url = make_conninfo(postgres_base_url, options=f"-c search_path={schema},public")
        try:
            migrate(url)
            yield url
        finally:
            connection.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


@pytest.fixture(params=["memory", pytest.param("postgres", marks=pytest.mark.postgres)])
def store(request: pytest.FixtureRequest):
    from simon.adapters.memory import InMemoryStore

    if request.param == "memory":
        return InMemoryStore()
    from simon.adapters.postgres import PostgresStore
    from simon.seed import seed_development_identity

    url = request.getfixturevalue("postgres_url")
    seed_development_identity(url)
    return PostgresStore(url)
