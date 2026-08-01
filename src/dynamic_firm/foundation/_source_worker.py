"""Private subprocess entry for the shipped employee-runtime smoke.

This module exercises the active Noruct Hermes fork and JSONL execution
boundary as :mod:`_employee_worker`.  It is not a broad upstream CLI
qualification: optional provider, plugin, MCP, TUI and development effects
remain parent-owned or disabled.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

from .protocol import FoundationFrame, FrameSequence, decode_frame, encode_frame


def _package_root() -> Path:
    return Path(__file__).parents[1].resolve()


def _project_worker_code(home: Path) -> Path:
    """Expose only the Noruct package to the isolated employee worker."""

    projection = home / "noruct-code"
    projection.mkdir(parents=True, exist_ok=True, mode=0o700)
    package_link = projection / "dynamic_firm"
    expected = _package_root()
    if package_link.is_symlink():
        if package_link.resolve() != expected:
            raise RuntimeError("foundation smoke code projection changed")
    elif package_link.exists():
        raise RuntimeError("foundation smoke code projection is not isolated")
    else:
        package_link.symlink_to(expected, target_is_directory=True)
    return projection


def _run(home: Path, *, worker_python: str) -> dict[str, object]:
    if os.environ.get("NORUCT_FOUNDATION_SOURCE_WORKER") != "1":
        raise RuntimeError("foundation worker is private and must be launched by Noruct")

    home.mkdir(parents=True, exist_ok=True, mode=0o700)
    code_projection = _project_worker_code(home)
    worker_home = home / "state"
    fork_root = _package_root() / "_vendor" / "hermes_agent" / "upstream"
    if not fork_root.is_dir():
        raise RuntimeError("active Noruct Hermes fork is missing")
    manifest = json.loads(
        (fork_root.parent / "UPSTREAM_MANIFEST.json").read_text(encoding="utf-8")
    )
    fork_file_count = int(manifest.get("file_count") or 0)
    if fork_file_count <= 0:
        raise RuntimeError("active Noruct Hermes fork manifest has no file count")
    if not os.access(worker_python, os.X_OK):
        raise RuntimeError("employee runtime Python is not executable")

    # Match the actual runtime process boundary.  The child inherits neither
    # credentials nor a caller-controlled PYTHONPATH, and only sees Noruct's
    # package projection plus the verified active fork selected by the parent.
    env = {
        "HOME": str(home),
        "HERMES_DISABLE_LAZY_INSTALLS": "1",
        "HERMES_HOME": str(worker_home),
        "HERMES_PYTHON_SRC_ROOT": str(fork_root),
        "LANG": os.environ.get("LANG", "C.UTF-8"),
        "LC_ALL": os.environ.get("LC_ALL", "C.UTF-8"),
        "NO_COLOR": "1",
        "NORUCT_FOUNDATION_EXECUTION_WORKER": "1",
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONPATH": str(code_projection),
        "TMPDIR": os.environ.get("TMPDIR", "/tmp"),
    }
    process = subprocess.Popen(
        [worker_python, "-m", "dynamic_firm.foundation._employee_worker"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        cwd=str(home),
    )
    assert process.stdin is not None and process.stdout is not None

    run_id = "foundation-smoke"
    outbound = FrameSequence()
    inbound = FrameSequence()

    def send(frame_type: str, payload: dict[str, object]) -> None:
        frame = FoundationFrame(frame_type, run_id, outbound.next(run_id), payload)
        process.stdin.write(encode_frame(frame))
        process.stdin.flush()

    send(
        "execute",
        {
            "conversation_history": [],
            "initial_model_call_index": 0,
            "max_model_calls": 1,
            "model_profile": "foundation-smoke",
            "session_id": run_id,
            "system_message": "You are the private Noruct employee runtime.",
            "task_id": run_id,
            "tools": [],
            "user_message": "Return the exact text: foundation smoke passed",
        },
    )

    model_requests = 0
    text_deltas = 0
    terminal: dict[str, object] | None = None
    try:
        while terminal is None:
            raw = process.stdout.readline()
            if not raw:
                detail = (process.stderr.read() or b"").decode("utf-8", errors="replace")
                raise RuntimeError(
                    f"employee runtime closed before terminal frame ({process.poll()}): {detail[-1000:]}"
                )
            frame = decode_frame(raw)
            inbound.accept(frame)
            if frame.run_id != run_id:
                raise RuntimeError("employee runtime emitted a cross-run smoke frame")
            if frame.type == "model_request":
                model_requests += 1
                if model_requests != 1:
                    raise RuntimeError("employee runtime requested more than the smoke budget")
                send(
                    "provider_response",
                    {
                        "content": "foundation smoke passed",
                        "finish_reason": "stop",
                        "tool_calls": [],
                        "usage": {"input_tokens": 1, "output_tokens": 1},
                    },
                )
            elif frame.type == "text_delta":
                text_deltas += 1
            elif frame.type == "terminal":
                terminal = dict(frame.payload)
            elif frame.type == "worker_error":
                raise RuntimeError(
                    f"employee runtime worker error: {frame.payload.get('error_type')}: "
                    f"{frame.payload.get('message')}"
                )
            else:
                raise RuntimeError(f"unexpected employee runtime smoke frame: {frame.type}")
    finally:
        process.stdin.close()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.terminate()
            process.wait(timeout=5)

    if process.returncode != 0:
        detail = (process.stderr.read() or b"").decode("utf-8", errors="replace")
        raise RuntimeError(f"employee runtime exited ({process.returncode}): {detail[-1000:]}")
    if not terminal or terminal.get("final_response") != "foundation smoke passed":
        raise RuntimeError("employee runtime did not return the expected smoke response")
    if terminal.get("interrupted") or not terminal.get("completed"):
        raise RuntimeError("employee runtime did not complete the smoke run")
    if model_requests != 1 or text_deltas < 1:
        raise RuntimeError("employee runtime did not stream exactly one bounded model response")

    side_effect_files = sorted(
        str(path.relative_to(home)) for path in home.rglob("*") if path.is_file()
    )
    return {
        "agent_class": "AIAgent",
        "fork_file_count": fork_file_count,
        "final_response": terminal["final_response"],
        "home_isolated": True,
        "model_request_count": model_requests,
        "network_calls": 0,
        "ok": True,
        "parent_authority": True,
        "session_database": False,
        "side_effect_files": side_effect_files,
        "text_delta_count": text_deltas,
        "tool_count": 0,
        "upstream_version": "0.18.2",
        "worker_protocol": "noruct.employee.v2",
    }


def main() -> int:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--home", type=Path, required=True)
    parser.add_argument("--worker-python", default=sys.executable)
    args = parser.parse_args()
    try:
        result = _run(args.home, worker_python=str(args.worker_python))
    except Exception as exc:
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
