from __future__ import annotations

import tempfile
import unittest
import sqlite3
from pathlib import Path

from dynamic_firm.product.sessions import CompanySessionStore, browse_company_sessions
from dynamic_firm.runtime.models import Usage


class CompanySessionStoreTests(unittest.TestCase):
    def test_provider_binding_is_secret_free_and_survives_store_restart(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary) / "workspace"
            workspace.mkdir()
            database = Path(temporary) / "runtime.db"
            store = CompanySessionStore(database)
            try:
                created = store.create(
                    workspace=workspace,
                    model="bound-model",
                    provider_kind="anthropic_api",
                    provider_base_url="https://api.example.invalid/v1",
                    provider_api_key_env="SESSION_PROVIDER_KEY",
                )
            finally:
                store.close()
            reopened = CompanySessionStore(database)
            try:
                resumed = reopened.resolve(created.session_id)
                with self.assertRaisesRegex(ValueError, "unsafe base URL"):
                    reopened.create(
                        workspace=workspace,
                        model="unsafe",
                        provider_kind="openai_api",
                        provider_base_url="https://token@example.invalid/v1",
                    )
            finally:
                reopened.close()

        self.assertIsNotNone(resumed)
        assert resumed is not None
        self.assertTrue(resumed.has_provider_binding)
        self.assertEqual(resumed.provider_kind, "anthropic_api")
        self.assertEqual(resumed.provider_base_url, "https://api.example.invalid/v1")
        self.assertEqual(resumed.provider_api_key_env, "SESSION_PROVIDER_KEY")

    def test_legacy_session_schema_migrates_without_claiming_provider_identity(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database = Path(temporary) / "runtime.db"
            workspace = Path(temporary) / "workspace"
            workspace.mkdir()
            connection = sqlite3.connect(database)
            try:
                connection.execute(
                    """
                    CREATE TABLE company_sessions (
                        session_id TEXT PRIMARY KEY, title TEXT NOT NULL, workspace TEXT NOT NULL,
                        model TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                    )
                    """
                )
                connection.execute(
                    "INSERT INTO company_sessions VALUES (?, ?, ?, ?, ?, ?)",
                    ("legacy-session", "Legacy", str(workspace), "legacy-model", "now", "now"),
                )
                connection.commit()
            finally:
                connection.close()
            store = CompanySessionStore(database)
            try:
                session = store.resolve("legacy-session")
            finally:
                store.close()

        self.assertIsNotNone(session)
        assert session is not None
        self.assertFalse(session.has_provider_binding)
        self.assertEqual(session.provider_kind, "")

    def test_session_turns_are_resumable_and_context_is_bounded(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database = Path(temporary) / "runtime.db"
            workspace = Path(temporary) / "workspace"
            workspace.mkdir()
            store = CompanySessionStore(database)
            try:
                session = store.create(workspace=workspace, model="contract-model")
                first = store.append_turn(
                    session_id=session.session_id,
                    goal="Inspect the repository",
                    job_id="job-1",
                    status="SUCCEEDED",
                    summary="Found the relevant module.",
                    usage=Usage(model_calls=2),
                )
                second = store.append_turn(
                    session_id=session.session_id,
                    goal="Now inspect its tests",
                    job_id="job-2",
                    status="SUCCEEDED",
                    summary="The tests cover the main path.",
                    usage=Usage(model_calls=3),
                )

                resumed = store.resolve(session.session_id[:10])
                listed = store.list(limit=5)
                context = store.recent_context(session.session_id, max_turns=1, max_bytes=500)
                usage = store.usage(session.session_id)
            finally:
                store.close()

        self.assertEqual(first.position, 1)
        self.assertEqual(second.position, 2)
        self.assertIsNotNone(resumed)
        assert resumed is not None
        self.assertEqual(resumed.turn_count, 2)
        self.assertEqual(resumed.title, "Inspect the repository")
        self.assertEqual(listed[0].session_id, session.session_id)
        self.assertEqual(len(context), 1)
        self.assertIn("Now inspect its tests", context[0])
        self.assertNotIn("Found the relevant module", context[0])
        self.assertEqual(usage.model_calls, 5)

    def test_unknown_session_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = CompanySessionStore(Path(temporary) / "runtime.db")
            try:
                with self.assertRaises(KeyError):
                    store.append_turn(
                        session_id="missing",
                        goal="goal",
                        job_id="job",
                        status="FAILED",
                        summary="summary",
                        usage=Usage(),
                    )
            finally:
                store.close()

    def test_session_model_can_be_switched_without_rewriting_turns(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary) / "workspace"
            workspace.mkdir()
            store = CompanySessionStore(Path(temporary) / "runtime.db")
            try:
                session = store.create(workspace=workspace, model="model-before")
                updated = store.update_model(session.session_id, "model-after")
                resolved = store.resolve(session.session_id)
            finally:
                store.close()

        self.assertEqual(updated.model, "model-after")
        self.assertIsNotNone(resolved)
        assert resolved is not None
        self.assertEqual(resolved.model, "model-after")
        self.assertEqual(resolved.turn_count, 0)

    def test_session_cost_mode_is_explicit_and_persists(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary) / "workspace"
            workspace.mkdir()
            store = CompanySessionStore(Path(temporary) / "runtime.db")
            try:
                session = store.create(
                    workspace=workspace,
                    model="model",
                    cost_efficiency_mode="economy",
                )
                updated = store.update_cost_efficiency_mode(session.session_id, "standard")
                resolved = store.resolve(session.session_id)
            finally:
                store.close()

        self.assertEqual(session.cost_efficiency_mode, "economy")
        self.assertEqual(updated.cost_efficiency_mode, "standard")
        assert resolved is not None
        self.assertEqual(resolved.cost_efficiency_mode, "standard")

    def test_input_history_is_session_scoped_complete_and_bounded(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary) / "workspace"
            workspace.mkdir()
            store = CompanySessionStore(Path(temporary) / "runtime.db")
            try:
                primary = store.create(workspace=workspace, model="model")
                other = store.create(workspace=workspace, model="model")
                for position, goal in enumerate(
                    ("First complete goal", "Repeat goal", "Repeat goal", "Last complete goal"),
                    1,
                ):
                    store.append_turn(
                        session_id=primary.session_id,
                        goal=goal,
                        job_id=f"job-{position}",
                        status="FAILED" if position == 2 else "SUCCEEDED",
                        summary="Local result",
                        usage=Usage(),
                    )
                store.append_turn(
                    session_id=primary.session_id,
                    goal="x" * 8_001,
                    job_id="job-oversized",
                    status="SUCCEEDED",
                    summary="Local result",
                    usage=Usage(),
                )
                store.append_turn(
                    session_id=other.session_id,
                    goal="Other company goal",
                    job_id="job-other",
                    status="SUCCEEDED",
                    summary="Local result",
                    usage=Usage(),
                )
                history = store.input_history(primary.session_id)
                with self.assertRaisesRegex(ValueError, "between 1 and 200"):
                    store.input_history(primary.session_id, limit=0)
            finally:
                store.close()

        self.assertEqual(
            history,
            ("First complete goal", "Repeat goal", "Last complete goal"),
        )
        self.assertNotIn("Other company goal", history)

    def test_session_browse_reuses_policy_without_exposing_unnamed_or_current(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary) / "workspace"
            workspace.mkdir()
            store = CompanySessionStore(Path(temporary) / "runtime.db")
            try:
                current = store.create(workspace=workspace, model="current-model", title="Current")
                target = store.create(workspace=workspace, model="target-model", title="Review alpha")
                store.append_turn(
                    session_id=target.session_id,
                    goal="Review the change",
                    job_id="job-target",
                    status="SUCCEEDED",
                    summary="The target session completed safely.",
                    usage=Usage(model_calls=1),
                )
                unnamed = store.create(workspace=workspace, model="new-model")

                listed = browse_company_sessions(
                    store,
                    "browse",
                    current_session_id=current.session_id,
                )
                searched = browse_company_sessions(
                    store,
                    "search alpha",
                    current_session_id=current.session_id,
                )
                recalled = browse_company_sessions(
                    store,
                    "search completed safely",
                    current_session_id=current.session_id,
                )
                full = browse_company_sessions(
                    store,
                    "full",
                    current_session_id=current.session_id,
                )
                target_intent = browse_company_sessions(
                    store,
                    '"Review alpha"',
                    current_session_id=current.session_id,
                )
            finally:
                store.close()

        self.assertEqual([item.session_id for item in listed.items], [target.session_id])
        self.assertEqual(listed.items[0].model, "target-model")
        self.assertIn("completed safely", listed.items[0].preview)
        self.assertEqual([item.session_id for item in searched.items], [target.session_id])
        self.assertEqual([item.session_id for item in recalled.items], [target.session_id])
        self.assertIn(unnamed.session_id, [item.session_id for item in full.items])
        self.assertEqual(target_intent.target, "Review alpha")
        self.assertEqual(target_intent.items, ())

    def test_advanced_session_search_branch_and_rewind_keep_firm_turns_immutable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary) / "workspace"
            workspace.mkdir()
            store = CompanySessionStore(Path(temporary) / "runtime.db")
            try:
                session = store.create(workspace=workspace, model="test")
                store.append_message(session_id=session.session_id, role="user", content="price strategy")
                store.append_message(session_id=session.session_id, role="assistant", content="review the evidence")
                hits = store.search_messages("strategy", session_id=session.session_id)
                branch = store.branch(session.session_id, title="Strategy branch")
                removed = store.rewind_messages(session.session_id, hits[0].message_id)
                original_messages = store.conversation(session.session_id)
                branch_messages = store.conversation(branch.session_id)
            finally:
                store.close()
        self.assertEqual(len(hits), 1)
        self.assertEqual(len(branch_messages), 2)
        self.assertEqual(removed, 1)
        self.assertEqual(len(original_messages), 1)


if __name__ == "__main__":
    unittest.main()
