from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from dynamic_firm.application.goal_runtime_assembly import assemble_goal_tool_registry


class GoalRuntimeAssemblyTests(unittest.TestCase):
    def _assemble(self, root: Path, **overrides: object):
        arguments: dict[str, object] = {
            "state_path": root / "runtime.sqlite3",
            "workspace": root,
            "config_path": root / "noruct.toml",
            "goal": "Inspect the local fixture.",
            "external_skill_dirs": (),
            "permission_mode": "read-only",
            "capability_lane": False,
            "session_key": "",
            "manager_assignment": None,
            "company_store": None,
            "run_store": None,
            "job_id": "job-fixture",
            "remote_worker": None,
            "container_workspace": None,
            "executable_plugins": None,
            "home_assistant": None,
            "workspace_id": "fixture-workspace",
        }
        arguments.update(overrides)
        return assemble_goal_tool_registry(**arguments)  # type: ignore[arg-type]

    def test_default_composition_only_exposes_bounded_workspace_reads(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            registry, session_store = self._assemble(Path(temporary))

        self.assertIsNone(session_store)
        for name in (
            "list_workspace_files",
            "read_external_skill_support",
            "read_workspace_file",
            "search_workspace_files",
        ):
            self.assertIsNotNone(registry.get(name))
        self.assertIsNone(
            registry.get("write_workspace_file"),
            "read-only composition must not expose a mutation tool",
        )

    def test_ask_mode_and_session_key_add_only_their_composed_tools(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            registry, session_store = self._assemble(
                Path(temporary),
                permission_mode="ask",
                session_key="session-current",
            )
            try:
                self.assertIsNotNone(session_store)
                self.assertIsNotNone(registry.get("write_workspace_file"))
                self.assertIsNotNone(registry.get("search_company_session_memory"))
                self.assertIsNotNone(registry.get("read_company_session_memory"))
            finally:
                assert session_store is not None
                session_store.close()


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
