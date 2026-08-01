"""Immutable, data-only receipt chains across existing Company boundaries.

This module creates no shared store and performs no cross-store transaction.
Callers record the receipts at their authoritative boundaries, then provide
those receipts here for deterministic replay and incomplete-chain diagnosis.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, replace
from enum import StrEnum
from typing import Iterable


CROSS_STORE_RECEIPT_CHAIN_SCHEMA = "noruct.cross-store-receipt-chain.v1"


class ReceiptBoundary(StrEnum):
    FIT = "FIT"
    PLAN = "PLAN"
    ASSIGNMENT = "ASSIGNMENT"
    GRAPH = "GRAPH"
    LEASE = "LEASE"
    TERMINAL_SUMMARY = "TERMINAL_SUMMARY"


class ReceiptPhase(StrEnum):
    PREPARED = "PREPARED"
    COMMITTED = "COMMITTED"
    FAILED = "FAILED"
    COMPENSATED = "COMPENSATED"


class ReceiptReplayStatus(StrEnum):
    COMPLETE = "COMPLETE"
    PENDING = "PENDING"
    UNKNOWN = "UNKNOWN"
    FAILED = "FAILED"


_HEX = frozenset("0123456789abcdef")


def _token(value: object, label: str) -> str:
    if type(value) is not str or not value.strip() or value != value.strip():
        raise ValueError(f"{label} must be a non-empty token")
    if any(character.isspace() for character in value):
        raise ValueError(f"{label} must not contain whitespace")
    return value


def _digest(value: object, label: str) -> str:
    value = _token(value, label)
    if len(value) != 64 or any(character not in _HEX for character in value):
        raise ValueError(f"{label} must be a lowercase SHA-256 digest")
    return value


def _content_digest(value: object) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class BoundaryReceipt:
    """One append-only fact from one existing authority boundary."""

    parent_id: str
    boundary: ReceiptBoundary
    source_id: str
    source_digest: str
    phase: ReceiptPhase
    effect_observed: bool = False

    def __post_init__(self) -> None:
        _token(self.parent_id, "parent_id")
        if type(self.boundary) is not ReceiptBoundary:
            raise TypeError("boundary must be a ReceiptBoundary")
        _token(self.source_id, "source_id")
        _digest(self.source_digest, "source_digest")
        if type(self.phase) is not ReceiptPhase:
            raise TypeError("phase must be a ReceiptPhase")
        if type(self.effect_observed) is not bool:
            raise TypeError("effect_observed must be boolean")
        if self.effect_observed and self.phase in {ReceiptPhase.FAILED, ReceiptPhase.COMPENSATED}:
            raise ValueError("a failed or compensated receipt cannot claim an observed effect")

    @property
    def content_digest(self) -> str:
        return _content_digest(self.payload())

    def payload(self) -> dict[str, object]:
        return {
            "schema": CROSS_STORE_RECEIPT_CHAIN_SCHEMA,
            "parent_id": self.parent_id,
            "boundary": self.boundary.value,
            "source_id": self.source_id,
            "source_digest": self.source_digest,
            "phase": self.phase.value,
            "effect_observed": self.effect_observed,
        }


@dataclass(frozen=True, slots=True)
class ReceiptReplay:
    """Content-free replay result; it owns no recovery or compensation action."""

    parent_id: str
    status: ReceiptReplayStatus
    committed_boundaries: tuple[ReceiptBoundary, ...]
    pending_boundaries: tuple[ReceiptBoundary, ...]
    unknown_boundaries: tuple[ReceiptBoundary, ...]
    failed_boundaries: tuple[ReceiptBoundary, ...]


@dataclass(frozen=True, slots=True)
class CrossStoreReceiptChain:
    """A pure append-only chain linking existing source identities by parent id."""

    parent_id: str
    receipts: tuple[BoundaryReceipt, ...] = ()

    def __post_init__(self) -> None:
        _token(self.parent_id, "parent_id")
        if not isinstance(self.receipts, tuple):
            raise ValueError("receipts must be an immutable tuple")
        for receipt in self.receipts:
            if not isinstance(receipt, BoundaryReceipt):
                raise TypeError("receipts must contain BoundaryReceipt values")
            if receipt.parent_id != self.parent_id:
                raise ValueError("receipt parent id does not match chain")
        self._validate_history()

    def append(self, receipt: BoundaryReceipt) -> "CrossStoreReceiptChain":
        """Return a new chain after validating one allowed boundary transition."""

        if not isinstance(receipt, BoundaryReceipt):
            raise TypeError("receipt must be a BoundaryReceipt")
        return replace(self, receipts=(*self.receipts, receipt))

    def replay(self) -> ReceiptReplay:
        """Project exact committed state; incomplete or effect-observed gaps fail closed."""

        latest = self._latest()
        committed = tuple(sorted((boundary for boundary, receipt in latest.items() if receipt.phase is ReceiptPhase.COMMITTED), key=str))
        pending = tuple(sorted((boundary for boundary, receipt in latest.items() if receipt.phase is ReceiptPhase.PREPARED and not receipt.effect_observed), key=str))
        unknown = tuple(sorted((boundary for boundary, receipt in latest.items() if receipt.phase is ReceiptPhase.PREPARED and receipt.effect_observed), key=str))
        failed = tuple(sorted((boundary for boundary, receipt in latest.items() if receipt.phase in {ReceiptPhase.FAILED, ReceiptPhase.COMPENSATED}), key=str))
        missing = tuple(boundary for boundary in ReceiptBoundary if boundary not in latest)
        if failed:
            status = ReceiptReplayStatus.FAILED
        elif unknown:
            status = ReceiptReplayStatus.UNKNOWN
        elif pending or missing:
            status = ReceiptReplayStatus.PENDING
        else:
            status = ReceiptReplayStatus.COMPLETE
        return ReceiptReplay(self.parent_id, status, committed, pending, unknown, failed)

    def _latest(self) -> dict[ReceiptBoundary, BoundaryReceipt]:
        latest: dict[ReceiptBoundary, BoundaryReceipt] = {}
        for receipt in self.receipts:
            latest[receipt.boundary] = receipt
        return latest

    def _validate_history(self) -> None:
        previous: dict[ReceiptBoundary, BoundaryReceipt] = {}
        for receipt in self.receipts:
            prior = previous.get(receipt.boundary)
            if prior is not None:
                if (prior.source_id, prior.source_digest) != (receipt.source_id, receipt.source_digest):
                    raise ValueError("receipt chain source authority conflicts")
                if prior.phase in {ReceiptPhase.COMMITTED, ReceiptPhase.FAILED, ReceiptPhase.COMPENSATED}:
                    raise ValueError("receipt chain boundary is already terminal")
                if receipt.phase not in {ReceiptPhase.COMMITTED, ReceiptPhase.FAILED, ReceiptPhase.COMPENSATED}:
                    raise ValueError("prepared receipt must resolve to a terminal phase")
            elif receipt.phase is not ReceiptPhase.PREPARED:
                raise ValueError("receipt chain boundary must begin PREPARED")
            previous[receipt.boundary] = receipt


def replay_receipt_chain(receipts: Iterable[BoundaryReceipt], *, parent_id: str) -> ReceiptReplay:
    """Build and replay a chain from receipts supplied by their own stores."""

    return CrossStoreReceiptChain(parent_id=parent_id, receipts=tuple(receipts)).replay()
