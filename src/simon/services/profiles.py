import re
from collections.abc import Sequence

from simon.config import Settings
from simon.domain.conversations import Message, SubmitRun
from simon.domain.errors import ValidationError
from simon.domain.model import AnswerLength, ProfileSelection, ReasoningEffort
from simon.services.canonical import digest


def request_digest(request: SubmitRun) -> str:
    if (
        request.profile == "auto"
        and request.answer_length == "normal"
        and not request.parent_run_id
        and not request.timezone
    ):
        return digest(request.text)  # Preserve pre-profile idempotency records.
    excluded = {"idempotency_key"} | ({"timezone"} if request.timezone is None else set())
    return digest(request.model_dump(mode="json", exclude=excluded))


def select_profile(
    request: SubmitRun, settings: Settings, history: Sequence[Message] = ()
) -> ProfileSelection:
    """Bounded local routing: no extra provider call or hidden retry. Not a difficulty oracle."""
    text = request.text.lower()
    cues = text
    followup = bool(
        re.match(
            r"\s*(why\b|what about\b|and\b|continue\b|go on\b|try again\b|still\b|"
            r"that (didn't|doesn't|did not)|explain (that|more)|make (it|that))",
            text,
        )
    )
    if followup and len(text) < 300:
        cues += "\n" + "\n".join(m.text for m in history[-8:] if m.role == "user")[-5000:].lower()
    deliberate = bool(
        re.search(
            r"\b(think carefully|think deeply|in[- ]depth|rigorous|step[- ]by[- ]step proof)\b",
            cues,
        )
    )
    hard = bool(
        re.search(
            r"\b(debug|diagnose|root cause|prove|proof|architecture|race condition|deadlock|"
            r"threat model|optimi[sz]e|security review)\b",
            cues,
        )
    )
    comparison = bool(re.search(r"\b(compare|trade[- ]?offs?|pros and cons|evaluate)\b", cues))
    constrained = (
        len(re.findall(r"\b(must|without|under|ensure|constraint|requirement)\b", cues)) >= 3
    )
    code = "```" in cues or "traceback (most recent" in cues
    score = 3 * (deliberate or hard) + 2 * comparison + 2 * constrained + code
    # A long input alone does not justify the larger model.
    score += int(len(cues) > 1600 and score > 0)
    simple = bool(re.fullmatch(r"\s*(hi|hello|hey|thanks|thank you)[!. ]*", text)) or bool(
        re.match(r"\s*(rewrite|translate|rephrase|spell|define|what is|who is|when was)\b", text)
        and len(text) < 600
        and not followup
    )
    selected = request.profile
    reason = "Your response setting"
    effort: ReasoningEffort
    if request.parent_run_id:
        selected, reason = "deep", "Think deeper"
    elif selected == "auto":
        if score >= 3 and settings.auto_deep_enabled:
            selected, reason = "deep", "Complex analysis or multiple constraints"
        elif score >= 2:
            selected, reason = "balanced", "Comparison or analysis needs more reasoning"
        elif simple and score == 0:
            selected, reason = "quick", "A direct answer or simple transformation"
        else:
            selected, reason = "balanced", "Everyday writing, explanation, or planning"
        if followup and history:
            reason += "; includes recent conversation cues"
    effort = "none" if selected == "quick" else "high" if selected == "deep" else "low"
    if selected == "balanced" and request.profile == "auto" and score >= 2:
        effort = "medium"
    length: AnswerLength = "normal"
    if request.answer_length != "auto":
        length = request.answer_length
    elif re.search(r"\b(brief|briefly|concise|one sentence|just the|short answer|tl;?dr)\b", text):
        length = "brief"
    elif re.search(r"\b(detailed|in detail|comprehensive|step[- ]by[- ]step|thorough)\b", text):
        length = "detailed"
    elif selected == "quick":
        length = "brief"
    model = settings.deep_model if selected == "deep" else settings.openai_model
    if model not in {"gpt-5.4-mini", "gpt-5.4"}:
        raise ValidationError("response profiles support gpt-5.4-mini and gpt-5.4")
    return ProfileSelection(
        version="profiles-v2",
        requested=request.profile,
        selected=selected,
        reason=reason,
        answer_length=length,
        reasoning_effort=effort,
    )
