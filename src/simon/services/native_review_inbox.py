"""Bounded, permission-checked decision summaries over current output authority."""

import json
from typing import Any
from uuid import UUID

from simon.domain.errors import (
    AuthorizationError,
    InvalidTransitionError,
    NotFoundError,
    ValidationError,
)
from simon.domain.models import ActorContext
from simon.domain.native_artifacts import NativeArtifact, NativeArtifactRevision
from simon.domain.native_delivery import NativeDeliveryPacket
from simon.domain.native_dependencies import semantic_hash
from simon.services.dependency_graph import strict_json
from simon.services.identity import IDENTITY_LOCK
from simon.services.native_artifacts import MAX_REVIEW_ATTEMPTS, MAX_REVISIONS
from simon.services.native_delivery import NativeDeliveryService

_UNAVAILABLE = (AuthorizationError, InvalidTransitionError, NotFoundError, ValidationError)
_GROUPS = (
    ("blocked", "Blocked or uncertain"),
    ("needs_repair", "Needs repair"),
    ("needs_decision", "Needs a decision"),
    ("needs_review", "Needs review"),
    ("accepted", "Accepted"),
)


def _display(value: Any) -> tuple[str, bool]:
    text = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return text[:500], len(text) > 500


def json_changes(before: Any, after: Any) -> tuple[list[dict[str, Any]], bool]:
    """Small RFC 6901 field differences; large arrays/subtrees stay explicit and bounded."""
    changes: list[dict[str, Any]] = []
    pending = [("", before, after, True, True, 0)]
    truncated = False
    while pending:
        path, old, new, had, has, depth = pending.pop()
        if had and has and semantic_hash("json", old) == semantic_hash("json", new):
            continue
        if len(changes) >= 20:
            truncated = True
            break
        if had and has and isinstance(old, dict) and isinstance(new, dict) and depth < 12:
            for key in sorted(old.keys() | new.keys(), reverse=True):
                pointer = path + "/" + key.replace("~", "~0").replace("/", "~1")
                pending.append(
                    (pointer, old.get(key), new.get(key), key in old, key in new, depth + 1)
                )
            continue
        old_text, old_cut = _display(old) if had else ("", False)
        new_text, new_cut = _display(new) if has else ("", False)
        truncated |= old_cut or new_cut or len(path) > 500
        changes.append(
            {
                "path": path[:500],
                "before": old_text,
                "after": new_text,
                "before_present": had,
                "after_present": has,
            }
        )
    return changes, truncated


class NativeReviewInboxService:
    def __init__(self, delivery: NativeDeliveryService) -> None:
        self.delivery, self.artifacts, self.store = delivery, delivery.artifacts, delivery.store

    @staticmethod
    def _base(kind: str, identifier: UUID) -> dict[str, Any]:
        return {
            "kind": kind,
            "id": str(identifier),
            "title": "Unavailable output",
            "revision_id": None,
            "sha256": None,
            "status": "unavailable",
            "disclosure": "unavailable",
            "next_action": {"kind": "none", "label": "Unavailable", "target_id": str(identifier)},
            "difference": {
                "text": "",
                "truncated": False,
                "supported": False,
                "field_changes": [],
                "fields_truncated": False,
            },
            "checks": {"passed": 0, "failed": 0, "total": 0, "items": []},
            "findings": [],
            "coverage": {
                "kind": "unknown",
                "reasons": ["Evidence is unavailable."],
                "source_ids": [],
                "omitted_source_count": 0,
            },
            "repair": {"used": 0, "limit": 0, "escalated": False},
            "component_ids": [],
        }

    def _difference(
        self, artifact: NativeArtifact, revision: NativeArtifactRevision
    ) -> dict[str, Any]:
        result = {
            **self.artifacts._difference(artifact, revision),
            "field_changes": [],
            "fields_truncated": False,
        }
        if not result["supported"] or not revision.parent_revision_id:
            return result
        if revision.media_type != "application/json":
            return result
        prior = self.artifacts._revision(artifact, revision.parent_revision_id)
        try:
            changes, cut = json_changes(
                strict_json(self.artifacts._text(prior)),
                strict_json(self.artifacts._text(revision)),
            )
        except (ValidationError, ValueError, RecursionError):
            return {
                **result,
                "field_changes_supported": False,
                "field_changes_limitation": "A supported JSON field comparison is unavailable.",
            }
        return {
            **result,
            "field_changes": changes,
            "fields_truncated": cut,
            "field_changes_supported": True,
        }

    def _artifact(self, artifact: NativeArtifact) -> tuple[str, dict[str, Any]]:
        item = self._base("artifact", artifact.id)
        if artifact.current_revision_id is None:
            return "blocked", item
        revision = self.artifacts._revision(artifact)
        self.artifacts.assert_disclosable_revision(artifact, revision)
        status = self.artifacts._status(artifact)
        policy = self.artifacts._policy(artifact.workspace_id, artifact.project_id)
        checks = self.store.artifact_checks(artifact.workspace_id, artifact.project_id, revision.id)
        reviews = self.store.artifact_reviews(
            artifact.workspace_id,
            artifact.project_id,
            revision.id,
            0,
            MAX_REVIEW_ATTEMPTS,
        )
        latest = reviews[-1] if reviews else None
        decisions = self.store.artifact_decisions(
            artifact.workspace_id, artifact.project_id, revision.id
        )
        repairs = sum(
            decision.decision == "changes_requested"
            for historical in self.store.artifact_revisions(
                artifact.workspace_id,
                artifact.project_id,
                artifact.id,
                0,
                MAX_REVISIONS,
            )
            for decision in self.store.artifact_decisions(
                artifact.workspace_id,
                artifact.project_id,
                historical.id,
            )
        )
        snapshot = self.artifacts.dependency_snapshot(revision)
        authority = self.artifacts._revision_source_authority(revision, snapshot)
        coverage = snapshot.get("coverage", "unknown") if snapshot else "unknown"
        reasons = list(snapshot.get("reasons", [])) if snapshot else []
        if coverage != "complete":
            reasons.append(
                "Field-level dependency coverage is not complete; conservative source checks apply."
            )
        if authority.get("coverage") != "complete":
            reasons.append(
                "Review source excerpts are partial; declared dependencies do not prove "
                "factual correctness."
            )
        findings = list(latest.findings) if latest else []
        if decisions and decisions[-1].decision == "changes_requested":
            findings.append(decisions[-1].feedback)
        escalated = (
            status in {"changes_requested", "validation_failed"}
            and repairs >= policy.max_repair_cycles
        )
        group, action, label = "needs_review", "artifact", "Review exact revision"
        if status == "accepted":
            group, label = "accepted", "View acceptance evidence"
        elif status == "awaiting_approval" or (
            status == "awaiting_review" and not policy.require_agent_review
        ):
            group, label = "needs_decision", "Decide on exact revision"
        elif status in {"changes_requested", "validation_failed"}:
            group, label = "needs_repair", "Inspect findings and repair"
        elif status == "stale":
            group, action, label = "blocked", "dependencies", "Reassess changed evidence"
        if self.artifacts.unresolved_review_usage(artifact):
            group, action, label = "blocked", "model_usage", "Resolve uncertain review cost"
            findings.append(
                "The review effect is unknown; its reserved cost remains visible in model usage."
            )
        elif status == "reviewing":
            group, label = "blocked", "Review is in progress"
        if escalated:
            group, action, label = "blocked", "policy", "Resolve exhausted repair allowance"
            findings.append(
                "Repair allowance is exhausted. An owner must deliberately revise "
                "the policy before another repair."
            )
        return group, {
            **item,
            "title": artifact.name,
            "revision_id": str(revision.id),
            "sha256": revision.sha256,
            "status": status,
            "disclosure": "available",
            "next_action": {"kind": action, "label": label, "target_id": str(artifact.id)},
            "difference": self._difference(artifact, revision),
            "checks": {
                "passed": sum(c.status == "passed" for c in checks),
                "failed": sum(c.status == "failed" for c in checks),
                "total": len(checks),
                "items": [
                    {"name": c.check_name, "status": c.status, "details": c.details} for c in checks
                ],
            },
            "findings": findings,
            "coverage": {
                "kind": coverage if coverage in {"complete", "partial"} else "unknown",
                "reasons": reasons,
                "source_ids": authority["source_ids"],
                "omitted_source_count": authority["omitted_count"],
            },
            "repair": {"used": repairs, "limit": policy.max_repair_cycles, "escalated": escalated},
        }

    def _packet(
        self, actor: ActorContext, packet: NativeDeliveryPacket
    ) -> tuple[str, dict[str, Any]]:
        packet = self.delivery._packet(actor, packet.project_id, packet.id)
        self.delivery._disclosure(packet)
        applicability = self.delivery._applicability(packet)
        item = self._base("packet", packet.id)
        group, status, label = "needs_decision", "unprofiled", "Assign a delivery profile"
        reasons = list(applicability["reasons"])
        acceptance: dict[str, Any] = (
            self.delivery.acceptance.status(actor, packet.project_id, packet.id)
            if self.delivery.acceptance
            else {"assigned": False}
        )
        if not applicability["current"]:
            group, status, label = "blocked", "stale", "Capture a current successor packet"
        elif not applicability["components_accepted"]:
            group, status, label = "needs_review", "candidate", "Resolve component reviews"
        elif acceptance.get("applicability", {}).get("accepted"):
            group, status, label = "accepted", "accepted", "View package acceptance"
        elif acceptance.get("assigned"):
            status, label = "profile_assigned", "Validate and decide on this package"
            validation = acceptance.get("latest_validation")
            if validation and not validation["passed"]:
                group, status, label = "needs_repair", "validation_failed", "Resolve package checks"
            elif acceptance.get("latest_acceptance"):
                group, status, label = "blocked", "stale", "Revalidate changed package authority"
        reasons.extend(acceptance.get("applicability", {}).get("reasons", []))
        checks = [c for component in packet.manifest.components for c in component.checks]
        check_items = [
            {"name": c.check_name, "status": c.status, "details": c.details} for c in checks
        ]
        validation = acceptance.get("latest_validation")
        if validation:
            check_items.extend(
                {"name": c["name"], "status": c["status"], "details": {"message": c["message"]}}
                for c in validation["checks"]
            )
        compact_acceptance = {
            key: acceptance[key]
            for key in ("assigned", "can_accept", "applicability")
            if key in acceptance
        }
        compact_acceptance["validation_id"] = validation["id"] if validation else None
        decision = acceptance.get("latest_acceptance")
        compact_acceptance["acceptance_id"] = decision["id"] if decision else None
        return group, {
            **item,
            "title": packet.manifest.title,
            "sha256": packet.manifest_sha256,
            "status": status,
            "disclosure": "available",
            "next_action": {"kind": "packet", "label": label, "target_id": str(packet.id)},
            "checks": {
                "passed": sum(c["status"] == "passed" for c in check_items),
                "failed": sum(c["status"] == "failed" for c in check_items),
                "total": len(check_items),
                "items": check_items,
            },
            "findings": reasons,
            "coverage": {
                "kind": "unknown",
                "reasons": list(packet.manifest.limitations),
                "source_ids": [str(i) for i in packet.manifest.source_revision_ids],
                "omitted_source_count": 0,
            },
            "component_ids": [str(c.artifact_id) for c in packet.manifest.components],
            "acceptance": compact_acceptance,
        }

    def view(
        self,
        actor: ActorContext,
        project_id: UUID,
        artifact_offset: int = 0,
        packet_offset: int = 0,
        limit: int = 20,
    ) -> dict[str, Any]:
        with self.store.transaction(IDENTITY_LOCK), self.store.transaction(actor.workspace_id):
            self.artifacts._access(actor, project_id)
            self.artifacts.projects._page(artifact_offset, limit)
            self.artifacts.projects._page(packet_offset, limit)
            groups: dict[str, list[dict[str, Any]]] = {key: [] for key, _ in _GROUPS}
            artifacts = self.store.artifacts(
                actor.workspace_id, project_id, offset=artifact_offset, limit=limit + 1
            )
            packets = self.store.delivery_packets(
                actor.workspace_id, project_id, packet_offset, limit + 1
            )
            for kind, records in (("artifact", artifacts[:limit]), ("packet", packets[:limit])):
                for record in records:
                    try:
                        group, item = (
                            self._artifact(record)
                            if isinstance(record, NativeArtifact)
                            else self._packet(actor, record)
                        )
                    except _UNAVAILABLE as exc:
                        group, item = "blocked", self._base(kind, record.id)
                        item["title"] = (
                            "Withheld output"
                            if isinstance(exc, AuthorizationError)
                            else "Unavailable output"
                        )
                        item["disclosure"] = (
                            "withheld" if isinstance(exc, AuthorizationError) else "unavailable"
                        )
                        item["findings"] = [
                            "Current evidence or authority is unavailable. "
                            "Open the project to resolve access."
                        ]
                    groups[group].append(item)
            return {
                "can_manage": self.delivery._can_manage(actor, project_id),
                "summary": {
                    "scope": "loaded_page",
                    "counts": {key: len(items) for key, items in groups.items()},
                    "warning": "Counts cover the loaded artifact and packet pages. "
                    "Open exact evidence before deciding.",
                },
                "groups": [
                    {"key": key, "label": label, "items": groups[key]} for key, label in _GROUPS
                ],
                "pagination": {
                    "artifact_offset": artifact_offset,
                    "packet_offset": packet_offset,
                    "limit": limit,
                    "artifacts_has_more": len(artifacts) > limit,
                    "packets_has_more": len(packets) > limit,
                },
                "policy": self.artifacts._policy(actor.workspace_id, project_id).model_dump(
                    mode="json"
                ),
                "limitations": [
                    "Summaries are bounded; they never approve or repair an output.",
                    "JSON field summaries show up to 20 differences and 500 characters "
                    "per value; text diffs show up to 4,000 characters.",
                    "Dependency coverage and deterministic checks do not establish "
                    "semantic correctness.",
                ],
            }
