from __future__ import annotations

import re
import unittest
from pathlib import Path

from dynamic_firm.providers.fake import ScriptedModelProvider
from dynamic_firm.runtime.models import CompletionEnvelope, ModelResponse, RunStatus, ToolCall
from dynamic_firm.runtime.service import NativeEmployeeRuntimeService
from dynamic_firm.runtime.store import RunStore
from dynamic_firm.runtime.tools import ToolRegistry, WorkspaceReadTools
from tests.runtime.helpers import make_request


FIXTURE_ROOT = Path(__file__).parents[1] / "fixtures" / "tiny_repo"
EVIDENCE_REFERENCE = re.compile(r"^(?P<path>[^:]+):(?P<line>[1-9][0-9]*)\b")


def score_repository_analysis(result) -> tuple[bool, tuple[str, ...]]:
    """Score required output fields against the immutable fixture contents."""

    failures: list[str] = []
    if "denominator" not in result.summary.lower() or "calculator.py" not in result.summary:
        failures.append("summary does not state the probable cause and change scope")
    if not result.suggested_followups or not any(
        "unittest" in item and "test_calculator" in item for item in result.suggested_followups
    ):
        failures.append("deterministic validation command is missing")
    if not result.unresolved_issues:
        failures.append("uncertainty is missing")

    referenced_paths: set[str] = set()
    for evidence in result.acceptance_evidence:
        match = EVIDENCE_REFERENCE.match(evidence)
        if not match:
            failures.append(f"invalid evidence reference: {evidence}")
            continue
        relative_path = match.group("path")
        line_number = int(match.group("line"))
        candidate = FIXTURE_ROOT / relative_path
        if not candidate.is_file():
            failures.append(f"evidence file does not exist: {relative_path}")
            continue
        lines = candidate.read_text(encoding="utf-8").splitlines()
        if line_number > len(lines):
            failures.append(f"evidence line is outside the file: {evidence}")
            continue
        referenced_paths.add(relative_path)

    required_paths = {"calculator.py", "test_calculator.py"}
    if not required_paths.issubset(referenced_paths):
        failures.append("implementation and test evidence are both required")
    return not failures, tuple(failures)


class RepositoryAnalystFixtureTests(unittest.IsolatedAsyncioTestCase):
    async def test_read_only_repository_analysis_passes_deterministic_scorer(self) -> None:
        store = RunStore()
        workspace_tools = WorkspaceReadTools({"tiny-repo": FIXTURE_ROOT})
        registry = ToolRegistry()
        for definition in workspace_tools.definitions():
            registry.register(definition)
        provider = ScriptedModelProvider(
            [
                ModelResponse(
                    tool_calls=(
                        ToolCall(
                            "list-root",
                            "list_workspace_files",
                            {"workspace_id": "tiny-repo", "path": "."},
                        ),
                    )
                ),
                ModelResponse(
                    tool_calls=(
                        ToolCall(
                            "read-task",
                            "read_workspace_file",
                            {"workspace_id": "tiny-repo", "path": "TASK.md"},
                        ),
                    )
                ),
                ModelResponse(
                    tool_calls=(
                        ToolCall(
                            "read-code",
                            "read_workspace_file",
                            {"workspace_id": "tiny-repo", "path": "calculator.py"},
                        ),
                    )
                ),
                ModelResponse(
                    tool_calls=(
                        ToolCall(
                            "read-test",
                            "read_workspace_file",
                            {"workspace_id": "tiny-repo", "path": "test_calculator.py"},
                        ),
                    )
                ),
                ModelResponse(
                    completion=CompletionEnvelope(
                        summary=(
                            "calculator.py should treat a zero denominator as non-divisible; "
                            "change only its boundary guard from < 0 to <= 0."
                        ),
                        acceptance_evidence=(
                            "calculator.py:3 zero is excluded from the current guard",
                            "calculator.py:5 zero reaches division",
                            "test_calculator.py:8 the boundary expectation is None",
                        ),
                        unresolved_issues=(
                            "The fixture specifies zero behavior but does not define every negative-denominator case.",
                        ),
                        suggested_followups=(
                            "Run: python -m unittest test_calculator.py",
                        ),
                    )
                ),
            ]
        )
        service = NativeEmployeeRuntimeService(store=store, provider=provider, registry=registry)
        request = make_request(
            request_id="repository-analyst-fixture-v1",
            tool_names=("list_workspace_files", "read_workspace_file"),
            resource_patterns=("workspace:tiny-repo:*",),
            workspace_id="tiny-repo",
        )

        result = await service.collect(await service.start(request))
        passed, failures = score_repository_analysis(result)

        self.assertEqual(result.status, RunStatus.SUCCEEDED)
        self.assertTrue(passed, failures)
        self.assertEqual(store.get_result(result.run_id), result)
        self.assertEqual(workspace_tools.list_call_count, 1)
        self.assertEqual(workspace_tools.read_call_count, 3)
        self.assertIn("calculator.py", provider.requests[1].messages[-1].content["content"])
        self.assertIn("denominator < 0", provider.requests[3].messages[-1].content["content"])
        self.assertIn("assertIsNone", provider.requests[4].messages[-1].content["content"])
        store.close()


if __name__ == "__main__":
    unittest.main()
