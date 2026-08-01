#!/usr/bin/env python3
"""Run the provider-free test packages shipped in the public Core export."""

from __future__ import annotations

import argparse
from pathlib import Path
import subprocess
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
PUBLIC_TEST_PACKAGES = (
    "application",
    "company",
    "compiler",
    "kernel",
    "runtime",
    "product",
    "knowledge",
    "network",
    "providers",
    "evolution",
    "foundation",
    "evaluation",
)
PUBLIC_ROOT_TESTS = (
    "tests.test_capability_status",
    "tests.test_cli",
    "tests.test_component_import_boundaries",
    "tests.test_openai_media",
)
PRIVATE_EVIDENCE_TEST_SUFFIXES = (
    "EmployeeFoundationTests.test_cli_validates_explicit_provenance_record_without_activation",
    "EmployeeFoundationTests.test_release_admission_can_project_explicit_completed_provenance_without_activation",
    "ProviderSlotEvidenceTests.test_historical_captured_provider_slots_are_rejected_after_capsule_identity_changes",
)


def _cases(suite: unittest.TestSuite):
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            yield from _cases(item)
        else:
            yield item


def _run_package(package: str) -> int:
    directory = ROOT / "tests" / package
    if not directory.is_dir():
        print(f"missing public test package: {directory}", file=sys.stderr)
        return 2
    discovered = unittest.defaultTestLoader.discover(
        str(directory), pattern="test_*.py"
    )
    selected = []
    excluded = []
    for case in _cases(discovered):
        if any(case.id().endswith(suffix) for suffix in PRIVATE_EVIDENCE_TEST_SUFFIXES):
            excluded.append(case.id())
        else:
            selected.append(case)
    for test_id in excluded:
        print(f"PRIVATE_EVIDENCE_NOT_EXPORTED: {test_id}", flush=True)
    result = unittest.TextTestRunner(verbosity=1).run(unittest.TestSuite(selected))
    return 0 if result.wasSuccessful() else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--package", choices=PUBLIC_TEST_PACKAGES)
    args = parser.parse_args(argv)
    if args.package:
        return _run_package(args.package)

    root_tests = list(PUBLIC_ROOT_TESTS)
    # A Git checkout must prove that its tracked allow-list can be re-exported.
    # A plain materialized export has deliberately discarded .git metadata and
    # is verified directly by verify_public_monorepo.py instead.
    if (ROOT / ".git").exists():
        root_tests.append("tests.test_public_monorepo_export")
    root_command = [sys.executable, "-m", "unittest", *root_tests, "-q"]
    print("+", " ".join(root_command), flush=True)
    root_result = subprocess.run(root_command, cwd=ROOT, check=False)
    if root_result.returncode:
        return root_result.returncode
    for package in PUBLIC_TEST_PACKAGES:
        command = [
            sys.executable,
            str(Path(__file__).resolve()),
            "--package",
            package,
        ]
        print("+", " ".join(command), flush=True)
        completed = subprocess.run(command, cwd=ROOT, check=False)
        if completed.returncode:
            return completed.returncode
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
