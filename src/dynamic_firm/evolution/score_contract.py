"""Cross-runtime decimal score contract for immutable Evolution digests."""

from __future__ import annotations

import hashlib
import math
from decimal import Decimal
from typing import Any, Mapping, Sequence

from dynamic_firm.company.models import canonical_json
from dynamic_firm.runtime.models import to_primitive


_SCORE_KEYS = frozenset({"quality_score", "safety_score"})
_SCORE_ERROR_SUFFIX = "must be a finite number from 0 to 1 using non-negative-zero 0.01 steps"


def validate_evolution_score(value: object, name: str) -> float:
    """Return a canonical score limited to exact 0.01 wire increments."""

    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise ValueError(f"{name} {_SCORE_ERROR_SUFFIX}")
    normalized = float(value)
    decimal_score = Decimal(str(value))
    if (
        not math.isfinite(normalized)
        or (normalized == 0.0 and math.copysign(1.0, normalized) < 0)
        or decimal_score < 0
        or decimal_score > 1
        or decimal_score * 100 != (decimal_score * 100).to_integral_value()
    ):
        raise ValueError(f"{name} {_SCORE_ERROR_SUFFIX}")
    return normalized


def _normalize_evolution_scores(value: object, *, path: str = "payload") -> Any:
    if isinstance(value, Mapping):
        normalized: dict[str, Any] = {}
        for raw_key, item in value.items():
            key = str(raw_key)
            item_path = f"{path}.{key}"
            if key in _SCORE_KEYS:
                normalized[key] = validate_evolution_score(item, item_path)
            else:
                normalized[key] = _normalize_evolution_scores(item, path=item_path)
        return normalized
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [
            _normalize_evolution_scores(item, path=f"{path}[{index}]")
            for index, item in enumerate(value)
        ]
    return value


def canonical_evolution_json(value: object) -> str:
    """Render immutable Evolution JSON with score representation parity.

    JSON has only one number type, while Python distinguishes ``1`` and
    ``1.0`` during serialization.  Normalizing every nested Evolution score to
    ``float`` keeps Python digests identical to the Worker wire contract.
    """

    return canonical_json(_normalize_evolution_scores(to_primitive(value)))


def evolution_content_digest(value: object) -> str:
    """Hash the canonical cross-runtime Evolution JSON representation."""

    return hashlib.sha256(canonical_evolution_json(value).encode("utf-8")).hexdigest()
