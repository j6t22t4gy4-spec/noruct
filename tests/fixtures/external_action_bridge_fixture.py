from __future__ import annotations

import json
import sys


PROTOCOL = "noruct-external-read-v1"


request = json.load(sys.stdin)
if request.get("transport") != "streamable_http" or request.get("server_url") != "https://mcp-action.example.invalid/v1":
    json.dump({"protocol": PROTOCOL, "ok": False, "error_code": "SDK_PROTOCOL_FAILURE"}, sys.stdout)
    raise SystemExit(2)
if request.get("http_headers") != {"Authorization": "fixture-action-token"}:
    json.dump({"protocol": PROTOCOL, "ok": False, "error_code": "SDK_PROTOCOL_FAILURE"}, sys.stdout)
    raise SystemExit(2)

tool = {
    "name": "write_ticket",
    "input_schema": {
        "type": "object",
        "properties": {"query": {"type": "string"}},
        "required": ["query"],
    },
    "read_only": False,
    "destructive": True,
    "open_world": True,
    "task_support": None,
}
response: dict[str, object] = {
    "protocol": PROTOCOL,
    "ok": True,
    "tools": [tool],
    "has_more_tools": False,
}
if request["operation"] == "call":
    response["result"] = {
        "content": [{"type": "text", "text": f"ticket written: {request['arguments']['query']}"}],
        "structuredContent": None,
        "isError": False,
    }
json.dump(response, sys.stdout, ensure_ascii=False, sort_keys=True)
