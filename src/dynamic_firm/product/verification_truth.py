"""Immutable verification truth from retained receipts only."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass


VERIFICATION_TRUTH_SCHEMA = "noruct.verification-truth.v1"
VERIFICATION_STATUSES = frozenset(
    {"PASSED", "FAILED", "PARTIAL", "NOT_RUN", "UNKNOWN"}
)
_MAX_ENTRIES = 5
_EFFECTS = frozenset({"WRITE", "EXECUTE", "EXTERNAL_COMMUNICATION"})
_INDETERMINATE = frozenset({"STARTED", "INDETERMINATE", "RUNNING"})


@dataclass(frozen=True, slots=True)
class VerificationEntry:
    """One named verification fact and opaque links to its retained evidence."""

    name: str
    status: str
    evidence_links: tuple[str, ...]

    def __post_init__(self) -> None:
        if self.status not in VERIFICATION_STATUSES:
            raise ValueError(f"unsupported verification status: {self.status!r}")


def _text(value: object, *, limit: int = 160) -> str:
    if not isinstance(value, str):
        return ""
    return " ".join(value.replace("\x00", "").split())[:limit]


def _items(receipts: object) -> tuple[Mapping[str, object], ...]:
    if not isinstance(receipts, Iterable) or isinstance(receipts, (str, bytes, Mapping)):
        return ()
    return tuple(item for item in receipts if isinstance(item, Mapping))


def _evidence_links(receipt: Mapping[str, object]) -> tuple[str, ...]:
    values: object = receipt.get("evidence_links", ())
    if isinstance(values, str):
        values = (values,)
    elif not isinstance(values, Iterable) or isinstance(values, Mapping):
        values = ()
    links: list[str] = []
    for value in values:
        link = _text(value)
        if link and link not in links:
            links.append(link)
    for key in ("evidence_link", "evidence_id", "receipt_id", "id"):
        link = _text(receipt.get(key))
        if link and link not in links:
            links.append(link)
    return tuple(links)


def _name(receipt: Mapping[str, object], default: str) -> str:
    return _text(receipt.get("name"), limit=128) or default


def _status(receipt: Mapping[str, object]) -> str:
    status = _text(receipt.get("status"), limit=64)
    return status if status in VERIFICATION_STATUSES else "UNKNOWN"


def _entry(receipt: Mapping[str, object], default_name: str) -> VerificationEntry:
    return VerificationEntry(
        name=_name(receipt, default_name),
        status=_status(receipt),
        evidence_links=_evidence_links(receipt),
    )


def _receipt_entries(
    receipts: object,
    *,
    default_name: str,
) -> tuple[VerificationEntry, ...]:
    retained = _items(receipts)
    if not retained:
        return (VerificationEntry(default_name, "NOT_RUN", ()),)
    return tuple(_entry(receipt, default_name) for receipt in retained)


def _effect_entry(receipt: Mapping[str, object]) -> VerificationEntry:
    raw_status = _text(receipt.get("status"), limit=64)
    if raw_status in _INDETERMINATE:
        status = "UNKNOWN"
    elif raw_status == "FAILED":
        status = "FAILED"
    elif raw_status == "NOT_RUN":
        status = "NOT_RUN"
    elif raw_status == "PARTIAL":
        status = "PARTIAL"
    else:
        # A terminal effect receipt proves only the recorded local lifecycle;
        # it cannot prove the external or real-world result.
        status = "PARTIAL" if raw_status in {"PASSED", "SUCCEEDED", "COMPLETED", "TERMINAL"} else "UNKNOWN"
    return VerificationEntry(
        name=_name(receipt, "EXTERNAL_EFFECT_RECEIPTS"),
        status=status,
        evidence_links=_evidence_links(receipt),
    )


def _effect_entries(receipts: object) -> tuple[VerificationEntry, ...]:
    retained = tuple(
        receipt
        for receipt in _items(receipts)
        if receipt.get("effect") in _EFFECTS
    )
    if not retained:
        return (VerificationEntry("EXTERNAL_EFFECT_RECEIPTS", "NOT_RUN", ()),)
    return tuple(_effect_entry(receipt) for receipt in retained)


def project_verification_truth(
    *,
    test_receipts: object = (),
    validator_receipts: object = (),
    review_receipts: object = (),
    external_effect_receipts: object = (),
) -> tuple[VerificationEntry, ...]:
    """Project retained verification receipts without reading Job terminal state.

    A receipt status is copied only when it is one of the fixed truth values.
    Missing receipts become ``NOT_RUN``; malformed or non-fixed receipt status
    becomes ``UNKNOWN``. External-effect terminal receipts remain ``PARTIAL``
    because their local lifecycle is not a real-world outcome proof.
    """

    entries = (
        *_receipt_entries(test_receipts, default_name="TEST_EXECUTION"),
        *_receipt_entries(validator_receipts, default_name="VALIDATOR_EXECUTION"),
        *_receipt_entries(review_receipts, default_name="REVIEW"),
        *_effect_entries(external_effect_receipts),
    )
    return entries[:_MAX_ENTRIES]


def verification_truth(inspection: object) -> tuple[VerificationEntry, ...]:
    """Read only the retained receipt fields of an inspection-like object."""

    return project_verification_truth(
        test_receipts=getattr(inspection, "test_receipts", ()),
        validator_receipts=getattr(inspection, "validator_receipts", getattr(inspection, "validation_receipts", ())),
        review_receipts=getattr(inspection, "review_receipts", ()),
        external_effect_receipts=getattr(inspection, "tool_receipts", ()),
    )
