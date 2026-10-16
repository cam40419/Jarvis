"""Bounded selectors and dependency authority; no model inference or file execution."""

import base64
import json
from typing import Any
from uuid import UUID

from simon.domain.errors import AuthorizationError, InvalidTransitionError, ValidationError
from simon.domain.native_dependencies import (
    DependencySelector,
    NativeFactRevision,
    canonical_bytes,
    semantic_hash,
    snapshot_hash,
)
from simon.domain.ports import Store

MAX_FACTS = 500
MAX_OUTPUTS = 200
MAX_GRAPH_VISITS = 6400


def strict_json(text: str) -> Any:
    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in items:
            if key in result:
                raise ValueError("Duplicate JSON object key.")
            result[key] = value
        return result

    def constant(value: str) -> Any:
        raise ValueError("Non-finite JSON number: " + value)

    try:
        value = json.loads(text, object_pairs_hook=pairs, parse_constant=constant)
        canonical_bytes(value)
        return value
    except (ValueError, TypeError, RecursionError) as exc:
        raise ValidationError("Selectors require complete, unambiguous finite JSON.") from exc


def tokens(selector: DependencySelector) -> list[str]:
    return [part.replace("~1", "/").replace("~0", "~") for part in selector.path.split("/")[1:]]


def contains(parent: DependencySelector, child: DependencySelector) -> bool:
    if parent.kind == "whole_file":
        return True
    if child.kind == "whole_file":
        return False
    left, right = tokens(parent), tokens(child)
    return right[: len(left)] == left


def overlaps(left: DependencySelector, right: DependencySelector) -> bool:
    return contains(left, right) or contains(right, left)


def select(value: Any, selector: DependencySelector) -> Any:
    if selector.kind == "whole_file":
        return value
    try:
        for token in tokens(selector):
            if isinstance(value, list):
                if not token.isascii() or not token.isdecimal() or str(int(token)) != token:
                    raise ValueError("Invalid array index.")
                value = value[int(token)]
            elif isinstance(value, dict):
                value = value[token]
            else:
                raise ValueError("Selector traverses a scalar.")
        return value
    except (KeyError, IndexError, ValueError) as exc:
        raise ValidationError("Dependency selector does not identify an existing value.") from exc


def artifact_text(revision: Any) -> str:
    try:
        return base64.b64decode(revision.content_base64).decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValidationError("Selective repair supports UTF-8 text and JSON outputs.") from exc


def artifact_value(revision: Any, selector: DependencySelector) -> Any:
    text = artifact_text(revision)
    return select(strict_json(text), selector) if selector.kind == "json_pointer" else text


def fact_value(revision: NativeFactRevision, selector: DependencySelector) -> str:
    value = select(revision.value, selector)
    if selector.kind == "whole_file":
        return semantic_hash(revision.value_type, value, revision.unit)
    return semantic_hash(
        "json", {"declared_type": revision.value_type, "selected": value}, revision.unit
    )


class DependencyAuthority:
    def __init__(self, store: Store) -> None:
        self.store = store

    def facts(self, ws: UUID, pid: UUID) -> list[Any]:
        values = self.store.facts(ws, pid, limit=MAX_FACTS + 1)
        if len(values) > MAX_FACTS:
            raise ValidationError("Project fact capacity exceeded; authority is incomplete.")
        return list(values)

    def outputs(self, ws: UUID, pid: UUID) -> list[Any]:
        values = self.store.artifacts(ws, pid, limit=MAX_OUTPUTS + 1)
        if len(values) > MAX_OUTPUTS:
            raise ValidationError("Selective repair supports at most 200 project outputs.")
        return list(values)

    def sources(self, ws: UUID, pid: UUID) -> list[Any]:
        values = self.store.native_intake_sources(ws, pid)
        if len(values) > 2000:
            raise ValidationError("Source authority capacity exceeded.")
        return list(values)

    def source_fingerprint(self, ws: UUID, pid: UUID) -> str:
        return snapshot_hash([source.model_dump(mode="json") for source in self.sources(ws, pid)])

    def assert_disclosable(self, ws: UUID, pid: UUID, snapshot: dict[str, Any]) -> None:
        for identifier in snapshot.get("source_ids", []):
            source = self.store.native_intake_source(ws, pid, UUID(str(identifier)))
            if source is None or source.revoked_at is not None:
                raise AuthorizationError("Dependency evidence is no longer available.")

    def fact_sources(self, ws: UUID, pid: UUID, revision: NativeFactRevision) -> set[str]:
        source_ids: set[str] = set()
        for binding in revision.sources:
            source = self.store.native_intake_source(ws, pid, binding.source_revision_id)
            if source is None or source.sha256 != binding.sha256 or source.revoked_at is not None:
                raise AuthorizationError("Fact evidence is no longer available.")
            source_ids.add(str(source.id))
        return source_ids

    def fact_ready(
        self, ws: UUID, pid: UUID, revision: NativeFactRevision
    ) -> tuple[bool, set[str]]:
        ids = self.fact_sources(ws, pid, revision)
        sources = self.sources(ws, pid)
        ready = True
        for binding in revision.sources:
            original = self.store.native_intake_source(ws, pid, binding.source_revision_id)
            assert original is not None
            latest = max(
                (source for source in sources if source.source_key == original.source_key),
                key=lambda source: source.revision,
            )
            ids.add(str(latest.id))
            if latest.revoked_at is not None:
                raise AuthorizationError("Current fact evidence has been revoked.")
            if binding.selector.kind == "whole_file":
                ready = ready and latest.sha256 == binding.sha256
            else:
                try:
                    self.validate_source_value(latest, binding.selector, revision)
                except ValidationError:
                    ready = False
        return ready, ids

    @staticmethod
    def validate_source_value(source: Any, selector: DependencySelector, revision: Any) -> None:
        if selector.kind == "whole_file":
            return
        if source.extraction_status != "text" or source.truncated or source.redactions:
            raise ValidationError("JSON provenance requires complete, unredacted source text.")
        value = select(strict_json(source.text), selector)
        if semantic_hash("json", value) != semantic_hash("json", revision.value):
            raise ValidationError("The selected source value conflicts with the typed fact.")

    def assert_revision_disclosable(self, ws: UUID, pid: UUID, revision_id: UUID) -> None:
        pending = [revision_id]
        visited: set[UUID] = set()
        visits = 0
        while pending:
            identifier = pending.pop()
            if identifier in visited:
                continue
            visited.add(identifier)
            declarations = self.store.dependency_declarations(ws, pid, identifier, limit=101)
            if len(declarations) > 100:
                raise ValidationError("Dependency history capacity exceeded.")
            for declaration in declarations:
                for edge in declaration.dependencies:
                    visits += 1
                    if visits > MAX_GRAPH_VISITS:
                        raise ValidationError(
                            "Dependency evidence graph exceeds its traversal bound."
                        )
                    if edge.input_kind == "artifact":
                        pending.append(edge.revision_id)
                    else:
                        fact = self.store.fact_revision(ws, pid, edge.revision_id)
                        if fact is None:
                            raise AuthorizationError("Fact evidence is unavailable.")
                        self.fact_sources(ws, pid, fact)

    def snapshot(self, ws: UUID, pid: UUID, revision_id: UUID) -> dict[str, Any] | None:
        root = self.store.dependency_declaration(ws, pid, revision_id)
        facts = self.facts(ws, pid)
        if root is None and not facts:
            return None
        self.assert_revision_disclosable(ws, pid, revision_id)
        coverage = root.coverage if root else "unknown"
        reasons: set[str] = set()
        source_ids: set[str] = set()
        signatures: dict[str, Any] = {}
        review_facts: dict[str, Any] = {}
        ready = True
        visited: set[tuple[UUID, str, str]] = set()
        pending = [(revision_id, DependencySelector())]
        while pending:
            rid, region = pending.pop()
            visit = (rid, region.kind, region.path)
            if visit in visited:
                continue
            visited.add(visit)
            if len(visited) > MAX_GRAPH_VISITS:
                raise ValidationError("Dependency graph exceeds its traversal bound.")
            declaration = self.store.dependency_declaration(ws, pid, rid)
            if declaration is None or declaration.coverage != "complete":
                reasons.add("Dependency coverage is incomplete; conservative project checks apply.")
                if rid != revision_id and coverage == "complete":
                    ready = False
            if declaration is None:
                continue
            for edge in declaration.dependencies:
                if not overlaps(edge.output_selector, region):
                    continue
                shape = {
                    "output": str(declaration.artifact_id),
                    "input_kind": edge.input_kind,
                    "input_id": str(edge.input_id),
                    "input_selector": edge.input_selector.model_dump(mode="json"),
                    "output_selector": edge.output_selector.model_dump(mode="json"),
                }
                key = snapshot_hash(shape)
                if edge.input_kind == "fact":
                    pinned = self.store.fact_revision(ws, pid, edge.revision_id)
                    head = self.store.fact(ws, pid, edge.input_id)
                    current = (
                        self.store.fact_revision(ws, pid, head.current_revision_id)
                        if head and head.current_revision_id
                        else None
                    )
                    if (
                        pinned is None
                        or current is None
                        or pinned.fact_id != edge.input_id
                        or pinned.sha256 != edge.sha256
                    ):
                        raise ValidationError("Dependency fact binding is invalid.")
                    source_ids.update(self.fact_sources(ws, pid, pinned))
                    current_ready, ids = self.fact_ready(ws, pid, current)
                    source_ids.update(ids)
                    try:
                        selected = fact_value(current, edge.input_selector)
                        unchanged = fact_value(pinned, edge.input_selector) == selected
                    except ValidationError:
                        selected, unchanged = "missing-selector", False
                    ready = ready and unchanged and current_ready
                    if not unchanged or not current_ready:
                        reasons.add("A declared fact or its selected evidence changed.")
                    signatures[key] = {**shape, "value": selected, "ready": current_ready}
                    assert head is not None
                    review_facts[key] = {
                        "fact_id": str(current.fact_id),
                        "key": head.key,
                        "kind": head.kind,
                        "value_type": current.value_type,
                        "value": select(current.value, edge.input_selector) if unchanged else None,
                        "selector": edge.input_selector.model_dump(mode="json"),
                        "unit": current.unit,
                        "sources": [binding.model_dump(mode="json") for binding in current.sources],
                    }
                else:
                    pinned_artifact = self.store.artifact_revision(ws, pid, edge.revision_id)
                    artifact = self.store.artifact(ws, pid, edge.input_id)
                    current_artifact = (
                        self.store.artifact_revision(ws, pid, artifact.current_revision_id)
                        if artifact and artifact.current_revision_id
                        else None
                    )
                    if (
                        pinned_artifact is None
                        or current_artifact is None
                        or pinned_artifact.artifact_id != edge.input_id
                        or pinned_artifact.sha256 != edge.sha256
                    ):
                        raise ValidationError("Dependency output binding is invalid.")
                    self.assert_revision_disclosable(ws, pid, pinned_artifact.id)
                    try:
                        selected = semantic_hash(
                            "json", artifact_value(current_artifact, edge.input_selector)
                        )
                        unchanged = (
                            semantic_hash(
                                "json", artifact_value(pinned_artifact, edge.input_selector)
                            )
                            == selected
                        )
                    except ValidationError:
                        selected, unchanged = "missing-selector", False
                    ready = ready and unchanged
                    if not unchanged:
                        reasons.add("A selected upstream output changed.")
                    signatures[key] = {**shape, "value": selected}
                    pending.append((current_artifact.id, edge.input_selector))
        conservative: dict[str, Any] | None = None
        if coverage != "complete":
            reasons.add("Dependency coverage is incomplete; conservative project checks apply.")
            global_facts = []
            for fact in facts:
                if fact.current_revision_id is None:
                    continue
                revision = self.store.fact_revision(ws, pid, fact.current_revision_id)
                assert revision is not None
                current_ready, ids = self.fact_ready(ws, pid, revision)
                source_ids.update(ids)
                ready = ready and current_ready
                if not current_ready:
                    reasons.add("A project fact no longer matches its selected source evidence.")
                review_facts[str(fact.id)] = {
                    "fact_id": str(fact.id),
                    "key": fact.key,
                    "kind": fact.kind,
                    "value_type": revision.value_type,
                    "value": revision.value,
                    "unit": revision.unit,
                    "selector": DependencySelector().model_dump(mode="json"),
                    "sources": [binding.model_dump(mode="json") for binding in revision.sources],
                }
                global_facts.append(
                    {
                        "id": str(fact.id),
                        "semantic": revision.semantic_sha256,
                        "ready": current_ready,
                    }
                )
            conservative = {
                "facts": sorted(global_facts, key=lambda item: item["id"]),
                "sources": self.source_fingerprint(ws, pid),
            }
        authority = {
            "declaration_id": str(root.id) if root else None,
            "declaration_version": root.version if root else 0,
            "coverage": coverage,
            "edges": [signatures[key] for key in sorted(signatures)],
            "conservative": conservative,
        }
        result = {
            "coverage": coverage,
            "fingerprint": snapshot_hash(authority),
            "declaration_id": authority["declaration_id"],
            "declaration_version": authority["declaration_version"],
            "source_ids": sorted(source_ids),
            "review_facts": [review_facts[key] for key in sorted(review_facts)],
            "ready": ready,
            "reasons": sorted(reasons),
        }
        if len(canonical_bytes(result)) > 96_000:
            raise ValidationError("Dependency review context exceeds its bounded capacity.")
        return result

    def assert_applicable(
        self, ws: UUID, pid: UUID, revision_id: UUID, snapshot: dict[str, Any]
    ) -> None:
        self.assert_disclosable(ws, pid, snapshot)
        current = self.snapshot(ws, pid, revision_id)
        if (
            current is None
            or not current["ready"]
            or current["fingerprint"] != snapshot.get("fingerprint")
        ):
            raise InvalidTransitionError(
                "Declared input authority changed; repair and review the current output."
            )
