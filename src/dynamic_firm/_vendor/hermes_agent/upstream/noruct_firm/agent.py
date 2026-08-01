"""Active agent-core loader for the Noruct Hermes fork."""

from __future__ import annotations

import importlib
from types import ModuleType
from typing import Any


def load_core() -> ModuleType:
    """Load the pinned Hermes core from the active fork tree."""

    return importlib.import_module("run_agent")


def create_agent(core: ModuleType, **kwargs: Any) -> Any:
    """Construct the core agent through the fork-owned seam."""

    agent = core.AIAgent(**kwargs)
    attach = getattr(core, "attach_noruct_company_context", None)
    if attach is not None:
        attach(agent, {})
    return agent
