from __future__ import annotations

import json
import subprocess
import tomllib
from dataclasses import dataclass
from pathlib import Path

from dynamic_firm import __version__
from dynamic_firm.company.store import COMPANY_STATE_SCHEMA_VERSION
from dynamic_firm.runtime.store import SCHEMA_VERSION as RUNTIME_SCHEMA_VERSION

from .causal_workflow import run_causal_workflow_evaluation


@dataclass(frozen=True, slots=True)
class AlphaReadinessCheck:
    name: str
    category: str
    passed: bool
    evidence: str
    operator_required: bool = False


@dataclass(frozen=True, slots=True)
class AlphaReadinessEvaluation:
    schema_version: str
    ready: bool
    target_version: str
    current_version: str
    classification: str
    external_model_calls: int
    quota_consumed: bool
    checks: tuple[AlphaReadinessCheck, ...]
    blocking_checks: tuple[str, ...]
    next_actions: tuple[str, ...]


def _git_clean(root: Path) -> tuple[bool, str]:
    if not (root / ".git").exists():
        return False, "source root is not a Git worktree"
    completed = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=root,
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        return False, "git status failed"
    entries = tuple(line for line in completed.stdout.splitlines() if line.strip())
    return not entries, f"working-tree entries={len(entries)}"


def _operator_release_approval(root: Path) -> tuple[bool, str]:
    approval_path = (
        root
        / "docs"
        / "60-governance"
        / "release-approvals"
        / "0.1.0-alpha.json"
    )
    if not approval_path.is_file():
        return False, "operator release approval record is absent"
    try:
        payload = json.loads(approval_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False, "operator release approval record is unreadable"
    if payload.get("schema_version") != "noruct.alpha-release-authorization.v1":
        return False, "operator release approval schema is unsupported"
    if payload.get("status") != "AUTHORIZED":
        return False, "operator release approval is not authorized"
    if payload.get("product_version") != __version__:
        return False, "operator release approval is not bound to the current product version"
    if not isinstance(payload.get("authorized_release_owner"), str) or not payload[
        "authorized_release_owner"
    ].strip():
        return False, "operator release approval is missing an authorized release owner"
    if not isinstance(payload.get("authorized_at"), str) or not payload["authorized_at"].strip():
        return False, "operator release approval is missing an authorization timestamp"
    required = {
        "legal_review_complete": True,
        "publisher_configured": True,
        "signing_configured": True,
        "update_channel_configured": True,
    }
    evidence = payload.get("evidence_refs")
    evidence_complete = (
        isinstance(evidence, dict)
        and all(
            isinstance(evidence.get(key), str) and evidence[key].strip()
            for key in (
                "terms_data_notice_review",
                "provider_terms_review",
                "shipped_provenance_review",
                "publisher_signing_update_runbook",
            )
        )
    )
    passed = all(payload.get(key) is value for key, value in required.items()) and evidence_complete
    return passed, (
        "legal/publisher/signing/update approvals complete"
        if passed
        else "operator release approval fields or evidence references are incomplete"
    )


async def run_alpha_readiness_evaluation(
    source_root: str | Path,
) -> AlphaReadinessEvaluation:
    root = Path(source_root).expanduser().resolve()
    pyproject_path = root / "pyproject.toml"
    metadata = tomllib.loads(pyproject_path.read_text(encoding="utf-8"))
    package_version = str(metadata["project"]["version"])
    dependencies = tuple(metadata["project"].get("dependencies", ()))
    sbom = json.loads(
        (root / "docs" / "60-governance" / "sbom.cdx.json").read_text(
            encoding="utf-8"
        )
    )
    sbom_version = str(sbom["metadata"]["component"]["version"])
    source_register = (
        root / "docs" / "60-governance" / "source-register.yaml"
    ).read_text(encoding="utf-8")
    notices = (root / "THIRD_PARTY_NOTICES.md").read_text(encoding="utf-8")
    readme = (root / "README.md").read_text(encoding="utf-8")
    entrypoint_source = (
        root / "src" / "dynamic_firm" / "application" / "entrypoint_cli.py"
    ).read_text(encoding="utf-8")
    data_parser_source = (
        root / "src" / "dynamic_firm" / "application" / "runtime_control_cli_parser.py"
    ).read_text(encoding="utf-8")
    data_command_source = (
        root / "src" / "dynamic_firm" / "product" / "data_commands.py"
    ).read_text(encoding="utf-8")
    causal = await run_causal_workflow_evaluation()
    ci_files = tuple((root / ".github" / "workflows").glob("*.y*ml"))
    ci_contract = any(
        "unittest" in path.read_text(encoding="utf-8", errors="replace")
        and "python3.11" in path.read_text(encoding="utf-8", errors="replace")
        for path in ci_files
    )
    governance_root = root / "docs" / "60-governance"
    legal_checklist = (governance_root / "commercial-release-checklist.md").is_file()
    terms_data_notice = governance_root / "terms-and-data-notice-draft.md"
    terms_data_notice_text = (
        terms_data_notice.read_text(encoding="utf-8")
        if terms_data_notice.is_file()
        else ""
    )
    terms_data_notice_ready = all(
        marker in terms_data_notice_text
        for marker in (
            "# Noruct 이용약관·데이터 안내 및 공유 진화 동의 초안",
            "## 1. 제품 이용약관 초안",
            "## 2. 개인정보·데이터 처리 안내 초안",
            "## 3. 공유 진화 네트워크: 별도 선택 동의 초안",
            "PENDING",
        )
    )
    operator_approved, operator_evidence = _operator_release_approval(root)
    clean, clean_evidence = _git_clean(root)
    version_coherent = package_version == __version__ == sbom_version
    vendored_components = tuple(sbom.get("components", ()))

    def _license_id(component: object) -> str:
        if not isinstance(component, dict):
            return ""
        licenses = component.get("licenses", ())
        if not isinstance(licenses, list) or not licenses:
            return ""
        first = licenses[0]
        if not isinstance(first, dict):
            return ""
        license_data = first.get("license", {})
        return str(license_data.get("id", "")) if isinstance(license_data, dict) else ""

    def _has_non_mit_exception(component: object) -> bool:
        if not isinstance(component, dict):
            return False
        properties = component.get("properties", ())
        return any(
            isinstance(property_item, dict)
            and property_item.get("name") == "noruct:non-mit-exception"
            and property_item.get("value") == "docs/80-decisions/ADR-0092-optional-modern-terminal-profile.md"
            for property_item in properties
        )

    required_components = tuple(
        component
        for component in vendored_components
        if isinstance(component, dict) and component.get("scope") != "optional"
    )
    optional_components = tuple(
        component
        for component in vendored_components
        if isinstance(component, dict) and component.get("scope") == "optional"
    )
    required_licenses_are_mit = bool(required_components) and all(
        _license_id(component) == "MIT" for component in required_components
    )
    optional_non_mit = tuple(
        component for component in optional_components if _license_id(component) != "MIT"
    )
    optional_licenses_reviewed = all(
        _license_id(component) in {"BSD-2-Clause", "PSF-2.0"}
        and _has_non_mit_exception(component)
        for component in optional_non_mit
    )
    licenses_are_mit = required_licenses_are_mit and optional_licenses_reviewed
    # Command registration and dispatch are intentionally split from the
    # Product adapter. Check all three boundaries so a harmless CLI component
    # extraction cannot turn a privacy gate red while the actual command stays
    # present and confirmation-gated.
    data_commands = all(
        token in data_parser_source
        for token in ('"data"', '"export"', '"delete"', '"support-bundle"')
    ) and "run_data_command" in entrypoint_source and "Local data deletion requires --confirm" in data_command_source
    migration_contract = (
        RUNTIME_SCHEMA_VERSION == 7
        and COMPANY_STATE_SCHEMA_VERSION == 9
        and (root / "tests" / "runtime" / "test_active_job_ledger.py").is_file()
        and (root / "tests" / "company" / "test_company_kernel.py").is_file()
    )
    target_version_staged = package_version.startswith("0.1.0a")
    checks = (
        AlphaReadinessCheck(
            "causal-workflow-mechanism",
            "product-value",
            causal.passed,
            (
                f"four-job cohort passed={causal.passed}; "
                f"external model calls={causal.external_model_calls}"
            ),
        ),
        AlphaReadinessCheck(
            "version-sbom-coherence",
            "supply-chain",
            version_coherent,
            (
                f"package={package_version}; runtime={__version__}; "
                f"sbom={sbom_version}"
            ),
        ),
        AlphaReadinessCheck(
            "zero-runtime-dependencies",
            "maintenance",
            dependencies == (),
            f"declared runtime dependencies={len(dependencies)}",
        ),
        AlphaReadinessCheck(
            "mit-provenance-and-notices",
            "legal",
            (
                licenses_are_mit
                and "vendored_sources:" in source_register
                and "Permission is hereby granted" in notices
            ),
            (
                f"required components={len(required_components)}; "
                f"all required licenses MIT={required_licenses_are_mit}; "
                f"optional components={len(optional_components)}; "
                f"documented optional non-MIT exceptions={len(optional_non_mit)}; "
                f"optional exception review={optional_licenses_reviewed}"
            ),
        ),
        AlphaReadinessCheck(
            "install-and-doctor-documentation",
            "distribution",
            all(
                token in readme
                for token in (
                    "pip install .",
                    "noruct doctor",
                    "noruct setup",
                )
            ),
            "README includes install, setup, and doctor paths",
        ),
        AlphaReadinessCheck(
            "local-data-lifecycle",
            "privacy",
            data_commands,
            "export, explicit delete, and redacted support-bundle commands are present",
        ),
        AlphaReadinessCheck(
            "migration-and-rollback-contract",
            "state",
            migration_contract,
            (
                f"runtime schema={RUNTIME_SCHEMA_VERSION}; "
                f"company schema={COMPANY_STATE_SCHEMA_VERSION}"
            ),
        ),
        AlphaReadinessCheck(
            "continuous-integration",
            "release-engineering",
            ci_contract,
            f"matching CI workflows={len(ci_files)}",
        ),
        AlphaReadinessCheck(
            "commercial-release-checklist",
            "legal",
            legal_checklist,
            (
                "commercial release checklist is versioned"
                if legal_checklist
                else "commercial release checklist is absent"
            ),
        ),
        AlphaReadinessCheck(
            "terms-data-notice-review-draft",
            "legal",
            terms_data_notice_ready,
            (
                "versioned terms, data notice, and separate shared-evolution consent draft is present"
                if terms_data_notice_ready
                else "terms/data notice review draft is absent or missing required sections"
            ),
        ),
        AlphaReadinessCheck(
            "operator-release-approval",
            "operations",
            operator_approved,
            operator_evidence,
            operator_required=True,
        ),
        AlphaReadinessCheck(
            "alpha-version-staged",
            "distribution",
            target_version_staged,
            f"current version={package_version}; required prefix=0.1.0a",
        ),
        AlphaReadinessCheck(
            "clean-release-worktree",
            "release-engineering",
            clean,
            clean_evidence,
            operator_required=True,
        ),
    )
    blocking = tuple(check.name for check in checks if not check.passed)
    actions = []
    if "continuous-integration" in blocking:
        actions.append(
            "Add a pinned, license-audited CI workflow that runs the Python 3.11 full suite."
        )
    if "operator-release-approval" in blocking:
        actions.append(
            "Complete the terms/data notice review, provider terms review, and record publisher, signing, and update-channel approval."
        )
    if "alpha-version-staged" in blocking:
        actions.append(
            "After all other gates pass, stage 0.1.0a1 and regenerate matching SBOM and artifacts."
        )
    if "clean-release-worktree" in blocking:
        actions.append(
            "Create a reviewed clean release commit before building or publishing artifacts."
        )
    ready = not blocking
    return AlphaReadinessEvaluation(
        schema_version="noruct.alpha-readiness.v1",
        ready=ready,
        target_version="0.1.0a1",
        current_version=package_version,
        classification=(
            "READY_FOR_ALPHA_BUILD"
            if ready
            else "BLOCKED_OPERATOR_AND_RELEASE_ENGINEERING"
        ),
        external_model_calls=0,
        quota_consumed=False,
        checks=checks,
        blocking_checks=blocking,
        next_actions=tuple(actions),
    )
