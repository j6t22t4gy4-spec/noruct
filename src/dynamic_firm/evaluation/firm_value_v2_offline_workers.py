"""Deterministic offline workers for the Firm Value v2 fixture harness.

They own scripted provider/worker/validator behavior only. Fixture identity,
artifact scoring, live-provider qualification, and report persistence remain in
their respective evaluation components.
"""

from __future__ import annotations

import asyncio
from typing import Mapping

from dynamic_firm.coding import CodingWorkResult, ValidationAttempt
from dynamic_firm.coding.ports import CodingValidatorPort
from dynamic_firm.runtime.models import CompletionEnvelope, ModelResponse, StructuredOutputResponse, Usage

from .closed_loop import CodingStrategyKind
from .firm_value_v2_execution import FirmValueV2FixtureKind, artifact_score_candidate


class _V2Provider:
    def __init__(self, plan: Mapping[str, object], *, count_compiler: bool) -> None:
        self.plan = plan
        self.count_compiler = count_compiler

    async def complete_structured(self, request, cancellation):
        cancellation.raise_if_cancelled()
        return StructuredOutputResponse(
            value=self.plan,
            usage=(Usage(input_tokens=11, output_tokens=7) if self.count_compiler else Usage()),
            provider_request_id="offline-v2-compiler",
        )

    async def complete(self, request, cancellation):
        cancellation.raise_if_cancelled()
        await asyncio.sleep(0)
        return ModelResponse(
            completion=CompletionEnvelope(
                summary="Deterministic dependency evidence prepared.",
                acceptance_evidence=("offline-v2:evidence",),
            ),
            usage=Usage(model_calls=1, input_tokens=5, output_tokens=3),
            provider_request_id="offline-v2-employee",
        )


class _V2Worker:
    def __init__(self, fixture: FirmValueV2FixtureKind, strategy: CodingStrategyKind) -> None:
        self.fixture = fixture
        self.strategy = strategy

    async def execute(self, request, cancellation):
        cancellation.raise_if_cancelled()
        if self.fixture == FirmValueV2FixtureKind.SOLO_EDIT:
            (request.workspace / "calculator.py").write_text(
                "def safe_divide(numerator: float, denominator: float) -> float | None:\n"
                "    if denominator == 0:\n"
                "        return None\n"
                "    return numerator / denominator\n",
                encoding="utf-8",
            )
        elif self.fixture == FirmValueV2FixtureKind.TEST_GUIDED_RECOVERY:
            content = (
                "def within_window(value: int, lower: int, upper: int) -> bool:\n"
                "    if lower > upper:\n"
                "        raise ValueError('lower must not exceed upper')\n"
                "    return lower <= value <= upper\n"
                if request.validation_feedback
                else "def within_window(value: int, lower: int, upper: int) -> bool:\n"
                "    return lower <= value <= upper\n"
            )
            (request.workspace / "window.py").write_text(content, encoding="utf-8")
        elif self.fixture == FirmValueV2FixtureKind.EVIDENCE_SYNTHESIS and self.strategy == CodingStrategyKind.DYNAMIC:
            (request.workspace / "delivery.py").write_text(
                "def route_delivery(channel: str, priority: int, verified: bool) -> str:\n"
                "    if channel not in {'direct', 'bulk'}:\n"
                "        raise ValueError('unsupported channel')\n"
                "    if type(priority) is not int or not 0 <= priority <= 10:\n"
                "        raise ValueError('priority must be an integer from 0 through 10')\n"
                "    if not verified:\n"
                "        return 'hold'\n"
                "    if priority >= 8:\n"
                "        return 'expedite'\n"
                "    return 'batch' if channel == 'bulk' else 'standard'\n",
                encoding="utf-8",
            )
        elif self.fixture == FirmValueV2FixtureKind.REVIEW_DEFECT_DETECTION and self.strategy == CodingStrategyKind.DYNAMIC:
            (request.workspace / "retry_policy.py").write_text(
                "def backoff_delay(attempt: int, base: int, cap: int) -> int:\n"
                "    values = (attempt, base, cap)\n"
                "    if any(type(value) is not int for value in values):\n"
                "        raise ValueError('arguments must be integers')\n"
                "    if attempt < 0 or base <= 0 or cap <= 0 or cap < base:\n"
                "        raise ValueError('invalid retry policy bounds')\n"
                "    return min(cap, base * 2**attempt)\n",
                encoding="utf-8",
            )
        return CodingWorkResult(
            summary="Prepared one bounded v2 shadow candidate.",
            acceptance_evidence=("offline-v2:shadow-change",),
            usage=Usage(model_calls=1, input_tokens=13, output_tokens=8),
            provider_request_id="offline-v2-shadow-worker",
        )


class _V2Validator(CodingValidatorPort):
    def __init__(self, fixture: FirmValueV2FixtureKind) -> None:
        self.fixture = fixture

    async def validate(self, request, cancellation):
        cancellation.raise_if_cancelled()
        score = artifact_score_candidate(self.fixture, request.workspace)
        failed = tuple(check.name for check in score.checks if not check.passed)
        expectations = {
            FirmValueV2FixtureKind.TEST_GUIDED_RECOVERY: {
                "reversed-bounds": "expect:raise-ValueError-when-lower-greater-than-upper",
            },
        }.get(self.fixture, {})
        hints = tuple(expectations[name] for name in failed if name in expectations)
        detail = "passed" if score.passed else " ".join(
            ("failed:" + ",".join(failed or ("change-scope",)), *hints)
        )
        return ValidationAttempt("noruct-firm-value-v2-validation", score.passed, detail)
