"""Canonical hashing primitives shared by RunStore ledger lifecycle mixins."""

from __future__ import annotations

import hashlib


def job_chain_digest(previous_hash: str, event_type: str, payload_hash: str) -> str:
    """Return one deterministic ACTIVE JOB audit-chain link."""

    return hashlib.sha256(
        f"noruct.active-job-ledger.v1|{previous_hash}|{event_type}|{payload_hash}".encode(
            "utf-8"
        )
    ).hexdigest()
