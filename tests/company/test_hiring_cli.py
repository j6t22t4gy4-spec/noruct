from __future__ import annotations

import io
import json
import tempfile
import unittest
from pathlib import Path

from dynamic_firm.cli import EXIT_INPUT, EXIT_OK, main
from dynamic_firm.company import (
    CompanyStateStore,
    EvidenceSource,
    OrganizationEpisode,
    StaffingDemandEvidence,
    WorkflowTaskTemplate,
)
from dynamic_firm.kernel.models import EmployeeRecord


def record_demand(store: CompanyStateStore, job_id: str) -> None:
    episode = OrganizationEpisode.create(
        job_id=job_id,
        source=EvidenceSource.REAL_JOB,
        task_family="coding.security-review",
        context_fingerprint="python-repository",
        execution_profile="SHADOW_CODING",
        planning_mode="DYNAMIC",
        plan_template=(
            WorkflowTaskTemplate("security", ("security_review",), final=True),
        ),
        success=True,
        quality_score=1.0,
        baseline_quality_score=None,
        model_calls=1,
        baseline_model_calls=None,
        employee_count=2,
        maximum_parallelism=1,
        writer_count=1,
        approvals_requested=1,
        approvals_granted=1,
        preapproval_mutations=0,
        validation_attempts=(True,),
        ledger_digest=f"ledger-{job_id}",
    )
    store.record_episode(episode)
    store.record_staffing_demand(
        StaffingDemandEvidence.create(
            episode_id=episode.episode_id,
            job_id=job_id,
            source=episode.source,
            context_fingerprint=episode.context_fingerprint,
            execution_profile=episode.execution_profile,
            base_roster_revision=store.roster().revision,
            task_id="security",
            capability="security_review",
            role_label="Temporary Security Review Specialist",
            job_succeeded=True,
            validation_attempts=(True,),
            safety_violations=(),
            writer_count=1,
            approvals_requested=1,
            approvals_granted=1,
            preapproval_mutations=0,
            ledger_digest=episode.ledger_digest,
            recorded_at=episode.recorded_at,
        )
    )


class HiringCliTests(unittest.TestCase):
    def test_cli_recommends_and_previews_but_requires_explicit_apply(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "runtime.db"
            with CompanyStateStore(state) as store:
                store.ensure_roster_baseline(
                    (
                        EmployeeRecord(
                            "employee-generalist",
                            "Generalist",
                            ("conversation",),
                            model_profile="company-default",
                        ),
                    )
                )
                record_demand(store, "job-one")
                record_demand(store, "job-two")

            demands_out = io.StringIO()
            self.assertEqual(
                main(
                    [
                        "company",
                        "staffing-demands",
                        "--state",
                        str(state),
                        "--json",
                    ],
                    stdout=demands_out,
                ),
                EXIT_OK,
            )
            self.assertEqual(len(json.loads(demands_out.getvalue())), 2)

            recommend_out = io.StringIO()
            self.assertEqual(
                main(
                    [
                        "company",
                        "roster-recommend",
                        "--state",
                        str(state),
                        "--json",
                    ],
                    stdout=recommend_out,
                ),
                EXIT_OK,
            )
            recommendation = json.loads(recommend_out.getvalue())
            patch_id = recommendation["candidates"][0]["patch_id"]
            self.assertEqual(recommendation["decision"], "CANDIDATE_AVAILABLE")
            self.assertEqual(recommendation["active_roster_revision"], 2)
            self.assertFalse(recommendation["automatic_apply"])
            self.assertEqual(len(recommendation["evidence_by_patch"][patch_id]), 2)

            preview_out = io.StringIO()
            self.assertEqual(
                main(
                    [
                        "company",
                        "roster-preview",
                        patch_id,
                        "--state",
                        str(state),
                        "--json",
                    ],
                    stdout=preview_out,
                ),
                EXIT_OK,
            )
            preview = json.loads(preview_out.getvalue())
            self.assertEqual(len(preview["evidence"]), 2)
            self.assertTrue(preview["evidence_eligible_for_apply"])
            self.assertEqual(preview["active_roster_revision"], 2)

            denied_error = io.StringIO()
            self.assertEqual(
                main(
                    [
                        "company",
                        "roster-apply",
                        patch_id,
                        "--state",
                        str(state),
                    ],
                    stderr=denied_error,
                ),
                EXIT_INPUT,
            )
            self.assertIn("requires --confirm", denied_error.getvalue())

            for command in ("roster-approve", "roster-apply"):
                self.assertEqual(
                    main(
                        [
                            "company",
                            command,
                            patch_id,
                            "--state",
                            str(state),
                            "--confirm",
                            "--json",
                        ],
                        stdout=io.StringIO(),
                    ),
                    EXIT_OK,
                )

            with CompanyStateStore(state) as store:
                self.assertEqual(store.roster().revision, 3)
                self.assertEqual(store.summary().staffing_demand_count, 2)

            contracts_out = io.StringIO()
            self.assertEqual(
                main(
                    [
                        "company",
                        "hire-contracts",
                        "--state",
                        str(state),
                        "--json",
                    ],
                    stdout=contracts_out,
                ),
                EXIT_OK,
            )
            contracts = json.loads(contracts_out.getvalue())
            self.assertEqual(len(contracts), 1)
            self.assertEqual(contracts[0]["patch_id"], patch_id)

            hire_preview_out = io.StringIO()
            self.assertEqual(
                main(
                    [
                        "company",
                        "hire-preview",
                        patch_id,
                        "--state",
                        str(state),
                        "--json",
                    ],
                    stdout=hire_preview_out,
                ),
                EXIT_OK,
            )
            hire_preview = json.loads(hire_preview_out.getvalue())
            self.assertEqual(hire_preview["contract"]["applied_roster_revision"], 3)
            self.assertEqual(hire_preview["observations"], [])
            self.assertIsNone(hire_preview["latest_assessment"])
            self.assertFalse(hire_preview["state_changed"])

            hire_assess_out = io.StringIO()
            self.assertEqual(
                main(
                    [
                        "company",
                        "hire-assess",
                        patch_id,
                        "--state",
                        str(state),
                        "--json",
                    ],
                    stdout=hire_assess_out,
                ),
                EXIT_OK,
            )
            hire_assess = json.loads(hire_assess_out.getvalue())
            self.assertEqual(
                hire_assess["assessment"]["decision"],
                "INSUFFICIENT_OBSERVATION",
            )
            self.assertEqual(hire_assess["roster_revision_before"], 3)
            self.assertEqual(hire_assess["roster_revision_after"], 3)
            self.assertFalse(hire_assess["automatic_set_active"])


if __name__ == "__main__":
    unittest.main()
