from __future__ import annotations

import unittest
from types import SimpleNamespace

from dynamic_firm.runtime.interruption import RecoveryDisposition
from dynamic_firm.runtime.job_inspector import ActiveJobInspector
from dynamic_firm.runtime.job_ledger import ActiveJobAuditStatus


class _EffectCaseStore:
    def __init__(self, cases, remote_claims=()):  # type: ignore[no-untyped-def]
        self._cases = tuple(cases)
        self._remote_claims = tuple(remote_claims)

    def list_job_effect_recovery_cases(self, job_id: str):  # type: ignore[no-untyped-def]
        if job_id != "terminal-job":
            raise AssertionError("unexpected fixture job")
        return self._cases

    def list_job_remote_effect_resource_claims(self, job_id: str):  # type: ignore[no-untyped-def]
        if job_id != "terminal-job":
            raise AssertionError("unexpected fixture job")
        return self._remote_claims


class _TerminalInspector(ActiveJobInspector):
    def __init__(self, cases=(), remote_claims=()):  # type: ignore[no-untyped-def]
        self.store = _EffectCaseStore(cases, remote_claims)  # type: ignore[assignment]
        self.company_coordination = None

    def inspect(self, job_id: str):  # type: ignore[no-untyped-def]
        if job_id != "terminal-job":
            raise AssertionError("unexpected fixture job")
        return SimpleNamespace(
            job_id=job_id,
            audit_status=ActiveJobAuditStatus.TERMINAL,
            runtime_runs=(),
        )


def _actions(advice):  # type: ignore[no-untyped-def]
    return {preview.action: preview for preview in advice.action_previews}


class TerminalEffectRecoveryAdviceTests(unittest.TestCase):
    def test_terminal_job_keeps_open_effect_case_visible_and_only_reconciliation_enabled(self) -> None:
        case = {
            "action_id": "action-open",
            "case_status": "OPEN",
            "resource_released": None,
        }

        advice = _TerminalInspector((case,)).recovery_advice("terminal-job")

        self.assertEqual(advice.recovery_state, "TERMINAL_EFFECT_OUTCOME_UNKNOWN")
        self.assertIs(
            advice.disposition,
            RecoveryDisposition.RECONCILE_OR_COMPENSATE_REQUIRED,
        )
        self.assertEqual(advice.effect_recovery_cases, (case,))
        actions = _actions(advice)
        self.assertTrue(actions["reconcile"].enabled)
        self.assertFalse(actions["resume"].enabled)
        self.assertFalse(actions["retry"].enabled)
        self.assertFalse(actions["skip"].enabled)
        self.assertFalse(actions["cancel"].enabled)

    def test_terminal_job_keeps_sealed_unknown_case_fail_closed_without_actions(self) -> None:
        case = {
            "action_id": "action-sealed",
            "case_status": "SEALED_UNKNOWN",
            "resource_released": False,
        }

        advice = _TerminalInspector((case,)).recovery_advice("terminal-job")

        self.assertEqual(
            advice.recovery_state,
            "TERMINAL_EFFECT_OUTCOME_SEALED_FAIL_CLOSED",
        )
        self.assertIs(advice.disposition, RecoveryDisposition.FAIL_CLOSED)
        self.assertEqual(advice.effect_recovery_cases, (case,))
        self.assertTrue(all(not preview.enabled for preview in advice.action_previews))

    def test_terminal_remote_only_claim_is_visible_and_explicitly_resolvable(self) -> None:
        claim = {
            "action_id": "action-remote-only",
            "case_status": "OPEN",
            "effect": "WRITE",
            "next_action": "CONFIRM_NO_EFFECT_AND_RELEASE_EXACT_OWNER",
        }

        advice = _TerminalInspector(remote_claims=(claim,)).recovery_advice(
            "terminal-job"
        )

        self.assertEqual(
            advice.recovery_state,
            "TERMINAL_REMOTE_EFFECT_CLAIM_REQUIRES_CLOSURE",
        )
        self.assertIs(
            advice.disposition,
            RecoveryDisposition.RECONCILE_OR_COMPENSATE_REQUIRED,
        )
        self.assertEqual(advice.remote_effect_resource_claims, (claim,))
        actions = _actions(advice)
        self.assertTrue(actions["reconcile"].enabled)
        self.assertTrue(all(
            not preview.enabled
            for name, preview in actions.items()
            if name != "reconcile"
        ))

    def test_terminal_remote_fail_closed_claim_never_recommends_release(self) -> None:
        claim = {
            "action_id": "action-remote-unknown",
            "case_status": "FAIL_CLOSED",
            "effect": "WRITE",
            "next_action": "MANUAL_INVESTIGATION_NO_RELEASE",
        }

        advice = _TerminalInspector(remote_claims=(claim,)).recovery_advice(
            "terminal-job"
        )

        self.assertEqual(
            advice.recovery_state,
            "TERMINAL_REMOTE_EFFECT_CLAIM_FAIL_CLOSED",
        )
        self.assertIs(advice.disposition, RecoveryDisposition.FAIL_CLOSED)
        self.assertTrue(all(not preview.enabled for preview in advice.action_previews))
        self.assertFalse(
            any("effect-resolve" in action for action in advice.recommended_actions)
        )

    def test_closed_remote_claim_does_not_create_recovery_action(self) -> None:
        claim = {
            "action_id": "action-remote-closed",
            "case_status": "CLOSED",
            "effect": "WRITE",
            "next_action": "NONE",
        }

        advice = _TerminalInspector(remote_claims=(claim,)).recovery_advice(
            "terminal-job"
        )

        self.assertEqual(advice.recovery_state, "TERMINAL_NO_RECOVERY")
        self.assertEqual(advice.remote_effect_resource_claims, (claim,))
        self.assertTrue(all(not preview.enabled for preview in advice.action_previews))


if __name__ == "__main__":
    unittest.main()
