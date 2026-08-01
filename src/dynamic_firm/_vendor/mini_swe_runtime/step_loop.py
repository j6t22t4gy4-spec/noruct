"""Bounded asynchronous step control-flow derived from mini-swe-agent.

Upstream: SWE-agent/mini-swe-agent
Commit: e187bcb2ff5825d85761a6f9c1f98c9fa6cfbc79
Path: src/minisweagent/agents/default.py
Upstream SHA-256: 547449ce3bdffd4767d38eb281bdd23b27b197165e74087e87b9b5d4a2528fa9
Copyright (c) 2025 Kilian A. Lieret and Carlos E. Jimenez
SPDX-License-Identifier: MIT

Modified for Noruct: retained the admission-before-step and
step-result-to-observation loop, converted it to dependency-free async
callbacks, returned a typed bounded trajectory, and removed model, shell,
template, traceback, configuration, and file-persistence responsibilities.
See the adjacent LICENSE and THIRD_PARTY_NOTICES.md.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Generic, TypeVar


_Step = TypeVar("_Step")
_Observation = TypeVar("_Observation")


@dataclass(frozen=True, slots=True)
class BoundedStepLoopResult(Generic[_Step, _Observation]):
    """Completed steps, ordered observations, and an optional admission refusal."""

    steps: tuple[_Step, ...]
    observations: tuple[_Observation, ...]
    admission_blocked_reason: str | None = None


async def run_bounded_step_loop(
    *,
    max_steps: int,
    run_step: Callable[[int], Awaitable[_Step]],
    observe_step: Callable[[int, _Step], Awaitable[_Observation]],
    should_continue: Callable[[int, _Observation], bool],
    admission_reason: Callable[[int], str | None] | None = None,
) -> BoundedStepLoopResult[_Step, _Observation]:
    """Run a small query/action/observation-style loop with pre-step admission.

    The caller owns cancellation, usage accounting, effects, and persistence.
    A non-empty admission reason prevents the next step and is returned without
    converting it into an external exception or losing the exact limit name.
    """

    if type(max_steps) is not int or not 1 <= max_steps <= 16:
        raise ValueError("Bounded step loop requires max_steps between 1 and 16")
    steps: list[_Step] = []
    observations: list[_Observation] = []
    for step_index in range(1, max_steps + 1):
        reason = admission_reason(step_index) if admission_reason is not None else None
        if reason is not None:
            normalized = str(reason).strip()
            if not normalized:
                raise ValueError("Bounded step admission reason must be non-empty")
            return BoundedStepLoopResult(
                tuple(steps),
                tuple(observations),
                normalized,
            )
        step = await run_step(step_index)
        steps.append(step)
        observation = await observe_step(step_index, step)
        observations.append(observation)
        if not should_continue(step_index, observation):
            break
    return BoundedStepLoopResult(tuple(steps), tuple(observations))
