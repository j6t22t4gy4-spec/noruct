from __future__ import annotations

import io
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from dynamic_firm.cli import EXIT_INPUT, EXIT_OK, EXIT_RUNTIME, _normalize_argv, main
from dynamic_firm.kernel.models import JobMetrics, JobResult, JobStatus
from dynamic_firm.knowledge import (
    KnowledgeExecutionOutcome,
    KnowledgeFirmBridge,
    KnowledgeStore,
    knowledge_state_path,
    knowledge_vault_path,
)
from dynamic_firm.knowledge.service import UserKnowledgeService
from dynamic_firm.knowledge.vault import KnowledgeVault, VaultObject
from dynamic_firm.runtime.models import Usage


class KnowledgeCliTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.state = self.root / "runtime.db"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def run_cli(
        self,
        *arguments: str,
        provider_factory=lambda _config: (_ for _ in ()).throw(
            AssertionError("provider-free command constructed a provider")
        ),
    ) -> tuple[int, str, str]:
        output = io.StringIO()
        error = io.StringIO()
        code = main(
            [*arguments, "--state", str(self.state), "--json"],
            provider_factory=provider_factory,
            stdin=io.StringIO(),
            stdout=output,
            stderr=error,
        )
        return code, output.getvalue(), error.getvalue()

    def test_local_commands_are_normalized_and_status_does_not_create_state(self) -> None:
        self.assertEqual(_normalize_argv(["knowledge", "status"]), ["knowledge", "status"])
        self.assertEqual(_normalize_argv(["intent", "list"]), ["intent", "list"])
        self.assertEqual(_normalize_argv(["decision", "due"]), ["decision", "due"])

        code, output, error = self.run_cli("knowledge", "status")

        self.assertEqual(code, EXIT_OK, error)
        self.assertFalse(knowledge_state_path(self.state).exists())
        self.assertFalse(json.loads(output)["database_present"])

        code, output, error = self.run_cli("knowledge", "capabilities")

        self.assertEqual(code, EXIT_OK, error)
        capabilities = json.loads(output)
        self.assertEqual(capabilities["processor"], "local-document")
        self.assertEqual(capabilities["execution_scope"], "local-process-only")
        self.assertFalse(knowledge_state_path(self.state).exists())

    def test_remote_fetch_requires_explicit_confirmation_before_any_network_read(self) -> None:
        code, _output, error = self.run_cli(
            "knowledge", "remote-fetch", "https://example.com/brief.txt"
        )

        self.assertEqual(code, EXIT_INPUT)
        self.assertIn("requires --confirm", error)
        self.assertFalse(knowledge_state_path(self.state).exists())

        with patch(
            "dynamic_firm.knowledge.service.download_public_https_asset",
            side_effect=AssertionError("invalid digest must fail before network"),
        ):
            code, _output, error = self.run_cli(
                "knowledge", "remote-fetch", "https://example.com/brief.txt",
                "--expected-sha256", "not-a-digest", "--confirm",
            )
        self.assertEqual(code, EXIT_INPUT)
        self.assertIn("expected SHA-256", error)
        self.assertFalse(knowledge_state_path(self.state).exists())

        code, _output, error = self.run_cli(
            "knowledge", "remote-refresh", "asset-prior"
        )

        self.assertEqual(code, EXIT_INPUT)
        self.assertIn("requires --confirm", error)
        self.assertFalse(knowledge_state_path(self.state).exists())

    def test_provider_free_knowledge_intent_decision_and_lifecycle_path(self) -> None:
        source = self.root / "brief.txt"
        source.write_text("North star launch is September 1.\n", encoding="utf-8")

        code, output, error = self.run_cli("knowledge", "add", str(source))
        self.assertEqual(code, EXIT_OK, error)

        asset = json.loads(output)["asset"]
        self.assertEqual(asset["status"], "READY")

        code, output, error = self.run_cli("knowledge", "recall", "north star launch")
        self.assertEqual(code, EXIT_OK, error)
        pack = json.loads(output)
        self.assertEqual(len(pack["items"]), 1)

        code, output, error = self.run_cli(
            "knowledge", "remember", "The launch owner is Mina.", "--kind", "FACT",
            "--epistemic-status", "OBSERVED", "--trust-class", "USER_ASSERTED",
            "--unknown-ref", "unknown:delegation-date",
        )
        self.assertEqual(code, EXIT_OK, error)
        record_id = json.loads(output)["record_id"]
        code, output, error = self.run_cli(
            "knowledge", "correct", record_id, "The launch owner is Joon."
        )
        self.assertEqual(code, EXIT_OK, error)
        corrected = json.loads(output)
        self.assertEqual(corrected["supersedes_record_id"], record_id)
        with KnowledgeStore(knowledge_state_path(self.state)) as store:
            annotation = store.epistemic_annotation("RECORD", corrected["record_id"])
            self.assertIsNotNone(annotation)
            assert annotation is not None
            self.assertEqual(annotation.epistemic_status.value, "OBSERVED")
            self.assertEqual(annotation.unknown_refs, ("unknown:delegation-date",))

        code, output, error = self.run_cli(
            "intent",
            "create",
            "Prepare the launch plan",
            "--knowledge-query",
            "north star launch",
            "--constraint",
            "No invented dates",
        )
        self.assertEqual(code, EXIT_OK, error)
        intent_id = json.loads(output)["intent_id"]
        code, output, error = self.run_cli("intent", "show", intent_id)
        self.assertEqual(code, EXIT_OK, error)
        self.assertEqual(len(json.loads(output)["history"]), 1)

        code, output, error = self.run_cli(
            "decision",
            "record",
            "Use a September launch",
            "--rationale",
            "The preserved brief states September 1",
            "--intent-id",
            intent_id,
            "--evidence-pack-id",
            pack["pack_id"],
            "--review-at",
            "2026-08-01T00:00:00Z",
        )
        self.assertEqual(code, EXIT_OK, error)
        decision_id = json.loads(output)["decision_id"]
        code, output, error = self.run_cli(
            "decision", "due", "--as-of", "2026-08-02T00:00:00Z"
        )
        self.assertEqual(code, EXIT_OK, error)
        self.assertEqual(json.loads(output)[0]["decision_id"], decision_id)

        archive = self.root / "knowledge.noruct"
        code, output, error = self.run_cli("knowledge", "export", str(archive))
        self.assertEqual(code, EXIT_OK, error)
        self.assertTrue(archive.is_file())
        exported_hash = json.loads(output)["archive_sha256"]
        code, _, error = self.run_cli("knowledge", "delete", "--confirm")
        self.assertEqual(code, EXIT_OK, error)
        self.assertFalse(knowledge_state_path(self.state).exists())
        code, output, error = self.run_cli("knowledge", "restore", str(archive))
        self.assertEqual(code, EXIT_OK, error)
        self.assertEqual(json.loads(output)["archive_sha256"], exported_hash)

        code, _, error = self.run_cli(
            "knowledge", "forget", corrected["record_id"], "--confirm"
        )
        self.assertEqual(code, EXIT_OK, error)

    def test_provider_free_folder_register_scan_list_open_and_recall(self) -> None:
        folder = self.root / "knowledge-folder"
        folder.mkdir()
        (folder / "strategy.md").write_text(
            "The pricing strategy review is August 20.", encoding="utf-8"
        )

        code, output, error = self.run_cli("knowledge", "folder-add", str(folder))
        self.assertEqual(code, EXIT_OK, error)
        registration = json.loads(output)
        folder_id = registration["folder"]["folder_id"]
        self.assertEqual(registration["scan"]["ready_files"], 1)

        code, output, error = self.run_cli("knowledge", "folder-files", folder_id)
        self.assertEqual(code, EXIT_OK, error)
        entry_id = json.loads(output)[0]["entry_id"]

        code, output, error = self.run_cli(
            "knowledge", "folder-open", entry_id, "--max-bytes", "512"
        )
        self.assertEqual(code, EXIT_OK, error)
        opened = json.loads(output)
        self.assertIn("pricing strategy", opened["content"])
        self.assertTrue(opened["snapshot_asset_id"].startswith("asset-"))

        code, output, error = self.run_cli(
            "knowledge", "recall", "pricing strategy"
        )
        self.assertEqual(code, EXIT_OK, error)
        item = json.loads(output)["items"][0]
        self.assertEqual(item["source_type"], "folder_file")
        self.assertEqual(item["location"]["relative_path"], "strategy.md")

    def test_provider_free_folder_lifecycle_never_mutates_raw_files(self) -> None:
        original = self.root / "knowledge-folder"
        original.mkdir()
        (original / "original.md").write_text("Original raw file.", encoding="utf-8")
        code, output, error = self.run_cli("knowledge", "folder-add", str(original))
        self.assertEqual(code, EXIT_OK, error)
        folder_id = json.loads(output)["folder"]["folder_id"]

        code, output, error = self.run_cli("knowledge", "folder-pause", folder_id)
        self.assertEqual(code, EXIT_OK, error)
        self.assertEqual(json.loads(output)["status"], "PAUSED")
        code, _, error = self.run_cli("knowledge", "folder-scan", folder_id)
        self.assertEqual(code, EXIT_INPUT, error)

        replacement = self.root / "replacement-folder"
        replacement.mkdir()
        (replacement / "replacement.md").write_text("Replacement raw file.", encoding="utf-8")
        code, output, error = self.run_cli(
            "knowledge", "folder-relink", folder_id, str(replacement)
        )
        self.assertEqual(code, EXIT_OK, error)
        self.assertEqual(Path(json.loads(output)["root_path"]), replacement.resolve())
        code, output, error = self.run_cli("knowledge", "folder-resume", folder_id)
        self.assertEqual(code, EXIT_OK, error)
        self.assertEqual(json.loads(output)["status"], "ACTIVE")

        code, output, error = self.run_cli(
            "knowledge", "folder-remove", folder_id, "--confirm"
        )
        self.assertEqual(code, EXIT_OK, error)
        self.assertTrue(json.loads(output)["removed"])
        self.assertTrue((original / "original.md").exists())
        self.assertTrue((replacement / "replacement.md").exists())

    def test_folder_plan_is_database_free_and_reports_secret_like_exclusions(self) -> None:
        folder = self.root / "folder-plan"
        folder.mkdir()
        (folder / "strategy.md").write_text("safe", encoding="utf-8")
        (folder / ".env").write_text("TOKEN=private", encoding="utf-8")

        code, output, error = self.run_cli("knowledge", "folder-plan", str(folder))

        self.assertEqual(code, EXIT_OK, error)
        payload = json.loads(output)
        self.assertEqual(payload["candidate_files"], 1)
        self.assertEqual(payload["ignored_secret_like"], 1)
        self.assertFalse(knowledge_state_path(self.state).exists())

    def test_folder_ignore_rules_are_previewable_persisted_and_require_confirmation(self) -> None:
        folder = self.root / "folder-ignore"
        folder.mkdir()
        (folder / "strategy.md").write_text("safe", encoding="utf-8")
        (folder / "draft.tmp").write_text("private scratch", encoding="utf-8")

        code, output, error = self.run_cli(
            "knowledge", "folder-plan", str(folder), "--ignore", "*.tmp"
        )
        self.assertEqual(code, EXIT_OK, error)
        self.assertEqual(json.loads(output)["ignored_user_patterns"], 1)
        self.assertFalse(knowledge_state_path(self.state).exists())

        code, output, error = self.run_cli(
            "knowledge", "folder-add", str(folder), "--ignore", "*.tmp"
        )
        self.assertEqual(code, EXIT_OK, error)
        registered = json.loads(output)
        folder_id = registered["folder"]["folder_id"]
        self.assertEqual(registered["folder"]["ignore_globs"], ["*.tmp"])
        self.assertEqual(registered["scan"]["skipped_user_ignored"], 1)

        code, _, error = self.run_cli("knowledge", "folder-ignore-set", folder_id)
        self.assertEqual(code, EXIT_INPUT, error)
        code, output, error = self.run_cli(
            "knowledge", "folder-ignore-set", folder_id, "--confirm", "--ignore", "*.md"
        )
        self.assertEqual(code, EXIT_OK, error)
        self.assertEqual(json.loads(output)["ignore_globs"], ["*.md"])

    def test_provider_free_question_research_review_to_explicit_intent_path(self) -> None:
        code, output, error = self.run_cli(
            "decision", "record", "Hold current price", "--rationale", "Review market evidence later",
            "--review-at", "2026-08-20T00:00:00Z",
        )
        self.assertEqual(code, EXIT_OK, error)
        decision_id = json.loads(output)["decision_id"]
        code, output, error = self.run_cli("research", "review-propose", decision_id)
        self.assertEqual(code, EXIT_OK, error)
        proposal = json.loads(output)
        request_id = proposal["research_request"]["request_id"]
        self.assertEqual(proposal["research_request"]["status"], "DRAFT")
        self.assertTrue(proposal["question"]["question_id"].startswith("question-"))
        code, output, error = self.run_cli("research", "accept", request_id, "--priority", "71")
        self.assertEqual(code, EXIT_OK, error)
        accepted = json.loads(output)
        self.assertEqual(accepted["research_request"]["status"], "ACCEPTED")
        self.assertEqual(accepted["intent"]["status"], "ACTIVE")
        self.assertEqual(accepted["intent"]["priority"], 71)
        code, output, error = self.run_cli("intent", "bindings")
        self.assertEqual(code, EXIT_OK, error)
        self.assertEqual(json.loads(output), [])
        archive = self.root / "control-plane.noruct"
        code, _, error = self.run_cli("knowledge", "export", str(archive))
        self.assertEqual(code, EXIT_OK, error)
        code, _, error = self.run_cli("knowledge", "delete", "--confirm")
        self.assertEqual(code, EXIT_OK, error)
        code, _, error = self.run_cli("knowledge", "restore", str(archive))
        self.assertEqual(code, EXIT_OK, error)
        code, output, error = self.run_cli("research", "show", request_id)
        self.assertEqual(code, EXIT_OK, error)
        self.assertEqual(json.loads(output)["research_request"]["status"], "ACCEPTED")

    def test_status_reports_and_repair_recovers_an_interrupted_asset_delete(self) -> None:
        source = self.root / "repair.txt"
        source.write_text("Crash recovery source", encoding="utf-8")
        code, output, error = self.run_cli("knowledge", "add", str(source))
        self.assertEqual(code, EXIT_OK, error)
        asset_id = json.loads(output)["asset"]["asset_id"]
        vault = KnowledgeVault(knowledge_vault_path(knowledge_state_path(self.state)))
        with KnowledgeStore(knowledge_state_path(self.state)) as store:
            asset = store.asset(asset_id)
            self.assertIsNotNone(asset)
            assert asset is not None
            representations = store.list_representations(asset_id)
            journal = vault.begin_delete(
                asset_id=asset_id,
                expected_asset_ids=(asset_id,),
                expected_representation_ids=tuple(
                    item.representation_id for item in representations
                ),
                objects=(
                    VaultObject(
                        asset.content_hash,
                        asset.byte_size,
                        asset.vault_relative_path,
                    ),
                    *(
                        VaultObject(
                            item.content_hash,
                            item.byte_size,
                            item.vault_relative_path,
                        )
                        for item in representations
                    ),
                ),
            )
            vault.stage_journal_delete(journal)

        code, output, error = self.run_cli("knowledge", "status")
        self.assertEqual(code, EXIT_OK, error)
        self.assertTrue(json.loads(output)["pending_asset_mutation"])

        code, _, error = self.run_cli("knowledge", "repair")
        self.assertEqual(code, EXIT_INPUT)
        self.assertTrue(vault.delete_journal_path.exists())

        code, output, error = self.run_cli("knowledge", "repair", "--confirm")
        self.assertEqual(code, EXIT_OK, error)
        self.assertEqual(json.loads(output)["recovery"], "RESTORED")
        self.assertFalse(vault.delete_journal_path.exists())
        with KnowledgeStore(knowledge_state_path(self.state)) as store:
            self.assertIsNotNone(store.asset(asset_id))

    def test_intent_run_passes_one_verified_binding_and_creates_pending_candidate(self) -> None:
        code, _, error = self.run_cli(
            "knowledge", "remember", "Release review requires two approvers."
        )
        self.assertEqual(code, EXIT_OK, error)
        code, output, error = self.run_cli(
            "intent",
            "create",
            "Draft the release review",
            "--knowledge-query",
            "release review approvers",
        )
        self.assertEqual(code, EXIT_OK, error)
        intent_id = json.loads(output)["intent_id"]
        calls: list[dict[str, object]] = []

        async def fake_run_goal(config, provider, **kwargs):
            del provider
            kwargs["task_evidence"].verify()
            self.assertEqual(config.goal, "Draft the release review")
            self.assertEqual(kwargs["execution_origin"].pack_id, kwargs["task_evidence"].pack_id)
            self.assertTrue(kwargs["request_id"].startswith("request-"))
            self.assertTrue(kwargs["job_id"].startswith("job-"))
            calls.append(kwargs)
            return JobResult(
                job_id=kwargs["job_id"],
                request_id=kwargs["request_id"],
                status=JobStatus.SUCCEEDED,
                summary="Two approvers must review the release.",
                acceptance_evidence=("bounded local evidence",),
                unresolved_issues=(),
                task_results=(),
                final_graph_version=1,
                final_tasks=(),
                metrics=JobMetrics(
                    unique_employee_count=1,
                    temporary_role_count=0,
                    maximum_parallelism=1,
                    graph_patch_count=0,
                    usage=Usage(),
                ),
            )

        output_stream = io.StringIO()
        error_stream = io.StringIO()
        with patch("dynamic_firm.cli.run_goal", new=fake_run_goal):
            code = main(
                [
                    "intent",
                    "run",
                    intent_id,
                    "--state",
                    str(self.state),
                    "--provider",
                    "ollama",
                    "--model",
                    "qwen-test",
                    "--no-auth",
                    "--employee-runtime",
                    "noruct",
                    "--permission-mode",
                    "read-only",
                    "--json",
                ],
                provider_factory=lambda _config: object(),
                stdin=io.StringIO(),
                stdout=output_stream,
                stderr=error_stream,
            )

        self.assertEqual(code, EXIT_OK, error_stream.getvalue())
        self.assertEqual(len(calls), 1)
        payload = json.loads(output_stream.getvalue())
        self.assertEqual(payload["knowledge"]["binding"]["status"], "TERMINAL")
        self.assertEqual(payload["knowledge"]["candidate"]["status"], "PENDING")
        with KnowledgeStore(knowledge_state_path(self.state)) as store:
            binding = store.execution_binding_for_job(payload["job"]["job_id"])
            self.assertIsNotNone(binding)
            assert binding is not None
            self.assertEqual(binding.status, "TERMINAL")
            self.assertEqual(binding.candidate_id, payload["knowledge"]["candidate"]["candidate_id"])

    def test_delayed_outcome_has_provider_free_list_and_observe_entrypoints(self) -> None:
        database = knowledge_state_path(self.state)
        with KnowledgeStore(database) as store:
            service = UserKnowledgeService(
                store,
                KnowledgeVault(knowledge_vault_path(database)),
            )
            store.create_record(kind="FACT", statement="A baseline exists.")
            intent = store.create_intent(
                goal="Evaluate the baseline",
                knowledge_query="baseline",
                acceptance_criteria=("Observed result is acceptable.",),
            )
            bridge = KnowledgeFirmBridge(service)
            prepared = bridge.prepare(
                intent.intent_id,
                request_id="request-cli-outcome",
                job_id="job-cli-outcome",
            )
            completed = bridge.complete(
                prepared,
                KnowledgeExecutionOutcome(
                    job_id=prepared.binding.job_id,
                    status="SUCCEEDED",
                    summary="Execution completed; outcome pending.",
                ),
            )

        code, output, error = self.run_cli("knowledge", "outcomes")
        self.assertEqual(code, EXIT_OK, error)
        self.assertEqual(json.loads(output)[0]["verdict"], "NOT_YET_OBSERVED")

        code, output, error = self.run_cli(
            "knowledge",
            "outcome-observe",
            completed.outcome.outcome_id,
            "--verdict",
            "INCONCLUSIVE",
            "--signal",
            "The sample was too small.",
            "--source-ref",
            "analytics:report-1",
            "--reviewer-ref",
            "user:owner",
            "--attribution-status",
            "CONFOUNDED",
        )
        self.assertEqual(code, EXIT_OK, error)
        observed = json.loads(output)
        self.assertEqual(observed["verdict"], "INCONCLUSIVE")
        self.assertEqual(observed["attribution_status"], "CONFOUNDED")

    def test_intent_run_failure_interrupts_prepared_binding_without_candidate(self) -> None:
        code, output, error = self.run_cli(
            "intent", "create", "Run a bounded failure", "--knowledge-query", "nothing"
        )
        self.assertEqual(code, EXIT_OK, error)
        intent_id = json.loads(output)["intent_id"]

        async def failed_run_goal(config, provider, **kwargs):
            del config, provider, kwargs
            raise RuntimeError("synthetic provider failure")

        output_stream = io.StringIO()
        error_stream = io.StringIO()
        with patch("dynamic_firm.cli.run_goal", new=failed_run_goal):
            code = main(
                [
                    "intent",
                    "run",
                    intent_id,
                    "--state",
                    str(self.state),
                    "--provider",
                    "ollama",
                    "--model",
                    "qwen-test",
                    "--no-auth",
                    "--employee-runtime",
                    "noruct",
                    "--permission-mode",
                    "read-only",
                    "--json",
                ],
                provider_factory=lambda _config: object(),
                stdin=io.StringIO(),
                stdout=output_stream,
                stderr=error_stream,
            )

        self.assertEqual(code, EXIT_RUNTIME)
        with KnowledgeStore(knowledge_state_path(self.state)) as store:
            bindings = store.list_execution_bindings()
            self.assertEqual(len(bindings), 1)
            self.assertEqual(bindings[0].status, "TERMINAL")
            self.assertEqual(bindings[0].job_status, "INTERRUPTED")
            self.assertIsNone(bindings[0].candidate_id)
            self.assertEqual(store.list_write_candidates(), ())

    def test_orphaned_prepared_binding_can_be_listed_and_explicitly_interrupted(self) -> None:
        code, output, error = self.run_cli(
            "intent", "create", "Recover an interrupted local run"
        )
        self.assertEqual(code, EXIT_OK, error)
        intent_id = json.loads(output)["intent_id"]
        with KnowledgeStore(knowledge_state_path(self.state)) as store:
            bridge = KnowledgeFirmBridge(
                UserKnowledgeService(
                    store,
                    KnowledgeVault(
                        knowledge_vault_path(knowledge_state_path(self.state))
                    ),
                )
            )
            prepared = bridge.prepare(
                intent_id,
                request_id="request-cli-crash-recovery",
                job_id="job-cli-crash-recovery",
            )

        code, output, error = self.run_cli("intent", "bindings", "--status", "PREPARED")
        self.assertEqual(code, EXIT_OK, error)
        self.assertEqual(json.loads(output)[0]["binding_id"], prepared.binding.binding_id)

        code, _, error = self.run_cli("intent", "interrupt", prepared.binding.binding_id)
        self.assertEqual(code, EXIT_INPUT)
        self.assertIn("requires --confirm", error)

        code, output, error = self.run_cli(
            "intent", "interrupt", prepared.binding.binding_id, "--confirm"
        )
        self.assertEqual(code, EXIT_OK, error)
        self.assertEqual(json.loads(output)["job_status"], "INTERRUPTED")
        with KnowledgeStore(knowledge_state_path(self.state)) as store:
            recovered = store.execution_binding(prepared.binding.binding_id)
            self.assertIsNotNone(recovered)
            assert recovered is not None
            self.assertEqual(
                (recovered.status, recovered.job_status),
                ("TERMINAL", "INTERRUPTED"),
            )

    def test_tampered_intent_fails_before_provider_construction(self) -> None:
        code, output, error = self.run_cli("intent", "create", "Authenticated goal")
        self.assertEqual(code, EXIT_OK, error)
        intent_id = json.loads(output)["intent_id"]
        connection = sqlite3.connect(knowledge_state_path(self.state))
        try:
            connection.execute(
                "UPDATE knowledge_intents SET goal = ? WHERE intent_id = ?",
                ("Tampered goal", intent_id),
            )
            connection.commit()
        finally:
            connection.close()
        provider_calls: list[object] = []

        def provider_factory(config):
            provider_calls.append(config)
            return object()

        code, _, error = self.run_cli(
            "intent",
            "run",
            intent_id,
            "--provider",
            "ollama",
            "--model",
            "qwen-test",
            "--no-auth",
            "--employee-runtime",
            "noruct",
            "--permission-mode",
            "read-only",
            provider_factory=provider_factory,
        )
        self.assertEqual(code, EXIT_INPUT)
        self.assertIn("immutable revision", error)
        self.assertEqual(provider_calls, [])

    def test_provider_construction_failure_terminalizes_prepared_binding(self) -> None:
        code, output, error = self.run_cli("intent", "create", "Provider factory failure")
        self.assertEqual(code, EXIT_OK, error)
        intent_id = json.loads(output)["intent_id"]

        def provider_factory(_config):
            raise RuntimeError("provider construction failed")

        code, _, _ = self.run_cli(
            "intent",
            "run",
            intent_id,
            "--provider",
            "ollama",
            "--model",
            "qwen-test",
            "--no-auth",
            "--employee-runtime",
            "noruct",
            "--permission-mode",
            "read-only",
            provider_factory=provider_factory,
        )
        self.assertEqual(code, EXIT_RUNTIME)
        with KnowledgeStore(knowledge_state_path(self.state)) as store:
            bindings = store.list_execution_bindings()
            self.assertEqual(len(bindings), 1)
            self.assertEqual(
                (bindings[0].status, bindings[0].job_status),
                ("TERMINAL", "INTERRUPTED"),
            )


if __name__ == "__main__":
    unittest.main()
