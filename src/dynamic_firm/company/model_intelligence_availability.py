"""Pure no-I/O intelligence availability resolution."""
from dataclasses import dataclass
from enum import StrEnum
class AvailabilityState(StrEnum): MISSING="MISSING"; STALE="STALE"; EXPIRED="EXPIRED"; INVALID="INVALID"; OFFLINE="OFFLINE"
@dataclass(frozen=True,slots=True)
class AvailabilityOutcome: identity:str; rollback_identity:str; source:str; state:AvailabilityState|None; explicit_route:str|None
def resolve(*,retained_identity:str|None,retained_valid:bool,state:AvailabilityState|None,bundled_default_identity:str,explicit_route:str|None=None)->AvailabilityOutcome:
 if retained_identity and retained_valid:return AvailabilityOutcome(retained_identity,retained_identity,"LAST_KNOWN_GOOD",None,explicit_route)
 if explicit_route:return AvailabilityOutcome(explicit_route,retained_identity or explicit_route,"EXPLICIT_ROUTE",state or AvailabilityState.MISSING,explicit_route)
 if not bundled_default_identity:raise ValueError("bundled default identity required")
 return AvailabilityOutcome(bundled_default_identity,retained_identity or bundled_default_identity,"BUNDLED_CONSERVATIVE_DEFAULT",state or AvailabilityState.MISSING,explicit_route)
