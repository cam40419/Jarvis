"""Execute queued agent plans: python -m simon.agent_dispatcher [--once].

Run beside the API with the same configuration and SIMON_AGENT_STATE_DIR on one
manager host. The directory contains the local lease journal and artifacts;
independent copies do not coordinate container or machine ownership.
"""

from __future__ import annotations

import argparse
import json
import logging
import signal
from collections.abc import Callable, Iterator, Sequence
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from threading import Event
from types import FrameType
from typing import TYPE_CHECKING
from uuid import UUID

from pydantic import ValidationError as PydanticError

from simon.adapters.external_action_binding import (
    external_action_service,
    external_tool_status,
    external_transport_factory,
)
from simon.adapters.native_tools import (
    native_tool_status,
    native_transport_factory,
    with_native_tools,
)
from simon.adapters.project_board_binding import project_board_service
from simon.adapters.project_work_tools import project_transport_factory
from simon.adapters.tool_preflight import INSTALLED_TRANSPORTS
from simon.config import Settings
from simon.domain.agent_runs import AgentRun
from simon.domain.errors import DomainError
from simon.services.agent_dispatcher import AgentDispatcher
from simon.services.agent_platform import AgentPlatformService, load_manifest
from simon.services.agent_runs import AgentRunService
from simon.services.audit import AuditService
from simon.services.connected import ConnectedService
from simon.services.identity import IdentityService
from simon.services.project_autonomy import ProjectAutonomyService
from simon.services.project_coordinator import ProjectCoordinator
from simon.services.project_work import ProjectWorkService
from simon.services.tasks import AssistantTaskService

if TYPE_CHECKING:
    from simon.adapters.postgres import PostgresStore

logger = logging.getLogger(__name__)


def _store(settings: Settings, concurrency: int) -> PostgresStore:
    # Keep the PostgreSQL extra optional for --help and configuration validation.
    from simon.adapters.postgres import PostgresStore

    return PostgresStore(
        settings.database_url.get_secret_value(), pool_size=min(32, max(4, concurrency + 2)),
    )


def _print_run(run: AgentRun) -> None:
    print(json.dumps({"id": str(run.id), "status": run.status, "version": run.version}), flush=True)


def _finish(future: Future[AgentRun | None]) -> bool:
    if future.cancelled():
        return False
    try:
        run = future.result()
    except Exception:
        # Provider/DB exception strings can contain credentials or sensitive payloads.
        logger.error("Agent dispatch failed; inspect interrupted runs before operator recovery")
        return False
    if run is not None:
        _print_run(run)
        return True
    return False


@contextmanager
def _shutdown_signals(stop: Event) -> Iterator[None]:
    def request_stop(number: int, frame: FrameType | None) -> None:
        stop.set()

    numbers = [signal.SIGINT, signal.SIGTERM]
    if hasattr(signal, "SIGBREAK"):
        numbers.append(signal.SIGBREAK)
    previous = {number: signal.signal(number, request_stop) for number in numbers}
    try:
        yield
    finally:
        for number, handler in previous.items():
            signal.signal(number, handler)


def _serve(
    dispatcher: AgentDispatcher, *, concurrency: int, poll_seconds: float, stop: Event,
    stop_file: Path | None = None,
    project_tick: Callable[[], int] | None = None,
) -> None:
    """Bound run futures independently of the dispatcher's durable agent-slot claims."""
    active: set[Future[AgentRun | None]] = set()
    executor = ThreadPoolExecutor(max_workers=concurrency, thread_name_prefix="agent-dispatch")
    project_executor = ThreadPoolExecutor(
        max_workers=1, thread_name_prefix="project-coordinate",
    ) if project_tick is not None else None
    project_future: Future[int] | None = None

    def finish_project(future: Future[int]) -> None:
        if not future.cancelled():
            try:
                future.result()
            except Exception:
                logger.error("Project scheduling failed; saved work requires review")

    try:
        while not stop.is_set():
            if stop_file is not None and stop_file.exists():
                # Retain the request so recovery supervision recognizes an
                # intentional stop. An explicit launcher clears it before setup.
                stop.set()
                logger.info("Agent dispatcher stop requested; draining active work")
                break
            if project_executor is not None and project_tick is not None:
                if project_future is not None and project_future.done():
                    finish_project(project_future)
                    project_future = None
                if project_future is None:
                    # Provider polling must not block unrelated agent runs or shutdown signals.
                    project_future = project_executor.submit(project_tick)
            idle_seen = not active
            for future in tuple(active):
                if future.done():
                    active.remove(future)
                    if not _finish(future):
                        idle_seen = True
            # After an idle tick, probe only once per polling interval. Running
            # work can fill the remaining slots on the next interval.
            available = concurrency - len(active)
            to_submit = min(1, available) if idle_seen else available
            for _ in range(to_submit):
                if stop.is_set():
                    break
                active.add(executor.submit(dispatcher.tick))
            stop.wait(poll_seconds)
    finally:
        # Stop scheduling, cancel futures that never began, and drain bounded
        # in-flight calls. Never reclaim a lease while its original worker runs.
        executor.shutdown(wait=True, cancel_futures=True)
        if project_executor is not None:
            project_executor.shutdown(wait=True, cancel_futures=True)
        if project_future is not None:
            finish_project(project_future)
        for future in active:
            _finish(future)


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--once", action="store_true", help="Execute one queued run, then exit")
    modes.add_argument("--recover-run", type=UUID, metavar="UUID", help=(
        "Operator recovery of an interrupted run after stopping its original dispatcher"
    ))
    parser.add_argument("--expected-version", type=int, help="Required version for recovery")
    parser.add_argument("--worker-stopped", action="store_true", help=(
        "Acknowledge the original dispatcher and its workers have stopped; required for recovery"
    ))
    parser.add_argument("--poll-seconds", type=float, default=1, help="Polling interval, 0.2 to 10")
    parser.add_argument("--stop-file", type=Path, help=(
        "Watch an operator-owned request file for graceful shutdown; existing requests are honored"
    ))
    args = parser.parse_args(argv)
    if not 0.2 <= args.poll_seconds <= 10:
        parser.error("poll-seconds must be between 0.2 and 10")
    if args.recover_run is not None:
        if args.expected_version is None or args.expected_version < 1 or not args.worker_stopped:
            parser.error("recovery requires --expected-version >= 1 and --worker-stopped")
    elif args.expected_version is not None or args.worker_stopped:
        parser.error("--expected-version and --worker-stopped require --recover-run")
    if args.stop_file is not None and (args.once or args.recover_run is not None):
        parser.error("--stop-file is only available in continuous dispatcher mode")
    try:
        settings = Settings()
    except PydanticError:
        parser.exit(2, "Agent dispatcher settings are invalid; check configuration.\n")
    if settings.storage_backend != "postgres":
        parser.error("The standalone dispatcher requires SIMON_STORAGE_BACKEND=postgres")
    if not settings.agent_execution_enabled:
        parser.error("The standalone dispatcher requires SIMON_AGENT_EXECUTION_ENABLED=true")
    try:
        manifest = load_manifest(settings.agent_manifest_file)
        store = _store(settings, manifest.max_parallel)
    except ImportError:
        parser.exit(2, "The standalone dispatcher requires the PostgreSQL package extra.\n")
    except (DomainError, OSError):
        parser.exit(2, "Agent dispatcher manifest or state configuration is unavailable.\n")
    logging.basicConfig(level=logging.INFO)
    try:
        connected = ConnectedService(
            store, AuditService(store), settings, IdentityService(store, settings),
        )
        manifest = with_native_tools(manifest, connected)
        external_actions = external_action_service(settings, store)
        platform = AgentPlatformService(
            store, manifest, state_dir=settings.agent_state_dir,
            available_transports=INSTALLED_TRANSPORTS,
        )
        runs = AgentRunService(platform, enabled=settings.agent_execution_enabled)
        platform.tool_availability = lambda actor, tool_id: (
            native_tool_status(connected, actor, tool_id) if tool_id.startswith("native.")
            else external_tool_status(external_actions, actor, tool_id)
        )
        if args.recover_run is not None:
            assert args.expected_version is not None
            recovered = runs.recover_interrupted(
                args.recover_run, args.expected_version,
                operator_actor_id=settings.account_admin_actor_id,
            )
            _print_run(recovered)
            return
        project_work = ProjectWorkService(
            store, project_resolver=AssistantTaskService(store, connected.identity).project,
        )
        boards = project_board_service(settings, store, project_work)
        coordinator = ProjectCoordinator(
            project_work, runs, external_actions=external_actions, boards=boards,
        )
        project_work.team_validator = coordinator.validate_team
        autonomy = ProjectAutonomyService(project_work, coordinator, enabled=True)
        def project_tick() -> int:
            return boards.tick() + autonomy.tick()

        dispatcher = AgentDispatcher(runs, transport_factory=external_transport_factory(
            project_transport_factory(native_transport_factory(connected), project_work, runs),
            external_actions,
        ))
        stop = Event()
        with _shutdown_signals(stop):
            if args.once:
                project_tick()
                run = dispatcher.tick()
                project_tick()
                if run is not None:
                    _print_run(run)
                else:
                    print(json.dumps({"status": "idle"}), flush=True)
            else:
                logger.info("Simon agent dispatcher started")
                _serve(dispatcher, concurrency=manifest.max_parallel,
                       poll_seconds=args.poll_seconds, stop=stop, stop_file=args.stop_file,
                       project_tick=project_tick)
                logger.info("Simon agent dispatcher stopped")
    except KeyboardInterrupt:
        # Signal handlers normally request graceful draining; keep direct
        # interruption safe for callers invoking main() programmatically too.
        logger.info("Simon agent dispatcher stopped")
    except DomainError:
        parser.exit(1, "Agent operation was rejected; verify run version and configuration.\n")
    except Exception:
        parser.exit(1, "Agent dispatcher failed; inspect run state before operator recovery.\n")
    finally:
        store.close()


if __name__ == "__main__":
    main()
