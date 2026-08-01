"""Parent-owned lifecycle for bounded local background workspace commands.

This is a narrow adaptation of the registered source process-registry
invariants: a tracked opaque identifier, rolling output, explicit status
queries, bounded waiting and process-group termination.  It deliberately
excludes upstream environment selection, gateway delivery, checkpoint files,
PTY and stdin authority.  The latter is now exposed only through an explicit
interactive-process flag and a separately approved input action; it never
becomes an ambient shell or terminal authority.
"""

from __future__ import annotations

import asyncio
import errno
import os
import signal
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Mapping


@dataclass(slots=True)
class BackgroundWorkspaceProcess:
    """One parent-authorized process and its bounded in-memory transcript."""

    process_id: str
    workspace_key: str
    command: str
    process: subprocess.Popen[bytes]
    started_at: float
    max_output_bytes: int
    output: bytearray = field(default_factory=bytearray)
    interactive: bool = False
    terminal_fd: int | None = None
    exit_code: int | None = None
    completion_reason: str = "running"
    finished: threading.Event = field(default_factory=threading.Event)
    lock: threading.Lock = field(default_factory=threading.Lock)

    @property
    def running(self) -> bool:
        return self.exit_code is None and self.process.poll() is None


class BackgroundProcessRegistry:
    """Thread-safe local registry with no external session or state authority."""

    def __init__(self, *, max_processes: int = 8, max_output_bytes: int = 64_000) -> None:
        self.max_processes = max_processes
        self.max_output_bytes = max_output_bytes
        self._processes: dict[str, BackgroundWorkspaceProcess] = {}
        self._lock = threading.Lock()

    def start(
        self,
        *,
        workspace_key: str,
        command: str,
        cwd: str,
        environment: Mapping[str, str],
        shell: str,
        interactive: bool = False,
    ) -> BackgroundWorkspaceProcess:
        with self._lock:
            finished = sorted(
                (item for item in self._processes.values() if not item.running),
                key=lambda item: item.started_at,
            )
            for item in finished[:-32]:
                self._processes.pop(item.process_id, None)
            if sum(1 for item in self._processes.values() if item.running) >= self.max_processes:
                raise ValueError("Background process capacity is exhausted")
        terminal_fd: int | None = None
        if interactive:
            if os.name != "posix":
                raise ValueError("Interactive workspace processes require a POSIX terminal")
            # This is the narrow local counterpart of the registered source
            # registry's `use_pty` path.  It intentionally does not inherit
            # its login-shell, backend or gateway authority.
            import pty

            terminal_fd, slave_fd = pty.openpty()
            try:
                process = subprocess.Popen(
                    [shell, "-c", command],
                    cwd=cwd,
                    env=dict(environment),
                    stdin=slave_fd,
                    stdout=slave_fd,
                    stderr=slave_fd,
                    start_new_session=True,
                )
            except BaseException:
                os.close(terminal_fd)
                raise
            finally:
                os.close(slave_fd)
        else:
            process = subprocess.Popen(
                [shell, "-c", command],
                cwd=cwd,
                env=dict(environment),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                start_new_session=(os.name == "posix"),
            )
        item = BackgroundWorkspaceProcess(
            process_id=f"process-{uuid.uuid4().hex[:16]}",
            workspace_key=workspace_key,
            command=command,
            process=process,
            started_at=time.time(),
            max_output_bytes=self.max_output_bytes,
            interactive=interactive,
            terminal_fd=terminal_fd,
        )
        with self._lock:
            self._processes[item.process_id] = item
        threading.Thread(
            target=self._collect_output,
            args=(item,),
            daemon=True,
            name=f"noruct-process-{item.process_id[-6:]}",
        ).start()
        return item

    @staticmethod
    def _append_output(item: BackgroundWorkspaceProcess, chunk: bytes) -> None:
        with item.lock:
            item.output.extend(chunk)
            overflow = len(item.output) - item.max_output_bytes
            if overflow > 0:
                del item.output[:overflow]

    @classmethod
    def _collect_output(cls, item: BackgroundWorkspaceProcess) -> None:
        stream = None
        try:
            if item.terminal_fd is not None:
                # POSIX reports EIO when the last PTY slave closes.  That is
                # the normal EOF signal for this transport, not a collector
                # failure that should overwrite the process exit status.
                while True:
                    try:
                        chunk = os.read(item.terminal_fd, 8_192)
                    except OSError as exc:
                        if exc.errno in {errno.EIO, errno.EBADF}:
                            break
                        raise
                    if not chunk:
                        break
                    cls._append_output(item, chunk)
            else:
                stream = item.process.stdout
                if stream is not None:
                    while chunk := stream.read(8_192):
                        cls._append_output(item, chunk)
            if item.process.poll() is None:
                item.process.wait(timeout=5)
            with item.lock:
                if item.exit_code is None:
                    item.exit_code = item.process.returncode
                    item.completion_reason = "exited"
        except Exception as exc:  # pragma: no cover - OS-specific failure path
            with item.lock:
                if item.exit_code is None:
                    item.exit_code = -1
                    item.completion_reason = f"collector_error:{type(exc).__name__}"
        finally:
            if stream is not None:
                try:
                    stream.close()
                except OSError:
                    pass
            if item.terminal_fd is not None:
                with item.lock:
                    terminal_fd = item.terminal_fd
                    item.terminal_fd = None
                if terminal_fd is not None:
                    try:
                        os.close(terminal_fd)
                    except OSError:
                        pass
            item.finished.set()

    def _get(self, *, workspace_key: str, process_id: str) -> BackgroundWorkspaceProcess:
        with self._lock:
            item = self._processes.get(process_id)
        if item is None or item.workspace_key != workspace_key:
            raise ValueError("Unknown process_id for this workspace")
        return item

    @staticmethod
    def _snapshot(item: BackgroundWorkspaceProcess, *, include_output: bool = False) -> dict[str, object]:
        with item.lock:
            process_returncode = item.process.poll()
            if process_returncode is not None and item.exit_code is None:
                item.exit_code = process_returncode
                item.completion_reason = "exited"
                item.finished.set()
            payload: dict[str, object] = {
                "process_id": item.process_id,
                "command": item.command,
                "pid": item.process.pid,
                "status": "running" if item.exit_code is None else "exited",
                "interactive": item.interactive,
                "uptime_seconds": int(time.time() - item.started_at),
            }
            if item.exit_code is not None:
                payload["exit_code"] = item.exit_code
                payload["completion_reason"] = item.completion_reason
            if include_output:
                payload["output"] = bytes(item.output).decode("utf-8", errors="replace")
            else:
                payload["output_preview"] = bytes(item.output[-4_096:]).decode("utf-8", errors="replace")
            return payload

    def list(self, *, workspace_key: str) -> list[dict[str, object]]:
        with self._lock:
            items = [item for item in self._processes.values() if item.workspace_key == workspace_key]
        return [self._snapshot(item) for item in sorted(items, key=lambda item: item.started_at)]

    def inspect(self, *, workspace_key: str, process_id: str, include_output: bool = False) -> dict[str, object]:
        return self._snapshot(self._get(workspace_key=workspace_key, process_id=process_id), include_output=include_output)

    def stop(self, *, workspace_key: str, process_id: str) -> dict[str, object]:
        item = self._get(workspace_key=workspace_key, process_id=process_id)
        if item.running:
            if os.name == "posix":
                try:
                    os.killpg(os.getpgid(item.process.pid), signal.SIGTERM)
                except ProcessLookupError:
                    pass
            else:  # pragma: no cover - Windows behavior is covered by platform integration.
                item.process.terminate()
            try:
                item.process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                if os.name == "posix":
                    try:
                        os.killpg(os.getpgid(item.process.pid), signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                else:  # pragma: no cover
                    item.process.kill()
                item.process.wait(timeout=2)
            with item.lock:
                item.exit_code = item.process.returncode
                item.completion_reason = "stopped"
                item.finished.set()
        return self._snapshot(item, include_output=True)

    def write_stdin(
        self,
        *,
        workspace_key: str,
        process_id: str,
        data: str,
    ) -> dict[str, object]:
        """Write exact bytes to one parent-created interactive process.

        No newline is appended.  The caller therefore decides whether a CLI
        receives ordinary text, an Enter key, or a control sequence.  Non-PTY
        processes deliberately have no writable stdin.
        """

        if not isinstance(data, str):
            raise ValueError("Process stdin data must be text")
        payload = data.encode("utf-8")
        if not payload:
            raise ValueError("Process stdin data cannot be empty")
        if len(payload) > 32_768:
            raise ValueError("Process stdin data exceeds the 32 KiB limit")
        item = self._get(workspace_key=workspace_key, process_id=process_id)
        with item.lock:
            if not item.running:
                raise ValueError("Process has already exited")
            if not item.interactive or item.terminal_fd is None:
                raise ValueError("Process was not started as interactive")
            remaining = memoryview(payload)
            try:
                while remaining:
                    written = os.write(item.terminal_fd, remaining)
                    if written <= 0:
                        raise OSError("terminal write returned no bytes")
                    remaining = remaining[written:]
            except OSError as exc:
                raise ValueError("Could not write to the interactive process") from exc
        return {
            "process_id": item.process_id,
            "status": "input_written",
            "bytes_written": len(payload),
        }

    async def wait(
        self,
        *,
        workspace_key: str,
        process_id: str,
        timeout_seconds: float,
        cancellation,
    ) -> dict[str, object]:
        item = self._get(workspace_key=workspace_key, process_id=process_id)
        deadline = time.monotonic() + timeout_seconds
        while item.running and time.monotonic() < deadline:
            cancellation.raise_if_cancelled()
            await asyncio.sleep(min(0.1, max(0.01, deadline - time.monotonic())))
        payload = self._snapshot(item, include_output=True)
        if item.running:
            payload["status"] = "timeout"
        return payload


DEFAULT_BACKGROUND_PROCESS_REGISTRY = BackgroundProcessRegistry()
