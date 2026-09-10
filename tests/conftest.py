from collections.abc import Iterator
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from jarvis.api.app import AppContainer, create_app


@pytest.fixture
def container() -> AppContainer:
    return AppContainer()


@pytest.fixture
def client(container: AppContainer) -> Iterator[TestClient]:
    with TestClient(create_app(container)) as test_client:
        yield test_client


@pytest.fixture
def auth_headers() -> dict[str, str]:
    return {
        "X-Actor-Id": str(uuid4()),
        "X-Household-Id": str(uuid4()),
        "X-Scopes": "system:read jobs:write",
        "X-Channel": "chat",
    }

