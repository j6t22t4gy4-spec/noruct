from __future__ import annotations

import unittest
from dataclasses import FrozenInstanceError

from dynamic_firm.company import (
    RosterSnapshotError,
    RosterVersion,
    decode_active_roster,
)


def employee(
    employee_id: str,
    *,
    active: bool = True,
    temporary: bool = False,
    capabilities: object = ("analysis",),
    model_profile: object = "roster-default",
) -> dict[str, object]:
    return {
        "employee_id": employee_id,
        "role": "Persistent Analyst",
        "capabilities": capabilities,
        "active": active,
        "temporary": temporary,
        "model_profile": model_profile,
    }


def roster(*employees: dict[str, object]) -> RosterVersion:
    return RosterVersion(
        revision=7,
        parent_revision=6,
        employees=employees,
        created_at="2026-07-15T00:00:00+00:00",
    )


class ActiveRosterAuthorityTests(unittest.TestCase):
    def test_decoder_filters_inactive_staff_and_freezes_one_revision(self) -> None:
        snapshot = decode_active_roster(
            roster(
                employee("employee-active", capabilities=("analysis", "evidence")),
                employee("employee-inactive", active=False, capabilities=("design",)),
            )
        )

        self.assertEqual(snapshot.revision, 7)
        self.assertEqual(snapshot.total_employee_count, 2)
        self.assertEqual(snapshot.active_employee_count, 1)
        self.assertEqual(snapshot.employees[0].employee_id, "employee-active")
        self.assertEqual(snapshot.available_capabilities, ("analysis", "evidence"))
        with self.assertRaises(FrozenInstanceError):
            snapshot.revision = 8  # type: ignore[misc]

    def test_legacy_default_execution_class_does_not_mutate_persisted_identity(self) -> None:
        snapshot = decode_active_roster(roster(employee("employee-persistent")))

        first = snapshot.resolve_execution_profiles("model-a")
        second = snapshot.resolve_execution_profiles("model-b")

        self.assertEqual(first[0].employee_id, second[0].employee_id)
        self.assertEqual(first[0].role, second[0].role)
        self.assertEqual(first[0].model_profile, "model-a")
        self.assertEqual(second[0].model_profile, "model-b")
        self.assertEqual(snapshot.employees[0].model_profile, "roster-default")

    def test_specialist_profile_is_not_overwritten_by_legacy_default(self) -> None:
        snapshot = decode_active_roster(
            roster(employee("default"), employee("specialist", model_profile="specialist-profile"))
        )

        resolved = snapshot.resolve_execution_profiles("model-a")

        self.assertEqual([item.model_profile for item in resolved], ["model-a", "specialist-profile"])

    def test_decoder_rejects_malformed_or_unsafe_persistent_staff(self) -> None:
        cases = (
            (roster(employee("temp", temporary=True)), "temporary employees"),
            (roster(employee("inactive", active=False)), "at least one active"),
            (
                roster(employee("duplicate"), employee("duplicate")),
                "must be unique",
            ),
            (
                roster(employee("bad-capabilities", capabilities="analysis")),
                "capabilities must be an array",
            ),
            (
                roster(employee("bad-model", model_profile="")),
                "model_profile must be a non-empty string",
            ),
            (
                roster({**employee("unknown"), "surprise": True}),
                "invalid schema",
            ),
        )
        for value, message in cases:
            with self.subTest(message=message):
                with self.assertRaisesRegex(RosterSnapshotError, message):
                    decode_active_roster(value)


if __name__ == "__main__":
    unittest.main()
