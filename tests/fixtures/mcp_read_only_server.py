"""Opt-in deterministic fixture for the audited external MCP SDK environment."""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path


mode = sys.argv[1] if len(sys.argv) > 1 else "normal"
if pid_file := os.environ.get("NORUCT_MCP_FIXTURE_PID_FILE"):
    Path(pid_file).write_text(str(os.getpid()), encoding="ascii")
if mode == "crash":
    raise SystemExit(7)
if mode == "malformed":
    sys.stdout.write("{malformed-json-rpc\n")
    sys.stdout.flush()
    raise SystemExit(0)

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations


server = FastMCP("noruct-contract-fixture")
annotations = ToolAnnotations(
    readOnlyHint=mode != "write",
    destructiveHint=mode == "write",
    openWorldHint=True,
)


@server.tool(name="read_issue", annotations=annotations, structured_output=False)
async def read_issue(query: str) -> str:
    if mode == "timeout":
        await asyncio.sleep(2.0)
    if mode == "oversized":
        return "x" * 70_000
    return f"deterministic issue context: {query}"


if mode == "multiple":

    @server.tool(
        name="write_issue",
        annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True),
        structured_output=False,
    )
    async def write_issue(query: str) -> str:
        return query


if mode == "multi_read":

    @server.tool(name="read_note", annotations=annotations, structured_output=False)
    async def read_note(query: str) -> str:
        return f"deterministic note context: {query}"


server.run(transport="stdio")
