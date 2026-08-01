from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sqlite3
import subprocess
import uuid
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Awaitable, Callable, Mapping

from dynamic_firm.company.models import content_digest
from dynamic_firm.providers.codex_exec import CodexExecProvider, CodexLoginStatus
from dynamic_firm.runtime.models import to_primitive, utc_now
from dynamic_firm.runtime.ports import ModelProviderError, OperationCancelled

from .closed_loop import (
    LiveCodingEvaluationConfig,
    LiveCodingEvaluationRecord,
    live_coding_record_to_json,
    run_closed_loop_evaluation,
    run_live_coding_evaluation,
)
from .firm_value import (
    FirmValueManifest,
    FirmValueReport,
    QUALITY_GAIN_THRESHOLD,
    aggregate_firm_value_records,
    create_firm_value_manifest,
    firm_value_expected_runs,
    firm_value_manifest_to_json,
    load_firm_value_manifest,
    validate_firm_value_record,
    wheel_distribution_sha256,
)


CAMPAIGN_PREFLIGHT_SCHEMA = "noruct.firm-value-campaign-preflight.v1"
CAMPAIGN_STATUS_SCHEMA = "noruct.firm-value-campaign-status.v1"
CAMPAIGN_LEDGER_SCHEMA = "noruct.firm-value-campaign-ledger.v1"
CAMPAIGN_COMPARISON_SCHEMA = "noruct.firm-value-campaign-comparison.v1"
SOURCE_SNAPSHOT_PREFIX = "snapshot-sha256:"
_MAX_SOURCE_FILES = 8_000
_MAX_SOURCE_BYTES = 100_000_000
_SNAPSHOT_TOP_LEVEL = (
    "LICENSE",
    "README.md",
    "THIRD_PARTY_NOTICES.md",
    "pyproject.toml",
)



def _canonical(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_private(path: Path, payload: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(payload + ("" if payload.endswith("\n") else "\n"), encoding="utf-8")
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
    return path


def source_snapshot_revision(root: str | Path) -> str:
    """Hash the bounded first-party source and evaluation inputs.

    Generated campaign data, build output, VCS metadata and caches are excluded,
    so a dirty workspace can still be frozen explicitly without inventing a
    commit identity.
    """

    base = Path(root).expanduser().resolve()
    if not base.is_dir() or base.is_symlink():
        raise ValueError(f"Campaign source root must be a regular directory: {base}")
    paths: list[Path] = []
    for name in _SNAPSHOT_TOP_LEVEL:
        candidate = base / name
        if candidate.is_file():
            paths.append(candidate)
    for subtree in (base / "src", base / "tests"):
        if not subtree.is_dir() or subtree.is_symlink():
            raise ValueError(f"Campaign source snapshot requires {subtree}")
        paths.extend(path for path in subtree.rglob("*") if path.is_file() or path.is_symlink())
    filtered = tuple(
        sorted(
            (
                path
                for path in paths
                if "__pycache__" not in path.parts
                and path.suffix not in {".pyc", ".pyo"}
                and path.name != ".DS_Store"
            ),
            key=lambda path: path.relative_to(base).as_posix(),
        )
    )
    if not filtered or len(filtered) > _MAX_SOURCE_FILES:
        raise ValueError("Campaign source snapshot file count is outside the bounded contract")
    digest = hashlib.sha256(b"noruct.source-snapshot.v1\0")
    total = 0
    for path in filtered:
        if path.is_symlink():
            raise ValueError(f"Campaign source snapshot rejects symlinks: {path}")
        raw = path.read_bytes()
        total += len(raw)
        if total > _MAX_SOURCE_BYTES:
            raise ValueError("Campaign source snapshot exceeds 100 MB")
        relative = path.relative_to(base).as_posix().encode("utf-8")
        digest.update(relative)
        digest.update(b"\0")
        digest.update(str(len(raw)).encode("ascii"))
        digest.update(b"\0")
        digest.update(raw)
        digest.update(b"\0")
    return SOURCE_SNAPSHOT_PREFIX + digest.hexdigest()


def probe_codex_structured_output(command: str) -> tuple[str | None, bool, str]:
    """Inspect the documented local CLI help without making a model request."""

    executable = CodexExecProvider.resolve_executable(command)
    if executable is None:
        return None, False, "executable-not-found"
    environment = {
        key: value
        for key, value in os.environ.items()
        if key in {"HOME", "LANG", "LC_ALL", "LC_CTYPE", "PATH", "SYSTEMROOT"}
    }
    try:
        result = subprocess.run(
            [executable, "exec", "--help"],
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            check=False,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return executable, False, "exec-help-unavailable"
    output = result.stdout[:128_000].decode("utf-8", errors="replace")
    required = ("--json", "--sandbox", "--output-schema", "--output-last-message")
    missing = tuple(flag for flag in required if flag not in output)
    return (
        executable,
        result.returncode == 0 and not missing,
        "supported" if not missing else "missing:" + ",".join(missing),
    )



