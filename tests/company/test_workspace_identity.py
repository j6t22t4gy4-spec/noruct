from __future__ import annotations

import json
import os
import tempfile
import unittest
from dataclasses import asdict
from pathlib import Path
from unittest.mock import patch

from dynamic_firm.company import (
    WORKFLOW_CONTEXT_FINGERPRINT_REVISION,
    WORKSPACE_STRUCTURE_PROJECTION_REVISION,
    WorkspaceProjectionError,
    WorkspaceProjectionFailureCode,
    project_workspace_manifest,
    project_workspace_structure,
    workflow_context_fingerprint,
    workflow_context_fingerprint_v2,
)
from dynamic_firm.runtime.ports import CancellationToken
from dynamic_firm.runtime.tools import ToolValidationError, WorkspaceReadTools


class WorkspaceIdentityTests(unittest.TestCase):
    def test_empty_and_single_file_workspaces_have_stable_nonempty_identity(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            empty = project_workspace_structure(root, "READ_ONLY")
            empty_fingerprint = workflow_context_fingerprint_v2(empty)
            (root / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
            single = project_workspace_structure(root, "READ_ONLY")
            single_fingerprint = workflow_context_fingerprint_v2(single)

        self.assertEqual(empty.revision, WORKSPACE_STRUCTURE_PROJECTION_REVISION)
        self.assertTrue(empty_fingerprint.startswith("wctx2-"))
        self.assertIn("pyproject.toml", single.project_markers)
        self.assertNotEqual(empty_fingerprint, single_fingerprint)
        self.assertEqual(WORKFLOW_CONTEXT_FINGERPRINT_REVISION, "noruct.workflow-context.v2")

    def test_500_501_and_thousands_of_files_do_not_collapse_to_empty_context(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for index in range(500):
                (root / f"file-{index:04d}.py").touch()
            projection_500 = project_workspace_structure(root, "READ_ONLY")
            (root / "file-0500.py").touch()
            projection_501 = project_workspace_structure(root, "READ_ONLY")
            for index in range(501, 1_501):
                (root / f"file-{index:04d}.py").touch()
            bounded = project_workspace_structure(
                root,
                "READ_ONLY",
                max_entries=1_000,
            )

        self.assertFalse(projection_500.truncated)
        self.assertFalse(projection_501.truncated)
        self.assertTrue(workflow_context_fingerprint_v2(projection_500))
        self.assertTrue(workflow_context_fingerprint_v2(projection_501))
        self.assertTrue(bounded.truncated)
        self.assertIn("ENTRY_LIMIT", bounded.truncation_reasons)
        self.assertTrue(workflow_context_fingerprint_v2(bounded))

    def test_protected_generated_sensitive_and_state_artifacts_do_not_change_identity(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "src").mkdir()
            (root / "src" / "app.py").write_text("print('safe')\n", encoding="utf-8")
            (root / "runtime.db").write_text("state-one", encoding="utf-8")
            baseline = project_workspace_structure(
                root,
                "READ_ONLY",
                excluded_paths=("runtime.db",),
            )
            for segment in (".git", ".noruct", ".venv", "node_modules", "dist", ".cache"):
                tree = root / segment
                tree.mkdir()
                (tree / "private-secret-name.py").write_text("secret content", encoding="utf-8")
            (root / ".env.production").write_text("TOKEN=secret", encoding="utf-8")
            (root / "server.pem").write_text("secret", encoding="utf-8")
            (root / "runtime.db").write_text("state-two", encoding="utf-8")
            after = project_workspace_structure(
                root,
                "READ_ONLY",
                excluded_paths=("runtime.db",),
            )

        self.assertEqual(baseline, after)
        serialized = json.dumps(asdict(after), sort_keys=True)
        self.assertNotIn("private-secret-name", serialized)
        self.assertNotIn("TOKEN", serialized)
        self.assertNotIn("runtime.db", serialized)
        self.assertNotIn(".pem", serialized)

    def test_symlink_loop_is_not_followed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "app.py").touch()
            try:
                (root / "loop").symlink_to(root, target_is_directory=True)
            except (NotImplementedError, OSError):
                self.skipTest("symlink creation is unavailable")
            projection = project_workspace_structure(root, "READ_ONLY")

        self.assertEqual(projection.extension_histogram, ((".py", 1),))
        self.assertFalse(projection.truncated)

    def test_unreadable_child_is_a_redacted_truncation_not_a_path_leak(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            denied = (root / "private-name").resolve()
            denied.mkdir()
            (root / "visible.py").touch()
            original = os.scandir

            def controlled_scandir(path):
                if Path(path) == denied:
                    raise PermissionError("private-name must not escape")
                return original(path)

            with patch(
                "dynamic_firm.company.workspace_identity.os.scandir",
                side_effect=controlled_scandir,
            ):
                projection = project_workspace_structure(root, "READ_ONLY")

        self.assertTrue(projection.truncated)
        self.assertEqual(projection.truncation_reasons, ("UNREADABLE_ENTRY",))
        self.assertNotIn("private-name", json.dumps(asdict(projection)))

    def test_manifest_adapter_is_order_and_path_separator_independent(self) -> None:
        posix = ("src/app.py", "tests/test_app.py", "pyproject.toml")
        windows_reversed = (
            "pyproject.toml",
            "tests\\test_app.py",
            "src\\app.py",
        )
        first = project_workspace_manifest("READ_ONLY", posix)
        second = project_workspace_manifest("READ_ONLY", windows_reversed)

        self.assertEqual(first, second)
        self.assertEqual(
            workflow_context_fingerprint_v2(first),
            workflow_context_fingerprint_v2(second),
        )

    def test_marker_extension_and_count_bucket_changes_are_meaningful(self) -> None:
        base = project_workspace_manifest(
            "READ_ONLY",
            tuple(f"notes/item-{index}.txt" for index in range(10)),
        )
        more = project_workspace_manifest(
            "READ_ONLY",
            tuple(f"notes/item-{index}.txt" for index in range(11)),
        )
        marker = project_workspace_manifest(
            "READ_ONLY",
            ("pyproject.toml",) + tuple(f"notes/item-{index}.txt" for index in range(10)),
        )
        extension = project_workspace_manifest(
            "READ_ONLY",
            tuple(f"notes/item-{index}.py" for index in range(10)),
        )

        fingerprints = {
            workflow_context_fingerprint_v2(item)
            for item in (base, more, marker, extension)
        }
        self.assertEqual(len(fingerprints), 4)

    def test_legacy_fingerprint_remains_available_but_is_namespaced_from_v2(self) -> None:
        manifest = ("src/app.py", "pyproject.toml")
        legacy = workflow_context_fingerprint("READ_ONLY", manifest)
        current = workflow_context_fingerprint_v2(
            project_workspace_manifest("READ_ONLY", manifest)
        )

        self.assertEqual(len(legacy), 24)
        self.assertTrue(current.startswith("wctx2-"))
        self.assertNotEqual(legacy, current)

    def test_invalid_roots_and_time_budget_fail_with_redacted_codes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            missing = root / "operator-private-workspace"
            with self.assertRaises(WorkspaceProjectionError) as missing_error:
                project_workspace_structure(missing, "READ_ONLY")
            with patch(
                "dynamic_firm.company.workspace_identity.time.monotonic",
                side_effect=(0.0, 2.0),
            ), self.assertRaises(WorkspaceProjectionError) as time_error:
                project_workspace_structure(root, "READ_ONLY", max_seconds=1.0)

        self.assertEqual(
            missing_error.exception.code,
            WorkspaceProjectionFailureCode.ROOT_UNAVAILABLE,
        )
        self.assertEqual(
            time_error.exception.code,
            WorkspaceProjectionFailureCode.TIME_BUDGET_EXCEEDED,
        )
        self.assertNotIn("operator-private-workspace", str(missing_error.exception))


class WorkspaceReadBoundaryTests(unittest.IsolatedAsyncioTestCase):
    async def test_model_facing_listing_still_rejects_the_501st_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for index in range(501):
                (root / f"file-{index:03d}.txt").touch()
            tools = WorkspaceReadTools({"workspace": root})
            definition = next(
                item for item in tools.definitions() if item.name == "list_workspace_files"
            )
            arguments = definition.validator(
                {"workspace_id": "workspace", "path": "."}
            )
            with self.assertRaises(ToolValidationError):
                await definition.handler(arguments, CancellationToken())

        self.assertEqual(tools.max_entries, 500)


if __name__ == "__main__":
    unittest.main()
