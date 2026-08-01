from __future__ import annotations

import json
import sys


PROTOCOL = "noruct-external-read-v1"


request = json.load(sys.stdin)
if request.get("transport") == "streamable_http":
    if (
        request.get("server_url") != "https://mcp.example.invalid/v1"
        or request.get("http_headers") != {"Authorization": "fixture-http-token"}
        or "server_command" in request
    ):
        json.dump({"protocol": PROTOCOL, "ok": False, "error_code": "SDK_PROTOCOL_FAILURE"}, sys.stdout)
        raise SystemExit(2)
mode = request.get("server_args", ["normal"])[0]
tool = {
    "name": "read_issue",
    "input_schema": {
        "type": "object",
        "title": "Untrusted external title",
        "properties": {"query": {"type": "string", "title": "Query"}},
        "required": ["query"],
    },
    "read_only": mode != "write",
    "destructive": mode == "write",
    "open_world": True,
    "task_support": None,
}
if mode == "malformed-bridge":
    sys.stdout.write("not-json")
    raise SystemExit(0)
if mode == "bridge-error":
    json.dump({"protocol": PROTOCOL, "ok": False, "error_code": "SDK_PROTOCOL_FAILURE"}, sys.stdout)
    raise SystemExit(2)
tools = [tool, {**tool, "name": "second_tool"}] if mode == "multiple" else [tool]
response = {
    "protocol": PROTOCOL,
    "ok": True,
    "tools": tools,
    "has_more_tools": False,
}
if request["operation"] == "call":
    text = "x" * 70_000 if mode == "oversized" else f"issue context for {request['arguments']['query']}"
    response["result"] = {
        "content": [{"type": "text", "text": text}],
        "structuredContent": None,
        "isError": mode == "remote-error",
    }
json.dump(response, sys.stdout, ensure_ascii=False, sort_keys=True)
