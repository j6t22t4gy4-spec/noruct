"""Application-only handoff for an already frozen EmployeeRun route plan.

The caller owns plan construction, receipt selection, and the local approval
input.  This value merely keeps those opaque inputs together long enough to
wire the existing foundation runtime ports.  It deliberately has no route
selection, configuration, credential, egress, or provider-call API.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from collections.abc import Callable
from pathlib import Path
from threading import Lock
from typing import Any

from dynamic_firm.application.local_approved_route_runtime import (
    LocalApprovedRouteRuntime,
    PreFrozenSelectionReceipt,
)
from dynamic_firm.company.approved_route_plan_admission import (
    require_fresh_approved_route_plan,
)
from dynamic_firm.company.independent_verification_plan import (
    IndependentCallShape,
    IndependentVerificationPlan,
)
from dynamic_firm.company.execution_route_binding import ExecutionRouteBinding
from dynamic_firm.company.multi_route_job_plan import (
    DependencyArtifactHandoff,
    MultiRouteAssignmentGuard,
    MultiRouteJobPlan,
    TaskRouteAssignment,
)
from dynamic_firm.company.multi_route_runtime_policy import MultiRouteRuntimePolicy
from dynamic_firm.company.route_provider_registry import (
    FrozenRouteProviderRegistry,
    RouteProviderDefinition,
)
from dynamic_firm.company.route_selection_receipt import RouteSelectionReceipt
from dynamic_firm.kernel.graph import graph_from_proposal
from dynamic_firm.kernel.models import (
    CompanyRunRequest,
    EmployeeRecord,
    JobTask,
    TaskAssignmentEvent,
)
from dynamic_firm.kernel.mutation import (
    content_digest,
    frozen_snapshot_digest,
    graph_structure_digest,
)
from dynamic_firm.product.local_routing_settings import load_local_routing_settings
from dynamic_firm.runtime.models import ActionPolicy


@dataclass(frozen=True, slots=True)
class FrozenRouteRegistryClosureReceipt:
    """Content-free result of the no-factory registry closure preflight.

    The registry receives the immutable multi-route policy and returns the
    exact sorted ``(route, config-digest, credential-reference)`` metadata it
    can construct.  This is an intentionally narrow, provider-free contract:
    invoking ``construct`` to discover a missing definition would already call
    an adapter factory.
    """

    status: str
    required_metadata: tuple[tuple[str, str, str], ...]
    validated_metadata: tuple[tuple[str, str, str], ...] = ()
    failure_code: str = ""

    @property
    def admitted(self) -> bool:
        return self.status == "ADMITTED"


@dataclass(frozen=True, slots=True)
class FrozenRouteContinuationBundle:
    """Content-free Job-level evidence needed to reassemble frozen dispatch.

    Per-run admissions only describe Employee runs that have already started.
    A partial Company continuation also needs the original plan and selection
    closure for tasks that never reached Employee dispatch.  This bundle keeps
    only content-free canonical routing inputs in the user-local Work Order
    authority.  Digests alone are not enough to recreate an unstarted task's
    exact binding after a process restart, so the same bundle also retains the
    immutable plan and selection closure; it never retains a factory,
    credential value, provider instance, or model response.
    """

    request_identity_digest: str
    route_policy_digest: str
    binding_digests: tuple[str, ...]
    selection_receipt_digests: tuple[str, ...]
    preplanned_blueprint_digest: str
    independent_verification_digest: str
    runtime_policy_json: str
    selection_closure_json: str
    independent_verification_json: str

    def __post_init__(self) -> None:
        values = (
            self.request_identity_digest,
            self.route_policy_digest,
            self.preplanned_blueprint_digest,
            self.independent_verification_digest,
            *self.binding_digests,
            *self.selection_receipt_digests,
        )
        if not values or any(
            not isinstance(value, str)
            or len(value) != 64
            or any(character not in "0123456789abcdef" for character in value)
            for value in values
        ):
            raise ValueError("frozen route continuation evidence must use SHA-256 digests")
        if (
            tuple(sorted(self.binding_digests)) != self.binding_digests
            or tuple(sorted(self.selection_receipt_digests)) != self.selection_receipt_digests
            or len(set(self.binding_digests)) != len(self.binding_digests)
            or len(set(self.selection_receipt_digests)) != len(self.selection_receipt_digests)
        ):
            raise ValueError("frozen route continuation evidence must be sorted and unique")
        for value, field_name in (
            (self.runtime_policy_json, "runtime policy"),
            (self.selection_closure_json, "selection closure"),
            (self.independent_verification_json, "independent verification"),
        ):
            if not isinstance(value, str) or not value:
                raise ValueError(f"frozen route continuation {field_name} must be canonical JSON")

    def canonical_payload(self) -> dict[str, object]:
        return {
            "binding_digests": list(self.binding_digests),
            "independent_verification_digest": self.independent_verification_digest,
            "preplanned_blueprint_digest": self.preplanned_blueprint_digest,
            "request_identity_digest": self.request_identity_digest,
            "route_policy_digest": self.route_policy_digest,
            "selection_receipt_digests": list(self.selection_receipt_digests),
            "runtime_policy_json": self.runtime_policy_json,
            "selection_closure_json": self.selection_closure_json,
            "independent_verification_json": self.independent_verification_json,
        }

    def canonical_json(self) -> str:
        import json

        return json.dumps(self.canonical_payload(), sort_keys=True, separators=(",", ":"))

    @property
    def digest(self) -> str:
        return content_digest(self.canonical_payload())

    @classmethod
    def from_canonical_json(cls, raw: object) -> "FrozenRouteContinuationBundle":
        import json

        try:
            payload = json.loads(raw) if isinstance(raw, str) else None
        except json.JSONDecodeError as exc:
            raise ValueError("frozen route continuation bundle JSON is invalid") from exc
        expected = {
            "binding_digests",
            "independent_verification_digest",
            "preplanned_blueprint_digest",
            "request_identity_digest",
            "route_policy_digest",
            "selection_receipt_digests",
            "runtime_policy_json",
            "selection_closure_json",
            "independent_verification_json",
        }
        if not isinstance(payload, dict) or set(payload) != expected:
            raise ValueError("frozen route continuation bundle fields are invalid")
        bundle = cls(
            request_identity_digest=payload["request_identity_digest"],
            route_policy_digest=payload["route_policy_digest"],
            binding_digests=tuple(payload["binding_digests"]),
            selection_receipt_digests=tuple(payload["selection_receipt_digests"]),
            preplanned_blueprint_digest=payload["preplanned_blueprint_digest"],
            independent_verification_digest=payload["independent_verification_digest"],
            runtime_policy_json=payload["runtime_policy_json"],
            selection_closure_json=payload["selection_closure_json"],
            independent_verification_json=payload["independent_verification_json"],
        )
        if raw != bundle.canonical_json():
            raise ValueError("frozen route continuation bundle JSON is not canonical")
        return bundle


def _canonical_json(payload: object) -> str:
    import json

    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def _runtime_policy_payload(policy: MultiRouteRuntimePolicy) -> dict[str, object]:
    return {
        "bindings": [binding.canonical_payload() for binding in policy.bindings],
        "plan": {
            "acting_integrator_id": policy.plan.acting_integrator_id,
            "assignments": [
                {
                    "depends_on": list(item.depends_on),
                    "employee_id": item.employee_id,
                    "expected_selection_receipt_digest": item.expected_selection_receipt_digest,
                    "final": item.final,
                    "route_binding_digest": item.route_binding_digest,
                    "task_id": item.task_id,
                }
                for item in policy.plan.assignments
            ],
            "graph_digest": policy.plan.graph_digest,
            "handoffs": [
                {
                    "artifact_digest": item.artifact_digest,
                    "source_task_id": item.source_task_id,
                    "target_task_id": item.target_task_id,
                }
                for item in policy.plan.handoffs
            ],
        },
    }


def _selection_closure_payload(
    receipts: tuple[PreFrozenSelectionReceipt, ...],
) -> list[dict[str, object]]:
    return [
        {
            "binding_digest": item.binding_digest,
            "selection_receipt": item.selection_receipt.canonical_payload(),
        }
        for item in sorted(receipts, key=lambda item: item.binding_digest)
    ]


def _independent_verification_payload(
    plan: IndependentVerificationPlan | None,
) -> dict[str, object] | None:
    if plan is None:
        return None

    def shape(value: IndependentCallShape) -> dict[str, object]:
        return {
            "availability_fallback": value.availability_fallback,
            "context_projection_digest": value.context_projection_digest,
            "model_identity_digest": value.model_identity_digest,
            "provider_route_digest": value.provider_route_digest,
            "read_only": value.read_only,
            "source_projection_digest": value.source_projection_digest,
            "tools_enabled": value.tools_enabled,
        }

    return {
        "candidate": shape(plan.candidate),
        "error_correlation": plan.error_correlation,
        "verifier": shape(plan.verifier),
    }


def _json_object(raw: str, field_name: str) -> dict[str, object]:
    import json

    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"frozen route continuation {field_name} JSON is invalid") from exc
    if not isinstance(value, dict) or raw != _canonical_json(value):
        raise ValueError(f"frozen route continuation {field_name} JSON is not canonical")
    return value


def _policy_from_continuation_json(raw: str) -> MultiRouteRuntimePolicy:
    payload = _json_object(raw, "runtime policy")
    if set(payload) != {"bindings", "plan"} or not isinstance(payload["bindings"], list):
        raise ValueError("frozen route continuation runtime policy fields are invalid")
    plan_payload = payload["plan"]
    if not isinstance(plan_payload, dict) or set(plan_payload) != {
        "acting_integrator_id", "assignments", "graph_digest", "handoffs"
    } or not isinstance(plan_payload["assignments"], list) or not isinstance(plan_payload["handoffs"], list):
        raise ValueError("frozen route continuation plan fields are invalid")
    try:
        plan = MultiRouteJobPlan(
            graph_digest=plan_payload["graph_digest"],
            assignments=tuple(
                TaskRouteAssignment(
                    task_id=item["task_id"],
                    employee_id=item["employee_id"],
                    route_binding_digest=item["route_binding_digest"],
                    depends_on=tuple(item["depends_on"]),
                    final=item["final"],
                    expected_selection_receipt_digest=item["expected_selection_receipt_digest"],
                )
                for item in plan_payload["assignments"]
                if isinstance(item, dict) and set(item) == {
                    "depends_on", "employee_id", "expected_selection_receipt_digest",
                    "final", "route_binding_digest", "task_id"
                }
            ),
            handoffs=tuple(
                DependencyArtifactHandoff(
                    source_task_id=item["source_task_id"],
                    target_task_id=item["target_task_id"],
                    artifact_digest=item["artifact_digest"],
                )
                for item in plan_payload["handoffs"]
                if isinstance(item, dict) and set(item) == {
                    "artifact_digest", "source_task_id", "target_task_id"
                }
            ),
            acting_integrator_id=plan_payload["acting_integrator_id"],
        )
        bindings = tuple(
            ExecutionRouteBinding(**item)
            for item in payload["bindings"]
            if isinstance(item, dict)
            and set(item) == set(ExecutionRouteBinding.__dataclass_fields__)
        )
        policy = MultiRouteRuntimePolicy(plan, bindings)
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("frozen route continuation runtime policy is invalid") from exc
    if raw != _canonical_json(_runtime_policy_payload(policy)):
        raise ValueError("frozen route continuation runtime policy changed during parsing")
    return policy


def _selection_closure_from_continuation_json(
    raw: str,
) -> tuple[PreFrozenSelectionReceipt, ...]:
    import json

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError("frozen route continuation selection closure JSON is invalid") from exc
    if not isinstance(payload, list) or raw != _canonical_json(payload):
        raise ValueError("frozen route continuation selection closure JSON is not canonical")
    try:
        receipts = tuple(
            PreFrozenSelectionReceipt(
                binding_digest=item["binding_digest"],
                selection_receipt=RouteSelectionReceipt.from_canonical_json(
                    _canonical_json(item["selection_receipt"])
                ),
            )
            for item in payload
            if isinstance(item, dict)
            and set(item) == {"binding_digest", "selection_receipt"}
            and isinstance(item["selection_receipt"], dict)
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("frozen route continuation selection closure is invalid") from exc
    if len(receipts) != len(payload) or raw != _canonical_json(_selection_closure_payload(receipts)):
        raise ValueError("frozen route continuation selection closure changed during parsing")
    return receipts


def _independent_verification_from_continuation_json(
    raw: str,
) -> IndependentVerificationPlan | None:
    import json

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError("frozen route continuation independent plan JSON is invalid") from exc
    if payload is None:
        if raw != "null":
            raise ValueError("frozen route continuation independent plan JSON is not canonical")
        return None
    if not isinstance(payload, dict) or set(payload) != {"candidate", "error_correlation", "verifier"}:
        raise ValueError("frozen route continuation independent plan fields are invalid")
    try:
        plan = IndependentVerificationPlan(
            candidate=IndependentCallShape(**payload["candidate"]),
            verifier=IndependentCallShape(**payload["verifier"]),
            error_correlation=payload["error_correlation"],
        )
    except (TypeError, ValueError) as exc:
        raise ValueError("frozen route continuation independent plan is invalid") from exc
    if raw != _canonical_json(_independent_verification_payload(plan)):
        raise ValueError("frozen route continuation independent plan changed during parsing")
    return plan


@dataclass(frozen=True, slots=True)
class FrozenRouteContinuationCatalog:
    """Provider-free local factory catalog for exact continuation reassembly.

    The catalog is supplied by product composition.  It stores callable
    adapter definitions only in memory, never serializes them, and cannot
    select a route or read a credential.  A retained bundle supplies the
    original immutable policy; current local approval is still rechecked by
    ``FrozenRouteGoalComposition`` before provider construction.
    """

    config_path: Path
    provider_definitions: tuple[RouteProviderDefinition, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.config_path, Path):
            raise TypeError("continuation catalog config path must be pathlib.Path")
        if not isinstance(self.provider_definitions, tuple) or not self.provider_definitions:
            raise ValueError("continuation catalog requires typed provider definitions")
        if not all(isinstance(item, RouteProviderDefinition) for item in self.provider_definitions):
            raise TypeError("continuation catalog provider definitions must be typed")
        object.__setattr__(self, "config_path", self.config_path.expanduser().resolve())

    def reassemble(
        self,
        bundle: FrozenRouteContinuationBundle,
    ) -> "FrozenRouteGoalComposition":
        if not isinstance(bundle, FrozenRouteContinuationBundle):
            raise TypeError("frozen route continuation bundle must be typed")
        policy = _policy_from_continuation_json(bundle.runtime_policy_json)
        receipts = _selection_closure_from_continuation_json(bundle.selection_closure_json)
        independent = _independent_verification_from_continuation_json(
            bundle.independent_verification_json
        )
        if policy.summary_digest != bundle.route_policy_digest:
            raise ValueError("frozen route continuation runtime policy digest drifted")
        if tuple(sorted(binding.digest for binding in policy.bindings)) != bundle.binding_digests:
            raise ValueError("frozen route continuation binding closure drifted")
        if tuple(sorted(item.selection_receipt.digest for item in receipts)) != bundle.selection_receipt_digests:
            raise ValueError("frozen route continuation selection closure drifted")
        if content_digest(_independent_verification_payload(independent)) != bundle.independent_verification_digest:
            raise ValueError("frozen route continuation independent plan digest drifted")
        return FrozenRouteGoalComposition(
            LocalApprovedRouteRuntime(self.config_path, policy, receipts),
            FrozenRouteProviderRegistry(self.provider_definitions),
            bundle.preplanned_blueprint_digest,
            independent_verification_plan=independent,
        )


@dataclass(frozen=True, slots=True)
class FrozenRouteCompanyRequestIdentity:
    """Content-free identity for the one Company request behind a route guard.

    A route graph by itself is not a Company authority.  The guard carries the
    exact request/job identity as well as the frozen Company, Work Order,
    ActionPolicy, egress/runtime, and Firm-admission evidence.  It deliberately
    retains only digests and stable identifiers: no prompt, credential, tool
    payload, or provider configuration crosses this application boundary.
    """

    request_id: str
    job_id: str
    content_digest: str

    @classmethod
    def from_request(
        cls,
        request: CompanyRunRequest,
        *,
        state_path: Path | None = None,
    ) -> "FrozenRouteCompanyRequestIdentity":
        if not isinstance(request, CompanyRunRequest):
            raise TypeError("CompanyRunRequest is required for route identity")
        if state_path is not None and not isinstance(state_path, Path):
            raise TypeError("state authority path must be a pathlib.Path")
        graph = graph_from_proposal(
            request.plan_proposal,
            max_tasks=request.job_limits.max_tasks,
        )
        return cls(
            request_id=request.request_id,
            job_id=request.job_id,
            content_digest=content_digest(
                {
                    "schema": "noruct.frozen-route-company-request.v1",
                    "request_id": request.request_id,
                    "job_id": request.job_id,
                    "goal_digest": content_digest(request.goal),
                    "graph_digest": graph_structure_digest(graph),
                    "frozen_snapshot_digest": frozen_snapshot_digest(request),
                    "company_revision": request.company_revision,
                    "roster_revision": request.roster_revision,
                    "playbook_revision": request.playbook_revision,
                    "work_order_id": request.work_order_id,
                    "work_order_digest": request.work_order_digest,
                    "work_order_authority_digest": request.work_order_authority_digest,
                    "firm_admission_digest": request.firm_admission_digest,
                    "action_policy_digest": content_digest(request.action_policy),
                    "runtime_provider_binding_digest": request.runtime_provider_binding_digest,
                    "runtime_tool_contract_digest": request.runtime_tool_contract_digest,
                    "runtime_company_coordination_digest": request.runtime_company_coordination_digest,
                    # This remains inside the ephemeral composition identity.
                    # The canonicalized local path is represented only by its
                    # digest, never emitted to a receipt or product event.
                    "state_authority_path_digest": (
                        content_digest(
                            {
                                "schema": "noruct.state-authority-path.v1",
                                "canonical_path": str(
                                    state_path.expanduser().resolve()
                                ),
                            }
                        )
                        if state_path is not None
                        else ""
                    ),
                }
            ),
        )


@dataclass(frozen=True, slots=True)
class FrozenRouteAssignmentAdmission:
    """One-use Kernel callback bound to an exact final Company request."""

    request_identity: FrozenRouteCompanyRequestIdentity
    _guard: MultiRouteAssignmentGuard = field(repr=False)

    def __call__(self, event: TaskAssignmentEvent) -> str:
        if not isinstance(event, TaskAssignmentEvent):
            raise TypeError("Kernel assignment event is required")
        if event.job_id != self.request_identity.job_id:
            raise ValueError("Kernel assignment does not match frozen Company request identity")
        return self._guard.accept(event)


@dataclass(frozen=True, slots=True)
class FrozenRouteGoalComposition:
    """Immutable, caller-supplied ports for frozen foundation dispatch.

    ``LocalApprovedRouteRuntime`` remains the required admission authority.
    Its callable form is passed only as the foundation runtime's durable
    binding cross-check; ``admission_for`` is the authority that re-admits
    before each EmployeeRun dispatch.
    """

    _admission_runtime: LocalApprovedRouteRuntime = field(repr=False)
    _provider_registry: FrozenRouteProviderRegistry = field(repr=False)
    _preplanned_blueprint_digest: str = field(repr=False)
    independent_verification_plan: IndependentVerificationPlan | None = None
    _request_identity_lock: Lock = field(default_factory=Lock, init=False, repr=False)
    _bound_request_identity: FrozenRouteCompanyRequestIdentity | None = field(
        default=None,
        init=False,
        repr=False,
    )
    _assignment_admission_issued: bool = field(
        default=False,
        init=False,
        repr=False,
    )

    def __post_init__(self) -> None:
        if not isinstance(self._admission_runtime, LocalApprovedRouteRuntime):
            raise TypeError("admission runtime must be a LocalApprovedRouteRuntime")
        if not isinstance(self._provider_registry, FrozenRouteProviderRegistry):
            raise TypeError("provider registry must be a FrozenRouteProviderRegistry")
        if (
            not isinstance(self._preplanned_blueprint_digest, str)
            or len(self._preplanned_blueprint_digest) != 64
            or any(character not in "0123456789abcdef" for character in self._preplanned_blueprint_digest)
        ):
            raise ValueError("preplanned Blueprint digest must be an exact SHA-256 digest")
        if self.independent_verification_plan is not None and not isinstance(
            self.independent_verification_plan, IndependentVerificationPlan
        ):
            raise TypeError("independent verification plan must be typed")

    def foundation_runtime_kwargs(self) -> dict[str, Any]:
        """Return the exact existing foundation ports for this frozen plan."""
        return {
            "frozen_route_binding_resolver": self._admission_runtime,
            "frozen_route_admission_resolver": self._admission_runtime.admission_for,
            "frozen_route_registry": self._provider_registry,
        }

    def continuation_bundle_for(
        self,
        request: CompanyRunRequest,
        *,
        state_path: Path,
    ) -> FrozenRouteContinuationBundle:
        """Return the durable, content-free evidence for one exact Job."""
        identity = FrozenRouteCompanyRequestIdentity.from_request(
            request,
            state_path=state_path,
        )
        policy = self._admission_runtime.frozen_runtime_policy
        receipts = self._admission_runtime.frozen_selection_receipts
        if receipts is None:
            raise ValueError("frozen route continuation requires selection closure")
        independent = self.independent_verification_plan
        return FrozenRouteContinuationBundle(
            request_identity_digest=identity.content_digest,
            route_policy_digest=policy.summary_digest,
            binding_digests=tuple(sorted(binding.digest for binding in policy.bindings)),
            selection_receipt_digests=tuple(
                sorted(item.selection_receipt.digest for item in receipts)
            ),
            preplanned_blueprint_digest=self._preplanned_blueprint_digest,
            independent_verification_digest=content_digest(
                _independent_verification_payload(independent)
            ),
            runtime_policy_json=_canonical_json(_runtime_policy_payload(policy)),
            selection_closure_json=_canonical_json(
                _selection_closure_payload(receipts)
            ),
            independent_verification_json=_canonical_json(
                _independent_verification_payload(independent)
            ),
        )

    def require_config_path(self, active_config_path: Path) -> None:
        """Refuse a composition whose approval source is not this Job's TOML."""
        if not isinstance(active_config_path, Path):
            raise TypeError("active config path must be a pathlib.Path")
        if active_config_path.expanduser().resolve() != self._admission_runtime.config_path:
            raise ValueError(
                "Frozen route composition approval source does not match active config path"
            )

    def registry_closure_receipt(self) -> FrozenRouteRegistryClosureReceipt:
        """Preflight every required adapter definition without constructing one.

        ``FrozenRouteProviderRegistry.validate_frozen_bindings`` is the public
        static validator.  Do not inspect registry private fields or call
        ``construct`` as a substitute: either choice could turn a closure
        check into an adapter construction side effect.
        """
        policy = self._admission_runtime.frozen_runtime_policy
        bindings = policy.bindings
        required_metadata = tuple(
            sorted(
                (
                    binding.route_id,
                    binding.provider_config_digest,
                    binding.credential_reference,
                )
                for binding in bindings
            )
        )
        try:
            validated_metadata = self._provider_registry.validate_frozen_bindings(policy)
        except (TypeError, ValueError):
            return FrozenRouteRegistryClosureReceipt(
                status="REJECTED",
                required_metadata=required_metadata,
                failure_code="REGISTRY_CLOSURE_VALIDATOR_REJECTED",
            )
        if not isinstance(validated_metadata, tuple) or any(
            not isinstance(item, tuple)
            or len(item) != 3
            or any(not isinstance(value, str) for value in item)
            for item in validated_metadata
        ):
            return FrozenRouteRegistryClosureReceipt(
                status="REJECTED",
                required_metadata=required_metadata,
                failure_code="REGISTRY_CLOSURE_VALIDATOR_MALFORMED",
            )
        if tuple(sorted(validated_metadata)) != required_metadata:
            return FrozenRouteRegistryClosureReceipt(
                status="REJECTED",
                required_metadata=required_metadata,
                validated_metadata=tuple(sorted(validated_metadata)),
                failure_code="REGISTRY_CLOSURE_MISMATCH",
            )
        return FrozenRouteRegistryClosureReceipt(
            status="ADMITTED",
            required_metadata=required_metadata,
            validated_metadata=required_metadata,
        )

    def require_registry_closure(self) -> FrozenRouteRegistryClosureReceipt:
        """Require exact selection and adapter closures before assembly."""
        # ``goal_runtime`` has already established that its active config path
        # equals the composition approval source.  Re-read the exact local
        # TOML before any registry inspection so first-run or drifted approval
        # state cannot reach resource assembly or a factory.  The returned
        # admission is intentionally not retained: it is neither selection nor
        # execution authority.
        settings = load_local_routing_settings(self._admission_runtime.config_path)
        require_fresh_approved_route_plan(
            settings.policy,
            settings.approved_routes,
            self._admission_runtime.frozen_runtime_policy,
        )
        # Caller-frozen selection evidence is part of the same top-level
        # dispatch closure.  Validate it before asking even the provider-free
        # registry metadata validator to inspect adapter definitions.
        self._admission_runtime.require_frozen_selection_closure()
        self._independent_verification_assignments()
        receipt = self.registry_closure_receipt()
        if not receipt.admitted:
            raise ValueError(
                "Frozen route composition registry closure preflight failed: "
                f"{receipt.failure_code}"
            )
        return receipt

    def _independent_verification_assignments(
        self,
    ) -> tuple[tuple[str, str], tuple[str, str]] | None:
        """Map an approved independent pair to two exact plan assignments.

        The data-only plan is not authority by itself.  Its two route digests
        must resolve to distinct task/Employee assignments in this
        same immutable multi-route plan before the Kernel receives its narrow
        no-tools policy projection.
        """
        plan = self.independent_verification_plan
        if plan is None:
            return None
        if not plan.effectively_independent:
            raise ValueError("independent verification plan is not effectively independent")
        assignments = self._admission_runtime.frozen_runtime_policy.plan.assignments

        def assignment_for(route_digest: str, role: str) -> tuple[str, str]:
            matched = tuple(
                item
                for item in assignments
                if item.route_binding_digest == route_digest
            )
            if len(matched) != 1:
                raise ValueError(
                    f"independent verification {role} route is not an exact plan assignment"
                )
            assignment = matched[0]
            return assignment.task_id, assignment.employee_id

        candidate = assignment_for(plan.candidate.provider_route_digest, "candidate")
        verifier = assignment_for(plan.verifier.provider_route_digest, "verifier")
        if candidate[0] == verifier[0] or candidate[1] == verifier[1]:
            raise ValueError(
                "independent verification requires distinct task and Employee assignments"
            )
        return candidate, verifier

    def kernel_task_action_policy_override(
        self,
    ) -> Callable[[JobTask, EmployeeRecord, ActionPolicy], ActionPolicy] | None:
        """Return the one narrow Kernel policy projection for an approved pair."""
        assignments = self._independent_verification_assignments()
        if assignments is None:
            return None
        expected_employee_by_task = dict(assignments)

        def no_tools_for_independent_pair(
            task: JobTask,
            employee: EmployeeRecord,
            default_policy: ActionPolicy,
        ) -> ActionPolicy:
            expected_employee_id = expected_employee_by_task.get(task.task_id)
            if expected_employee_id is None:
                return default_policy
            if employee.employee_id != expected_employee_id:
                raise ValueError(
                    "independent verification Employee does not match frozen assignment"
                )
            # Candidate and verifier are both isolated from tools/effects.
            # The verifier's read-only contract is therefore enforced as the
            # stronger default-deny/no-tools projection, not a prompt hint.
            return ActionPolicy()

        return no_tools_for_independent_pair

    def require_preplanned_blueprint(self, binding: object | None) -> None:
        """Require an exact provider-free Blueprint binding before dispatch setup."""
        blueprint_ref = getattr(binding, "blueprint_ref", None)
        proposal = getattr(binding, "proposal", None)
        if (
            blueprint_ref is None
            or getattr(blueprint_ref, "content_digest", None)
            != self._preplanned_blueprint_digest
            or proposal is None
        ):
            raise ValueError(
                "Frozen route composition requires its exact preplanned local Blueprint"
            )
        graph = graph_from_proposal(
            proposal,
            max_tasks=6,
        )
        if (
            graph_structure_digest(graph)
            != self._admission_runtime.frozen_runtime_policy.plan.graph_digest
        ):
            raise ValueError(
                "Frozen route composition preplanned Blueprint does not match frozen route graph"
            )

    def assignment_admission_for(
        self,
        request: CompanyRunRequest,
        *,
        state_path: Path | None = None,
    ) -> Callable[[TaskAssignmentEvent], str]:
        """Bind this frozen plan to one finalized managed Company request.

        The caller supplies the complete request only after Company planning,
        Firm admission, and continuation preflight have frozen its graph
        inputs.  Reconstructing the initial graph here proves that the opaque
        route plan cannot be reused for a different task topology before the
        Kernel receives its read-only assignment guard.
        """
        if not isinstance(request, CompanyRunRequest):
            raise TypeError("CompanyRunRequest is required for route admission")
        graph = graph_from_proposal(
            request.plan_proposal,
            max_tasks=request.job_limits.max_tasks,
        )
        actual_digest = graph_structure_digest(graph)
        expected_digest = self._admission_runtime.frozen_runtime_policy.plan.graph_digest
        if actual_digest != expected_digest:
            raise ValueError(
                "Frozen multi-route plan does not match the finalized Company graph"
            )
        request_identity = FrozenRouteCompanyRequestIdentity.from_request(
            request,
            state_path=state_path,
        )
        # A frozen route composition is one Company Job handoff, never a
        # reusable graph-shaped capability.  First binding is deliberately
        # late enough to include the final Firm/admission/runtime digests;
        # any later request with the same topology but different Job, Work
        # Order, Company/ROSTER revision, ActionPolicy, or egress/runtime
        # domain is refused rather than silently reusing route evidence.
        with self._request_identity_lock:
            bound_identity = self._bound_request_identity
            if bound_identity is None:
                object.__setattr__(self, "_bound_request_identity", request_identity)
            elif bound_identity != request_identity:
                raise ValueError(
                    "Frozen route composition is already bound to a different Company request identity"
                )
            if self._assignment_admission_issued:
                raise ValueError(
                    "Frozen route composition assignment admission has already been issued"
                )
            object.__setattr__(self, "_assignment_admission_issued", True)
        return FrozenRouteAssignmentAdmission(
            request_identity,
            MultiRouteAssignmentGuard(
                self._admission_runtime.frozen_runtime_policy.plan,
                graph_version=graph.version,
                task_attempts=tuple(
                    (task.task_id, task.attempt)
                    for task in graph.tasks
                ),
            ),
        )
