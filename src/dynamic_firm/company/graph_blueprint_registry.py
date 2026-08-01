"""Local immutable Graph Blueprint registry adapters."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Mapping

from .graph_blueprint_models import (
    BlueprintRevisionReceipt,
    BlueprintRevisionStatus,
    GraphBlueprint,
    GraphBlueprintExecutionReplica,
    GraphBlueprintOrigin,
    GraphBlueprintRef,
    GraphBlueprintTask,
    GraphMutationPolicy,
    GraphUserConstraints,
    identifier,
)
from dynamic_firm.kernel.models import (
    ExecutionReplicaAggregation,
    ExecutionReplicaStrategy,
)


class GraphBlueprintRegistry:
    """In-memory reference implementation used by deterministic unit tests."""

    def __init__(self) -> None:
        self._blueprints: dict[tuple[str, int], GraphBlueprint] = {}
        self._pins: dict[str, GraphBlueprintRef] = {}
        self._constraints: dict[str, GraphUserConstraints] = {}
        self._revision_receipts: dict[str, BlueprintRevisionReceipt] = {}

    def save(self, blueprint: GraphBlueprint) -> GraphBlueprint:
        blueprint.verify()
        key = (blueprint.blueprint_id, blueprint.version)
        existing = self._blueprints.get(key)
        if existing is not None and existing.content_digest != blueprint.content_digest:
            raise ValueError("A Blueprint version cannot be overwritten")
        self._blueprints[key] = blueprint
        return blueprint

    def get(self, ref: GraphBlueprintRef) -> GraphBlueprint:
        blueprint = self._blueprints.get((ref.blueprint_id, ref.version))
        if blueprint is None or blueprint.content_digest != ref.content_digest:
            raise ValueError("Graph Blueprint exact revision is not available")
        blueprint.verify()
        return blueprint

    def revision(self, blueprint_id: str, version: int) -> GraphBlueprint:
        identifier(blueprint_id, "blueprint_id")
        if type(version) is not int or version < 1:
            raise ValueError("version must be a positive integer")
        blueprint = self._blueprints.get((blueprint_id, version))
        if blueprint is None:
            raise ValueError("Graph Blueprint revision is not available")
        blueprint.verify()
        return blueprint

    def list(self) -> tuple[GraphBlueprint, ...]:
        return tuple(self._blueprints[key] for key in sorted(self._blueprints))

    def pin(self, slot: str, ref: GraphBlueprintRef) -> None:
        identifier(slot, "pin slot")
        self.get(ref)
        self._pins[slot] = ref

    def pinned(self, slot: str) -> GraphBlueprintRef | None:
        identifier(slot, "pin slot")
        return self._pins.get(slot)

    def clear_pin(self, slot: str) -> None:
        identifier(slot, "pin slot")
        self._pins.pop(slot, None)

    def constraints(self, slot: str) -> GraphUserConstraints:
        identifier(slot, "pin slot")
        return self._constraints.get(slot, GraphUserConstraints())

    def set_constraints(self, slot: str, constraints: GraphUserConstraints) -> None:
        identifier(slot, "pin slot")
        if not isinstance(constraints, GraphUserConstraints):
            raise TypeError("constraints must be GraphUserConstraints")
        self._constraints[slot] = constraints

    def fork(
        self,
        ref: GraphBlueprintRef,
        *,
        blueprint_id: str,
        version: int = 1,
    ) -> GraphBlueprint:
        source = self.get(ref)
        return self.save(
            GraphBlueprint(
                blueprint_id=blueprint_id,
                version=version,
                objective_class=source.objective_class,
                execution_profiles=source.execution_profiles,
                parameters=source.parameters,
                tasks=source.tasks,
                final_task_id=source.final_task_id,
                origin=GraphBlueprintOrigin.USER_FORK,
                parent_ref=source.ref,
            )
        )

    def record_revision_receipt(
        self,
        receipt: BlueprintRevisionReceipt,
    ) -> BlueprintRevisionReceipt:
        receipt.verify()
        existing = self._revision_receipts.get(receipt.content_digest)
        if existing is not None and existing != receipt:
            raise ValueError("Blueprint revision receipt digest collision")
        self._revision_receipts[receipt.content_digest] = receipt
        return receipt

    def revision_receipts(self, blueprint_id: str) -> tuple[BlueprintRevisionReceipt, ...]:
        identifier(blueprint_id, "blueprint_id")
        return tuple(
            receipt
            for receipt in self._revision_receipts.values()
            if receipt.source_ref.blueprint_id == blueprint_id
        )

    def compatible(
        self,
        *,
        objective_class: str,
        execution_profile: str,
        available_capabilities: tuple[str, ...],
        pin_slot: str | None = None,
    ) -> tuple[GraphBlueprint, ...]:
        pinned = self.pinned(pin_slot) if pin_slot is not None else None
        all_blueprints = self.list()
        candidates = [
            item
            for item in all_blueprints
            if item.origin is GraphBlueprintOrigin.VERIFIED_PLAYBOOK
        ]
        if pinned is not None:
            candidates.append(self.get(pinned))
        candidates.extend(
            item
            for item in all_blueprints
            if item.origin
            in {
                GraphBlueprintOrigin.STAGED_COMMUNITY,
                GraphBlueprintOrigin.PINNED_EXTERNAL,
            }
            and item not in candidates
        )
        available = set(available_capabilities)
        return tuple(
            blueprint
            for blueprint in candidates
            if blueprint.objective_class in {objective_class, "general"}
            and execution_profile in blueprint.execution_profiles
            and {
                capability
                for task in blueprint.tasks
                for capability in task.required_capabilities
            }.issubset(available)
        )


class SQLiteGraphBlueprintRegistry(GraphBlueprintRegistry):
    """Durable local store that preserves exact revisions and user pins."""

    def __init__(self, path: Path | str) -> None:
        super().__init__()
        self._connection = sqlite3.connect(str(path))
        self._connection.row_factory = sqlite3.Row
        self._connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS graph_blueprints (
                blueprint_id TEXT NOT NULL,
                version INTEGER NOT NULL,
                content_digest TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                PRIMARY KEY (blueprint_id, version)
            );
            CREATE TABLE IF NOT EXISTS graph_blueprint_pins (
                slot TEXT PRIMARY KEY,
                blueprint_id TEXT NOT NULL,
                version INTEGER NOT NULL,
                content_digest TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS graph_blueprint_constraints (
                slot TEXT PRIMARY KEY,
                constraints_json TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS graph_blueprint_revision_receipts (
                receipt_sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                content_digest TEXT NOT NULL UNIQUE,
                blueprint_id TEXT NOT NULL,
                payload_json TEXT NOT NULL
            );
            """
        )
        self._connection.commit()

    def close(self) -> None:
        self._connection.close()

    def save(self, blueprint: GraphBlueprint) -> GraphBlueprint:
        blueprint.verify()
        payload = json.dumps(
            blueprint.canonical_payload(), ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        with self._connection:
            row = self._connection.execute(
                "SELECT content_digest FROM graph_blueprints WHERE blueprint_id = ? AND version = ?",
                (blueprint.blueprint_id, blueprint.version),
            ).fetchone()
            if row is not None:
                if str(row["content_digest"]) != blueprint.content_digest:
                    raise ValueError("A Blueprint version cannot be overwritten")
                return blueprint
            self._connection.execute(
                """
                INSERT INTO graph_blueprints(blueprint_id, version, content_digest, payload_json)
                VALUES (?, ?, ?, ?)
                """,
                (blueprint.blueprint_id, blueprint.version, blueprint.content_digest, payload),
            )
        return blueprint

    def get(self, ref: GraphBlueprintRef) -> GraphBlueprint:
        row = self._connection.execute(
            """
            SELECT content_digest, payload_json FROM graph_blueprints
            WHERE blueprint_id = ? AND version = ?
            """,
            (ref.blueprint_id, ref.version),
        ).fetchone()
        if row is None or str(row["content_digest"]) != ref.content_digest:
            raise ValueError("Graph Blueprint exact revision is not available")
        return blueprint_from_payload(json.loads(str(row["payload_json"])))

    def revision(self, blueprint_id: str, version: int) -> GraphBlueprint:
        identifier(blueprint_id, "blueprint_id")
        if type(version) is not int or version < 1:
            raise ValueError("version must be a positive integer")
        row = self._connection.execute(
            """
            SELECT payload_json FROM graph_blueprints
            WHERE blueprint_id = ? AND version = ?
            """,
            (blueprint_id, version),
        ).fetchone()
        if row is None:
            raise ValueError("Graph Blueprint revision is not available")
        return blueprint_from_payload(json.loads(str(row["payload_json"])))

    def list(self) -> tuple[GraphBlueprint, ...]:
        rows = self._connection.execute(
            "SELECT payload_json FROM graph_blueprints ORDER BY blueprint_id ASC, version ASC"
        ).fetchall()
        return tuple(blueprint_from_payload(json.loads(str(row["payload_json"]))) for row in rows)

    def pin(self, slot: str, ref: GraphBlueprintRef) -> None:
        identifier(slot, "pin slot")
        self.get(ref)
        with self._connection:
            self._connection.execute(
                """
                INSERT INTO graph_blueprint_pins(slot, blueprint_id, version, content_digest)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(slot) DO UPDATE SET
                    blueprint_id = excluded.blueprint_id,
                    version = excluded.version,
                    content_digest = excluded.content_digest
                """,
                (slot, ref.blueprint_id, ref.version, ref.content_digest),
            )

    def pinned(self, slot: str) -> GraphBlueprintRef | None:
        identifier(slot, "pin slot")
        row = self._connection.execute(
            """
            SELECT blueprint_id, version, content_digest
            FROM graph_blueprint_pins WHERE slot = ?
            """,
            (slot,),
        ).fetchone()
        if row is None:
            return None
        return GraphBlueprintRef(
            blueprint_id=str(row["blueprint_id"]),
            version=int(row["version"]),
            content_digest=str(row["content_digest"]),
        )

    def clear_pin(self, slot: str) -> None:
        identifier(slot, "pin slot")
        with self._connection:
            self._connection.execute(
                "DELETE FROM graph_blueprint_pins WHERE slot = ?",
                (slot,),
            )

    def constraints(self, slot: str) -> GraphUserConstraints:
        identifier(slot, "pin slot")
        row = self._connection.execute(
            "SELECT constraints_json FROM graph_blueprint_constraints WHERE slot = ?",
            (slot,),
        ).fetchone()
        if row is None:
            return GraphUserConstraints()
        return constraints_from_payload(json.loads(str(row["constraints_json"])))

    def set_constraints(self, slot: str, constraints: GraphUserConstraints) -> None:
        identifier(slot, "pin slot")
        if not isinstance(constraints, GraphUserConstraints):
            raise TypeError("constraints must be GraphUserConstraints")
        payload = json.dumps(
            constraints_payload(constraints),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        with self._connection:
            self._connection.execute(
                """
                INSERT INTO graph_blueprint_constraints(slot, constraints_json)
                VALUES (?, ?)
                ON CONFLICT(slot) DO UPDATE SET constraints_json = excluded.constraints_json
                """,
                (slot, payload),
            )

    def record_revision_receipt(
        self,
        receipt: BlueprintRevisionReceipt,
    ) -> BlueprintRevisionReceipt:
        receipt.verify()
        payload = json.dumps(
            receipt.canonical_payload(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        with self._connection:
            row = self._connection.execute(
                "SELECT payload_json FROM graph_blueprint_revision_receipts WHERE content_digest = ?",
                (receipt.content_digest,),
            ).fetchone()
            if row is not None:
                if str(row["payload_json"]) != payload:
                    raise ValueError("Blueprint revision receipt digest collision")
                return receipt
            self._connection.execute(
                """
                INSERT INTO graph_blueprint_revision_receipts(
                    content_digest, blueprint_id, payload_json
                ) VALUES (?, ?, ?)
                """,
                (receipt.content_digest, receipt.source_ref.blueprint_id, payload),
            )
        return receipt

    def revision_receipts(self, blueprint_id: str) -> tuple[BlueprintRevisionReceipt, ...]:
        identifier(blueprint_id, "blueprint_id")
        rows = self._connection.execute(
            """
            SELECT payload_json FROM graph_blueprint_revision_receipts
            WHERE blueprint_id = ? ORDER BY receipt_sequence ASC
            """,
            (blueprint_id,),
        ).fetchall()
        return tuple(
            revision_receipt_from_payload(json.loads(str(row["payload_json"])))
            for row in rows
        )


def blueprint_from_payload(payload: object) -> GraphBlueprint:
    if not isinstance(payload, Mapping):
        raise ValueError("Graph Blueprint payload must be an object")
    raw_tasks = payload.get("tasks")
    if not isinstance(raw_tasks, list):
        raise ValueError("Graph Blueprint payload tasks must be a list")
    raw_parent = payload.get("parent_ref")
    parent = None
    if raw_parent is not None:
        if not isinstance(raw_parent, Mapping):
            raise ValueError("Graph Blueprint parent_ref must be an object")
        parent = GraphBlueprintRef(
            blueprint_id=str(raw_parent.get("blueprint_id", "")),
            version=int(raw_parent.get("version", 0)),
            content_digest=str(raw_parent.get("content_digest", "")),
        )
    return GraphBlueprint(
        blueprint_id=str(payload.get("blueprint_id", "")),
        version=int(payload.get("version", 0)),
        objective_class=str(payload.get("objective_class", "")),
        execution_profiles=tuple(str(item) for item in payload.get("execution_profiles", ())),
        parameters=tuple(str(item) for item in payload.get("parameters", ())),
        tasks=tuple(
            GraphBlueprintTask(
                task_id=str(raw.get("task_id", "")),
                objective_template=str(raw.get("objective_template", "")),
                depends_on=tuple(str(item) for item in raw.get("depends_on", ())),
                required_capabilities=tuple(str(item) for item in raw.get("required_capabilities", ())),
                acceptance_templates=tuple(str(item) for item in raw.get("acceptance_templates", ())),
                risk_level=str(raw.get("risk_level", "LOW")),
                execution_replica=_replica_from_payload(raw.get("execution_replica")),
            )
            for raw in raw_tasks
            if isinstance(raw, Mapping)
        ),
        final_task_id=str(payload.get("final_task_id", "")),
        origin=GraphBlueprintOrigin(str(payload.get("origin", ""))),
        parent_ref=parent,
    )


def _replica_from_payload(payload: object) -> GraphBlueprintExecutionReplica | None:
    if payload is None:
        return None
    if not isinstance(payload, Mapping):
        raise ValueError("Graph Blueprint execution_replica must be an object")
    return GraphBlueprintExecutionReplica(
        group_id=str(payload.get("group_id", "")),
        replica_id=str(payload.get("replica_id", "")),
        strategy=ExecutionReplicaStrategy(str(payload.get("strategy", ""))),
        scope_template=str(payload.get("scope_template", "")),
        aggregation_task_id=str(payload.get("aggregation_task_id", "")),
        aggregation=ExecutionReplicaAggregation(
            str(payload.get("aggregation", ""))
        ),
        marginal_value_reason_template=str(
            payload.get("marginal_value_reason_template", "")
        ),
    )


def revision_receipt_from_payload(payload: object) -> BlueprintRevisionReceipt:
    if not isinstance(payload, Mapping):
        raise ValueError("Blueprint revision receipt payload must be an object")
    raw_source = payload.get("source_ref")
    raw_candidate = payload.get("candidate_ref")
    if not isinstance(raw_source, Mapping) or not isinstance(raw_candidate, Mapping):
        raise ValueError("Blueprint revision receipt references are invalid")
    return BlueprintRevisionReceipt(
        source_ref=GraphBlueprintRef(
            blueprint_id=str(raw_source.get("blueprint_id", "")),
            version=int(raw_source.get("version", 0)),
            content_digest=str(raw_source.get("content_digest", "")),
        ),
        candidate_ref=GraphBlueprintRef(
            blueprint_id=str(raw_candidate.get("blueprint_id", "")),
            version=int(raw_candidate.get("version", 0)),
            content_digest=str(raw_candidate.get("content_digest", "")),
        ),
        status=BlueprintRevisionStatus(str(payload.get("status", ""))),
        reason=str(payload.get("reason", "")),
        rationale=str(payload.get("rationale", "")),
    )


def constraints_payload(constraints: GraphUserConstraints) -> dict[str, object]:
    return {
        "pinned_employee_ids": list(constraints.pinned_employee_ids),
        "excluded_employee_ids": list(constraints.excluded_employee_ids),
        "require_independent_review": constraints.require_independent_review,
        "max_concurrency": constraints.max_concurrency,
        "max_cost_usd": constraints.max_cost_usd,
        "max_wall_time_ms": constraints.max_wall_time_ms,
        "mutation_policy": constraints.mutation_policy.value,
    }


def constraints_from_payload(payload: object) -> GraphUserConstraints:
    if not isinstance(payload, Mapping):
        raise ValueError("Graph Blueprint constraints payload must be an object")
    return GraphUserConstraints(
        pinned_employee_ids=tuple(
            str(item) for item in payload.get("pinned_employee_ids", ())
        ),
        excluded_employee_ids=tuple(
            str(item) for item in payload.get("excluded_employee_ids", ())
        ),
        require_independent_review=bool(
            payload.get("require_independent_review", False)
        ),
        max_concurrency=payload.get("max_concurrency"),
        max_cost_usd=payload.get("max_cost_usd"),
        max_wall_time_ms=payload.get("max_wall_time_ms"),
        mutation_policy=GraphMutationPolicy(
            str(payload.get("mutation_policy", "BOUNDED_AUTO"))
        ),
    )
