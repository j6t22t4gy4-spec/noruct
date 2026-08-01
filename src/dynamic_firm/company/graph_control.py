"""Surface-neutral Graph Blueprint control API.

The CLI, terminal UI, a future desktop/web GUI, and automation adapters use
the same typed operations.  This module deliberately exposes inert Blueprint
selection and preview only: it never starts a Job, changes Company authority,
or applies an execution-graph mutation.
"""

from __future__ import annotations

from dataclasses import dataclass

from dynamic_firm.kernel.models import EmployeeRecord, JobLimits

from .frontdoor import WorkOrder
from .graph_blueprint_models import (
    BlueprintRevisionReceipt,
    BlueprintRevisionStatus,
    GraphBlueprint,
    GraphBlueprintOrigin,
    GraphBlueprintRef,
    GraphPreview,
    GraphUserConstraints,
)
from .graph_blueprint_registry import GraphBlueprintRegistry
from .graph_blueprint_registry import blueprint_from_payload
from .graph_blueprint_service import bind_blueprint, preview_binding


GRAPH_CONTROL_SCHEMA = "noruct.graph-control.v1"
DEFAULT_GRAPH_SLOT = "default"


@dataclass(frozen=True, slots=True)
class GraphBlueprintSelection:
    """Persisted user preference, not execution authority."""

    slot: str
    blueprint_ref: GraphBlueprintRef | None
    constraints: GraphUserConstraints


@dataclass(frozen=True, slots=True)
class GraphBlueprintCatalog:
    """A display-safe immutable projection for any product surface."""

    schema: str
    selection: GraphBlueprintSelection
    blueprints: tuple[GraphBlueprint, ...]


@dataclass(frozen=True, slots=True)
class GraphBlueprintRevisionDiff:
    """Content-free structural delta between two immutable local revisions.

    This is an explanation aid for a user-owned reusable Blueprint, not an
    ACTIVE JOB graph patch and not quality or cost attribution.
    """

    source_ref: GraphBlueprintRef
    candidate_ref: GraphBlueprintRef
    added_task_ids: tuple[str, ...]
    removed_task_ids: tuple[str, ...]
    changed_tasks: tuple[tuple[str, tuple[str, ...]], ...]
    changed_envelope_fields: tuple[str, ...]

    @property
    def changed_task_count(self) -> int:
        return len(self.added_task_ids) + len(self.removed_task_ids) + len(self.changed_tasks)


class GraphBlueprintControlService:
    """One surface-neutral control boundary above the local registry."""

    def __init__(self, registry: GraphBlueprintRegistry) -> None:
        self._registry = registry

    def catalog(self, *, slot: str = DEFAULT_GRAPH_SLOT) -> GraphBlueprintCatalog:
        return GraphBlueprintCatalog(
            schema=GRAPH_CONTROL_SCHEMA,
            selection=self.selection(slot=slot),
            blueprints=self._registry.list(),
        )

    def selection(self, *, slot: str = DEFAULT_GRAPH_SLOT) -> GraphBlueprintSelection:
        return GraphBlueprintSelection(
            slot=slot,
            blueprint_ref=self._registry.pinned(slot),
            constraints=self._registry.constraints(slot),
        )

    def save(self, blueprint: GraphBlueprint) -> GraphBlueprint:
        """Save an inert exact revision; origin policy remains on the model."""

        if blueprint.origin is GraphBlueprintOrigin.VERIFIED_PLAYBOOK:
            raise ValueError(
                "Only the Company qualification lifecycle may register a verified Playbook Blueprint"
            )
        return self._registry.save(blueprint)

    def import_payload(self, payload: object) -> GraphBlueprint:
        """Validate a data-only local import through the same save policy."""

        return self.save(self.parse_payload(payload))

    @staticmethod
    def parse_payload(payload: object) -> GraphBlueprint:
        """Decode an inert Blueprint without changing local registry state."""

        return blueprint_from_payload(payload)

    def revision(self, blueprint_id: str, version: int) -> GraphBlueprint:
        return self._registry.revision(blueprint_id, version)

    def fork(
        self,
        ref: GraphBlueprintRef,
        *,
        blueprint_id: str,
        version: int = 1,
    ) -> GraphBlueprint:
        return self._registry.fork(ref, blueprint_id=blueprint_id, version=version)

    def revise(
        self,
        source_ref: GraphBlueprintRef,
        candidate: GraphBlueprint,
        *,
        rationale: str,
    ) -> tuple[GraphBlueprint, BlueprintRevisionReceipt]:
        """Validate and save one new immutable revision of a user Blueprint.

        A revision is an inert local artifact. It is intentionally not a
        runtime graph patch, does not update a pin, and cannot change an
        already active Job. A caller can preview/select the accepted exact
        revision separately for a future Work Order.
        """

        source = self._registry.get(source_ref)
        candidate.verify()
        reason = "VALID_USER_REVISION"
        if candidate.blueprint_id != source.blueprint_id:
            reason = "BLUEPRINT_ID_CHANGED"
        elif source.origin not in {
            GraphBlueprintOrigin.DRAFT,
            GraphBlueprintOrigin.USER_FORK,
            GraphBlueprintOrigin.USER_REVISION,
        }:
            reason = "SOURCE_IS_NOT_USER_OWNED"
        elif candidate.version != source.version + 1:
            reason = "REVISION_MUST_INCREMENT_BY_ONE"
        elif candidate.origin is not GraphBlueprintOrigin.USER_REVISION:
            reason = "USER_REVISION_ORIGIN_REQUIRED"
        elif candidate.parent_ref != source.ref:
            reason = "EXACT_PARENT_REFERENCE_REQUIRED"
        elif candidate.content_digest == source.content_digest:
            reason = "NO_MATERIAL_CHANGE"
        receipt = BlueprintRevisionReceipt(
            source_ref=source.ref,
            candidate_ref=candidate.ref,
            status=(
                BlueprintRevisionStatus.ACCEPTED
                if reason == "VALID_USER_REVISION"
                else BlueprintRevisionStatus.REJECTED
            ),
            reason=reason,
            rationale=rationale,
        )
        self._registry.record_revision_receipt(receipt)
        if receipt.status is BlueprintRevisionStatus.REJECTED:
            raise ValueError(f"Blueprint revision rejected: {receipt.reason}")
        return self.save(candidate), receipt

    def revision_receipts(self, blueprint_id: str) -> tuple[BlueprintRevisionReceipt, ...]:
        return self._registry.revision_receipts(blueprint_id)

    def revision_diff(self, ref: GraphBlueprintRef) -> GraphBlueprintRevisionDiff | None:
        """Describe one parent-linked reusable Blueprint revision locally.

        Text templates themselves are never copied into the diff projection;
        a surface gets only which typed fields changed.  Exact artifacts remain
        available through the local registry for explicit user inspection.
        """

        candidate = self._registry.get(ref)
        if candidate.parent_ref is None:
            return None
        source = self._registry.get(candidate.parent_ref)
        source_tasks = {task.task_id: task for task in source.tasks}
        candidate_tasks = {task.task_id: task for task in candidate.tasks}
        added = tuple(sorted(set(candidate_tasks) - set(source_tasks)))
        removed = tuple(sorted(set(source_tasks) - set(candidate_tasks)))
        changed: list[tuple[str, tuple[str, ...]]] = []
        for task_id in sorted(set(source_tasks) & set(candidate_tasks)):
            before = source_tasks[task_id]
            after = candidate_tasks[task_id]
            fields = tuple(
                field
                for field, before_value, after_value in (
                    ("objective", before.objective_template, after.objective_template),
                    ("dependencies", before.depends_on, after.depends_on),
                    ("capabilities", before.required_capabilities, after.required_capabilities),
                    ("acceptance", before.acceptance_templates, after.acceptance_templates),
                    ("execution_replica", before.execution_replica, after.execution_replica),
                )
                if before_value != after_value
            )
            if fields:
                changed.append((task_id, fields))
        envelope = tuple(
            field
            for field, before_value, after_value in (
                ("objective_class", source.objective_class, candidate.objective_class),
                ("execution_profiles", source.execution_profiles, candidate.execution_profiles),
                ("parameters", source.parameters, candidate.parameters),
                ("final_task", source.final_task_id, candidate.final_task_id),
            )
            if before_value != after_value
        )
        return GraphBlueprintRevisionDiff(
            source_ref=source.ref,
            candidate_ref=candidate.ref,
            added_task_ids=added,
            removed_task_ids=removed,
            changed_tasks=tuple(changed),
            changed_envelope_fields=envelope,
        )

    def select(
        self,
        ref: GraphBlueprintRef | None,
        *,
        slot: str = DEFAULT_GRAPH_SLOT,
        constraints: GraphUserConstraints | None = None,
    ) -> GraphBlueprintSelection:
        """Persist a local preference without activating or executing it."""

        if ref is None:
            self._registry.clear_pin(slot)
        else:
            self._registry.pin(slot, ref)
        if constraints is not None:
            self._registry.set_constraints(slot, constraints)
        return self.selection(slot=slot)

    def preview(
        self,
        *,
        ref: GraphBlueprintRef,
        work_order: WorkOrder,
        roster: tuple[EmployeeRecord, ...],
        limits: JobLimits,
        slot: str = DEFAULT_GRAPH_SLOT,
        constraints: GraphUserConstraints | None = None,
    ) -> GraphPreview:
        """Validate an inert selection before any surface offers execution."""

        selected_constraints = constraints or self._registry.constraints(slot)
        binding = bind_blueprint(
            self._registry.get(ref),
            work_order=work_order,
            constraints=selected_constraints,
            limits=limits,
        )
        return preview_binding(
            binding,
            work_order=work_order,
            roster=roster,
            limits=limits,
        )
