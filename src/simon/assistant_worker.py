"""Run the durable workflow worker: python -m simon.assistant_worker [--once]."""

import argparse
import logging
from concurrent.futures import Future, ThreadPoolExecutor
from threading import Event

from simon.config import Settings
from simon.services.audit import AuditService
from simon.services.connected import ConnectedService
from simon.services.identity import IdentityService
from simon.services.work_sessions import WorkSessionService


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--once", action="store_true", help="Process one batch of due runs, then exit"
    )
    parser.add_argument("--poll-seconds", type=float, default=1)
    args = parser.parse_args()
    if not 0.2 <= args.poll_seconds <= 10:
        parser.error("poll-seconds must be between 0.2 and 10")
    settings = Settings()
    if settings.storage_backend != "postgres":
        parser.error("The standalone worker requires SIMON_STORAGE_BACKEND=postgres")
    from simon.adapters.postgres import PostgresStore

    store = PostgresStore(settings.database_url.get_secret_value())
    identity = IdentityService(store, settings)
    audit = AuditService(store)
    connected = ConnectedService(store, audit, settings, identity)
    task_service = None
    if settings.model_provider == "openai" and settings.openai_api_key:
        from simon.adapters.openai_model import OpenAIModel
        from simon.services.model_conversations import ModelConversationService
        from simon.services.tasks import AssistantTaskService

        conversations = ModelConversationService(
            store,
            audit,
            OpenAIModel(settings.openai_api_key.get_secret_value()),
            settings,
            connected,
        )
        task_service = AssistantTaskService(store, identity, conversations)
        connected.tasks = task_service
    sessions = WorkSessionService(task_service) if task_service else None
    session_futures: list[Future[int]] = []
    session_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="work-session")
    logging.basicConfig(level=logging.INFO)
    logging.info("Simon assistant worker started")
    task_future: Future[int] | None = None
    executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="assistant-task")
    try:
        while True:
            try:
                for future in session_futures:
                    if future.done():
                        try:
                            future.result()
                        except Exception as error:
                            logging.error("Work session failed (%s)", type(error).__name__)
                session_futures = [future for future in session_futures if not future.done()]
                if sessions:
                    while len(session_futures) < 2:
                        session_futures.append(session_executor.submit(sessions.tick))
                if task_future and task_future.done():
                    try:
                        completed = task_future.result()
                        if completed:
                            logging.info("Executed %s assistant task", completed)
                    except Exception as error:
                        logging.error("Assistant task failed (%s)", type(error).__name__)
                    task_future = None
                if task_service and task_future is None:
                    task_future = executor.submit(task_service.tick)
            except Exception as error:
                logging.error(
                    "Assistant tick failed (%s); pending leases will recover", type(error).__name__
                )
                if args.once:
                    raise SystemExit(1) from None
            if args.once:
                break
            Event().wait(args.poll_seconds)
    except KeyboardInterrupt:
        logging.info("Simon assistant worker stopped")
    finally:
        session_executor.shutdown(wait=True, cancel_futures=True)
        executor.shutdown(wait=True, cancel_futures=True)
        store.close()


if __name__ == "__main__":
    main()
