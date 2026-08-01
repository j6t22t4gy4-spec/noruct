"""Project frozen local Evolution Artifacts into bounded runtime inputs.

The Evolution catalog is not a second COMPANY or employee runtime.  This
adapter is deliberately a read-only, per-Job projection: it turns only the
data-only part of a pinned Skill Package into the existing Employee Skill
snapshot contract.  It can also use an Agent Blueprint as a local role-to-skill
composition constraint.  No Artifact may add a tool, capability, credential,
employee, workflow task, or mutable memory through this module.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from dynamic_firm.company.models import canonical_json, content_digest
from dynamic_firm.compiler.models import CompilerExecutionProfile, WorkflowPrior
from dynamic_firm.kernel.models import EmployeeRecord
from dynamic_firm.network.adapters import (
    NetworkEvaluationAdapter,
    WorkflowPlaybookCandidate,
    evaluation_adapter_from_manifests,
    workflow_candidate_from_manifest,
    workflow_prior_from_candidate,
)
from dynamic_firm.runtime.models import VersionedContent

from .service import validate_evolution_artifact
from .mcp_package import mcp_policy_binding_digest_from_artifact
from .score_contract import evolution_content_digest
from .store import EvolutionStore


RUNTIME_ARTIFACT_ADAPTER_REVISION = "noruct-evolution-runtime-adapter-v1"
SUPPORTED_RUNTIME_CONTRACTS = frozenset({"noruct_v1"})
COMPANY_DEFAULT_SCOPE = "company_default"
_ROLE_KEY = re.compile(r"[^a-z0-9]+")


@dataclass(frozen=True, slots=True)
class RuntimeArtifactResolution:
    """Immutable execution projection for one already-pinned Job.

    ``employee_skills`` is intentionally compatible with
    ``CompanyRunRequest.employee_skill_snapshots``.  ``effects`` records why a
    pinned Artifact was projected or deliberately left declarative, so a
    release cannot appear to have changed execution when it did not.
    """

    job_id: str
    pins: tuple[Mapping[str, Any], ...]
    employee_skills: Mapping[str, tuple[VersionedContent, ...]]
    effects: tuple[Mapping[str, str], ...]
    mcp_policy_binding_digests: tuple[str, ...] = ()
    workflow_playbooks: tuple[WorkflowPlaybookCandidate, ...] = ()
    evaluation_adapters: tuple[NetworkEvaluationAdapter, ...] = ()


def runtime_artifact_scopes(roster: Sequence[EmployeeRecord]) -> tuple[str, ...]:
    """Return the only local scopes that may influence this Job.

    The company default is shared across the frozen roster.  An employee scope
    can refine that default for exactly one persistent employee.  Temporary
    employees never receive network-projected procedure knowledge.
    """

    scopes = [COMPANY_DEFAULT_SCOPE]
    scopes.extend(
        employee.employee_id
        for employee in roster
        if not employee.temporary
    )
    return tuple(dict.fromkeys(scopes))


def merge_employee_skill_snapshots(
    local: Mapping[str, tuple[VersionedContent, ...]],
    network: Mapping[str, tuple[VersionedContent, ...]],
) -> Mapping[str, tuple[VersionedContent, ...]]:
    """Merge immutable local and shared procedure candidates without mutation.

    The existing Kernel retrieval layer still selects at most three skills for
    an individual task.  Keeping local procedures first preserves tenant-local
    specificity when lexical scores tie; duplicate identities are rejected
    rather than silently replacing a local procedure.
    """

    result: dict[str, tuple[VersionedContent, ...]] = {}
    for employee_id in sorted(set(local) | set(network)):
        selected: list[VersionedContent] = []
        seen: set[tuple[str, str]] = set()
        for item in tuple(local.get(employee_id, ())) + tuple(network.get(employee_id, ())):
            identity = (item.content_id, item.revision)
            if identity in seen:
                continue
            seen.add(identity)
            selected.append(item)
        result[employee_id] = tuple(selected)
    return result


class EvolutionRuntimeArtifactAdapter:
    """Resolve local catalog entries that were frozen before a Job started."""

    def __init__(self, store: EvolutionStore) -> None:
        self.store = store

    def resolve(
        self,
        *,
        job_id: str,
        roster: Sequence[EmployeeRecord],
        pins: Sequence[Mapping[str, Any]] | None = None,
    ) -> RuntimeArtifactResolution:
        frozen_pins = tuple(
            pins if pins is not None else self.store.list_runtime_job_artifact_pins(job_id)
        )
        employees = tuple(employee for employee in roster if not employee.temporary)
        by_id = {employee.employee_id: employee for employee in employees}
        effects: list[Mapping[str, str]] = []
        skill_entries: list[tuple[Mapping[str, Any], Mapping[str, Any]]] = []
        blueprints: list[tuple[Mapping[str, Any], Mapping[str, Any]]] = []
        workflow_playbooks: list[WorkflowPlaybookCandidate] = []
        benchmarks: list[tuple[Mapping[str, Any], Mapping[str, Any]]] = []
        evaluators: list[tuple[Mapping[str, Any], Mapping[str, Any]]] = []
        mcp_policy_bindings: set[str] = set()

        for pin in frozen_pins:
            artifact_id = str(pin["artifact_id"])
            version = str(pin["version"])
            artifact = self.store.get_artifact_version(artifact_id, version)
            manifest = validate_evolution_artifact(artifact["manifest"])
            recomputed_digest = evolution_content_digest(manifest)
            if (
                str(artifact["manifest_digest"]) != recomputed_digest
                or str(pin["manifest_digest"]) != recomputed_digest
            ):
                raise ValueError(
                    "Pinned Evolution Artifact digest does not match its immutable manifest content"
                )
            effect_base = {
                "artifact_id": artifact_id,
                "version": version,
                "kind": str(manifest["kind"]),
                "scope_key": str(pin["scope_key"]),
            }
            if manifest["compatibility"]["runtime_contract"] not in SUPPORTED_RUNTIME_CONTRACTS:
                effects.append({**effect_base, "decision": "IGNORED_UNSUPPORTED_RUNTIME_CONTRACT"})
                continue
            if manifest["kind"] == "SKILL_PACKAGE":
                skill_entries.append((effect_base, manifest))
                continue
            if manifest["kind"] == "AGENT_BLUEPRINT":
                blueprints.append((effect_base, manifest))
                continue
            if manifest["kind"] == "WORKFLOW_PLAYBOOK":
                candidate = workflow_candidate_from_manifest(
                    manifest, scope_key=str(pin["scope_key"])
                )
                if candidate is None:
                    effects.append(
                        {**effect_base, "decision": "DECLARATIVE_ONLY_NO_EXECUTION_ADAPTER"}
                    )
                else:
                    workflow_playbooks.append(candidate)
                    effects.append(
                        {
                            **effect_base,
                            "decision": "REGISTERED_WORKFLOW_ADAPTER_PENDING_COMPILER_PROFILE",
                        }
                    )
                continue
            if manifest["kind"] == "BENCHMARK_SUITE":
                benchmarks.append((effect_base, manifest))
                continue
            if manifest["kind"] == "EVALUATOR_PROFILE":
                evaluators.append((effect_base, manifest))
                continue
            mcp_binding = mcp_policy_binding_digest_from_artifact(manifest)
            if mcp_binding is not None:
                mcp_policy_bindings.add(mcp_binding)
                effects.append({**effect_base, "decision": "PROJECTED_MCP_POLICY_BINDING"})
                continue
            effects.append({**effect_base, "decision": "DECLARATIVE_ONLY_NO_EXECUTION_ADAPTER"})

        blueprint_refs: dict[str, set[str]] = {employee_id: set() for employee_id in by_id}
        for effect_base, blueprint in blueprints:
            targets = self._scope_employees(effect_base["scope_key"], by_id)
            matched = [
                employee
                for employee in targets
                if _role_key(employee.role) == blueprint["content"]["role"]
            ]
            if not matched:
                effects.append({**effect_base, "decision": "IGNORED_BLUEPRINT_ROLE_NOT_IN_FROZEN_ROSTER"})
                continue
            references = set(blueprint["content"]["skill_refs"])
            for employee in matched:
                blueprint_refs[employee.employee_id].update(references)
            effects.append({**effect_base, "decision": "PROJECTED_BLUEPRINT_SKILL_REFS"})

        selected: dict[str, dict[str, tuple[int, VersionedContent]]] = {
            employee.employee_id: {} for employee in employees
        }
        for effect_base, skill in skill_entries:
            content = skill["content"]
            skill_key = str(content["skill_key"])
            targets = self._scope_employees(effect_base["scope_key"], by_id)
            projected = 0
            for employee in targets:
                direct_match = bool(
                    set(content["applies_to"])
                    & {employee.employee_id, _role_key(employee.role), *employee.capabilities}
                )
                blueprint_match = skill_key in blueprint_refs[employee.employee_id]
                if not direct_match and not blueprint_match:
                    continue
                value = self._skill_snapshot(skill, employee)
                # An employee-scoped release deliberately wins over the
                # company default for the same stable skill key.
                precedence = 1 if effect_base["scope_key"] == employee.employee_id else 0
                existing = selected[employee.employee_id].get(skill_key)
                if existing is None or precedence > existing[0]:
                    selected[employee.employee_id][skill_key] = (precedence, value)
                projected += 1
            effects.append(
                {
                    **effect_base,
                    "decision": (
                        "PROJECTED_TO_EMPLOYEE_SKILL_SNAPSHOT"
                        if projected
                        else "IGNORED_SKILL_NOT_APPLICABLE_TO_FROZEN_ROSTER"
                    ),
                }
            )

        evaluation_adapters: list[NetworkEvaluationAdapter] = []
        matched_benchmarks: set[tuple[str, str]] = set()
        matched_evaluators: set[tuple[str, str]] = set()
        for benchmark_effect, benchmark in benchmarks:
            for evaluator_effect, evaluator in evaluators:
                adapter = evaluation_adapter_from_manifests(benchmark, evaluator)
                if adapter is None:
                    continue
                evaluation_adapters.append(adapter)
                matched_benchmarks.add((str(benchmark["artifact_id"]), str(benchmark["version"])))
                matched_evaluators.add((str(evaluator["artifact_id"]), str(evaluator["version"])))
                effects.append(
                    {
                        **benchmark_effect,
                        "decision": "PROJECTED_BENCHMARK_EVALUATOR_ADAPTER",
                    }
                )
                effects.append(
                    {
                        **evaluator_effect,
                        "decision": "PROJECTED_BENCHMARK_EVALUATOR_ADAPTER",
                    }
                )
        for effect_base, benchmark in benchmarks:
            if (str(benchmark["artifact_id"]), str(benchmark["version"])) not in matched_benchmarks:
                effects.append(
                    {**effect_base, "decision": "DECLARATIVE_ONLY_NO_MATCHING_EVALUATOR_ADAPTER"}
                )
        for effect_base, evaluator in evaluators:
            if (str(evaluator["artifact_id"]), str(evaluator["version"])) not in matched_evaluators:
                effects.append(
                    {**effect_base, "decision": "DECLARATIVE_ONLY_NO_MATCHING_BENCHMARK_ADAPTER"}
                )

        return RuntimeArtifactResolution(
            job_id=job_id,
            pins=frozen_pins,
            employee_skills={
                employee_id: tuple(
                    value
                    for _, value in sorted(
                        entries.values(), key=lambda item: (item[1].content_id, item[1].revision)
                    )
                )
                for employee_id, entries in selected.items()
            },
            effects=tuple(effects),
            mcp_policy_binding_digests=tuple(sorted(mcp_policy_bindings)),
            workflow_playbooks=tuple(workflow_playbooks),
            evaluation_adapters=tuple(evaluation_adapters),
        )

    @staticmethod
    def _scope_employees(
        scope_key: str,
        by_id: Mapping[str, EmployeeRecord],
    ) -> tuple[EmployeeRecord, ...]:
        if scope_key == COMPANY_DEFAULT_SCOPE:
            return tuple(by_id.values())
        employee = by_id.get(scope_key)
        return () if employee is None else (employee,)

    @staticmethod
    def _skill_snapshot(
        manifest: Mapping[str, Any], employee: EmployeeRecord) -> VersionedContent:
        content = manifest["content"]
        rendered = canonical_json(
            {
                "contract": RUNTIME_ARTIFACT_ADAPTER_REVISION,
                "precedence": (
                    "COMPANY_AND_ACTION_POLICY_THEN_PLAYBOOK_THEN_LOCAL_EMPLOYEE_SKILL_THEN_NETWORK_SKILL"
                ),
                "artifact": {
                    "artifact_id": manifest["artifact_id"],
                    "version": manifest["version"],
                    "kind": manifest["kind"],
                },
                "procedure": {
                    "skill_key": content["skill_key"],
                    "steps": content["steps"],
                    "required_capabilities": content["required_capabilities"],
                    "source_receipt_digest": content.get("source_receipt_digest"),
                },
            }
        )
        return VersionedContent(
            content_id=(
                f"employee-skill:{employee.employee_id}:network:{manifest['artifact_id']}"
            ),
            revision=str(manifest["version"]),
            content=rendered,
            content_hash=content_digest(rendered),
        )


def project_network_workflow_priors(
    resolution: RuntimeArtifactResolution,
    *,
    execution_profile: CompilerExecutionProfile,
    available_capabilities: Sequence[str],
) -> tuple[tuple[WorkflowPrior, ...], tuple[Mapping[str, str], ...]]:
    """Project compatible registered playbooks to bounded Compiler priors.

    This is intentionally a planning hint rather than a graph mutation.  The
    existing Compiler still receives the user goal, verifies its own proposal,
    and the Firm Kernel is still the authority that admits the resulting Job.
    """

    priors: list[WorkflowPrior] = []
    effects: list[Mapping[str, str]] = []
    for candidate in resolution.workflow_playbooks:
        prior, decision = workflow_prior_from_candidate(
            candidate,
            execution_profile=execution_profile,
            available_capabilities=available_capabilities,
        )
        effects.append(
            {
                "artifact_id": candidate.artifact_id,
                "version": candidate.version,
                "kind": "WORKFLOW_PLAYBOOK",
                "scope_key": candidate.scope_key,
                "decision": decision,
            }
        )
        if prior is not None:
            priors.append(prior)
    return tuple(priors), tuple(effects)


def _role_key(role: str) -> str:
    return _ROLE_KEY.sub("_", role.strip().casefold()).strip("_")
