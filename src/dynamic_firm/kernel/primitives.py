"""Request-local execution primitives for the Firm Kernel."""
from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import Protocol

from dynamic_firm.runtime.company_budget import (
    CompanyBudgetAdmission,
    CompanyBudgetAuthorityPort,
    CompanyBudgetForfeit,
    CompanyBudgetLease,
    CompanyBudgetSettlement,
)
from dynamic_firm.runtime.models import ActionPolicy, EmployeeRunResult, RunHandle

from .models import (
    AttemptFailureKind,
    CompanyRunRequest,
    EmployeeRecord,
    GraphPatch,
    JobResult,
    ReplanContext,
    TaskMutationType,
)

class ReplannerPort(Protocol):
    async def propose(self, context: ReplanContext) -> GraphPatch | None: ...


@dataclass(frozen=True, slots=True)
class _Reservation:
    model_calls: int
    tool_calls: int
    cost_usd: float


@dataclass(slots=True)
class _RunningTask:
    task_id: str
    employee: EmployeeRecord
    reservation: _Reservation
    attempt_id: str
    source_attempt_id: str | None
    action_policy: ActionPolicy
    capability_profile_digest: str
    capability_material_digest: str
    execution_instance_id: str = ""
    replica_group_id: str = ""
    handle: RunHandle | None = None


@dataclass(frozen=True, slots=True)
class _MutationCandidate:
    mutation_type: TaskMutationType
    employee: EmployeeRecord
    failure_kind: AttemptFailureKind
    reservation: _Reservation
    downstream_task_ids: tuple[str, ...]


class _TrackedCompanyBudgetAuthority:
    """Track one Kernel invocation without putting mutable state on FirmKernel.

    A FirmKernel instance may be reused concurrently, so exception cleanup
    cannot store the current lease on ``self``. This request-local proxy owns
    the lease lifecycle and delegates every durable write to the configured
    Company authority.
    """

    def __init__(self, delegate: CompanyBudgetAuthorityPort) -> None:
        self.delegate = delegate
        self.lease: CompanyBudgetLease | None = None
        self.terminalized = False

    def admit_job(self, request: CompanyRunRequest) -> CompanyBudgetAdmission:
        admission = self.delegate.admit_job(request)
        if admission.lease is not None:
            if self.lease is not None and self.lease != admission.lease:
                raise RuntimeError("Company budget admission changed within one Kernel run")
            self.lease = admission.lease
        return admission

    def settle_job(
        self,
        lease: CompanyBudgetLease,
        result: JobResult,
    ) -> CompanyBudgetSettlement:
        settlement = self.delegate.settle_job(lease, result)
        self.terminalized = True
        return settlement

    def forfeit_job(
        self,
        lease: CompanyBudgetLease,
        *,
        reason: str,
    ) -> CompanyBudgetForfeit:
        forfeiture = self.delegate.forfeit_job(lease, reason=reason)
        self.terminalized = True
        return forfeiture

    def forfeit_unsettled(self, *, reason: str) -> None:
        if self.lease is None or self.terminalized:
            return
        self.forfeit_job(self.lease, reason=reason)


_DEPENDENCY_RESULT_MAX_BYTES = 2_048


def _bounded_utf8(value: str, max_bytes: int) -> str:
    encoded = str(value).encode("utf-8")
    if len(encoded) <= max_bytes:
        return str(value)
    return encoded[:max_bytes].decode("utf-8", errors="ignore")


def _dependency_result_projection(
    dependency_id: str,
    result: EmployeeRunResult,
) -> str:
    """Serialize a bounded, typed handoff instead of a lossy summary string."""

    def items(values: tuple[str, ...], *, limit: int, max_bytes: int) -> list[str]:
        return [_bounded_utf8(value, max_bytes) for value in values[:limit]]

    payload: dict[str, object] = {
        "schema_version": "noruct.task-dependency.v1",
        "task_id": _bounded_utf8(dependency_id, 256),
        "status": result.status.value,
        "summary": _bounded_utf8(result.summary, 512),
        "acceptance_evidence": items(
            result.acceptance_evidence,
            limit=3,
            max_bytes=192,
        ),
        "unresolved_issues": items(
            result.unresolved_issues,
            limit=3,
            max_bytes=192,
        ),
        "output_artifact_refs": items(
            result.output_artifact_refs,
            limit=3,
            max_bytes=192,
        ),
        "partial": result.partial_result,
    }
    serialized = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    if len(serialized.encode("utf-8")) <= _DEPENDENCY_RESULT_MAX_BYTES:
        return serialized

    # Keep the schema and every semantic field when unusually large Unicode
    # content would exceed the prompt budget; only evidence cardinality and
    # text length are reduced.
    payload.update(
        summary=_bounded_utf8(result.summary, 192),
        acceptance_evidence=items(
            result.acceptance_evidence,
            limit=1,
            max_bytes=96,
        ),
        unresolved_issues=items(
            result.unresolved_issues,
            limit=1,
            max_bytes=96,
        ),
        output_artifact_refs=items(
            result.output_artifact_refs,
            limit=1,
            max_bytes=96,
        ),
    )
    serialized = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    if len(serialized.encode("utf-8")) <= _DEPENDENCY_RESULT_MAX_BYTES:
        return serialized
    payload.update(
        summary="",
        acceptance_evidence=[],
        unresolved_issues=[],
        output_artifact_refs=[],
    )
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


