from __future__ import annotations

import dataclasses
import unittest
from datetime import UTC, datetime, timedelta, timezone

from dynamic_firm.company.frontdoor import (
    AuthoritySnapshotIdentity,
    WORK_ORDER_SCHEMA,
    WorkOrderBudgetSnapshot,
    normalize_work_order,
    verify_work_order_binding,
)
from dynamic_firm.company.operating import (
    CompanyOperatingDecision,
    CompanyWorkMode,
    InitialCoordinationPolicy,
    OperatingReason,
    RequestedEffect,
)
from dynamic_firm.kernel.models import ExecutionReplicaPreference


def authority(**changes) -> AuthoritySnapshotIdentity:  # type: ignore[no-untyped-def]
    values = {
        "company_id": "company-local",
        "company_revision": 7,
        "roster_revision": 11,
        "playbook_revision": 4,
        "action_policy_digest": "policy:read-only:revision-3",
    }
    values.update(changes)
    return AuthoritySnapshotIdentity(**values)


def budget(**changes) -> WorkOrderBudgetSnapshot:  # type: ignore[no-untyped-def]
    values = {
        "max_model_calls": 8,
        "max_tool_calls": 16,
        "max_cost_usd": 2.5,
        "max_wall_time_ms": 30_000,
    }
    values.update(changes)
    return WorkOrderBudgetSnapshot(**values)


class WorkOrderFrontDoorTests(unittest.TestCase):
    def test_normalizes_raw_input_before_any_workflow_exists(self) -> None:
        requested_at = datetime(2026, 7, 25, 12, 30, tzinfo=timezone(timedelta(hours=9)))

        order = normalize_work_order(
            "\r\n  이 저장소의 구조를 분석해줘.  \r\n",
            work_order_id="work-order-1",
            requested_outcome="  구조적 병목을 설명하는 보고서  ",
            constraints=("  파일은 수정하지 않는다. ", "파일은 수정하지 않는다."),
            acceptance_criteria=("근거를 제시한다.",),
            context_refs=(" knowledge:brief:17 ", "knowledge:brief:17"),
            workspace_ref=" workspace:current ",
            authority_snapshot=authority(),
            budget_snapshot=budget(),
            requested_at=requested_at,
        )

        self.assertEqual(order.objective, "이 저장소의 구조를 분석해줘.")
        self.assertEqual(order.requested_outcome, "구조적 병목을 설명하는 보고서")
        self.assertEqual(order.constraints, ("파일은 수정하지 않는다.",))
        self.assertEqual(order.context_refs, ("knowledge:brief:17",))
        self.assertEqual(order.workspace_ref, "workspace:current")
        self.assertEqual(order.requested_at.tzinfo, UTC)
        self.assertEqual(order.operating_decision.work_mode, CompanyWorkMode.SOLO_JOB)
        self.assertEqual(
            order.operating_decision.coordination_policy,
            InitialCoordinationPolicy.SOLO_FIRST,
        )
        self.assertEqual(order.operating_decision.requested_effect, RequestedEffect.READ)
        self.assertEqual(order.canonical_payload()["schema"], WORK_ORDER_SCHEMA)
        self.assertEqual(order.canonical_payload()["authority_snapshot_identity"], authority().identity_digest)
        self.assertEqual(
            order.canonical_payload()["operating_decision"][
                "execution_replica_preference"
            ],
            ExecutionReplicaPreference.PERFORMANCE_FIRST.value,
        )
        self.assertIsNone(
            order.canonical_payload()["operating_decision"][
                "suggested_execution_replica_strategy"
            ]
        )
        order.verify()

    def test_digest_is_reproducible_for_the_same_normalized_order(self) -> None:
        requested_at = datetime(2026, 7, 25, 3, 30, tzinfo=UTC)
        first = normalize_work_order(
            "hello",
            work_order_id="work-order-repeatable",
            constraints=(" concise ", "concise"),
            authority_snapshot=authority(),
            budget_snapshot=budget(),
            requested_at=requested_at,
        )
        second = normalize_work_order(
            " hello ",
            work_order_id="work-order-repeatable",
            constraints=("concise",),
            authority_snapshot=authority(),
            budget_snapshot=budget(),
            requested_at=datetime(
                2026,
                7,
                25,
                12,
                30,
                tzinfo=timezone(timedelta(hours=9)),
            ),
        )

        self.assertEqual(first, second)
        self.assertEqual(first.content_digest, second.content_digest)
        self.assertEqual(first.operating_decision.work_mode, CompanyWorkMode.DIRECT)
        self.assertEqual(first.requested_outcome, "hello")

    def test_digest_changes_when_authority_budget_or_requested_outcome_changes(self) -> None:
        values = {
            "raw_company_input": "Analyze this project.",
            "work_order_id": "work-order-sensitive",
            "authority_snapshot": authority(),
            "budget_snapshot": budget(),
            "requested_at": datetime(2026, 7, 25, 3, 30, tzinfo=UTC),
        }
        baseline = normalize_work_order(**values)
        changed_authority = normalize_work_order(
            **{**values, "authority_snapshot": authority(company_revision=8)}
        )
        changed_budget = normalize_work_order(
            **{**values, "budget_snapshot": budget(max_model_calls=9)}
        )
        changed_outcome = normalize_work_order(
            **{**values, "requested_outcome": "Return a concise report."}
        )

        self.assertEqual(
            len(
                {
                    baseline.content_digest,
                    changed_authority.content_digest,
                    changed_budget.content_digest,
                    changed_outcome.content_digest,
                }
            ),
            4,
        )

    def test_explicit_operating_decision_override_is_frozen_but_grants_no_authority(self) -> None:
        forced = CompanyOperatingDecision(
            work_mode=CompanyWorkMode.SOLO_JOB,
            coordination_policy=InitialCoordinationPolicy.SOLO_FIRST,
            requested_effect=RequestedEffect.READ,
            reason=OperatingReason.ACTION_ORIENTED_GOAL,
        )

        order = normalize_work_order(
            "hello",
            work_order_id="work-order-explicit-mode",
            authority_snapshot=authority(),
            budget_snapshot=budget(),
            requested_at=datetime(2026, 7, 25, tzinfo=UTC),
            operating_decision=forced,
        )

        self.assertIs(order.operating_decision, forced)
        self.assertTrue(order.operating_decision.company_owned)
        self.assertNotIn("tool_grants", order.canonical_payload())
        self.assertNotIn("permissions", order.canonical_payload())

    def test_work_order_is_frozen_and_detects_forced_in_memory_tampering(self) -> None:
        order = normalize_work_order(
            "hello",
            work_order_id="work-order-immutable",
            authority_snapshot=authority(),
            budget_snapshot=budget(),
            requested_at=datetime(2026, 7, 25, tzinfo=UTC),
        )

        with self.assertRaises(dataclasses.FrozenInstanceError):
            order.objective = "changed"  # type: ignore[misc]

        object.__setattr__(order, "objective", "forced mutation")
        with self.assertRaisesRegex(ValueError, "digest is invalid"):
            order.verify()

    def test_binding_rejects_different_authority_or_budget_snapshot(self) -> None:
        frozen_authority = authority()
        frozen_budget = budget()
        order = normalize_work_order(
            "hello",
            work_order_id="work-order-binding",
            authority_snapshot=frozen_authority,
            budget_snapshot=frozen_budget,
            requested_at=datetime(2026, 7, 25, tzinfo=UTC),
        )

        verify_work_order_binding(
            order,
            authority_snapshot=frozen_authority,
            budget_snapshot=frozen_budget,
        )
        with self.assertRaisesRegex(ValueError, "authority snapshot"):
            verify_work_order_binding(
                order,
                authority_snapshot=authority(company_revision=8),
                budget_snapshot=frozen_budget,
            )
        with self.assertRaisesRegex(ValueError, "budget snapshot"):
            verify_work_order_binding(
                order,
                authority_snapshot=frozen_authority,
                budget_snapshot=budget(max_model_calls=9),
            )

    def test_rejects_invalid_identity_budget_time_and_raw_input(self) -> None:
        with self.assertRaisesRegex(ValueError, "raw_company_input must be non-empty"):
            normalize_work_order(
                "  ",
                work_order_id="work-order-invalid",
                authority_snapshot=authority(),
                budget_snapshot=budget(),
                requested_at=datetime(2026, 7, 25, tzinfo=UTC),
            )
        with self.assertRaisesRegex(ValueError, "timezone-aware"):
            normalize_work_order(
                "hello",
                work_order_id="work-order-invalid",
                authority_snapshot=authority(),
                budget_snapshot=budget(),
                requested_at=datetime(2026, 7, 25),
            )
        with self.assertRaisesRegex(ValueError, "non-negative integer"):
            authority(company_revision=-1)
        with self.assertRaisesRegex(ValueError, "finite non-negative"):
            budget(max_cost_usd=float("nan"))
        with self.assertRaisesRegex(TypeError, "must be a tuple"):
            normalize_work_order(
                "hello",
                work_order_id="work-order-invalid",
                authority_snapshot=authority(),
                budget_snapshot=budget(),
                requested_at=datetime(2026, 7, 25, tzinfo=UTC),
                context_refs=["knowledge:brief:1"],  # type: ignore[arg-type]
            )


if __name__ == "__main__":
    unittest.main()
