"""Public compatibility facade for the explicit CLI component contract."""

from __future__ import annotations

from typing import Any

from dynamic_firm.application.cli_component_contract import cli as _cli


def __getattr__(name: str) -> Any:
    """Preserve the legacy CLI import surface without mutating components."""

    return getattr(_cli, name)


if __name__ == "__main__":
    __getattr__("main")()
