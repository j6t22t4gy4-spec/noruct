"""Foundation CLI components composed by the global command ingress."""

from .evidence_parser import add_foundation_evidence_commands
from .core_parser import add_foundation_core_commands

__all__ = ["add_foundation_core_commands", "add_foundation_evidence_commands"]
