from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import replace

from dynamic_firm.kernel.graph import GraphValidationError, apply_patch, task_map
from dynamic_firm.kernel.models import (
    GraphPatch,
    GraphPatchOperation,
    JobTask,
    PatchOperationKind,
    ReplanContext,
    SemanticOperation,
    TaskStatus,
)
from dynamic_firm.runtime.models import (
    RunSignal,
    SemanticReplanDirective,
    SemanticReplanOperation,
    SignalCode,
)

from .admission import (
    OrganizationAdmissionDecision,
    TypedCapabilityAdmissionPolicy,
)
from .models import WorkflowPrior, WorkflowPriorTask


_SAFE_IDENTIFIER = re.compile(r"[^a-z0-9_]+")
_SAFE_CAPABILITY = re.compile(r"^[a-z][a-z0-9_]{0,63}$")


class CapabilityInsertReplanner:
    """Translate one typed capability gap into one bounded INSERT proposal."""

    def __init__(
        self,
        *,
        admission_policy: TypedCapabilityAdmissionPolicy | None = None,
        decision_sink: Callable[[OrganizationAdmissionDecision], None] | None = None,
        workflow_priors: tuple[WorkflowPrior, ...] = (),
    ) -> None:
        self.admission_policy = admission_policy or TypedCapabilityAdmissionPolicy()
        self.decision_sink = decision_sink
        self.workflow_priors = workflow_priors
        self.decisions: list[OrganizationAdmissionDecision] = []
        self.exposed_workflow_prior_ids: list[str] = []
        self.aligned_workflow_prior_ids: list[str] = []

    async def propose(self, context: ReplanContext) -> GraphPatch | None:
        decision = self.admission_policy.decide(context)
        self.decisions.append(decision)
        if self.decision_sink is not None:
            self.decision_sink(decision)
        if not decision.admitted:
            return None
        capability = decision.capability
        if decision.expands_final_task:
            prior_patch = self._prior_replay_patch(context, capability)
            if prior_patch is not None:
                return prior_patch
        return self._generic_insert_patch(context, capability)

    def _prior_replay_patch(
        self,
        context: ReplanContext,
        capability: str,
    ) -> GraphPatch | None:
        candidates: list[tuple[WorkflowPrior, GraphPatch]] = []
        for prior in self.workflow_priors:
            patch = self._validated_prior_patch(context, capability, prior)
            if patch is None:
                continue
            if prior.pattern_id not in self.exposed_workflow_prior_ids:
                self.exposed_workflow_prior_ids.append(prior.pattern_id)
            candidates.append((prior, patch))
        if len(candidates) != 1:
            return None
        prior, patch = candidates[0]
        if prior.pattern_id not in self.aligned_workflow_prior_ids:
            self.aligned_workflow_prior_ids.append(prior.pattern_id)
        return patch

    def _validated_prior_patch(
        self,
        context: ReplanContext,
        capability: str,
        prior: WorkflowPrior,
    ) -> GraphPatch | None:
        if prior.evidence_count < 2 or len(prior.tasks) < 2:
            return None
        keys = tuple(task.task_key.strip() for task in prior.tasks)
        if (
            any(not key for key in keys)
            or len(keys) != len(set(keys))
            or sum(1 for task in prior.tasks if task.final) != 1
        ):
            return None
        known_keys = set(keys)
        for task in prior.tasks:
            if (
                not task.required_capabilities
                or any(not _SAFE_CAPABILITY.fullmatch(item) for item in task.required_capabilities)
                or len(task.required_capabilities) != len(set(task.required_capabilities))
                or len(task.depends_on) != len(set(task.depends_on))
                or task.task_key in task.depends_on
                or any(dependency not in known_keys for dependency in task.depends_on)
            ):
                return None
        original_final = next(
            task
            for task in context.graph.tasks
            if task.task_id == context.graph.final_task_id
        )
        root_tasks = tuple(task for task in prior.tasks if not task.depends_on)
        mapped_root = next(
            (
                task
                for task in root_tasks
                if task.task_key == context.trigger_task.task_id
                and task.required_capabilities == original_final.required_capabilities
                and not task.final
            ),
            None,
        )
        replay_tasks = tuple(
            task
            for task in prior.tasks
            if mapped_root is None or task.task_key != mapped_root.task_key
        )
        if (
            not replay_tasks
            or not any(capability in task.required_capabilities for task in replay_tasks)
        ):
            return None
        if (
            len(context.graph.tasks) + len(replay_tasks)
            > context.request.job_limits.max_tasks
        ):
            return None
        if not self._temporary_role_budget_allows(context, replay_tasks):
            return None

        existing_ids = {task.task_id for task in context.graph.tasks}
        task_ids: dict[str, str] = (
            {mapped_root.task_key: context.trigger_task.task_id}
            if mapped_root is not None
            else {}
        )
        for task in replay_tasks:
            slug = _SAFE_IDENTIFIER.sub(
                "_",
                task.task_key.strip().lower(),
            ).strip("_") or "task"
            base_id = slug[:64].rstrip("_")
            task_id = base_id
            suffix = 2
            while task_id in existing_ids or task_id in task_ids.values():
                tail = f"_{suffix}"
                task_id = base_id[: 64 - len(tail)] + tail
                suffix += 1
            task_ids[task.task_key] = task_id

        added_tasks: list[JobTask] = []
        final_task_id = ""
        for task in replay_tasks:
            dependencies = tuple(task_ids[item] for item in task.depends_on)
            if not dependencies:
                dependencies = (context.trigger_task.task_id,)
            added = JobTask(
                task_id=task_ids[task.task_key],
                objective=(
                    f"Execute the verified playbook step '{task.task_key}' after the "
                    f"typed {capability} capability gap."
                ),
                depends_on=dependencies,
                required_capabilities=task.required_capabilities,
                acceptance_criteria=(
                    f"Produce verified evidence for playbook step '{task.task_key}'.",
                ),
                risk_level=original_final.risk_level if task.final else "LOW",
            )
            added_tasks.append(added)
            if task.final:
                final_task_id = added.task_id
        patch = GraphPatch(
            patch_id=(
                f"playbook-{context.graph.version}-"
                f"{_SAFE_IDENTIFIER.sub('_', prior.pattern_id.lower()).strip('_')[:40]}"
            ).rstrip("_"),
            base_graph_version=context.graph.version,
            trigger_task_id=context.trigger_task.task_id,
            semantic_operation=SemanticOperation.INSERT,
            rationale=(
                f"Replay verified workflow pattern {prior.pattern_id} only after the "
                f"SOLO attempt emitted admitted typed capability gap: {capability}."
            ),
            expected_gain=(
                "Preserve the solo evidence and reuse an exact-context workflow topology "
                "supported by repeated successful evidence."
            ),
            operations=tuple(
                GraphPatchOperation(PatchOperationKind.ADD_TASK, task=task)
                for task in added_tasks
            )
            + (
                GraphPatchOperation(
                    PatchOperationKind.SET_FINAL_TASK,
                    task_id=final_task_id,
                ),
            ),
        )
        try:
            apply_patch(
                context.graph,
                patch,
                max_tasks=context.request.job_limits.max_tasks,
            )
        except GraphValidationError:
            return None
        return patch

    @staticmethod
    def _temporary_role_budget_allows(
        context: ReplanContext,
        tasks: tuple[WorkflowPriorTask, ...],
    ) -> bool:
        available_profiles = [
            frozenset(employee.capabilities)
            for employee in context.roster
            if employee.active
        ]
        temporary_count = sum(employee.temporary for employee in context.roster)
        remaining = context.request.job_limits.max_temporary_roles - temporary_count
        created = 0
        for task in tasks:
            required = frozenset(task.required_capabilities)
            if any(required.issubset(profile) for profile in available_profiles):
                continue
            created += 1
            if created > remaining:
                return False
            available_profiles.append(required)
        return True

    @staticmethod
    def _generic_insert_patch(
        context: ReplanContext,
        capability: str,
    ) -> GraphPatch:
        tasks = {task.task_id: task for task in context.graph.tasks}
        final = tasks[context.graph.final_task_id]

        base_id = f"specialist_{capability}"[:64].rstrip("_")
        task_id = base_id
        suffix = 2
        while task_id in tasks:
            tail = f"_{suffix}"
            task_id = base_id[: 64 - len(tail)] + tail
            suffix += 1
        specialist = JobTask(
            task_id=task_id,
            objective=(
                f"Resolve the typed capability gap {capability} and produce evidence for final integration."
            ),
            depends_on=(context.trigger_task.task_id,),
            required_capabilities=(capability,),
            acceptance_criteria=(f"Return evidence that resolves the {capability} capability gap.",),
        )
        if context.trigger_task.task_id == final.task_id:
            integration_base = "integrate_goal"
            integration_id = integration_base
            suffix = 2
            while integration_id in tasks or integration_id == task_id:
                tail = f"_{suffix}"
                integration_id = integration_base[: 64 - len(tail)] + tail
                suffix += 1
            integration = JobTask(
                task_id=integration_id,
                objective=(
                    "Integrate the original solo attempt with the admitted specialist evidence "
                    "and complete the user goal."
                ),
                depends_on=(context.trigger_task.task_id, specialist.task_id),
                required_capabilities=final.required_capabilities,
                acceptance_criteria=final.acceptance_criteria,
                risk_level=final.risk_level,
            )
            return GraphPatch(
                patch_id=f"escalate-{context.graph.version}-{task_id}",
                base_graph_version=context.graph.version,
                trigger_task_id=context.trigger_task.task_id,
                semantic_operation=SemanticOperation.INSERT,
                rationale=(
                    f"SOLO attempt emitted admitted typed capability gap: {capability}."
                ),
                expected_gain=(
                    "Preserve the solo evidence, add one specialist, and assign one final "
                    "integration pass."
                ),
                operations=(
                    GraphPatchOperation(PatchOperationKind.ADD_TASK, task=specialist),
                    GraphPatchOperation(PatchOperationKind.ADD_TASK, task=integration),
                    GraphPatchOperation(
                        PatchOperationKind.SET_FINAL_TASK,
                        task_id=integration.task_id,
                    ),
                ),
            )
        return GraphPatch(
            patch_id=f"insert-{context.graph.version}-{task_id}",
            base_graph_version=context.graph.version,
            trigger_task_id=context.trigger_task.task_id,
            semantic_operation=SemanticOperation.INSERT,
            rationale=f"Employee emitted typed capability gap: {capability}.",
            expected_gain="Add the missing specialist evidence before final integration.",
            operations=(
                GraphPatchOperation(PatchOperationKind.ADD_TASK, task=specialist),
                GraphPatchOperation(
                    PatchOperationKind.ADD_DEPENDENCY,
                    task_id=final.task_id,
                    dependency_id=task_id,
                ),
            ),
        )


class ManagerFollowUpReplanner:
    """Translate an explicit Manager follow-up signal into one safe insert.

    This is intentionally not a second planning model or employee chat loop.
    An Employee may state that an assumption or constraint needs a specialist
    follow-up only with ``follow_up_capability:<capability>``.  The adapter
    rewrites that one typed request into the existing capability admission
    path; Kernel still owns graph policy, leases, validation and execution.
    """

    _PREFIX = "follow_up_capability:"

    def __init__(self, delegate: CapabilityInsertReplanner, *, manager_employee_id: str) -> None:
        if not manager_employee_id.strip():
            raise ValueError("Manager follow-up replanner requires a Manager identity")
        self.delegate = delegate
        self.manager_employee_id = manager_employee_id

    @property
    def exposed_workflow_prior_ids(self) -> list[str]:
        """Expose the delegate's immutable-prior disclosure receipts.

        The Manager wrapper changes only which typed signals are eligible for
        a follow-up insert. It must not hide the underlying replanner's
        content-free attribution facts from the Company learning projection.
        """

        return self.delegate.exposed_workflow_prior_ids

    @property
    def aligned_workflow_prior_ids(self) -> list[str]:
        """Expose the delegate's accepted-prior replay receipts."""

        return self.delegate.aligned_workflow_prior_ids

    async def propose(self, context: ReplanContext) -> GraphPatch | None:
        signal = context.signal
        if signal.code not in {
            SignalCode.ASSUMPTION_INVALIDATED,
            SignalCode.CONSTRAINT_CHANGED,
        }:
            return await self.delegate.propose(context)
        capability = self._follow_up_capability(signal.value)
        if capability is None:
            return None
        return await self.delegate.propose(
            replace(
                context,
                signal=RunSignal(
                    SignalCode.CAPABILITY_MISSING,
                    capability,
                    evidence=(
                        *signal.evidence,
                        f"manager-follow-up:{self.manager_employee_id}",
                    ),
                ),
            )
        )

    @classmethod
    def _follow_up_capability(cls, value: str) -> str | None:
        if not value.startswith(cls._PREFIX):
            return None
        capability = value.removeprefix(cls._PREFIX).strip()
        return capability or None


class SemanticSignalReplanner:
    """Translate a small explicit signal grammar into bounded graph rewrites.

    The model/Employee may *suggest* a semantic operation, but it never emits
    a graph patch directly.  This adapter accepts only a short typed envelope,
    reconstructs deterministic primitive operations from the current graph,
    and leaves the Kernel's graph, lease, approval and structural-distance
    validators authoritative.  Free prose keeps the prior fail-closed path.

    Supported envelopes are deliberately narrow::

        split:capability_a,capability_b
        join:completed_task_id
        merge:pending_sibling_a,pending_sibling_b

    ``split`` is valid only after an assumption invalidation; ``join`` and
    ``merge`` are valid only after a constraint change.  A Manager-only
    ``follow_up_capability:<capability>`` remains delegated to the prior
    CapabilityInsertReplanner rather than becoming a hidden fourth grammar.
    """

    _SPLIT = "split:"
    _JOIN = "join:"
    _MERGE = "merge:"

    def __init__(
        self,
        delegate: CapabilityInsertReplanner | ManagerFollowUpReplanner,
    ) -> None:
        self.delegate = delegate

    @property
    def exposed_workflow_prior_ids(self) -> list[str]:
        return self.delegate.exposed_workflow_prior_ids

    @property
    def aligned_workflow_prior_ids(self) -> list[str]:
        return self.delegate.aligned_workflow_prior_ids

    async def propose(self, context: ReplanContext) -> GraphPatch | None:
        signal = context.signal
        if signal.semantic_replan is not None:
            return self._directive_patch(context, signal.semantic_replan)
        value = signal.value.strip()
        try:
            if signal.code is SignalCode.ASSUMPTION_INVALIDATED and value.startswith(self._SPLIT):
                return self._split(context, self._identifiers(value.removeprefix(self._SPLIT)))
            if signal.code is SignalCode.CONSTRAINT_CHANGED and value.startswith(self._JOIN):
                return self._join(context, self._identifiers(value.removeprefix(self._JOIN)))
            if signal.code is SignalCode.CONSTRAINT_CHANGED and value.startswith(self._MERGE):
                return self._merge(context, self._identifiers(value.removeprefix(self._MERGE)))
        except (TypeError, ValueError, GraphValidationError):
            return None
        return await self.delegate.propose(context)

    def _directive_patch(
        self,
        context: ReplanContext,
        directive: SemanticReplanDirective,
    ) -> GraphPatch | None:
        """Map a typed semantic intent to a deterministic patch shape.

        The directive cannot name operations, dependencies, employees, tools,
        or budget. It can only point at bounded existing task ids plus opaque
        evidence references. That keeps every topology decision below this
        boundary reproducible from the current graph and preserves the legacy
        free-text path as a strict compatibility-only fallback.
        """

        try:
            directive.verify()
        except ValueError:
            return None
        signal = context.signal
        if directive.operation is SemanticReplanOperation.SPLIT:
            if signal.code is not SignalCode.ASSUMPTION_INVALIDATED:
                return None
            patch = self._split(context, directive.capability_ids)
        elif directive.operation is SemanticReplanOperation.JOIN:
            if signal.code is not SignalCode.CONSTRAINT_CHANGED:
                return None
            patch = self._join(context, directive.task_ids)
        elif directive.operation is SemanticReplanOperation.MERGE:
            if signal.code is not SignalCode.CONSTRAINT_CHANGED:
                return None
            patch = self._merge(context, directive.task_ids)
        elif directive.operation is SemanticReplanOperation.CANCEL:
            if signal.code is not SignalCode.CONSTRAINT_CHANGED:
                return None
            patch = self._cancel(context, directive.task_ids)
        else:  # Defensive for future enum extensions.
            return None
        if patch is None:
            return None
        return replace(
            patch,
            semantic_evidence_refs=tuple(
                dict.fromkeys(
                    (*directive.assumption_refs, *directive.constraint_refs)
                )
            ),
        )

    @staticmethod
    def _identifiers(value: str) -> tuple[str, ...]:
        items = tuple(item.strip() for item in value.split(",") if item.strip())
        if not items or len(items) != len(set(items)) or any(not _SAFE_CAPABILITY.fullmatch(item) for item in items):
            raise ValueError("semantic replan identifiers are invalid")
        return items

    @staticmethod
    def _next_id(existing: set[str], base: str) -> str:
        normalized = _SAFE_IDENTIFIER.sub("_", base.lower()).strip("_") or "semantic"
        candidate = normalized[:64].rstrip("_")
        suffix = 2
        while candidate in existing:
            tail = f"_{suffix}"
            candidate = normalized[: 64 - len(tail)].rstrip("_") + tail
            suffix += 1
        existing.add(candidate)
        return candidate

    @staticmethod
    def _temporary_capacity_allows(context: ReplanContext, required: tuple[tuple[str, ...], ...]) -> bool:
        profiles = [frozenset(employee.capabilities) for employee in context.roster if employee.active]
        remaining = context.request.job_limits.max_temporary_roles - sum(
            employee.temporary for employee in context.roster
        )
        created = 0
        for capabilities in required:
            profile = frozenset(capabilities)
            if any(profile.issubset(candidate) for candidate in profiles):
                continue
            created += 1
            if created > remaining:
                return False
            profiles.append(profile)
        return True

    def _split(self, context: ReplanContext, capabilities: tuple[str, ...]) -> GraphPatch | None:
        if len(capabilities) < 2 or len(capabilities) > 4:
            return None
        graph = context.graph
        tasks = task_map(graph)
        trigger = tasks.get(context.trigger_task.task_id)
        final = tasks.get(graph.final_task_id)
        if trigger is None or final is None or trigger.status is not TaskStatus.SUCCEEDED:
            return None
        added_count = len(capabilities) + (1 if trigger.task_id == final.task_id else 0)
        if len(tasks) + added_count > context.request.job_limits.max_tasks:
            return None
        required_profiles = tuple((capability,) for capability in capabilities) + (
            (final.required_capabilities,) if trigger.task_id == final.task_id else ()
        )
        if not self._temporary_capacity_allows(context, required_profiles):
            return None
        existing = set(tasks)
        branches = tuple(
            JobTask(
                task_id=self._next_id(existing, f"check_{capability}"),
                objective=f"Resolve the invalidated assumption through bounded {capability} evidence.",
                depends_on=(trigger.task_id,),
                required_capabilities=(capability,),
                acceptance_criteria=(f"Return evidence for the {capability} assumption check.",),
            )
            for capability in capabilities
        )
        operations: list[GraphPatchOperation] = [
            GraphPatchOperation(PatchOperationKind.ADD_TASK, task=task) for task in branches
        ]
        if trigger.task_id == final.task_id:
            integration = JobTask(
                task_id=self._next_id(existing, "integrate_assumption_checks"),
                objective="Integrate the original result and the independent assumption checks.",
                depends_on=(trigger.task_id, *(task.task_id for task in branches)),
                required_capabilities=final.required_capabilities,
                acceptance_criteria=final.acceptance_criteria,
                risk_level=final.risk_level,
            )
            operations.extend(
                (
                    GraphPatchOperation(PatchOperationKind.ADD_TASK, task=integration),
                    GraphPatchOperation(PatchOperationKind.SET_FINAL_TASK, task_id=integration.task_id),
                )
            )
        elif final.status is TaskStatus.PENDING:
            operations.extend(
                GraphPatchOperation(
                    PatchOperationKind.ADD_DEPENDENCY,
                    task_id=final.task_id,
                    dependency_id=task.task_id,
                )
                for task in branches
            )
        else:
            return None
        return GraphPatch(
            patch_id=f"semantic-split-{graph.version}-{trigger.task_id}",
            base_graph_version=graph.version,
            trigger_task_id=trigger.task_id,
            semantic_operation=SemanticOperation.SPLIT,
            rationale="A typed assumption invalidation requested independent capability checks.",
            expected_gain="Partition independent evidence before the final result is accepted.",
            operations=tuple(operations),
        )

    def _join(self, context: ReplanContext, task_ids: tuple[str, ...]) -> GraphPatch | None:
        if len(task_ids) != 1:
            return None
        tasks = task_map(context.graph)
        trigger = tasks.get(context.trigger_task.task_id)
        other = tasks.get(task_ids[0])
        final = tasks.get(context.graph.final_task_id)
        if (
            trigger is None or other is None or final is None
            or trigger.task_id == other.task_id
            or trigger.status is not TaskStatus.SUCCEEDED
            or other.status is not TaskStatus.SUCCEEDED
            or final.status is not TaskStatus.PENDING and final.task_id != trigger.task_id
            or not self._temporary_capacity_allows(context, (final.required_capabilities,))
            or len(tasks) >= context.request.job_limits.max_tasks
        ):
            return None
        existing = set(tasks)
        joined = JobTask(
            task_id=self._next_id(existing, f"join_{trigger.task_id}_{other.task_id}"),
            objective="Integrate the two completed evidence branches under the changed constraint.",
            depends_on=tuple(sorted((trigger.task_id, other.task_id))),
            required_capabilities=final.required_capabilities,
            acceptance_criteria=final.acceptance_criteria,
            risk_level=final.risk_level,
        )
        operations: tuple[GraphPatchOperation, ...]
        if final.task_id == trigger.task_id:
            operations = (
                GraphPatchOperation(PatchOperationKind.ADD_TASK, task=joined),
                GraphPatchOperation(PatchOperationKind.SET_FINAL_TASK, task_id=joined.task_id),
            )
        else:
            operations = (
                GraphPatchOperation(PatchOperationKind.ADD_TASK, task=joined),
                GraphPatchOperation(
                    PatchOperationKind.ADD_DEPENDENCY,
                    task_id=final.task_id,
                    dependency_id=joined.task_id,
                ),
            )
        return GraphPatch(
            patch_id=f"semantic-join-{context.graph.version}-{trigger.task_id}",
            base_graph_version=context.graph.version,
            trigger_task_id=trigger.task_id,
            semantic_operation=SemanticOperation.JOIN,
            rationale="A typed constraint change required two completed branches to be reconciled.",
            expected_gain="Create one explicit integration boundary before final acceptance.",
            operations=operations,
        )

    def _merge(self, context: ReplanContext, task_ids: tuple[str, ...]) -> GraphPatch | None:
        if not 2 <= len(task_ids) <= 4:
            return None
        tasks = task_map(context.graph)
        sources = tuple(tasks.get(task_id) for task_id in task_ids)
        if any(source is None or source.status is not TaskStatus.PENDING for source in sources):
            return None
        source_tasks = tuple(source for source in sources if source is not None)
        if context.graph.final_task_id in task_ids or len({source.depends_on for source in source_tasks}) != 1:
            return None
        if len(tasks) + 1 > context.request.job_limits.max_tasks:
            return None
        capabilities = tuple(sorted({capability for source in source_tasks for capability in source.required_capabilities}))
        acceptance = tuple(sorted({item for source in source_tasks for item in source.acceptance_criteria}))
        if not self._temporary_capacity_allows(context, (capabilities,)):
            return None
        existing = set(tasks)
        replacement = JobTask(
            task_id=self._next_id(existing, "merge_" + "_".join(task_ids)),
            objective="Consolidate the now-overlapping pending evidence work under the changed constraint.",
            depends_on=source_tasks[0].depends_on,
            required_capabilities=capabilities,
            acceptance_criteria=acceptance,
        )
        operations: list[GraphPatchOperation] = [
            GraphPatchOperation(PatchOperationKind.ADD_TASK, task=replacement),
            *(GraphPatchOperation(PatchOperationKind.CANCEL_TASK, task_id=source.task_id) for source in source_tasks),
        ]
        source_ids = set(task_ids)
        for task in tasks.values():
            if task.task_id in source_ids:
                continue
            dependencies = set(task.depends_on)
            if dependencies & source_ids:
                operations.append(
                    GraphPatchOperation(
                        PatchOperationKind.REPLACE_DEPENDENCIES,
                        task_id=task.task_id,
                        dependencies=tuple(sorted((dependencies - source_ids) | {replacement.task_id})),
                    )
                )
        return GraphPatch(
            patch_id=f"semantic-merge-{context.graph.version}-{context.trigger_task.task_id}",
            base_graph_version=context.graph.version,
            trigger_task_id=context.trigger_task.task_id,
            semantic_operation=SemanticOperation.MERGE,
            rationale="A typed constraint change identified pending sibling work that can share one bounded pass.",
            expected_gain="Remove duplicate pending work while preserving dependency and acceptance coverage.",
            operations=tuple(operations),
        )

    def _cancel(self, context: ReplanContext, task_ids: tuple[str, ...]) -> GraphPatch | None:
        """Retire bounded, no-longer-required final branches safely.

        A valid graph makes every live task contribute to its final task, so a
        truly independent live leaf cannot exist. Instead cancellation is
        restricted to one to four pending *direct final branches*: their final
        dependencies are explicitly removed in the same atomic patch, at least
        one unaffected final dependency must remain, and no other task may
        depend on them. Replacement/rewiring belongs to ``MERGE``.
        """

        if not 1 <= len(task_ids) <= 4:
            return None
        tasks = task_map(context.graph)
        sources = tuple(tasks.get(task_id) for task_id in task_ids)
        if any(source is None or source.status is not TaskStatus.PENDING for source in sources):
            return None
        final = tasks.get(context.graph.final_task_id)
        if final is None or final.status is not TaskStatus.PENDING:
            return None
        selected = set(task_ids)
        if not selected.issubset(set(final.depends_on)):
            return None
        if not set(final.depends_on) - selected:
            return None
        if any(
            selected & set(task.depends_on)
            for task in tasks.values()
            if task.task_id not in selected
            and task.task_id != final.task_id
            and task.status is not TaskStatus.CANCELLED
        ):
            return None
        return GraphPatch(
            patch_id=f"semantic-cancel-{context.graph.version}-{context.trigger_task.task_id}",
            base_graph_version=context.graph.version,
            trigger_task_id=context.trigger_task.task_id,
            semantic_operation=SemanticOperation.CANCEL,
            rationale="A typed constraint change made independent pending work unnecessary.",
            expected_gain="Remove obsolete pending final branches while preserving a bounded final path.",
            operations=(
                *(
                    GraphPatchOperation(
                        PatchOperationKind.REMOVE_DEPENDENCY,
                        task_id=final.task_id,
                        dependency_id=task_id,
                    )
                    for task_id in task_ids
                ),
                *(
                    GraphPatchOperation(PatchOperationKind.CANCEL_TASK, task_id=task_id)
                    for task_id in task_ids
                ),
            ),
        )
