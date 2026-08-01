from __future__ import annotations

import argparse
import io
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from dynamic_firm.product.company_commands import (
    parse_operator_timestamp,
    propose_roster_patch,
    render_company_observability,
    render_roster_patch_preview,
    run_company_curate_daemon,
)


class _RosterPatchService:
    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []

    def propose_add_employee(self, employee, **kwargs):  # type: ignore[no-untyped-def]
        self.calls.append(("add", (employee, kwargs)))
        return {"result": "added"}

    def propose_set_active(self, employee_id, active, **kwargs):  # type: ignore[no-untyped-def]
        self.calls.append(("active", (employee_id, active, kwargs)))
        return {"result": "active"}

    def propose_update_employee(self, employee, **kwargs):  # type: ignore[no-untyped-def]
        self.calls.append(("update", (employee, kwargs)))
        return {"result": "updated"}

    def propose_set_capabilities(self, employee_id, capabilities, **kwargs):  # type: ignore[no-untyped-def]
        self.calls.append(("capabilities", (employee_id, capabilities, kwargs)))
        return {"result": "capabilities"}


class CompanyCommandAdapterTests(unittest.TestCase):
    def test_timestamp_requires_an_explicit_offset_and_normalizes_to_utc(self) -> None:
        self.assertEqual(
            parse_operator_timestamp("2026-07-29T09:00:00+09:00").isoformat(),
            "2026-07-29T00:00:00+00:00",
        )
        with self.assertRaisesRegex(ValueError, "UTC offset"):
            parse_operator_timestamp("2026-07-29T09:00:00")

    def test_roster_proposal_normalizes_an_add_without_owning_lifecycle(self) -> None:
        service = _RosterPatchService()
        result = propose_roster_patch(
            argparse.Namespace(
                operation="ADD_EMPLOYEE",
                employee_id="researcher",
                role="Researcher",
                capability=("research", "summarize"),
                active=None,
                model_profile="bounded-model",
                rationale="A durable research capability is needed.",
            ),
            service,  # type: ignore[arg-type]
        )
        self.assertEqual(result, {"result": "added"})
        kind, payload = service.calls[0]
        employee, kwargs = payload  # type: ignore[misc]
        self.assertEqual(kind, "add")
        self.assertEqual(employee.employee_id, "researcher")
        self.assertEqual(employee.capabilities, ("research", "summarize"))
        self.assertEqual(kwargs["actor"], "user:cli")

    def test_preview_renderer_is_read_only_and_uses_stable_summary(self) -> None:
        output = io.StringIO()
        render_roster_patch_preview(
            {
                "patch": {
                    "patch_id": "roster-1",
                    "status": "PROPOSED",
                    "operation": "ADD_EMPLOYEE",
                    "base_roster_revision": 2,
                    "before_employee": None,
                    "after_employee": {
                        "employee_id": "researcher",
                        "role": "Researcher",
                        "active": True,
                        "capabilities": ("research",),
                    },
                    "rationale": "bounded need",
                    "proposed_by": "user:cli",
                    "content_hash": "a" * 64,
                },
                "active_roster_revision": 2,
                "events": (),
            },
            output,
        )
        rendered = output.getvalue()
        self.assertIn("ROSTER r2 → proposed r3", rendered)
        self.assertIn("researcher · Researcher · active=true", rendered)
        self.assertTrue(rendered.rstrip().endswith("Active ROSTER changed: no"))

    def test_organization_metrics_renderer_keeps_approval_friction_observational(self) -> None:
        output = io.StringIO()
        handled = render_company_observability(
            "organization-metrics",
            {
                "episode_count": 3,
                "observed_time_to_first_runnable_count": 2,
                "median_time_to_first_runnable_ms": 12,
                "graph_proposal_decisions": {
                    "APPROVED": 1,
                    "REJECTED": 2,
                    "UNAVAILABLE": 0,
                },
            },
            output,
        )
        self.assertTrue(handled)
        self.assertIn("approved=1 · rejected=2 · unavailable=0", output.getvalue())
        self.assertIn("automatic graph, budget, and Patch changes: disabled", output.getvalue())

    def test_unknown_company_command_is_not_rendered_by_observability_adapter(self) -> None:
        self.assertFalse(render_company_observability("curate", {}, io.StringIO()))

    def test_foreground_curation_is_bounded_and_never_starts_a_company_job(self) -> None:
        with TemporaryDirectory() as temporary:
            output = io.StringIO()
            result = run_company_curate_daemon(
                argparse.Namespace(confirm=True, poll_seconds=30, max_cycles=1, json=True),
                state_path=Path(temporary) / "company.db",
                output=output,
            )
        record = json.loads(output.getvalue())
        self.assertEqual(result, 0)
        self.assertEqual(len(record["cycles"]), 1)
        self.assertEqual(record["provider_calls"], 0)
        self.assertEqual(record["company_jobs_created"], 0)
        self.assertFalse(record["background_service"])
