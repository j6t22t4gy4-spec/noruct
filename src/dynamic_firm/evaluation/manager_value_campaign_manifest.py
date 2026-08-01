"""Immutable manifest identity for one Manager-value campaign."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

from dynamic_firm.runtime.models import to_primitive


MANAGER_CAMPAIGN_SCHEMA = "noruct.manager-value-campaign.v4"
LEGACY_MANAGER_CAMPAIGN_SCHEMAS = frozenset({"noruct.manager-value-campaign.v3"})


@dataclass(frozen=True, slots=True)
class ManagerValueCampaignManifest:
    """Frozen source, comparison, and fixture identity for a sealed campaign."""

    schema_version: str
    benchmark_id: str
    content_hash: str
    created_at: str
    source_revision: str
    distribution_sha256: str
    model_id: str
    company_revision: int
    roster_revision: int
    playbook_revision: int
    max_total_model_calls: int
    max_wall_time_ms: int
    slots: tuple[tuple[str, str], ...]
    # The fixture name alone is not an immutable evaluation input: fixture
    # semantics can evolve while an old campaign ledger remains valuable
    # historical evidence. v4 freezes the exact revision alongside each
    # fixture so status/report verification never silently consults today's
    # contract for a record that was sealed against yesterday's source.
    fixture_revisions: tuple[tuple[str, str], ...] = ()

    def content_payload(self) -> Mapping[str, object]:
        payload = {
            key: value
            for key, value in to_primitive(self).items()
            if key not in {"benchmark_id", "content_hash"}
        }
        if self.schema_version in LEGACY_MANAGER_CAMPAIGN_SCHEMAS:
            payload.pop("fixture_revisions", None)
        return payload

    def fixture_revision_for(self, fixture: str) -> str | None:
        return dict(self.fixture_revisions).get(fixture)
