"""Private bridge to the exact vendored workspace file-operation implementation.

The source file-operation implementation is intentionally run in a short
lived subprocess.  It keeps its upstream import names and shell-oriented
backend contract private while the caller retains the Noruct workspace root,
ActionPolicy, output bound and cancellation authority.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Mapping


_UPSTREAM_ROOT = (
    Path(__file__).resolve().parents[1] / "_vendor" / "hermes_agent" / "upstream"
)
_PROGRAM = r'''
import json
import os
import re
import subprocess
import sys
import types
from pathlib import Path

# The source file backend imports this module only for the platform-specific
# shell-path helper.  Provide its exact narrow behavior as an inert private
# import seam, rather than importing the upstream local terminal environment
# (which would bring host-process, interrupt and persistent-session authority
# into this disposable child).  `LocalEnvironment` is intentionally a marker
# only: LocalWorkspaceEnvironment below is not an instance, so optional LSP
# enrichment in the source backend stays disabled.
_source_local = types.ModuleType("tools.environments.local")
_source_is_windows = sys.platform == "win32"


def _source_windows_to_msys_path(path):
    if not _source_is_windows or not path:
        return path
    match = re.match(r'^([a-zA-Z]):[\\/]*(.*)$', path)
    if not match:
        return path
    drive = match.group(1).lower()
    tail = (match.group(2) or "").replace("\\", "/").lstrip("/")
    return f"/{drive}/{tail}" if tail else f"/{drive}/"


def _source_bash_safe_path(path):
    if not _source_is_windows or not path:
        return path
    path = _source_windows_to_msys_path(path)
    return path.replace("\\", "/")


class _SourceLocalEnvironment:
    pass


_source_local._bash_safe_path = _source_bash_safe_path
_source_local.LocalEnvironment = _SourceLocalEnvironment
sys.modules["tools.environments.local"] = _source_local

from tools.file_operations import ShellFileOperations


def _operation_record(operation):
    """Return the minimum parent-auditable shape of one parsed V4A operation."""
    return {
        "operation": operation.operation.value,
        "path": operation.file_path,
        "new_path": operation.new_path,
        "hunk_count": len(operation.hunks),
        "added_bytes": sum(
            len(line.content.encode("utf-8")) + 1
            for hunk in operation.hunks
            for line in hunk.lines
            if line.prefix == "+"
        ),
    }


class LocalWorkspaceEnvironment:
    def __init__(self, root):
        self.cwd = str(root)

    def execute(self, command, cwd=None, timeout=None, stdin_data=None):
        try:
            # The exact vendored search backend deliberately uses
            # ``set -o pipefail`` so rg/grep errors survive its bounded head
            # pipeline.  POSIX ``subprocess`` otherwise selects /bin/sh,
            # which is dash on Ubuntu and rejects that contract before the
            # search starts.  Keep this compatibility choice in the private
            # Noruct adapter instead of rewriting the pinned upstream source.
            shell_executable = None if _source_is_windows else "/bin/bash"
            completed = subprocess.run(
                command,
                shell=True,
                executable=shell_executable,
                cwd=str(cwd or self.cwd),
                input=stdin_data,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                timeout=max(1, int(timeout or 30)),
                env={
                    "HOME": os.environ["HOME"],
                    "HERMES_HOME": os.environ["HERMES_HOME"],
                    "HERMES_WRITE_SAFE_ROOT": os.environ["HERMES_WRITE_SAFE_ROOT"],
                    "LANG": os.environ.get("LANG", "C.UTF-8"),
                    "LC_ALL": os.environ.get("LC_ALL", "C.UTF-8"),
                    "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                },
            )
            return {"output": completed.stdout or "", "returncode": completed.returncode}
        except subprocess.TimeoutExpired as exc:
            return {
                "output": (exc.stdout or "") + "\n[Command timed out after %ss]" % int(timeout or 30),
                "returncode": 124,
            }


request = json.loads(sys.stdin.read())
root = Path(request["root"]).resolve()
ops = ShellFileOperations(LocalWorkspaceEnvironment(root), cwd=str(root))
if request["operation"] == "read":
    result = ops.read_file(
        request["path"],
        offset=request["offset"],
        limit=request["limit"],
    )
    result_data = result.to_dict()
elif request["operation"] == "search":
    result = ops.search(
        pattern=request["pattern"],
        path=request["path"],
        target=request["target"],
        file_glob=request.get("file_glob"),
        limit=request["limit"],
        offset=request["offset"],
        output_mode=request["output_mode"],
        context=request["context"],
    )
    result_data = result.to_dict(densify=True)
elif request["operation"] == "patch_replace":
    result = ops.patch_replace(
        request["path"],
        request["old_string"],
        request["new_string"],
        request["replace_all"],
    )
    result_data = result.to_dict()
elif request["operation"] == "write":
    result = ops.write_file(
        request["path"],
        request["content"],
    )
    result_data = result.to_dict()
elif request["operation"] == "delete":
    result_data = ops.delete_file(request["path"]).to_dict()
elif request["operation"] == "move":
    result_data = ops.move_file(request["path"], request["destination"]).to_dict()
elif request["operation"] == "parse_v4a":
    from tools.patch_parser import parse_v4a_patch

    operations, error = parse_v4a_patch(request["patch"])
    result_data = {
        "operations": [_operation_record(operation) for operation in operations],
        "error": error,
    }
elif request["operation"] == "patch_v4a":
    result = ops.patch_v4a(request["patch"])
    result_data = result.to_dict()
else:
    raise ValueError("Unsupported workspace operation")
print(json.dumps(result_data, ensure_ascii=False, sort_keys=True))
'''


def _run_workspace_operation(
    *,
    root: Path,
    operation: str,
    path: str,
    limit: int,
    offset: int,
    pattern: str = "",
    target: str = "content",
    file_glob: str | None = None,
    output_mode: str = "content",
    context: int = 0,
    old_string: str = "",
    new_string: str = "",
    replace_all: bool = False,
    content: str = "",
    patch: str = "",
    destination: str = "",
    timeout_seconds: float = 15.0,
) -> Mapping[str, Any]:
    """Run one exact source operation after parent root/path authorization."""

    workspace = root.resolve()
    if not workspace.is_dir() or not _UPSTREAM_ROOT.is_dir():
        raise ValueError("Vendored workspace operation source is unavailable")
    environment = {
        "HOME": str(workspace),
        "HERMES_HOME": str(workspace / ".noruct-source-home"),
        "HERMES_WRITE_SAFE_ROOT": str(workspace),
        "LANG": os.environ.get("LANG", "C.UTF-8"),
        "LC_ALL": os.environ.get("LC_ALL", "C.UTF-8"),
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONPATH": str(_UPSTREAM_ROOT),
    }
    request = {
        "operation": operation,
        "root": str(workspace),
        "path": path,
        "pattern": pattern,
        "target": target,
        "file_glob": file_glob,
        "limit": limit,
        "offset": offset,
        "output_mode": output_mode,
        "context": context,
        "old_string": old_string,
        "new_string": new_string,
        "replace_all": replace_all,
        "content": content,
        "patch": patch,
        "destination": destination,
    }
    try:
        completed = subprocess.run(
            [sys.executable, "-c", _PROGRAM],
            input=json.dumps(request, ensure_ascii=False),
            text=True,
            cwd=str(workspace),
            env=environment,
            capture_output=True,
            timeout=max(1.0, timeout_seconds),
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise ValueError("Workspace operation exceeded its bounded execution time") from exc
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "source operation failed").strip()
        raise ValueError(f"Vendored workspace operation failed: {detail[:240]}")
    try:
        result = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise ValueError("Vendored workspace operation returned an invalid result") from exc
    if not isinstance(result, dict):
        raise ValueError("Vendored workspace operation returned an invalid record")
    return result


def search_workspace(
    *,
    root: Path,
    path: str,
    pattern: str,
    target: str,
    file_glob: str | None,
    limit: int,
    offset: int,
    output_mode: str,
    context: int,
    timeout_seconds: float = 15.0,
) -> Mapping[str, Any]:
    """Run exact source search after the parent has authorized the root/path."""

    return _run_workspace_operation(
        root=root,
        operation="search",
        path=path,
        pattern=pattern,
        target=target,
        file_glob=file_glob,
        limit=limit,
        offset=offset,
        output_mode=output_mode,
        context=context,
        timeout_seconds=timeout_seconds,
    )


def read_workspace(
    *,
    root: Path,
    path: str,
    offset: int = 1,
    limit: int = 500,
    timeout_seconds: float = 15.0,
) -> Mapping[str, Any]:
    """Run exact source line-numbered file reading inside an approved root."""

    return _run_workspace_operation(
        root=root,
        operation="read",
        path=path,
        limit=limit,
        offset=offset,
        timeout_seconds=timeout_seconds,
    )


def patch_workspace(
    *,
    root: Path,
    path: str,
    old_string: str,
    new_string: str,
    replace_all: bool = False,
    timeout_seconds: float = 15.0,
) -> Mapping[str, Any]:
    """Run exact source fuzzy replacement after parent mutation authorization."""

    return _run_workspace_operation(
        root=root,
        operation="patch_replace",
        path=path,
        limit=1,
        offset=1,
        old_string=old_string,
        new_string=new_string,
        replace_all=replace_all,
        timeout_seconds=timeout_seconds,
    )


def write_workspace(
    *,
    root: Path,
    path: str,
    content: str,
    timeout_seconds: float = 15.0,
) -> Mapping[str, Any]:
    """Run exact source atomic write after parent mutation authorization."""

    return _run_workspace_operation(
        root=root,
        operation="write",
        path=path,
        limit=1,
        offset=1,
        content=content,
        timeout_seconds=timeout_seconds,
    )


def parse_workspace_v4a_patch(
    *,
    root: Path,
    patch: str,
    timeout_seconds: float = 15.0,
) -> Mapping[str, Any]:
    """Parse a V4A patch with the exact source parser, without mutation."""

    return _run_workspace_operation(
        root=root,
        operation="parse_v4a",
        path="",
        limit=1,
        offset=1,
        patch=patch,
        timeout_seconds=timeout_seconds,
    )


def apply_workspace_v4a_patch(
    *,
    root: Path,
    patch: str,
    timeout_seconds: float = 20.0,
) -> Mapping[str, Any]:
    """Apply a parent-authorized V4A patch through the exact source engine."""

    return _run_workspace_operation(
        root=root,
        operation="patch_v4a",
        path="",
        limit=1,
        offset=1,
        patch=patch,
        timeout_seconds=timeout_seconds,
    )


def delete_workspace(
    *,
    root: Path,
    path: str,
    timeout_seconds: float = 15.0,
) -> Mapping[str, Any]:
    """Delete one parent-authorized regular workspace file with source behavior."""

    return _run_workspace_operation(
        root=root,
        operation="delete",
        path=path,
        limit=1,
        offset=1,
        timeout_seconds=timeout_seconds,
    )


def move_workspace(
    *,
    root: Path,
    source: str,
    destination: str,
    timeout_seconds: float = 15.0,
) -> Mapping[str, Any]:
    """Move one parent-authorized workspace file with source behavior."""

    return _run_workspace_operation(
        root=root,
        operation="move",
        path=source,
        destination=destination,
        limit=1,
        offset=1,
        timeout_seconds=timeout_seconds,
    )
