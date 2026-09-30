"""Bounded plain-text extraction for Gmail MIME payloads; never fetch remote content."""

import base64
from email.message import EmailMessage
from html.parser import HTMLParser
from typing import Any


class PlainHTML(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.hidden = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"script", "style"}:
            self.hidden += 1
        if tag in {"p", "div", "br", "li", "tr"}:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style"}:
            self.hidden = max(0, self.hidden - 1)

    def handle_data(self, data: str) -> None:
        if not self.hidden:
            self.parts.append(data)


def message_text(payload: dict[str, Any], depth: int = 0) -> str:
    if depth > 12 or payload.get("filename"):
        return ""
    mime = payload.get("mimeType", "")
    if mime.startswith("multipart/"):
        parts = payload.get("parts", [])[:50]
        if mime == "multipart/alternative":
            parts = sorted(parts, key=lambda part: part.get("mimeType") != "text/plain")
            for part in parts:
                text = message_text(part, depth + 1)
                if text:
                    return text
            return ""
        return "\n".join(message_text(part, depth + 1) for part in parts)
    if mime not in {"text/plain", "text/html"}:
        return ""
    encoded = payload.get("body", {}).get("data", "")
    if not encoded:
        return ""
    message = EmailMessage()
    for header in payload.get("headers", []):
        if header.get("name", "").lower() == "content-type":
            message["Content-Type"] = header["value"]
            break
    raw = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
    try:
        text = raw.decode(message.get_content_charset() or "utf-8", errors="replace")
    except LookupError:
        text = raw.decode("utf-8", errors="replace")
    if mime == "text/html":
        parser = PlainHTML()
        parser.feed(text)
        text = "".join(parser.parts)
    return text.strip()
