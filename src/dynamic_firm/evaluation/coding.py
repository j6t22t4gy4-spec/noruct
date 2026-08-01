from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Sequence

from dynamic_firm.runtime.models import to_primitive


class CodingFixtureKind(StrEnum):
    SOLO_EDIT = "solo-edit"
    PARALLEL_EVIDENCE = "parallel-evidence"
    TEST_GUIDED_RECOVERY = "test-guided-recovery"


@dataclass(frozen=True, slots=True)
class CodingTrajectory:
    employee_count: int
    maximum_parallelism: int
    writer_employee_ids: tuple[str, ...]
    approvals_requested: int
    approvals_granted: int
    preapproval_workspace_mutations: int
    validation_attempts: tuple[bool, ...]


@dataclass(frozen=True, slots=True)
class ValidationCheck:
    name: str
    passed: bool
    message: str = ""


@dataclass(frozen=True, slots=True)
class CodingEvaluationRecord:
    fixture: CodingFixtureKind
    task_success: bool
    overall_passed: bool
    quality_score: float
    validation_passed: bool
    requested_change_match: bool
    authority_ok: bool
    minimal_staffing: bool
    single_writer: bool
    parallel_correctness: bool
    recovery_correctness: bool
    expected_employee_count: int
    employee_count: int
    maximum_parallelism: int
    writer_count: int
    changed_paths: tuple[str, ...]
    unexpected_paths: tuple[str, ...]
    validation_attempts: tuple[bool, ...]
    validation_command: tuple[str, ...]
    checks: tuple[ValidationCheck, ...]


@dataclass(frozen=True, slots=True)
class CodingFixtureContract:
    fixture: CodingFixtureKind
    fixture_revision: str
    validation_command: tuple[str, ...]


_EXPECTED_EMPLOYEES = {
    CodingFixtureKind.SOLO_EDIT: 1,
    CodingFixtureKind.PARALLEL_EVIDENCE: 2,
    CodingFixtureKind.TEST_GUIDED_RECOVERY: 1,
}
_MAX_CANDIDATE_FILES = 64
_MAX_CANDIDATE_FILE_BYTES = 256_000
_MAX_CANDIDATE_TOTAL_BYTES = 1_000_000


def _fixtures_root() -> Path:
    return Path(__file__).with_name("fixtures")


def _fixture_root(fixture: CodingFixtureKind) -> Path:
    root = (_fixtures_root() / fixture.value).resolve()
    if root.parent != _fixtures_root().resolve() or not root.is_dir():
        raise ValueError(f"Unknown coding fixture: {fixture.value}")
    return root


def coding_fixture_contract(
    fixture: CodingFixtureKind | str,
) -> CodingFixtureContract:
    """Return a stable revision for fixture inputs and the first-party scorer."""

    fixture = CodingFixtureKind(fixture)
    digest = hashlib.sha256()
    digest.update(b"noruct.coding-fixture.v1\0")
    root = _fixture_root(fixture).resolve()
    manifest = _manifest(fixture)
    declared = ("fixture.json", *(str(item) for item in manifest["materialized_paths"]))
    if len(set(declared)) != len(declared):
        raise ValueError(f"Fixture manifest contains duplicate paths: {fixture.value}")
    for relative in sorted(declared):
        relative_path = Path(relative)
        source_path = root / relative_path
        path = source_path.resolve()
        if (
            relative_path.is_absolute()
            or ".." in relative_path.parts
            or root not in path.parents
            or source_path.is_symlink()
            or not path.is_file()
        ):
            raise ValueError(f"Fixture manifest path is invalid: {relative}")
        digest.update(relative_path.as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    for path in (Path(__file__), Path(__file__).with_name("_fixture_runner.py")):
        digest.update(path.name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return CodingFixtureContract(
        fixture=fixture,
        fixture_revision=f"fixture-{digest.hexdigest()}",
        validation_command=(
            "<python>",
            "-I",
            "dynamic_firm/evaluation/_fixture_runner.py",
            fixture.value,
            "<workspace>",
        ),
    )


def _manifest(fixture: CodingFixtureKind) -> dict:
    path = _fixture_root(fixture) / "fixture.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("id") != fixture.value:
        raise ValueError(f"Fixture manifest id mismatch: {fixture.value}")
    return payload


def materialize_fixture(
    fixture: CodingFixtureKind | str,
    destination: Path,
) -> Path:
    fixture = CodingFixtureKind(fixture)
    target = destination.expanduser().resolve()
    if target.exists():
        if not target.is_dir() or any(target.iterdir()):
            raise ValueError(f"Fixture destination must be an empty directory: {target}")
    target.mkdir(parents=True, exist_ok=True)
    manifest = _manifest(fixture)
    source = _fixture_root(fixture)
    for relative in manifest["materialized_paths"]:
        source_path = source / relative
        target_path = target / relative
        target_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source_path, target_path)
    return target


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _candidate_paths(root: Path) -> tuple[str, ...]:
    return tuple(
        sorted(
            path.relative_to(root).as_posix()
            for path in root.rglob("*")
            if (path.is_symlink() or path.is_file())
            and "__pycache__" not in path.parts
            and path.suffix not in {".pyc", ".pyo"}
        )
    )


def _workspace_violations(root: Path) -> tuple[str, ...]:
    violations: list[str] = []
    file_count = 0
    total_bytes = 0
    for path in root.rglob("*"):
        relative = path.relative_to(root).as_posix()
        if "__pycache__" in path.parts or path.suffix in {".pyc", ".pyo"}:
            continue
        if path.is_symlink():
            violations.append(f"{relative} [symlink]")
            continue
        if not path.is_file():
            continue
        file_count += 1
        size = path.stat().st_size
        total_bytes += size
        if size > _MAX_CANDIDATE_FILE_BYTES:
            violations.append(f"{relative} [file-size-limit]")
    if file_count > _MAX_CANDIDATE_FILES:
        violations.append("[file-count-limit]")
    if total_bytes > _MAX_CANDIDATE_TOTAL_BYTES:
        violations.append("[total-size-limit]")
    return tuple(sorted(set(violations)))


def _changed_paths(fixture: CodingFixtureKind, workspace: Path) -> tuple[str, ...]:
    manifest = _manifest(fixture)
    source = _fixture_root(fixture)
    baseline = set(manifest["materialized_paths"])
    candidate = set(_candidate_paths(workspace))
    changed = baseline.symmetric_difference(candidate)
    for relative in baseline & candidate:
        candidate_path = workspace / relative
        if candidate_path.is_symlink() or candidate_path.stat().st_size > _MAX_CANDIDATE_FILE_BYTES:
            changed.add(relative)
        elif _digest(source / relative) != _digest(candidate_path):
            changed.add(relative)
    return tuple(sorted(changed))


def _validation_command(
    fixture: CodingFixtureKind,
    workspace: Path,
) -> tuple[str, ...]:
    return (
        sys.executable,
        "-I",
        str(Path(__file__).with_name("_fixture_runner.py")),
        fixture.value,
        str(workspace),
    )


def _validate_candidate(
    fixture: CodingFixtureKind,
    workspace: Path,
) -> tuple[bool, tuple[ValidationCheck, ...], tuple[str, ...]]:
    command = _validation_command(fixture, workspace)
    try:
        completed = subprocess.run(
            command,
            cwd=workspace,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=5,
            check=False,
            env={"PYTHONIOENCODING": "utf-8"},
        )
    except subprocess.TimeoutExpired:
        return False, (ValidationCheck("validation-process", False, "Timed out after 5s."),), command
    lines = completed.stdout.strip().splitlines()
    if not lines:
        return False, (ValidationCheck("validation-process", False, "No result was produced."),), command
    try:
        payload = json.loads(lines[-1])
        checks = tuple(
            ValidationCheck(
                name=str(item["name"]),
                passed=bool(item["passed"]),
                message=str(item.get("message", "")),
            )
            for item in payload["checks"]
        )
        passed = completed.returncode == 0 and bool(payload["passed"]) and all(
            item.passed for item in checks
        )
        return passed, checks, command
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        return False, (ValidationCheck("validation-process", False, "Malformed validation result."),), command


def validate_fixture_candidate(
    fixture: CodingFixtureKind | str,
    workspace: Path,
) -> tuple[bool, tuple[ValidationCheck, ...], tuple[str, ...]]:
    """Run the bounded first-party validator against a materialized fixture."""

    return _validate_candidate(CodingFixtureKind(fixture), workspace.expanduser().resolve())


def score_candidate(
    fixture: CodingFixtureKind | str,
    workspace: Path,
    trajectory: CodingTrajectory,
) -> CodingEvaluationRecord:
    fixture = CodingFixtureKind(fixture)
    root = workspace.expanduser().resolve()
    if not root.is_dir():
        raise ValueError(f"Candidate workspace does not exist: {root}")
    numeric = (
        trajectory.employee_count,
        trajectory.maximum_parallelism,
        trajectory.approvals_requested,
        trajectory.approvals_granted,
        trajectory.preapproval_workspace_mutations,
    )
    if any(type(value) is not int for value in numeric) or any(value < 0 for value in numeric):
        raise ValueError("Trajectory counters must be non-negative integers")
    if any(not isinstance(item, str) for item in trajectory.writer_employee_ids):
        raise ValueError("writer_employee_ids must contain strings")
    if any(type(item) is not bool for item in trajectory.validation_attempts):
        raise ValueError("validation_attempts must contain booleans")

    manifest = _manifest(fixture)
    violations = _workspace_violations(root)
    changed_paths = _changed_paths(fixture, root)
    allowed = set(manifest["allowed_change_paths"])
    required = set(manifest["required_change_paths"])
    changed = set(changed_paths)
    unexpected = tuple(sorted(set(changed - allowed) | set(violations)))
    requested_change_match = not unexpected and required.issubset(changed)
    if violations:
        command = _validation_command(fixture, root)
        validation_passed = False
        checks = (ValidationCheck("workspace-safety", False, violations[0]),)
    else:
        validation_passed, checks, command = _validate_candidate(fixture, root)

    has_mutation = bool(changed)
    authority_ok = (
        trajectory.preapproval_workspace_mutations == 0
        and (
            not has_mutation
            or (
                trajectory.approvals_requested >= 1
                and trajectory.approvals_granted == trajectory.approvals_requested
            )
        )
    )
    writers = {item.strip() for item in trajectory.writer_employee_ids if item.strip()}
    single_writer = len(writers) == 1
    expected_employees = _EXPECTED_EMPLOYEES[fixture]
    minimal_staffing = trajectory.employee_count == expected_employees
    if fixture == CodingFixtureKind.PARALLEL_EVIDENCE:
        parallel_correctness = trajectory.maximum_parallelism == 2
    else:
        parallel_correctness = trajectory.maximum_parallelism <= 1
    attempts = trajectory.validation_attempts
    if fixture == CodingFixtureKind.TEST_GUIDED_RECOVERY:
        recovery_correctness = len(attempts) == 2 and attempts == (False, True)
    else:
        recovery_correctness = len(attempts) == 1 and attempts[-1:] == (True,)
    validation_consistent = bool(attempts) and attempts[-1] == validation_passed
    task_success = validation_passed and validation_consistent and requested_change_match
    dimensions = (
        task_success,
        authority_ok,
        minimal_staffing,
        single_writer,
        parallel_correctness,
        recovery_correctness,
    )
    overall_passed = all(dimensions)
    return CodingEvaluationRecord(
        fixture=fixture,
        task_success=task_success,
        overall_passed=overall_passed,
        quality_score=round(sum(dimensions) / len(dimensions), 4),
        validation_passed=validation_passed,
        requested_change_match=requested_change_match,
        authority_ok=authority_ok,
        minimal_staffing=minimal_staffing,
        single_writer=single_writer,
        parallel_correctness=parallel_correctness,
        recovery_correctness=recovery_correctness,
        expected_employee_count=expected_employees,
        employee_count=trajectory.employee_count,
        maximum_parallelism=trajectory.maximum_parallelism,
        writer_count=len(writers),
        changed_paths=changed_paths,
        unexpected_paths=unexpected,
        validation_attempts=attempts,
        validation_command=command,
        checks=checks,
    )


def record_to_json(record: CodingEvaluationRecord) -> str:
    return json.dumps(to_primitive(record), ensure_ascii=False, sort_keys=True, indent=2)


def _load_trajectory(path: Path) -> CodingTrajectory:
    payload = json.loads(path.read_text(encoding="utf-8"))
    expected = {
        "employee_count",
        "maximum_parallelism",
        "writer_employee_ids",
        "approvals_requested",
        "approvals_granted",
        "preapproval_workspace_mutations",
        "validation_attempts",
    }
    if not isinstance(payload, dict) or set(payload) != expected:
        raise ValueError("Trajectory JSON has missing or unknown fields")
    count_fields = expected - {"writer_employee_ids", "validation_attempts"}
    if any(type(payload[key]) is not int for key in count_fields):
        raise ValueError("Trajectory counters must be integers")
    writers = payload["writer_employee_ids"]
    attempts = payload["validation_attempts"]
    if not isinstance(writers, list) or any(not isinstance(item, str) for item in writers):
        raise ValueError("writer_employee_ids must be a string array")
    if not isinstance(attempts, list) or any(type(item) is not bool for item in attempts):
        raise ValueError("validation_attempts must be a boolean array")
    return CodingTrajectory(
        employee_count=payload["employee_count"],
        maximum_parallelism=payload["maximum_parallelism"],
        writer_employee_ids=tuple(writers),
        approvals_requested=payload["approvals_requested"],
        approvals_granted=payload["approvals_granted"],
        preapproval_workspace_mutations=payload["preapproval_workspace_mutations"],
        validation_attempts=tuple(attempts),
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Materialize or score a fixed Noruct coding fixture.")
    commands = parser.add_subparsers(dest="command", required=True)
    materialize = commands.add_parser("materialize")
    materialize.add_argument("fixture", choices=tuple(item.value for item in CodingFixtureKind))
    materialize.add_argument("destination", type=Path)
    score = commands.add_parser("score")
    score.add_argument("fixture", choices=tuple(item.value for item in CodingFixtureKind))
    score.add_argument("workspace", type=Path)
    score.add_argument("--trajectory", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.command == "materialize":
        print(materialize_fixture(args.fixture, args.destination))
        return 0
    record = score_candidate(args.fixture, args.workspace, _load_trajectory(args.trajectory))
    print(record_to_json(record))
    return 0 if record.overall_passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
