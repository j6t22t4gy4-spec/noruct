"""Evaluation-owned bridge for bounded Product-surface parity checks.

Foundation parity owns runtime scenarios.  Product-facing command, event, and
terminal types are supplied from this evaluation boundary so the Foundation
package never imports the Product CLI directly.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from dynamic_firm.cli import RunCommandConfig, run_goal
from dynamic_firm.product.events import ProductEventType
from dynamic_firm.product.routing import InputRoute
from dynamic_firm.product.tui import InlineTerminalUI


@dataclass(frozen=True, slots=True)
class ProductPreviewContract:
    """The narrow Product API required by offline evaluation fixtures."""

    run_command_config: type[Any]
    run_goal: Callable[..., Any]
    product_event_type: type[Any]
    input_route: type[Any]
    terminal_ui: type[Any]


def product_preview_contract() -> ProductPreviewContract:
    return ProductPreviewContract(
        run_command_config=RunCommandConfig,
        run_goal=run_goal,
        product_event_type=ProductEventType,
        input_route=InputRoute,
        terminal_ui=InlineTerminalUI,
    )
