"""Local Evolution Artifact catalog, staging, and activation lifecycle."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from typing import Any, Mapping

from dynamic_firm.company.models import content_digest

from . import service as _service
from .artifact_bundle import (
    artifact_registry_bundle_signing_payload,
    build_artifact_registry_bundle,
    discover_artifact_registries,
    fetch_artifact_registry_bundle,
    fetch_artifact_registry_signature,
    fetch_discovered_artifact_registry,
    fetch_private_network_artifact_registry,
    read_artifact_registry_bundle,
)
from .signing import verify_openssh_signature
from .artifact_origin import (
    ArtifactOriginKind,
    local_derived_origin,
    user_imported_origin,
    validate_artifact_origin,
)
from .shadow_evaluation import (
    ARTIFACT_SHADOW_PROJECTION_SCHEMA,
    ShadowEvaluationIntegrityError,
)


class ArtifactLifecycleMixin:
    """Artifact catalog operations composed into the public network service.

    The mixin owns local catalog intake, reviewed registry staging, and the
    explicit next-Job activation lifecycle. Shared schema validation stays in
    ``service`` so every public ingress has one fail-closed policy boundary.
    """

    _MAX_TRACK_STABLE_CANDIDATES = 3
    _MAX_SHADOW_EVALUATION_ATTEMPTS = 8
    _TRACK_STABLE_PROMOTION_COOLDOWN = timedelta(hours=24)

    def preview_artifact_file(self, path: Path) -> Mapping[str, Any]:
        return self.preview_artifact_manifest(_service._load_json(path))

    def preview_artifact_manifest(self, manifest: Mapping[str, Any]) -> Mapping[str, Any]:
        artifact = _service.validate_evolution_artifact(manifest)
        return {
            "accepted": True,
            "artifact": artifact,
            "manifest_digest": _service.evolution_content_digest(artifact),
            "runtime_effect": "NONE",
            "automatic_update_default": "PINNED",
        }

    def register_artifact_file(self, path: Path) -> Mapping[str, Any]:
        return self.register_artifact_manifest(_service._load_json(path))

    def register_artifact_manifest(
        self,
        manifest: Mapping[str, Any],
        *,
        ingress: str = "DIRECT_LOCAL_REGISTRATION",
    ) -> Mapping[str, Any]:
        return self.store.register_artifact_version(
            _service.validate_evolution_artifact(manifest),
            origin_kind=ArtifactOriginKind.USER_IMPORTED,
            origin_metadata=user_imported_origin(ingress=ingress),
        )

    def register_local_derived_artifact_manifest(
        self,
        manifest: Mapping[str, Any],
        *,
        base_artifact_id: str,
        base_version: str,
        producer: str,
        evidence_digest: str,
    ) -> Mapping[str, Any]:
        """Catalog a first-party local derivative with immutable base evidence.

        There is intentionally no file-based equivalent: a path being local
        is not evidence that its content was derived by Noruct.
        """

        artifact = _service.validate_evolution_artifact(manifest)
        base_id = _service._safe_id(base_artifact_id, "base_artifact_id")
        base_release = _service._semver(base_version, "base_version")
        base = self.store.get_artifact_version(base_id, base_release)
        if artifact["artifact_id"] != base_id:
            raise ValueError("Local-derived Artifact must retain its base artifact id")
        if _service._semver_key(str(artifact["version"])) <= _service._semver_key(base_release):
            raise ValueError("Local-derived Artifact must use a version newer than its base")
        return self.store.register_artifact_version(
            artifact,
            origin_kind=ArtifactOriginKind.LOCAL_DERIVED,
            origin_metadata=local_derived_origin(
                base_artifact_id=base_id,
                base_version=base_release,
                base_manifest_digest=str(base["manifest_digest"]),
                producer=_service._safe_id(producer, "producer"),
                evidence_digest=evidence_digest,
            ),
        )

    def list_artifacts(
        self, *, artifact_id: str | None = None, kind: str | None = None
    ) -> tuple[Mapping[str, Any], ...]:
        return self.store.list_artifact_versions(artifact_id=artifact_id, kind=kind)

    def build_artifact_registry_bundle(self, registry_id: str) -> Mapping[str, Any]:
        return build_artifact_registry_bundle(self.list_artifacts(), registry_id=registry_id)

    @staticmethod
    def inspect_artifact_registry_bundle(path: Path) -> Mapping[str, Any]:
        return read_artifact_registry_bundle(path)

    @staticmethod
    def artifact_registry_bundle_signing_payload(bundle: Mapping[str, Any]) -> bytes:
        return artifact_registry_bundle_signing_payload(bundle)

    @staticmethod
    def fetch_artifact_registry_bundle(
        url: str, *, allow_insecure_loopback: bool = False, bearer_token: str | None = None
    ) -> Mapping[str, Any]:
        return fetch_artifact_registry_bundle(
            url,
            allow_insecure_loopback=allow_insecure_loopback,
            bearer_token=bearer_token,
        )

    @staticmethod
    def discover_artifact_registries(
        origin: str, *, allow_insecure_loopback: bool = False, bearer_token: str | None = None
    ) -> tuple[Mapping[str, str], ...]:
        return discover_artifact_registries(
            origin,
            allow_insecure_loopback=allow_insecure_loopback,
            bearer_token=bearer_token,
        )

    @staticmethod
    def fetch_discovered_artifact_registry(
        origin: str,
        registry_id: str,
        *,
        allow_insecure_loopback: bool = False,
        bearer_token: str | None = None,
    ) -> tuple[Mapping[str, str], Mapping[str, Any], bytes]:
        """Fetch the exact index-pinned public bundle and signature.

        The caller must still verify the returned detached signature against a
        local trust root before staging.  Keeping that step separate means an
        index publisher never becomes an implicit local authority.
        """

        return fetch_discovered_artifact_registry(
            origin,
            registry_id,
            allow_insecure_loopback=allow_insecure_loopback,
            bearer_token=bearer_token,
        )

    @staticmethod
    def fetch_private_network_artifact_registry(
        origin: str,
        registry_id: str,
        *,
        allow_insecure_loopback: bool = False,
        bearer_token: str,
    ) -> tuple[Mapping[str, str], Mapping[str, Any], bytes]:
        return fetch_private_network_artifact_registry(
            origin,
            registry_id,
            allow_insecure_loopback=allow_insecure_loopback,
            bearer_token=bearer_token,
        )

    @staticmethod
    def fetch_artifact_registry_signature(
        url: str, *, allow_insecure_loopback: bool = False, bearer_token: str | None = None
    ) -> bytes:
        return fetch_artifact_registry_signature(
            url,
            allow_insecure_loopback=allow_insecure_loopback,
            bearer_token=bearer_token,
        )

    @staticmethod
    def validate_candidate_evaluation(value: object) -> Mapping[str, Any]:
        return _service.validate_candidate_evaluation(value)

    @staticmethod
    def verify_artifact_registry_bundle_signature(bundle: Mapping[str, Any], *, signature: Path, allowed_signers: Path, principal: str, ssh_keygen: Path) -> Mapping[str, str]:
        return verify_openssh_signature(artifact_registry_bundle_signing_payload(bundle), signature_path=signature, allowed_signers_path=allowed_signers, principal=principal, command=ssh_keygen)

    @staticmethod
    def verify_artifact_registry_bundle_signature_bytes(bundle: Mapping[str, Any], *, signature: bytes, allowed_signers: Path, principal: str, ssh_keygen: Path) -> Mapping[str, str]:
        from .signing import verify_openssh_signature_bytes
        return verify_openssh_signature_bytes(artifact_registry_bundle_signing_payload(bundle), signature=signature, allowed_signers_path=allowed_signers, principal=principal, command=ssh_keygen)

    def stage_verified_artifact_registry_bundle(
        self,
        bundle: Mapping[str, Any],
        *,
        source_label: str,
        signature: bytes,
        allowed_signers: Path,
        principal: str,
        ssh_keygen: Path,
    ) -> Mapping[str, Any]:
        return self.store.stage_verified_artifact_registry_bundle(
            bundle,
            source_label=source_label,
            signature=signature,
            allowed_signers_path=allowed_signers,
            principal=principal,
            command=ssh_keygen,
        )

    def preview_staged_artifact_registry_compatibility(self, snapshot_id: str) -> Mapping[str, Any]:
        return self.store.preview_staged_artifact_registry_compatibility(snapshot_id)

    def review_staged_artifact_registry_snapshot(self, snapshot_id: str, *, operator_id: str, decision: str, reason: str) -> Mapping[str, Any]:
        return self.store.review_staged_artifact_registry_snapshot(snapshot_id, operator_id=operator_id, decision=decision, reason=reason)

    def list_staged_artifact_registry_snapshots(self) -> tuple[Mapping[str, Any], ...]:
        return self.store.list_staged_artifact_registry_snapshots()

    def import_reviewed_staged_artifact(self, snapshot_id: str, artifact_id: str, version: str) -> Mapping[str, Any]:
        return self.store.import_reviewed_staged_artifact(snapshot_id, _service._safe_id(artifact_id, "artifact_id"), _service._semver(version, "version"))

    def import_tracked_artifacts_from_reviewed_snapshot(
        self, *, snapshot_id: str, scope_key: str, allowed_capabilities: tuple[str, ...]
    ) -> Mapping[str, Any]:
        """Import opted-in releases without activating a remote version.

        Network I/O, signer verification, and operator review have already
        happened before this method. It deliberately never imports an
        untracked artifact. Imported versions retain registry provenance and
        require a separate exact-version stage/install/activate action even
        when a local stable tracker exists.
        """
        scope = _service._safe_id(scope_key, "scope_key")
        snapshot = self.store.get_staged_artifact_registry_snapshot(snapshot_id)
        if snapshot["status"] != "REVIEW_APPROVED_NOT_IMPORTED":
            raise ValueError("Tracked Artifact import requires an approved registry snapshot")
        imported: list[Mapping[str, Any]] = []
        for subscription in self.store.list_artifact_update_subscriptions(scope):
            mode = str(subscription["mode"])
            if mode == "PINNED":
                continue
            artifact_id = str(subscription["artifact_id"])
            kind = str(subscription["kind"])
            allowed_channels = {"STABLE"} if mode == "TRACK_STABLE" else {"STABLE", "EXPERIMENTAL"}
            candidates = [
                entry for entry in snapshot["artifacts"]
                if entry["artifact_id"] == artifact_id
                and entry["kind"] == kind
                and entry["release_channel"] in allowed_channels
            ]
            if not candidates:
                continue
            selected = max(candidates, key=lambda entry: _service._semver_key(str(entry["version"])))
            imported.append(
                self.store.import_reviewed_staged_artifact(
                    snapshot_id, artifact_id, str(selected["version"])
                )
            )
        updates = self.apply_artifact_update_subscriptions(
            scope_key=scope, allowed_capabilities=allowed_capabilities
        )
        return {
            "snapshot_id": snapshot_id,
            "scope_key": scope,
            "imported": tuple(imported),
            "updates": updates,
            "runtime_effect": "NONE_REQUIRES_EXPLICIT_ACTIVATION",
        }

    def stage_artifact(self, artifact_id: str, version: str) -> Mapping[str, Any]:
        artifact = self.store.get_artifact_version(_service._safe_id(artifact_id, "artifact_id"), _service._semver(version, "version"))
        self._assert_not_graph_blueprint_runtime_artifact(artifact)
        return self.store.stage_artifact_version(str(artifact["artifact_id"]), str(artifact["version"]))

    def install_artifact(self, artifact_id: str, version: str) -> Mapping[str, Any]:
        artifact = self.store.get_artifact_version(_service._safe_id(artifact_id, "artifact_id"), _service._semver(version, "version"))
        self._assert_not_graph_blueprint_runtime_artifact(artifact)
        return self.store.install_artifact_version(str(artifact["artifact_id"]), str(artifact["version"]))

    @staticmethod
    def _assert_not_graph_blueprint_runtime_artifact(artifact: Mapping[str, Any]) -> None:
        if artifact.get("kind") == "GRAPH_BLUEPRINT":
            raise ValueError(
                "Community Graph Blueprints must use graph community-import-reviewed and graph community-activate; generic Artifact runtime activation is forbidden"
            )

    @staticmethod
    def _artifact_required_capabilities(artifact: Mapping[str, Any]) -> frozenset[str]:
        manifest = artifact.get("manifest", artifact)
        compatibility = manifest["compatibility"]
        content = manifest["content"]
        return frozenset(
            tuple(compatibility["required_capabilities"])
            + tuple(content.get("required_capabilities", ()))
        )

    def _assert_local_authority(
        self, artifact: Mapping[str, Any], allowed_capabilities: tuple[str, ...]
    ) -> None:
        allowed = frozenset(_service._safe_id(item, "allowed_capability") for item in allowed_capabilities)
        missing = self._artifact_required_capabilities(artifact) - allowed
        if missing:
            raise ValueError(
                "Artifact requires capabilities outside the supplied local authority: "
                + ", ".join(sorted(missing))
            )

    def _assert_local_derivation_origin(self, artifact: Mapping[str, Any]) -> None:
        origin, metadata = validate_artifact_origin(
            str(artifact.get("origin_kind", "")),
            artifact.get("origin_metadata", {}),
        )
        if origin != ArtifactOriginKind.LOCAL_DERIVED.value:
            raise ValueError("Artifact is not a local derivative")
        if metadata["base_artifact_id"] != artifact["artifact_id"]:
            raise ValueError("Local-derived Artifact origin does not retain the base id")
        base = self.store.get_artifact_version(
            str(metadata["base_artifact_id"]), str(metadata["base_version"])
        )
        if base["manifest_digest"] != metadata["base_manifest_digest"]:
            raise ValueError("Local-derived Artifact base digest no longer matches its origin")
        if _service._semver_key(str(artifact["version"])) <= _service._semver_key(
            str(base["version"])
        ):
            raise ValueError("Local-derived Artifact version is not newer than its base")

    @staticmethod
    def _assert_artifact_manifest_integrity(artifact: Mapping[str, Any]) -> None:
        manifest = artifact.get("manifest")
        if not isinstance(manifest, Mapping) or content_digest(manifest) != artifact.get(
            "manifest_digest"
        ):
            raise ValueError("Artifact manifest digest verification failed")

    def activate_artifact(
        self,
        *,
        scope_key: str,
        artifact_id: str,
        version: str,
        allowed_capabilities: tuple[str, ...],
        reason: str = "EXPLICIT_LOCAL_ACTIVATION",
    ) -> Mapping[str, Any]:
        scope = _service._safe_id(scope_key, "scope_key")
        artifact = self.store.get_artifact_version(_service._safe_id(artifact_id, "artifact_id"), _service._semver(version, "version"))
        self._assert_not_graph_blueprint_runtime_artifact(artifact)
        self._assert_local_authority(artifact, allowed_capabilities)
        return self.store.activate_artifact_version(
            scope_key=scope,
            artifact_id=str(artifact["artifact_id"]),
            version=str(artifact["version"]),
            activation_reason=reason,
        )

    def list_active_artifacts(self, scope_key: str) -> tuple[Mapping[str, Any], ...]:
        return self.store.list_active_artifact_activations(_service._safe_id(scope_key, "scope_key"))

    def rollback_artifact(
        self,
        *,
        scope_key: str,
        kind: str | None = None,
        artifact_id: str | None = None,
    ) -> Mapping[str, Any]:
        if kind is None and artifact_id is None:
            raise ValueError("Artifact rollback requires artifact_id or kind")
        if kind is not None and kind not in _service._ARTIFACT_KINDS:
            raise ValueError("Evolution Artifact kind is not permitted")
        return self.store.rollback_artifact_activation(
            scope_key=_service._safe_id(scope_key, "scope_key"),
            kind=kind,
            artifact_id=(
                None if artifact_id is None else _service._safe_id(artifact_id, "artifact_id")
            ),
        )

    def set_artifact_update_subscription(
        self, *, scope_key: str, kind: str, artifact_id: str, mode: str
    ) -> Mapping[str, Any]:
        if kind not in _service._ARTIFACT_KINDS:
            raise ValueError("Evolution Artifact kind is not permitted")
        if mode not in {"PINNED", "TRACK_STABLE", "TRACK_EXPERIMENTAL"}:
            raise ValueError("Artifact update mode must be PINNED, TRACK_STABLE, or TRACK_EXPERIMENTAL")
        versions = self.store.list_artifact_versions(artifact_id=_service._safe_id(artifact_id, "artifact_id"), kind=kind)
        if not versions:
            raise KeyError("An Artifact must exist in the local catalog before it can be subscribed")
        if kind == "GRAPH_BLUEPRINT":
            raise ValueError("Community Graph Blueprints do not support generic Artifact tracking; use explicit graph activation")
        return self.store.set_artifact_update_subscription(
            scope_key=_service._safe_id(scope_key, "scope_key"), kind=kind,
            artifact_id=_service._safe_id(artifact_id, "artifact_id"), mode=mode,
        )

    def list_artifact_update_subscriptions(self, scope_key: str) -> tuple[Mapping[str, Any], ...]:
        return self.store.list_artifact_update_subscriptions(_service._safe_id(scope_key, "scope_key"))

    def record_artifact_shadow_evaluation(
        self,
        *,
        scope_key: str,
        artifact_id: str,
        candidate_version: str,
        fixture_kind: str,
        fixture_id: str,
        fixture_version: str,
        fixture_digest: str,
        baseline_quality: object,
        candidate_quality: object,
        baseline_safety: object,
        candidate_safety: object,
        baseline_cost: object,
        candidate_cost: object,
        cost_ceiling: object,
        terminal_state: str,
        complete: bool,
        attempt_count: int,
        failure_count: int,
        failure_history_digest: str,
    ) -> Mapping[str, Any]:
        """Append provider-free evidence for the currently active exact base."""

        if (
            isinstance(attempt_count, bool)
            or not isinstance(attempt_count, int)
            or not 1 <= attempt_count <= self._MAX_SHADOW_EVALUATION_ATTEMPTS
        ):
            raise ValueError(
                "Shadow evaluation attempt_count exceeds the bounded recursive-improvement policy"
            )

        scope = _service._safe_id(scope_key, "scope_key")
        selected_id = _service._safe_id(artifact_id, "artifact_id")
        active = next(
            (
                item
                for item in self.store.list_active_artifact_activations(scope)
                if item["artifact_id"] == selected_id
            ),
            None,
        )
        if active is None:
            raise ValueError("Artifact shadow evaluation requires an active exact base")
        candidate = self.store.get_artifact_version(
            selected_id, _service._semver(candidate_version, "candidate_version")
        )
        self._assert_artifact_manifest_integrity(active["artifact"])
        self._assert_artifact_manifest_integrity(candidate)
        self._assert_local_derivation_origin(candidate)
        if _service._semver_key(str(candidate["version"])) <= _service._semver_key(
            str(active["version"])
        ):
            raise ValueError("Artifact shadow candidate must be newer than the active base")
        return self.store.record_artifact_shadow_evaluation(
            scope_key=scope,
            base=active["artifact"],
            candidate=candidate,
            fixture_kind=fixture_kind,
            fixture_id=fixture_id,
            fixture_version=fixture_version,
            fixture_digest=fixture_digest,
            baseline_quality=baseline_quality,
            candidate_quality=candidate_quality,
            baseline_safety=baseline_safety,
            candidate_safety=candidate_safety,
            baseline_cost=baseline_cost,
            candidate_cost=candidate_cost,
            cost_ceiling=cost_ceiling,
            terminal_state=terminal_state,
            complete=complete,
            attempt_count=attempt_count,
            failure_count=failure_count,
            failure_history_digest=failure_history_digest,
        )

    def artifact_shadow_evaluation_projection(
        self,
        *,
        scope_key: str | None = None,
        artifact_id: str | None = None,
    ) -> Mapping[str, Any]:
        """Return a content-free, local-only operator view of receipt history."""

        scope = None if scope_key is None else _service._safe_id(scope_key, "scope_key")
        selected_id = (
            None
            if artifact_id is None
            else _service._safe_id(artifact_id, "artifact_id")
        )
        try:
            receipts = self.store.list_artifact_shadow_receipts(
                scope_key=scope, artifact_id=selected_id
            )
        except ShadowEvaluationIntegrityError:
            return {
                "schema": ARTIFACT_SHADOW_PROJECTION_SCHEMA,
                "scope_key": scope,
                "artifact_id": selected_id,
                "network_request_performed": False,
                "integrity_state": "TAMPERED",
                "next_action": "EXPLICIT_REVIEW",
                "receipts": (),
            }
        latest_by_slot = {
            str(receipt["slot_digest"]): int(receipt["sequence"])
            for receipt in receipts
        }
        active_by_subject: dict[tuple[str, str], Mapping[str, Any]] = {}
        for receipt_scope in sorted({str(item["scope_key"]) for item in receipts}):
            for activation in self.store.list_active_artifact_activations(
                receipt_scope
            ):
                active_by_subject[(receipt_scope, str(activation["artifact_id"]))] = (
                    activation
                )

        def active_base_exact(receipt: Mapping[str, Any]) -> bool:
            activation = active_by_subject.get(
                (str(receipt["scope_key"]), str(receipt["artifact_id"]))
            )
            return bool(
                activation is not None
                and activation["version"] == receipt["base_version"]
                and activation["artifact"]["manifest_digest"]
                == receipt["base_manifest_digest"]
                and content_digest(activation["artifact"]["manifest"])
                == activation["artifact"]["manifest_digest"]
            )

        def candidate_manifest_exact(receipt: Mapping[str, Any]) -> bool:
            try:
                candidate = self.store.get_artifact_version(
                    str(receipt["artifact_id"]),
                    str(receipt["candidate_version"]),
                )
            except KeyError:
                return False
            return bool(
                candidate["manifest_digest"]
                == receipt["candidate_manifest_digest"]
                and content_digest(candidate["manifest"])
                == candidate["manifest_digest"]
            )

        def evidence_state(receipt: Mapping[str, Any]) -> str:
            latest = latest_by_slot[str(receipt["slot_digest"])] == receipt["sequence"]
            if not latest:
                return "HISTORICAL"
            if not active_base_exact(receipt):
                return "STALE"
            if not candidate_manifest_exact(receipt):
                return "TAMPERED"
            return "EXACT_PASS" if receipt["result"] == "PASS" else str(
                receipt["result"]
            )

        projection = tuple(
            {
                "sequence": receipt["sequence"],
                "receipt_id": receipt["receipt_id"],
                "slot_digest": receipt["slot_digest"],
                "scope_key": receipt["scope_key"],
                "kind": receipt["kind"],
                "artifact_id": receipt["artifact_id"],
                "base_version": receipt["base_version"],
                "base_manifest_digest": receipt["base_manifest_digest"],
                "base_contract_digest": receipt["base_contract_digest"],
                "candidate_version": receipt["candidate_version"],
                "candidate_manifest_digest": receipt["candidate_manifest_digest"],
                "candidate_contract_digest": receipt["candidate_contract_digest"],
                "fixture_kind": receipt["fixture_kind"],
                "fixture_id": receipt["fixture_id"],
                "fixture_version": receipt["fixture_version"],
                "fixture_digest": receipt["fixture_digest"],
                "baseline_quality": receipt["baseline_quality"],
                "candidate_quality": receipt["candidate_quality"],
                "baseline_safety": receipt["baseline_safety"],
                "candidate_safety": receipt["candidate_safety"],
                "baseline_cost": receipt["baseline_cost"],
                "candidate_cost": receipt["candidate_cost"],
                "cost_ceiling": receipt["cost_ceiling"],
                "terminal_state": receipt["terminal_state"],
                "complete": receipt["complete"],
                "attempt_count": receipt["attempt_count"],
                "failure_count": receipt["failure_count"],
                "failure_history_digest": receipt["failure_history_digest"],
                "result": receipt["result"],
                "receipt_digest": receipt["receipt_digest"],
                "recorded_at": receipt["recorded_at"],
                "latest_for_slot": latest_by_slot[str(receipt["slot_digest"])]
                == receipt["sequence"],
                "active_base_exact": active_base_exact(receipt),
                "candidate_manifest_exact": candidate_manifest_exact(receipt),
                "evidence_state": evidence_state(receipt),
            }
            for receipt in receipts
        )
        return {
            "schema": ARTIFACT_SHADOW_PROJECTION_SCHEMA,
            "scope_key": scope,
            "artifact_id": selected_id,
            "network_request_performed": False,
            "integrity_state": "VERIFIED",
            "next_action": "NONE",
            "receipts": projection,
        }

    @staticmethod
    def _pending_shadow_evaluation(
        *,
        subscription: Mapping[str, Any],
        staged: Mapping[str, Any],
        candidate: Mapping[str, Any],
        shadow_state: str,
        receipt: Mapping[str, Any] | None = None,
    ) -> Mapping[str, Any]:
        result: dict[str, Any] = {
            "subscription_id": subscription["subscription_id"],
            "decision": "STAGED_PENDING_SHADOW_EVALUATION",
            "shadow_state": shadow_state,
            "installation_id": staged["installation_id"],
            "version": candidate["version"],
        }
        if receipt is not None:
            result["receipt_id"] = receipt["receipt_id"]
            result["receipt_digest"] = receipt["receipt_digest"]
        return result

    def apply_artifact_update_subscriptions(
        self, *, scope_key: str, allowed_capabilities: tuple[str, ...]
    ) -> tuple[Mapping[str, Any], ...]:
        """Advance only user-authorized local derived trackers; no network I/O.

        `TRACK_STABLE` is the user's explicit ``always-approve`` signal for a
        locally derived artifact family.  It never mutates an Artifact in
        place, interrupts a pinned Job, or promotes a Network-imported Tool,
        Skill, Plugin, Workflow, or Agent.  Before activation the candidate
        must pass a static shadow compatibility check against the active
        runtime contract and authority envelope.
        """
        scope = _service._safe_id(scope_key, "scope_key")
        active = {
            (str(item["kind"]), str(item["artifact_id"])): item
            for item in self.store.list_active_artifact_activations(scope)
        }
        results: list[Mapping[str, Any]] = []

        def has_external_provenance(version: str) -> bool:
            try:
                self.store.get_network_artifact_provenance(artifact_id, version)
                return True
            except KeyError:
                pass
            try:
                self.store.get_artifact_registry_import_provenance(artifact_id, version)
                return True
            except KeyError:
                return False

        for subscription in self.store.list_artifact_update_subscriptions(scope):
            mode = str(subscription["mode"])
            kind = str(subscription["kind"])
            artifact_id = str(subscription["artifact_id"])
            current = active.get((kind, artifact_id))
            if mode == "PINNED":
                results.append({"subscription_id": subscription["subscription_id"], "decision": "PINNED_NO_UPDATE"})
                continue
            if current is None:
                results.append({"subscription_id": subscription["subscription_id"], "decision": "NO_ACTIVE_MATCHING_ARTIFACT"})
                continue
            versions = sorted(
                self.store.list_artifact_versions(artifact_id=artifact_id, kind=kind),
                key=lambda item: _service._semver_key(str(item["version"])),
            )
            allowed_channels = {"STABLE"} if mode == "TRACK_STABLE" else {"STABLE", "EXPERIMENTAL"}
            candidates = [
                item
                for item in versions
                if item["release_channel"] in allowed_channels
                and item["origin_kind"] == ArtifactOriginKind.LOCAL_DERIVED.value
            ]
            if not candidates:
                newer = [
                    item
                    for item in versions
                    if item["release_channel"] in allowed_channels
                    and _service._semver_key(str(item["version"]))
                    > _service._semver_key(str(current["version"]))
                ]
                results.append({
                    "subscription_id": subscription["subscription_id"],
                    "decision": (
                        "NON_LOCAL_DERIVED_REQUIRES_EXPLICIT_ACTIVATION"
                        if newer else "NO_ELIGIBLE_RELEASE"
                    ),
                    "origin_kinds": tuple(sorted({str(item["origin_kind"]) for item in newer})),
                })
                continue
            latest = candidates[-1]
            if _service._semver_key(str(latest["version"])) <= _service._semver_key(str(current["version"])):
                results.append({"subscription_id": subscription["subscription_id"], "decision": "ALREADY_LATEST", "version": current["version"]})
                continue
            newer_candidates = [
                item for item in candidates
                if _service._semver_key(str(item["version"]))
                > _service._semver_key(str(current["version"]))
            ]
            if len(newer_candidates) > self._MAX_TRACK_STABLE_CANDIDATES:
                results.append(
                    {
                        "subscription_id": subscription["subscription_id"],
                        "decision": "CANDIDATE_REVIEW_BOUND_REACHED",
                        "candidate_count": len(newer_candidates),
                        "candidate_limit": self._MAX_TRACK_STABLE_CANDIDATES,
                        "next_action": "EXPLICIT_EXACT_VERSION_REVIEW",
                    }
                )
                continue
            if self.store.has_recent_tracker_artifact_activation(
                scope_key=scope,
                artifact_id=artifact_id,
                cooldown=self._TRACK_STABLE_PROMOTION_COOLDOWN,
            ):
                results.append(
                    {
                        "subscription_id": subscription["subscription_id"],
                        "decision": "PROMOTION_COOLDOWN_ACTIVE",
                        "cooldown_seconds": int(self._TRACK_STABLE_PROMOTION_COOLDOWN.total_seconds()),
                        "next_action": "WAIT_OR_EXPLICIT_EXACT_VERSION_REVIEW",
                    }
                )
                continue
            try:
                self._assert_local_derivation_origin(latest)
            except (KeyError, ValueError) as exc:
                results.append({
                    "subscription_id": subscription["subscription_id"],
                    "decision": "LOCAL_DERIVATION_PROVENANCE_INVALID",
                    "version": latest["version"],
                    "reason": str(exc),
                })
                continue
            if has_external_provenance(str(latest["version"])):
                results.append({
                    "subscription_id": subscription["subscription_id"],
                    "decision": "ORIGIN_PROVENANCE_CONFLICT_REQUIRES_EXPLICIT_REVIEW",
                    "version": latest["version"],
                    "origin_kind": latest["origin_kind"],
                })
                continue
            staged = self.store.stage_artifact_version(artifact_id, str(latest["version"]))
            if mode == "TRACK_EXPERIMENTAL" and latest["release_channel"] == "EXPERIMENTAL":
                results.append({"subscription_id": subscription["subscription_id"], "decision": "STAGED_EXPERIMENTAL_REQUIRES_CONFIRMATION", "installation_id": staged["installation_id"], "version": latest["version"]})
                continue
            try:
                self._assert_artifact_manifest_integrity(current["artifact"])
                self._assert_artifact_manifest_integrity(latest)
            except ValueError:
                results.append(
                    self._pending_shadow_evaluation(
                        subscription=subscription,
                        staged=staged,
                        candidate=latest,
                        shadow_state="TAMPERED",
                    )
                )
                continue
            current_manifest = _service.validate_evolution_artifact(
                self.store.get_artifact_version(artifact_id, str(current["version"]))["manifest"]
            )
            candidate_manifest = _service.validate_evolution_artifact(latest["manifest"])
            base_capabilities = self._artifact_required_capabilities(current_manifest)
            candidate_capabilities = self._artifact_required_capabilities(
                candidate_manifest
            )
            if candidate_capabilities - base_capabilities:
                results.append(
                    self._pending_shadow_evaluation(
                        subscription=subscription,
                        staged=staged,
                        candidate=latest,
                        shadow_state="PERMISSION_EXPANSION",
                    )
                )
                continue
            if (
                current_manifest["compatibility"]
                != candidate_manifest["compatibility"]
                or base_capabilities != candidate_capabilities
            ):
                results.append(
                    self._pending_shadow_evaluation(
                        subscription=subscription,
                        staged=staged,
                        candidate=latest,
                        shadow_state="CONTRACT_MISMATCH",
                    )
                )
                continue
            try:
                self._assert_local_authority(latest, allowed_capabilities)
            except ValueError:
                results.append(
                    self._pending_shadow_evaluation(
                        subscription=subscription,
                        staged=staged,
                        candidate=latest,
                        shadow_state="LOCAL_AUTHORITY_UNAVAILABLE",
                    )
                )
                continue
            try:
                receipt, stale_history = (
                    self.store.latest_exact_artifact_shadow_receipt(
                        scope_key=scope,
                        base=current["artifact"],
                        candidate=latest,
                    )
                )
            except ShadowEvaluationIntegrityError:
                results.append(
                    self._pending_shadow_evaluation(
                        subscription=subscription,
                        staged=staged,
                        candidate=latest,
                        shadow_state="TAMPERED",
                    )
                )
                continue
            if receipt is None:
                results.append(
                    self._pending_shadow_evaluation(
                        subscription=subscription,
                        staged=staged,
                        candidate=latest,
                        shadow_state=("STALE" if stale_history else "MISSING"),
                    )
                )
                continue
            if receipt["result"] != "PASS":
                results.append(
                    self._pending_shadow_evaluation(
                        subscription=subscription,
                        staged=staged,
                        candidate=latest,
                        shadow_state=str(receipt["result"]),
                        receipt=receipt,
                    )
                )
                continue
            self.store.install_artifact_version(artifact_id, str(latest["version"]))
            activated = self.store.activate_artifact_version(
                scope_key=scope, artifact_id=artifact_id, version=str(latest["version"]),
                activation_reason="TRACK_STABLE_SHADOW_PASS",
            )
            results.append(
                {
                    "subscription_id": subscription["subscription_id"],
                    "decision": "ACTIVATED_NEXT_JOB",
                    "shadow_receipt_id": receipt["receipt_id"],
                    "shadow_receipt_digest": receipt["receipt_digest"],
                    "activation": activated,
                }
            )
        return tuple(results)

    def pin_active_artifacts_for_job(self, *, job_id: str, scope_key: str) -> tuple[Mapping[str, Any], ...]:
        return self.store.pin_active_artifacts_for_job(
            job_id=_service._safe_id(job_id, "job_id"), scope_key=_service._safe_id(scope_key, "scope_key")
        )
