from __future__ import annotations

import unittest

from dynamic_firm._vendor.mini_swe_runtime import run_bounded_step_loop


class MiniSweBoundedStepLoopTests(unittest.IsolatedAsyncioTestCase):
    async def test_observation_controls_the_next_step_in_order(self) -> None:
        calls: list[str] = []

        async def run_step(index: int) -> str:
            calls.append(f"step:{index}")
            return f"candidate:{index}"

        async def observe_step(index: int, candidate: str) -> bool:
            calls.append(f"observe:{index}:{candidate}")
            return index == 2

        result = await run_bounded_step_loop(
            max_steps=2,
            run_step=run_step,
            observe_step=observe_step,
            should_continue=lambda index, passed: not passed,
        )

        self.assertEqual(result.steps, ("candidate:1", "candidate:2"))
        self.assertEqual(result.observations, (False, True))
        self.assertIsNone(result.admission_blocked_reason)
        self.assertEqual(
            calls,
            [
                "step:1",
                "observe:1:candidate:1",
                "step:2",
                "observe:2:candidate:2",
            ],
        )

    async def test_admission_reason_stops_before_the_next_step(self) -> None:
        calls: list[int] = []

        async def run_step(index: int) -> str:
            calls.append(index)
            return f"candidate:{index}"

        async def observe_step(index: int, candidate: str) -> bool:
            return False

        result = await run_bounded_step_loop(
            max_steps=2,
            run_step=run_step,
            observe_step=observe_step,
            should_continue=lambda index, passed: not passed,
            admission_reason=lambda index: (
                "max_output_tokens" if index == 2 else None
            ),
        )

        self.assertEqual(calls, [1])
        self.assertEqual(result.steps, ("candidate:1",))
        self.assertEqual(result.observations, (False,))
        self.assertEqual(result.admission_blocked_reason, "max_output_tokens")


if __name__ == "__main__":
    unittest.main()
