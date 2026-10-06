"""Bounded public-source requirements for project delegation, never new grants."""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from simon.domain.errors import ValidationError
from simon.domain.project_coordination import LeadDecision
from simon.domain.tool_catalog import ToolDefinition

_LOCAL_ONLY = re.compile(
    r"\b(?:only|solely|exclusively)\s+(?:use|using|from|based on)\s+(?:the\s+)?"
    r"(?:saved|provided|existing|attached|project|local)\b|"
    r"\b(?:using|from)\s+only\s+(?:the\s+)?(?:saved|provided|existing|attached|project|local)\b",
    re.I,
)
_PUBLIC = re.compile(r"\b(?:web|internet|online|public[- ]web|public sources?)\b", re.I)
_RESEARCH = re.compile(
    r"\b(?:research|investigat\w*|search|find|identify|discover|look(?:ing)?\s+(?:up|for)|"
    r"dig(?:ging)?\s+into|assess|compar\w*|verify)\b",
    re.I,
)
_MARKET = re.compile(
    r"\b(?:suppliers?|manufacturers?|competitors?|competition|other brands|similar brands)\b",
    re.I,
)
_DISCOVERY = re.compile(
    r"\b(?:search|find|identify|discover|look(?:ing)?\s+(?:up|for)|dig(?:ging)?\s+into)\b",
    re.I,
)
_SAVED_RESEARCH = re.compile(
    r"\b(?:saved|provided|existing|attached|previous|prior)\b[^.!?\n]{0,80}"
    r"\b(?:research|reports?|documents?|findings)\b",
    re.I,
)
_ARTIFACT_EXTENSION = (
    r"(?:md|markdown|txt|rst|pdf|docx?|odt|rtf|csv|tsv|xlsx?|ods|json|jsonl|ya?ml|"
    r"html?|xml|png|jpe?g|svg|webp|pptx?|zip|py|js|jsx|ts|tsx|css|sh|ps1|sql)"
)
_ARTIFACT_REFERENCE = re.compile(
    # Match URLs first so a document URL is never mistaken for a local filename.
    r"https?://[^\s`<>\"']+|"
    # Quoted paths may contain spaces after an identifiable directory component;
    # a quoted natural-language instruction is not itself an artifact reference.
    r"(?P<quote>[`\"'])(?:[A-Z]:[\\/]|/)?[\w.-]+[/\\]"
    r"[^`<>\"'|\r\n]+?\." + _ARTIFACT_EXTENSION + r"(?P=quote)|"
    r"(?<![\w/\\])(?:[A-Z]:[\\/])?"
    r"[^\s`<>\"'|()\[\]{},;:!?]+\." + _ARTIFACT_EXTENSION + r"(?=$|[\s`<>\"'.,;:!?()\[\]{}])|"
    # Explicit relative/absolute paths and backtick-delimited path atoms can
    # identify extensionless artifacts too. Bare suppliers/competitors stays prose.
    r"`[^\s`<>\"'|]+[/\\][^\s`<>\"'|]+`|"
    r"(?<![\w:/\\])(?:[A-Z]:[\\/]|\.{1,2}/|~/|/)"
    r"[\w.-]+(?:[/\\][\w.-]+)*",
    re.I,
)


def _without_local_artifact_references(text: str) -> str:
    return _ARTIFACT_REFERENCE.sub(
        # Keep the URL signal for explicit reading and known-source work, but not
        # words in its path/query that might look like additional instructions.
        lambda match: (
            "https://source.invalid/"
            if re.match(r"https?://", match[0].strip("`"), re.I)
            else " local_artifact "
        ),
        text,
    )


def research_requirements(text: str) -> frozenset[str]:
    """Recognize explicit outside-source work, not generic research/document editing.

    This is a conservative deterministic backstop to the lead's instructions. It
    does not classify arbitrary prose or treat a saved document named 'research'
    as permission to browse. The full request remains authoritative for the model.
    """
    # Remove reference tokens before sentence splitting: a path such as
    # research/competition.md names an artifact, not a public research assignment.
    # Retain the surrounding actions and URLs so genuine source work still counts.
    text = _without_local_artifact_references(text)
    if _LOCAL_ONLY.search(text):
        return frozenset()
    requirements: set[str] = set()
    if re.search(r"\b(?:read|inspect|verify|assess)\b[^\n]{0,120}https?://", text, re.I):
        requirements.add("read")
    # Keep source/action matches within one sentence rather than mixing unrelated
    # instructions (for example, a website deliverable and local file research).
    for sentence in re.split(r"[.!?\n]+(?!\w*/)", text):
        if not _RESEARCH.search(sentence) and not (
            _PUBLIC.search(sentence) and re.search(r"\bread\b", sentence, re.I)
        ):
            continue
        if _SAVED_RESEARCH.search(sentence) and not _DISCOVERY.search(sentence):
            continue
        if _PUBLIC.search(sentence):
            requirements.add("read")
            if _DISCOVERY.search(sentence) or (
                re.search(r"\bresearch\b", sentence, re.I)
                and not re.search(r"https?://", sentence, re.I)
            ):
                requirements.add("search")
        elif _MARKET.search(sentence):
            # Finding suppliers or investigating other brands is outside-source
            # research even when the request does not spell out 'internet'.
            requirements.update(("search", "read"))
    return frozenset(requirements)


def public_web_capabilities(tool: ToolDefinition) -> tuple[str, ...]:
    """Use exact operator declarations; Drive/file search and screenshots do not qualify."""
    capabilities = set()
    if tool.id == "browser.read" or "web.read" in tool.capabilities:
        capabilities.add("read")
    if "web.search" in tool.capabilities:
        capabilities.add("search")
    return tuple(sorted(capabilities))


def _tool_capabilities(roster: list[dict[str, Any]]) -> dict[str, dict[str, frozenset[str]]]:
    return {
        member["agent_id"]: {
            tool["id"]: frozenset(tool.get("public_web", ())) for tool in member["ready_tools"]
        }
        for member in roster
    }


def research_blocker(instruction: str, roster: list[dict[str, Any]]) -> str | None:
    required = research_requirements(instruction)
    supplied = {
        capability
        for tools in _tool_capabilities(roster).values()
        for capabilities in tools.values()
        for capability in capabilities
    }
    missing = required - supplied
    if not missing:
        return None
    labels = {"search": "public-web search", "read": "public-web page reading"}
    return (
        "This request needs "
        + " and ".join(labels[key] for key in sorted(missing))
        + ", but no project team member currently has that capability ready. "
        "Enable/configure it and select it for a team member before retrying. "
        "Drive and project-file search do not search public websites."
    )


def validate_research_plan(
    instruction: str,
    decision: LeadDecision,
    roster: list[dict[str, Any]],
    *,
    objective_overrides: Mapping[str, str] | None = None,
) -> None:
    """Stop unsupported source work before execution, including tool-free 'complete'."""
    if decision.status == "waiting":
        return
    required = research_requirements(instruction)
    tasks_require = {
        task.id: research_requirements(
            task.title + ". " + (objective_overrides or {}).get(task.id, task.objective)
        )
        for task in decision.tasks
    }
    if not required and not any(tasks_require.values()):
        return
    blocker = research_blocker(instruction, roster)
    if blocker:
        raise ValidationError(blocker)
    tools = _tool_capabilities(roster)
    own = {
        task.id: frozenset(
            capability
            for identifier in task.tool_ids
            for capability in tools.get(task.agent_id, {}).get(identifier, ())
        )
        for task in decision.tasks
    }
    supplied = frozenset(capability for values in own.values() for capability in values)
    if required - supplied:
        raise ValidationError(
            "The plan omits the public-web research this request requires. Assign search and "
            "source reading to a member with those ready skills; project files alone are "
            "insufficient. No research tasks have been started."
        )
    lookup = {task.id: task for task in decision.tasks}

    def inherited(identifier: str) -> frozenset[str]:
        # LeadDecision has already checked dependency membership and acyclicity.
        return own[identifier] | frozenset(
            capability
            for predecessor in lookup[identifier].depends_on
            for capability in inherited(predecessor)
        )

    for task in decision.tasks:
        if tasks_require[task.id] - inherited(task.id):
            raise ValidationError(
                f"The task '{task.title}' requires public-web research, but its assigned "
                "member/tools and prerequisite tasks do not supply it. Assign it to a "
                "member with ready web skills or add a real research prerequisite."
            )
