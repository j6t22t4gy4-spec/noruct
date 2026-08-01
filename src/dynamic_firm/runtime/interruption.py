"""Typed interruption and recovery decisions for first-party runtime surfaces.

An interruption cause describes what the local runtime observed.  It is not
evidence that a provider, process, or external system stopped before producing
an effect.  Recovery disposition and operator actions therefore remain
separate from the cause.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class InterruptionCause(StrEnum):
    USER_CANCEL = "USER_CANCEL"
    PROCESS_OR_MACHINE_LOSS = "PROCESS_OR_MACHINE_LOSS"
    DEADLINE_TIMEOUT = "DEADLINE_TIMEOUT"
    PROVIDER_DISCONNECT = "PROVIDER_DISCONNECT"
    UNKNOWN = "UNKNOWN"


class RecoveryDisposition(StrEnum):
    NO_RECOVERY_REQUIRED = "NO_RECOVERY_REQUIRED"
    RECEIPT_BOUND_READ_ONLY_CONTINUATION = "RECEIPT_BOUND_READ_ONLY_CONTINUATION"
    NEW_KERNEL_ATTEMPT_REQUIRED = "NEW_KERNEL_ATTEMPT_REQUIRED"
    RECONCILE_OR_COMPENSATE_REQUIRED = "RECONCILE_OR_COMPENSATE_REQUIRED"
    FAIL_CLOSED = "FAIL_CLOSED"


class EffectRecoveryOutcome(StrEnum):
    """An explicit operator conclusion recorded beside immutable evidence."""

    CONFIRMED_SUCCEEDED = "CONFIRMED_SUCCEEDED"
    CONFIRMED_NO_EFFECT = "CONFIRMED_NO_EFFECT"
    COMPENSATED = "COMPENSATED"
    SEALED_UNKNOWN = "SEALED_UNKNOWN"

    @property
    def releases_resource(self) -> bool:
        return self is not EffectRecoveryOutcome.SEALED_UNKNOWN


class EffectInterruptionReason(StrEnum):
    USER_CANCEL = "USER_CANCEL"
    DEADLINE_TIMEOUT = "DEADLINE_TIMEOUT"
    HANDLER_ERROR = "HANDLER_ERROR"
    PROCESS_OR_MACHINE_LOSS = "PROCESS_OR_MACHINE_LOSS"
    TERMINAL_RECEIPT_FAILURE = "TERMINAL_RECEIPT_FAILURE"


@dataclass(frozen=True, slots=True)
class RecoveryActionPreview:
    """Non-executing semantics shown before an operator recovery choice."""

    action: str
    enabled: bool
    requires_confirmation: bool
    creates_new_work_order: bool
    expected_effect: str
    reason: str
