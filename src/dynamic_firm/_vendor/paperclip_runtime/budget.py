"""Narrow, dependency-free budget-window helpers adapted from Paperclip.

This private helper deliberately retains no upstream persistence, actor, approval,
or product types.  Noruct owns the policy, incident, pause, and execution
authority that calls these pure calculations.
"""

from __future__ import annotations

from datetime import UTC, datetime


def resolve_budget_window(window_kind: str, now: datetime | None = None) -> tuple[datetime, datetime]:
    """Return the inclusive-start, exclusive-end UTC window for one budget."""

    current = now or datetime.now(UTC)
    if current.tzinfo is None:
        raise ValueError("Budget window time must be timezone-aware")
    current = current.astimezone(UTC)
    if window_kind == "lifetime":
        return (
            datetime(1970, 1, 1, tzinfo=UTC),
            datetime(9999, 1, 1, tzinfo=UTC),
        )
    if window_kind != "calendar_month_utc":
        raise ValueError(f"Unsupported company budget window: {window_kind}")
    start = datetime(current.year, current.month, 1, tzinfo=UTC)
    if current.month == 12:
        end = datetime(current.year + 1, 1, 1, tzinfo=UTC)
    else:
        end = datetime(current.year, current.month + 1, 1, tzinfo=UTC)
    return start, end


def budget_status_from_observed(
    observed_amount: float,
    amount: float,
    warn_percent: int,
) -> str:
    """Classify a scalar observed amount without mutating any state."""

    if amount <= 0:
        return "disabled"
    if observed_amount >= amount:
        return "hard_stop"
    if observed_amount >= amount * warn_percent / 100:
        return "warning"
    return "ok"
