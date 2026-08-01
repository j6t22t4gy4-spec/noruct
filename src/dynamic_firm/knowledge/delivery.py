from __future__ import annotations

from dynamic_firm.runtime.models import TaskEvidenceItem, TaskEvidencePack

from .models import EvidencePack


def runtime_delivery_from_evidence_pack(pack: EvidencePack) -> TaskEvidencePack:
    """Build the exact redacted, hash-bound provider delivery for a persisted pack."""

    pack.verify()
    provisional = TaskEvidencePack(
        pack_id=pack.pack_id,
        revision=pack.revision,
        pack_digest=pack.digest,
        delivery_digest="",
        access_scope=pack.access_scope,
        items=tuple(
            TaskEvidenceItem(
                citation_id=item.evidence_id,
                source_id=item.source_id,
                source_revision=item.source_revision,
                title=item.title,
                content=item.excerpt,
                source_hash=item.content_hash,
                content_hash=item.excerpt_hash,
                location=dict(item.location),
            )
            for item in pack.items
        ),
    )
    return provisional.redacted()
