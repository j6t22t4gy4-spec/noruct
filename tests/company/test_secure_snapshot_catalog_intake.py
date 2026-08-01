import hashlib
import json
import sqlite3
import sys
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from dynamic_firm.company.model_intelligence import MODEL_INTELLIGENCE_SNAPSHOT_SCHEMA, ModelIntelligenceSnapshot
from dynamic_firm.company.model_intelligence_catalog import SQLiteModelIntelligenceCatalog
from dynamic_firm.company.secure_snapshot_catalog_intake import SecureSnapshotCatalogFinalStatus, SecureSnapshotCatalogIntake, SecureSnapshotCatalogIntakeReceipt, SecureSnapshotCatalogRecord
from dynamic_firm.company.snapshot_intake_security import IntakeRejection, IntakeStatus, SnapshotIntakeEnvelope, SnapshotIntakeVerifier, TrustKeyPolicy, TrustKeyState
from dynamic_firm.kernel.graph import graph_from_proposal
from dynamic_firm.kernel.models import EmployeeRecord
from dynamic_firm.kernel.mutation import frozen_snapshot_digest
from dynamic_firm.runtime.job_ledger import ActiveJobInspector, SQLiteActiveJobLedger
from dynamic_firm.runtime.store import RunStore
from tests.kernel.helpers import company_request, task


def snapshot_payload(snapshot_id: str, *, generated_at: str = "2026-08-01T00:00:00Z") -> dict[str, object]:
    return {
        "schema": MODEL_INTELLIGENCE_SNAPSHOT_SCHEMA, "snapshot_id": snapshot_id,
        "generated_at": generated_at, "expires_at": "2026-08-08T00:00:00Z",
        "publisher_identity": "publisher-one", "signature_reference": "sig-one",
        "benchmark_harness_revision": "harness-r1", "dataset_revision": "dataset-r1", "evaluator_revision": "evaluator-r1",
        "provider_route_class": "general", "requested_model_id": "model-r1", "identity_assurance": "VERSIONED_MODEL_ID",
        "task_class_distributions": {"coding": {"sample_count": 1, "success_rate": 1, "lower_bound": 1, "upper_bound": 1}},
        "error_correlation": [],
        "cost_latency_source": {"region": "local", "observed_at": generated_at, "source_revision": "r1", "latency_availability": "UNAVAILABLE", "latency_ms_p50": None, "cost_availability": "UNAVAILABLE", "input_cost_per_million": None, "output_cost_per_million": None},
        "limitations": ["synthetic-only"], "contamination_disclosure": "synthetic-only",
    }


def envelope(payload: dict[str, object], *, sequence: int = 1, revision: int = 1, signature_reference: str = "sig-one") -> SnapshotIntakeEnvelope:
    return SnapshotIntakeEnvelope(
        ModelIntelligenceSnapshot.from_mapping(payload).canonical_bytes(),
        "publisher-one", "key-one", 1, sequence, revision, signature_reference,
    )


class SecureSnapshotCatalogIntakeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.clock = datetime(2026, 8, 1, tzinfo=UTC)
        self.verifier = SnapshotIntakeVerifier(
            trust_keys=(TrustKeyPolicy("publisher-one", "key-one", 1, TrustKeyState.ACTIVE),),
            synthetic_signature_checker=lambda content, key, signature: key == "key-one" and signature == "sig-one" and bool(content),
        )
        self.catalog = SQLiteModelIntelligenceCatalog(
            Path(self.temporary.name) / "catalog.sqlite3",
            synthetic_verifier=lambda content, signature: signature == "detached:" + hashlib.sha256(content).hexdigest(),
            now=lambda: self.clock,
        )
        self.intake = SecureSnapshotCatalogIntake(self.verifier, self.catalog)

    def tearDown(self) -> None:
        self.catalog.close()
        self.temporary.cleanup()

    def detached_signature(self, submission: SnapshotIntakeEnvelope) -> str:
        return "detached:" + hashlib.sha256(submission.snapshot_canonical_bytes).hexdigest()

    def test_invalid_security_intake_never_records_candidate_or_changes_active(self) -> None:
        good = envelope(snapshot_payload("active"))
        accepted = self.intake.ingest(good, detached_signature=self.detached_signature(good))
        active = self.catalog.activate(accepted.catalog_record.digest)  # type: ignore[union-attr]
        original_ingest = self.catalog.ingest
        observed_ingest = Mock(wraps=original_ingest)
        self.catalog.ingest = observed_ingest  # type: ignore[method-assign]
        invalid_key = SnapshotIntakeEnvelope(good.snapshot_canonical_bytes, "publisher-one", "unknown-key", 1, 2, 2, "sig-one")
        invalid_signature = envelope(snapshot_payload("invalid-signature"), sequence=2, revision=2, signature_reference="wrong")
        noncanonical = SnapshotIntakeEnvelope(b"{", "publisher-one", "key-one", 1, 2, 2, "sig-one")
        for submission, rejection in (
            (invalid_key, IntakeRejection.UNKNOWN_KEY),
            (invalid_signature, IntakeRejection.SIGNATURE_REFERENCE_MISMATCH),
            (noncanonical, IntakeRejection.MALFORMED_OR_OVERSIZED_SNAPSHOT),
        ):
            with self.subTest(rejection=rejection):
                result = self.intake.ingest(submission, detached_signature="unused")
                self.assertEqual(result.intake.rejection, rejection)
                self.assertIsNone(result.catalog_record)
                self.assertEqual(self.catalog.active().digest, active.digest)  # type: ignore[union-attr]
        observed_ingest.assert_not_called()
        self.assertEqual(self.verifier.accepted_cursors[0].sequence, 1)

    def test_catalog_rejection_does_not_advance_prepared_cursor(self) -> None:
        submission = envelope(snapshot_payload("bad-catalog"))
        result = self.intake.ingest(submission, detached_signature="not-the-synthetic-signature")
        self.assertEqual(result.final_status, SecureSnapshotCatalogFinalStatus.CATALOG_REJECTED)
        self.assertEqual(result.intake.status, IntakeStatus.ACCEPTED)
        self.assertEqual(result.catalog_record.state, "REJECTED")  # type: ignore[union-attr]
        self.assertEqual(self.verifier.accepted_cursors, ())
        self.assertIsNone(self.catalog.active())

    def test_synthetic_signature_rejection_never_records_candidate(self) -> None:
        verifier = SnapshotIntakeVerifier(
            trust_keys=(TrustKeyPolicy("publisher-one", "key-one", 1, TrustKeyState.ACTIVE),),
            synthetic_signature_checker=lambda *_: False,
        )
        intake = SecureSnapshotCatalogIntake(verifier, self.catalog)
        submission = envelope(snapshot_payload("bad-signature"))

        result = intake.ingest(submission, detached_signature=self.detached_signature(submission))

        self.assertEqual(result.final_status, SecureSnapshotCatalogFinalStatus.INTAKE_REJECTED)
        self.assertEqual(result.intake.rejection, IntakeRejection.SYNTHETIC_SIGNATURE_REJECTED)
        self.assertIsNone(result.catalog_record)
        self.assertEqual(verifier.accepted_cursors, ())
        self.assertIsNone(self.catalog.active())

    def test_verified_candidate_requires_explicit_activation_and_result_is_content_free(self) -> None:
        submission = envelope(snapshot_payload("candidate"))
        result = self.intake.ingest(submission, detached_signature=self.detached_signature(submission))
        self.assertEqual(result.final_status, SecureSnapshotCatalogFinalStatus.ACCEPTED)
        self.assertEqual(result.intake.status, IntakeStatus.ACCEPTED)
        self.assertEqual(result.catalog_record.state, "VERIFIED_CANDIDATE")  # type: ignore[union-attr]
        self.assertIsNone(self.catalog.active())
        self.assertEqual(self.catalog.activate(result.catalog_record.digest).state, "ACTIVE")  # type: ignore[union-attr]
        self.assertEqual(set(SecureSnapshotCatalogIntakeReceipt.__dataclass_fields__), {"final_status", "intake", "catalog_record"})
        self.assertEqual(set(SecureSnapshotCatalogRecord.__dataclass_fields__), {"digest", "snapshot_id", "state", "signature_digest", "receipt_status"})
        self.assertNotIn("sig-one", repr(result))
        self.assertNotIn(submission.snapshot_canonical_bytes.decode("utf-8"), repr(result))

    def test_replay_rejects_before_catalog_mutation(self) -> None:
        first = envelope(snapshot_payload("first"))
        self.intake.ingest(first, detached_signature=self.detached_signature(first))
        replay = envelope(snapshot_payload("replay", generated_at="2026-08-01T00:01:00Z"), sequence=1, revision=2)
        result = self.intake.ingest(replay, detached_signature=self.detached_signature(replay))
        self.assertEqual(result.intake.rejection, IntakeRejection.REPLAYED_SEQUENCE)
        self.assertIsNone(result.catalog_record)
        self.assertEqual(self.verifier.accepted_cursors[0].sequence, 1)

    def test_mismatched_retry_signature_or_reference_cannot_reuse_candidate_or_advance_cursor(self) -> None:
        submission = envelope(snapshot_payload("candidate"))
        self.catalog.ingest(
            json.loads(submission.snapshot_canonical_bytes),
            detached_signature=self.detached_signature(submission),
            signature_reference="sig-one",
        )

        result = self.intake.ingest(submission, detached_signature="different-detached-signature")

        self.assertEqual(result.final_status, SecureSnapshotCatalogFinalStatus.CATALOG_REJECTED)
        self.assertEqual(result.intake.status, IntakeStatus.ACCEPTED)
        self.assertIsNone(result.catalog_record)
        self.assertEqual(self.verifier.accepted_cursors, ())
        digest = ModelIntelligenceSnapshot.from_mapping(json.loads(submission.snapshot_canonical_bytes)).content_digest
        self.assertEqual(self.catalog._record(digest).state, "VERIFIED_CANDIDATE")

    def test_checker_or_clock_failure_does_not_leave_pending_catalog_row(self) -> None:
        submission = envelope(snapshot_payload("checker-failure"))
        broken_catalog = SQLiteModelIntelligenceCatalog(
            Path(self.temporary.name) / "broken.sqlite3",
            synthetic_verifier=lambda *_: (_ for _ in ()).throw(RuntimeError("checker failed")),
            now=lambda: self.clock,
        )
        try:
            result = SecureSnapshotCatalogIntake(self.verifier, broken_catalog).ingest(
                submission,
                detached_signature=self.detached_signature(submission),
            )
            self.assertEqual(result.final_status, SecureSnapshotCatalogFinalStatus.CATALOG_REJECTED)
            self.assertEqual(result.intake.status, IntakeStatus.ACCEPTED)
            self.assertIsNone(result.catalog_record)
            self.assertEqual(self.verifier.accepted_cursors, ())
            rows = broken_catalog._connection.execute("SELECT count(*) FROM model_intelligence_snapshots").fetchone()[0]
            self.assertEqual(rows, 0)
        finally:
            broken_catalog.close()

    def test_catalog_finalization_failure_does_not_advance_cursor(self) -> None:
        submission = envelope(snapshot_payload("finalization-failure"))

        def fail_finalize(*_: object) -> object:
            raise sqlite3.OperationalError("injected write failure")

        self.catalog.finalize_prepared = fail_finalize  # type: ignore[method-assign]
        with self.assertRaises(sqlite3.OperationalError):
            self.intake.ingest(
                submission,
                detached_signature=self.detached_signature(submission),
            )

        self.assertEqual(self.verifier.accepted_cursors, ())
        self.assertRaises(
            KeyError,
            self.catalog._record,
            hashlib.sha256(submission.snapshot_canonical_bytes).hexdigest(),
        )

    def test_reentrant_catalog_preflight_cannot_commit_or_persist_stale_candidate(self) -> None:
        first = envelope(snapshot_payload("first"), sequence=1, revision=1)
        newer = envelope(snapshot_payload("newer", generated_at="2026-08-01T00:01:00Z"), sequence=2, revision=2)
        original_prepare = self.catalog.prepare_ingest

        def reentrant_prepare(*args: object, **kwargs: object):
            self.assertEqual(self.verifier.verify(newer).status, IntakeStatus.ACCEPTED)
            return original_prepare(*args, **kwargs)

        self.catalog.prepare_ingest = reentrant_prepare  # type: ignore[method-assign]
        result = self.intake.ingest(first, detached_signature=self.detached_signature(first))

        self.assertEqual(result.final_status, SecureSnapshotCatalogFinalStatus.INTAKE_REJECTED)
        self.assertEqual(result.intake.rejection, IntakeRejection.REPLAYED_SEQUENCE)
        self.assertIsNone(result.catalog_record)
        self.assertEqual(self.verifier.accepted_cursors[0].sequence, 2)
        self.assertRaises(KeyError, self.catalog._record, hashlib.sha256(first.snapshot_canonical_bytes).hexdigest())

    def test_snapshot_intake_cannot_mutate_an_active_job_audit(self) -> None:
        """Catalog intake has no path to a retained Job or its running state."""

        runtime = RunStore(Path(self.temporary.name) / "runtime.sqlite3")
        try:
            request = company_request(
                (task("pending"),),
                final_task_id="pending",
                roster=(EmployeeRecord("analyst", "Analyst", ("analysis",)),),
            )
            graph = graph_from_proposal(
                request.plan_proposal,
                max_tasks=request.job_limits.max_tasks,
            )
            SQLiteActiveJobLedger(runtime).start_job(
                request,
                graph,
                frozen_snapshot_digest(request),
            )
            before = ActiveJobInspector(runtime).inspect(request.job_id)

            submission = envelope(snapshot_payload("isolated-catalog"))
            accepted = self.intake.ingest(
                submission,
                detached_signature=self.detached_signature(submission),
            )
            self.assertEqual(accepted.final_status, SecureSnapshotCatalogFinalStatus.ACCEPTED)

            after = ActiveJobInspector(runtime).inspect(request.job_id)
            self.assertEqual(after.audit_status, before.audit_status)
            self.assertEqual(after.chain_head, before.chain_head)
            self.assertEqual(after.frozen_snapshot_hash, before.frozen_snapshot_hash)
            self.assertEqual(after.runtime_runs, before.runtime_runs)
            self.assertEqual(after.attempts, before.attempts)
        finally:
            runtime.close()


if __name__ == "__main__":
    unittest.main()
