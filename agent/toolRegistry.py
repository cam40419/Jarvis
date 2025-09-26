# agent/tool_registry.py
from __future__ import annotations
import base64
from dataclasses import dataclass
from typing import Any, Dict, List, Callable, Optional, Literal
from agent.memory import MemoryStore

import email_builder


@dataclass
class ToolResult:
    content: Dict[str, Any]


class ToolRegistry:
    def __init__(self, memory: MemoryStore):
        self.memory = memory
        self.handlers: Dict[str, Callable[[Dict[str, Any]], ToolResult]] = {
            "send_email": self.send_email,
        }

    def get_tools(self) -> List[Dict[str, Any]]:
        return [
            # send_email
            {
                "name": "send_email",
                "description": "Send an email via Gmail with HTML body, optional CC/BCC, and optional file attachments (base64-encoded).",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "to": {
                            "type": "array",
                            "description": "Primary recipient email addresses.",
                            "items": {"type": "string", "format": "email"},
                            "minItems": 1,
                        },
                        "subject": {
                            "type": "string",
                            "description": "Email subject line.",
                        },
                        "html_body": {
                            "type": "string",
                            "description": "HTML content of the email body.",
                        },
                        "cc": {
                            "type": "array",
                            "description": "CC recipient email addresses.",
                            "items": {"type": "string", "format": "email"},
                        },
                        "bcc": {
                            "type": "array",
                            "description": "BCC recipient email addresses.",
                            "items": {"type": "string", "format": "email"},
                        },
                        "attachments": {
                            "type": "array",
                            "description": "List of attachments to include. Content must be base64-encoded bytes.",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "filename": {
                                        "type": "string",
                                        "description": "The filename as it should appear to the recipient (e.g., 'report.pdf').",
                                    },
                                    "content_base64": {
                                        "type": "string",
                                        "description": "Base64-encoded file content.",
                                    },
                                },
                                "required": ["filename", "content_base64"],
                                "additionalProperties": False,
                            },
                        },
                    },
                    "required": ["to", "subject", "html_body"],
                    "additionalProperties": False,
                },
                "strict": True,
            }
        ]

    def send_email(
        *,
        to: List[str],
        subject: str,
        html_body: str,
        cc: Optional[List[str]] = None,
        bcc: Optional[List[str]] = None,
        attachments: Optional[List[Dict[str, str]]] = None,
    ) -> Dict[str, str]:

        sender_email = "cam40419@gmail.com"
        gmail_password = "Ebye4kyq!"

        client = email_builder.clients.Gmail(password=gmail_password)

        # Build email
        msg = (
            email_builder.Email().sender(sender_email).subject(subject).html(html_body)
        )

        # Recipients
        for r in to or []:
            msg = msg.to(r)
        for r in cc or []:
            msg = msg.cc(r)
        for r in bcc or []:
            msg = msg.bcc(r)

        # Attachments
        if attachments:
            for a in attachments:
                print("to be implemented")

        # Send
        return msg.send(client)
