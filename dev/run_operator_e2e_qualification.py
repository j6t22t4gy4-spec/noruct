#!/usr/bin/env python3
"""Qualify a real installed ``noruct run`` write/restart flow in a pseudo-terminal.

This complements, rather than replaces, the provider-free Foundation
reliability suite.  It builds a clean wheel, invokes only the installed public
``noruct`` executable in a real PTY, approves each requested local action,
and verifies this concrete operator sequence:

1. create a deliberately failing target after an interactive approval;
2. run its failing test, repair the target, then rerun the test;
3. retain the resulting Job audit across a new executable process.

The same receipt also proves an actual Ctrl-C cancellation against a delayed
provider response, persists its cancelled Job audit, and starts a fresh
installed Job successfully.  The deterministic reliability matrix remains a
separate provider-free boundary check.  The direct interactive write → failed
test → repair → passing test path is provider-backed and runs through the
installed public executable.
PTY interaction is a
POSIX operator-terminal concern; Windows clean-install qualification remains
covered by the cross-platform public-ingress CI lane until a ConPTY driver is
added.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
import json
import os
from pathlib import Path
import pty
import select
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Iterator, Sequence

from run_clean_install_operator_qualification import (
    EMPLOYEE_RUNTIME_LOCK,
    ROOT,
    _build_wheel,
    _clean_child_environment,
    _json_list_command,
    _run,
    _venv_paths,
)


class _StreamingFixtureHandler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:  # noqa: N802 - base class API
        length = int(self.headers.get("Content-Length", "0"))
        raw_request = self.rfile.read(length)
        try:
            request_payload = json.loads(raw_request.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            request_payload = {"_invalid_fixture_request": True}
        self.server.requests.append(request_payload)  # type: ignore[attr-defined]
        try:
            payload = self.server.responses.pop(0)  # type: ignore[attr-defined]
        except IndexError:
            self.send_error(500, "unexpected model request")
            return
        delay_seconds = payload.get("delay_seconds", 0)
        if isinstance(delay_seconds, (int, float)) and delay_seconds > 0:
            time.sleep(float(delay_seconds))
        choice = payload["choices"][0]
        message = choice["message"]
        delta: dict[str, object] = {"content": message.get("content") or ""}
        raw_calls = message.get("tool_calls") or []
        if raw_calls:
            delta["tool_calls"] = [
                {"index": index, **call}
                for index, call in enumerate(raw_calls)
            ]
        event = {
            "id": payload.get("id", "operator-e2e-stream"),
            "choices": [{"delta": delta, "finish_reason": choice.get("finish_reason")}],
            "usage": payload.get("usage", {}),
        }
        body = (
            f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
            "data: [DONE]\n\n"
        ).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        # A Ctrl-C driven operator cancellation may close the PTY/client while
        # the deliberately delayed fixture is still preparing its response.
        # That is expected qualification behaviour, not a server-side failure.
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            return

    def log_message(self, _format: str, *_args: object) -> None:
        return


@contextmanager
def _fixture_server(*responses: dict[str, object]) -> Iterator[tuple[str, ThreadingHTTPServer]]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _StreamingFixtureHandler)
    server.responses = list(responses)  # type: ignore[attr-defined]
    server.requests = []  # type: ignore[attr-defined]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/v1", server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def _tool_call(call_id: str, name: str, arguments: dict[str, object]) -> dict[str, object]:
    return {
        "id": f"operator-e2e-{call_id}",
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": call_id,
                            "type": "function",
                            "function": {
                                "name": name,
                                "arguments": json.dumps(arguments, ensure_ascii=False),
                            },
                        }
                    ],
                },
                "finish_reason": "tool_calls",
            }
        ],
        "usage": {"prompt_tokens": 10, "completion_tokens": 4},
    }


def _completion(summary: str, *, delay_seconds: float = 0) -> dict[str, object]:
    payload: dict[str, object] = {
        "id": "operator-e2e-completion",
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": json.dumps(
                        {
                            "summary": summary,
                            "artifact_refs": [],
                            "acceptance_evidence": ["operator-e2e-local-fixture"],
                            "unresolved_issues": [],
                            "suggested_followups": [],
                            "observations": [],
                            "signals": [],
                        }
                    ),
                },
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 10, "completion_tokens": 6},
    }
    if delay_seconds:
        payload["delay_seconds"] = delay_seconds
    return payload


def _run_with_approvals(
    command: Sequence[str | Path], *, cwd: Path, env: dict[str, str], approvals: int
) -> str:
    """Drive a normal terminal approval interaction without faking stdin."""

    # ``Popen`` with a slave descriptor is not equivalent to a user terminal
    # on macOS: the child has no controlling TTY, and a nested workspace
    # command can make the slave report EIO even after the durable Job has
    # succeeded.  ``pty.fork`` gives the installed public executable the same
    # controlling-terminal contract it has in an actual operator shell.
    pid, master = pty.fork()
    if pid == 0:  # pragma: no cover - child replaces itself immediately
        os.chdir(cwd)
        os.execvpe(str(command[0]), [str(item) for item in command], env)
    output = bytearray()
    approved = 0
    deadline = time.monotonic() + 60
    prompt_offset = 0
    status: int | None = None
    try:
        while status is None:
            if time.monotonic() > deadline:
                os.kill(pid, 9)
                raise RuntimeError("operator E2E interactive run exceeded 60 seconds")
            waited_pid, wait_status = os.waitpid(pid, os.WNOHANG)
            if waited_pid == pid:
                status = wait_status
                break
            ready, _, _ = select.select([master], [], [], 0.2)
            if not ready:
                continue
            try:
                chunk = os.read(master, 65_536)
            except OSError:
                break
            if not chunk:
                break
            output.extend(chunk)
            text = output.decode("utf-8", errors="replace")
            while approved < approvals:
                marker = "Select ["
                position = text.find(marker, prompt_offset)
                if position < 0:
                    break
                prompt_offset = position + len(marker)
                os.write(master, b"1\n")
                approved += 1
        # Drain final status and keep an approval count assertion independent
        # of terminal escape/control formatting.
        while True:
            ready, _, _ = select.select([master], [], [], 0)
            if not ready:
                break
            try:
                chunk = os.read(master, 65_536)
            except OSError:
                break
            if not chunk:
                break
            output.extend(chunk)
        if status is None:
            _, status = os.waitpid(pid, 0)
    finally:
        os.close(master)
    text = output.decode("utf-8", errors="replace")
    code = (
        os.waitstatus_to_exitcode(status)
        if status is not None
        else -1
    )
    if code != 0:
        raise RuntimeError(f"interactive noruct run failed ({code})\n{text}")
    if approved != approvals:
        raise RuntimeError(
            f"operator E2E expected {approvals} approval prompt(s), received {approved}\n{text}"
        )
    return text


def _run_then_interrupt(
    command: Sequence[str | Path], *, cwd: Path, env: dict[str, str]
) -> str:
    """Send a real terminal Ctrl-C while an installed run awaits its provider."""

    pid, master = pty.fork()
    if pid == 0:  # pragma: no cover - child replaces itself immediately
        os.chdir(cwd)
        os.execvpe(str(command[0]), [str(item) for item in command], env)
    output = bytearray()
    deadline = time.monotonic() + 20
    interrupted = False
    status: int | None = None
    try:
        while status is None:
            if time.monotonic() > deadline:
                os.kill(pid, 9)
                raise RuntimeError("operator E2E cancellation run exceeded 20 seconds")
            waited_pid, wait_status = os.waitpid(pid, os.WNOHANG)
            if waited_pid == pid:
                status = wait_status
                break
            ready, _, _ = select.select([master], [], [], 0.1)
            if ready:
                try:
                    chunk = os.read(master, 65_536)
                except OSError:
                    chunk = b""
                output.extend(chunk)
            if not interrupted and time.monotonic() > deadline - 18:
                os.write(master, b"\x03")
                interrupted = True
        if status is None:
            _, status = os.waitpid(pid, 0)
    finally:
        os.close(master)
    text = output.decode("utf-8", errors="replace")
    code = os.waitstatus_to_exitcode(status) if status is not None else -1
    if not interrupted or code not in {130, 0}:
        raise RuntimeError(f"operator E2E cancellation exited unexpectedly ({code})\n{text}")
    return text


def qualify(wheel: Path | None = None, *, include_command: bool = True) -> dict[str, object]:
    if os.name == "nt":
        raise RuntimeError(
            "PTY E2E qualification requires a POSIX terminal; run the clean-install public ingress lane on Windows."
        )
    with tempfile.TemporaryDirectory(prefix="noruct-operator-e2e-") as temporary:
        root = Path(temporary)
        wheel = wheel.resolve() if wheel is not None else _build_wheel(root / "wheel")
        venv = root / "venv"
        base_env = _clean_child_environment()
        _run([sys.executable, "-m", "venv", venv], cwd=ROOT, env=base_env)
        python, noruct = _venv_paths(venv)
        _run([python, "-m", "pip", "install", "--disable-pip-version-check", "--no-deps", wheel], cwd=ROOT, env=base_env)
        _run(
            [python, "-m", "pip", "install", "--disable-pip-version-check", "--only-binary=:all:", "--require-hashes", "-r", EMPLOYEE_RUNTIME_LOCK],
            cwd=ROOT,
            env=base_env,
        )
        state = root / "runtime.db"
        config = root / "config.toml"
        workspace = root / "workspace"
        workspace.mkdir()
        env = _clean_child_environment(
            extra={
                "NORUCT_HOME": str(root / "home"),
                "TERM": "dumb",
                "NO_COLOR": "1",
                "PYTHONDONTWRITEBYTECODE": "1",
            }
        )
        responses: tuple[dict[str, object], ...] = (
            _tool_call(
                "write-broken", "write_workspace_file", {
                    "workspace_id": "noruct-workspace",
                    "path": "repair_target.py",
                    "content": "def value():\n    return 0\n",
                }
            ),
        )
        if include_command:
            responses += (
                _tool_call(
                    "run-test-fails",
                    "run_workspace_command",
                    {
                        "workspace_id": "noruct-workspace",
                        "command": "python3 -B -c \"from repair_target import value; assert value() == 1\"",
                    },
                ),
                _tool_call(
                    "write-repair",
                    "write_workspace_file",
                    {
                        "workspace_id": "noruct-workspace",
                        "path": "repair_target.py",
                        "content": "def value():\n    return 1\n",
                    },
                ),
                _tool_call(
                    "run-test-passes",
                    "run_workspace_command",
                    {
                        "workspace_id": "noruct-workspace",
                        "command": "python3 -B -c \"from repair_target import value; assert value() == 1\"",
                    },
                ),
            )
        responses += (_completion("Created and verified the approved operator E2E target."),)
        with _fixture_server(*responses) as (base_url, fixture):
            _run(
                [noruct, "--config", config, "setup", "--provider", "openai-api", "--base-url", base_url, "--model", "operator-e2e-model", "--no-auth", "--state", state],
                cwd=workspace,
                env=env,
            )
            try:
                terminal = _run_with_approvals(
                    [
                        noruct,
                        "--config",
                        config,
                        "run",
                        "Create the approved operator E2E target.",
                        "--workspace",
                        workspace,
                        "--state",
                        state,
                        "--permission-mode",
                        "ask",
                        "--trust-mode",
                        "strict",
                        "--no-live-screen",
                    ],
                    cwd=workspace,
                    env=env,
                    approvals=4 if include_command else 1,
                )
            except RuntimeError as exc:
                request_shapes = [
                    [str(item.get("role") or "") for item in request.get("messages", [])]
                    for request in fixture.requests
                    if isinstance(request, dict)
                ]
                event_rows: list[tuple[str, str]] = []
                run_rows: list[tuple[str, str | None]] = []
                if state.exists():
                    with sqlite3.connect(state) as connection:
                        tables = {
                            row[0]
                            for row in connection.execute(
                                "SELECT name FROM sqlite_master WHERE type='table'"
                            )
                        }
                        if {"run_events", "employee_runs"} <= tables:
                            event_rows = connection.execute(
                                "SELECT event_type, payload_json FROM run_events ORDER BY occurred_at, seq"
                            ).fetchall()
                            run_rows = connection.execute(
                                "SELECT status, failure_json FROM employee_runs ORDER BY created_at"
                            ).fetchall()
                raise RuntimeError(
                    f"{exc}\nfixture provider request role sequences: {request_shapes!r}"
                    f"\nrecent runtime event types: {[row[0] for row in event_rows][-16:]!r}"
                    f"\nruntime run statuses: {[row[0] for row in run_rows]!r}"
                ) from exc
        repaired = workspace / "repair_target.py"
        expected_file = "def value():\n    return 1\n" if include_command else "def value():\n    return 0\n"
        if repaired.read_text(encoding="utf-8") != expected_file:
            raise RuntimeError("operator E2E write did not persist the expected workspace content")
        # A separate installed cancellation and fresh restart close the
        # operator-facing lifecycle boundary.  The cancellation fixture keeps
        # its single provider response pending long enough for a real terminal
        # Ctrl-C; neither the test driver nor the runtime writes state directly.
        with _fixture_server(
            _completion("This response must not complete before cancellation.", delay_seconds=5)
        ) as (cancel_url, _cancel_fixture):
            _run(
                [
                    noruct,
                    "--config",
                    config,
                    "setup",
                    "--provider",
                    "openai-api",
                    "--base-url",
                    cancel_url,
                    "--model",
                    "operator-e2e-model",
                    "--no-auth",
                    "--state",
                    state,
                    "--force",
                ],
                cwd=workspace,
                env=env,
            )
            _run_then_interrupt(
                [
                    noruct,
                    "--config",
                    config,
                    "run",
                    "Cancel this installed operator qualification run.",
                    "--workspace",
                    workspace,
                    "--state",
                    state,
                    "--permission-mode",
                    "read-only",
                    "--plain",
                    "--no-live-screen",
                ],
                cwd=workspace,
                env=env,
            )
        with _fixture_server(_completion("The restarted installed run completed.")) as (
            restart_url,
            _restart_fixture,
        ):
            _run(
                [
                    noruct,
                    "--config",
                    config,
                    "setup",
                    "--provider",
                    "openai-api",
                    "--base-url",
                    restart_url,
                    "--model",
                    "operator-e2e-model",
                    "--no-auth",
                    "--state",
                    state,
                    "--force",
                ],
                cwd=workspace,
                env=env,
            )
            restart = _run(
                [
                    noruct,
                    "--config",
                    config,
                    "run",
                    "Restart after the cancelled installed run.",
                    "--workspace",
                    workspace,
                    "--state",
                    state,
                    "--permission-mode",
                    "read-only",
                    "--plain",
                    "--no-live-screen",
                ],
                cwd=workspace,
                env=env,
            )
        jobs = _json_list_command(
            [noruct, "job", "list", "--state", state, "--json"], cwd=workspace, env=env
        )
        reliability = json.loads(
            _run(
                [noruct, "foundation", "reliability", "--runtime-python", python, "--json"],
                cwd=workspace,
                env=env,
            ).stdout
        )
        cancelled_job_observed = any(
            isinstance(item, dict)
            and any(
                str(item.get(key) or "").upper() in {"CANCELLED", "INTERRUPTED"}
                for key in ("audit_status", "job_status")
            )
            for item in jobs
        )
        if not jobs:
            raise RuntimeError("fresh executable did not retain the E2E Job audit")
        if not cancelled_job_observed:
            raise RuntimeError("installed Ctrl-C run did not retain a cancelled Job audit")
        if not reliability.get("passed"):
            raise RuntimeError("installed runtime reliability matrix did not pass")
        with sqlite3.connect(state) as connection:
            event_types = [
                str(row[0])
                for row in connection.execute(
                    "SELECT event_type FROM run_events ORDER BY occurred_at, seq"
                )
            ]
            action_results = connection.execute(
                "SELECT tool_call_id, result_json FROM tool_actions ORDER BY created_at"
            ).fetchall()
        command_exit_codes: dict[str, int] = {}
        for call_id, raw_result in action_results:
            try:
                result = json.loads(str(raw_result))
                content = json.loads(str(result.get("content") or "{}"))
            except (json.JSONDecodeError, TypeError, AttributeError):
                continue
            exit_code = content.get("exit_code")
            if isinstance(exit_code, int):
                command_exit_codes[str(call_id)] = exit_code
        failed_test_repaired = (
            command_exit_codes.get("run-test-fails") not in {None, 0}
            and command_exit_codes.get("run-test-passes") == 0
            and event_types.count("TOOL_SUCCEEDED") >= (4 if include_command else 1)
        )
        approval_resume_verified = (
            event_types.count("APPROVAL_RESUME_CLAIMED") >= (4 if include_command else 1)
            and event_types.count("APPROVAL_RESUME_COMPLETED") >= (4 if include_command else 1)
        )
        if include_command and not failed_test_repaired:
            raise RuntimeError(
                "operator E2E did not record the failed-test repair sequence: "
                + ",".join(event_types)
                + f"; command exits={command_exit_codes!r}"
            )
        if include_command and not approval_resume_verified:
            raise RuntimeError(
                "operator E2E did not durably resume every approved action: "
                + ",".join(event_types)
            )
        return {
            "schema": "noruct.operator-e2e-qualification.v1",
            "wheel": wheel.name,
            "run_status": "SUCCEEDED",
            "approval_prompts": 4 if include_command else 1,
            "file_change_verified": True,
            "command_verified": include_command,
            "failed_test_repaired": failed_test_repaired,
            "approval_resume_verified": approval_resume_verified,
            "command_exit_codes": command_exit_codes,
            "provider_request_count": len(fixture.requests),
            "live_multitool_repair_qualified": include_command and failed_test_repaired,
            "restart_job_audit_count": len(jobs),
            "cancel_restart_reliability": bool(reliability.get("passed")),
            "installed_cancelled_job_observed": cancelled_job_observed,
            "installed_restart_verified": "The restarted installed run completed." in restart.stdout,
            "terminal_output_bytes": len(terminal.encode("utf-8")),
        }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--wheel", type=Path)
    parser.add_argument(
        "--write-only",
        action="store_true",
        help="Run only the narrower write/restart diagnostic instead of the default repair flow.",
    )
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    receipt = qualify(args.wheel, include_command=not args.write_only)
    if args.json:
        print(json.dumps(receipt, sort_keys=True))
    else:
        print("operator E2E qualification passed")
        for key, value in receipt.items():
            print(f"{key}: {value}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
