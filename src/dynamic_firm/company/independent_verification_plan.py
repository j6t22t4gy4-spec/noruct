"""Provider-free admission checks for independent exploration and verification."""
from __future__ import annotations
import math
import re
from dataclasses import dataclass

_DIGEST=re.compile(r"[0-9a-f]{64}\Z")
def _digest(value, field):
 if not isinstance(value,str) or not _DIGEST.fullmatch(value): raise ValueError(f"{field} must be sha256")
 return value
def _correlation(value):
 if isinstance(value,bool) or not isinstance(value,(int,float)) or not math.isfinite(value) or not -1<=value<=1: raise ValueError("error_correlation must be a signed finite coefficient")
 return float(value)
@dataclass(frozen=True,slots=True)
class IndependentCallShape:
 provider_route_digest:str; model_identity_digest:str; context_projection_digest:str; source_projection_digest:str; tools_enabled:bool; read_only:bool; availability_fallback:bool=False
 def __post_init__(self):
  for field in ("provider_route_digest","model_identity_digest","context_projection_digest","source_projection_digest"): object.__setattr__(self,field,_digest(getattr(self,field),field))
@dataclass(frozen=True,slots=True)
class IndependentVerificationPlan:
 candidate:IndependentCallShape; verifier:IndependentCallShape; error_correlation:float
 def __post_init__(self):
  if not isinstance(self.candidate,IndependentCallShape) or not isinstance(self.verifier,IndependentCallShape): raise TypeError("typed call shapes are required")
  object.__setattr__(self,"error_correlation",_correlation(self.error_correlation))
  if self.candidate.tools_enabled or not self.verifier.read_only or self.verifier.tools_enabled: raise ValueError("candidates must be no-tools and verifier must be read-only")
 @property
 def effectively_independent(self):
  if self.candidate.availability_fallback or self.verifier.availability_fallback or self.error_correlation>0.5: return False
  route_differs=(self.candidate.provider_route_digest!=self.verifier.provider_route_digest or self.candidate.model_identity_digest!=self.verifier.model_identity_digest)
  return route_differs and self.candidate.context_projection_digest!=self.verifier.context_projection_digest and self.candidate.source_projection_digest!=self.verifier.source_projection_digest
