#!/usr/bin/env python3
"""Qualify the operator's local path from a freshly installed Noruct wheel.

This is deliberately stronger than an import or ``--help`` smoke test.  It
builds the current wheel, installs it into an empty virtual environment, and
uses only that environment's ``noruct`` executable for setup, persisted
provider configuration, a local model call, Knowledge Folder retrieval,
Blueprint control, a restarted ACTIVE JOB audit, and the non-interactive
approval fail-closed boundary. Interactive approval belongs to the dedicated
terminal acceptance lane because CI cannot own an operator's terminal.

It does not publish a release, download an installer, read a real credential,
or call a hosted model.  The local OpenAI-compatible fixture is a deterministic
protocol probe owned by this script.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Iterator, Sequence


ROOT = Path(__file__).resolve().parents[1]
EMPLOYEE_RUNTIME_LOCK = ROOT / "dev" / "requirements-employee-runtime-py311.lock"


def _clean_child_environment(*, extra: dict[str, str] | None = None) -> dict[str, str]:
    """Prevent the source checkout from leaking into a clean wheel process."""

    environment = os.environ.copy()
    environment.pop("PYTHONPATH", None)
    environment.pop("PYTHONHOME", None)
    if extra:
        environment.update(extra)
    return environment


def _completion(summary: str) -> dict[str, object]:
    content = {
        "summary": summary,
        "artifact_refs": [],
        "acceptance_evidence": ["clean-install:local-fixture"],
        "unresolved_issues": [],
        "suggested_followups": [],
        "observations": [],
        "signals": [],
    }
    return {
        "id": "clean-install-completion",
        "choices": [{"message": {"role": "assistant", "content": json.dumps(content)}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 11, "completion_tokens": 7},
    }


class _FixtureHandler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler contract
        length = int(self.headers.get("Content-Length", "0"))
        self.rfile.read(length)
        try:
            payload = self.server.responses.pop(0)  # type: ignore[attr-defined]
        except IndexError:
            self.send_error(500, "unexpected model request")
            return
        encoded = json.dumps(payload).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def log_message(self, _format: str, *_args: object) -> None:
        return


@contextmanager
def _fixture_server(*responses: dict[str, object]) -> Iterator[str]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _FixtureHandler)
    server.responses = list(responses)  # type: ignore[attr-defined]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/v1"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def _venv_paths(root: Path) -> tuple[Path, Path]:
    if os.name == "nt":
        return root / "Scripts" / "python.exe", root / "Scripts" / "noruct.exe"
    return root / "bin" / "python", root / "bin" / "noruct"


def _run(command: Sequence[str | Path], *, cwd: Path, env: dict[str, str], input_text: str | None = None) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(
        [str(item) for item in command],
        cwd=cwd,
        env=env,
        input=input_text,
        text=True,
        capture_output=True,
        check=False,
        timeout=45,
    )
    if completed.returncode:
        raise RuntimeError(
            "command failed: " + " ".join(map(str, command))
            + "\nstdout:\n" + completed.stdout
            + "\nstderr:\n" + completed.stderr
        )
    return completed


def _json_command(command: Sequence[str | Path], *, cwd: Path, env: dict[str, str]) -> dict[str, object]:
    completed = _run(command, cwd=cwd, env=env)
    try:
        value = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"expected JSON from {' '.join(map(str, command))}: {completed.stdout}") from exc
    if not isinstance(value, dict):
        raise RuntimeError("operator command did not return a JSON object")
    return value


def _json_list_command(command: Sequence[str | Path], *, cwd: Path, env: dict[str, str]) -> list[object]:
    completed = _run(command, cwd=cwd, env=env)
    try:
        value = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"expected JSON from {' '.join(map(str, command))}: {completed.stdout}") from exc
    if not isinstance(value, list):
        raise RuntimeError("operator command did not return a JSON list")
    return value


def _expect_noninteractive_approval_refusal(
    command: Sequence[str | Path], *, cwd: Path, env: dict[str, str]
) -> None:
    """Assert that an installed wheel cannot auto-approve through a pipe."""

    completed = subprocess.run(
        [str(item) for item in command],
        cwd=cwd,
        env=env,
        text=True,
        capture_output=True,
        check=False,
        timeout=20,
    )
    if completed.returncode == 0 or "requires an interactive input and output terminal" not in completed.stderr:
        raise RuntimeError(
            "installed wheel did not fail closed for non-interactive approval mode\n"
            + completed.stdout
            + completed.stderr
        )


def _build_wheel(output: Path) -> Path:
    _run(
        [sys.executable, "-m", "pip", "wheel", ".", "--no-deps", "--no-build-isolation", "--wheel-dir", output],
        cwd=ROOT,
        env=_clean_child_environment(),
    )
    wheels = sorted(output.glob("noruct-*.whl"))
    if len(wheels) != 1:
        raise RuntimeError("clean-install qualification expected exactly one Noruct wheel")
    return wheels[0]


def qualify(wheel: Path | None = None) -> dict[str, object]:
    with tempfile.TemporaryDirectory(prefix="noruct-clean-install-") as temporary:
        root = Path(temporary)
        wheel = wheel.resolve() if wheel is not None else _build_wheel(root / "wheel")
        venv = root / "venv"
        clean_environment = _clean_child_environment()
        _run([sys.executable, "-m", "venv", venv], cwd=ROOT, env=clean_environment)
        python, noruct = _venv_paths(venv)
        if not python.is_file() or not noruct.parent.exists():
            raise RuntimeError("virtual environment did not expose the expected executable layout")
        _run([python, "-m", "pip", "install", "--disable-pip-version-check", "--no-deps", wheel], cwd=ROOT, env=clean_environment)
        _run(
            [python, "-m", "pip", "install", "--disable-pip-version-check", "--only-binary=:all:", "--require-hashes", "-r", EMPLOYEE_RUNTIME_LOCK],
            cwd=ROOT,
            env=clean_environment,
        )
        _run([python, "-m", "pip", "check"], cwd=ROOT, env=clean_environment)
        if not noruct.is_file():
            entries = ", ".join(sorted(item.name for item in noruct.parent.glob("*")))
            raise RuntimeError(
                "wheel install did not expose the noruct executable at "
                f"{noruct}; available entries: {entries}"
            )

        state = root / "runtime.db"
        knowledge_state = root / "knowledge.db"
        config = root / "config.toml"
        workspace = root / "workspace"
        knowledge_folder = root / "knowledge-folder"
        workspace.mkdir()
        knowledge_folder.mkdir()
        (knowledge_folder / "operator.txt").write_text(
            "Noruct clean-install qualification stores user knowledge locally.\n",
            encoding="utf-8",
        )
        blueprint = root / "blueprint.json"
        blueprint.write_text(
            json.dumps(
                {
                    "blueprint_id": "clean-install-analysis",
                    "version": 1,
                    "objective_class": "general",
                    "execution_profiles": ["read_only"],
                    "parameters": ["objective"],
                    "tasks": [{
                        "task_id": "final",
                        "objective_template": "Analyze {{objective}}",
                        "depends_on": [],
                        "required_capabilities": ["analysis"],
                        "acceptance_templates": ["A concise result"],
                        "risk_level": "LOW",
                    }],
                    "final_task_id": "final",
                    "origin": "DRAFT",
                    "parent_ref": None,
                }
            ),
            encoding="utf-8",
        )
        env = _clean_child_environment(
            extra={"NORUCT_HOME": str(root / "home"), "PYTHONDONTWRITEBYTECODE": "1"}
        )

        with _fixture_server(_completion("Installed wheel completed the Company goal.")) as base_url:
            _run(
                [noruct, "--config", config, "setup", "--provider", "openai-api", "--base-url", base_url, "--model", "clean-install-model", "--no-auth", "--state", state],
                cwd=workspace,
                env=env,
            )
            provider = _json_command([noruct, "--config", config, "provider", "status", "--json"], cwd=workspace, env=env)
            doctor = _json_command([noruct, "--config", config, "doctor", "--json"], cwd=workspace, env=env)
            foundation = _json_command([noruct, "foundation", "cutover-status", "--json"], cwd=workspace, env=env)
            foundation_source = _json_command(
                [noruct, "foundation", "verify-source", "--json"],
                cwd=workspace,
                env=env,
            )
            demo = _json_command([noruct, "demo", "solo", "--json"], cwd=workspace, env=env)
            _json_command([noruct, "knowledge", "folder-add", knowledge_folder, "--state", knowledge_state, "--json"], cwd=workspace, env=env)
            evidence = _json_command([noruct, "knowledge", "recall", "clean-install", "--state", knowledge_state, "--json"], cwd=workspace, env=env)
            _json_command([noruct, "graph", "import", blueprint, "--state", state, "--confirm", "--json"], cwd=workspace, env=env)
            result = _json_command([noruct, "--config", config, "run", "Answer from the installed wheel", "--workspace", workspace, "--state", state, "--json"], cwd=workspace, env=env)
            preview = _json_command(
                [
                    noruct,
                    "graph",
                    "preview",
                    "Analyze the installed runtime",
                    "--blueprint-id",
                    "clean-install-analysis",
                    "--version",
                    "1",
                    "--state",
                    state,
                    "--json",
                ],
                cwd=workspace,
                env=env,
            )
            _expect_noninteractive_approval_refusal(
                [noruct, "--config", config, "run", "Create a file", "--workspace", workspace, "--state", state, "--permission-mode", "ask", "--plain", "--no-live-screen"],
                cwd=workspace,
                env=env,
            )
        jobs = _json_list_command([noruct, "job", "list", "--state", state, "--json"], cwd=workspace, env=env)

        if provider.get("kind") != "openai_api":
            raise RuntimeError(f"fresh setup was not recovered by the installed executable: {provider}")
        if foundation.get("default_runtime") != "noruct":
            raise RuntimeError("installed wheel did not use the Noruct employee runtime")
        if not foundation_source.get("ok"):
            raise RuntimeError("installed wheel did not pass foundation source verification")
        if demo.get("status") != "SUCCEEDED" or result.get("status") != "SUCCEEDED":
            raise RuntimeError("installed Company path did not finish successfully")
        if not evidence.get("pack_id") or not preview.get("blueprint_ref"):
            raise RuntimeError(
                "installed Knowledge or Graph read-only path did not return its contract: "
                f"evidence={evidence}, preview={preview}"
            )
        if not jobs:
            raise RuntimeError("restarted installed executable did not retain an ACTIVE JOB audit")
        return {
            "schema": "noruct.clean-install-operator-qualification.v1",
            "wheel": wheel.name,
            "platform": sys.platform,
            "provider": provider.get("kind"),
            "runtime": foundation.get("default_runtime"),
            "foundation_source_verified": foundation_source.get("ok"),
            "demo_status": demo.get("status"),
            "run_status": result.get("status"),
            "knowledge_evidence_pack": evidence.get("pack_id"),
            "graph_preview": bool(preview.get("blueprint_ref")),
            "active_job_audit_count": len(jobs),
            "noninteractive_approval_fail_closed": True,
            "doctor_schema": doctor.get("schema"),
        }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--wheel", type=Path, help="Use an already-built Noruct wheel instead of building one")
    parser.add_argument("--json", action="store_true", help="Print the qualification receipt as JSON")
    args = parser.parse_args(argv)
    receipt = qualify(args.wheel)
    if args.json:
        print(json.dumps(receipt, sort_keys=True))
    else:
        print("clean-install operator qualification passed")
        for key, value in receipt.items():
            print(f"{key}: {value}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
