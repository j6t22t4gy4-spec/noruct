"""Content-free delivery facts derived from an already replayed ACTIVE JOB."""

from __future__ import annotations

from typing import Any, Mapping

from dynamic_firm.runtime.models import EventType

from .store import RunStore


def safe_final_task_capabilities(
    snapshot: Mapping[str, Any],
    *,
    final_task_id: str,
    errors: list[str],
) -> tuple[str, ...]:
    """Project only frozen final capability names, never task prose."""

    candidates = [
        item
        for item in snapshot.get("tasks", ())
        if isinstance(item, Mapping) and str(item.get("task_id", "")) == final_task_id
    ]
    if len(candidates) != 1:
        return ()
    capabilities = candidates[0].get("required_capabilities", ())
    if not isinstance(capabilities, (list, tuple)) or len(capabilities) > 32:
        errors.append("final task capabilities malformed")
        return ()
    projected = tuple(str(value) for value in capabilities)
    if (
        not all(value and len(value.encode("utf-8")) <= 64 for value in projected)
        or len(set(projected)) != len(projected)
    ):
        errors.append("final task capabilities invalid")
        return ()
    return tuple(sorted(projected))


def validation_receipts(store: RunStore, job_id: str) -> tuple[Mapping[str, str], ...]:
    """Read only name/pass state from existing coding validation events."""

    receipts: list[Mapping[str, str]] = []
    for run in store.list_job_runs(job_id):
        run_id = str(run["run_id"])
        for event in store.list_events(run_id, 0):
            if event.type is not EventType.VALIDATION_RECORDED:
                continue
            name = event.payload.get("name")
            passed = event.payload.get("passed")
            if (
                not isinstance(name, str)
                or not name
                or len(name.encode("utf-8")) > 128
                or type(passed) is not bool
            ):
                continue
            receipts.append(
                {
                    "task_id": str(run["task_id"]),
                    "employee_id": str(run["employee_id"]),
                    "name": name,
                    "status": "PASSED" if passed else "FAILED",
                }
            )
            if len(receipts) == 16:
                return tuple(receipts)
    return tuple(receipts)
