from __future__ import annotations

import argparse
import io
import json
import tempfile
import unittest
from pathlib import Path

from dynamic_firm.application.graph_cli import GraphCommunityPorts, run_graph_community_command
from dynamic_firm.application.graph_community_cli_parser import add_community_graph_commands
from dynamic_firm.company import CommunityBlueprintRegistry, GraphBlueprintControlService
from dynamic_firm.company.graph_blueprint_models import GraphBlueprint, GraphBlueprintTask
from dynamic_firm.company.graph_blueprint_registry import GraphBlueprintRegistry


def _blueprint() -> GraphBlueprint:
    return GraphBlueprint(
        blueprint_id="release_review",
        version=1,
        objective_class="general",
        execution_profiles=("read_only",),
        parameters=("objective",),
        tasks=(
            GraphBlueprintTask(
                task_id="inspect",
                objective_template="Inspect {{objective}}.",
                depends_on=(),
                required_capabilities=("analysis",),
                acceptance_templates=("Record bounded evidence.",),
            ),
            GraphBlueprintTask(
                task_id="final",
                objective_template="Summarize {{objective}}.",
                depends_on=("inspect",),
                required_capabilities=("analysis",),
                acceptance_templates=("Return a concise result.",),
            ),
        ),
        final_task_id="final",
    )


class GraphCommunityCliAdapterTests(unittest.TestCase):
    def test_parser_component_exposes_confirmed_community_lifecycle_schema(self) -> None:
        parser = argparse.ArgumentParser()
        commands = parser.add_subparsers(dest="graph_command", required=True)
        add_community_graph_commands(commands)

        parsed = parser.parse_args(
            [
                "community-prepare",
                "release_review",
                "1",
                "release_share",
                "release_review_artifact",
                "--state",
                "company.sqlite3",
                "--confirm",
                "--json",
            ]
        )

        self.assertEqual(parsed.graph_command, "community-prepare")
        self.assertEqual(parsed.version, 1)
        self.assertEqual(parsed.state, Path("company.sqlite3"))
        self.assertTrue(parsed.confirm)
        self.assertTrue(parsed.json)

    def setUp(self) -> None:
        self._directory = tempfile.TemporaryDirectory()
        self.control = GraphBlueprintControlService(GraphBlueprintRegistry())
        self.control.save(_blueprint())
        self.community = CommunityBlueprintRegistry(
            Path(self._directory.name) / "community.sqlite3"
        )
        self.rendered: list[object] = []
        self.output = io.StringIO()
        self.ports = GraphCommunityPorts(
            evolution_state_path=lambda: Path("unused-evolution.sqlite3"),
            render=self._render,
        )

    def tearDown(self) -> None:
        self.community.close()
        self._directory.cleanup()

    def _render(self, payload: object, _as_json: bool, _output: io.StringIO) -> int:
        self.rendered.append(payload)
        return 0

    def _run(self, command: str, **values: object) -> int | None:
        args = argparse.Namespace(graph_command=command, json=True, **values)
        return run_graph_community_command(
            args,
            control=self.control,
            community_registry=self.community,
            ports=self.ports,
            output=self.output,
        )

    def test_mutations_fail_closed_until_confirmed(self) -> None:
        with self.assertRaisesRegex(ValueError, "require --confirm"):
            self._run(
                "community-prepare",
                confirm=False,
                blueprint_id="release_review",
                version=1,
                draft_id="release_share",
                artifact_id="release_review_artifact",
                passport=None,
            )
        self.assertEqual(self.community.list(), ())

    def test_prepares_publishes_exports_stages_and_activates_through_ports(self) -> None:
        self.assertEqual(
            self._run(
                "community-prepare",
                confirm=True,
                blueprint_id="release_review",
                version=1,
                draft_id="release_share",
                artifact_id="release_review_artifact",
                passport=None,
            ),
            0,
        )
        self.assertEqual(self._run("community-publish", confirm=True, draft_id="release_share"), 0)

        with tempfile.TemporaryDirectory() as directory:
            release_path = Path(directory) / "release.json"
            self.assertEqual(
                self._run(
                    "community-export",
                    confirm=True,
                    draft_id="release_share",
                    output=release_path,
                ),
                0,
            )
            self.assertTrue(release_path.is_file())
            self.assertNotIn("Inspect {{objective}}", release_path.read_text(encoding="utf-8"))
            self.assertEqual(
                self._run(
                    "community-stage",
                    confirm=True,
                    release_file=release_path,
                ),
                0,
            )

        staged = self.rendered[-1]["staged_blueprint"]  # type: ignore[index]
        self.assertEqual(
            self._run(
                "community-activate",
                confirm=True,
                blueprint_id=staged.blueprint_id,
                version=staged.version,
                slot="default",
            ),
            0,
        )
        self.assertEqual(self.control.catalog().selection.blueprint_ref, staged.ref)

    def test_unrelated_graph_command_is_left_for_its_own_adapter(self) -> None:
        self.assertIsNone(self._run("list", confirm=False))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
