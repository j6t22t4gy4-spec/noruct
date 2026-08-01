"""Regression coverage for the runtime-store to coding import boundary."""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import unittest


class RuntimeStoreImportBoundaryTests(unittest.TestCase):
    def test_fresh_interpreter_imports_store_before_lazy_coding_services(self) -> None:
        project_root = Path(__file__).resolve().parents[2]
        environment = os.environ.copy()
        environment["PYTHONPATH"] = str(project_root / "src")
        child = subprocess.run(
            [
                sys.executable,
                "-c",
                "\n".join(
                    (
                        "from dynamic_firm.runtime.store import RunStore",
                        "from dynamic_firm.coding import (",
                        "    APPLY_CHANGE_SET_TOOL,",
                        "    RoutedEmployeeExecutionService,",
                        "    ShadowCodingEmployeeRuntimeService,",
                        ")",
                        "assert RunStore.__name__ == 'RunStore'",
                        "assert APPLY_CHANGE_SET_TOOL == 'apply_workspace_change_set'",
                        "assert RoutedEmployeeExecutionService.__name__ == 'RoutedEmployeeExecutionService'",
                        "assert ShadowCodingEmployeeRuntimeService.__name__ == 'ShadowCodingEmployeeRuntimeService'",
                    )
                ),
            ],
            cwd=project_root,
            env=environment,
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertEqual(child.returncode, 0, child.stderr)
        self.assertEqual(child.stdout, "")


if __name__ == "__main__":
    unittest.main()
