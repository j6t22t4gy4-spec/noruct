"""Noruct-owned command bridge for the forked Hermes CLI.

The product shell remains available for Company-specific subcommands while
Hermes' interactive terminal is reachable through the same installed command
with ``--hermes``.  The bridge owns argument translation; it does not create a
second agent loop.
"""

from __future__ import annotations

import argparse
import os
import sys
import tomllib
from pathlib import Path
from pathlib import Path
from typing import Sequence


def _hermes_main():
    root = Path(__file__).parents[1]
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    try:
        from cli import main as run
    except Exception as exc:  # pragma: no cover - qualified profile owns deps
        raise RuntimeError(
            "Noruct fork CLI dependencies are unavailable; install the modern CLI profile"
        ) from exc
    return run


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="noruct --hermes")
    parser.add_argument("-q", "--query")
    parser.add_argument("--image")
    parser.add_argument("--toolsets")
    parser.add_argument("--skills")
    parser.add_argument("--model")
    parser.add_argument("--provider")
    parser.add_argument("--api-key")
    parser.add_argument("--base-url")
    parser.add_argument("--max-turns", type=int)
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--quiet", action="store_true")
    parser.add_argument("--compact", action="store_true")
    parser.add_argument("--list-tools", action="store_true")
    parser.add_argument("--list-toolsets", action="store_true")
    parser.add_argument("--gateway", action="store_true")
    parser.add_argument("--resume")
    parser.add_argument("--config", default=os.environ.get("NORUCT_CONFIG", ""))
    parser.add_argument("-w", "--worktree", action="store_true")
    return parser


def _global_defaults(path: str) -> dict[str, object]:
    """Read only secret-free provider defaults from Noruct's global profile."""
    if not path:
        return {}
    try:
        with open(os.path.expanduser(path), "rb") as handle:
            raw = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError):
        return {}
    provider = raw.get("provider", {})
    if not isinstance(provider, dict):
        return {}
    values: dict[str, object] = {}
    if provider.get("model"):
        values["model"] = str(provider["model"])
    if provider.get("base_url"):
        values["base_url"] = str(provider["base_url"])
    # Keep provider naming private to this compatibility bridge.  The public
    # authority remains Noruct's provider profile and settings center.
    aliases = {"openai_codex": "openai-codex", "anthropic": "anthropic", "openai_api": "openai"}
    if provider.get("kind") in aliases:
        values["provider"] = aliases[str(provider["kind"])]
    return values


def _company_surface_line(config_path: str) -> str:
    """Return a content-light Company snapshot for the diagnostic fork TUI."""
    try:
        from dynamic_firm.company.store import CompanyStateStore
        raw_path = "~/.noruct/runtime.db"
        if config_path:
            with open(os.path.expanduser(config_path), "rb") as handle:
                raw = tomllib.load(handle)
            run = raw.get("run", {})
            if isinstance(run, dict) and run.get("state"):
                raw_path = str(run["state"])
        state_path = Path(raw_path).expanduser().resolve()
        if not state_path.exists():
            return "Company state · not initialized"
        with CompanyStateStore(state_path) as store:
            roster = store.roster()
            active = sum(1 for employee in roster.employees if str(employee.get("status", "ACTIVE")) == "ACTIVE")
            return f"Company · roster r{roster.revision} · {active} active employees · {state_path}"
    except Exception:
        return "Company state · unavailable (direct fork diagnostics)"


def main(argv: Sequence[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    parsed = _parser().parse_args(args)
    values = vars(parsed)
    config_path = str(values.get("config") or "")
    defaults = _global_defaults(config_path)
    for key, value in defaults.items():
        if not values.get(key):
            values[key] = value
    values.pop("config", None)
    stream = sys.stderr if values.get("query") or values.get("q") else sys.stdout
    print(_company_surface_line(config_path or os.environ.get("NORUCT_CONFIG", "")), file=stream)
    result = _hermes_main()(**values)
    return int(result or 0)
