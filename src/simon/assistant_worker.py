"""Run durable assistant tasks and work sessions: python -m simon.assistant_worker."""

from __future__ import annotations

import argparse
import logging
import signal
from collections.abc import Iterator, Sequence
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import contextmanager
from logging.handlers import RotatingFileHandler
from pathlib import Path
from threading import Event
from types import FrameType
from typing import TYPE_CHECKING, Protocol

from pydantic import ValidationError as PydanticError

from simon.config import Settings
from simon.services.audit import AuditService
from simon.services.connected import ConnectedService
from simon.services.identity import IdentityService
from simon.services.work_sessions import WorkSessionService

if TYPE_CHECKING:
    from simon.adapters.postgres import PostgresStore
    from simon.services.tasks import AssistantTaskService

logger = logging.getLogger(__name__)


class TickService(Protocol):
    def tick(self) -> int: ...


def _store(settings: Settings) -> PostgresStore:
    from simon.adapters.postgres import PostgresStore

    return PostgresStore(settings.database_url.get_secret_value())


def _services(
    store: PostgresStore, settings: Settings,
) -> tuple[AssistantTaskService, WorkSessionService]:
    from simon.adapters.openai_model import OpenAIModel
    from simon.services.model_conversations import ModelConversationService
    from simon.services.tasks import AssistantTaskService

    assert settings.openai_api_key is not None
    identity, audit = IdentityService(store, settings), AuditService(store)
    connected = ConnectedService(store, audit, settings, identity)
    conversations = ModelConversationService(
        store, audit, OpenAIModel(settings.openai_api_key.get_secret_value()), settings, connected,
    )
    tasks = AssistantTaskService(store, identity, conversations)
    connected.tasks = tasks
    return tasks, WorkSessionService(tasks)


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


def _finish(future: Future[int], label: str) -> bool:
    if future.cancelled():
        return True
    try:
        completed = future.result()
        if completed:
            logger.info("Completed %s %s tick(s)", completed, label)
        return True
    except Exception as error:
        logger.error("%s failed (%s); inspect saved work before retrying",
                     label, type(error).__name__)
        return False


def _serve(
    tasks: TickService, sessions: TickService, *, poll_seconds: float, stop: Event,
    once: bool = False, stop_file: Path | None = None,
) -> bool:
    active: dict[Future[int], str] = {}
    executor = ThreadPoolExecutor(max_workers=3, thread_name_prefix="assistant-worker")
    failed = False
    try:
        while not stop.is_set():
            if stop_file is not None and stop_file.exists():
                logger.info("Assistant stop requested; draining active work")
                stop.set()
                break
            for future in tuple(active):
                if future.done():
                    failed = not _finish(future, active.pop(future)) or failed
            services = (("work session", sessions, 2), ("assistant task", tasks, 1))
            for label, service, capacity in services:
                pending = sum(value == label for value in active.values())
                for _ in range(capacity - pending):
                    if stop.is_set() or (stop_file is not None and stop_file.exists()):
                        break
                    active[executor.submit(service.tick)] = label
            if once:
                break
            stop.wait(poll_seconds)
    finally:
        executor.shutdown(wait=True, cancel_futures=not once)
        for future, label in active.items():
            failed = not _finish(future, label) or failed
    return not (once and failed)


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--once", action="store_true", help="Process one batch, then exit")
    modes.add_argument("--check", action="store_true",
                       help="Validate settings without opening a DB")
    parser.add_argument("--poll-seconds", type=float, default=1)
    parser.add_argument("--stop-file", type=Path,
                        help="Persistent operator-owned graceful stop marker")
    parser.add_argument("--log-file", type=Path,
                        help="Append rotating operational logs (5 MiB, 3 backups)")
    args = parser.parse_args(argv)
    if not 0.2 <= args.poll_seconds <= 10:
        parser.error("poll-seconds must be between 0.2 and 10")
    if args.stop_file is not None and (args.once or args.check):
        parser.error("--stop-file is only available in continuous mode")
    try:
        settings = Settings()
    except PydanticError:
        parser.exit(2, "Assistant settings are invalid; check configuration.\n")
    if settings.storage_backend != "postgres":
        parser.error("The standalone worker requires SIMON_STORAGE_BACKEND=postgres")
    if settings.model_provider != "openai" or not settings.openai_api_key:
        parser.error("Assistant tasks require SIMON_MODEL_PROVIDER=openai and a configured API key")
    if args.check:
        print("Assistant configuration valid; database and provider availability were not checked.")
        return
    logging.basicConfig(level=logging.INFO)
    file_handler = None
    store = None
    try:
        if args.log_file is not None:
            args.log_file.parent.mkdir(parents=True, exist_ok=True)
            file_handler = RotatingFileHandler(args.log_file, maxBytes=5 * 1024 * 1024,
                                              backupCount=3, encoding="utf-8")
            file_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
            logger.addHandler(file_handler)
        stop = Event()
        with _shutdown_signals(stop):
            if args.stop_file is not None and args.stop_file.exists():
                logger.info("Assistant remains stopped; remove the stop marker to resume")
                return
            store = _store(settings)
            tasks, sessions = _services(store, settings)
            logger.info("Simon assistant worker started")
            if not _serve(tasks, sessions, poll_seconds=args.poll_seconds, stop=stop,
                          once=args.once, stop_file=args.stop_file):
                parser.exit(1, "Assistant batch failed; inspect the worker log and saved work.\n")
            logger.info("Simon assistant worker stopped")
    except KeyboardInterrupt:
        logger.info("Simon assistant worker interrupted; active work drained")
    except Exception as error:
        logger.error("Assistant worker failed (%s)", type(error).__name__)
        parser.exit(1, "Assistant worker failed; inspect configuration and saved work.\n")
    finally:
        try:
            if store is not None:
                store.close()
        except Exception as error:
            logger.error("Assistant database cleanup failed (%s)", type(error).__name__)
            parser.exit(1, "Assistant database cleanup failed; inspect service availability.\n")
        finally:
            if file_handler is not None:
                logger.removeHandler(file_handler)
                file_handler.close()


if __name__ == "__main__":
    main()
