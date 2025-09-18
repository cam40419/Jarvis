from __future__ import annotations
import json
import logging
import logging.handlers
import os
import queue
import sys
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, Optional, Tuple


@dataclass
class LoggerConfig:
    level: str = os.getenv("LOG_LEVEL", "INFO")
    filename: Optional[str] = os.getenv("LOG_FILE", None)
    max_bytes: int = int(os.getenv("LOG_MAX_BYTES", "5_000_000"))
    backup_count: int = int(os.getenv("LOG_BACKUP_COUNT", "5"))
    json_logs: bool = os.getenv("LOG_JSON", "0") in ("1", "true", "True")
    use_queue: bool = False
    propagate: bool = False


class Logger:
    _instance: Optional["Logger"] = None
    _configured: bool = False
    _listener: Optional[logging.handlers.QueueListener] = None
    _queue: Optional["queue.Queue[logging.LogRecord]"] = None
    _config: LoggerConfig

    def __new__(cls, *args, **kwargs):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    @classmethod
    def init(cls, config: Optional[LoggerConfig] = None) -> None:
        if cls._configured:
            return
        cls._config = config or LoggerConfig()
        cls._configured = True

    @classmethod
    def get_logger(cls) -> logging.Logger:
        if not cls._configured:
            cls.init()
        logger = logging.getLogger("jarvis")
        logger.propagate = cls._config.propagate
        return logger

    @classmethod
    def set_level(cls, level: str) -> None:
        if not cls._configured:
            cls.init()
        logging.getLogger().setLevel(level.upper())
