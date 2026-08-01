from __future__ import annotations

import contextlib
import importlib.util
import json
import os
import sys
from pathlib import Path
from typing import Callable


def _load(path: Path):
    spec = importlib.util.spec_from_file_location("noruct_fixture_candidate", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("Candidate module could not be loaded")
    module = importlib.util.module_from_spec(spec)
    with open(os.devnull, "w", encoding="utf-8") as sink:
        with contextlib.redirect_stdout(sink), contextlib.redirect_stderr(sink):
            spec.loader.exec_module(module)
    return module


def _check(name: str, operation: Callable[[], bool]) -> dict:
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


def _checks(fixture: str, workspace: Path) -> list[dict]:
    if fixture == "solo-edit":
        module = _load(workspace / "calculator.py")
        return [
            _check("zero-denominator", lambda: module.safe_divide(10, 0) is None),
            _check("regular-division", lambda: module.safe_divide(9, 3) == 3),
            _check("negative-denominator", lambda: module.safe_divide(10, -2) == -5),
        ]
    if fixture == "parallel-evidence":
        module = _load(workspace / "identifier.py")
        return [
            _check("space-normalization", lambda: module.canonical_identifier(" Release  Candidate ") == "release-candidate"),
            _check("underscore-normalization", lambda: module.canonical_identifier("release__candidate") == "release-candidate"),
            _check("mixed-separator-collapse", lambda: module.canonical_identifier(" A_  B ") == "a-b"),
        ]
    if fixture == "test-guided-recovery":
        module = _load(workspace / "window.py")
        return [
            _check("lower-bound-inclusive", lambda: module.within_window(1, 1, 3) is True),
            _check("upper-bound-inclusive", lambda: module.within_window(3, 1, 3) is True),
            _check("outside-window", lambda: module.within_window(4, 1, 3) is False),
            _check("reversed-bounds", lambda: _raises_value_error(lambda: module.within_window(2, 3, 1))),
        ]
    raise ValueError("Unknown fixture")


def main() -> int:
    try:
        fixture = sys.argv[1]
        workspace = Path(sys.argv[2]).resolve()
        checks = _checks(fixture, workspace)
        payload = {"passed": all(item["passed"] for item in checks), "checks": checks}
    except Exception as exc:
        payload = {
            "passed": False,
            "checks": [{"name": "fixture-load", "passed": False, "message": type(exc).__name__}],
        }
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    return 0 if payload["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
