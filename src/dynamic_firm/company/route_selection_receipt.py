"""Canonical, content-free explanation of a route decision already made."""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from enum import StrEnum


_TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:@/-]{0,255}\Z")
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")


class HardConstraintRejection(StrEnum):
    AUTHORITY = "AUTHORITY"
    EGRESS = "EGRESS"
    CAPABILITY = "CAPABILITY"
    AVAILABILITY = "AVAILABILITY"
    CONTINUATION = "CONTINUATION"


class SelectionReason(StrEnum):
    HARD_CONSTRAINTS_SATISFIED = "HARD_CONSTRAINTS_SATISFIED"
    SIMPLE_ROUTE_TIE_PREFERENCE = "SIMPLE_ROUTE_TIE_PREFERENCE"
    POLICY_ORDER = "POLICY_ORDER"


def _token(value: object, name: str) -> str:
    if not isinstance(value, str) or not _TOKEN.fullmatch(value): raise ValueError(f"{name} must be a bounded opaque token")
    return value


@dataclass(frozen=True, slots=True)
class RouteCandidateReceipt:
    route_id: str
    rejected_by: tuple[HardConstraintRejection, ...] = ()
    uncertainty: float = 0.0

    def __post_init__(self) -> None:
        object.__setattr__(self, "route_id", _token(self.route_id, "route_id"))
        if tuple(sorted(set(self.rejected_by), key=str)) != self.rejected_by: raise ValueError("rejections must be unique and sorted")
        if any(not isinstance(item, HardConstraintRejection) for item in self.rejected_by): raise ValueError("rejection is unknown")
        if isinstance(self.uncertainty, bool) or not isinstance(self.uncertainty, (int, float)) or not 0 <= self.uncertainty <= 1: raise ValueError("uncertainty must be in [0, 1]")


@dataclass(frozen=True, slots=True)
class RouteSelectionReceipt:
    candidates: tuple[RouteCandidateReceipt, ...]
    selected_route_id: str | None
    selection_reasons: tuple[SelectionReason, ...]
    policy_digest: str

    def __post_init__(self) -> None:
        if not self.candidates or len({item.route_id for item in self.candidates}) != len(self.candidates): raise ValueError("candidates must be nonempty and unique")
        if any(not isinstance(item, RouteCandidateReceipt) for item in self.candidates): raise ValueError("candidates must be typed")
        if not isinstance(self.policy_digest, str) or not _DIGEST.fullmatch(self.policy_digest): raise ValueError("policy_digest must be sha256")
        if any(not isinstance(item, SelectionReason) for item in self.selection_reasons): raise ValueError("selection reason is unknown")
        selected = self.selected_candidate
        if self.selected_route_id is None:
            if self.selection_reasons: raise ValueError("no selection cannot have reasons")
        elif selected is None or selected.rejected_by or not self.selection_reasons:
            raise ValueError("selected route must be an unrejected candidate with reasons")

    @property
    def selected_candidate(self) -> RouteCandidateReceipt | None:
        return next((item for item in self.candidates if item.route_id == self.selected_route_id), None)

    def canonical_payload(self) -> dict[str, object]:
        return {"candidates":[{"route_id":c.route_id,"rejected_by":[x.value for x in c.rejected_by],"uncertainty":c.uncertainty} for c in self.candidates],"selected_route_id":self.selected_route_id,"selection_reasons":[x.value for x in self.selection_reasons],"policy_digest":self.policy_digest}

    def canonical_json(self) -> str: return json.dumps(self.canonical_payload(), sort_keys=True, separators=(",",":"))
    @property
    def digest(self) -> str: return hashlib.sha256(self.canonical_json().encode()).hexdigest()

    def explanation(self) -> tuple[str, ...]:
        """Fixed labels; rejected routes are never assigned a comparative rank."""
        return tuple(reason.value for reason in self.selection_reasons)

    @classmethod
    def from_canonical_json(cls, raw: object) -> "RouteSelectionReceipt":
        try: value=json.loads(raw) if isinstance(raw,str) else None
        except json.JSONDecodeError as exc: raise ValueError("receipt JSON is invalid") from exc
        if not isinstance(value,dict) or set(value)!={"candidates","selected_route_id","selection_reasons","policy_digest"} or not isinstance(value["candidates"],list): raise ValueError("receipt JSON fields are invalid")
        return cls(tuple(RouteCandidateReceipt(item["route_id"],tuple(HardConstraintRejection(x) for x in item["rejected_by"]),item["uncertainty"]) for item in value["candidates"]),value["selected_route_id"],tuple(SelectionReason(x) for x in value["selection_reasons"]),value["policy_digest"])
