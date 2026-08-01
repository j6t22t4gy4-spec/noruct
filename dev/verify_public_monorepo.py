#!/usr/bin/env python3
"""Fail closed unless a materialized public Core obeys the private boundary."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess
import tomllib
from typing import Mapping


REQUIRED = (
    "CONTRIBUTING.md",
    "LICENSE",
    "PUBLIC_CORE_BOUNDARY.md",
    "README.md",
    "THIRD_PARTY_NOTICES.md",
    "public-monorepo.toml",
    "pyproject.toml",
    "src/dynamic_firm/__init__.py",
    "src/dynamic_firm/application/entrypoint_cli.py",
    "src/dynamic_firm/foundation/runtime.py",
    "src/dynamic_firm/kernel/service.py",
    "dev/run_provider_free_test_shard.py",
)
FORBIDDEN_PREFIXES = (
    ".agents/",
    ".codex/",
    ".git/",
    "docs/30-runtime/",
    "docs/50-mvp/",
    "docs/60-governance/",
    "docs/70-research/",
    "docs/80-decisions/",
    "docs/90-history/",
    "services/evolution-network-worker/",
)
FORBIDDEN_EXACT = ("AGENTS.md",)


class PublicMonorepoError(RuntimeError):
    pass


def _files(project: Path) -> tuple[str, ...]:
    tracked = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=project,
        check=False,
        capture_output=True,
    )
    if tracked.returncode == 0:
        return tuple(
            sorted(item.decode("utf-8") for item in tracked.stdout.split(b"\0") if item)
        )
    return tuple(
        sorted(
            path.relative_to(project).as_posix()
            for path in project.rglob("*")
            if path.is_file()
        )
    )


def verify(root: Path) -> Mapping[str, object]:
    project = root.expanduser().resolve()
    if not project.is_dir():
        raise PublicMonorepoError("public monorepo root does not exist")
    files = _files(project)
    missing = tuple(path for path in REQUIRED if path not in files)
    forbidden = tuple(
        path
        for path in files
        if path in FORBIDDEN_EXACT or path.startswith(FORBIDDEN_PREFIXES)
    )
    if missing:
        raise PublicMonorepoError("missing public Core files: " + ", ".join(missing))
    if forbidden:
        raise PublicMonorepoError("private paths escaped into public Core: " + ", ".join(forbidden))
    with (project / "public-monorepo.toml").open("rb") as stream:
        manifest = tomllib.load(stream)
    if manifest.get("publication_state") != "SOURCE_PUBLICATION_AUTHORIZED":
        raise PublicMonorepoError("public Core source publication is not authorized")
    if any(path.endswith((".db", ".sqlite", ".pem", ".key", ".p12")) for path in files):
        raise PublicMonorepoError("credential/state-shaped file escaped into public Core")
    return {
        "schema": "noruct.public-monorepo-verification.v1",
        "ok": True,
        "file_count": len(files),
        "hosted_service_present": False,
        "private_evidence_present": False,
        "source_publication_authorized": True,
        "artifact_release_authorized": False,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-root", type=Path, required=True)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    try:
        receipt = verify(args.project_root)
    except PublicMonorepoError as exc:
        parser.error(str(exc))
    print(json.dumps(receipt, sort_keys=True) if args.json else receipt)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
