"""Provider-free operator qualification run on GitHub's clean Windows worker.

The assertions are intentionally portable so contributors can run them on any
platform. CI invokes this module on Windows after a clean wheel install; this
test is the runtime scenario rather than a claim that template syntax alone
qualifies Windows support.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
import tempfile
import unittest

from dynamic_firm.cli import _ModernInteractiveController, _load_config, build_parser


PROJECT_ROOT = Path(__file__).parents[2]
FIXTURE_ROOT = PROJECT_ROOT / "tests" / "fixtures" / "tiny_repo"


class WindowsOperatorQualificationTests(unittest.TestCase):
    def _args(self, *, config: Path, state: Path, command: str, session: str | None = None):
        values = [
            "--config", str(config), command,
        ]
        if command == "resume" and session is not None:
            values.append(session)
        values.extend(["--state", str(state)])
        if command != "resume":
            values.extend(
                [
                    "--workspace", str(FIXTURE_ROOT), "--provider", "openai-api",
                    "--base-url", "http://127.0.0.1:9/v1", "--model",
                    "windows-qualification-model", "--no-auth", "--permission-mode",
                    "read-only",
                ]
            )
        return build_parser().parse_args(values)

    def test_manager_knowledge_delegation_tool_approval_and_session_resume_are_local(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config_path = root / "config.toml"
            state_path = root / "runtime.db"
            controller = _ModernInteractiveController(
                self._args(config=config_path, state=state_path, command="chat"),
                {},
                provider_factory=lambda _config: self.fail("qualification must not build a provider"),
                coding_worker_factory=lambda _config: self.fail("qualification must not build a coding worker"),
            )
            try:
                session_id = controller.session.session_id
                snapshot = controller.snapshot()
                self.assertTrue(snapshot.operator_snapshot["manager"]["employee_id"])
                self.assertIn("company.manager.model_profile", {
                    item["key"] for item in snapshot.settings_entries
                })

                remembered = asyncio.run(
                    controller.execute_command("/remember Windows qualification evidence")
                )
                knowledge = asyncio.run(controller.execute_command("/knowledge qualification"))
                permission = asyncio.run(controller.execute_command("/permission ask"))
                tools = asyncio.run(controller.execute_command("/tools"))
                graph_saved = controller.apply_graph_control(
                    {
                        "blueprint_id": None,
                        "version": None,
                        "pinned_employee_ids": (),
                        "excluded_employee_ids": (),
                        "require_independent_review": True,
                        "max_concurrency": 1,
                        "max_cost_usd": 0.5,
                        "max_wall_time_ms": 10_000,
                        "mutation_policy": "PROPOSE",
                    }
                )
                self.assertIn("Remembered locally", "\n".join(remembered.messages))
                self.assertIn("Windows qualification evidence", "\n".join(knowledge.messages))
                self.assertIn("read-only → ask", "\n".join(permission.messages))
                self.assertIn("approve", "\n".join(tools.messages).lower())
                self.assertIn("Future Job Graph defaults saved", graph_saved[0])
            finally:
                controller.close()

            resumed = _ModernInteractiveController(
                self._args(config=config_path, state=state_path, command="resume", session=session_id),
                _load_config(config_path),
                provider_factory=lambda _config: self.fail("resume must not build a provider"),
                coding_worker_factory=lambda _config: self.fail("resume must not build a coding worker"),
            )
            try:
                self.assertEqual(resumed.session.session_id, session_id)
                self.assertEqual(resumed.config.permission_mode, "ask")
                restored_knowledge = asyncio.run(
                    resumed.execute_command("/knowledge qualification")
                )
                self.assertIn("Windows qualification evidence", "\n".join(restored_knowledge.messages))
                graph = resumed.graph_control_snapshot()["selection"]
                self.assertEqual(graph["mutation_policy"], "PROPOSE")
                self.assertEqual(graph["max_concurrency"], 1)
            finally:
                resumed.close()


if __name__ == "__main__":
    unittest.main()
