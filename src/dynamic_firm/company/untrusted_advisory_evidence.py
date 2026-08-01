"""Bounded untrusted advisory evidence for an aggregator-owned prompt surface."""
from __future__ import annotations
import hashlib
from dataclasses import dataclass
from enum import StrEnum

_MAX_BYTES = 16_384

class AdvisoryAvailability(StrEnum): AVAILABLE="AVAILABLE"; UNAVAILABLE="UNAVAILABLE"

@dataclass(frozen=True, slots=True)
class UntrustedAdvisoryEvidence:
    source_label: str
    availability: AdvisoryAvailability
    text: str | None
    structured_digest: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.source_label,str) or not self.source_label or any(char.isspace() for char in self.source_label): raise ValueError("source_label must be an opaque token")
        if not isinstance(self.availability,AdvisoryAvailability): object.__setattr__(self,"availability",AdvisoryAvailability(self.availability))
        if self.availability is AdvisoryAvailability.UNAVAILABLE:
            if self.text is not None or self.structured_digest is not None: raise ValueError("unavailable advisory evidence cannot carry content")
            return
        if not isinstance(self.text,str) or not self.text or len(self.text.encode())>_MAX_BYTES: raise ValueError("advisory text is missing or oversized")
        if self.structured_digest is not None and (len(self.structured_digest)!=64 or any(char not in "0123456789abcdef" for char in self.structured_digest)): raise ValueError("structured evidence digest is invalid")

    @property
    def evidence_digest(self) -> str:
        if self.text is None: return hashlib.sha256(f"{self.source_label}:unavailable".encode()).hexdigest()
        return hashlib.sha256(self.text.encode()).hexdigest()

    def aggregator_message(self) -> dict[str,str]:
        """Only a labelled user-content envelope; never a system/tool message."""
        if self.availability is AdvisoryAvailability.UNAVAILABLE:
            return {"role":"user","content":f"[untrusted advisory {self.source_label}: unavailable]"}
        return {"role":"user","content":f"[untrusted advisory {self.source_label}]\n{self.text}"}
