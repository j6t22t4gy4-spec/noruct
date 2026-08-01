from __future__ import annotations

import asyncio
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from dynamic_firm.cli import RunCommandConfig, _action_policy
from dynamic_firm.runtime.models import RunLimits
from dynamic_firm.runtime.knowledge_tools import KnowledgeRuntimeTools
from dynamic_firm.runtime.ports import CancellationToken
from dynamic_firm.knowledge import (
    KnowledgeFolderService,
    KnowledgeStore,
    KnowledgeVault,
    knowledge_runtime_paths,
)


class KnowledgeRuntimeToolTests(unittest.TestCase):
    def test_action_policy_exposes_reads_and_approval_gated_writes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = RunCommandConfig(
                goal="Use the local knowledge base.",
                workspace=root,
                state_path=root / "runtime.db",
                provider_kind="openai_codex",
                base_url="",
                model="codex-default",
                codex_model=None,
                codex_command="codex",
                api_key_env=None,
                request_timeout_seconds=10,
                permission_mode="ask",
                run_limits=RunLimits(),
            )
            grants = {
                item.tool_name: item for item in _action_policy(config).tool_grants
            }

        self.assertIn("knowledge_recall", grants)
        self.assertIn("knowledge_folder_open", grants)
        self.assertTrue(grants["knowledge_remember"].requires_approval)
        self.assertTrue(grants["knowledge_ingest"].requires_approval)
        self.assertIn("knowledge_remember", _action_policy(config).auto_approved_tool_names)
        self.assertIn("knowledge_ingest", _action_policy(config).auto_approved_tool_names)
        self.assertEqual(
            _action_policy(replace(config, capability_trust_mode="strict")).auto_approved_tool_names,
            (),
        )
        read_only = {
            item.tool_name
            for item in _action_policy(
                replace(config, permission_mode="read-only")
            ).tool_grants
        }
        self.assertIn("knowledge_folder_open", read_only)
        self.assertNotIn("knowledge_remember", read_only)
        self.assertNotIn("knowledge_ingest", read_only)

    def test_employee_tools_use_the_canonical_operator_database_and_vault_paths(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runtime_state = root / "runtime.db"
            expected_database, expected_vault = knowledge_runtime_paths(runtime_state)
            store, service = KnowledgeRuntimeTools(
                state_path=runtime_state,
                workspace=root,
            )._service()
            try:
                self.assertEqual(store.path, expected_database)
                self.assertEqual(service.vault.root, expected_vault)
            finally:
                store.close()

    def test_recall_and_write_tools_use_bounded_contracts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            tools = {item.name: item for item in KnowledgeRuntimeTools(state_path=root / "runtime.db", workspace=root).definitions()}
            self.assertEqual(
                set(tools),
                {
                    "knowledge_recall",
                    "knowledge_folder_open",
                    "knowledge_remember",
                    "knowledge_ingest",
                },
            )
            self.assertEqual(tools["knowledge_recall"].effect.value, "READ")
            self.assertTrue(tools["knowledge_remember"].requires_approval)
            self.assertTrue(tools["knowledge_ingest"].requires_approval)
            tools["knowledge_recall"].validator({"query": "pricing"})
            with self.assertRaises(Exception):
                tools["knowledge_ingest"].validator({"path": "../outside.pdf"})

    def test_remember_is_persisted_only_through_tool_handler(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            definition = next(item for item in KnowledgeRuntimeTools(state_path=root / "runtime.db", workspace=root).definitions() if item.name == "knowledge_remember")
            arguments = definition.validator({"statement": "The price review is due in August."})
            result = asyncio.run(definition.handler(arguments, CancellationToken()))
            self.assertIn("price review", result)

    def test_folder_open_tool_uses_indexed_entry_and_freezes_exact_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runtime_state = root / "runtime.db"
            raw = root / "knowledge"
            raw.mkdir()
            (raw / "strategy.md").write_text(
                "The pricing strategy review is due in August.",
                encoding="utf-8",
            )
            database, vault_path = knowledge_runtime_paths(runtime_state)
            with KnowledgeStore(database) as store:
                folders = KnowledgeFolderService(store, KnowledgeVault(vault_path))
                folder, _ = folders.register(raw)
                folders.scan(folder.folder_id)
                entry = store.list_knowledge_folder_entries(folder.folder_id)[0]

            definition = next(
                item
                for item in KnowledgeRuntimeTools(
                    state_path=runtime_state,
                    workspace=root,
                ).definitions()
                if item.name == "knowledge_folder_open"
            )
            arguments = definition.validator({"entry_id": entry.entry_id})
            result = asyncio.run(definition.handler(arguments, CancellationToken()))

            self.assertIn("pricing strategy", result)
            with KnowledgeStore(database) as store:
                current = store.folder_entry(entry.entry_id)
                assert current is not None
                self.assertIsNotNone(current.snapshot_asset_id)


if __name__ == "__main__":
    unittest.main()
