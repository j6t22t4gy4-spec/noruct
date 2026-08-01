"""Operator-owned lifecycle for one isolated local Chromium-family browser.

This module never runs inside an employee ToolIntent.  It only supports the
explicit operator CLI path and launches a temporary profile with a loopback
CDP endpoint, so it cannot inherit the user's normal browser profile/cookies.
"""

from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import tempfile
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from urllib.request import urlopen


class BrowserLifecycleError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class BrowserLaunchRecord:
    pid: int
    endpoint: str
    profile_directory: str
    browser_command: str
    started_at: float


def lifecycle_state_path(config_path: Path) -> Path:
    return config_path.expanduser().resolve().parent / "browser-lifecycle.json"


def _load(path: Path) -> BrowserLaunchRecord | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            return None
        record = BrowserLaunchRecord(
            pid=int(value["pid"]), endpoint=str(value["endpoint"]),
            profile_directory=str(value["profile_directory"]), browser_command=str(value["browser_command"]),
            started_at=float(value["started_at"]),
        )
        if record.pid <= 0 or not record.endpoint.startswith("http://127.0.0.1:"):
            return None
        return record
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
        return None


def _write(path: Path, record: BrowserLaunchRecord) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=".noruct-browser-", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(asdict(record), handle, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
            handle.flush(); os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        try: temporary.unlink()
        except FileNotFoundError: pass


def _is_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ProcessLookupError):
        return False


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _ready(endpoint: str, timeout_seconds: float) -> bool:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        try:
            with urlopen(endpoint + "/json/version", timeout=0.5) as response:  # noqa: S310 - fixed loopback endpoint
                if response.status == 200:
                    return True
        except OSError:
            time.sleep(0.1)
    return False


def browser_lifecycle_status(path: Path) -> dict[str, object]:
    record = _load(path)
    if record is None:
        return {"managed": False, "running": False}
    alive = _is_alive(record.pid)
    return {
        "managed": True,
        "running": alive,
        "pid": record.pid if alive else None,
        "endpoint": "managed_loopback" if alive else None,
        "started_at": record.started_at if alive else None,
        "stale_record": not alive,
    }


def launch_isolated_browser(*, state_path: Path, browser_command: Path, timeout_seconds: float = 8.0) -> BrowserLaunchRecord:
    command = browser_command.expanduser().resolve(strict=False)
    if not command.is_absolute() or not command.is_file() or not os.access(command, os.X_OK):
        raise BrowserLifecycleError("Browser command must resolve to an absolute executable")
    current = _load(state_path)
    if current is not None and _is_alive(current.pid):
        raise BrowserLifecycleError("A Noruct-managed isolated browser is already running; close it before launching another")
    if not 1.0 <= timeout_seconds <= 20.0:
        raise BrowserLifecycleError("Browser launch timeout must be between 1 and 20 seconds")
    root = state_path.parent / "browser-runs"
    root.mkdir(parents=True, exist_ok=True)
    profile = Path(tempfile.mkdtemp(prefix="profile-", dir=root))
    port = _free_port()
    endpoint = f"http://127.0.0.1:{port}"
    args = (
        str(command),
        "--remote-debugging-address=127.0.0.1",
        f"--remote-debugging-port={port}",
        f"--user-data-dir={profile}",
        "--no-first-run",
        "--no-default-browser-check",
        "about:blank",
    )
    environment = {key: value for key, value in os.environ.items() if key in {"HOME", "PATH", "LANG", "LC_ALL", "SYSTEMROOT", "WINDIR", "DISPLAY", "WAYLAND_DISPLAY", "XDG_RUNTIME_DIR"}}
    try:
        process = subprocess.Popen(  # noqa: S603 - executable was validated and argv is fixed
            args, cwd=tempfile.gettempdir(), env=environment, stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True,
        )
    except OSError as exc:
        profile.rmdir()
        raise BrowserLifecycleError("Isolated browser could not start") from exc
    if not _ready(endpoint, timeout_seconds):
        _terminate(process.pid)
        _remove_profile(profile, root)
        raise BrowserLifecycleError("Isolated browser did not expose its loopback DevTools endpoint")
    # The lifecycle receipt, not this short-lived CLI process, owns later
    # termination.  Mark the Popen child detached so Python does not emit a
    # ResourceWarning merely because the operator intentionally leaves the
    # isolated browser running after this command exits.
    process._child_created = False  # type: ignore[attr-defined]
    record = BrowserLaunchRecord(process.pid, endpoint, str(profile), str(command), time.time())
    _write(state_path, record)
    return record


def _terminate(pid: int) -> None:
    if not _is_alive(pid): return
    try:
        if os.name == "posix": os.killpg(pid, signal.SIGTERM)
        else: os.kill(pid, signal.SIGTERM)
    except OSError: return
    deadline = time.monotonic() + 3.0
    while _is_alive(pid) and time.monotonic() < deadline:
        _reap(pid)
        time.sleep(0.05)
    if _is_alive(pid):
        try:
            if os.name == "posix": os.killpg(pid, signal.SIGKILL)
            else: os.kill(pid, signal.SIGKILL)
        except OSError: pass
    _reap(pid)


def _reap(pid: int) -> None:
    """Reap a process we launched when it is our direct child.

    A lifecycle record can outlive the CLI process, so `waitpid` commonly
    raises `ChildProcessError`; that is normal and must not affect cleanup.
    """

    try:
        os.waitpid(pid, os.WNOHANG)
    except ChildProcessError:
        return


def _remove_profile(profile: Path, root: Path) -> None:
    try:
        candidate = profile.resolve()
        if candidate.parent != root.resolve(): return
        import shutil
        shutil.rmtree(candidate, ignore_errors=True)
    except OSError:
        return


def close_isolated_browser(state_path: Path) -> bool:
    record = _load(state_path)
    if record is None:
        return False
    _terminate(record.pid)
    _remove_profile(Path(record.profile_directory), state_path.parent / "browser-runs")
    try: state_path.unlink()
    except FileNotFoundError: pass
    return True
