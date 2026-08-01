"""Provider-free, in-memory publication simulator for signed snapshot bytes.

This is deliberately not a server, catalog, or signature implementation.  It
keeps only canonical ``ModelIntelligenceSnapshot`` bytes plus content-free
publication receipts.  In particular, benchmark input, prompts, datasets,
executables, provider state, and detached-signature contents are never stored.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from pathlib import Path

from .model_intelligence import ModelIntelligenceSnapshot


SyntheticDetachedSignatureVerifier = Callable[[bytes, str], bool]


_PERSISTED_STATE_SCHEMA = "noruct.local-snapshot-publication-state.v1"
_MAX_PERSISTED_STATE_BYTES = 16 * 1024 * 1024
_MAX_PERSISTED_ENTRIES = 4096


class PublicationStatus(str, Enum):
    PUBLISHED = "PUBLISHED"
    REJECTED = "REJECTED"
    ROLLED_BACK = "ROLLED_BACK"


def _opaque_reference(value: object, *, field_name: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 256 or "\x00" in value:
        raise ValueError(f"{field_name} must be a non-empty bounded opaque reference")
    return value


def _fixed_timestamp(value: object) -> str:
    if not isinstance(value, str) or not value or len(value) > 40:
        raise ValueError("recorded_at must be a bounded ISO-8601 timestamp")
    candidate = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError as error:
        raise ValueError("recorded_at must be an ISO-8601 timestamp") from error
    if parsed.tzinfo is None:
        raise ValueError("recorded_at must include a timezone")
    return value


def _signature_digest(value: object) -> str:
    if not isinstance(value, str) or not value or len(value) > 4096 or "\x00" in value:
        return "UNAVAILABLE"
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _digest(value: object, *, field_name: str) -> str:
    if not isinstance(value, str) or len(value) != 64:
        raise ValueError(f"{field_name} must be a sha256 digest")
    try:
        int(value, 16)
    except ValueError as error:
        raise ValueError(f"{field_name} must be a sha256 digest") from error
    if value != value.lower():
        raise ValueError(f"{field_name} must be a lowercase sha256 digest")
    return value


def _canonical_state_bytes(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=True, separators=(",", ":"), sort_keys=True).encode("utf-8")


def _strict_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("persisted local state contains duplicate object keys")
        result[key] = value
    return result


def _decode_persisted_json(raw: bytes) -> object:
    if not raw or len(raw) > _MAX_PERSISTED_STATE_BYTES:
        raise ValueError("persisted local state is empty or exceeds its bound")
    try:
        decoded = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_strict_json_object,
            parse_constant=lambda value: (_ for _ in ()).throw(ValueError(f"invalid JSON value {value}")),
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        raise ValueError("persisted local state is not strict JSON") from error
    if _canonical_state_bytes(decoded) != raw:
        raise ValueError("persisted local state is not canonical")
    return decoded


@dataclass(frozen=True, slots=True)
class BenchmarkRunRecord:
    """An opaque local benchmark reference, never its source material."""

    benchmark_run_reference: str
    snapshot_digest: str


@dataclass(frozen=True, slots=True)
class PublishedSnapshotIdentity:
    """The immutable content identity which can be selected or downloaded."""

    snapshot_digest: str


@dataclass(frozen=True, slots=True)
class PublicationManifestEvent:
    sequence: int
    status: PublicationStatus
    benchmark_run_reference: str
    snapshot_digest: str
    signature_digest: str
    recorded_at: str


@dataclass(frozen=True, slots=True)
class PublicationReceipt:
    status: PublicationStatus
    snapshot_digest: str
    signature_digest: str
    benchmark_run_reference: str
    recorded_at: str


class LocalSnapshotPublicationSimulator:
    """Append-only local publication history with explicit, reversible selection."""

    def __init__(
        self,
        *,
        synthetic_verifier: SyntheticDetachedSignatureVerifier,
        max_snapshot_bytes: int = 64 * 1024,
        local_state_path: str | Path | None = None,
    ) -> None:
        if not callable(synthetic_verifier):
            raise TypeError("synthetic_verifier must be callable")
        if not isinstance(max_snapshot_bytes, int) or isinstance(max_snapshot_bytes, bool) or not 1 <= max_snapshot_bytes <= 1024 * 1024:
            raise ValueError("max_snapshot_bytes must be between 1 and 1048576")
        self._synthetic_verifier = synthetic_verifier
        self._max_snapshot_bytes = max_snapshot_bytes
        self._manifest: list[PublicationManifestEvent] = []
        self._benchmark_runs: dict[str, BenchmarkRunRecord] = {}
        self._published: dict[str, PublishedSnapshotIdentity] = {}
        self._canonical_bytes: dict[str, bytes] = {}
        self._active_digest: str | None = None
        self._local_state_path = self._validated_state_path(local_state_path)
        if self._local_state_path is not None and self._local_state_path.exists():
            self._load_persisted_state(self._local_state_path.read_bytes())

    @staticmethod
    def _validated_state_path(value: str | Path | None) -> Path | None:
        if value is None:
            return None
        path = Path(value)
        if path.exists() and not path.is_file():
            raise ValueError("local_state_path must name a regular file")
        if not path.parent.is_dir():
            raise ValueError("local_state_path parent must already exist")
        return path

    @staticmethod
    def _event_mapping(event: PublicationManifestEvent) -> dict[str, object]:
        return {
            "benchmark_run_reference": event.benchmark_run_reference,
            "recorded_at": event.recorded_at,
            "sequence": event.sequence,
            "signature_digest": event.signature_digest,
            "snapshot_digest": event.snapshot_digest,
            "status": event.status.value,
        }

    def _state_mapping(
        self,
        *,
        manifest: list[PublicationManifestEvent] | None = None,
        benchmark_runs: dict[str, BenchmarkRunRecord] | None = None,
        published: dict[str, PublishedSnapshotIdentity] | None = None,
        canonical_bytes: dict[str, bytes] | None = None,
        active_digest: str | None | object = ...,
    ) -> dict[str, object]:
        selected_manifest = self._manifest if manifest is None else manifest
        selected_runs = self._benchmark_runs if benchmark_runs is None else benchmark_runs
        selected_published = self._published if published is None else published
        selected_bytes = self._canonical_bytes if canonical_bytes is None else canonical_bytes
        selected_active = self._active_digest if active_digest is ... else active_digest
        return {
            "active_digest": selected_active,
            "benchmark_runs": [
                {"benchmark_run_reference": item.benchmark_run_reference, "snapshot_digest": item.snapshot_digest}
                for _, item in sorted(selected_runs.items())
            ],
            "manifest": [self._event_mapping(item) for item in selected_manifest],
            "max_snapshot_bytes": self._max_snapshot_bytes,
            "published_digests": sorted(selected_published),
            "schema": _PERSISTED_STATE_SCHEMA,
            "snapshots": [
                {
                    "canonical_bytes_base64": base64.b64encode(item).decode("ascii"),
                    "snapshot_digest": digest,
                }
                for digest, item in sorted(selected_bytes.items())
            ],
        }

    def _persist(
        self,
        *,
        manifest: list[PublicationManifestEvent] | None = None,
        benchmark_runs: dict[str, BenchmarkRunRecord] | None = None,
        published: dict[str, PublishedSnapshotIdentity] | None = None,
        canonical_bytes: dict[str, bytes] | None = None,
        active_digest: str | None | object = ...,
    ) -> None:
        if self._local_state_path is None:
            return
        encoded = _canonical_state_bytes(
            self._state_mapping(
                manifest=manifest,
                benchmark_runs=benchmark_runs,
                published=published,
                canonical_bytes=canonical_bytes,
                active_digest=active_digest,
            )
        )
        if len(encoded) > _MAX_PERSISTED_STATE_BYTES:
            raise ValueError("local state exceeds its persistence bound")
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{self._local_state_path.name}.", suffix=".tmp", dir=self._local_state_path.parent
        )
        try:
            with os.fdopen(descriptor, "wb") as temporary:
                temporary.write(encoded)
                temporary.flush()
                os.fsync(temporary.fileno())
            os.replace(temporary_name, self._local_state_path)
            directory_fd = os.open(self._local_state_path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            if os.path.exists(temporary_name):
                os.unlink(temporary_name)

    def _load_persisted_state(self, raw: bytes) -> None:
        value = _decode_persisted_json(raw)
        if not isinstance(value, dict) or set(value) != {
            "active_digest", "benchmark_runs", "manifest", "max_snapshot_bytes", "published_digests", "schema", "snapshots"
        }:
            raise ValueError("persisted local state has unknown or missing fields")
        if (
            value["schema"] != _PERSISTED_STATE_SCHEMA
            or isinstance(value["max_snapshot_bytes"], bool)
            or not isinstance(value["max_snapshot_bytes"], int)
            or value["max_snapshot_bytes"] != self._max_snapshot_bytes
        ):
            raise ValueError("persisted local state schema or bounds do not match configuration")
        manifest_value = value["manifest"]
        runs_value = value["benchmark_runs"]
        published_value = value["published_digests"]
        snapshots_value = value["snapshots"]
        if not all(isinstance(item, list) for item in (manifest_value, runs_value, published_value, snapshots_value)):
            raise ValueError("persisted local state collections must be lists")
        if any(len(item) > _MAX_PERSISTED_ENTRIES for item in (manifest_value, runs_value, published_value, snapshots_value)):
            raise ValueError("persisted local state exceeds entry bounds")

        manifest: list[PublicationManifestEvent] = []
        for sequence, item in enumerate(manifest_value, start=1):
            if not isinstance(item, dict) or set(item) != {
                "benchmark_run_reference", "recorded_at", "sequence", "signature_digest", "snapshot_digest", "status"
            }:
                raise ValueError("persisted manifest event has unknown or missing fields")
            if isinstance(item["sequence"], bool) or not isinstance(item["sequence"], int) or item["sequence"] != sequence:
                raise ValueError("persisted manifest sequence is not contiguous")
            try:
                status = PublicationStatus(item["status"])
            except (TypeError, ValueError) as error:
                raise ValueError("persisted manifest has an unknown status") from error
            signature_digest = item["signature_digest"]
            if signature_digest != "UNAVAILABLE":
                _digest(signature_digest, field_name="signature_digest")
            manifest.append(PublicationManifestEvent(
                sequence=sequence,
                status=status,
                benchmark_run_reference=_opaque_reference(item["benchmark_run_reference"], field_name="benchmark_run_reference"),
                snapshot_digest=_digest(item["snapshot_digest"], field_name="snapshot_digest"),
                signature_digest=signature_digest,
                recorded_at=_fixed_timestamp(item["recorded_at"]),
            ))

        canonical_bytes: dict[str, bytes] = {}
        for item in snapshots_value:
            if not isinstance(item, dict) or set(item) != {"canonical_bytes_base64", "snapshot_digest"}:
                raise ValueError("persisted snapshot has unknown or missing fields")
            digest = _digest(item["snapshot_digest"], field_name="snapshot_digest")
            encoded = item["canonical_bytes_base64"]
            if not isinstance(encoded, str) or len(encoded) > self._max_snapshot_bytes * 2:
                raise ValueError("persisted snapshot bytes are invalid or exceed bounds")
            try:
                content = base64.b64decode(encoded.encode("ascii"), validate=True)
                decoded = json.loads(content.decode("utf-8"))
                snapshot = ModelIntelligenceSnapshot.from_mapping(decoded)
            except (UnicodeDecodeError, ValueError, json.JSONDecodeError) as error:
                raise ValueError("persisted snapshot bytes are invalid") from error
            if (not content or len(content) > self._max_snapshot_bytes or snapshot.canonical_bytes() != content
                    or hashlib.sha256(content).hexdigest() != digest or digest in canonical_bytes):
                raise ValueError("persisted snapshot bytes fail canonical identity validation")
            canonical_bytes[digest] = content

        published: dict[str, PublishedSnapshotIdentity] = {}
        for digest in published_value:
            digest = _digest(digest, field_name="published_digest")
            if digest in published:
                raise ValueError("persisted published identities are duplicated")
            if digest not in canonical_bytes:
                raise ValueError("published identity has no canonical snapshot bytes")
            published[digest] = PublishedSnapshotIdentity(digest)
        if set(canonical_bytes) != set(published):
            raise ValueError("persisted snapshots and identities do not match")

        benchmark_runs: dict[str, BenchmarkRunRecord] = {}
        for item in runs_value:
            if not isinstance(item, dict) or set(item) != {"benchmark_run_reference", "snapshot_digest"}:
                raise ValueError("persisted benchmark run has unknown or missing fields")
            reference = _opaque_reference(item["benchmark_run_reference"], field_name="benchmark_run_reference")
            digest = _digest(item["snapshot_digest"], field_name="snapshot_digest")
            if reference in benchmark_runs or digest not in published:
                raise ValueError("persisted benchmark run is invalid")
            benchmark_runs[reference] = BenchmarkRunRecord(reference, digest)

        published_events = [event for event in manifest if event.status is PublicationStatus.PUBLISHED]
        if {event.snapshot_digest for event in published_events} != set(published):
            raise ValueError("persisted published identities do not match manifest")
        if {event.benchmark_run_reference for event in published_events} != set(benchmark_runs):
            raise ValueError("persisted benchmark records do not match manifest")
        for event in published_events:
            if benchmark_runs[event.benchmark_run_reference].snapshot_digest != event.snapshot_digest:
                raise ValueError("persisted benchmark record conflicts with manifest")
        expected_active: str | None = None
        for event in manifest:
            if event.status in {PublicationStatus.PUBLISHED, PublicationStatus.ROLLED_BACK}:
                if event.snapshot_digest not in published:
                    raise ValueError("persisted active selection targets an unpublished identity")
                expected_active = event.snapshot_digest
            if event.status is PublicationStatus.ROLLED_BACK and event.benchmark_run_reference != f"published:{event.snapshot_digest}":
                raise ValueError("persisted rollback reference is invalid")
        active_digest = value["active_digest"]
        if active_digest is not None:
            active_digest = _digest(active_digest, field_name="active_digest")
        if active_digest != expected_active:
            raise ValueError("persisted active identity does not match manifest")
        self._manifest = manifest
        self._benchmark_runs = benchmark_runs
        self._published = published
        self._canonical_bytes = canonical_bytes
        self._active_digest = active_digest

    @property
    def active_digest(self) -> str | None:
        return self._active_digest

    @property
    def manifest(self) -> tuple[PublicationManifestEvent, ...]:
        return tuple(self._manifest)

    @property
    def benchmark_runs(self) -> tuple[BenchmarkRunRecord, ...]:
        return tuple(self._benchmark_runs[key] for key in sorted(self._benchmark_runs))

    @property
    def published_identities(self) -> tuple[PublishedSnapshotIdentity, ...]:
        return tuple(self._published[key] for key in sorted(self._published))

    def _next_event(
        self,
        *,
        status: PublicationStatus,
        benchmark_run_reference: str,
        snapshot_digest: str,
        signature_digest: str,
        recorded_at: str,
    ) -> PublicationManifestEvent:
        return PublicationManifestEvent(
            sequence=len(self._manifest) + 1,
            status=status,
            benchmark_run_reference=benchmark_run_reference,
            snapshot_digest=snapshot_digest,
            signature_digest=signature_digest,
            recorded_at=recorded_at,
        )

    def publish(
        self,
        *,
        benchmark_run_reference: str,
        snapshot_canonical_bytes: bytes,
        detached_signature: str,
        recorded_at: str,
    ) -> PublicationReceipt:
        """Publish exact signed canonical bytes, recording all acceptance failures."""

        reference = _opaque_reference(benchmark_run_reference, field_name="benchmark_run_reference")
        timestamp = _fixed_timestamp(recorded_at)
        signature_digest = _signature_digest(detached_signature)
        raw_bytes = snapshot_canonical_bytes if isinstance(snapshot_canonical_bytes, bytes) else b""
        digest = hashlib.sha256(raw_bytes).hexdigest()

        snapshot: ModelIntelligenceSnapshot | None = None
        valid = bool(raw_bytes) and len(raw_bytes) <= self._max_snapshot_bytes and signature_digest != "UNAVAILABLE"
        if valid:
            try:
                decoded = json.loads(raw_bytes.decode("utf-8"))
                snapshot = ModelIntelligenceSnapshot.from_mapping(decoded)
                valid = snapshot.canonical_bytes() == raw_bytes and snapshot.content_digest == digest
            except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError):
                valid = False
        if valid:
            try:
                valid = bool(self._synthetic_verifier(raw_bytes, detached_signature))
            except Exception:
                valid = False

        existing_run = self._benchmark_runs.get(reference)
        if valid and existing_run is not None and existing_run.snapshot_digest != digest:
            valid = False

        status = PublicationStatus.PUBLISHED if valid and snapshot is not None else PublicationStatus.REJECTED
        event = self._next_event(
            status=status,
            benchmark_run_reference=reference,
            snapshot_digest=digest,
            signature_digest=signature_digest,
            recorded_at=timestamp,
        )
        if status is PublicationStatus.REJECTED:
            self._persist(manifest=[*self._manifest, event])
            self._manifest.append(event)
            return PublicationReceipt(status, digest, signature_digest, reference, timestamp)

        identity = PublishedSnapshotIdentity(digest)
        prospective_runs = dict(self._benchmark_runs)
        prospective_published = dict(self._published)
        prospective_bytes = dict(self._canonical_bytes)
        prospective_runs.setdefault(reference, BenchmarkRunRecord(reference, digest))
        prospective_published.setdefault(digest, identity)
        prospective_bytes.setdefault(digest, raw_bytes)
        self._persist(
            manifest=[*self._manifest, event],
            benchmark_runs=prospective_runs,
            published=prospective_published,
            canonical_bytes=prospective_bytes,
            active_digest=digest,
        )
        self._manifest.append(event)
        self._benchmark_runs = prospective_runs
        self._published = prospective_published
        self._canonical_bytes = prospective_bytes
        self._active_digest = digest
        return PublicationReceipt(status, digest, signature_digest, reference, timestamp)

    def rollback(self, snapshot_digest: str, *, recorded_at: str) -> PublicationReceipt:
        """Select a prior published identity; history and bytes are never rewritten."""

        digest = _opaque_reference(snapshot_digest, field_name="snapshot_digest")
        timestamp = _fixed_timestamp(recorded_at)
        identity = self._published.get(digest)
        if identity is None:
            raise KeyError("rollback target must be an already published snapshot digest")
        reference = f"published:{digest}"
        event = self._next_event(
            status=PublicationStatus.ROLLED_BACK,
            benchmark_run_reference=reference,
            snapshot_digest=digest,
            signature_digest="UNAVAILABLE",
            recorded_at=timestamp,
        )
        self._persist(manifest=[*self._manifest, event], active_digest=digest)
        self._manifest.append(event)
        self._active_digest = digest
        return PublicationReceipt(PublicationStatus.ROLLED_BACK, digest, "UNAVAILABLE", reference, timestamp)

    def download(self, snapshot_digest: str) -> bytes:
        """Return exact bounded canonical bytes for a known published identity only."""

        digest = _opaque_reference(snapshot_digest, field_name="snapshot_digest")
        if digest not in self._published:
            raise KeyError("unknown or unpublished snapshot digest")
        canonical_bytes = self._canonical_bytes[digest]
        if len(canonical_bytes) > self._max_snapshot_bytes:
            raise ValueError("published snapshot exceeds download bound")
        return canonical_bytes
