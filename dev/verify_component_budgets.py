#!/usr/bin/env python3
"""Report first-party file budgets with a hard 1,000-line release gate."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


WARNING_LINES = 900
HARD_LIMIT_LINES = 1_000
PYTHON_SOURCE_SUFFIXES = frozenset({".py"})
SERVICE_SOURCE_SUFFIXES = frozenset(
    {".cjs", ".cts", ".js", ".jsx", ".mjs", ".mts", ".ts", ".tsx"}
)
EXCLUSIONS = {
    "src/dynamic_firm/_vendor": {
        "owner": "docs/60-governance/source-register.yaml",
        "reason": "Exact-pinned audited external source is governed by its tracked manifest and provenance review.",
        "match": "tree",
    },
    "src/dynamic_firm/vendored_sources": {
        "owner": "docs/60-governance/source-register.yaml",
        "reason": "Registered vendored source is governed by its exact source record, notices, SBOM, and tracked-file manifest.",
        "match": "tree",
    },
    "services/evolution-network-worker/worker-configuration.d.ts": {
        "owner": "docs/60-governance/source-register.yaml",
        "reason": "Exact Wrangler 4.115.0 generated binding path; freshness is enforced by npm run check:bindings.",
        "match": "exact",
    },
}


def _excluded(relative: str) -> bool:
    for configured, policy in EXCLUSIONS.items():
        if policy["match"] == "exact" and relative == configured:
            return True
        if policy["match"] == "tree" and (
            relative == configured or relative.startswith(f"{configured}/")
        ):
            return True
    return False


def _source_files(project_root: Path) -> list[Path]:
    python_root = project_root / "src" / "dynamic_firm"
    if not python_root.is_dir():
        raise ValueError(f"First-party source root was not found: {python_root}")

    paths = [
        path
        for path in python_root.rglob("*")
        if path.is_file() and path.suffix.lower() in PYTHON_SOURCE_SUFFIXES
    ]

    # Only a direct service child's own src directory is first-party production
    # source.  A recursive services/**/src search would also traverse installed
    # node_modules packages and is therefore deliberately not used here.
    services_root = project_root / "services"
    if services_root.is_dir():
        for service_root in sorted(
            path for path in services_root.iterdir() if path.is_dir()
        ):
            source_root = service_root / "src"
            if not source_root.is_dir():
                continue
            paths.extend(
                path
                for path in source_root.rglob("*")
                if path.is_file()
                and path.suffix.lower() in SERVICE_SOURCE_SUFFIXES
            )

    # Exact generated paths are inventoried explicitly so their exclusion is
    # exercised and auditable rather than merely falling outside a source root.
    paths.extend(
        project_root / relative
        for relative, policy in EXCLUSIONS.items()
        if policy["match"] == "exact" and (project_root / relative).is_file()
    )
    return sorted(set(paths))


def verify(project_root: Path) -> dict[str, Any]:
    warnings: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    checked_files: list[str] = []
    exact_excluded_files: list[str] = []
    for path in _source_files(project_root):
        relative = path.relative_to(project_root).as_posix()
        if _excluded(relative):
            if EXCLUSIONS.get(relative, {}).get("match") == "exact":
                exact_excluded_files.append(relative)
            continue
        checked_files.append(relative)
        lines = len(path.read_text(encoding="utf-8").splitlines())
        record = {"path": relative, "lines": lines}
        if lines > HARD_LIMIT_LINES:
            failures.append(record)
        elif lines > WARNING_LINES:
            warnings.append(record)
    return {
        "ok": not failures,
        "warning_lines": WARNING_LINES,
        "hard_limit_lines": HARD_LIMIT_LINES,
        "checked_file_count": len(checked_files),
        "checked_files": checked_files,
        "source_scopes": {
            "src/dynamic_firm": sorted(PYTHON_SOURCE_SUFFIXES),
            "services/*/src": sorted(SERVICE_SOURCE_SUFFIXES),
        },
        "warnings": warnings,
        "failures": failures,
        "exclusions": EXCLUSIONS,
        "exact_excluded_files": exact_excluded_files,
        "present_exclusion_paths": sorted(
            relative
            for relative in EXCLUSIONS
            if (project_root / relative).exists()
        ),
        "generated_or_migration_blanket_exception": False,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project-root", type=Path, default=Path.cwd())
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    result = verify(args.project_root.resolve())
    if args.json:
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    else:
        for item in result["warnings"]:
            print(f"WARNING component budget: {item['path']} has {item['lines']} lines")
        for item in result["failures"]:
            print(f"ERROR component budget: {item['path']} has {item['lines']} lines")
        print(
            f"Checked {result['checked_file_count']} first-party files; "
            f"warnings={len(result['warnings'])}, failures={len(result['failures'])}."
        )
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
