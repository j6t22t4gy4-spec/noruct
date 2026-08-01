from __future__ import annotations

import contextlib
import importlib.util
import json
import os
import sys
from pathlib import Path
from typing import Callable


def _load(path: Path):
    spec = importlib.util.spec_from_file_location("noruct_fixture_v2_candidate", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("Candidate module could not be loaded")
    module = importlib.util.module_from_spec(spec)
    with open(os.devnull, "w", encoding="utf-8") as sink:
        with contextlib.redirect_stdout(sink), contextlib.redirect_stderr(sink):
            spec.loader.exec_module(module)
    return module


def _check(name: str, operation: Callable[[], bool]) -> dict[str, object]:
    try:
        with open(os.devnull, "w", encoding="utf-8") as sink:
            with contextlib.redirect_stdout(sink), contextlib.redirect_stderr(sink):
                passed = operation() is True
        return {"name": name, "passed": passed, "message": "" if passed else "Expectation failed."}
    except Exception as exc:
        return {"name": name, "passed": False, "message": type(exc).__name__}


def _raises_value_error(operation: Callable[[], object]) -> bool:
    try:
        operation()
    except ValueError:
        return True
    return False


def _checks(fixture: str, workspace: Path) -> list[dict[str, object]]:
    if fixture == "evidence-synthesis":
        module = _load(workspace / "delivery.py")
        return [
            _check("unverified-hold", lambda: module.route_delivery("bulk", 10, False) == "hold"),
            _check("urgent-expedite", lambda: module.route_delivery("direct", 8, True) == "expedite"),
            _check("bulk-batch", lambda: module.route_delivery("bulk", 4, True) == "batch"),
            _check("direct-standard", lambda: module.route_delivery("direct", 3, True) == "standard"),
            _check("invalid-channel", lambda: _raises_value_error(lambda: module.route_delivery("mail", 3, True))),
            _check(
                "invalid-priority",
                lambda: _raises_value_error(lambda: module.route_delivery("direct", True, True))
                and _raises_value_error(lambda: module.route_delivery("direct", 11, True)),
            ),
        ]
    if fixture == "review-defect-detection":
        module = _load(workspace / "retry_policy.py")
        return [
            _check("initial-delay", lambda: module.backoff_delay(0, 2, 20) == 2),
            _check("exponential-delay", lambda: module.backoff_delay(3, 2, 20) == 16),
            _check("capped-delay", lambda: module.backoff_delay(5, 2, 10) == 10),
            _check("negative-attempt", lambda: _raises_value_error(lambda: module.backoff_delay(-1, 2, 10))),
            _check("boolean-attempt", lambda: _raises_value_error(lambda: module.backoff_delay(True, 2, 10))),
            _check(
                "invalid-bounds",
                lambda: _raises_value_error(lambda: module.backoff_delay(1, 0, 10))
                and _raises_value_error(lambda: module.backoff_delay(1, 10, 2)),
            ),
        ]
    raise ValueError("Unknown fixture")


def main() -> int:
    try:
        fixture = sys.argv[1]
        workspace = Path(sys.argv[2]).resolve()
        checks = _checks(fixture, workspace)
        payload = {"passed": all(bool(item["passed"]) for item in checks), "checks": checks}
    except Exception as exc:
        payload = {
            "passed": False,
            "checks": [{"name": "fixture-load", "passed": False, "message": type(exc).__name__}],
        }
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    return 0 if payload["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
