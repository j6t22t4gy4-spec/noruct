"""Operator-facing regression proposals for conservative local evolution."""

from __future__ import annotations

from typing import Any, Mapping

from . import service as _service
from .store_artifact_regression import (
    ARTIFACT_REGRESSION_PROJECTION_SCHEMA,
    ArtifactRegressionIntegrityError,
)


class ArtifactRegressionLifecycleMixin:
    """Expose exact rollback proposals without granting automatic rollback."""

    def report_artifact_regression(
        self,
        *,
        scope_key: str,
        artifact_id: str,
        signal_kind: str,
        evidence_digest: str,
    ) -> Mapping[str, Any]:
        """Persist a content-free signal for the currently active version only."""

        return self.store.record_artifact_regression_signal(
            scope_key=_service._safe_id(scope_key, "scope_key"),
            artifact_id=_service._safe_id(artifact_id, "artifact_id"),
            signal_kind=signal_kind,
            evidence_digest=evidence_digest,
        )

    def artifact_regression_projection(
        self,
        *,
        scope_key: str | None = None,
        artifact_id: str | None = None,
    ) -> Mapping[str, Any]:
        """Return an inert exact rollback proposal for current regressions.

        A signal is evidence, not authority.  The projection never disables a
        running Job or changes activation; it only gives an operator the one
        already-existing, exact rollback action where a prior activation is
        available.
        """

        scope = None if scope_key is None else _service._safe_id(scope_key, "scope_key")
        subject = None if artifact_id is None else _service._safe_id(artifact_id, "artifact_id")
        try:
            signals = self.store.list_artifact_regression_signals(
                scope_key=scope, artifact_id=subject
            )
        except ArtifactRegressionIntegrityError:
            return {
                "schema": ARTIFACT_REGRESSION_PROJECTION_SCHEMA,
                "scope_key": scope,
                "artifact_id": subject,
                "network_request_performed": False,
                "integrity_state": "TAMPERED",
                "next_action": "EXPLICIT_REVIEW",
                "signals": (),
            }
        active = {
            (str(item["scope_key"]), str(item["artifact_id"])): item
            for scope_value in sorted({str(item["scope_key"]) for item in signals})
            for item in self.store.list_active_artifact_activations(scope_value)
        }
        projected: list[Mapping[str, Any]] = []
        has_proposal = False
        for signal in signals:
            activation = self.store.get_artifact_activation(str(signal["activation_id"]))
            current = active.get((str(signal["scope_key"]), str(signal["artifact_id"])))
            current_exact = current is not None and current["activation_id"] == signal["activation_id"]
            previous_id = activation.get("replaced_activation_id")
            if current_exact and previous_id is not None:
                previous = self.store.get_artifact_activation(str(previous_id))
                proposal_state = "ROLLBACK_PROPOSED"
                rollback_command = (
                    "noruct evolution artifact rollback "
                    f"{signal['scope_key']} --artifact-id {signal['artifact_id']} --confirm"
                )
                has_proposal = True
                rollback_target = {
                    "activation_id": previous["activation_id"],
                    "version": previous["version"],
                }
            elif current_exact:
                proposal_state = "NO_PRIOR_ACTIVATION"
                rollback_command = None
                rollback_target = None
            else:
                proposal_state = "HISTORICAL_SIGNAL"
                rollback_command = None
                rollback_target = None
            projected.append(
                {
                    **signal,
                    "active_activation_exact": current_exact,
                    "proposal_state": proposal_state,
                    "rollback_target": rollback_target,
                    "rollback_command": rollback_command,
                }
            )
        return {
            "schema": ARTIFACT_REGRESSION_PROJECTION_SCHEMA,
            "scope_key": scope,
            "artifact_id": subject,
            "network_request_performed": False,
            "integrity_state": "VERIFIED",
            "next_action": "CONFIRM_EXACT_ROLLBACK" if has_proposal else "NONE",
            "signals": tuple(projected),
        }
