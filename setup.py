"""Setuptools build hygiene for generated private runtime sources.

The active Hermes fork and historical capsule both have exact file scopes.
Setuptools otherwise reuses old package-data files from ``build/lib`` when a
wheel is rebuilt in the same checkout.  Clear both generated subtrees before
the normal build so excluded bytecode or removed upstream files cannot re-enter
a release artifact.
"""

from __future__ import annotations

import shutil
from pathlib import Path

from setuptools import setup
from setuptools.command.build_py import build_py as _BuildPy


class _NoructBuildPy(_BuildPy):
    def run(self) -> None:
        vendor_build_root = Path(self.build_lib) / "dynamic_firm" / "_vendor"
        for name in ("employee_runtime_capsule", "hermes_agent"):
            generated_root = vendor_build_root / name
            if generated_root.exists():
                shutil.rmtree(generated_root)
        super().run()


setup(cmdclass={"build_py": _NoructBuildPy})
