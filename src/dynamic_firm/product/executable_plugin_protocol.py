"""Bounded subprocess exchange for the executable-plugin wire protocol."""

from __future__ import annotations

import asyncio
import os
import signal

from dynamic_firm.runtime.tools import ToolExecutionError


async def terminate_process(process: asyncio.subprocess.Process) -> None:
    if process.returncode is not None:
        return
    try:
        if os.name == "posix" and process.pid:
            os.killpg(process.pid, signal.SIGTERM)
        else:
            process.terminate()
        await asyncio.wait_for(process.wait(), timeout=1)
    except (ProcessLookupError, TimeoutError):
        try:
            process.kill()
        except ProcessLookupError:
            return
        await process.wait()


async def bounded_exchange(
    process: asyncio.subprocess.Process, request: bytes, *, max_response_bytes: int
) -> bytes:
    """Write one request and drain stdout without unbounded buffering."""

    assert process.stdin is not None
    assert process.stdout is not None
    try:
        process.stdin.write(request)
        await process.stdin.drain()
        process.stdin.close()
        await process.stdin.wait_closed()
    except (BrokenPipeError, ConnectionResetError, OSError):
        pass
    chunks: list[bytes] = []
    observed = 0
    while chunk := await process.stdout.read(min(16_384, max_response_bytes + 1 - observed)):
        chunks.append(chunk)
        observed += len(chunk)
        if observed > max_response_bytes:
            await terminate_process(process)
            raise ToolExecutionError("Plugin response exceeds the output limit")
    await process.wait()
    return b"".join(chunks)


def reject_json_constant(value: str) -> object:
    raise ValueError(f"non-finite JSON constant is not allowed: {value}")
