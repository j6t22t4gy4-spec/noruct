"""Provider-free fail-closed intake gate for synthetic snapshot signatures.

The gate is intentionally a local contract, not a cryptographic verifier or a
publication client.  Callers supply a synthetic checker and an explicit,
versioned trust-key policy.  Only a canonical Model Intelligence snapshot that
passes every local monotonicity check may advance the per-publisher cursor.
"""

from __future__ import annotations

import hashlib
import json
import threading
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from enum import StrEnum

from .model_intelligence import ModelIntelligenceSnapshot


SyntheticSnapshotSignatureChecker = Callable[[bytes, str, str], bool]
_MAX_OPAQUE_LENGTH = 256


def _opaque(value: object, *, field_name: str) -> str:
    if not isinstance(value, str) or not value or len(value) > _MAX_OPAQUE_LENGTH or "\x00" in value:
        raise ValueError(f"{field_name} must be a non-empty bounded opaque identifier")
    return value


def _positive_integer(value: object, *, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{field_name} must be a positive integer")
    return value


class TrustKeyState(StrEnum):
    ACTIVE = "ACTIVE"
    RETIRED = "RETIRED"
    REVOKED = "REVOKED"


class IntakeStatus(StrEnum):
    ACCEPTED = "ACCEPTED"
    REJECTED = "REJECTED"


class IntakeRejection(StrEnum):
    UNKNOWN_KEY = "UNKNOWN_KEY"
    KEY_NOT_ACTIVE = "KEY_NOT_ACTIVE"
    KEY_GENERATION_MISMATCH = "KEY_GENERATION_MISMATCH"
    KEY_GENERATION_ROLLBACK = "KEY_GENERATION_ROLLBACK"
    MALFORMED_OR_OVERSIZED_SNAPSHOT = "MALFORMED_OR_OVERSIZED_SNAPSHOT"
    SNAPSHOT_PUBLISHER_MISMATCH = "SNAPSHOT_PUBLISHER_MISMATCH"
    SIGNATURE_REFERENCE_MISMATCH = "SIGNATURE_REFERENCE_MISMATCH"
    SYNTHETIC_SIGNATURE_REJECTED = "SYNTHETIC_SIGNATURE_REJECTED"
    REPLAYED_SEQUENCE = "REPLAYED_SEQUENCE"
    REVISION_DOWNGRADE = "REVISION_DOWNGRADE"
    GENERATED_TIME_ROLLBACK = "GENERATED_TIME_ROLLBACK"
    EXPIRY_TIME_ROLLBACK = "EXPIRY_TIME_ROLLBACK"


@dataclass(frozen=True, slots=True)
class TrustKeyPolicy:
    """An opaque, synthetic trust key bound to exactly one publisher."""

    publisher_identity: str
    key_id: str
    generation: int
    state: TrustKeyState

    def __post_init__(self) -> None:
        object.__setattr__(self, "publisher_identity", _opaque(self.publisher_identity, field_name="publisher_identity"))
        object.__setattr__(self, "key_id", _opaque(self.key_id, field_name="key_id"))
        object.__setattr__(self, "generation", _positive_integer(self.generation, field_name="generation"))
        if not isinstance(self.state, TrustKeyState):
            object.__setattr__(self, "state", TrustKeyState(self.state))


@dataclass(frozen=True, slots=True)
class SnapshotIntakeEnvelope:
    """One untrusted submission; the signature reference never enters receipts."""

    snapshot_canonical_bytes: bytes
    publisher_identity: str
    key_id: str
    key_generation: int
    sequence: int
    revision: int
    synthetic_signature_reference: str

    def __post_init__(self) -> None:
        if not isinstance(self.snapshot_canonical_bytes, bytes):
            raise TypeError("snapshot_canonical_bytes must be bytes")
        object.__setattr__(self, "publisher_identity", _opaque(self.publisher_identity, field_name="publisher_identity"))
        object.__setattr__(self, "key_id", _opaque(self.key_id, field_name="key_id"))
        object.__setattr__(self, "key_generation", _positive_integer(self.key_generation, field_name="key_generation"))
        object.__setattr__(self, "sequence", _positive_integer(self.sequence, field_name="sequence"))
        object.__setattr__(self, "revision", _positive_integer(self.revision, field_name="revision"))
        object.__setattr__(self, "synthetic_signature_reference", _opaque(self.synthetic_signature_reference, field_name="synthetic_signature_reference"))


@dataclass(frozen=True, slots=True)
class SnapshotIntakeReceipt:
    """Content-free outcome: metadata and digests only, never bytes or signatures."""

    status: IntakeStatus
    rejection: IntakeRejection | None
    publisher_identity: str
    key_id: str
    key_generation: int
    sequence: int
    revision: int
    snapshot_digest: str


@dataclass(frozen=True, slots=True)
class AcceptedSnapshotCursor:
    """The sole durable state advanced by accepted submissions."""

    publisher_identity: str
    sequence: int
    revision: int
    generated_at: str
    expires_at: str
    snapshot_digest: str


class SnapshotIntakeVerifier:
    """Fail-closed synthetic verification with per-publisher replay protection."""

    def __init__(
        self,
        *,
        trust_keys: Iterable[TrustKeyPolicy],
        synthetic_signature_checker: SyntheticSnapshotSignatureChecker,
        max_snapshot_bytes: int = 64 * 1024,
    ) -> None:
        if not callable(synthetic_signature_checker):
            raise TypeError("synthetic_signature_checker must be callable")
        if isinstance(max_snapshot_bytes, bool) or not isinstance(max_snapshot_bytes, int) or not 1 <= max_snapshot_bytes <= 1024 * 1024:
            raise ValueError("max_snapshot_bytes must be between 1 and 1048576")
        policies = tuple(trust_keys)
        if not policies or not all(isinstance(item, TrustKeyPolicy) for item in policies):
            raise ValueError("trust_keys must be a non-empty sequence of TrustKeyPolicy values")
        policy_index: dict[tuple[str, str], TrustKeyPolicy] = {}
        generations: dict[str, set[int]] = {}
        for policy in policies:
            identity = (policy.publisher_identity, policy.key_id)
            if identity in policy_index:
                raise ValueError("trust key publisher/key identifiers must be unique")
            publisher_generations = generations.setdefault(policy.publisher_identity, set())
            if policy.generation in publisher_generations:
                raise ValueError("trust key generations must be unique per publisher")
            publisher_generations.add(policy.generation)
            policy_index[identity] = policy
        self._policies = policy_index
        self._current_generation = {publisher: max(values) for publisher, values in generations.items()}
        self._synthetic_signature_checker = synthetic_signature_checker
        self._max_snapshot_bytes = max_snapshot_bytes
        self._accepted: dict[str, AcceptedSnapshotCursor] = {}
        self._lock = threading.RLock()

    @property
    def accepted_cursors(self) -> tuple[AcceptedSnapshotCursor, ...]:
        with self._lock:
            return tuple(self._accepted[publisher] for publisher in sorted(self._accepted))

    def _receipt(
        self,
        envelope: SnapshotIntakeEnvelope,
        *,
        status: IntakeStatus,
        rejection: IntakeRejection | None,
    ) -> SnapshotIntakeReceipt:
        return SnapshotIntakeReceipt(
            status=status,
            rejection=rejection,
            publisher_identity=envelope.publisher_identity,
            key_id=envelope.key_id,
            key_generation=envelope.key_generation,
            sequence=envelope.sequence,
            revision=envelope.revision,
            snapshot_digest=hashlib.sha256(envelope.snapshot_canonical_bytes).hexdigest(),
        )

    def _reject(self, envelope: SnapshotIntakeEnvelope, reason: IntakeRejection) -> SnapshotIntakeReceipt:
        return self._receipt(envelope, status=IntakeStatus.REJECTED, rejection=reason)

    def _canonical_snapshot(self, envelope: SnapshotIntakeEnvelope) -> ModelIntelligenceSnapshot | None:
        raw = envelope.snapshot_canonical_bytes
        if not raw or len(raw) > self._max_snapshot_bytes:
            return None
        try:
            decoded = json.loads(raw.decode("utf-8"))
            snapshot = ModelIntelligenceSnapshot.from_mapping(decoded)
        except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError):
            return None
        return snapshot if snapshot.canonical_bytes() == raw else None

    def _prepare(self, envelope: SnapshotIntakeEnvelope) -> SnapshotIntakeReceipt:
        """Validate one intake without advancing its replay cursor.

        A local catalog composition uses this before it writes a candidate.
        The returned receipt remains content-free and must be supplied back to
        :meth:`commit_prepared` only after that candidate is verified.
        """

        if not isinstance(envelope, SnapshotIntakeEnvelope):
            raise TypeError("envelope must be SnapshotIntakeEnvelope")
        policy = self._policies.get((envelope.publisher_identity, envelope.key_id))
        if policy is None:
            return self._reject(envelope, IntakeRejection.UNKNOWN_KEY)
        if policy.state is not TrustKeyState.ACTIVE:
            return self._reject(envelope, IntakeRejection.KEY_NOT_ACTIVE)
        if envelope.key_generation != policy.generation:
            return self._reject(envelope, IntakeRejection.KEY_GENERATION_MISMATCH)
        if envelope.key_generation != self._current_generation[envelope.publisher_identity]:
            return self._reject(envelope, IntakeRejection.KEY_GENERATION_ROLLBACK)

        snapshot = self._canonical_snapshot(envelope)
        if snapshot is None:
            return self._reject(envelope, IntakeRejection.MALFORMED_OR_OVERSIZED_SNAPSHOT)
        if snapshot.publisher_identity != envelope.publisher_identity:
            return self._reject(envelope, IntakeRejection.SNAPSHOT_PUBLISHER_MISMATCH)
        if snapshot.signature_reference != envelope.synthetic_signature_reference:
            return self._reject(envelope, IntakeRejection.SIGNATURE_REFERENCE_MISMATCH)
        try:
            valid_signature = bool(
                self._synthetic_signature_checker(
                    envelope.snapshot_canonical_bytes,
                    envelope.key_id,
                    envelope.synthetic_signature_reference,
                )
            )
        except Exception:
            valid_signature = False
        if not valid_signature:
            return self._reject(envelope, IntakeRejection.SYNTHETIC_SIGNATURE_REJECTED)

        previous = self._accepted.get(envelope.publisher_identity)
        if previous is not None:
            if envelope.sequence <= previous.sequence:
                return self._reject(envelope, IntakeRejection.REPLAYED_SEQUENCE)
            if envelope.revision <= previous.revision:
                return self._reject(envelope, IntakeRejection.REVISION_DOWNGRADE)
            if snapshot.generated_at <= previous.generated_at:
                return self._reject(envelope, IntakeRejection.GENERATED_TIME_ROLLBACK)
            if snapshot.expires_at <= previous.expires_at:
                return self._reject(envelope, IntakeRejection.EXPIRY_TIME_ROLLBACK)

        return self._receipt(envelope, status=IntakeStatus.ACCEPTED, rejection=None)

    def prepare(self, envelope: SnapshotIntakeEnvelope) -> SnapshotIntakeReceipt:
        """Validate one intake without advancing its replay cursor."""

        if not isinstance(envelope, SnapshotIntakeEnvelope):
            raise TypeError("envelope must be SnapshotIntakeEnvelope")
        with self._lock:
            return self._prepare(envelope)

    def commit_prepared(
        self,
        envelope: SnapshotIntakeEnvelope,
        prepared_receipt: SnapshotIntakeReceipt,
    ) -> SnapshotIntakeReceipt:
        """Advance exactly a still-valid receipt prepared by this verifier.

        Revalidating before the write prevents a caller from committing a
        forged, stale, or mismatched receipt.  A rejection never advances the
        cursor.  This is intentionally local process state rather than a
        cross-store transaction or an activation mechanism.
        """

        if not isinstance(envelope, SnapshotIntakeEnvelope):
            raise TypeError("envelope must be SnapshotIntakeEnvelope")
        if not isinstance(prepared_receipt, SnapshotIntakeReceipt):
            raise TypeError("prepared_receipt must be SnapshotIntakeReceipt")
        with self._lock:
            current = self._prepare(envelope)
            if current != prepared_receipt:
                raise ValueError("prepared receipt does not match current intake validation")
            return self._commit_current(envelope, current)

    def _commit_current(
        self,
        envelope: SnapshotIntakeEnvelope,
        receipt: SnapshotIntakeReceipt,
    ) -> SnapshotIntakeReceipt:
        if receipt.status is IntakeStatus.REJECTED:
            return receipt
        snapshot = self._canonical_snapshot(envelope)
        if snapshot is None:  # Defensive: prepare already rejects this case.
            raise ValueError("accepted intake unexpectedly lacks canonical snapshot")
        self._accepted[envelope.publisher_identity] = AcceptedSnapshotCursor(
            publisher_identity=envelope.publisher_identity,
            sequence=envelope.sequence,
            revision=envelope.revision,
            generated_at=snapshot.generated_at,
            expires_at=snapshot.expires_at,
            snapshot_digest=receipt.snapshot_digest,
        )
        return receipt

    def commit_if_current(
        self,
        envelope: SnapshotIntakeEnvelope,
        *,
        approve: Callable[[SnapshotIntakeReceipt], bool],
        finalize: Callable[[], None] | None = None,
    ) -> SnapshotIntakeReceipt:
        """Serialize a local preflight, accepted outcome, and cursor commit.

        A second validation after ``approve`` prevents a re-entrant or stale
        submission from being finalized as an accepted catalog candidate.
        """

        if not isinstance(envelope, SnapshotIntakeEnvelope):
            raise TypeError("envelope must be SnapshotIntakeEnvelope")
        if not callable(approve):
            raise TypeError("approve must be callable")
        if finalize is not None and not callable(finalize):
            raise TypeError("finalize must be callable when supplied")
        with self._lock:
            prepared = self._prepare(envelope)
            if prepared.status is IntakeStatus.REJECTED or not bool(approve(prepared)):
                return prepared
            current = self._prepare(envelope)
            if current != prepared:
                return current
            if finalize is not None:
                finalize()
            return self._commit_current(envelope, current)

    def verify(self, envelope: SnapshotIntakeEnvelope) -> SnapshotIntakeReceipt:
        """Accept at most one strict, forward-only snapshot per submission."""

        if not isinstance(envelope, SnapshotIntakeEnvelope):
            raise TypeError("envelope must be SnapshotIntakeEnvelope")
        with self._lock:
            prepared = self._prepare(envelope)
            return self._commit_current(envelope, prepared)
