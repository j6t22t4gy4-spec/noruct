"""Surface-neutral, secret-safe Knowledge previews for CLI/TUI/future GUI."""
from __future__ import annotations

from dataclasses import dataclass
from dynamic_firm.runtime.redaction import redact_prompt_text
from .folder_models import KnowledgeFolderEntry
from .structure import KnowledgeStructureHint, inspect_extracted_text

@dataclass(frozen=True, slots=True)
class KnowledgePreview:
    entry: KnowledgeFolderEntry
    content: str
    selected_bytes: int
    truncated: bool
    redacted: bool
    structure: KnowledgeStructureHint

def safe_folder_preview(entry: KnowledgeFolderEntry, content: str, *, selected_bytes: int, truncated: bool) -> KnowledgePreview:
    redacted = redact_prompt_text(content)
    return KnowledgePreview(entry, redacted, selected_bytes, truncated, redacted != content, inspect_extracted_text(redacted))
