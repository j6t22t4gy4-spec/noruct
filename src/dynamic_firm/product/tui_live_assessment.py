"""Content-free current-decision projection for the persistent live terminal."""

from __future__ import annotations

from collections.abc import Iterable


def live_assessment_entries(
    *,
    stage: str,
    status: str,
    tasks: Iterable[object],
) -> list[tuple[str, str]]:
    """Describe the current Product event projection without model prose.

    The caller owns mutable task state. This helper only derives the bounded
    operator-facing ``NOW/FOCUS/WHY/NEXT/GUARD`` rows from it.
    """

    active = next(
        (
            task
            for task in tasks
            if str(getattr(task, "status", ""))
            in {"working", "tool", "verifying", "retry", "rerouted"}
        ),
        None,
    )
    focus = (
        f"{getattr(active, 'employee', 'employee')} · {getattr(active, 'label', 'task')}"
        if active is not None
        else "company-level decision"
    )
    guidance = {
        "IDLE": (
            "Hold the persistent company ready for a new goal",
            "Wait for an explicit goal before compiling work",
        ),
        "ROUTING": (
            "Classify whether the request needs company execution",
            "Choose direct response or the smallest sufficient team",
        ),
        "PLANNING": (
            "Minimize roles and derive only runnable dependencies",
            "Freeze the first executable plan",
        ),
        "COMPILING": (
            "Analyze dependencies before assigning parallel work",
            "Emit a bounded plan or safe solo fallback",
        ),
        "READY": (
            "Keep only ready work eligible for execution",
            "Start the next independent task",
        ),
        "EXECUTING": (
            "Advance the active task without widening authority",
            "Re-evaluate dependencies when its result arrives",
        ),
        "VERIFYING": (
            "Check the produced result before integration",
            "Accept, retry, or reroute the affected task",
        ),
        "RECOVERING": (
            "Contain failure to the smallest affected task",
            "Retry or reroute within the bounded workflow budget",
        ),
        "REVIEW": (
            "Pause a protected action at the authority boundary",
            "Wait for the operator's explicit decision",
        ),
        "BLOCKED": (
            "Keep the protected action blocked",
            "Report the unresolved condition in the final result",
        ),
        "ANSWERING": (
            "Prepare the direct answer from the selected route",
            "Write the response to the single conversation lane",
        ),
        "RESPONDING": (
            "Commit the integrated company result to the transcript",
            "Return the composer to an idle company surface",
        ),
        "COMPLETE": (
            "Preserve the completed result and current company state",
            "Wait for the operator's next goal",
        ),
        "FAILED": (
            "Preserve the failure boundary without hidden retries",
            "Return unresolved conditions for operator review",
        ),
        "ESCALATING": (
            "Admit only the evidence-backed specialist need",
            "Recompile the minimum organization",
        ),
    }
    why, next_step = guidance.get(
        stage,
        ("Maintain the current bounded execution state", "Re-evaluate the active Job"),
    )
    entries: list[tuple[str, str]] = [
        (f"NOW     {status}", "accent"),
        (f"FOCUS   {focus}", "normal"),
        (f"WHY     {why}", "muted"),
        (f"NEXT    {next_step}", "normal"),
    ]
    if stage == "REVIEW":
        entries.append(("GUARD   protected action remains unexecuted", "warning"))
    elif stage == "RECOVERING":
        entries.append(("GUARD   workflow mutation budget remains bounded", "warning"))
    elif stage == "BLOCKED":
        entries.append(("GUARD   no irreversible action is being retried", "error"))
    else:
        entries.append(("GUARD   authority and active Job pins stay unchanged", "muted"))
    return entries
