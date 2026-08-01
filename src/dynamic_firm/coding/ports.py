from __future__ import annotations

from typing import Protocol

from dynamic_firm.runtime.ports import CancellationToken

from .models import CodingWorkRequest, CodingWorkResult, ValidationAttempt


class CodingWorkerError(Exception):
    """Safe failure from a replaceable external coding execution surface."""

    def __init__(self, code: str, message_safe: str, *, retryable: bool) -> None:
        super().__init__(message_safe)
        self.code = code
        self.message_safe = message_safe
        self.retryable = retryable


class CodingWorkerPort(Protocol):
    async def execute(
        self,
        request: CodingWorkRequest,
        cancellation: CancellationToken,
    ) -> CodingWorkResult: ...


class CodingValidatorPort(Protocol):
    """First-party validator for one candidate in a disposable shadow."""

    async def validate(
        self,
        request: CodingWorkRequest,
        cancellation: CancellationToken,
    ) -> ValidationAttempt: ...
