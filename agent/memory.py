from __future__ import annotations
from typing import List, Dict, Any, Optional, Tuple
import os, json, math
import mysql.connector as mysql
import numpy as np
from openai import OpenAI

from utils.sem import cosine


class MemoryStore:
    def __init__(self, dsn: Dict[str, Any]):
        self.db = mysql.connect(**dsn)
        self.client = OpenAI()

    def append_message(self, thread_key: str, role: str, content: str) -> int:
        tid = self._ensure_thread(thread_key)
        cur = self.db.cursor()
        cur.execute(
            "INSERT INTO messages (thread_id, role, content) VALUES (%s,%s,%s)",
            (tid, role, content),
        )
        self.db.commit()
        return cur.lastrowid

    def last_messages(self, thread_key: str, limit: int = 12) -> List[Dict[str, Any]]:
        tid = self._get_thread_id(thread_key)
        cur = self.db.cursor()
        cur.execute(
            """SELECT role, content FROM messages
                       WHERE thread_id=%s ORDER BY id DESC LIMIT %s""",
            (tid, limit),
        )
        rows = cur.fetchall()
        return [{"role": r, "content": c} for (r, c) in reversed(rows)]

    # --- profile facts ---
    def upsert_fact(
        self,
        subject: str,
        k: str,
        v: str,
        confidence: int = 80,
        source: str = "extracted",
    ):
        cur = self.db.cursor()
        cur.execute(
            """
          INSERT INTO profile_facts (subject,k,v,confidence,source,last_seen)
          VALUES (%s,%s,%s,%s,%s,NOW())
          ON DUPLICATE KEY UPDATE v=VALUES(v), confidence=GREATEST(confidence, VALUES(confidence)), last_seen=NOW()
        """,
            (subject, k, v, confidence, source),
        )
        self.db.commit()

    def top_facts(
        self, subject: str, keys_like: Optional[str] = None, limit: int = 20
    ) -> List[Dict[str, Any]]:
        cur = self.db.cursor()
        if keys_like:
            cur.execute(
                """SELECT k,v,confidence FROM profile_facts
                           WHERE subject=%s AND k LIKE %s
                           ORDER BY confidence DESC, last_seen DESC
                           LIMIT %s""",
                (subject, keys_like, limit),
            )
        else:
            cur.execute(
                """SELECT k,v,confidence FROM profile_facts
                           WHERE subject=%s
                           ORDER BY confidence DESC, last_seen DESC
                           LIMIT %s""",
                (subject, limit),
            )
        return [{"k": k, "v": v, "confidence": conf} for (k, v, conf) in cur.fetchall()]

    # --- semantic memory ---
    def embed(self, text: str) -> np.ndarray:
        e = self.client.embeddings.create(model="text-embedding-3-small", input=text)
        return np.array(e.data[0].embedding, dtype=np.float32)

    def add_chunk(
        self, thread_key: Optional[str], kind: str, text: str, store_vector: bool = True
    ):
        tid = self._get_thread_id(thread_key) if thread_key else None
        cur = self.db.cursor()
        if store_vector:
            vec = self.embed(text).tobytes()
            cur.execute(
                """INSERT INTO memory_chunks (thread_id, kind, text, embedding)
                           VALUES (%s,%s,%s,%s)""",
                (tid, kind, text, vec),
            )
        else:
            cur.execute(
                """INSERT INTO memory_chunks (thread_id, kind, text)
                           VALUES (%s,%s,%s)""",
                (tid, kind, text),
            )
        self.db.commit()

    def search(
        self, query: str, k: int = 5, thread_key: Optional[str] = None
    ) -> List[str]:
        qvec = self.embed(query)
        cur = self.db.cursor()
        if thread_key:
            tid = self._get_thread_id(thread_key)
            cur.execute(
                """SELECT id, text, embedding FROM memory_chunks
                           WHERE thread_id=%s ORDER BY id DESC LIMIT 200""",
                (tid,),
            )
        else:
            cur.execute(
                """SELECT id, text, embedding FROM memory_chunks
                           ORDER BY id DESC LIMIT 500"""
            )
        rows = cur.fetchall()

        scored: List[Tuple[float, str]] = []
        for _id, text, emb in rows:
            if emb is None:
                continue
            vec = np.frombuffer(emb, dtype=np.float32)
            scored.append((cosine(qvec, vec), text))

        scored.sort(key=lambda x: x[0], reverse=True)
        return [t for _, t in scored[:k]]

    # --- episodic summaries ---
    def summarize_segment(self, thread_key: str, start_id: int, end_id: int):
        tid = self._get_thread_id(thread_key)
        cur = self.db.cursor()
        cur.execute(
            """SELECT role, content FROM messages 
                       WHERE thread_id=%s AND id BETWEEN %s AND %s ORDER BY id ASC""",
            (tid, start_id, end_id),
        )
        parts = [{"role": r, "content": c} for (r, c) in cur.fetchall()]
        prompt = [
            {
                "role": "system",
                "content": "Summarize crisply; capture decisions, preferences, tasks, and open questions.",
            },
            *parts,
        ]
        resp = self.client.responses.create(model="gpt-4o-mini", input=prompt)
        summary = (resp.output_text or "").strip()

        cur.execute(
            """INSERT INTO thread_summaries (thread_id,segment_start_id,segment_end_id,summary,tokens)
                       VALUES (%s,%s,%s,%s,%s)""",
            (tid, start_id, end_id, summary, len(summary.split())),
        )
        self.db.commit()
        # also embed the summary for semantic recall
        self.add_chunk(thread_key, "summary", summary, store_vector=True)

    def _ensure_thread(self, thread_key: str) -> int:
        cur = self.db.cursor()
        cur.execute("SELECT id FROM threads WHERE thread_key=%s", (thread_key,))
        row = cur.fetchone()
        if row:
            return row[0]
        cur.execute("INSERT INTO threads (thread_key) VALUES (%s)", (thread_key,))
        self.db.commit()
        return cur.lastrowid

    def _get_thread_id(self, thread_key: str) -> int:
        cur = self.db.cursor()
        cur.execute("SELECT id FROM threads WHERE thread_key=%s", (thread_key,))
        row = cur.fetchone()
        if not row:
            raise RuntimeError(f"Unknown thread_key={thread_key}")
        return row[0]
