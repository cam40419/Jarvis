from types import SimpleNamespace

import pytest

from scripts import backup_idle


@pytest.fixture(autouse=True)
def validated_target(monkeypatch):
    monkeypatch.setattr(backup_idle, "configured_identity", lambda settings: ("jarvis", "1234"))
    monkeypatch.setattr(backup_idle, "compose_identity", lambda: ("jarvis", "1234"))
    monkeypatch.setattr(
        backup_idle, "Settings", lambda: SimpleNamespace(storage_backend="postgres")
    )


@pytest.mark.parametrize(
    "active",
    [
        None,
        ("assistant.session", "queued"),
        ("assistant.session", "running"),
    ],
)
def test_complete_backups_defer_active_work_and_close_connection(monkeypatch, active):
    closed = []
    store = SimpleNamespace(
        jobs_all=lambda kind, limit, status: [object()] if (kind, status) == active else [],
        close=lambda: closed.append(True),
    )
    monkeypatch.setattr(backup_idle, "AppContainer", lambda: SimpleNamespace(store=store))
    assert backup_idle.main() == (10 if active else 0)
    assert closed == [True]


@pytest.mark.parametrize("configured", [("jarvis", "other-cluster"), ("other-database", "1234")])
def test_wrong_database_is_rejected_before_service_or_store_changes(monkeypatch, configured):
    monkeypatch.setattr(backup_idle, "configured_identity", lambda settings: configured)
    monkeypatch.setattr(
        backup_idle, "AppContainer", lambda: pytest.fail("Must not load mismatched store")
    )
    assert backup_idle.main() == 2


def test_memory_backend_is_rejected_before_database_or_service_changes(monkeypatch):
    monkeypatch.setattr(backup_idle, "Settings", lambda: SimpleNamespace(storage_backend="memory"))
    monkeypatch.setattr(
        backup_idle, "configured_identity", lambda _: pytest.fail("Unexpected database connection")
    )
    assert backup_idle.main() == 2


def test_backup_connection_failure_does_not_leave_an_open_pool(monkeypatch):
    closed = []

    def failing(*_):
        raise RuntimeError("database unavailable")

    store = SimpleNamespace(jobs_all=failing, close=lambda: closed.append(True))
    monkeypatch.setattr(backup_idle, "AppContainer", lambda: SimpleNamespace(store=store))
    with pytest.raises(RuntimeError):
        backup_idle.main()
    assert closed == [True]
