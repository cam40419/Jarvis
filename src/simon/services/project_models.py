"""Project-owned credentials and qualified administrator-approved model routes.

The catalog is server-network authority, not a tenant-controlled URL list. Keys
are scoped ciphertext at rest and are never inherited from process credentials.
Qualification proves only a small text/JSON exchange, not task quality or tools.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import timedelta
from decimal import ROUND_CEILING, Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import UUID, uuid4

import httpx
from cryptography.fernet import Fernet, InvalidToken
from pydantic import SecretStr

from simon.adapters.model_endpoints import ModelEndpointClient, ModelEndpointError
from simon.domain.errors import (
    AuthorizationError,
    InvalidTransitionError,
    NotFoundError,
    ValidationError,
)
from simon.domain.model_routing import (
    ModelEndpoint,
    RoutingDecision,
    RoutingRequest,
    TextGenerationRequest,
)
from simon.domain.models import ActorContext, Channel, utc_now
from simon.domain.native_agents import NativeActor
from simon.domain.native_models import (
    EnrollProjectModel,
    ModelResourcePolicy,
    ModelTemplate,
    ModelUsage,
    ProbeProjectModel,
    ProjectModel,
    ProjectModelCredential,
    UpdateProjectModel,
)
from simon.domain.ports import Store
from simon.services.audit import AuditService
from simon.services.canonical import canonical_json, digest
from simon.services.identity import IDENTITY_LOCK
from simon.services.model_router import ModelRouter, ModelRoutingError
from simon.services.native_projects import NativeProjectService
from simon.services.safe_files import open_regular_nofollow

if TYPE_CHECKING:
    from simon.services.model_usage import ModelUsageService

_PROBE_SYSTEM = "Return only the exact JSON object requested. Do not add other text."
_PROBE_PROMPT = 'Return this exact JSON object: {"simon_model_probe":true,"version":1}'
_PROBE_INPUT = len((_PROBE_SYSTEM + _PROBE_PROMPT).encode()) + 2048
_PROBE_OUTPUT = 512


def _probe_output(endpoint: ModelEndpoint) -> int:
    maximum = min(_PROBE_OUTPUT, endpoint.max_output_tokens)
    if maximum < 32:
        raise ValidationError("The model output bound cannot support the qualification request.")
    return maximum


def configured_templates(path: Path | None, workspace_id: UUID) -> tuple[ModelTemplate, ...]:
    """Read a bounded, strict administrator catalog without following file redirects."""
    if path is None:
        return ()
    try:
        with open_regular_nofollow(path) as stream:
            raw = stream.read(1024 * 1024 + 1)
        if len(raw) > 1024 * 1024:
            raise ValueError

        def unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
            value: dict[str, Any] = {}
            for key, item in pairs:
                if key in value:
                    raise ValueError
                value[key] = item
            return value

        rows = json.loads(raw, object_pairs_hook=unique)
        if not isinstance(rows, list) or len(rows) > 100:
            raise ValueError
        templates = tuple(ModelTemplate.model_validate(row) for row in rows)
        if len({row.id for row in templates}) != len(templates):
            raise ValueError
        return tuple(row for row in templates if workspace_id in row.workspace_ids)
    except (OSError, ValueError, TypeError, RecursionError):
        raise ValidationError(
            "The administrator's model catalog is unavailable or invalid."
        ) from None


class ScopedModelSecrets:
    def __init__(self, cipher_factory: Callable[[], Fernet]) -> None:
        self._cipher = cipher_factory

    @staticmethod
    def _scope(
        workspace_id: UUID, project_id: UUID, model_id: UUID, revision: int
    ) -> dict[str, Any]:
        if type(revision) is not int or revision < 1:
            raise ValidationError("The credential revision is invalid.")
        return {
            "workspace_id": str(workspace_id),
            "project_id": str(project_id),
            "model_id": str(model_id),
            "revision": revision,
        }

    @staticmethod
    def validate(value: SecretStr) -> str:
        secret = value.get_secret_value()
        if not 1 <= len(secret) <= 4096 or any(
            char.isspace() or ord(char) < 32 or ord(char) == 127 for char in secret
        ):
            raise ValidationError("Enter a valid provider credential without whitespace.")
        try:
            secret.encode("utf-8")
        except UnicodeError:
            raise ValidationError("Enter a valid provider credential.") from None
        return secret

    def encrypt(
        self, workspace_id: UUID, project_id: UUID, model_id: UUID, revision: int, value: SecretStr
    ) -> str:
        document = {
            "kind": "simon_project_model_key_v1",
            **self._scope(workspace_id, project_id, model_id, revision),
            "secret": self.validate(value),
        }
        return self._cipher().encrypt(canonical_json(document).encode()).decode()

    def decrypt(self, record: ProjectModelCredential) -> str:
        try:
            document = json.loads(self._cipher().decrypt(record.encrypted_secret.encode()))
            scope = self._scope(
                record.workspace_id, record.project_id, record.model_id, record.revision
            )
            if (
                not isinstance(document, dict)
                or set(document) != {"kind", "secret", *scope}
                or document["kind"] != "simon_project_model_key_v1"
                or any(document[key] != value for key, value in scope.items())
                or not isinstance(document["secret"], str)
            ):
                raise ValueError
            return self.validate(SecretStr(document["secret"]))
        except (InvalidToken, ValueError, TypeError, UnicodeError, ValidationError, RecursionError):
            raise ValidationError(
                "The project model credential is unavailable; enroll it again."
            ) from None


@dataclass(frozen=True)
class ResolvedProjectModel:
    model: ProjectModel
    endpoint: ModelEndpoint
    fingerprint: str
    credential: str | None = field(default=None, repr=False)

    @property
    def environ(self) -> dict[str, str]:
        if self.endpoint.api_key_env and self.credential:
            return {self.endpoint.api_key_env: self.credential}
        return {}


@dataclass(frozen=True)
class ProjectModelRuntime:
    endpoints: tuple[ModelEndpoint, ...]
    environ: Mapping[str, str] = field(repr=False)
    planning_endpoint_id: str
    review_endpoint_id: str
    bindings: Mapping[str, ResolvedProjectModel]


class ProjectModelService:
    def __init__(
        self,
        store: Store,
        projects: NativeProjectService,
        templates_factory: Callable[[UUID], tuple[ModelTemplate, ...]],
        cipher_factory: Callable[[], Fernet],
        usage_service: ModelUsageService,
        *,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.store, self.projects = store, projects
        self.templates_factory = templates_factory
        self.secrets = ScopedModelSecrets(cipher_factory)
        self.usage = usage_service
        self.transport = transport
        self.audit = AuditService(store)

    def _access(self, actor: NativeActor, project_id: UUID, *, write: bool = False) -> None:
        if not isinstance(actor, ActorContext) or actor.channel != Channel.API:
            raise AuthorizationError("Project model management requires a human project session.")
        project = self.projects._project(actor, project_id, write=write, owner=write)
        if write:
            self.projects._active(project)

    def _policy(self, workspace_id: UUID, project_id: UUID) -> ModelResourcePolicy:
        return self.store.model_resource_policy(workspace_id, project_id) or ModelResourcePolicy(
            workspace_id=workspace_id, project_id=project_id
        )

    def _template(self, workspace_id: UUID, identifier: str) -> ModelTemplate:
        result = next(
            (row for row in self.templates_factory(workspace_id) if row.id == identifier), None
        )
        if result is None or workspace_id not in result.workspace_ids:
            raise ValidationError("This model template is no longer available to the workspace.")
        return result

    def _model(self, workspace_id: UUID, project_id: UUID, model_id: UUID) -> ProjectModel:
        value = self.store.project_model(workspace_id, project_id, model_id)
        if value is None:
            raise NotFoundError("Project model not found.")
        return value

    @staticmethod
    def _rates(endpoint: ModelEndpoint) -> tuple[Decimal, Decimal]:
        incoming, outgoing = (
            endpoint.input_cost_per_million_usd,
            endpoint.output_cost_per_million_usd,
        )
        if incoming is None and outgoing is None and endpoint.local:
            return Decimal(0), Decimal(0)
        if incoming is None or outgoing is None:
            raise ValidationError("Both input and output prices must be configured.")
        return Decimal(str(incoming)), Decimal(str(outgoing))

    def _binding(self, model: ProjectModel, *, require_qualified: bool) -> ResolvedProjectModel:
        template = self._template(model.workspace_id, model.template_id)
        if not model.enabled or not template.endpoint.enabled:
            raise ValidationError("This project model or its administrator template is disabled.")
        secret, credential = None, None
        if template.credential_required:
            credential = self.store.project_model_credential(
                model.workspace_id, model.project_id, model.id, model.credential_revision
            )
            if credential is None:
                raise ValidationError("Enroll a project credential before using this model.")
            secret = self.secrets.decrypt(credential)
        fingerprint = digest(
            {
                "template": template.model_dump(mode="json"),
                "model_id": str(model.id),
                "credential_revision": model.credential_revision,
                "ciphertext_digest": hashlib.sha256(
                    credential.encrypted_secret.encode()
                ).hexdigest()
                if credential
                else None,
            }
        )
        if require_qualified and (
            model.qualification_status != "ready" or model.qualification_fingerprint != fingerprint
        ):
            raise ValidationError(
                "Run the model check after its configuration or credential changes."
            )
        endpoint = template.endpoint.model_copy(
            update={
                "id": "model_" + model.id.hex,
                "api_key_env": "MODEL_PROJECT_" + model.id.hex.upper()
                if template.credential_required
                else None,
            }
        )
        self._rates(endpoint)
        return ResolvedProjectModel(model, endpoint, fingerprint, secret)

    @staticmethod
    def _grant(binding: ResolvedProjectModel, policy: ModelResourcePolicy) -> None:
        if not binding.endpoint.local and not policy.allow_cloud:
            raise ValidationError("Allow cloud inference in the project model policy first.")
        rates = ProjectModelService._rates(binding.endpoint)
        if any(rate > 0 for rate in rates) and not policy.allow_paid:
            raise ValidationError("Allow paid inference in the project model policy first.")

    def resolve(
        self,
        actor: ActorContext,
        project_id: UUID,
        model_id: UUID,
        *,
        require_qualified: bool = True,
    ) -> ResolvedProjectModel:
        with self.store.transaction(IDENTITY_LOCK), self.store.transaction(actor.workspace_id):
            self._access(actor, project_id, write=True)
            binding = self._binding(
                self._model(actor.workspace_id, project_id, model_id),
                require_qualified=require_qualified,
            )
            self._grant(binding, self._policy(actor.workspace_id, project_id))
            return binding

    def assert_current(
        self, actor: ActorContext, project_id: UUID, binding: ResolvedProjectModel
    ) -> None:
        current = self.resolve(actor, project_id, binding.model.id, require_qualified=False)
        if (
            current.model.version != binding.model.version
            or current.fingerprint != binding.fingerprint
        ):
            raise InvalidTransitionError("The model or credential changed before dispatch.")

    def runtime(self, actor: ActorContext, project_id: UUID) -> ProjectModelRuntime:
        with self.store.transaction(IDENTITY_LOCK), self.store.transaction(actor.workspace_id):
            self._access(actor, project_id, write=True)
            return self._runtime(actor.workspace_id, project_id)

    def _runtime(self, workspace_id: UUID, project_id: UUID) -> ProjectModelRuntime:
        policy = self._policy(workspace_id, project_id)
        workspace_policy = self.store.model_resource_policy(workspace_id, None)
        if policy.paused or (workspace_policy is not None and workspace_policy.paused):
            raise ValidationError("Model inference is paused by project or workspace policy.")
        bindings: dict[str, ResolvedProjectModel] = {}
        for model in self.store.project_models(workspace_id, project_id):
            try:
                binding = self._binding(model, require_qualified=True)
                self._grant(binding, policy)
            except ValidationError:
                continue
            bindings[binding.endpoint.id] = binding
        endpoints = tuple(value.endpoint for value in bindings.values())
        environ = {
            key: value for binding in bindings.values() for key, value in binding.environ.items()
        }
        router = ModelRouter(endpoints, environ=environ)

        def select(model_id: UUID | None) -> str:
            try:
                return router.route(
                    RoutingRequest(
                        model_override="model_" + model_id.hex if model_id else None,
                        privacy="allow_cloud" if policy.allow_cloud else "local_only",
                        input_tokens=2048,
                        output_tokens=1024,
                    )
                ).endpoint_id
            except ModelRoutingError:
                raise ValidationError(
                    "No qualified model satisfies the selected project route."
                ) from None

        return ProjectModelRuntime(
            endpoints,
            environ,
            select(policy.planning_model_id),
            select(policy.review_model_id),
            bindings,
        )

    @staticmethod
    def _template_public(template: ModelTemplate) -> dict[str, Any]:
        endpoint = template.endpoint
        try:
            incoming, outgoing = ProjectModelService._rates(endpoint)
            reservation = int(
                (_PROBE_INPUT * incoming + _probe_output(endpoint) * outgoing).to_integral_value(
                    rounding=ROUND_CEILING
                )
            )
        except ValidationError:
            reservation = None
        return {
            "id": template.id,
            "name": template.name,
            "provider": endpoint.provider,
            "model": endpoint.model,
            "local": endpoint.local,
            "input_cost_per_million_usd": endpoint.input_cost_per_million_usd,
            "output_cost_per_million_usd": endpoint.output_cost_per_million_usd,
            "credential_required": template.credential_required,
            "data_policy": template.data_policy,
            "license_note": template.license_note,
            "qualification_reservation_microusd": reservation,
        }

    def _public(self, model: ProjectModel) -> dict[str, Any]:
        result = model.model_dump(
            mode="json",
            exclude={"qualification_fingerprint", "workspace_id", "project_id", "created_by"},
        )
        result.update(
            has_credential=model.credential_revision > 0,
            ready=False,
            reason=None,
            model=None,
            provider=None,
            local=None,
            input_cost_per_million_usd=None,
            output_cost_per_million_usd=None,
            qualification_reservation_microusd=None,
        )
        try:
            template = self._template(model.workspace_id, model.template_id)
            safe = self._template_public(template)
            result.update(
                {
                    key: safe[key]
                    for key in (
                        "model",
                        "provider",
                        "local",
                        "input_cost_per_million_usd",
                        "output_cost_per_million_usd",
                    )
                }
            )
            incoming, outgoing = self._rates(template.endpoint)
            result["qualification_reservation_microusd"] = int(
                (
                    _PROBE_INPUT * incoming + _probe_output(template.endpoint) * outgoing
                ).to_integral_value(rounding=ROUND_CEILING)
            )
            binding = self._binding(model, require_qualified=True)
            self._grant(binding, self._policy(model.workspace_id, model.project_id))
            result["ready"] = True
        except ValidationError as exc:
            result["reason"] = str(exc)
        return result

    @staticmethod
    def _namespace(actor: ActorContext, project_id: UUID) -> str:
        return f"model-credentials:{actor.workspace_id}:{project_id}:{actor.actor_id}"

    def _record(self, actor: ActorContext, event: str, model: ProjectModel) -> None:
        self.audit.record(
            event_type="native.model." + event,
            actor=actor,
            resource_type="project_model",
            resource_id=str(model.id),
            payload={
                "project_id": str(model.project_id),
                "version": model.version,
                "template_id": model.template_id,
                "credential_revision": model.credential_revision,
                "enabled": model.enabled,
            },
        )

    def _credential(self, actor: ActorContext, model: ProjectModel, value: SecretStr) -> None:
        self.store.insert_project_model_credential(
            ProjectModelCredential(
                workspace_id=model.workspace_id,
                project_id=model.project_id,
                model_id=model.id,
                revision=model.credential_revision,
                created_by=actor.actor_id,
                encrypted_secret=self.secrets.encrypt(
                    model.workspace_id, model.project_id, model.id, model.credential_revision, value
                ),
            )
        )

    def _command_digest(
        self,
        actor: ActorContext,
        project_id: UUID,
        command: EnrollProjectModel | UpdateProjectModel,
        model_id: UUID | None,
    ) -> str:
        values = command.model_dump(mode="json", exclude={"credential", "idempotency_key"})
        values.update(
            model_id=str(model_id) if model_id else None,
            credential_digest=digest(
                {
                    "workspace": str(actor.workspace_id),
                    "project": str(project_id),
                    "secret": self.secrets.validate(command.credential),
                }
            )
            if command.credential
            else None,
        )
        return digest(values)

    def enroll(
        self, actor: ActorContext, project_id: UUID, command: EnrollProjectModel
    ) -> dict[str, Any]:
        with self.store.transaction(IDENTITY_LOCK), self.store.transaction(actor.workspace_id):
            self._access(actor, project_id, write=True)
            request_digest = self._command_digest(actor, project_id, command, None)

            def operation() -> dict[str, Any]:
                template = self._template(actor.workspace_id, command.template_id)
                if bool(command.credential) != template.credential_required:
                    raise ValidationError(
                        "Supply a credential only when this model template requires it."
                    )
                if len(self.store.project_models(actor.workspace_id, project_id)) >= 100:
                    raise ValidationError(
                        "This project already has the maximum number of model connections."
                    )
                model = ProjectModel(
                    workspace_id=actor.workspace_id,
                    project_id=project_id,
                    template_id=template.id,
                    label=command.label,
                    created_by=actor.actor_id,
                    credential_revision=1 if command.credential else 0,
                )
                self.store.insert_project_model(model)
                if command.credential:
                    self._credential(actor, model, command.credential)
                self._record(actor, "enrolled", model)
                return self._public(model)

            return self.store.execute_once(
                self._namespace(actor, project_id),
                command.idempotency_key,
                request_digest,
                operation,
            )[0]

    def update(
        self, actor: ActorContext, project_id: UUID, model_id: UUID, command: UpdateProjectModel
    ) -> dict[str, Any]:
        with self.store.transaction(IDENTITY_LOCK), self.store.transaction(actor.workspace_id):
            self._access(actor, project_id, write=True)
            self._model(actor.workspace_id, project_id, model_id)
            request_digest = self._command_digest(actor, project_id, command, model_id)

            def operation() -> dict[str, Any]:
                current = self._model(actor.workspace_id, project_id, model_id)
                self.projects._version(current.version, command.expected_version)
                if (
                    command.credential
                    and not self._template(
                        actor.workspace_id, current.template_id
                    ).credential_required
                ):
                    raise ValidationError("This template does not accept a project credential.")
                changed = command.credential is not None or command.enabled != current.enabled
                update: dict[str, Any] = {
                    "label": command.label,
                    "enabled": command.enabled,
                    "version": current.version + 1,
                    "updated_at": utc_now(),
                }
                if changed:
                    update.update(
                        qualification_status="unverified",
                        qualification_fingerprint=None,
                        qualification_usage_id=None,
                        qualification_error=None,
                        qualified_at=None,
                    )
                if command.credential:
                    update["credential_revision"] = current.credential_revision + 1
                model = current.model_copy(update=update)
                self.store.update_project_model(model, expected_version=current.version)
                if command.credential:
                    self._credential(actor, model, command.credential)
                self._record(actor, "updated", model)
                return self._public(model)

            return self.store.execute_once(
                self._namespace(actor, project_id),
                command.idempotency_key,
                request_digest,
                operation,
            )[0]

    def operation_receipt(self, actor: ActorContext, project_id: UUID, key: str) -> dict[str, Any]:
        with self.store.transaction(IDENTITY_LOCK), self.store.transaction(actor.workspace_id):
            self._access(actor, project_id, write=True)
            result = self.store.command_receipt(self._namespace(actor, project_id), key)
            if result is None:
                raise NotFoundError("Model operation receipt not found.")
            return result

    def view(self, actor: ActorContext, project_id: UUID) -> dict[str, Any]:
        with self.store.transaction(IDENTITY_LOCK), self.store.transaction(actor.workspace_id):
            self._access(actor, project_id)
            # The ledger owns policy/totals visibility and current owner permissions.
            result = self.usage.view(actor, project_id)
            try:
                templates = self.templates_factory(actor.workspace_id)
                configuration_error = None
            except ValidationError as exc:
                templates, configuration_error = (), str(exc)
            result.update(
                models=[
                    self._public(row)
                    for row in self.store.project_models(actor.workspace_id, project_id)
                ],
                templates=[self._template_public(row) for row in templates],
                configuration_error=configuration_error,
            )
            routing: dict[str, Any] = {
                "ready": False,
                "reason": None,
                "planning_model_id": None,
                "review_model_id": None,
                "planning_label": None,
                "review_label": None,
            }
            try:
                runtime = self._runtime(actor.workspace_id, project_id)
                planning = runtime.bindings[runtime.planning_endpoint_id].model
                review = runtime.bindings[runtime.review_endpoint_id].model
                routing.update(
                    ready=True,
                    planning_model_id=str(planning.id),
                    review_model_id=str(review.id),
                    planning_label=planning.label,
                    review_label=review.label,
                )
            except ValidationError as exc:
                routing["reason"] = str(exc)
            result["routing"] = routing
            return result

    def probe(
        self, actor: ActorContext, project_id: UUID, model_id: UUID, command: ProbeProjectModel
    ) -> dict[str, Any]:
        """Admit one deliberate synthetic call, retaining any uncertain provider liability."""
        binding: ResolvedProjectModel | None = None
        with self.store.transaction(IDENTITY_LOCK), self.store.transaction(actor.workspace_id):
            self._access(actor, project_id, write=True)
            namespace = self._namespace(actor, project_id) + ":probe"
            request_digest = digest(
                {
                    "model_id": str(model_id),
                    **command.model_dump(mode="json", exclude={"idempotency_key"}),
                }
            )

            def admit() -> dict[str, Any]:
                nonlocal binding
                model = self._model(actor.workspace_id, project_id, model_id)
                self.projects._version(model.version, command.expected_version)
                binding = self.resolve(actor, project_id, model_id, require_qualified=False)
                self._probe_decision(binding)  # Verify context/output contracts before reserving.
                incoming, outgoing = self._rates(binding.endpoint)
                reservation = int(
                    (
                        _PROBE_INPUT * incoming + _probe_output(binding.endpoint) * outgoing
                    ).to_integral_value(rounding=ROUND_CEILING)
                )
                started = utc_now()
                usage = ModelUsage(
                    workspace_id=actor.workspace_id,
                    project_id=project_id,
                    operation_id=uuid4(),
                    phase="qualification",
                    requested_by=actor.actor_id,
                    model_id=model.id,
                    model_version=model.version,
                    credential_revision=model.credential_revision,
                    template_id=model.template_id,
                    model=binding.endpoint.model,
                    endpoint_fingerprint=binding.fingerprint,
                    endpoint_snapshot=binding.endpoint.model_dump(mode="json"),
                    input_rate=incoming,
                    output_rate=outgoing,
                    reserved_microusd=reservation,
                    held_microusd=reservation,
                    started_at=started,
                    deadline_at=started + timedelta(minutes=5),
                )
                self.usage.reserve(actor, project_id, usage.operation_id, (usage,))
                return {"usage_id": str(usage.id), "model_id": str(model.id)}

            receipt, replayed = self.store.execute_once(
                namespace, command.idempotency_key, request_digest, admit
            )
            if replayed:
                return self._public(self._model(actor.workspace_id, project_id, model_id))
        assert binding is not None
        usage_id = UUID(receipt["usage_id"])
        result = None
        error = None
        dispatched = False
        unknown = False
        try:
            with self.store.transaction(IDENTITY_LOCK), self.store.transaction(actor.workspace_id):
                self.assert_current(actor, project_id, binding)
                self.usage.dispatch(actor, project_id, usage_id)
                dispatched = True
            result = ModelEndpointClient(
                (binding.endpoint,), environ=binding.environ, transport=self.transport
            ).generate(
                self._probe_decision(binding),
                TextGenerationRequest(
                    system=_PROBE_SYSTEM,
                    prompt=_PROBE_PROMPT,
                    max_output_tokens=_probe_output(binding.endpoint),
                ),
            )
            if result.refused or result.truncated or not self._valid_probe(result.text):
                error = "qualification_output_invalid"
        except ModelEndpointError as exc:
            error, unknown = exc.code, exc.may_have_been_dispatched
        except (AuthorizationError, InvalidTransitionError, ValidationError):
            error = "qualification_authority_changed"
        except Exception:
            error, unknown = "qualification_interrupted", dispatched
        finally:
            if dispatched:
                settled = self.usage.settle(
                    actor.workspace_id,
                    project_id,
                    usage_id,
                    result,
                    unknown=unknown,
                    error_code=error,
                )
            else:
                settled = self.usage.release(actor.workspace_id, project_id, usage_id)
        with self.store.transaction(IDENTITY_LOCK), self.store.transaction(actor.workspace_id):
            current = self._model(actor.workspace_id, project_id, model_id)
            try:
                self.assert_current(actor, project_id, binding)
            except (AuthorizationError, InvalidTransitionError, ValidationError, NotFoundError):
                # Accounting survives authority loss; stale calls cannot qualify a new key/config.
                self._access(actor, project_id, write=True)
                return self._public(current)
            if settled.status != "settled" or settled.charged_microusd > settled.reserved_microusd:
                error = error or "qualification_usage_unresolved"
            updated = current.model_copy(
                update={
                    "version": current.version + 1,
                    "updated_at": utc_now(),
                    "qualification_status": "failed" if error else "ready",
                    "qualification_fingerprint": None if error else binding.fingerprint,
                    "qualification_usage_id": usage_id,
                    "qualification_error": error,
                    "qualified_at": utc_now(),
                }
            )
            self.store.update_project_model(updated, expected_version=current.version)
            self._record(actor, "qualification_completed", updated)
            return self._public(updated)

    @staticmethod
    def _probe_decision(binding: ResolvedProjectModel) -> RoutingDecision:
        try:
            return ModelRouter((binding.endpoint,), environ=binding.environ).route(
                RoutingRequest(
                    model_override=binding.endpoint.id,
                    input_tokens=_PROBE_INPUT,
                    output_tokens=_probe_output(binding.endpoint),
                )
            )
        except ModelRoutingError:
            raise ValidationError(
                "The model cannot support the bounded qualification request."
            ) from None

    @staticmethod
    def _valid_probe(text: str) -> bool:
        if len(text) > 4096:
            return False
        try:

            def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
                result: dict[str, Any] = {}
                for key, value in items:
                    if key in result:
                        raise ValueError
                    result[key] = value
                return result

            value = json.loads(text, object_pairs_hook=pairs)
            return (
                isinstance(value, dict)
                and set(value) == {"simon_model_probe", "version"}
                and value["simon_model_probe"] is True
                and type(value["version"]) is int
                and value["version"] == 1
            )
        except (ValueError, TypeError, RecursionError):
            return False
