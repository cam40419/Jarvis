"""Fake reviewers cite controller passages that contain asserted fixture evidence."""

import json
import re


def review_passages(prompt, source):
    prefix = {"candidate": "C", "task_context": "T"}[source]
    passages = re.findall(rf"\[({prefix}\d{{4}})\]\n(.*?)\n\[/\1\]\n", prompt, re.S)
    assert passages, f"No numbered {source} passages supplied to reviewer"
    assert len({identifier for identifier, _ in passages}) == len(passages)
    return passages


def review_text(prompt, source):
    return "".join(text for _, text in review_passages(prompt, source))


def reference_check(
    prompt, requirement, *, kind="deliverable", status="satisfied", source="candidate", text=""
):
    evidence = []
    if text:
        passages = review_passages(prompt, source)
        original = "".join(value for _, value in passages)
        # Recorded outputs may themselves be JSON strings. Preserve the exact
        # returned content check; select the latest occurrence (read-back) when
        # the same text also appeared in an earlier write's arguments.
        forms = [text, *(json.dumps(text, ensure_ascii=mode)[1:-1] for mode in (True, False))]
        matching = [(original.rfind(form), form) for form in forms if form in original]
        assert matching, "Reviewer fixture evidence is absent from the supplied source"
        start, matched = max(matching, key=lambda item: item[0])
        end, offset = start + len(matched), 0
        for identifier, value in passages:
            if offset < end and offset + len(value) > start:
                evidence.append({"source": source, "passage_id": identifier})
            offset += len(value)
        assert 1 <= len(evidence) <= 4
    return {
        "requirement": requirement,
        "kind": kind,
        "status": status,
        "evidence": evidence,
    }
