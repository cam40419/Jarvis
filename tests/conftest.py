from __future__ import annotations

import os
from collections.abc import Iterator
from typing import TYPE_CHECKING
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from simon.config import Settings, get_settings

if TYPE_CHECKING:
    from simon.api.app import AppContainer

_TEST_SETTINGS = {
    "SIMON_ENVIRONMENT": "test",
    "SIMON_AGENT_MANIFEST_FILE": "",
    "SIMON_AGENT_EXECUTION_ENABLED": "false",
    "SIMON_EXTERNAL_PROVIDERS_FILE": "",
    "SIMON_PROJECT_BOARDS_FILE": "",
    "SIMON_HOME_API_URL": "",
    "SIMON_HOME_API_TOKEN": "",
    "SIMON_STORAGE_BACKEND": "memory",
    "SIMON_DEV_LOGIN_ENABLED": "false",
    "SIMON_PUBLIC_ORIGIN": "http://localhost:8000",
    "SIMON_PUBLIC_PATH": "",
    "SIMON_RP_ID": "localhost",
    "SIMON_ACCOUNT_ADMIN_ACTOR_ID": "11111111-1111-4111-8111-111111111111",
    "SIMON_MODEL_PROVIDER": "local",
    "SIMON_GOOGLE_CLIENT_ID": "",
    "SIMON_PROJECT_DRIVE_SYNC_ENABLED": "false",
    "SIMON_LOCAL_FILES_ENABLED": "false",
}


def pytest_configure() -> None:
    # Test modules can import the global ASGI app before autouse fixtures run.
    # Establish isolation before collection can construct any runtime services.
    os.environ.update(_TEST_SETTINGS)
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def isolate_runtime_settings(
    monkeypatch: pytest.MonkeyPatch, powershell_cache_path: str
) -> Iterator[None]:
    for key, value in _TEST_SETTINGS.items():
        monkeypatch.setenv(key, value)
    # Restricted Windows profiles can resolve PowerShell's default cache root
    # to an empty path, otherwise creating Microsoft/ in the working tree.
    monkeypatch.setenv("PSModuleAnalysisCachePath", powershell_cache_path)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture(scope="session")
def powershell_cache_path(tmp_path_factory: pytest.TempPathFactory) -> str:
    return str(tmp_path_factory.mktemp("powershell-cache") / "ModuleAnalysisCache")


@pytest.fixture
def container(tmp_path) -> AppContainer:
    from simon.api.app import AppContainer

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
    from simon.api.app import create_app

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
