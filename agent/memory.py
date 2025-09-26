import mysql.connector as mysql
from typing import List, Optional, Dict, Any


class MemoryStore:
    def __init__(self, dsn: Dict[str, Any]):
        self.db = mysql.connect(**dsn)
        self.current_conversation_id = None

    def create_conversation(self, type: str) -> int:
        cur = self.db.cursor()
        cur.execute(
            "INSERT INTO conversations (type) VALUES (%s)",
            (type,),
        )
        self.db.commit()
        return cur.lastrowid

    def new_conversation(self, type: str) -> List[Dict[str, Any]]:
        self.current_conversation_id = self.create_conversation(type)

        return self.get_conversation()

    def add_message(self, conversation_id: int, role: str, content: str) -> int:
        cur = self.db.cursor()
        cur.execute(
            "INSERT INTO messages (conversation_id, role, content) VALUES (%s,%s,%s)",
            (conversation_id, role, content),
        )
        self.db.commit()
        return cur.lastrowid

    def get_conversation(self) -> List[Dict[str, Any]]:
        cur = self.db.cursor()
        cur.execute(
            """SELECT role, content FROM messages
               WHERE conversation_id=%s
               ORDER BY id DESC""",
            (self.current_conversation_id,),
        )
        rows = cur.fetchall()
        return [{"role": r, "content": c} for (r, c) in reversed(rows)]
