"""Fail-closed fallback admissibility before any route substitution."""
from __future__ import annotations
from dataclasses import dataclass
from enum import StrEnum
class FallbackFailureKind(StrEnum): TRANSPORT="TRANSPORT"; RATE_LIMIT="RATE_LIMIT"; AUTH="AUTH"; POLICY="POLICY"; CANCEL="CANCEL"; OTHER="OTHER"
class FallbackDecision(StrEnum): ALLOWED="ALLOWED"; DENIED="DENIED"
@dataclass(frozen=True,slots=True)
class FallbackAttemptState:
 equivalence_group_preapproved:bool; retryable:bool; partial_stream:bool; effect_started:bool; failure_kind:FallbackFailureKind
 def __post_init__(self): object.__setattr__(self,"failure_kind",FallbackFailureKind(self.failure_kind))
def admit_fallback(state:FallbackAttemptState)->FallbackDecision:
 if not isinstance(state,FallbackAttemptState): raise TypeError("typed fallback state is required")
 if state.equivalence_group_preapproved and state.retryable and not state.partial_stream and not state.effect_started and state.failure_kind in {FallbackFailureKind.TRANSPORT,FallbackFailureKind.RATE_LIMIT}: return FallbackDecision.ALLOWED
 return FallbackDecision.DENIED
