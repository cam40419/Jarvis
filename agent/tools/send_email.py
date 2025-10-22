from typing import Any, Dict, Tuple, List, Optional
import os
from base64 import b64decode
import email_builder

SCHEMA = {
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
                "description": "Email subject line. Create one if not provided explicitly. Clean and legible.",
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
                            "description": "e.g., 'report.pdf'",
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


def execute(args: Dict[str, Any]) -> Tuple[Dict[str, Any], bool]:
    try:
        # Required args
        to: List[str] = args["to"]
        subject: str = args["subject"]
        html_body: str = args["html_body"]

        # Optionals
        cc: List[str] = args.get("cc") or []
        bcc: List[str] = args.get("bcc") or []
        attachments: List[Dict[str, str]] = args.get("attachments") or []

        # Secrets from environment (avoid hardcoding)
        sender_email = os.getenv("GMAIL")
        gmail_password = os.getenv("GMAIL_APP_PASSWORD")
        if not sender_email or not gmail_password:
            return (
                {
                    "error": "Missing credentials. Set GMAIL and GMAIL_APP_PASSWORD environment variables."
                },
                False,
            )

        # Initialize Gmail client
        client = email_builder.clients.Gmail(password=gmail_password)

        # Build message
        msg = (
            email_builder.Email().sender(sender_email).subject(subject).html(html_body)
        )
        for r in to:
            msg = msg.to(r)
        for r in cc:
            msg = msg.cc(r)
        for r in bcc:
            msg = msg.bcc(r)

        # Attachments (try common method names)
        for a in attachments:
            filename = a["filename"]
            content_b64 = a["content_base64"]
            content_bytes = b64decode(content_b64)

            attached = False
            # Common method signatures across different builders
            if hasattr(msg, "attach"):
                try:
                    msg = msg.attach(filename, content_bytes)  # (filename, bytes)
                    attached = True
                except TypeError:
                    try:
                        msg = msg.attach(
                            content_bytes, filename=filename
                        )  # (bytes, filename=)
                        attached = True
                    except TypeError:
                        pass
            if not attached and hasattr(msg, "attachment"):
                try:
                    msg = msg.attachment(filename, content_bytes)
                    attached = True
                except TypeError:
                    pass
            if not attached and hasattr(msg, "attach_bytes"):
                try:
                    msg = msg.attach_bytes(content_bytes, filename=filename)
                    attached = True
                except TypeError:
                    pass

            if not attached:
                return (
                    {
                        "error": f"Attachment method not supported by email builder for '{filename}'.",
                        "hint": "Provide an attachment method compatible with your email builder.",
                    },
                    False,
                )

        # Send
        result = msg.send(client)

        # Normalize return
        if isinstance(result, dict):
            return (result, True)
        return ({"status": "sent", "result": str(result)}, True)

    except KeyError as e:
        return ({"error": f"Missing required field: {e.args[0]}"}, False)
    except Exception as e:
        return ({"error": f"send_email failed: {e}"}, False)
