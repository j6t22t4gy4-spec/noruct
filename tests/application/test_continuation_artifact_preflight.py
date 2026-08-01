from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from dynamic_firm.application.continuation_artifact_preflight import (
    ContinuationArtifactPreflightCode,
    ContinuationArtifactPreflightError,
    preflight_continuation_artifacts,
)
from dynamic_firm.company.models import canonical_json
from dynamic_firm.evolution.runtime_adapter import (
    EvolutionRuntimeArtifactAdapter,
    runtime_artifact_scopes,
)
from dynamic_firm.evolution.service import EvolutionNetworkService
from dynamic_firm.evolution.store import EvolutionStore
from dynamic_firm.kernel.models import (
    CompanyRunRequest,
    EmployeeRecord,
    JobTask,
    PlanProposal,
)
from dynamic_firm.runtime.models import VersionedContent


def _artifact(*, runtime_contract: str = "noruct_v1") -> dict[str, object]:
    return {
        "schema": "noruct.evolution-artifact.v1",
        "artifact_id": "repository_skill",
        "version": "1.0.0",
        "kind": "SKILL_PACKAGE",
        "release_channel": "STABLE",
        "compatibility": {
            "runtime_contract": runtime_contract,
            "required_capabilities": ["workspace_read"],
        },
        "content": {
            "skill_key": "repository_analysis",
            "applies_to": ["repository_analysis"],
            "steps": ["Inspect workspace shape before choosing a workflow"],
            "required_capabilities": [],
        },
        "passport": {
            "schema": "noruct.workforce-passport.v1",
            "benchmark": {
                "suite_id": "repository_suite",
                "version": "1.0.0",
                "digest": "b" * 64,
            },
            "metrics": {
                "quality_score": 0.8,
                "safety_score": 1.0,
                "cost_bucket": "LOW",
                "latency_bucket": "LOW",
            },
            "limitations": [],
        },
    }


def _request(
    *,
    employee_skills: tuple[VersionedContent, ...] = (),
) -> CompanyRunRequest:
    employee = EmployeeRecord(
        "employee-repository-analyst",
        "Repository Analyst",
        ("repository_analysis",),
    )
    task = JobTask(
        task_id="inspect",
        objective="Inspect the repository",
        depends_on=(),
        required_capabilities=("repository_analysis",),
        acceptance_criteria=("Repository evidence",),
    )
    return CompanyRunRequest(
        request_id="request-artifact-continuation",
        job_id="job-artifact-continuation",
        goal="Inspect the repository",
        plan_proposal=PlanProposal(
            proposal_id="proposal-artifact-continuation",
            goal="Inspect the repository",
            tasks=(task,),
            final_task_id=task.task_id,
        ),
        roster=(employee,),
        employee_skill_snapshots={employee.employee_id: employee_skills},
    )


def _audit_pins(pins: tuple[dict, ...]) -> tuple[dict[str, str], ...]:
    return tuple(
        {
            key: str(pin[key])
            for key in ("kind", "artifact_id", "version", "manifest_digest", "scope_key")
        }
        for pin in pins
    )


class ContinuationArtifactPreflightTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.store = EvolutionStore(self.root / "evolution.db")
        self.service = EvolutionNetworkService(self.store)

    def tearDown(self) -> None:
        self.store.close()
        self.temporary.cleanup()

    def _pin(self, *, runtime_contract: str = "noruct_v1") -> tuple[dict, ...]:
        self.service.register_artifact_manifest(
            _artifact(runtime_contract=runtime_contract)
        )
        self.service.stage_artifact("repository_skill", "1.0.0")
        self.service.install_artifact("repository_skill", "1.0.0")
        self.service.activate_artifact(
            scope_key="company_default",
            artifact_id="repository_skill",
            version="1.0.0",
            allowed_capabilities=("workspace_read",),
        )
        request = _request()
        return tuple(
            self.store.pin_active_artifacts_for_runtime_job(
                job_id=request.job_id,
                scope_keys=runtime_artifact_scopes(request.roster),
            )
        )

    def _request_with_projection(self, pins: tuple[dict, ...]) -> CompanyRunRequest:
        request = _request()
        resolution = EvolutionRuntimeArtifactAdapter(self.store).resolve(
            job_id=request.job_id,
            roster=request.roster,
            pins=pins,
        )
        projected = resolution.employee_skills[request.roster[0].employee_id]
        local = VersionedContent(
            content_id="employee-skill:local-review",
            revision="3",
            content="Local procedure remains alongside the pinned projection.",
            content_hash="c" * 64,
        )
        return replace(
            request,
            employee_skill_snapshots={request.roster[0].employee_id: (local, *projected)},
        )

    def assert_preflight_code(
        self,
        code: ContinuationArtifactPreflightCode,
        *,
        request: CompanyRunRequest,
        pins: tuple[dict, ...],
        store: EvolutionStore | None,
    ) -> None:
        with self.assertRaises(ContinuationArtifactPreflightError) as raised:
            preflight_continuation_artifacts(
                request=request,
                audit_pins=_audit_pins(pins),
                store=store,
            )
        self.assertEqual(raised.exception.code, code)

    def test_empty_pins_need_no_optional_store(self) -> None:
        result = preflight_continuation_artifacts(
            request=_request(),
            audit_pins=(),
            store=None,
        )

        self.assertEqual(result.pin_count, 0)
        self.assertEqual(result.projected_skill_count, 0)
        self.assertIsNone(result.resolution)

    def test_empty_pins_reject_a_frozen_network_projection(self) -> None:
        request = _request(
            employee_skills=(
                VersionedContent(
                    content_id=(
                        "employee-skill:employee-repository-analyst:network:unbound"
                    ),
                    revision="1.0.0",
                    content="Unbound",
                    content_hash="d" * 64,
                ),
            )
        )

        self.assert_preflight_code(
            ContinuationArtifactPreflightCode.SKILL_SNAPSHOT_MISMATCH,
            request=request,
            pins=(),
            store=None,
        )

    def test_exact_pin_and_frozen_projection_are_revalidated_provider_free(self) -> None:
        pins = self._pin()
        request = self._request_with_projection(pins)

        result = preflight_continuation_artifacts(
            request=request,
            audit_pins=_audit_pins(pins),
            store=self.store,
        )

        self.assertEqual(result.pin_count, 1)
        self.assertEqual(result.projected_skill_count, 1)
        self.assertIsNotNone(result.resolution)

    def test_pins_require_the_existing_optional_store(self) -> None:
        pins = self._pin()
        self.assert_preflight_code(
            ContinuationArtifactPreflightCode.STORE_REQUIRED,
            request=self._request_with_projection(pins),
            pins=pins,
            store=None,
        )

    def test_audit_and_local_runtime_pin_must_match_exactly(self) -> None:
        pins = self._pin()
        request = self._request_with_projection(pins)
        drifted = tuple({**pin, "manifest_digest": "e" * 64} for pin in pins)

        self.assert_preflight_code(
            ContinuationArtifactPreflightCode.RUNTIME_PIN_MISMATCH,
            request=request,
            pins=drifted,
            store=self.store,
        )

    def test_manifest_content_drift_fails_closed(self) -> None:
        pins = self._pin()
        request = self._request_with_projection(pins)
        artifact = _artifact()
        artifact["content"] = {
            **artifact["content"],
            "steps": ["Changed after the Job was frozen"],
        }
        with self.store._transaction() as connection:  # noqa: SLF001 - corruption fixture
            connection.execute(
                """UPDATE evolution_artifact_versions
                      SET manifest_json = ?
                    WHERE artifact_id = 'repository_skill' AND version = '1.0.0'""",
                (canonical_json(artifact),),
            )

        self.assert_preflight_code(
            ContinuationArtifactPreflightCode.CATALOG_INVALID,
            request=request,
            pins=pins,
            store=self.store,
        )

    def test_unsupported_runtime_contract_fails_closed(self) -> None:
        pins = self._pin(runtime_contract="future_v2")
        request = self._request_with_projection(pins)

        self.assert_preflight_code(
            ContinuationArtifactPreflightCode.RUNTIME_CONTRACT_UNSUPPORTED,
            request=request,
            pins=pins,
            store=self.store,
        )

    def test_recomputed_projected_skills_must_exactly_match_frozen_network_slice(self) -> None:
        pins = self._pin()
        request = self._request_with_projection(pins)
        employee_id = request.roster[0].employee_id
        frozen = request.employee_skill_snapshots[employee_id]
        network = next(item for item in frozen if ":network:" in item.content_id)
        local = next(item for item in frozen if ":network:" not in item.content_id)
        cases = {
            "missing": (local,),
            "changed": (local, replace(network, content="Changed frozen content")),
            "duplicated": (*frozen, network),
            "extra": (
                *frozen,
                replace(
                    network,
                    content_id=f"employee-skill:{employee_id}:network:unexpected",
                ),
            ),
        }
        for label, skills in cases.items():
            with self.subTest(label=label):
                self.assert_preflight_code(
                    ContinuationArtifactPreflightCode.SKILL_SNAPSHOT_MISMATCH,
                    request=replace(
                        request,
                        employee_skill_snapshots={employee_id: skills},
                    ),
                    pins=pins,
                    store=self.store,
                )


if __name__ == "__main__":
    unittest.main()
