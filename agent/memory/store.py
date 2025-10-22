# agent/memory/store.py
from __future__ import annotations
import pymysql as mysql
from typing import List, Optional, Dict, Any

Message = Dict[str, str]


class MemoryStore:
    def __init__(self, dsn: Dict[str, Any]):
        self.db = mysql.connect(**dsn)
        self.current_conversation_id: Optional[int] = None

    def create_conversation(self, kind: str = "chat") -> int:
        cur = self.db.cursor()
        cur.execute("INSERT INTO conversations (type) VALUES (%s)", (kind,))
        self.db.commit()
        return cur.lastrowid

    def ensure_conversation(self, kind: str = "chat") -> int:
        if self.current_conversation_id is None:
            self.current_conversation_id = self.create_conversation(kind)
        return self.current_conversation_id

    def set_current_conversation(self, conversation_id: int) -> None:
        self.current_conversation_id = conversation_id

    def list_conversations(self, limit: int = 50) -> List[Dict[str, Any]]:
        cur = self.db.cursor(dictionary=True)
        cur.execute(
            "SELECT id, type, created_at FROM conversations ORDER BY id DESC LIMIT %s",
            (limit,),
        )
        return list(cur.fetchall())

    def store_message(self, conversation_id: int, role: str, content: str) -> int:
        cur = self.db.cursor()
        cur.execute(
            "INSERT INTO messages (conversation_id, role, content) VALUES (%s,%s,%s)",
            (conversation_id, role, content),
        )
        self.db.commit()
        return cur.lastrowid

    def get_conversation(
        self, conversation_id: Optional[int] = None, limit: Optional[int] = None
    ) -> List[Message]:
        cid = conversation_id or self.current_conversation_id
        if cid is None:
            return []

        cur = self.db.cursor()
        if limit:
            cur.execute(
                """SELECT role, content FROM messages
                   WHERE conversation_id=%s
                   ORDER BY created_at ASC, id ASC
                   LIMIT %s""",
                (cid, limit),
            )
        else:
            cur.execute(
                """SELECT role, content FROM messages
                   WHERE conversation_id=%s
                   ORDER BY created_at ASC, id ASC""",
                (cid,),
            )
        rows = cur.fetchall()
        return [{"role": r, "content": c} for (r, c) in rows]
