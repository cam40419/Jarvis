import pytest

from simon.config import Settings
from simon.domain.conversations import SubmitRun
from simon.domain.errors import ValidationError
from simon.services.profiles import request_digest, select_profile


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Rewrite this paragraph", "quick"),
        ("Hello", "quick"),
        ("Plan dinner for six people", "balanced"),
        ("Debug a difficult issue", "deep"),
    ],
)
def test_auto_defaults_choose_effort_for_task(text, expected):
    request = SubmitRun(text=text, idempotency_key="profiles-001")
    assert select_profile(request, Settings()).selected == expected


def test_manual_selection_length_and_auto_deep():
    request = SubmitRun(
        text="Debug this issue",
        profile="quick",
        answer_length="detailed",
        idempotency_key="profiles-001",
    )
    assert select_profile(request, Settings(auto_deep_enabled=True)).selected == "quick"
    auto = request.model_copy(update={"profile": "auto"})
    assert select_profile(auto, Settings(auto_deep_enabled=True)).selected == "deep"
    assert select_profile(auto, Settings()).answer_length == "detailed"
    with pytest.raises(ValidationError, match="profiles support"):
        select_profile(request, Settings(openai_model="unsupported-model"))


def test_idempotency_covers_profile_and_length_preserves_legacy():
    from simon.services.canonical import digest

    request = SubmitRun(text="Hello", idempotency_key="profiles-001")
    assert request_digest(request) == digest("Hello")
    assert request_digest(request) != request_digest(request.model_copy(update={"profile": "deep"}))
    assert request_digest(request) != request_digest(
        request.model_copy(update={"answer_length": "brief"})
    )


@pytest.mark.parametrize(
    ("text", "profile", "effort", "length"),
    [
        ("Hello!", "quick", "none", "brief"),
        ("What is the capital of France?", "quick", "none", "brief"),
        ("Help me plan dinner for six", "balanced", "low", "normal"),
        ("Compare two ways to organize my notes", "balanced", "medium", "normal"),
        ("Diagnose this intermittent deadlock", "deep", "high", "normal"),
        ("Think carefully about the tradeoffs. Keep it brief.", "deep", "high", "brief"),
        ("Explain photosynthesis in detail", "balanced", "low", "detailed"),
        (
            "Plan a trip under $500, without a car, and ensure we can walk",
            "balanced",
            "medium",
            "normal",
        ),
        ("Hello, debug this race condition", "deep", "high", "normal"),
        ("Translate: " + "some text " * 190, "balanced", "low", "normal"),
    ],
)
def test_automatic_policy_examples(text, profile, effort, length):
    chosen = select_profile(
        SubmitRun(text=text, answer_length="auto", idempotency_key="automatic-routing"),
        Settings(_env_file=None),
    )
    assert (chosen.selected, chosen.reasoning_effort, chosen.answer_length) == (
        profile,
        effort,
        length,
    )
    assert chosen.version == "profiles-v2"


def test_followup_uses_recent_user_context_and_honors_deep_limit():
    from uuid import uuid4

    from simon.domain.conversations import Message

    prior = Message(
        thread_id=uuid4(), sequence=1, role="user", text="Debug an intermittent deadlock"
    )
    followup = SubmitRun(text="Still broken. Try again.", idempotency_key="followup-routing")
    chosen = select_profile(followup, Settings(), [prior])
    assert chosen.selected == "deep" and "recent conversation" in chosen.reason
    limited = select_profile(followup, Settings(auto_deep_enabled=False), [prior])
    assert limited.selected == "balanced" and limited.reasoning_effort == "medium"
    unrelated = followup.model_copy(update={"text": "Hello"})
    assert select_profile(unrelated, Settings(), [prior]).selected == "quick"
    explicit = followup.model_copy(update={"profile": "quick"})
    assert select_profile(explicit, Settings(), [prior]).reasoning_effort == "none"
