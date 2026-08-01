"""Private bridge to the exact vendored local skill manager.

The source manager owns useful filesystem invariants (frontmatter validation,
atomic writes, support-file allowlists, fuzzy patches and non-recursive delete)
but its profile home, registry, approval and telemetry state are not Noruct
authority.  It therefore runs in a disposable subprocess with those upstream
integration points replaced by inert shims.  The caller owns the chosen skill
root, confirmation, receipts and product-facing errors.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path
from typing import Any, Mapping


_UPSTREAM_ROOT = (
    Path(__file__).resolve().parents[1] / "_vendor" / "hermes_agent" / "upstream"
)
_ACTIONS = frozenset({"create", "edit", "patch", "delete", "write_file", "remove_file"})
_SKILL_NAME = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_PROGRAM = r'''
import json
import os
import sys
import types
from pathlib import Path

request = json.loads(sys.stdin.read())
skills_root = Path(request["skills_root"]).resolve()
skills_root.mkdir(parents=True, exist_ok=True)

# The exact source imports its profile/config/registry helpers at module load.
# Supply only the narrow contracts required for a local isolated skill root;
# no source registry, approval system, telemetry or profile state is allowed
# to become a Noruct authority.
constants = types.ModuleType("hermes_constants")
constants.get_hermes_home = lambda: skills_root.parent
constants.display_hermes_home = lambda: str(skills_root.parent)
constants.get_config_path = lambda: skills_root.parent / "config.yaml"
constants.get_skills_dir = lambda: skills_root
constants.get_default_hermes_root = lambda: skills_root.parent
sys.modules["hermes_constants"] = constants

utils = types.ModuleType("utils")
utils.atomic_replace = os.replace
utils.is_truthy_value = lambda value, default=False: bool(value) if value is not None else default
utils.env_var_enabled = lambda _name: False
sys.modules["utils"] = utils

config = types.ModuleType("hermes_cli.config")
config.cfg_get = lambda value, *keys: value.get(keys[0], {}) if keys and isinstance(value, dict) else None
# Always enable the exact source static scanner for management writes.  It is
# a local scan only; it neither installs nor executes skill content.
config.load_config = lambda: {"skills": {"guard_agent_created": True}}
sys.modules["hermes_cli.config"] = config

registry = types.ModuleType("tools.registry")
registry.registry = types.SimpleNamespace(register=lambda **_kwargs: None)
registry.tool_error = lambda message, success=False: json.dumps({"success": success, "error": message})
sys.modules["tools.registry"] = registry

skill_utils = types.ModuleType("agent.skill_utils")
skill_utils.get_all_skills_dirs = lambda: [skills_root]
skill_utils.is_excluded_skill_path = lambda _path: False
sys.modules["agent.skill_utils"] = skill_utils

file_safety = types.ModuleType("agent.file_safety")
file_safety._resolve_active_profile_name = lambda: "noruct"
sys.modules["agent.file_safety"] = file_safety

write_approval = types.ModuleType("tools.write_approval")
write_approval.SKILLS = "skills"
write_approval.evaluate_gate = lambda _scope: types.SimpleNamespace(allow=True, blocked=False, message="")
sys.modules["tools.write_approval"] = write_approval

provenance = types.ModuleType("tools.skill_provenance")
provenance.is_background_review = lambda: False
sys.modules["tools.skill_provenance"] = provenance

import tools.skill_manager_tool as manager
manager.SKILLS_DIR = skills_root
result = manager.skill_manage(**request["arguments"])
print(result)
'''


def _qualified_worker_python() -> str:
    """Use the same minimal local worker profile as the active Employee Runtime."""

    candidates = [os.environ.get("NORUCT_EMPLOYEE_RUNTIME_PYTHON", ""), sys.executable]
    candidates.extend(
        candidate
        for command in ("python3.11", "python3", "python")
        if (candidate := shutil.which(command))
    )
    seen: set[str] = set()
    for candidate in candidates:
        if not candidate:
            continue
        resolved = str(Path(candidate).expanduser().resolve())
        if resolved in seen or not os.access(resolved, os.X_OK):
            continue
        seen.add(resolved)
        try:
            probe = subprocess.run(
                [resolved, "-c", "from importlib.metadata import version; raise SystemExit(0 if version('PyYAML') == '6.0.3' else 1)"],
                capture_output=True,
                check=False,
                timeout=3,
            )
        except (OSError, subprocess.TimeoutExpired):
            continue
        if probe.returncode == 0:
            return resolved
    raise ValueError(
        "Managed skill operation requires the Noruct worker profile (PyYAML==6.0.3); no legacy Python fallback exists"
    )


def _check_root(root: Path) -> Path:
    candidate = Path(root).expanduser()
    if candidate.exists() and candidate.is_symlink():
        raise ValueError("Managed skill root cannot be a symbolic link")
    candidate.mkdir(parents=True, exist_ok=True)
    resolved = candidate.resolve()
    if not resolved.is_dir():
        raise ValueError("Managed skill root must be a directory")
    try:
        for child in resolved.iterdir():
            if child.is_symlink():
                raise ValueError("Managed skill root cannot contain symbolic-link entries")
    except OSError as exc:
        raise ValueError("Managed skill root cannot be inspected") from exc
    return resolved


def _tree_receipt(root: Path) -> Mapping[str, Any]:
    """Return a bounded content-addressed receipt without reading arbitrary size."""

    records: list[tuple[str, int, str]] = []
    try:
        entries = sorted(root.rglob("*"))
    except OSError as exc:
        raise ValueError("Managed skill root cannot be enumerated") from exc
    if len(entries) > 256:
        raise ValueError("Managed skill root exceeds the 256-entry receipt limit")
    for item in entries:
        if item.is_symlink():
            raise ValueError("Managed skill root cannot contain symbolic-link entries")
        if not item.is_file():
            continue
        try:
            size = item.stat().st_size
        except OSError as exc:
            raise ValueError("Managed skill file cannot be inspected") from exc
        if size > 1_048_576:
            raise ValueError("Managed skill file exceeds the 1 MiB receipt limit")
        try:
            digest = hashlib.sha256(item.read_bytes()).hexdigest()
            relative = item.relative_to(root).as_posix()
        except (OSError, ValueError) as exc:
            raise ValueError("Managed skill file cannot be read safely") from exc
        records.append((relative, size, digest))
    encoded = json.dumps(records, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return {
        "entry_count": len(records),
        "tree_sha256": hashlib.sha256(encoded).hexdigest(),
    }


def managed_skill_receipt(*, skills_root: Path, name: str) -> Mapping[str, Any]:
    """Identify one source-managed skill and return its bounded tree receipt.

    This does not read instructions into a Company prompt and cannot install or
    execute a skill.  It only gives a later, explicitly reviewed semantic
    Artifact a stable reference to the exact local manager-owned tree from
    which it was reviewed.  The upstream manager permits one category level;
    searching by the manager's stable directory name keeps that source rule
    while refusing ambiguity created outside the managed command.
    """

    name = _managed_skill_name(name)
    root = _check_root(skills_root)
    matches: list[Path] = []
    try:
        candidates = sorted(root.rglob("SKILL.md"))
    except OSError as exc:
        raise ValueError("Managed skill root cannot be enumerated") from exc
    if len(candidates) > 256:
        raise ValueError("Managed skill root exceeds the 256-entry receipt limit")
    for skill_file in candidates:
        try:
            if skill_file.is_symlink() or not skill_file.is_file():
                continue
            skill_dir = skill_file.parent.resolve()
            skill_dir.relative_to(root)
        except (OSError, ValueError):
            continue
        if skill_dir.name == name:
            matches.append(skill_dir)
    if not matches:
        raise ValueError("Managed skill was not found in the explicit skill root")
    if len(matches) != 1:
        raise ValueError("Managed skill name is ambiguous inside the explicit skill root")
    skill_dir = matches[0]
    return {
        "name": name,
        "relative_path": skill_dir.relative_to(root).as_posix(),
        "receipt": _tree_receipt(skill_dir),
        "skill_dir": str(skill_dir),
    }


def _managed_skill_name(value: object) -> str:
    if not isinstance(value, str) or _SKILL_NAME.fullmatch(value.strip()) is None:
        raise ValueError("Managed skill name must be a lower-case bounded identifier")
    return value.strip()


def _import_backup_root(skills_root: Path) -> Path:
    """Return an adjacent private backup root without polluting skill discovery."""

    candidate = skills_root.parent / f".{skills_root.name}.noruct-import-backups"
    if candidate.exists() and candidate.is_symlink():
        raise ValueError("Managed skill import backup root cannot be a symbolic link")
    candidate.mkdir(parents=True, exist_ok=True)
    return candidate.resolve()


def import_local_skill(
    *,
    skills_root: Path,
    source_dir: Path,
    name: str,
    replace: bool = False,
) -> Mapping[str, Any]:
    """Copy one explicitly selected audited skill using source-sync invariants.

    This is deliberately a local user-owned import, not the upstream bundled
    or hub sync authority.  It follows the useful source invariants: bounded
    tree hashing, complete staged copy, atomic replacement, and a recoverable
    backup for explicit rollback.  The static scanner is rerun before any
    target mutation.
    """

    name = _managed_skill_name(name)
    if not isinstance(replace, bool):
        raise ValueError("Managed skill import replace must be a boolean")
    root = _check_root(skills_root)
    source = Path(source_dir).expanduser()
    if source.exists() and source.is_symlink():
        raise ValueError("Managed skill import source cannot be a symbolic link")
    try:
        source = source.resolve(strict=True)
    except OSError as exc:
        raise ValueError("Managed skill import source cannot be resolved") from exc
    if not source.is_dir() or not (source / "SKILL.md").is_file() or (source / "SKILL.md").is_symlink():
        raise ValueError("Managed skill import source must be a directory containing SKILL.md")
    source_receipt = _tree_receipt(source)
    from dynamic_firm.foundation.hermes_skill_guard import audit_user_skill

    audit = audit_user_skill(source)
    if audit.get("verdict") == "dangerous":
        raise ValueError("Managed skill import is blocked by the static skill scanner")
    target = root / name
    if target.exists() and target.is_symlink():
        raise ValueError("Managed skill import target cannot be a symbolic link")
    if target.exists() and not replace:
        raise ValueError("Managed skill import target already exists; use explicit replace")
    operation_id = f"skill-import-{uuid.uuid4().hex[:16]}"
    backups = _import_backup_root(root)
    backup = backups / operation_id / name
    staged_root = Path(tempfile.mkdtemp(prefix=".noruct-skill-import-", dir=root.parent))
    staged = staged_root / name
    prior_receipt: Mapping[str, Any] | None = _tree_receipt(target) if target.exists() else None
    try:
        shutil.copytree(source, staged, symlinks=False)
        staged_receipt = _tree_receipt(staged)
        if staged_receipt["tree_sha256"] != source_receipt["tree_sha256"]:
            raise ValueError("Managed skill import staging receipt does not match its source")
        if target.exists():
            backup.parent.mkdir(parents=True, exist_ok=True)
            os.replace(target, backup)
        try:
            os.replace(staged, target)
        except BaseException:
            if backup.exists() and not target.exists():
                os.replace(backup, target)
            raise
    finally:
        shutil.rmtree(staged_root, ignore_errors=True)
    imported_receipt = _tree_receipt(target)
    return {
        "schema": "noruct.managed-skill-import-receipt.v1",
        "operation_id": operation_id,
        "name": name,
        "source_tree_sha256": source_receipt["tree_sha256"],
        "imported_tree_sha256": imported_receipt["tree_sha256"],
        "prior_tree_sha256": prior_receipt["tree_sha256"] if prior_receipt else "",
        "backup_path": str(backup) if prior_receipt else "",
        "scanner_verdict": audit.get("verdict"),
        "rollback": "explicit_receipt_bound_only",
    }


def rollback_local_skill_import(*, skills_root: Path, receipt: Mapping[str, Any]) -> Mapping[str, Any]:
    """Undo one untouched local import while preserving a rollback backup."""

    if not isinstance(receipt, Mapping) or receipt.get("schema") != "noruct.managed-skill-import-receipt.v1":
        raise ValueError("Managed skill import rollback requires a valid local receipt")
    name = _managed_skill_name(receipt.get("name"))
    operation_id = receipt.get("operation_id")
    imported_digest = receipt.get("imported_tree_sha256")
    backup_path = receipt.get("backup_path", "")
    if not isinstance(operation_id, str) or not operation_id.startswith("skill-import-"):
        raise ValueError("Managed skill import receipt has an invalid operation identity")
    if not isinstance(imported_digest, str) or len(imported_digest) != 64:
        raise ValueError("Managed skill import receipt has an invalid imported digest")
    root = _check_root(skills_root)
    target = root / name
    if not target.is_dir() or _tree_receipt(target)["tree_sha256"] != imported_digest:
        raise ValueError("Managed skill import rollback refuses a changed or missing target")
    backups = _import_backup_root(root)
    reverted = backups / operation_id / "reverted-import" / name
    reverted.parent.mkdir(parents=True, exist_ok=True)
    os.replace(target, reverted)
    if backup_path:
        backup = Path(str(backup_path)).expanduser().resolve()
        expected_backup = (backups / operation_id / name).resolve()
        try:
            backup.relative_to(backups)
        except ValueError as exc:
            raise ValueError("Managed skill import receipt backup is outside the owned backup root") from exc
        if backup != expected_backup:
            os.replace(reverted, target)
            raise ValueError("Managed skill import receipt backup does not match its operation")
        if not backup.is_dir():
            os.replace(reverted, target)
            raise ValueError("Managed skill import backup is unavailable")
        os.replace(backup, target)
        restored = "prior_skill_restored"
    else:
        restored = "import_removed"
    return {
        "operation_id": operation_id,
        "name": name,
        "status": restored,
        "reverted_tree_sha256": _tree_receipt(reverted)["tree_sha256"],
        "current_tree_sha256": _tree_receipt(target)["tree_sha256"] if target.exists() else "",
    }


def manage_local_skill(
    *,
    skills_root: Path,
    action: str,
    name: str,
    content: str | None = None,
    category: str | None = None,
    file_path: str | None = None,
    file_content: str | None = None,
    old_string: str | None = None,
    new_string: str | None = None,
    replace_all: bool = False,
    absorbed_into: str | None = None,
    timeout_seconds: float = 20.0,
) -> Mapping[str, Any]:
    """Run one exact source manager operation in a parent-owned local root."""

    if action not in _ACTIONS:
        raise ValueError("Unsupported managed skill action")
    if not isinstance(name, str) or not name.strip() or len(name) > 64:
        raise ValueError("Managed skill name must be a non-empty bounded string")
    for label, value, maximum in (
        ("content", content, 100_000),
        ("file_content", file_content, 1_048_576),
        ("old_string", old_string, 100_000),
        ("new_string", new_string, 100_000),
    ):
        if value is not None and (not isinstance(value, str) or len(value.encode("utf-8")) > maximum):
            raise ValueError(f"Managed skill {label} exceeds its byte limit")
    if not isinstance(replace_all, bool):
        raise ValueError("Managed skill replace_all must be a boolean")
    root = _check_root(skills_root)
    before = _tree_receipt(root)
    arguments = {
        "action": action,
        "name": name,
        "content": content,
        "category": category,
        "file_path": file_path,
        "file_content": file_content,
        "old_string": old_string,
        "new_string": new_string,
        "replace_all": replace_all,
        "absorbed_into": absorbed_into,
    }
    environment = {
        "HOME": str(root.parent),
        "HERMES_HOME": str(root.parent),
        "LANG": os.environ.get("LANG", "C.UTF-8"),
        "LC_ALL": os.environ.get("LC_ALL", "C.UTF-8"),
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONPATH": str(_UPSTREAM_ROOT),
    }
    try:
        completed = subprocess.run(
            [_qualified_worker_python(), "-c", _PROGRAM],
            input=json.dumps({"skills_root": str(root), "arguments": arguments}, ensure_ascii=False),
            text=True,
            cwd=str(root),
            env=environment,
            capture_output=True,
            timeout=max(1.0, timeout_seconds),
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise ValueError("Managed skill operation exceeded its bounded execution time") from exc
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "source manager failed").strip()
        raise ValueError(f"Vendored skill manager failed: {detail[:240]}")
    try:
        source_result = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise ValueError("Vendored skill manager returned an invalid result") from exc
    if not isinstance(source_result, dict):
        raise ValueError("Vendored skill manager returned an invalid record")
    after = _tree_receipt(root)
    return {
        "action": action,
        "name": name,
        "skills_root": str(root),
        "before": before,
        "after": after,
        "changed": before["tree_sha256"] != after["tree_sha256"],
        "source": source_result,
    }
