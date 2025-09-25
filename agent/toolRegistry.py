# agent/tool_registry.py
from __future__ import annotations
from dataclasses import dataclass
from typing import Any, Dict, List, Callable, Optional
from agent.memory import MemoryStore


@dataclass
class ToolCall:
    name: str
    call_id: str
    arguments: Dict[str, Any]

    @classmethod
    def from_event(cls, event) -> "ToolCall":
        # normalize SDK event → ToolCall
        return cls(
            name=getattr(event, "name", event.data.get("name")),
            call_id=getattr(event, "id", event.data.get("id")),
            arguments=getattr(event, "arguments", event.data.get("arguments", {})),
        )


@dataclass
class ToolResult:
    content: Dict[str, Any]


class ToolRegistry:
    def __init__(self, memory: MemoryStore):
        self.memory = memory
        self._handlers: Dict[str, Callable[[Dict[str, Any]], ToolResult]] = {
            "contacts_lookup": self._contacts_lookup,
            "email_draft": self._email_draft,
            "email_send_local": self._email_send_local,
            "artifacts_save_links": self._save_links,
            "artifacts_save_files": self._save_files,
        }

    # ----- Expose schemas to OpenAI -----
    def schemas(self) -> List[Dict[str, Any]]:
        return [
            # contacts.lookup
            {
                "type": "function",
                "name": "contacts_lookup",
                "description": "Find contacts by name or email from the user's address book.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string"},
                        "limit": {
                            "type": "integer",
                            "minimum": 1,
                            "maximum": 10,
                            "default": 5,
                        },
                    },
                    "required": ["query"],
                },
            },
            # email.draft
            {
                "type": "function",
                "name": "email_draft",
                "description": "Create an email draft from a template and variables.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "to": {
                            "type": "array",
                            "items": {"type": "string", "format": "email"},
                        },
                        "cc": {
                            "type": "array",
                            "items": {"type": "string", "format": "email"},
                            "default": [],
                        },
                        "bcc": {
                            "type": "array",
                            "items": {"type": "string", "format": "email"},
                            "default": [],
                        },
                        "template_key": {"type": "string"},
                        "variables": {
                            "type": "object",
                            "additionalProperties": {
                                "type": ["string", "number", "boolean"]
                            },
                        },
                        "tone": {
                            "type": "string",
                            "enum": ["friendly", "professional", "direct"],
                            "default": "professional",
                        },
                        "lang": {"type": "string", "default": "en"},
                    },
                    "required": ["to", "template_key"],
                },
            },
            # email.send_local
            {
                "type": "function",
                "name": "email_send_local",
                "description": "Send an email using local SMTP with optional attachments.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "to": {
                            "type": "array",
                            "items": {"type": "string", "format": "email"},
                        },
                        "cc": {
                            "type": "array",
                            "items": {"type": "string", "format": "email"},
                            "default": [],
                        },
                        "bcc": {
                            "type": "array",
                            "items": {"type": "string", "format": "email"},
                            "default": [],
                        },
                        "subject": {"type": "string"},
                        "body_html": {"type": "string"},
                        "body_text": {"type": "string"},
                        "attachments": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {"file_id": {"type": "string"}},
                            },
                            "default": [],
                        },
                        "dry_run": {"type": "boolean", "default": False},
                    },
                    "required": ["to", "subject"],
                },
            },
            # artifacts.save_links
            {
                "type": "function",
                "name": "artifacts_save_links",
                "description": "Persist a list of links (title, url, notes, confidence) for later reuse.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "links": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "title": {"type": "string"},
                                    "url": {"type": "string", "format": "uri"},
                                    "notes": {"type": "string"},
                                    "confidence": {"type": "number"},
                                },
                                "required": ["url"],
                            },
                        }
                    },
                    "required": ["links"],
                },
            },
            # artifacts.save_files
            {
                "type": "function",
                "name": "artifacts_save_files",
                "description": "Store generated files (markdown/attachments) and return file_ids.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "files": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "filename": {"type": "string"},
                                    "mime": {"type": "string"},
                                    "content": {"type": "string"},
                                },
                                "required": ["filename", "content"],
                            },
                        }
                    },
                    "required": ["files"],
                },
            },
        ]

    # ----- Dispatch from Agent stream -----
    def dispatch(self, call: ToolCall) -> ToolResult:
        handler = self._handlers.get(call.name)
        if not handler:
            return ToolResult(
                content={"ok": False, "error": f"Unknown tool {call.name}"}
            )
        return handler(call.arguments)

    # ====== TOOL HANDLERS (fill in DB, SMTP, templating) ======

    # contacts.lookup → smart search in your MemoryStore/DB
    def _contacts_lookup(self, args: Dict[str, Any]) -> ToolResult:
        query = args["query"]
        limit = int(args.get("limit", 5))
        # 1) prefilter via DB LIKE on normalized columns
        candidates = self.memory.find_contacts_prefilter(query, limit=50)
        # 2) score & sort (rapidfuzz or your custom scorer)
        results = self.memory.rank_contacts(query, candidates)[:limit]
        return ToolResult(content={"contacts": results})

    # email.draft → render via your template system (e.g., Jinja2)
    def _email_draft(self, args: Dict[str, Any]) -> ToolResult:
        to = args["to"]
        cc = args.get("cc", [])
        bcc = args.get("bcc", [])
        tpl_key = args["template_key"]
        variables = args.get("variables", {})
        tone = args.get("tone", "professional")
        lang = args.get("lang", "en")
        draft = self.memory.render_email_template(
            tpl_key, variables, tone=tone, lang=lang
        )
        draft.update({"to": to, "cc": cc, "bcc": bcc})
        return ToolResult(content={"draft": draft})

    # email.send_local → local SMTP dispatch
    def _email_send_local(self, args: Dict[str, Any]) -> ToolResult:
        res = self.memory.smtp_send(
            to=args["to"],
            cc=args.get("cc", []),
            bcc=args.get("bcc", []),
            subject=args["subject"],
            body_html=args.get("body_html"),
            body_text=args.get("body_text"),
            attachments=args.get("attachments", []),
            dry_run=bool(args.get("dry_run", False)),
        )
        return ToolResult(content=res)

    # artifacts.save_links → upsert links into DB
    def _save_links(self, args: Dict[str, Any]) -> ToolResult:
        saved = self.memory.save_links(
            args["links"], thread_id=self.memory.current_thread_id()
        )
        return ToolResult(content={"saved_links": saved})

    # artifacts.save_files → write files to disk + DB; return file_ids
    def _save_files(self, args: Dict[str, Any]) -> ToolResult:
        files = args["files"]
        saved = self.memory.save_files(files, thread_id=self.memory.current_thread_id())
        return ToolResult(content={"file_ids": saved})
