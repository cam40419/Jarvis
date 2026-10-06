"""Recover presentation structure from retained plain-text report outlines.

Only heading markers are added. Original prose, figures and source URLs are
preserved; this is not a content summarizer or a source-verification step.
"""

import re

_REPORT_TITLE = re.compile(r"\b(?:report|memo|brief|strategy|assessment|analysis)\b", re.I)
_MARKDOWN_HEADING = re.compile(r"^\s{0,3}#{1,6}\s+", re.M)
_NUMBERED_SECTION = re.compile(r"^\d+\)\s+\S")
_CLOSING_SECTION = re.compile(
    r"^(?:cross[- ]|.*\b(?:summary|notes|limitations|recommendations|conclusions?)\b|bottom line)",
    re.I,
)


def prepare_report(markdown: str) -> str:
    lines = markdown.splitlines()
    first = next((index for index, line in enumerate(lines) if line.strip()), None)
    if first is None:
        return markdown
    title = lines[first].strip()
    if (
        len(title) > 160
        or not _REPORT_TITLE.search(title)
        or title.startswith(("#", "-", "*", ">", "`", "|", "["))
        or title.endswith((".", "?", "!", ":", ";"))
    ):
        return markdown
    already_structured = bool(_MARKDOWN_HEADING.search(markdown))
    lines[first] = "# " + title
    if not already_structured:
        in_numbered_section = False
        fence = ""
        for index in range(first + 1, len(lines)):
            text = lines[index].strip()
            if text.startswith(("```", "~~~")):
                marker = text[:3]
                fence = "" if fence == marker else fence or marker
                continue
            if (
                fence
                or not text
                or len(text) > 90
                or len(text.split()) > 12
                or lines[index - 1].strip()
                or text.startswith(("#", "-", "*", ">", "`", "|", "[", "<"))
                or text.endswith((".", "?", "!", ":", ";"))
                or "http" in text.lower()
                or re.match(r"^\d+\.\s", text)
            ):
                continue
            if _NUMBERED_SECTION.match(text):
                level = 2
                in_numbered_section = True
            elif _CLOSING_SECTION.match(text):
                level = 2
                in_numbered_section = False
            else:
                level = 3 if in_numbered_section else 2
            lines[index] = "#" * level + " " + text
    return "\n".join(lines) + ("\n" if markdown.endswith("\n") else "")
