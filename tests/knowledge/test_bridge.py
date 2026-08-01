from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from dynamic_firm.foundation.runtime import NoructEmployeeRuntimeService
from dynamic_firm.foundation.protocol import FoundationFrame
from dynamic_firm.kernel.models import EmployeeRecord, ExecutionOriginBinding
from dynamic_firm.kernel.service import FirmKernel
from dynamic_firm.kernel.testing import ScriptedEmployeeExecutionPort, ScriptedOutcome
from dynamic_firm.knowledge.bridge import KnowledgeFirmBridge
from dynamic_firm.knowledge.models import EvidenceItem, EvidencePack, KnowledgeExecutionOutcome
from dynamic_firm.knowledge.service import UserKnowledgeService
from dynamic_firm.knowledge.store import KnowledgeStore
from dynamic_firm.knowledge.vault import KnowledgeVault
from dynamic_firm.providers.fake import ScriptedModelProvider
from dynamic_firm.runtime.job_ledger import SQLiteActiveJobLedger
from dynamic_firm.runtime.models import (
    ContextBundle,
    EmployeeSessionRetention,
    ModelResponse,
    RunStatus,
    TaskEvidenceItem,
    TaskEvidencePack,
)
from dynamic_firm.runtime.prompt import PromptBuilder
from dynamic_firm.runtime.service import NativeEmployeeRuntimeService
from dynamic_firm.runtime.store import RunStore, employee_session_namespace
from dynamic_firm.runtime.tools import ToolRegistry
from tests.kernel.helpers import company_request, task
from tests.runtime.helpers import completion, make_request


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _task_evidence_pack(*contents: str) -> TaskEvidencePack:
    items = tuple(
        TaskEvidenceItem(
            citation_id=f"evidence-{index}",
            source_id=f"source-{index}",
            source_revision="1",
            title=f"Evidence {index}",
            content=content,
            source_hash=_sha256(f"complete source {index}: {content}"),
            content_hash=_sha256(content),
            location={"ordinal": index},
        )
        for index, content in enumerate(contents, start=1)
    )
    provisional = TaskEvidencePack(
        pack_id="pack-bridge-contract",
        revision=1,
        pack_digest=_sha256("canonical persisted evidence pack"),
        delivery_digest="",
        access_scope="private:bridge-test",
        items=items,
    )
    return replace(
        provisional,
        delivery_digest=provisional.computed_delivery_digest(),
    )


def _knowledge_evidence_pack(content: str = "bounded excerpt") -> EvidencePack:
    item = EvidenceItem(
        evidence_id="evidence-knowledge-contract",
        source_type="representation_chunk",
        source_id="chunk-knowledge-contract",
        asset_id="asset-knowledge-contract",
        representation_id="repr-knowledge-contract",
        title="Knowledge contract",
        excerpt=content,
        content_hash=_sha256(f"complete source: {content}"),
        excerpt_hash=_sha256(content),
        source_revision="asset-r1:repr-r1",
        source_created_at="2026-07-24T00:00:00+00:00",
        location={"ordinal": 0},
        confidence=1.0,
    )
    provisional = EvidencePack(
        pack_id="pack-knowledge-contract",
        query="knowledge contract",
        items=(item,),
        selected_bytes=len(content.encode("utf-8")),
        candidate_count=1,
        created_at="2026-07-24T00:00:01+00:00",
        access_scope="private:bridge-test",
        digest="",
    )
    return replace(provisional, digest=provisional.computed_digest())


def _execution_origin(pack: TaskEvidencePack) -> ExecutionOriginBinding:
    return ExecutionOriginBinding(
        binding_id="binding-bridge-contract",
        intent_id="intent-bridge-contract",
        intent_revision=3,
        intent_hash=_sha256("intent revision 3"),
        pack_id=pack.pack_id,
        pack_revision=pack.revision,
        pack_digest=pack.pack_digest,
        delivery_digest=pack.delivery_digest,
        item_count=len(pack.items),
        selected_bytes=pack.selected_bytes,
        access_scope=pack.access_scope,
    )


def _knowledge_outcome(result) -> KnowledgeExecutionOutcome:
    return KnowledgeExecutionOutcome(
        job_id=result.job_id,
        status=result.status.value,
        summary=result.summary,
    )


class _InProcessFoundationWorker:
    """Minimal protocol peer for testing the parent retention boundary only."""

    def __init__(self) -> None:
        self.lock = asyncio.Lock()
        self.frames: asyncio.Queue[FoundationFrame] = asyncio.Queue()
        self.execute_payloads: list[dict[str, object]] = []
        self._active: dict[str, dict[str, object]] = {}

    async def send(self, frame_type: str, run_id: str, payload) -> None:
        value = dict(payload)
        if frame_type == "execute":
            self.execute_payloads.append(value)
            self._active[run_id] = value
            messages = [
                {"role": "system", "content": str(value["system_message"])},
                *list(value.get("conversation_history") or []),
                {"role": "user", "content": str(value["user_message"])},
            ]
            await self.frames.put(
                FoundationFrame(
                    "model_request",
                    run_id,
                    1,
                    {
                        "call_index": 1,
                        "messages": messages,
                        "tools": list(value.get("tools") or []),
                    },
                )
            )
            return
        if frame_type == "provider_response":
            active = self._active[run_id]
            history = [
                *list(active.get("conversation_history") or []),
                {"role": "user", "content": str(active["user_message"])},
                {"role": "assistant", "content": str(value.get("content") or "")},
            ]
            await self.frames.put(
                FoundationFrame(
                    "terminal",
                    run_id,
                    2,
                    {
                        "final_response": str(value.get("content") or ""),
                        "messages": history,
                        "turn_exit_reason": "completed",
                    },
                )
            )
            return
        raise AssertionError(f"Unexpected test worker frame: {frame_type}")

    async def receive(self) -> FoundationFrame:
        return await self.frames.get()

    async def close(self) -> None:
        return None


class _InProcessFoundationRuntime(NoructEmployeeRuntimeService):
    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.test_worker = _InProcessFoundationWorker()

    def _worker_for(self, request):
        key = self._session_key(request)
        self._workers[key] = self.test_worker
        return key, self.test_worker


class EvidenceBridgeContractTests(unittest.TestCase):
    def test_persisted_pack_rejects_excerpt_or_digest_tampering(self) -> None:
        pack = _knowledge_evidence_pack()
        pack.verify()

        with self.assertRaisesRegex(ValueError, "excerpt hash"):
            replace(
                pack,
                items=(replace(pack.items[0], excerpt="tampered excerpt"),),
                selected_bytes=len("tampered excerpt".encode("utf-8")),
                digest=replace(
                    pack,
                    items=(replace(pack.items[0], excerpt="tampered excerpt"),),
                    selected_bytes=len("tampered excerpt".encode("utf-8")),
                    digest="",
                ).computed_digest(),
            ).verify()

        with self.assertRaisesRegex(ValueError, "digest"):
            replace(pack, digest="0" * 64).verify()

    def test_persisted_pack_requires_a_canonical_source_hash(self) -> None:
        pack = _knowledge_evidence_pack()
        malformed = replace(
            pack,
            items=(replace(pack.items[0], content_hash="not-a-sha256"),),
            digest="",
        )
        malformed = replace(malformed, digest=malformed.computed_digest())

        with self.assertRaisesRegex(ValueError, "source hash"):
            malformed.verify()

    def test_runtime_pack_rejects_content_digest_and_source_hash_tampering(self) -> None:
        pack = _task_evidence_pack("bounded runtime excerpt")
        pack.verify()

        oversized_item = replace(
            pack.items[0],
            title="T" * 500_000,
            location={"blob": "L" * 500_000},
        )
        oversized = replace(pack, items=(oversized_item,), delivery_digest="")
        oversized = replace(
            oversized,
            delivery_digest=oversized.computed_delivery_digest(),
        )
        with self.assertRaisesRegex(ValueError, "namespace|metadata|payload"):
            oversized.verify(max_items=1, max_bytes=256)

        changed_content = replace(
            pack,
            items=(replace(pack.items[0], content="tampered runtime excerpt"),),
            delivery_digest="",
        )
        changed_content = replace(
            changed_content,
            delivery_digest=changed_content.computed_delivery_digest(),
        )
        with self.assertRaisesRegex(ValueError, "content hash"):
            changed_content.verify()

        changed_source = replace(
            pack,
            items=(replace(pack.items[0], source_hash="f" * 63),),
            delivery_digest="",
        )
        changed_source = replace(
            changed_source,
            delivery_digest=changed_source.computed_delivery_digest(),
        )
        with self.assertRaisesRegex(ValueError, "source hash"):
            changed_source.verify()

    def test_kernel_projects_only_task_relevant_evidence_as_run_only(self) -> None:
        full_pack = _task_evidence_pack(
            "Complete analysis with alpha audit evidence.",
            "Banana orchard inventory unrelated to the requested work.",
        )
        base = company_request(
            (task("analysis"),),
            final_task_id="analysis",
            roster=(EmployeeRecord("analyst", "Analyst", ("analysis",)),),
        )
        request = replace(
            base,
            context_snapshot=ContextBundle(task_evidence=full_pack),
            execution_origin=_execution_origin(full_pack),
        )
        runtime = ScriptedEmployeeExecutionPort(
            {"analysis": ScriptedOutcome("Analysis complete")}
        )

        result = asyncio.run(FirmKernel(employee_execution=runtime).run(request))

        self.assertEqual(result.status.value, "SUCCEEDED")
        self.assertEqual(len(runtime.requests), 1)
        dispatched = runtime.requests[0]
        self.assertEqual(
            dispatched.session_retention,
            EmployeeSessionRetention.RUN_ONLY,
        )
        self.assertIsNotNone(dispatched.context.task_evidence)
        projected = dispatched.context.task_evidence
        assert projected is not None
        projected.verify()
        self.assertEqual(
            [item.citation_id for item in projected.items],
            ["evidence-1"],
        )
        self.assertEqual(projected.pack_id, full_pack.pack_id)
        self.assertEqual(projected.pack_digest, full_pack.pack_digest)
        self.assertNotEqual(projected.delivery_digest, full_pack.delivery_digest)
        self.assertNotIn("Banana orchard", str(projected.delivery_payload()))

    def test_kernel_rejects_same_size_evidence_with_a_different_delivery_digest(self) -> None:
        original = _task_evidence_pack("signed alpha")
        replacement_item = replace(
            original.items[0],
            content="forged omega",
            content_hash=_sha256("forged omega"),
        )
        replacement = replace(original, items=(replacement_item,), delivery_digest="")
        replacement = replace(
            replacement,
            delivery_digest=replacement.computed_delivery_digest(),
        )
        self.assertEqual(replacement.selected_bytes, original.selected_bytes)
        base = company_request(
            (task("analysis"),),
            final_task_id="analysis",
            roster=(EmployeeRecord("analyst", "Analyst", ("analysis",)),),
        )
        request = replace(
            base,
            context_snapshot=ContextBundle(task_evidence=replacement),
            execution_origin=_execution_origin(original),
        )

        with self.assertRaisesRegex(ValueError, "execution origin"):
            asyncio.run(FirmKernel(employee_execution=ScriptedEmployeeExecutionPort({})).run(request))

    def test_active_job_persists_only_content_free_execution_origin(self) -> None:
        hidden = "PRIVATE-EVIDENCE-BODY-MUST-NOT-ENTER-ACTIVE-JOB"
        full_pack = _task_evidence_pack(f"Complete analysis. {hidden}")
        base = company_request(
            (task("analysis"),),
            final_task_id="analysis",
            roster=(EmployeeRecord("analyst", "Analyst", ("analysis",)),),
        )
        origin = _execution_origin(full_pack)
        request = replace(
            base,
            context_snapshot=ContextBundle(task_evidence=full_pack),
            execution_origin=origin,
        )

        with tempfile.TemporaryDirectory() as directory:
            store = RunStore(Path(directory) / "runtime.db")
            runtime = ScriptedEmployeeExecutionPort(
                {"analysis": ScriptedOutcome("Analysis complete")}
            )
            result = asyncio.run(
                FirmKernel(
                    employee_execution=runtime,
                    active_job_ledger=SQLiteActiveJobLedger(store),
                ).run(request)
            )
            snapshot = json.loads(store.list_job_snapshot_rows(1)[0]["payload_json"])
            persisted = "\n".join(store.active_job_table_payloads(request.job_id))

            self.assertEqual(result.status.value, "SUCCEEDED")
            self.assertEqual(snapshot["knowledge_binding"], {
                "access_scope": origin.access_scope,
                "binding_id": origin.binding_id,
                "decision_context_digest": origin.decision_context_digest,
                "decision_context_id": origin.decision_context_id,
                "intent_hash": origin.intent_hash,
                "intent_id": origin.intent_id,
                "intent_revision": origin.intent_revision,
                "item_count": origin.item_count,
                "pack_digest": origin.pack_digest,
                "delivery_digest": origin.delivery_digest,
                "pack_id": origin.pack_id,
                "pack_revision": origin.pack_revision,
                "oracle_contract_digest": origin.oracle_contract_digest,
                "oracle_contract_id": origin.oracle_contract_id,
                "selected_bytes": origin.selected_bytes,
            })
            self.assertNotIn(hidden, persisted)
            self.assertNotIn("task_evidence", persisted)
            store.close()


class KnowledgeExecutionBindingTests(unittest.TestCase):
    @staticmethod
    def _service(directory: str) -> tuple[KnowledgeStore, UserKnowledgeService, KnowledgeFirmBridge]:
        store = KnowledgeStore(Path(directory) / "knowledge.db")
        service = UserKnowledgeService(
            store,
            KnowledgeVault(Path(directory) / "knowledge.vault"),
        )
        return store, service, KnowledgeFirmBridge(service)

    @staticmethod
    def _company_execution(prepared, *, succeeds: bool = True):
        base = company_request(
            (task("analysis"),),
            final_task_id="analysis",
            roster=(EmployeeRecord("analyst", "Analyst", ("analysis",)),),
        )
        request = replace(
            base,
            request_id=prepared.binding.request_id,
            job_id=prepared.binding.job_id,
            context_snapshot=ContextBundle(task_evidence=prepared.task_evidence),
            execution_origin=prepared.execution_origin,
        )
        outcome = ScriptedOutcome(
            "Verified analysis result" if succeeds else "Analysis could not be completed",
            status=RunStatus.SUCCEEDED if succeeds else RunStatus.FAILED,
        )
        runtime = ScriptedEmployeeExecutionPort({"analysis": outcome})
        return asyncio.run(FirmKernel(employee_execution=runtime).run(request))

    def test_prepare_binds_exact_revision_and_pack_without_content(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store, _service, bridge = self._service(directory)
            store.create_record(
                kind="FACT",
                statement="Analysis requires the signed alpha audit evidence.",
            )
            intent = store.create_intent(
                goal="Complete the analysis",
                knowledge_query="analysis alpha audit",
            )

            prepared = bridge.prepare(
                intent.intent_id,
                request_id="request-knowledge-bridge-prepare",
                job_id="job-knowledge-bridge-prepare",
                access_scope="private",
            )

            self.assertEqual(prepared.binding.status, "PREPARED")
            self.assertEqual(prepared.binding.job_status, "")
            self.assertIsNone(prepared.binding.candidate_id)
            self.assertEqual(prepared.binding.request_id, "request-knowledge-bridge-prepare")
            self.assertEqual(prepared.binding.job_id, "job-knowledge-bridge-prepare")
            self.assertEqual(prepared.execution_origin.binding_id, prepared.binding.binding_id)
            self.assertEqual(prepared.execution_origin.intent_hash, prepared.binding.intent_hash)
            self.assertEqual(prepared.execution_origin.pack_digest, prepared.evidence_pack.digest)
            self.assertEqual(prepared.task_evidence.pack_digest, prepared.evidence_pack.digest)
            self.assertEqual(
                prepared.task_evidence.delivery_digest,
                prepared.binding.delivery_digest,
            )
            prepared.evidence_pack.verify()
            prepared.task_evidence.verify()
            self.assertEqual(len(prepared.evidence_pack.items), 1)
            self.assertEqual(len(prepared.task_evidence.items), 1)
            source = prepared.evidence_pack.items[0]
            delivery = prepared.task_evidence.items[0]
            self.assertEqual(delivery.citation_id, source.evidence_id)
            self.assertEqual(delivery.source_id, source.source_id)
            self.assertEqual(delivery.source_revision, source.source_revision)
            self.assertEqual(delivery.source_hash, source.content_hash)
            self.assertEqual(delivery.content_hash, source.excerpt_hash)
            self.assertEqual(delivery.content, source.excerpt)

            # Repeating the exact content-free store operation is idempotent;
            # changing even the delivery identity under either caller-owned id is not.
            repeated = store.prepare_execution_binding(
                request_id=prepared.binding.request_id,
                job_id=prepared.binding.job_id,
                intent_id=prepared.binding.intent_id,
                intent_revision=prepared.binding.intent_revision,
                intent_hash=prepared.binding.intent_hash,
                pack_id=prepared.binding.pack_id,
                pack_revision=prepared.binding.pack_revision,
                pack_digest=prepared.binding.pack_digest,
                delivery_digest=prepared.binding.delivery_digest,
                item_count=prepared.binding.item_count,
                selected_bytes=prepared.binding.selected_bytes,
                access_scope=prepared.binding.access_scope,
            )
            self.assertEqual(repeated, prepared.binding)
            with self.assertRaisesRegex(ValueError, "different knowledge"):
                store.prepare_execution_binding(
                    request_id=prepared.binding.request_id,
                    job_id=prepared.binding.job_id,
                    intent_id=prepared.binding.intent_id,
                    intent_revision=prepared.binding.intent_revision,
                    intent_hash=prepared.binding.intent_hash,
                    pack_id=prepared.binding.pack_id,
                    pack_revision=prepared.binding.pack_revision,
                    pack_digest=prepared.binding.pack_digest,
                    delivery_digest="0" * 64,
                    item_count=prepared.binding.item_count,
                    selected_bytes=prepared.binding.selected_bytes,
                    access_scope=prepared.binding.access_scope,
                )
            store.close()

    def test_prepare_rejects_current_intent_content_that_no_longer_matches_revision(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store, _service, bridge = self._service(directory)
            intent = store.create_intent(goal="ORIGINAL signed goal")
            connection = sqlite3.connect(store.path)
            try:
                connection.execute(
                    "UPDATE knowledge_intents SET goal = ? WHERE intent_id = ?",
                    ("TAMPERED unsigned goal", intent.intent_id),
                )
                connection.commit()
            finally:
                connection.close()

            with self.assertRaisesRegex(ValueError, "immutable revision"):
                bridge.prepare(
                    intent.intent_id,
                    request_id="request-tampered-intent",
                    job_id="job-tampered-intent",
                )
            self.assertEqual(store.counts()["evidence_packs"], 0)
            self.assertEqual(store.counts()["knowledge_execution_bindings"], 0)
            store.close()

    def test_prepare_redacts_credentials_before_signing_the_runtime_delivery(self) -> None:
        secret = "sk-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
        with tempfile.TemporaryDirectory() as directory:
            store, _service, bridge = self._service(directory)
            store.create_record(
                kind="NOTE",
                statement=f"Credential evidence {secret} must never cross the prompt boundary.",
            )
            intent = store.create_intent(
                goal="Review credential evidence",
                knowledge_query="credential evidence",
            )
            prepared = bridge.prepare(
                intent.intent_id,
                request_id="request-redacted-evidence",
                job_id="job-redacted-evidence",
            )

            self.assertIn(secret, prepared.evidence_pack.items[0].excerpt)
            self.assertNotIn(secret, prepared.task_evidence.items[0].content)
            self.assertEqual(
                prepared.binding.delivery_digest,
                prepared.task_evidence.delivery_digest,
            )
            employee_request = replace(
                make_request(),
                context=ContextBundle(task_evidence=prepared.task_evidence),
                session_retention=EmployeeSessionRetention.RUN_ONLY,
            )
            snapshot = PromptBuilder().build(employee_request)
            self.assertNotIn(secret, snapshot.user_message)
            self.assertIn(prepared.task_evidence.delivery_digest, snapshot.user_message)
            self.assertEqual(
                snapshot.knowledge_projection["evidence_pack"]["delivery_digest"],
                prepared.task_evidence.delivery_digest,
            )
            store.close()

    def test_success_terminalizes_once_and_creates_only_a_pending_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store, _service, bridge = self._service(directory)
            source_record = store.create_record(
                kind="FACT",
                statement="Analysis is supported by alpha audit evidence.",
            )
            intent = store.create_intent(
                goal="Complete the analysis",
                knowledge_query="analysis alpha audit",
            )
            prepared = bridge.prepare(
                intent.intent_id,
                request_id="request-knowledge-bridge-success",
                job_id="job-knowledge-bridge-success",
            )
            result = self._company_execution(prepared)

            outcome = _knowledge_outcome(result)
            completed = bridge.complete(prepared, outcome)
            repeated = bridge.complete(prepared, outcome)

            self.assertEqual(completed, repeated)
            self.assertEqual(completed.binding.status, "TERMINAL")
            self.assertEqual(completed.binding.job_status, "SUCCEEDED")
            self.assertIsNotNone(completed.candidate)
            assert completed.candidate is not None
            self.assertEqual(completed.binding.candidate_id, completed.candidate.candidate_id)
            self.assertEqual(completed.candidate.status, "PENDING")
            self.assertIsNone(completed.candidate.accepted_record_id)
            self.assertEqual(completed.candidate.statement, result.summary)
            self.assertEqual(completed.candidate.evidence_pack_id, prepared.evidence_pack.pack_id)
            self.assertEqual(
                store.execution_binding_for_job(result.job_id),
                completed.binding,
            )
            self.assertEqual(
                store.list_execution_bindings(status="TERMINAL"),
                (completed.binding,),
            )
            self.assertEqual(
                [candidate.candidate_id for candidate in store.list_write_candidates()],
                [completed.candidate.candidate_id],
            )
            # The bridge stages a reviewable candidate; it must not silently
            # promote the result into authoritative User Knowledge.
            self.assertEqual(
                [record.record_id for record in store.list_records()],
                [source_record.record_id],
            )

            with self.assertRaisesRegex(ValueError, "does not match"):
                bridge.complete(
                    prepared,
                    replace(outcome, job_id="job-some-other-execution"),
                )
            with self.assertRaisesRegex(TypeError, "full Firm Job or graph state"):
                bridge.complete(prepared, result)  # type: ignore[arg-type]
            store.close()

    def test_prepared_execution_leases_its_sources_until_terminal(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store, _service, bridge = self._service(directory)
            source = store.create_record(
                kind="FACT",
                statement="Leased alpha evidence for the active analysis.",
            )
            intent = store.create_intent(
                goal="Complete the analysis",
                knowledge_query="leased alpha evidence",
            )
            prepared = bridge.prepare(
                intent.intent_id,
                request_id="request-knowledge-lease",
                job_id="job-knowledge-lease",
            )

            with self.assertRaisesRegex(ValueError, "leased by an active Intent Job"):
                store.forget_record(source.record_id)

            bridge.interrupt(prepared)
            self.assertTrue(store.forget_record(source.record_id))
            store.close()

    def test_failed_and_interrupted_executions_never_create_candidates(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store, _service, bridge = self._service(directory)
            store.create_record(
                kind="FACT",
                statement="Analysis has bounded supporting evidence.",
            )
            failed_intent = store.create_intent(
                goal="Complete failed analysis",
                knowledge_query="analysis evidence",
            )
            failed = bridge.prepare(
                failed_intent.intent_id,
                request_id="request-knowledge-bridge-failed",
                job_id="job-knowledge-bridge-failed",
            )
            failed_result = self._company_execution(failed, succeeds=False)
            failed_completion = bridge.complete(failed, _knowledge_outcome(failed_result))

            self.assertEqual(failed_completion.binding.status, "TERMINAL")
            self.assertEqual(failed_completion.binding.job_status, "FAILED")
            self.assertIsNone(failed_completion.binding.candidate_id)
            self.assertIsNone(failed_completion.candidate)

            interrupted_intent = store.create_intent(
                goal="Complete interrupted analysis",
                knowledge_query="analysis evidence",
            )
            interrupted = bridge.prepare(
                interrupted_intent.intent_id,
                request_id="request-knowledge-bridge-interrupted",
                job_id="job-knowledge-bridge-interrupted",
            )
            first = bridge.interrupt(interrupted)
            second = bridge.interrupt(interrupted)

            self.assertEqual(first, second)
            self.assertEqual(first.status, "TERMINAL")
            self.assertEqual(first.job_status, "INTERRUPTED")
            self.assertIsNone(first.candidate_id)
            self.assertEqual(store.list_write_candidates(), ())
            store.close()


class NativeRunOnlyRetentionTests(unittest.IsolatedAsyncioTestCase):
    async def test_evidence_is_delivered_but_not_persisted_or_committed_to_session(self) -> None:
        hidden = "NATIVE-RUN-ONLY-EVIDENCE-SENTINEL"
        store = RunStore()
        provider = ScriptedModelProvider(
            [
                ModelResponse(completion=completion("Persistent baseline turn")),
                ModelResponse(completion=completion("Run-only evidence answer")),
            ]
        )
        service = NativeEmployeeRuntimeService(
            store=store,
            provider=provider,
            registry=ToolRegistry(),
        )
        session_key = "bridge-native-session"
        baseline = replace(
            make_request(request_id="bridge-native-baseline"),
            session_key=session_key,
        )
        baseline_result = await service.collect(await service.start(baseline))
        namespace = employee_session_namespace(
            baseline.employee.employee_id,
            session_key,
        )
        before = store.load_employee_session(namespace, baseline.employee.employee_id)
        self.assertIsNotNone(before)

        evidence_request = replace(
            baseline,
            request_id="bridge-native-run-only",
            context=replace(
                baseline.context,
                task_evidence=_task_evidence_pack(hidden),
            ),
            session_retention=EmployeeSessionRetention.RUN_ONLY,
        )
        result = await service.collect(await service.start(evidence_request))
        after = store.load_employee_session(namespace, baseline.employee.employee_id)
        persisted_run = json.dumps(
            {
                "run": store.get_run(result.run_id),
                "messages": [message.content for message in store.list_messages(result.run_id)],
            },
            ensure_ascii=False,
            sort_keys=True,
            default=str,
        )

        self.assertEqual(baseline_result.status.value, "SUCCEEDED")
        self.assertEqual(result.status.value, "SUCCEEDED")
        self.assertEqual(before, after)
        self.assertIn(hidden, str(provider.requests[1].messages))
        self.assertNotIn(hidden, persisted_run)
        assert after is not None
        self.assertNotIn(hidden, str(after.messages))
        self.assertNotIn("Run-only evidence answer", str(after.messages))
        await service.close()
        store.close()


class FoundationRunOnlyRetentionTests(unittest.IsolatedAsyncioTestCase):
    async def test_evidence_is_delivered_but_not_committed_to_foundation_session(self) -> None:
        hidden = "FOUNDATION-RUN-ONLY-EVIDENCE-SENTINEL"
        store = RunStore()
        provider = ScriptedModelProvider(
            [
                ModelResponse(completion=completion("Foundation baseline turn")),
                ModelResponse(completion=completion("Foundation run-only answer")),
            ]
        )
        service = _InProcessFoundationRuntime(
            store=store,
            provider=provider,
            registry=ToolRegistry(),
        )
        session_key = "bridge-foundation-session"
        baseline = replace(
            make_request(request_id="bridge-foundation-baseline"),
            session_key=session_key,
        )
        baseline_result = await service.collect(await service.start(baseline))
        namespace = employee_session_namespace(
            baseline.employee.employee_id,
            session_key,
        )
        before = store.load_employee_session(namespace, baseline.employee.employee_id)
        self.assertIsNotNone(before)

        evidence_request = replace(
            baseline,
            request_id="bridge-foundation-run-only",
            context=replace(
                baseline.context,
                task_evidence=_task_evidence_pack(hidden),
            ),
            session_retention=EmployeeSessionRetention.RUN_ONLY,
        )
        result = await service.collect(await service.start(evidence_request))
        after = store.load_employee_session(namespace, baseline.employee.employee_id)
        persisted_run = json.dumps(
            {
                "run": store.get_run(result.run_id),
                "messages": [message.content for message in store.list_messages(result.run_id)],
            },
            ensure_ascii=False,
            sort_keys=True,
            default=str,
        )

        self.assertEqual(baseline_result.status.value, "SUCCEEDED")
        self.assertEqual(result.status.value, "SUCCEEDED")
        self.assertEqual(before, after)
        self.assertIn(hidden, str(provider.requests[1].messages))
        self.assertEqual(
            service.test_worker.execute_payloads[1]["conversation_history"],
            [],
        )
        self.assertIn(
            hidden,
            str(service.test_worker.execute_payloads[1]["user_message"]),
        )
        self.assertNotIn(hidden, persisted_run)
        assert after is not None
        self.assertNotIn(hidden, str(after.messages))
        self.assertNotIn("Foundation run-only answer", str(after.messages))
        await service.close()
        store.close()


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
