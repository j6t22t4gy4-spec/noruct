"""Read-only Modern terminal snapshot assembly.

The interactive controller owns session and command orchestration.  This
component opens the established Company and runtime projections only to
assemble a presentation snapshot; it owns no state transition, provider call,
approval, Job, Knowledge, or Evolution lifecycle.
"""

from __future__ import annotations

from typing import Any, Callable, Mapping

from dynamic_firm import __version__
from dynamic_firm.application.operator_surface_read_model import read_operator_surface
from dynamic_firm.product.modern_tui import ModernTerminalSnapshot
from dynamic_firm.product.settings_registry import SettingsRegistry


def assemble_modern_terminal_snapshot(
    *,
    config: Any,
    state_path: Any,
    roster_snapshot: Any,
    session_id: str,
    facts: Mapping[str, object],
    provider: str,
    authority: str,
    company_settings_entries: Callable[..., object],
) -> ModernTerminalSnapshot:
    """Build one display-only snapshot from existing Company/runtime facts."""

    read_model = read_operator_surface(state_path, roster_snapshot=roster_snapshot)
    settings_entries = tuple(
        item.as_dict()
        for item in (
            *SettingsRegistry(config.config_path).entries(),
            *company_settings_entries(
                roster_snapshot,
                manager_report=read_model.manager_report,
            ),
        )
    )
    return ModernTerminalSnapshot(
        workspace=str(config.workspace),
        session_id=session_id,
        model=config.model,
        provider=provider,
        authority=authority,
        version=__version__,
        roster_revision=roster_snapshot.revision,
        active_employee_count=roster_snapshot.active_employee_count,
        settings_entries=settings_entries,
        review_mode=read_model.review_mode,
        evolution_mode=read_model.evolution_mode,
        operating_report=read_model.operating_report,
        operator_snapshot=read_model.operator_snapshot,
        **facts,
    )


__all__ = ["assemble_modern_terminal_snapshot"]
