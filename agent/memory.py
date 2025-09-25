import mysql.connector as mysql
from typing import List, Optional, Dict, Any


class MemoryStore:
    def __init__(self, dsn: Dict[str, Any]):
        self.db = mysql.connect(**dsn)
        self.current_conversation_id = None

    def create_conversation(self, subject_id: str, channel: str) -> int:
        cur = self.db.cursor()
        cur.execute(
            "INSERT INTO conversations (subject_id, channel, created_at, updated_at) VALUES (%s,%s,NOW(),NOW())",
            (subject_id, channel),
        )
        self.db.commit()
        return cur.lastrowid

    def get_last_message_time(self, conversation_id: int) -> Optional[float]:
        cur = self.db.cursor()
        cur.execute(
            "SELECT UNIX_TIMESTAMP(MAX(created_at)) FROM messages WHERE conversation_id=%s",
            (conversation_id,),
        )
        row = cur.fetchone()
        return float(row[0]) if row and row[0] is not None else None

    def set_current_conversation(self, conversation_id: int) -> None:
        self.current_conversation_id = conversation_id

    def get_current_conversation(self) -> Optional[int]:
        return self.current_conversation_id

    def add_message(self, conversation_id: int, role: str, content: str) -> int:
        cur = self.db.cursor()
        cur.execute(
            "INSERT INTO messages (conversation_id, role, content, created_at) VALUES (%s,%s,%s,NOW())",
            (conversation_id, role, content),
        )
        self.db.commit()
        return cur.lastrowid

    def get_messages(
        self, conversation_id: int, limit: int = 20
    ) -> List[Dict[str, Any]]:
        cur = self.db.cursor()
        cur.execute(
            """SELECT role, content FROM messages
               WHERE conversation_id=%s
               ORDER BY id DESC LIMIT %s""",
            (conversation_id, limit),
        )
        rows = cur.fetchall()
        return [{"role": r, "content": c} for (r, c) in reversed(rows)]
