"""Data-only Community Graph Blueprint lifecycle.

This module is deliberately narrower than a network client.  It creates a
reviewable public artifact from a local Blueprint *without* exporting its
objective/acceptance templates, Work Order, run record, Knowledge, memory,
path, prompt, or credential.  A future opt-in network adapter may transport
the release payload, but it cannot bypass this serializer or the ordinary
local Blueprint registry and Kernel validation path.
"""

from __future__ import annotations

import json
import math
import sqlite3
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Mapping

from dynamic_firm.kernel.models import (
    ExecutionReplicaAggregation,
    ExecutionReplicaStrategy,
)

from .graph_blueprint_models import (
    GraphBlueprint,
    GraphBlueprintExecutionReplica,
    GraphBlueprintOrigin,
    GraphBlueprintRef,
    GraphBlueprintTask,
    digest,
    identifier,
)


COMMUNITY_GRAPH_BLUEPRINT_SCHEMA = "noruct.community-graph-blueprint.v1"
COMMUNITY_GRAPH_BLUEPRINT_RELEASE_SCHEMA = "noruct.community-graph-blueprint-release.v1"
COMMUNITY_GRAPH_BLUEPRINT_DRAFT_SCHEMA = "noruct.community-graph-blueprint-draft.v1"
COMMUNITY_RUNTIME_CONTRACT = "employee-runtime-v1"
COMMUNITY_GRAPH_ARTIFACT_KIND = "GRAPH_BLUEPRINT"
EVOLUTION_ARTIFACT_SCHEMA = "noruct.evolution-artifact.v1"
WORKFORCE_PASSPORT_SCHEMA = "noruct.workforce-passport.v1"
_MAX_PUBLIC_TASKS = 32
_FORBIDDEN_PUBLIC_KEYS = (
    "work_order",
    "prompt",
    "transcript",
    "credential",
    "secret",
    "knowledge",
    "memory",
    "run_record",
    "source",
    "path",
    "objective_template",
    "acceptance_template",
    "scope_template",
    "rationale",
)


def _sha256(value: str) -> bool:
    return len(value) == 64 and all(character in "0123456789abcdef" for character in value)


def _public_identifier(value: str, label: str) -> str:
    identifier(value, label)
    if len(value) > 55:
        raise ValueError(f"{label} must be at most 55 characters for a local staged id")
    return value


def _non_empty_identifier_tuple(values: tuple[str, ...], label: str) -> tuple[str, ...]:
    if not isinstance(values, tuple) or not values:
        raise ValueError(f"{label} must be a non-empty tuple")
    normalized = tuple(identifier(item, label) for item in values)
    if len(normalized) != len(set(normalized)):
        raise ValueError(f"{label} must be unique")
    return normalized


@dataclass(frozen=True, slots=True)
class CommunityBlueprintReplica:
    """Structural replica metadata with no private scope or reasoning text."""

    group_id: str
    replica_id: str
    strategy: ExecutionReplicaStrategy
    aggregation_task_id: str
    aggregation: ExecutionReplicaAggregation

    def __post_init__(self) -> None:
        identifier(self.group_id, "community replica group_id")
        identifier(self.replica_id, "community replica replica_id")
        identifier(self.aggregation_task_id, "community replica aggregation_task_id")
        if not isinstance(self.strategy, ExecutionReplicaStrategy):
            raise TypeError("community replica strategy must be typed")
        if not isinstance(self.aggregation, ExecutionReplicaAggregation):
            raise TypeError("community replica aggregation must be typed")

    def payload(self) -> dict[str, str]:
        return {
            "group_id": self.group_id,
            "replica_id": self.replica_id,
            "strategy": self.strategy.value,
            "aggregation_task_id": self.aggregation_task_id,
            "aggregation": self.aggregation.value,
        }


@dataclass(frozen=True, slots=True)
class CommunityBlueprintTask:
    """Only reusable task topology and capability demand are public."""

    task_id: str
    depends_on: tuple[str, ...]
    required_capabilities: tuple[str, ...]
    execution_replica: CommunityBlueprintReplica | None = None

    def __post_init__(self) -> None:
        identifier(self.task_id, "community task_id")
        dependencies = tuple(identifier(item, "community task dependency") for item in self.depends_on)
        capabilities = _non_empty_identifier_tuple(
            self.required_capabilities, "community task capability"
        )
        if len(dependencies) != len(set(dependencies)) or self.task_id in dependencies:
            raise ValueError("community task dependencies must be unique and cannot self-reference")
        if self.execution_replica is not None and not isinstance(
            self.execution_replica, CommunityBlueprintReplica
        ):
            raise TypeError("community execution_replica must be typed")

    def payload(self) -> dict[str, object]:
        return {
            "task_id": self.task_id,
            "depends_on": list(self.depends_on),
            "required_capabilities": list(self.required_capabilities),
            **(
                {"execution_replica": self.execution_replica.payload()}
                if self.execution_replica is not None
                else {}
            ),
        }


@dataclass(frozen=True, slots=True)
class BlueprintPassport:
    """Numeric evidence envelope; missing evidence is explicit, never implied."""

    runtime_contract: str = COMMUNITY_RUNTIME_CONTRACT
    evaluator_revision: str = "unqualified"
    sample_count: int = 0
    p10_quality: float | None = None
    complete_failure_rate: float | None = None
    safety_failure_rate: float | None = None
    mean_model_calls: float | None = None
    mean_elapsed_ms: float | None = None
    mutation_frequency: float | None = None
    known_limitations: tuple[str, ...] = ("no_qualified_outcome_evidence",)
    evidence_digest: str | None = None

    def __post_init__(self) -> None:
        identifier(self.runtime_contract, "community runtime_contract")
        identifier(self.evaluator_revision, "community evaluator_revision")
        if type(self.sample_count) is not int or self.sample_count < 0:
            raise ValueError("community sample_count must be non-negative")
        metrics = (
            self.p10_quality,
            self.complete_failure_rate,
            self.safety_failure_rate,
            self.mean_model_calls,
            self.mean_elapsed_ms,
            self.mutation_frequency,
        )
        if self.sample_count == 0 and any(value is not None for value in metrics):
            raise ValueError("unqualified community Passport cannot claim numeric evidence")
        if self.sample_count > 0 and any(value is None for value in metrics):
            raise ValueError("qualified community Passport requires every numeric metric")
        if self.sample_count == 0 and self.evidence_digest is not None:
            raise ValueError("unqualified community Passport cannot claim evidence provenance")
        if self.evidence_digest is not None and not _sha256(self.evidence_digest):
            raise ValueError("community Passport evidence digest is invalid")
        for label, value, lower, upper in (
            ("p10_quality", self.p10_quality, 0.0, 1.0),
            ("complete_failure_rate", self.complete_failure_rate, 0.0, 1.0),
            ("safety_failure_rate", self.safety_failure_rate, 0.0, 1.0),
            ("mean_model_calls", self.mean_model_calls, 0.0, None),
            ("mean_elapsed_ms", self.mean_elapsed_ms, 0.0, None),
            ("mutation_frequency", self.mutation_frequency, 0.0, None),
        ):
            if value is None:
                continue
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
                raise ValueError(f"community Passport {label} must be finite")
            if float(value) < lower or (upper is not None and float(value) > upper):
                raise ValueError(f"community Passport {label} is outside its range")
        if not isinstance(self.known_limitations, tuple) or not self.known_limitations:
            raise ValueError("community Passport requires an explicit limitation")
        for limitation in self.known_limitations:
            identifier(limitation, "community Passport limitation")

    def payload(self) -> dict[str, object]:
        return {
            "runtime_contract": self.runtime_contract,
            "evaluator_revision": self.evaluator_revision,
            "sample_count": self.sample_count,
            "p10_quality": self.p10_quality,
            "complete_failure_rate": self.complete_failure_rate,
            "safety_failure_rate": self.safety_failure_rate,
            "mean_model_calls": self.mean_model_calls,
            "mean_elapsed_ms": self.mean_elapsed_ms,
            "mutation_frequency": self.mutation_frequency,
            "known_limitations": list(self.known_limitations),
            "evidence_digest": self.evidence_digest,
        }


@dataclass(frozen=True, slots=True)
class CommunityGraphBlueprint:
    """Strict public structural artifact, intentionally unable to execute."""

    artifact_id: str
    revision: int
    objective_class: str
    execution_profiles: tuple[str, ...]
    tasks: tuple[CommunityBlueprintTask, ...]
    final_task_id: str
    passport: BlueprintPassport = BlueprintPassport()
    parent_release_digest: str | None = None
    content_digest: str = field(init=False)

    def __post_init__(self) -> None:
        _public_identifier(self.artifact_id, "community artifact_id")
        identifier(self.objective_class, "community objective_class")
        _non_empty_identifier_tuple(self.execution_profiles, "community execution_profile")
        if type(self.revision) is not int or self.revision < 1:
            raise ValueError("community revision must be positive")
        if not 1 <= len(self.tasks) <= _MAX_PUBLIC_TASKS:
            raise ValueError("community Blueprint task count is outside the public bound")
        task_ids = tuple(task.task_id for task in self.tasks)
        if len(task_ids) != len(set(task_ids)) or self.final_task_id not in set(task_ids):
            raise ValueError("community Blueprint task identity is invalid")
        known = set(task_ids)
        for task in self.tasks:
            if not set(task.depends_on).issubset(known):
                raise ValueError("community Blueprint dependency is unknown")
        if not isinstance(self.passport, BlueprintPassport):
            raise TypeError("community Blueprint Passport must be typed")
        if self.parent_release_digest is not None and not _sha256(self.parent_release_digest):
            raise ValueError("community parent_release_digest must be a SHA-256 digest")
        object.__setattr__(self, "content_digest", digest(self.canonical_payload()))

    def canonical_payload(self) -> dict[str, object]:
        return {
            "schema": COMMUNITY_GRAPH_BLUEPRINT_SCHEMA,
            "artifact_id": self.artifact_id,
            "revision": self.revision,
            "objective_class": self.objective_class,
            "execution_profiles": list(self.execution_profiles),
            "tasks": [task.payload() for task in self.tasks],
            "final_task_id": self.final_task_id,
            "passport": self.passport.payload(),
            "parent_release_digest": self.parent_release_digest,
        }

    def verify(self) -> None:
        if self.content_digest != digest(self.canonical_payload()):
            raise ValueError("community Blueprint content digest is invalid")

    def public_payload(self) -> dict[str, object]:
        return {**self.canonical_payload(), "content_digest": self.content_digest}


@dataclass(frozen=True, slots=True)
class CommunityGraphBlueprintRelease:
    """Transportable release wrapper with no local source reference."""

    artifact: CommunityGraphBlueprint
    release_id: str = field(init=False)
    release_digest: str = field(init=False)

    def __post_init__(self) -> None:
        self.artifact.verify()
        release_digest = digest(self.canonical_payload())
        object.__setattr__(self, "release_digest", release_digest)
        object.__setattr__(self, "release_id", f"community-release-{release_digest[:24]}")

    def canonical_payload(self) -> dict[str, object]:
        return {
            "schema": COMMUNITY_GRAPH_BLUEPRINT_RELEASE_SCHEMA,
            "artifact": self.artifact.public_payload(),
        }

    def public_payload(self) -> dict[str, object]:
        return {
            **self.canonical_payload(),
            "release_id": self.release_id,
            "release_digest": self.release_digest,
        }

    def verify(self) -> None:
        self.artifact.verify()
        if self.release_digest != digest(self.canonical_payload()):
            raise ValueError("community Blueprint release digest is invalid")


class CommunityBlueprintPublicationState(StrEnum):
    DRAFT = "DRAFT"
    PENDING_REVIEW = "PENDING_REVIEW"
    WITHDRAWN = "WITHDRAWN"


@dataclass(frozen=True, slots=True)
class CommunityBlueprintDraft:
    """Private local publication state. ``source_ref`` is never exported."""

    draft_id: str
    source_ref: GraphBlueprintRef
    release: CommunityGraphBlueprintRelease
    state: CommunityBlueprintPublicationState = CommunityBlueprintPublicationState.DRAFT

    def __post_init__(self) -> None:
        _public_identifier(self.draft_id, "community draft_id")
        if not isinstance(self.state, CommunityBlueprintPublicationState):
            raise TypeError("community draft state must be typed")
        self.release.verify()


@dataclass(frozen=True, slots=True)
class CommunityBlueprintStageReceipt:
    """Private local binding from one exact public release to one local ref."""

    release_digest: str
    artifact_content_digest: str
    staged_ref: GraphBlueprintRef

    def __post_init__(self) -> None:
        if not _sha256(self.release_digest) or not _sha256(self.artifact_content_digest):
            raise ValueError("community Blueprint stage receipt digest is invalid")


def _reject_private_keys(value: object) -> None:
    if isinstance(value, Mapping):
        for key, nested in value.items():
            key_text = str(key).lower()
            if any(forbidden in key_text for forbidden in _FORBIDDEN_PUBLIC_KEYS):
                raise ValueError("community Blueprint payload contains a forbidden private field")
            _reject_private_keys(nested)
    elif isinstance(value, list):
        for nested in value:
            _reject_private_keys(nested)


def _replica_from_local(replica: GraphBlueprintExecutionReplica | None) -> CommunityBlueprintReplica | None:
    if replica is None:
        return None
    return CommunityBlueprintReplica(
        group_id=replica.group_id,
        replica_id=replica.replica_id,
        strategy=replica.strategy,
        aggregation_task_id=replica.aggregation_task_id,
        aggregation=replica.aggregation,
    )


def project_community_blueprint(
    blueprint: GraphBlueprint,
    *,
    artifact_id: str,
    revision: int = 1,
    parent_release_digest: str | None = None,
    passport: BlueprintPassport | None = None,
) -> CommunityGraphBlueprint:
    """Project a local Blueprint into public topology while dropping all text.

    The local Blueprint may contain a user objective in templates.  It is read
    only long enough to derive IDs, dependencies, capability demand and typed
    replica topology; no source template appears in the resulting artifact.
    """

    blueprint.verify()
    return CommunityGraphBlueprint(
        artifact_id=artifact_id,
        revision=revision,
        objective_class=blueprint.objective_class,
        execution_profiles=blueprint.execution_profiles,
        tasks=tuple(
            CommunityBlueprintTask(
                task_id=task.task_id,
                depends_on=task.depends_on,
                required_capabilities=task.required_capabilities,
                execution_replica=_replica_from_local(task.execution_replica),
            )
            for task in blueprint.tasks
        ),
        final_task_id=blueprint.final_task_id,
        passport=BlueprintPassport() if passport is None else passport,
        parent_release_digest=parent_release_digest,
    )


def _parse_replica(payload: object) -> CommunityBlueprintReplica | None:
    if payload is None:
        return None
    if not isinstance(payload, Mapping) or set(payload) != {
        "group_id", "replica_id", "strategy", "aggregation_task_id", "aggregation"
    }:
        raise ValueError("community Blueprint replica payload is invalid")
    return CommunityBlueprintReplica(
        group_id=str(payload["group_id"]),
        replica_id=str(payload["replica_id"]),
        strategy=ExecutionReplicaStrategy(str(payload["strategy"])),
        aggregation_task_id=str(payload["aggregation_task_id"]),
        aggregation=ExecutionReplicaAggregation(str(payload["aggregation"])),
    )


def _parse_passport(payload: object) -> BlueprintPassport:
    expected = {
        "runtime_contract", "evaluator_revision", "sample_count", "p10_quality",
        "complete_failure_rate", "safety_failure_rate", "mean_model_calls",
        "mean_elapsed_ms", "mutation_frequency", "known_limitations",
    }
    # v1 releases did not bind qualified metrics to a precise input digest.
    # Keep them parseable, but never synthesize provenance for them.
    allowed = expected | {"evidence_digest"}
    if not isinstance(payload, Mapping) or not expected.issubset(payload) or not set(payload).issubset(allowed):
        raise ValueError("community Blueprint Passport payload is invalid")
    return BlueprintPassport(
        runtime_contract=str(payload["runtime_contract"]),
        evaluator_revision=str(payload["evaluator_revision"]),
        sample_count=payload["sample_count"],
        p10_quality=payload["p10_quality"],
        complete_failure_rate=payload["complete_failure_rate"],
        safety_failure_rate=payload["safety_failure_rate"],
        mean_model_calls=payload["mean_model_calls"],
        mean_elapsed_ms=payload["mean_elapsed_ms"],
        mutation_frequency=payload["mutation_frequency"],
        known_limitations=tuple(str(item) for item in payload["known_limitations"]),
        evidence_digest=(
            None if payload.get("evidence_digest") is None else str(payload["evidence_digest"])
        ),
    )


def blueprint_passport_from_payload(payload: object) -> BlueprintPassport:
    """Parse a public Passport without opening a local publication registry."""

    return _parse_passport(payload)


def community_blueprint_from_payload(payload: object) -> CommunityGraphBlueprint:
    if not isinstance(payload, Mapping):
        raise ValueError("community Blueprint payload must be an object")
    _reject_private_keys(payload)
    expected = {
        "schema", "artifact_id", "revision", "objective_class", "execution_profiles",
        "tasks", "final_task_id", "passport", "parent_release_digest", "content_digest",
    }
    if set(payload) != expected or payload.get("schema") != COMMUNITY_GRAPH_BLUEPRINT_SCHEMA:
        raise ValueError("community Blueprint schema is invalid")
    raw_tasks = payload["tasks"]
    if not isinstance(raw_tasks, list):
        raise ValueError("community Blueprint tasks must be a list")
    tasks: list[CommunityBlueprintTask] = []
    for item in raw_tasks:
        expected_task = {"task_id", "depends_on", "required_capabilities"}
        if not isinstance(item, Mapping) or not set(item).issubset(expected_task | {"execution_replica"}) or not expected_task.issubset(item):
            raise ValueError("community Blueprint task payload is invalid")
        tasks.append(
            CommunityBlueprintTask(
                task_id=str(item["task_id"]),
                depends_on=tuple(str(value) for value in item["depends_on"]),
                required_capabilities=tuple(str(value) for value in item["required_capabilities"]),
                execution_replica=_parse_replica(item.get("execution_replica")),
            )
        )
    blueprint = CommunityGraphBlueprint(
        artifact_id=str(payload["artifact_id"]),
        revision=payload["revision"],
        objective_class=str(payload["objective_class"]),
        execution_profiles=tuple(str(value) for value in payload["execution_profiles"]),
        tasks=tuple(tasks),
        final_task_id=str(payload["final_task_id"]),
        passport=_parse_passport(payload["passport"]),
        parent_release_digest=(
            None if payload["parent_release_digest"] is None else str(payload["parent_release_digest"])
        ),
    )
    if payload["content_digest"] != blueprint.content_digest:
        raise ValueError("community Blueprint artifact identity is invalid")
    return blueprint


def community_release_from_payload(payload: object) -> CommunityGraphBlueprintRelease:
    if not isinstance(payload, Mapping):
        raise ValueError("community Blueprint release payload must be an object")
    _reject_private_keys(payload)
    expected = {"schema", "artifact", "release_id", "release_digest"}
    if set(payload) != expected or payload.get("schema") != COMMUNITY_GRAPH_BLUEPRINT_RELEASE_SCHEMA:
        raise ValueError("community Blueprint release schema is invalid")
    release = CommunityGraphBlueprintRelease(community_blueprint_from_payload(payload["artifact"]))
    if payload.get("release_id") != release.release_id or payload.get("release_digest") != release.release_digest:
        raise ValueError("community Blueprint release identity is invalid")
    return release


def materialize_staged_blueprint(release: CommunityGraphBlueprintRelease) -> GraphBlueprint:
    """Create a safe local draft with first-party generic templates only."""

    release.verify()
    artifact = release.artifact
    tasks = tuple(
        GraphBlueprintTask(
            task_id=task.task_id,
            objective_template=(
                f"Complete the {{{{objective}}}} work required by {task.task_id}."
            ),
            depends_on=task.depends_on,
            required_capabilities=task.required_capabilities,
            acceptance_templates=(
                f"Return bounded evidence for {{{{requested_outcome}}}} from {task.task_id}.",
            ),
            execution_replica=(
                None
                if task.execution_replica is None
                else GraphBlueprintExecutionReplica(
                    group_id=task.execution_replica.group_id,
                    replica_id=task.execution_replica.replica_id,
                    strategy=task.execution_replica.strategy,
                    scope_template=(
                        f"Independent {task.task_id} scope for {{{{objective}}}}."
                    ),
                    aggregation_task_id=task.execution_replica.aggregation_task_id,
                    aggregation=task.execution_replica.aggregation,
                    marginal_value_reason_template=(
                        "The structural replica is evaluated locally for {{requested_outcome}}."
                    ),
                )
            ),
        )
        for task in artifact.tasks
    )
    return GraphBlueprint(
        blueprint_id=f"community_{artifact.artifact_id}",
        version=artifact.revision,
        objective_class=artifact.objective_class,
        execution_profiles=artifact.execution_profiles,
        parameters=("objective", "requested_outcome"),
        tasks=tasks,
        final_task_id=artifact.final_task_id,
        origin=GraphBlueprintOrigin.STAGED_COMMUNITY,
    )


class CommunityBlueprintRegistry:
    """Local pending-publication ledger; no network, credential, or Job authority."""

    def __init__(self, path: Path | str | None = None) -> None:
        self._drafts: dict[str, CommunityBlueprintDraft] = {}
        self._connection: sqlite3.Connection | None = None
        if path is not None:
            self._connection = sqlite3.connect(str(path))
            self._connection.row_factory = sqlite3.Row
            self._connection.execute(
                """
                CREATE TABLE IF NOT EXISTS community_blueprint_drafts (
                    draft_id TEXT PRIMARY KEY,
                    source_blueprint_id TEXT NOT NULL,
                    source_version INTEGER NOT NULL,
                    source_digest TEXT NOT NULL,
                    release_json TEXT NOT NULL,
                    state TEXT NOT NULL
                )
                """
            )
            self._connection.execute(
                """
                CREATE TABLE IF NOT EXISTS community_blueprint_stages (
                    release_digest TEXT PRIMARY KEY,
                    artifact_content_digest TEXT NOT NULL,
                    staged_blueprint_id TEXT NOT NULL,
                    staged_version INTEGER NOT NULL,
                    staged_content_digest TEXT NOT NULL,
                    UNIQUE(staged_blueprint_id, staged_version)
                )
                """
            )
            self._connection.commit()

    def close(self) -> None:
        if self._connection is not None:
            self._connection.close()
            self._connection = None

    def __enter__(self) -> "CommunityBlueprintRegistry":
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.close()

    def _save(self, draft: CommunityBlueprintDraft) -> CommunityBlueprintDraft:
        existing = self.get(draft.draft_id, required=False)
        if existing is not None and (
            existing.source_ref != draft.source_ref or existing.release != draft.release
        ):
            raise ValueError("community Blueprint draft cannot be overwritten")
        self._drafts[draft.draft_id] = draft
        if self._connection is not None:
            with self._connection:
                self._connection.execute(
                    """
                    INSERT INTO community_blueprint_drafts(
                        draft_id, source_blueprint_id, source_version, source_digest, release_json, state
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """
                    """ ON CONFLICT(draft_id) DO UPDATE SET
                        source_blueprint_id=excluded.source_blueprint_id,
                        source_version=excluded.source_version,
                        source_digest=excluded.source_digest,
                        release_json=excluded.release_json,
                        state=excluded.state
                    """,
                    (
                        draft.draft_id,
                        draft.source_ref.blueprint_id,
                        draft.source_ref.version,
                        draft.source_ref.content_digest,
                        json.dumps(draft.release.public_payload(), ensure_ascii=False, sort_keys=True, separators=(",", ":")),
                        draft.state.value,
                    ),
                )
        return draft

    def get(self, draft_id: str, *, required: bool = True) -> CommunityBlueprintDraft | None:
        identifier(draft_id, "community draft_id")
        if draft_id in self._drafts:
            return self._drafts[draft_id]
        if self._connection is not None:
            row = self._connection.execute(
                "SELECT * FROM community_blueprint_drafts WHERE draft_id = ?", (draft_id,)
            ).fetchone()
            if row is not None:
                draft = CommunityBlueprintDraft(
                    draft_id=str(row["draft_id"]),
                    source_ref=GraphBlueprintRef(
                        str(row["source_blueprint_id"]),
                        int(row["source_version"]),
                        str(row["source_digest"]),
                    ),
                    release=community_release_from_payload(json.loads(str(row["release_json"]))),
                    state=CommunityBlueprintPublicationState(str(row["state"])),
                )
                self._drafts[draft_id] = draft
                return draft
        if required:
            raise ValueError("community Blueprint draft is not available")
        return None

    def list(self) -> tuple[CommunityBlueprintDraft, ...]:
        if self._connection is not None:
            rows = self._connection.execute(
                "SELECT draft_id FROM community_blueprint_drafts ORDER BY draft_id"
            ).fetchall()
            return tuple(self.get(str(row["draft_id"])) for row in rows)  # type: ignore[arg-type]
        return tuple(self._drafts[key] for key in sorted(self._drafts))

    def prepare(
        self,
        source: GraphBlueprint,
        *,
        draft_id: str,
        artifact_id: str,
        passport: BlueprintPassport | None = None,
    ) -> CommunityBlueprintDraft:
        if self.get(draft_id, required=False) is not None:
            raise ValueError("community Blueprint draft already exists")
        release = CommunityGraphBlueprintRelease(
            project_community_blueprint(source, artifact_id=artifact_id, passport=passport)
        )
        return self._save(CommunityBlueprintDraft(draft_id, source.ref, release))

    def publish(self, draft_id: str) -> CommunityBlueprintDraft:
        draft = self.get(draft_id)
        assert draft is not None
        if draft.state is not CommunityBlueprintPublicationState.DRAFT:
            raise ValueError("only a community Blueprint DRAFT can enter review")
        return self._save(
            CommunityBlueprintDraft(
                draft.draft_id,
                draft.source_ref,
                draft.release,
                CommunityBlueprintPublicationState.PENDING_REVIEW,
            )
        )

    def withdraw(self, draft_id: str) -> CommunityBlueprintDraft:
        draft = self.get(draft_id)
        assert draft is not None
        if draft.state is not CommunityBlueprintPublicationState.PENDING_REVIEW:
            raise ValueError("only a pending community Blueprint can be withdrawn")
        return self._save(
            CommunityBlueprintDraft(
                draft.draft_id,
                draft.source_ref,
                draft.release,
                CommunityBlueprintPublicationState.WITHDRAWN,
            )
        )

    def export_release(self, draft_id: str) -> dict[str, object]:
        draft = self.get(draft_id)
        assert draft is not None
        if draft.state is not CommunityBlueprintPublicationState.PENDING_REVIEW:
            raise ValueError("only a pending community Blueprint can be exported")
        return draft.release.public_payload()

    def record_stage(
        self,
        release: CommunityGraphBlueprintRelease,
        staged_ref: GraphBlueprintRef,
    ) -> CommunityBlueprintStageReceipt:
        """Record exact release-to-local identity without changing the local graph."""

        release.verify()
        receipt = CommunityBlueprintStageReceipt(
            release.release_digest,
            release.artifact.content_digest,
            staged_ref,
        )
        existing = self.stage_for(staged_ref, required=False)
        if existing is not None and existing != receipt:
            raise ValueError("a different Community release is already staged at this local revision")
        if self._connection is not None:
            with self._connection:
                self._connection.execute(
                    """
                    INSERT INTO community_blueprint_stages(
                        release_digest, artifact_content_digest, staged_blueprint_id, staged_version, staged_content_digest
                    ) VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(release_digest) DO UPDATE SET
                        artifact_content_digest=excluded.artifact_content_digest,
                        staged_blueprint_id=excluded.staged_blueprint_id,
                        staged_version=excluded.staged_version,
                        staged_content_digest=excluded.staged_content_digest
                    """,
                    (
                        receipt.release_digest,
                        receipt.artifact_content_digest,
                        receipt.staged_ref.blueprint_id,
                        receipt.staged_ref.version,
                        receipt.staged_ref.content_digest,
                    ),
                )
        return receipt

    def stage_for(
        self,
        staged_ref: GraphBlueprintRef,
        *,
        required: bool = True,
    ) -> CommunityBlueprintStageReceipt | None:
        if self._connection is None:
            if required:
                raise ValueError("community Blueprint stage receipt is not available")
            return None
        row = self._connection.execute(
            """
            SELECT release_digest, artifact_content_digest, staged_blueprint_id, staged_version, staged_content_digest
            FROM community_blueprint_stages
            WHERE staged_blueprint_id = ? AND staged_version = ?
            """,
            (staged_ref.blueprint_id, staged_ref.version),
        ).fetchone()
        if row is None:
            if required:
                raise ValueError("community Blueprint stage receipt is not available")
            return None
        receipt = CommunityBlueprintStageReceipt(
            str(row["release_digest"]),
            str(row["artifact_content_digest"]),
            GraphBlueprintRef(
                str(row["staged_blueprint_id"]),
                int(row["staged_version"]),
                str(row["staged_content_digest"]),
            ),
        )
        if receipt.staged_ref != staged_ref:
            raise ValueError("community Blueprint stage receipt does not match local revision")
        return receipt
