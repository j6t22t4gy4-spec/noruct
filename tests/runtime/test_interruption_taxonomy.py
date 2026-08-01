from __future__ import annotations

import unittest
from types import SimpleNamespace

from dynamic_firm.runtime.interruption import InterruptionCause
from dynamic_firm.runtime.job_inspector import ActiveJobInspector
from dynamic_firm.runtime.models import FailureCategory, RunStatus


class _EvidenceStore:
    def __init__(self) -> None:
        self.results = {
            "timeout": SimpleNamespace(
                status=RunStatus.FAILED,
                failure=SimpleNamespace(
                    category=FailureCategory.TIMEOUT,
                    code="MODEL_TIMEOUT",
                ),
            ),
            "cancel": SimpleNamespace(
                status=RunStatus.CANCELLED,
                failure=SimpleNamespace(
                    category=FailureCategory.CANCEL,
                    code="OPERATION_CANCELLED",
                ),
            ),
            "provider": SimpleNamespace(
                status=RunStatus.FAILED,
                failure=SimpleNamespace(
                    category=FailureCategory.MODEL,
                    code="MODEL_PROVIDER_ERROR",
                ),
            ),
            "unknown": SimpleNamespace(
                status=RunStatus.SUCCEEDED,
                failure=None,
            ),
        }

    def get_result(self, run_id: str):  # type: ignore[no-untyped-def]
        return self.results.get(run_id)

    @staticmethod
    def list_events(_run_id: str):  # type: ignore[no-untyped-def]
        return ()


class InterruptionTaxonomyTests(unittest.TestCase):
    def test_runtime_evidence_projects_every_stable_interruption_cause(self) -> None:
        inspector = object.__new__(ActiveJobInspector)
        inspector.store = _EvidenceStore()  # type: ignore[assignment]
        inspection = SimpleNamespace(
            runtime_runs=(
                SimpleNamespace(run_id="process", status=RunStatus.RUNNING.value),
                SimpleNamespace(run_id="timeout", status=RunStatus.FAILED.value),
                SimpleNamespace(run_id="cancel", status=RunStatus.CANCELLED.value),
                SimpleNamespace(run_id="provider", status=RunStatus.FAILED.value),
                SimpleNamespace(run_id="unknown", status=RunStatus.SUCCEEDED.value),
            )
        )

        evidence = inspector._interruption_evidence(inspection)

        self.assertEqual(
            set(evidence.causes),
            {
                InterruptionCause.USER_CANCEL,
                InterruptionCause.PROCESS_OR_MACHINE_LOSS,
                InterruptionCause.DEADLINE_TIMEOUT,
                InterruptionCause.PROVIDER_DISCONNECT,
            },
        )

    def test_unknown_is_used_only_when_no_specific_cause_exists(self) -> None:
        inspector = object.__new__(ActiveJobInspector)
        inspector.store = _EvidenceStore()  # type: ignore[assignment]

        evidence = inspector._interruption_evidence(
            SimpleNamespace(
                runtime_runs=(
                    SimpleNamespace(
                        run_id="unknown", status=RunStatus.SUCCEEDED.value
                    ),
                )
            )
        )

        self.assertEqual(evidence.causes, (InterruptionCause.UNKNOWN,))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
