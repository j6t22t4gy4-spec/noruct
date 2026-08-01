from __future__ import annotations

from typing import Any, Mapping

from .job_ledger_primitives import (
    ActiveJobCheckpoint,
    ActiveJobCheckpointHistory,
    _digest,
    _payload,
)


class ActiveJobCheckpointMixin:

    def checkpoints(self, job_id: str) -> ActiveJobCheckpointHistory:
        """Reconstruct inspectable state boundaries without enabling execution resume.

        The immutable ledger already records the authoritative snapshot, terminal
        attempts, scheduling mutations, topology patches, and terminal aggregate.
        This method only turns those facts into parent-linked, privacy-bounded
        checkpoints after the regular hash/relation replay has accepted them.
        """

        inspection = self.inspect(job_id)
        if not inspection.replay_matches:
            raise ValueError("Checkpoint history requires a replay-matching ACTIVE JOB audit")
        rows = self.store.get_job_ledger_rows(job_id)
        if rows is None:
            raise KeyError(f"Unknown ACTIVE JOB: {job_id}")
        snapshot = _payload(rows["snapshot"])
        snapshot_tasks = snapshot.get("tasks", ())
        if not isinstance(snapshot_tasks, (list, tuple)):
            raise ValueError("Checkpoint history requires a valid task snapshot")

        task_states: dict[str, dict[str, Any]] = {}
        for task in snapshot_tasks:
            if not isinstance(task, Mapping):
                continue
            task_id = str(task.get("task_id", ""))
            if task_id:
                task_states[task_id] = {
                    "task_id": task_id,
                    "status": "PENDING",
                    "assignee_id": None,
                    "attempt": 1,
                }

        graph_blueprint = snapshot.get("graph_blueprint", {})
        if not isinstance(graph_blueprint, Mapping):
            graph_blueprint = {}
        graph_version = int(snapshot.get("graph_version", 0) or 0)
        graph_digest = str(graph_blueprint.get("initial_graph_digest", ""))
        if not graph_digest:
            graph_digest = inspection.initial_graph_digest

        checkpoints: list[ActiveJobCheckpoint] = []
        parent_checkpoint_id: str | None = None

        def append_checkpoint(
            *,
            ledger_sequence: int,
            event_type: str,
            chain_hash: str,
            changed_task_ids: tuple[str, ...] = (),
        ) -> None:
            nonlocal parent_checkpoint_id
            checkpoint_id = "job-checkpoint-" + _digest(
                {
                    "job_id": job_id,
                    "ledger_sequence": ledger_sequence,
                    "chain_hash": chain_hash,
                }
            )[:24]
            checkpoints.append(
                ActiveJobCheckpoint(
                    checkpoint_id=checkpoint_id,
                    parent_checkpoint_id=parent_checkpoint_id,
                    ledger_sequence=ledger_sequence,
                    event_type=event_type,
                    chain_hash=chain_hash,
                    graph_version=graph_version,
                    graph_digest=graph_digest,
                    changed_task_ids=tuple(sorted(set(changed_task_ids))),
                    task_states=tuple(
                        dict(task_states[task_id]) for task_id in sorted(task_states)
                    ),
                )
            )
            parent_checkpoint_id = checkpoint_id

        append_checkpoint(
            ledger_sequence=0,
            event_type="ADMITTED",
            chain_hash=str(rows["snapshot"]["chain_hash"]),
        )

        ordered: list[tuple[int, str, Mapping[str, Any]]] = []
        ordered.extend(
            (int(row["ledger_seq"]), "ATTEMPT", row) for row in rows["attempts"]
        )
        ordered.extend(
            (int(row["ledger_seq"]), "MUTATION", row) for row in rows["mutations"]
        )
        ordered.extend(
            (int(row["ledger_seq"]), "GRAPH_PATCH", row)
            for row in rows["graph_patches"]
        )
        if rows["terminal"] is not None:
            ordered.append((int(rows["terminal"]["ledger_seq"]), "TERMINAL", rows["terminal"]))
        ordered.sort(key=lambda item: item[0])

        for ledger_sequence, event_type, row in ordered:
            payload = _payload(row)
            changed: tuple[str, ...] = ()
            if event_type == "ATTEMPT":
                task_id = str(payload.get("task_id", ""))
                if task_id:
                    status = str(payload.get("status", ""))
                    task_states[task_id] = {
                        "task_id": task_id,
                        "status": (
                            "SUCCEEDED"
                            if status == "SUCCEEDED"
                            else "CANCELLED"
                            if status == "CANCELLED"
                            else "FAILED"
                        ),
                        "assignee_id": str(payload.get("employee_id", "")) or None,
                        "attempt": int(payload.get("sequence", 0) or 0),
                    }
                    changed = (task_id,)
            elif event_type == "MUTATION":
                task_id = str(payload.get("task_id", ""))
                if task_id:
                    task_states[task_id] = {
                        "task_id": task_id,
                        "status": "PENDING",
                        "assignee_id": None,
                        "attempt": int(payload.get("target_attempt_sequence", 0) or 0),
                    }
                    changed = (task_id,)
            elif event_type == "GRAPH_PATCH":
                graph_version = int(payload.get("target_graph_version", graph_version) or graph_version)
                graph_digest = str(payload.get("after_graph_digest", graph_digest)) or graph_digest
                patch = payload.get("patch", {})
                operations = patch.get("operations", ()) if isinstance(patch, Mapping) else ()
                changed_ids: list[str] = []
                for operation in operations if isinstance(operations, (list, tuple)) else ():
                    if not isinstance(operation, Mapping):
                        continue
                    kind = str(operation.get("kind", ""))
                    if kind == "ADD_TASK":
                        task = operation.get("task", {})
                        task_id = str(task.get("task_id", "")) if isinstance(task, Mapping) else ""
                        if task_id:
                            task_states[task_id] = {
                                "task_id": task_id,
                                "status": "PENDING",
                                "assignee_id": None,
                                "attempt": 1,
                            }
                            changed_ids.append(task_id)
                    elif kind == "CANCEL_TASK":
                        task_id = str(operation.get("task_id", ""))
                        if task_id in task_states:
                            prior = task_states[task_id]
                            task_states[task_id] = {
                                "task_id": task_id,
                                "status": "CANCELLED",
                                "assignee_id": None,
                                "attempt": int(prior.get("attempt", 1) or 1),
                            }
                            changed_ids.append(task_id)
                changed = tuple(changed_ids)
            else:
                terminal_tasks = payload.get("tasks", ())
                if isinstance(terminal_tasks, (list, tuple)):
                    changed_ids = []
                    for task in terminal_tasks:
                        if not isinstance(task, Mapping):
                            continue
                        task_id = str(task.get("task_id", ""))
                        if task_id:
                            task_states[task_id] = {
                                "task_id": task_id,
                                "status": str(task.get("status", "")),
                                "assignee_id": (
                                    str(task.get("assignee_id", "")) or None
                                ),
                                "attempt": int(task.get("attempt", 0) or 0),
                            }
                            changed_ids.append(task_id)
                    changed = tuple(changed_ids)
            append_checkpoint(
                ledger_sequence=ledger_sequence,
                event_type=event_type,
                chain_hash=str(row["chain_hash"]),
                changed_task_ids=changed,
            )

        return ActiveJobCheckpointHistory(
            job_id=inspection.job_id,
            audit_status=inspection.audit_status,
            checkpoint_count=len(checkpoints),
            checkpoints=tuple(checkpoints),
        )

