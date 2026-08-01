"""Private subprocess and JSONL transport for the Employee Runtime worker."""

from __future__ import annotations

import asyncio
import os
import tempfile
from pathlib import Path
from typing import Any, Mapping

from .protocol import (
    MAX_FRAME_BYTES,
    FoundationFrame,
    FoundationProtocolError,
    FrameSequence,
    decode_frame,
    encode_frame,
)
from .runtime import NoructEmployeeRuntimeError
from .runtime_support import (
    _employee_runtime_core_root,
    _project_worker_code,
)

class _WorkerProcess:
    def __init__(self, *, python_executable: str, home: Path) -> None:
        self.python_executable = python_executable
        self.home = home
        self.process: asyncio.subprocess.Process | None = None
        self.lock = asyncio.Lock()
        self.outbound = FrameSequence()
        self.inbound = FrameSequence()
        self.stderr = bytearray()
        self.stderr_task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        if self.process is not None and self.process.returncode is None:
            return
        self.home.mkdir(parents=True, exist_ok=True, mode=0o700)
        code_projection = _project_worker_code(self.home)
        env = {
            "HOME": str(self.home),
            "HERMES_DISABLE_LAZY_INSTALLS": "1",
            "HERMES_HOME": str(self.home / "state"),
            "HERMES_PYTHON_SRC_ROOT": str(_employee_runtime_core_root()),
            "LANG": os.environ.get("LANG", "C.UTF-8"),
            "LC_ALL": os.environ.get("LC_ALL", "C.UTF-8"),
            "NO_COLOR": "1",
            "NORUCT_FOUNDATION_EXECUTION_WORKER": "1",
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONPATH": str(code_projection),
            "TMPDIR": os.environ.get("TMPDIR", tempfile.gettempdir()),
        }
        self.process = await asyncio.create_subprocess_exec(
            self.python_executable,
            "-m",
            "dynamic_firm.foundation._employee_worker",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            # ``StreamReader.readline`` otherwise keeps asyncio's 64 KiB
            # default.  The worker protocol deliberately permits bounded
            # frames up to ``MAX_FRAME_BYTES`` (for example a second explicit
            # repository read included in the next model request), so the
            # transport must honor the same contract instead of converting a
            # valid frame into an unclassified ``ValueError``.
            limit=MAX_FRAME_BYTES + 1,
            env=env,
            cwd=str(self.home),
        )
        self.stderr_task = asyncio.create_task(self._drain_stderr())

    async def _drain_stderr(self) -> None:
        assert self.process is not None and self.process.stderr is not None
        while True:
            chunk = await self.process.stderr.read(4096)
            if not chunk:
                return
            self.stderr.extend(chunk)
            if len(self.stderr) > 32_768:
                del self.stderr[:-32_768]

    async def send(self, frame_type: str, run_id: str, payload: Mapping[str, Any]) -> None:
        await self.start()
        assert self.process is not None and self.process.stdin is not None
        if self.process.returncode is not None:
            raise NoructEmployeeRuntimeError(
                f"Employee worker exited before send ({self.process.returncode})"
            )
        frame = FoundationFrame(
            frame_type,
            run_id,
            self.outbound.next(run_id),
            dict(payload),
        )
        self.process.stdin.write(encode_frame(frame))
        await self.process.stdin.drain()

    async def receive(self) -> FoundationFrame:
        assert self.process is not None and self.process.stdout is not None
        raw = await self.process.stdout.readline()
        if not raw:
            code = await self.process.wait()
            detail = self.stderr.decode("utf-8", errors="replace").strip()
            raise NoructEmployeeRuntimeError(
                f"Employee worker closed its channel ({code}): {detail[-1000:]}"
            )
        if len(raw) > MAX_FRAME_BYTES + 1:
            raise FoundationProtocolError("worker frame exceeds the byte limit")
        frame = decode_frame(raw)
        self.inbound.accept(frame)
        return frame

    async def interrupt(self, run_id: str, reason: str) -> None:
        if self.process is None or self.process.returncode is not None:
            return
        await self.send("cancel", run_id, {"reason": reason})

    async def close(self) -> None:
        process = self.process
        if process is None:
            return
        if process.stdin is not None:
            process.stdin.close()
            try:
                await process.stdin.wait_closed()
            except (BrokenPipeError, ConnectionResetError):
                pass
        if process.returncode is None:
            try:
                await asyncio.wait_for(process.wait(), timeout=0.5)
            except TimeoutError:
                process.terminate()
                try:
                    await asyncio.wait_for(process.wait(), timeout=0.5)
                except TimeoutError:
                    process.kill()
                    await process.wait()
        if self.stderr_task is not None:
            await asyncio.gather(self.stderr_task, return_exceptions=True)
