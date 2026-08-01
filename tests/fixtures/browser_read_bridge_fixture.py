"""Small private-bridge fixture for BrowserReadOnlyConnector tests."""

from __future__ import annotations

import json
import sys


PROTOCOL = "noruct-local-browser-v2"


def main() -> int:
    request = json.loads(sys.stdin.buffer.read().decode("utf-8"))
    operation = request.get("operation")
    if operation == "list":
        result = {"tabs": [{"tab_index": 1, "title": "Fixture", "url": "https://example.test/"}]}
    elif operation == "snapshot":
        result = {
            "tab_index": request["tab_index"],
            "title": "Fixture",
            "url": "https://example.test/",
            "text": "fixture page evidence",
        }
    elif operation == "navigate":
        result = {"tab_index": request["tab_index"], "operation": "navigate", "target_url": request["url"]}
    elif operation in {"click", "type"}:
        result = {"tab_index": request["tab_index"], "operation": operation, "changed": True}
    elif operation == "screenshot":
        result = {
            "tab_index": request["tab_index"],
            "operation": "screenshot",
            "png_base64": "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Y9Jv5wAAAAASUVORK5CYII=",
        }
    else:
        print(json.dumps({"protocol": PROTOCOL, "ok": False, "error_code": "INVALID_REQUEST"}))
        return 2
    print(json.dumps({"protocol": PROTOCOL, "ok": True, "result": result}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
