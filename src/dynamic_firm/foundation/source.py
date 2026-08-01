"""Noruct-owned contract around the private employee-agent foundation.

No upstream type crosses this module.  H1 uses a subprocess so the unchanged
upstream top-level package names cannot collide with Noruct or a customer
workspace.  H2 may add a richer Employee Execution Port adapter after the
source, dependency, and authority gates established here remain green.
"""

from __future__ import annotations

import hashlib
import importlib.metadata
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path, PurePosixPath
from typing import Any, Mapping


EMPLOYEE_FOUNDATION_COMMIT = "89bd0fba903bbfd78b0d99ce6f194863dd01b7e1"
EMPLOYEE_FOUNDATION_VERSION = "0.18.2"
EMPLOYEE_FOUNDATION_TREE_SHA256 = "fd993f4a6f5dd3f4fd8445dc34b8e84518c3b72bc953c356e5813663238f96d6"
EMPLOYEE_ACTIVE_FORK_TREE_SHA256 = "737e9e689bad0b382f01537a89a86c3ea2c262ac5d109fecd0e057b7052cfbc5"
HISTORICAL_EMPLOYEE_CAPSULE_TREE_SHA256 = "d97c040bcf8182d4c29688f4c618ae38e75bcebd60f6dd89d9ed602a2b34c2bc"
_DEVELOPMENT_BASELINE_SECONDARY_PROVENANCE_FINDING_COUNT = 60
_ACTIVE_SECONDARY_PROVENANCE_FINDING_COUNT = 4
_VENDOR_ROOT = Path(__file__).parents[1] / "_vendor" / "hermes_agent"
_MANIFEST_PATH = _VENDOR_ROOT / "UPSTREAM_MANIFEST.json"
_POLICY_PATH = _VENDOR_ROOT / "VENDOR_POLICY.json"
_REQUIREMENTS_PATH = _VENDOR_ROOT / "DEPENDENCY_REQUIREMENTS.json"
_CAPSULE_ROOT = Path(__file__).parents[1] / "_vendor" / "employee_runtime_capsule"
_CAPSULE_MANIFEST_PATH = _CAPSULE_ROOT / "CAPSULE_MANIFEST.json"
_CAPSULE_UPSTREAM_ROOT = _CAPSULE_ROOT / "upstream"
_RUNTIME_REQUIREMENTS_PATH = _CAPSULE_ROOT / "RUNTIME_DEPENDENCY_REQUIREMENTS.json"
_REQUIREMENT_NAME = re.compile(r"^[A-Za-z0-9_.-]+")
_SECONDARY_PROVENANCE_MARKER = re.compile(
    r"\b(?:ported|adapted)(?:\s+and\s+adapted)?\s+from\b", re.IGNORECASE
)

# The historical capsule is deliberately excluded from the installed product
# artifact. Runtime selection must therefore not read its source-only audit
# metadata. This small first-party declaration mirrors the audited H2 closure
# (one exact PyYAML dependency) and is independently bound by pyproject and
# the clean-wheel qualification.
_PRODUCT_RUNTIME_REQUIREMENTS: Mapping[str, object] = {
    "audit_profile": "employee_runtime_h2_py311_macos_arm64",
    "commercial_release_approved": False,
    "direct": ("PyYAML==6.0.3",),
    "exact_closure": ("PyYAML==6.0.3",),
    "license_review_blockers": (),
}


class FoundationSourceError(RuntimeError):
    """A typed foundation verification or isolated-worker failure."""


def _read_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise FoundationSourceError(f"invalid foundation metadata: {path.name}") from exc
    if not isinstance(payload, dict):
        raise FoundationSourceError(f"invalid foundation metadata object: {path.name}")
    return payload


def _tree_hash(files: list[Mapping[str, Any]]) -> str:
    digest = hashlib.sha256()
    for item in files:
        digest.update(str(item["upstream_path"]).encode("utf-8"))
        digest.update(b"\0")
        digest.update(str(item["sha256"]).encode("ascii"))
        digest.update(b"\n")
    return digest.hexdigest()


def _safe_capsule_relative_path(value: object) -> PurePosixPath:
    relative = PurePosixPath(str(value or ""))
    if not relative.parts or relative.is_absolute() or ".." in relative.parts:
        raise FoundationSourceError("employee runtime capsule path escaped its root")
    return relative


def verify_employee_runtime_capsule() -> dict[str, Any]:
    """Verify the historical source-only capsule audit evidence.

    The full H1 intake is a development qualification baseline, not a product
    runtime import tree. It is not part of the product wheel after the active
    Hermes-fork cutover, and remains available only in source checkouts for
    historical provenance review.
    """

    manifest = _read_json(_CAPSULE_MANIFEST_PATH)
    if manifest.get("capsule_kind") != "noruct_employee_runtime_trace_bound_source_capsule":
        raise FoundationSourceError("employee runtime capsule kind mismatch")
    baseline = manifest.get("development_baseline")
    if not isinstance(baseline, dict):
        raise FoundationSourceError("employee runtime capsule has no baseline identity")
    if baseline.get("source_commit") != EMPLOYEE_FOUNDATION_COMMIT:
        raise FoundationSourceError("employee runtime capsule commit mismatch")
    if baseline.get("upstream_version") != EMPLOYEE_FOUNDATION_VERSION:
        raise FoundationSourceError("employee runtime capsule version mismatch")
    if baseline.get("source_tree_sha256") != EMPLOYEE_FOUNDATION_TREE_SHA256:
        raise FoundationSourceError("employee runtime capsule baseline tree mismatch")
    if baseline.get("file_count") != 872:
        raise FoundationSourceError("employee runtime capsule baseline file count mismatch")
    if manifest.get("license") != "MIT":
        raise FoundationSourceError("employee runtime capsule license mismatch")

    license_path = _CAPSULE_ROOT / "LICENSE"
    try:
        license_data = license_path.read_bytes()
    except OSError as exc:
        raise FoundationSourceError("employee runtime capsule license is missing") from exc
    if hashlib.sha256(license_data).hexdigest() != manifest.get("license_sha256"):
        raise FoundationSourceError("employee runtime capsule license hash mismatch")

    files = manifest.get("source_files")
    if not isinstance(files, list) or not files:
        raise FoundationSourceError("employee runtime capsule has no source files")
    if len(files) != manifest.get("source_file_count"):
        raise FoundationSourceError("employee runtime capsule source file count mismatch")

    total = 0
    shipped_secondary_provenance_marker_count = 0
    paths: list[str] = []
    for item in files:
        if not isinstance(item, dict):
            raise FoundationSourceError("employee runtime capsule has an invalid source entry")
        relative = _safe_capsule_relative_path(item.get("upstream_path"))
        path = _CAPSULE_UPSTREAM_ROOT / relative
        try:
            data = path.read_bytes()
        except OSError as exc:
            raise FoundationSourceError(
                f"missing employee runtime capsule file: {relative.as_posix()}"
            ) from exc
        if hashlib.sha256(data).hexdigest() != item.get("sha256"):
            raise FoundationSourceError(
                f"employee runtime capsule hash mismatch: {relative.as_posix()}"
            )
        if len(data) != item.get("bytes"):
            raise FoundationSourceError(
                f"employee runtime capsule size mismatch: {relative.as_posix()}"
            )
        total += len(data)
        shipped_secondary_provenance_marker_count += sum(
            bool(_SECONDARY_PROVENANCE_MARKER.search(line))
            for line in data.decode("utf-8").splitlines()
        )
        paths.append(relative.as_posix())

    if paths != sorted(set(paths)):
        raise FoundationSourceError("employee runtime capsule source paths are not unique and sorted")
    tree_sha256 = _tree_hash(files)
    if tree_sha256 != manifest.get("source_tree_sha256"):
        raise FoundationSourceError("employee runtime capsule source tree mismatch")
    runtime_audit = manifest.get("runtime_audit")
    if not isinstance(runtime_audit, dict) or not runtime_audit.get("sha256"):
        raise FoundationSourceError("employee runtime capsule runtime-audit binding is missing")
    runtime_requirements = manifest.get("runtime_requirements")
    if not isinstance(runtime_requirements, dict):
        raise FoundationSourceError("employee runtime capsule requirements binding is missing")
    if runtime_requirements.get("path") != _RUNTIME_REQUIREMENTS_PATH.name:
        raise FoundationSourceError("employee runtime capsule requirements path mismatch")
    try:
        requirements_data = _RUNTIME_REQUIREMENTS_PATH.read_bytes()
    except OSError as exc:
        raise FoundationSourceError("employee runtime capsule requirements are missing") from exc
    if hashlib.sha256(requirements_data).hexdigest() != runtime_requirements.get("sha256"):
        raise FoundationSourceError("employee runtime capsule requirements hash mismatch")
    return {
        "capsule_kind": manifest["capsule_kind"],
        "development_baseline_file_count": baseline["file_count"],
        "development_baseline_tree_sha256": baseline["source_tree_sha256"],
        "file_count": len(files),
        "license": manifest["license"],
        "license_sha256": manifest["license_sha256"],
        "runtime_audit_sha256": runtime_audit["sha256"],
        "source_bytes": total,
        "source_commit": baseline["source_commit"],
        "secondary_provenance_marker_count": shipped_secondary_provenance_marker_count,
        "tree_sha256": tree_sha256,
        "upstream_version": baseline["upstream_version"],
    }


def verify_foundation_source() -> dict[str, Any]:
    """Verify the full development-only H1 qualification baseline.

    Source checkouts keep this complete active tree for provenance analysis and
    qualification smoke tests; installed Noruct wheels use this exact manifest.
    """

    if not _MANIFEST_PATH.is_file():
        raise FoundationSourceError(
            "full H1 development baseline is not installed; verify the Employee Runtime capsule instead"
        )

    manifest = _read_json(_MANIFEST_PATH)
    policy = _read_json(_POLICY_PATH)
    if manifest.get("source_commit") != EMPLOYEE_FOUNDATION_COMMIT:
        raise FoundationSourceError("foundation manifest commit mismatch")
    if manifest.get("upstream_version") != EMPLOYEE_FOUNDATION_VERSION:
        raise FoundationSourceError("foundation manifest version mismatch")
    if manifest.get("tree_sha256") != EMPLOYEE_ACTIVE_FORK_TREE_SHA256:
        raise FoundationSourceError("foundation manifest tree identity mismatch")
    if policy.get("source_commit") != EMPLOYEE_FOUNDATION_COMMIT:
        raise FoundationSourceError("foundation policy commit mismatch")

    files = manifest.get("files")
    if not isinstance(files, list) or not files:
        raise FoundationSourceError("foundation manifest has no files")
    if len(files) != manifest.get("file_count"):
        raise FoundationSourceError("foundation manifest file count mismatch")

    total = 0
    for item in files:
        if not isinstance(item, dict):
            raise FoundationSourceError("foundation manifest contains a non-object file")
        prefix = "src/dynamic_firm/_vendor/hermes_agent/"
        local = str(item.get("local_path") or "")
        if not local.startswith(prefix):
            raise FoundationSourceError("foundation manifest local path escaped vendor root")
        relative = local[len(prefix) :]
        relative_path = PurePosixPath(relative)
        if relative_path.is_absolute() or ".." in relative_path.parts:
            raise FoundationSourceError("foundation manifest local path escaped vendor root")
        path = _VENDOR_ROOT / relative
        try:
            data = path.read_bytes()
        except OSError as exc:
            raise FoundationSourceError(f"missing foundation file: {relative}") from exc
        if hashlib.sha256(data).hexdigest() != item.get("sha256"):
            raise FoundationSourceError(f"foundation source hash mismatch: {relative}")
        if len(data) != item.get("bytes"):
            raise FoundationSourceError(f"foundation source size mismatch: {relative}")
        total += len(data)

    if total != manifest.get("source_bytes"):
        raise FoundationSourceError("foundation source byte total mismatch")
    if _tree_hash(files) != manifest.get("tree_sha256"):
        raise FoundationSourceError("foundation source tree hash mismatch")
    return {
        "file_count": len(files),
        "license": manifest.get("license"),
        "source_bytes": total,
        "source_commit": manifest.get("source_commit"),
        "tree_sha256": manifest.get("tree_sha256"),
        "active_fork_tree_sha256": manifest.get("tree_sha256"),
        "development_baseline_tree_sha256": EMPLOYEE_FOUNDATION_TREE_SHA256,
        "upstream_version": manifest.get("upstream_version"),
    }


def _requirement_name(requirement: str) -> str:
    match = _REQUIREMENT_NAME.match(requirement.strip())
    if match is None:
        raise FoundationSourceError(f"invalid dependency requirement: {requirement!r}")
    return match.group(0).replace("_", "-").lower()


def _requirement_applies(requirement: str) -> bool:
    if ";" not in requirement:
        return True
    marker = requirement.split(";", 1)[1].strip().replace('"', "'")
    if marker == "sys_platform == 'win32'":
        return sys.platform == "win32"
    if marker == "sys_platform != 'win32'":
        return sys.platform != "win32"
    raise FoundationSourceError(f"unsupported foundation dependency marker: {marker}")


def _source_qualification_dependency_status() -> dict[str, Any]:
    if not _REQUIREMENTS_PATH.is_file():
        return {
            "available": False,
            "direct_requirement_count": 0,
            "installed": {},
            "missing": [],
            "ready": False,
            "selected_extras": ["cli", "mcp"],
        }
    requirements = _read_json(_REQUIREMENTS_PATH)
    raw = list(requirements.get("core_direct") or [])
    extras = requirements.get("selected_extras") or {}
    for name in ("cli", "mcp"):
        raw.extend(extras.get(name) or [])
    names = sorted({_requirement_name(item) for item in raw if _requirement_applies(item)})
    installed: dict[str, str] = {}
    missing: list[str] = []
    for name in names:
        try:
            installed[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            missing.append(name)
    return {
        "direct_requirement_count": len(names),
        "installed": installed,
        "missing": missing,
        "ready": not missing,
        "selected_extras": ["cli", "mcp"],
    }


def _runtime_dependency_status() -> dict[str, Any]:
    # Do not couple the installed default runtime to the historical capsule,
    # which is retained only in a source checkout for provenance review.
    requirements = _PRODUCT_RUNTIME_REQUIREMENTS
    direct = list(requirements.get("direct") or [])
    closure = list(requirements.get("exact_closure") or [])
    direct_names = sorted({_requirement_name(item) for item in direct})
    expected = {
        _requirement_name(item): item.split("==", 1)[1]
        for item in closure
        if "==" in item
    }
    if len(expected) != len(closure):
        raise FoundationSourceError("runtime dependency closure must use exact pins")
    installed: dict[str, str] = {}
    missing: list[str] = []
    mismatched: dict[str, dict[str, str]] = {}
    for name, version in sorted(expected.items()):
        try:
            actual = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            missing.append(name)
            continue
        installed[name] = actual
        if actual != version:
            mismatched[name] = {"expected": version, "installed": actual}
    return {
        "commercial_release_approved": bool(
            requirements.get("commercial_release_approved", False)
        ),
        "direct_requirement_count": len(direct_names),
        "exact_package_count": len(expected),
        "installed": installed,
        "license_review_blockers": list(
            requirements.get("license_review_blockers") or []
        ),
        "mismatched": mismatched,
        "missing": missing,
        "profile": str(requirements.get("audit_profile") or "employee_runtime_h2"),
        "ready": not missing and not mismatched,
        "selected_extras": [],
    }


def foundation_cutover_status() -> dict[str, Any]:
    """Return runtime readiness and separate, non-blocking release advisories.

    Legal/provenance review is evidence for a commercial release decision, not
    an execution-control input. The product selects the Noruct runtime by
    default whenever its audited runtime dependency is available; pending review
    stays visible without routing developers to an alternate runtime. Older
    employee-state receipts remain readable through a separate compatibility
    path; they are not an executable rollback profile.
    """

    dependencies = _runtime_dependency_status()
    license_blockers = list(dependencies["license_review_blockers"])
    technical_runtime_ready = bool(dependencies["ready"])
    dependency_review_state = "advisory_open" if license_blockers else "advisory_closed"
    provenance_review_state = (
        "advisory_open"
        if _ACTIVE_SECONDARY_PROVENANCE_FINDING_COUNT
        else "advisory_closed"
    )
    return {
        "activation": "noruct_runtime_default",
        "commercial_release_approved": False,
        "default_runtime": "noruct",
        "default_runtime_eligible": technical_runtime_ready,
        "technical_default_ready": technical_runtime_ready,
        "employee_execution_port": "noruct.employee.v2",
        "gate": {
            "dependency": {
                "exact_package_count": dependencies["exact_package_count"],
                "license_review_blockers": license_blockers,
                "runtime_ready": technical_runtime_ready,
                "state": dependency_review_state,
            },
            "secondary_provenance": {
                "active_import_surface_finding_count": _ACTIVE_SECONDARY_PROVENANCE_FINDING_COUNT,
                "development_baseline_finding_count": _DEVELOPMENT_BASELINE_SECONDARY_PROVENANCE_FINDING_COUNT,
                "distributed_source_finding_count": 0,
                "state": provenance_review_state,
            },
        },
        "legal_review": {
            "commercial_release_status": "pending_human_review",
            "affects_runtime_selection": False,
        },
        "reason": (
            "pending commercial provenance and legal review is advisory only; "
            "it does not restrict local development or the Noruct runtime default"
        ),
        # There is one executable Employee Runtime. Do not present the
        # historical state compatibility label as a second runtime choice:
        # CLI configuration has accepted only ``noruct`` since the cutover.
        "rollback_runtime": None,
        "runtime_rollback_available": False,
        "historical_state_compatibility": {
            "available": True,
            "label": "historical_employee_state",
            "execution": "read_only_preview_and_backup_receipt",
            "alternate_runtime_activated": False,
        },
        "source_commit": EMPLOYEE_FOUNDATION_COMMIT,
    }


def foundation_status(*, verify_source: bool = True) -> dict[str, Any]:
    source = verify_foundation_source() if verify_source else {
        "source_commit": EMPLOYEE_FOUNDATION_COMMIT,
        "upstream_version": EMPLOYEE_FOUNDATION_VERSION,
    }
    development_baseline: dict[str, Any] = {
        "available": _MANIFEST_PATH.is_file() and _VENDOR_ROOT.joinpath("upstream").is_dir(),
        "file_count": 872,
        "purpose": "development_qualification_and_provenance_review_only",
        "shipped_in_product_wheel": False,
    }
    if development_baseline["available"] and verify_source:
        development_baseline.update(verify_foundation_source())
    dependencies = _runtime_dependency_status()
    source_qualification = _source_qualification_dependency_status()
    cutover = foundation_cutover_status()
    return {
        "activation": cutover["activation"],
        "cutover": cutover,
        "default_runtime": cutover["default_runtime"],
        "technical_default_ready": cutover["technical_default_ready"],
        "dependencies": dependencies,
        "development_baseline": development_baseline,
        "active_execution_source": "hermes_fork",
        "historical_capsule": None,
        "dependency_ready": dependencies["ready"],
        "employee_execution_port": "noruct.employee.v2",
        "product_identity": "noruct",
        "source": source,
        "source_qualification_dependencies": source_qualification,
        "source_qualification_ready": source_qualification["ready"],
        "source_ready": True,
        "state_authority": "noruct",
    }


def run_foundation_smoke(
    *,
    python_executable: str | os.PathLike[str] | None = None,
    timeout_seconds: float = 90.0,
) -> dict[str, Any]:
    """Run the shipped employee worker with an explicitly selected Python.

    ``python_executable`` is the actual execution-worker interpreter, not the
    interpreter that launches this private harness.  This mirrors
    :class:`NoructEmployeeRuntimeService`: a minimal worker environment only
    needs the audited runtime dependency, while the parent projects Noruct's
    own code into the child.  It therefore remains useful when the selected
    worker Python does not itself have Noruct installed.
    """

    worker_python = os.fspath(python_executable or sys.executable)
    if not os.access(worker_python, os.X_OK):
        raise FoundationSourceError(
            f"employee runtime Python is not executable: {worker_python}"
        )
    with tempfile.TemporaryDirectory(prefix="noruct-employee-foundation-") as raw_home:
        home = Path(raw_home)
        env = dict(os.environ)
        package_parent = str(Path(__file__).parents[2])
        inherited_python_path = env.get("PYTHONPATH", "")
        env.update(
            {
                "HOME": str(home),
                "HERMES_DISABLE_LAZY_INSTALLS": "1",
                "HERMES_HOME": str(home / "state"),
                "NO_COLOR": "1",
                "NORUCT_FOUNDATION_SOURCE_WORKER": "1",
                # The private parent harness needs Noruct, while the worker
                # receives its own clean projection inside _source_worker.
                "PYTHONPATH": os.pathsep.join(
                    part for part in (package_parent, inherited_python_path) if part
                ),
            }
        )
        try:
            completed = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "dynamic_firm.foundation._source_worker",
                    "--home",
                    str(home / "state"),
                    "--worker-python",
                    worker_python,
                ],
                check=False,
                capture_output=True,
                env=env,
                text=True,
                timeout=timeout_seconds,
            )
        except subprocess.TimeoutExpired as exc:
            raise FoundationSourceError(
                f"isolated employee runtime smoke timed out after {timeout_seconds:g}s"
            ) from exc
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "worker failed").strip()
        if "ModuleNotFoundError: No module named 'yaml'" in detail:
            raise FoundationSourceError(
                "selected Noruct runtime Python lacks required PyYAML==6.0.3; "
                "repair the Noruct installation in that worker environment"
            )
        raise FoundationSourceError(
            f"isolated foundation smoke failed ({completed.returncode}): {detail[:1000]}"
        )
    try:
        result = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise FoundationSourceError("isolated foundation worker returned invalid JSON") from exc
    if not isinstance(result, dict) or not result.get("ok"):
        raise FoundationSourceError("isolated foundation worker did not pass")
    result["worker_python"] = worker_python
    return result


def foundation_preview_preflight(
    *,
    python_executable: str | os.PathLike[str],
    timeout_seconds: float = 90.0,
) -> dict[str, Any]:
    """Return a quota-free readiness record for the default runtime worker.

    A passing selected interpreter proves that the local default runtime can
    start. Commercial-release review remains a separate advisory and never
    changes the selected runtime in this report.
    """

    source = verify_foundation_source()
    cutover = foundation_cutover_status()
    worker = run_foundation_smoke(
        python_executable=python_executable,
        timeout_seconds=timeout_seconds,
    )
    return {
        "activation": cutover["activation"],
        "commercial_default_eligible": False,
        "technical_default_ready": cutover["technical_default_ready"],
        "cutover": cutover,
        "execution": "runtime_default_readiness",
        "external_model_calls": 0,
        "network_access": "denied_in_worker",
        "ok": True,
        "product_identity": "noruct",
        "schema_version": "noruct.employee-runtime-preflight.v1",
        "source": source,
        "historical_capsule": None,
        "state_authority": "noruct",
        "worker": worker,
    }
