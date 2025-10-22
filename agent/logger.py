from typing import Any, Dict, Optional
from datetime import datetime, timezone
import pymysql as mysql
from dataclasses import dataclass
from enum import Enum
import json


class LogType(Enum):
    DEBUG = "debug"
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


@dataclass
class Log:
    timestamp: str = datetime.now(timezone.utc).isoformat()
    type: LogType = LogType.INFO
    message: str = ""
    args: Dict[str, Any] = None


class Logger:
    def __init__(self, dsn: Dict[str, Any]):
        self.db = mysql.connect(**dsn)

    def log(
        self,
        type: LogType,
        message: str,
        args: Optional[Dict[str, Any]] = None,
    ):
        # Build log entry
        entry = Log(
            timestamp=datetime.now(),
            type=type,
            message=message,
            args=args or {},
        )

        # Store log entry
        self.add_log(entry)

    def add_log(self, entry: Log) -> int:
        args_json = json.dumps(entry.args, separators=(",", ":"), default=str)

        cur = self.db.cursor()
        cur.execute(
            "INSERT INTO logs (timestamp, type, message, args) VALUES (%s,%s,%s,%s)",
            (entry.timestamp, entry.type, entry.message, args_json),
        )
        self.db.commit()
        return cur.lastrowid
