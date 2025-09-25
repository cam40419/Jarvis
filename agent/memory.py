# --- add this at top of MemoryStore ---
from email.message import EmailMessage
import mimetypes, hashlib, os, time, uuid
from openai import OpenAI
from rapidfuzz import fuzz, process as rf_process  # pip install rapidfuzz
import mysql.connector as mysql
from typing import List, Optional, Dict, Any, Tuple
import numpy as np

from utils.sem import cosine

ARTIFACT_DIR = os.getenv("ARTIFACT_DIR", "/var/app/artifacts")


class MemoryStore:
    def __init__(self, dsn: Dict[str, Any]):
        self.db = mysql.connect(**dsn)
        # Embeddings/OpenAI client (used by embed())
        self.client = OpenAI(api_key=os.getenv("OPENAI_API_KEY", ""))
        self._current_thread_id = None  # optional: set by agent per request

    # ---------- conversation helpers ----------
    def create_conversation(self, subject_id: str, channel: str) -> int:
        cur = self.db.cursor()
        cur.execute(
            "INSERT INTO conversations (subject_id, channel, created_at, updated_at) VALUES (%s,%s,NOW(),NOW())",
            (subject_id, channel),
        )
        self.db.commit()
        return cur.lastrowid

    def last_message_ts(self, conversation_id: int) -> Optional[float]:
        cur = self.db.cursor()
        cur.execute(
            "SELECT UNIX_TIMESTAMP(MAX(created_at)) FROM messages WHERE conversation_id=%s",
            (conversation_id,),
        )
        row = cur.fetchone()
        return float(row[0]) if row and row[0] is not None else None

    def set_current_thread(self, conversation_id: int) -> None:
        self._current_thread_id = conversation_id

    def current_thread_id(self) -> Optional[int]:
        return self._current_thread_id

    # ---------- message helpers (role-specific wrappers) ----------
    def append_user(self, conversation_id: int, content: str) -> int:
        return self.add_message(conversation_id, "user", content)

    def append_assistant(self, conversation_id: int, content: str) -> int:
        return self.add_message(conversation_id, "assistant", content)

    def append_system(self, conversation_id: int, content: str) -> int:
        return self.add_message(conversation_id, "system", content)

    def add_message(self, conversation_id: int, role: str, content: str) -> int:
        cur = self.db.cursor()
        cur.execute(
            "INSERT INTO messages (conversation_id, role, content, created_at) VALUES (%s,%s,%s,NOW())",
            (conversation_id, role, content),
        )
        self.db.commit()
        return cur.lastrowid

    def last_messages(
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

    # ---------- embeddings / semantic memory ----------
    def embed(self, text: str) -> np.ndarray:
        e = self.client.embeddings.create(model="text-embedding-3-small", input=text)
        return np.array(e.data[0].embedding, dtype=np.float32)

    def add_chunk(
        self, thread_key: Optional[str], kind: str, text: str, store_vector: bool = True
    ):
        tid = self._get_thread_id(thread_key) if thread_key else self._current_thread_id
        cur = self.db.cursor()
        if store_vector:
            vec = self.embed(text).tobytes()
            cur.execute(
                """INSERT INTO memory_chunks (thread_id, kind, text, embedding, created_at)
                   VALUES (%s,%s,%s,%s,NOW())""",
                (tid, kind, text, vec),
            )
        else:
            cur.execute(
                """INSERT INTO memory_chunks (thread_id, kind, text, created_at)
                   VALUES (%s,%s,%s,NOW())""",
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

    # You might already have this; placeholder for completeness
    def _get_thread_id(self, thread_key: str) -> Optional[int]:
        # If you use alphanumeric keys, map to integer id via conversations table
        return self._current_thread_id

    # ---------- contacts (prefilter + ranking) ----------
    def find_contacts_prefilter(
        self, query: str, limit: int = 50
    ) -> List[Dict[str, Any]]:
        q = f"%{query.strip().lower()}%"
        cur = self.db.cursor(dictionary=True)
        cur.execute(
            """SELECT contact_id, display_name, email, org, COALESCE(UNIX_TIMESTAMP(last_interacted_at),0) AS last_seen
               FROM contacts
               WHERE LOWER(display_name) LIKE %s OR LOWER(email) LIKE %s
               ORDER BY last_interacted_at DESC
               LIMIT %s""",
            (q, q, limit),
        )
        return cur.fetchall()

    def rank_contacts(
        self, query: str, candidates: List[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        q = query.strip().lower()

        def score(row):
            base = 0.0
            # exact email
            if row["email"].lower() == q:
                base = 1.0
            # name similarity
            name_sim = (
                fuzz.token_set_ratio(q, (row["display_name"] or "").lower()) / 100.0
            )
            base = max(base, 0.6 * name_sim)
            # recency bias
            rec = row.get("last_seen", 0.0)
            if rec:
                base += 0.05
            return min(base, 1.0)

        scored = [{**r, "score": score(r)} for r in candidates]
        scored.sort(key=lambda r: r["score"], reverse=True)
        return scored

    # ---------- email templating + sending ----------
    def render_email_template(
        self,
        template_key: str,
        variables: Dict[str, Any],
        tone: str = "professional",
        lang: str = "en",
    ) -> Dict[str, str]:
        # Minimal placeholder; swap to Jinja2 later
        subject = variables.get("subject") or f"{template_key.replace('_',' ').title()}"
        summary = variables.get("summary", "")
        bullets = variables.get("bullets", [])
        links = variables.get("links", [])
        body_text = summary + (
            "\n\n" + "\n".join(f"- {b}" for b in bullets) if bullets else ""
        )
        if links:
            body_text += "\n\nLinks:\n" + "\n".join(
                f"- {x.get('title','link')}: {x['url']}" for x in links if "url" in x
            )
        body_html = f"<p>{summary}</p>" + (
            "<ul>" + "".join(f"<li>{b}</li>" for b in bullets) + "</ul>"
            if bullets
            else ""
        )
        if links:
            body_html += (
                "<p><strong>Links:</strong></p><ul>"
                + "".join(
                    f"<li><a href='{x['url']}'>{x.get('title','link')}</a></li>"
                    for x in links
                    if "url" in x
                )
                + "</ul>"
            )
        return {"subject": subject, "body_html": body_html, "body_text": body_text}

    def smtp_send(
        self,
        to: List[str],
        cc: List[str],
        bcc: List[str],
        subject: str,
        body_html: Optional[str],
        body_text: Optional[str],
        attachments: List[Dict[str, str]],
        dry_run: bool = False,
    ) -> Dict[str, Any]:
        msg = EmailMessage()
        msg["Subject"] = subject
        msg["To"] = ", ".join(to)
        if cc:
            msg["Cc"] = ", ".join(cc)
        if body_html:
            msg.add_alternative(body_html, subtype="html")
            if body_text:
                msg.set_content(body_text)
        else:
            msg.set_content(body_text or "")
        # Attachments by file_id
        for att in attachments or []:
            file_id = att.get("file_id")
            if not file_id:
                continue
            file_row = self._get_file(file_id)
            if not file_row:
                continue
            path = file_row["storage_uri"].replace("file://", "")
            ctype, _ = mimetypes.guess_type(path)
            maintype, subtype = (ctype or "application/octet-stream").split("/", 1)
            with open(path, "rb") as f:
                msg.add_attachment(
                    f.read(),
                    maintype=maintype,
                    subtype=subtype,
                    filename=file_row["filename"],
                )
        if dry_run:
            # Store preview
            preview_path = os.path.join(ARTIFACT_DIR, f"preview_{int(time.time())}.eml")
            os.makedirs(os.path.dirname(preview_path), exist_ok=True)
            with open(preview_path, "wb") as f:
                f.write(bytes(msg))
            return {
                "ok": True,
                "provider": "smtp",
                "message_id": None,
                "task_id": None,
                "preview_path": f"file://{preview_path}",
            }
        # Real send: configure SMTP here (left for your env)
        # Example:
        # import smtplib
        # with smtplib.SMTP(os.getenv("SMTP_HOST","localhost"), int(os.getenv("SMTP_PORT","25"))) as s:
        #     s.send_message(msg)
        message_id = f"smtp-{int(time.time())}"
        return {
            "ok": True,
            "provider": "smtp",
            "message_id": message_id,
            "task_id": None,
            "preview_path": None,
        }

    def _get_file(self, file_id: str) -> Optional[Dict[str, Any]]:
        cur = self.db.cursor(dictionary=True)
        cur.execute(
            "SELECT file_id, filename, storage_uri FROM files WHERE file_id=%s",
            (file_id,),
        )
        return cur.fetchone()

    # ---------- artifacts (links/files) ----------
    def save_links(
        self, links: List[Dict[str, Any]], thread_id: Optional[int]
    ) -> List[Dict[str, Any]]:
        cur = self.db.cursor(dictionary=True)
        out = []
        for x in links:
            url = x.get("url")
            title = x.get("title")
            notes = x.get("notes")
            conf = x.get("confidence")
            if not url:
                continue
            cur.execute(
                """INSERT INTO links (url,title,notes,source_thread_id,first_seen,last_seen,confidence)
                   VALUES (%s,%s,%s,%s,NOW(),NOW(),%s)
                   ON DUPLICATE KEY UPDATE title=VALUES(title), notes=VALUES(notes),
                     last_seen=NOW(), confidence=GREATEST(VALUES(confidence), confidence)""",
                (url, title, notes, thread_id, conf),
            )
            out.append({"url": url})
        self.db.commit()
        return out

    def save_files(
        self, files: List[Dict[str, Any]], thread_id: Optional[int]
    ) -> List[str]:
        os.makedirs(ARTIFACT_DIR, exist_ok=True)
        cur = self.db.cursor()
        ids = []
        for f in files:
            name = f["filename"]
            content = f["content"].encode("utf-8")
            h = hashlib.sha256(content).hexdigest()
            file_id = f"f_{h[:12]}"
            path = os.path.join(ARTIFACT_DIR, file_id + "_" + name)
            with open(path, "wb") as fp:
                fp.write(content)
            uri = "file://" + path
            mime = f.get("mime", "text/plain")
            size_bytes = len(content)
            cur.execute(
                """INSERT INTO files (file_id, filename, mime, size_bytes, storage_uri, hash_sha256, source_thread_id, created_at)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,NOW())
                   ON DUPLICATE KEY UPDATE filename=VALUES(filename), mime=VALUES(mime), size_bytes=VALUES(size_bytes),
                     storage_uri=VALUES(storage_uri), source_thread_id=VALUES(source_thread_id)""",
                (file_id, name, mime, size_bytes, uri, h, thread_id),
            )
            ids.append(file_id)
        self.db.commit()
        return ids

    def create_task(self, conversation_id: int, title: str) -> str:
        task_id = f"t_{uuid.uuid4().hex[:12]}"
        # Optional: insert into a 'tasks' table; for now log system message
        self.add_message(conversation_id, "system", f"[task.created] {task_id} {title}")
        return task_id

    def update_task(
        self, conversation_id: int, task_id: str, status: str, stage: str = ""
    ):
        self.add_message(
            conversation_id, "system", f"[task.{status}] {task_id} {stage}"
        )


# --- add this at top of MemoryStore ---
from email.message import EmailMessage
import mimetypes, hashlib, os, time, uuid
from openai import OpenAI
from rapidfuzz import fuzz, process as rf_process  # pip install rapidfuzz
import mysql.connector as mysql
from typing import List, Optional, Dict, Any, Tuple
import numpy as np

from utils.sem import cosine

ARTIFACT_DIR = os.getenv("ARTIFACT_DIR", "/var/app/artifacts")


class MemoryStore:
    def __init__(self, dsn: Dict[str, Any]):
        self.db = mysql.connect(**dsn)
        # Embeddings/OpenAI client (used by embed())
        self.client = OpenAI(api_key=os.getenv("OPENAI_API_KEY", ""))
        self._current_thread_id = None  # optional: set by agent per request

    # ---------- conversation helpers ----------
    def create_conversation(self, subject_id: str, channel: str) -> int:
        cur = self.db.cursor()
        cur.execute(
            "INSERT INTO conversations (subject_id, channel, created_at, updated_at) VALUES (%s,%s,NOW(),NOW())",
            (subject_id, channel),
        )
        self.db.commit()
        return cur.lastrowid

    def last_message_ts(self, conversation_id: int) -> Optional[float]:
        cur = self.db.cursor()
        cur.execute(
            "SELECT UNIX_TIMESTAMP(MAX(created_at)) FROM messages WHERE conversation_id=%s",
            (conversation_id,),
        )
        row = cur.fetchone()
        return float(row[0]) if row and row[0] is not None else None

    def set_current_thread(self, conversation_id: int) -> None:
        self._current_thread_id = conversation_id

    def current_thread_id(self) -> Optional[int]:
        return self._current_thread_id

    # ---------- message helpers (role-specific wrappers) ----------
    def append_user(self, conversation_id: int, content: str) -> int:
        return self.add_message(conversation_id, "user", content)

    def append_assistant(self, conversation_id: int, content: str) -> int:
        return self.add_message(conversation_id, "assistant", content)

    def append_system(self, conversation_id: int, content: str) -> int:
        return self.add_message(conversation_id, "system", content)

    def add_message(self, conversation_id: int, role: str, content: str) -> int:
        cur = self.db.cursor()
        cur.execute(
            "INSERT INTO messages (conversation_id, role, content, created_at) VALUES (%s,%s,%s,NOW())",
            (conversation_id, role, content),
        )
        self.db.commit()
        return cur.lastrowid

    def last_messages(
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

    # ---------- embeddings / semantic memory ----------
    def embed(self, text: str) -> np.ndarray:
        e = self.client.embeddings.create(model="text-embedding-3-small", input=text)
        return np.array(e.data[0].embedding, dtype=np.float32)

    def add_chunk(
        self, thread_key: Optional[str], kind: str, text: str, store_vector: bool = True
    ):
        tid = self._get_thread_id(thread_key) if thread_key else self._current_thread_id
        cur = self.db.cursor()
        if store_vector:
            vec = self.embed(text).tobytes()
            cur.execute(
                """INSERT INTO memory_chunks (thread_id, kind, text, embedding, created_at)
                   VALUES (%s,%s,%s,%s,NOW())""",
                (tid, kind, text, vec),
            )
        else:
            cur.execute(
                """INSERT INTO memory_chunks (thread_id, kind, text, created_at)
                   VALUES (%s,%s,%s,NOW())""",
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

    # You might already have this; placeholder for completeness
    def _get_thread_id(self, thread_key: str) -> Optional[int]:
        # If you use alphanumeric keys, map to integer id via conversations table
        return self._current_thread_id

    # ---------- contacts (prefilter + ranking) ----------
    def find_contacts_prefilter(
        self, query: str, limit: int = 50
    ) -> List[Dict[str, Any]]:
        q = f"%{query.strip().lower()}%"
        cur = self.db.cursor(dictionary=True)
        cur.execute(
            """SELECT contact_id, display_name, email, org, COALESCE(UNIX_TIMESTAMP(last_interacted_at),0) AS last_seen
               FROM contacts
               WHERE LOWER(display_name) LIKE %s OR LOWER(email) LIKE %s
               ORDER BY last_interacted_at DESC
               LIMIT %s""",
            (q, q, limit),
        )
        return cur.fetchall()

    def rank_contacts(
        self, query: str, candidates: List[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        q = query.strip().lower()

        def score(row):
            base = 0.0
            # exact email
            if row["email"].lower() == q:
                base = 1.0
            # name similarity
            name_sim = (
                fuzz.token_set_ratio(q, (row["display_name"] or "").lower()) / 100.0
            )
            base = max(base, 0.6 * name_sim)
            # recency bias
            rec = row.get("last_seen", 0.0)
            if rec:
                base += 0.05
            return min(base, 1.0)

        scored = [{**r, "score": score(r)} for r in candidates]
        scored.sort(key=lambda r: r["score"], reverse=True)
        return scored

    # ---------- email templating + sending ----------
    def render_email_template(
        self,
        template_key: str,
        variables: Dict[str, Any],
        tone: str = "professional",
        lang: str = "en",
    ) -> Dict[str, str]:
        # Minimal placeholder; swap to Jinja2 later
        subject = variables.get("subject") or f"{template_key.replace('_',' ').title()}"
        summary = variables.get("summary", "")
        bullets = variables.get("bullets", [])
        links = variables.get("links", [])
        body_text = summary + (
            "\n\n" + "\n".join(f"- {b}" for b in bullets) if bullets else ""
        )
        if links:
            body_text += "\n\nLinks:\n" + "\n".join(
                f"- {x.get('title','link')}: {x['url']}" for x in links if "url" in x
            )
        body_html = f"<p>{summary}</p>" + (
            "<ul>" + "".join(f"<li>{b}</li>" for b in bullets) + "</ul>"
            if bullets
            else ""
        )
        if links:
            body_html += (
                "<p><strong>Links:</strong></p><ul>"
                + "".join(
                    f"<li><a href='{x['url']}'>{x.get('title','link')}</a></li>"
                    for x in links
                    if "url" in x
                )
                + "</ul>"
            )
        return {"subject": subject, "body_html": body_html, "body_text": body_text}

    def smtp_send(
        self,
        to: List[str],
        cc: List[str],
        bcc: List[str],
        subject: str,
        body_html: Optional[str],
        body_text: Optional[str],
        attachments: List[Dict[str, str]],
        dry_run: bool = False,
    ) -> Dict[str, Any]:
        msg = EmailMessage()
        msg["Subject"] = subject
        msg["To"] = ", ".join(to)
        if cc:
            msg["Cc"] = ", ".join(cc)
        if body_html:
            msg.add_alternative(body_html, subtype="html")
            if body_text:
                msg.set_content(body_text)
        else:
            msg.set_content(body_text or "")
        # Attachments by file_id
        for att in attachments or []:
            file_id = att.get("file_id")
            if not file_id:
                continue
            file_row = self._get_file(file_id)
            if not file_row:
                continue
            path = file_row["storage_uri"].replace("file://", "")
            ctype, _ = mimetypes.guess_type(path)
            maintype, subtype = (ctype or "application/octet-stream").split("/", 1)
            with open(path, "rb") as f:
                msg.add_attachment(
                    f.read(),
                    maintype=maintype,
                    subtype=subtype,
                    filename=file_row["filename"],
                )
        if dry_run:
            # Store preview
            preview_path = os.path.join(ARTIFACT_DIR, f"preview_{int(time.time())}.eml")
            os.makedirs(os.path.dirname(preview_path), exist_ok=True)
            with open(preview_path, "wb") as f:
                f.write(bytes(msg))
            return {
                "ok": True,
                "provider": "smtp",
                "message_id": None,
                "task_id": None,
                "preview_path": f"file://{preview_path}",
            }
        # Real send: configure SMTP here (left for your env)
        # Example:
        # import smtplib
        # with smtplib.SMTP(os.getenv("SMTP_HOST","localhost"), int(os.getenv("SMTP_PORT","25"))) as s:
        #     s.send_message(msg)
        message_id = f"smtp-{int(time.time())}"
        return {
            "ok": True,
            "provider": "smtp",
            "message_id": message_id,
            "task_id": None,
            "preview_path": None,
        }

    def _get_file(self, file_id: str) -> Optional[Dict[str, Any]]:
        cur = self.db.cursor(dictionary=True)
        cur.execute(
            "SELECT file_id, filename, storage_uri FROM files WHERE file_id=%s",
            (file_id,),
        )
        return cur.fetchone()

    # ---------- artifacts (links/files) ----------
    def save_links(
        self, links: List[Dict[str, Any]], thread_id: Optional[int]
    ) -> List[Dict[str, Any]]:
        cur = self.db.cursor(dictionary=True)
        out = []
        for x in links:
            url = x.get("url")
            title = x.get("title")
            notes = x.get("notes")
            conf = x.get("confidence")
            if not url:
                continue
            cur.execute(
                """INSERT INTO links (url,title,notes,source_thread_id,first_seen,last_seen,confidence)
                   VALUES (%s,%s,%s,%s,NOW(),NOW(),%s)
                   ON DUPLICATE KEY UPDATE title=VALUES(title), notes=VALUES(notes),
                     last_seen=NOW(), confidence=GREATEST(VALUES(confidence), confidence)""",
                (url, title, notes, thread_id, conf),
            )
            out.append({"url": url})
        self.db.commit()
        return out

    def save_files(
        self, files: List[Dict[str, Any]], thread_id: Optional[int]
    ) -> List[str]:
        os.makedirs(ARTIFACT_DIR, exist_ok=True)
        cur = self.db.cursor()
        ids = []
        for f in files:
            name = f["filename"]
            content = f["content"].encode("utf-8")
            h = hashlib.sha256(content).hexdigest()
            file_id = f"f_{h[:12]}"
            path = os.path.join(ARTIFACT_DIR, file_id + "_" + name)
            with open(path, "wb") as fp:
                fp.write(content)
            uri = "file://" + path
            mime = f.get("mime", "text/plain")
            size_bytes = len(content)
            cur.execute(
                """INSERT INTO files (file_id, filename, mime, size_bytes, storage_uri, hash_sha256, source_thread_id, created_at)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,NOW())
                   ON DUPLICATE KEY UPDATE filename=VALUES(filename), mime=VALUES(mime), size_bytes=VALUES(size_bytes),
                     storage_uri=VALUES(storage_uri), source_thread_id=VALUES(source_thread_id)""",
                (file_id, name, mime, size_bytes, uri, h, thread_id),
            )
            ids.append(file_id)
        self.db.commit()
        return ids

    def create_task(self, conversation_id: int, title: str) -> str:
        task_id = f"t_{uuid.uuid4().hex[:12]}"
        # Optional: insert into a 'tasks' table; for now log system message
        self.add_message(conversation_id, "system", f"[task.created] {task_id} {title}")
        return task_id

    def update_task(
        self, conversation_id: int, task_id: str, status: str, stage: str = ""
    ):
        self.add_message(
            conversation_id, "system", f"[task.{status}] {task_id} {stage}"
        )

    def latest_thread_summary(
        self, conversation_id: int, take: int = 8, max_chars: int = 600
    ) -> str:
        """
        Cheap, on-the-fly summary of the last N messages (no model call).
        Keeps it short so it can be used as a system context block.
        """
        cur = self.db.cursor()
        cur.execute(
            """
            SELECT role, content
            FROM messages
            WHERE conversation_id = %s
            ORDER BY id DESC
            LIMIT %s
            """,
            (conversation_id, take),
        )
        rows = list(reversed(cur.fetchall()))
        if not rows:
            return ""

        # Build a terse transcript-style recap
        parts: List[str] = []
        for role, content in rows:
            snippet = (content or "").strip().replace("\n", " ")
            if len(snippet) > 180:
                snippet = snippet[:177] + "…"
            parts.append(f"{role}: {snippet}")

        summary = "Recent context:\n" + "\n".join(parts)
        if len(summary) > max_chars:
            summary = summary[: max_chars - 1] + "…"
        return summary

    def upsert_fact(
        self,
        subject: str,
        k: str,
        v: str,
        confidence: int = 80,
        source: str = "extracted",
    ) -> None:
        cur = self.db.cursor()
        cur.execute(
            """
            INSERT INTO profile_facts (subject, k, v, confidence, source, last_seen)
            VALUES (%s, %s, %s, %s, %s, NOW())
            ON DUPLICATE KEY UPDATE
                v = VALUES(v),
                confidence = GREATEST(confidence, VALUES(confidence)),
                last_seen = NOW()
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
                """
                SELECT k, v, confidence
                FROM profile_facts
                WHERE subject = %s AND k LIKE %s
                ORDER BY confidence DESC, last_seen DESC
                LIMIT %s
                """,
                (subject, keys_like, limit),
            )
        else:
            cur.execute(
                """
                SELECT k, v, confidence
                FROM profile_facts
                WHERE subject = %s
                ORDER BY confidence DESC, last_seen DESC
                LIMIT %s
                """,
                (subject, limit),
            )
        return [{"k": k, "v": v, "confidence": conf} for (k, v, conf) in cur.fetchall()]
