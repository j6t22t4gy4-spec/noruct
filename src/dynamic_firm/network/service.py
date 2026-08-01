"""Source-aware client for the Noruct Network distribution plane.

This module intentionally composes the existing immutable Evolution Artifact
catalog.  It adds publisher/source provenance, user-facing update modes and
the trusted fetch-to-install path without introducing a competing Company,
ROSTER, Tool, or credential authority.
"""

from __future__ import annotations

import json
import os
import re
import shutil
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlparse

from dynamic_firm.evolution.artifact_bundle import discover_artifact_registries
from dynamic_firm.evolution.evaluation import evaluate_blueprint_delta_holdout_suite
from dynamic_firm.evolution.service import validate_blueprint, validate_blueprint_delta
from dynamic_firm.evolution.service import EvolutionNetworkService
from dynamic_firm.evolution.signing import allowed_signers_digest
from dynamic_firm.evolution.store import EvolutionStore

from .adapters import evaluation_adapter_from_manifests


NETWORK_PUBLISHER_CLASSES = frozenset({"FIRST_PARTY", "COMMUNITY", "PRIVATE_TEAM"})
# An imported capability is an exact user-owned artifact, not a self-improving
# agent input.  Discovery may propose a newer immutable version, but only a
# separately confirmed exact activation can alter a future Job.
NETWORK_UPDATE_MODES = frozenset({"PINNED", "PROPOSE"})
FIRST_PARTY_NETWORK_SOURCE_ID = "noruct_first_party"
FIRST_PARTY_NETWORK_ORIGIN = "https://noruct-evolution-network.asdj0902.workers.dev"
FIRST_PARTY_NETWORK_SIGNER_PRINCIPAL = "noruct_network_release"
_SAFE_ID = re.compile(r"^[a-z][a-z0-9_-]{1,79}$")
_TOKEN_ENV = re.compile(r"^[A-Z][A-Z0-9_]{1,127}$")


class NoructNetworkService:
    """Operate Network sources and installations through local authority.

    A source is only a signed distribution origin.  It cannot grant a local
    capability, alter an active Job, write credentials, or execute downloaded
    code.  Those effects remain with the Firm Kernel, ActionPolicy and the
    registered local adapters.
    """

    def __init__(self, store: EvolutionStore) -> None:
        self.store = store
        self.evolution = EvolutionNetworkService(store)

    @staticmethod
    def _id(value: object, label: str) -> str:
        if not isinstance(value, str) or not _SAFE_ID.fullmatch(value):
            raise ValueError(f"{label} must be a lower-case identifier")
        return value

    @staticmethod
    def _origin(value: object, *, allow_insecure_loopback: bool) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ValueError("Network source origin is required")
        parsed = urlparse(value.strip())
        loopback = parsed.hostname in {"127.0.0.1", "localhost", "::1"}
        if parsed.scheme != "https" and not (
            allow_insecure_loopback and parsed.scheme == "http" and loopback
        ):
            raise ValueError("Network source origin requires HTTPS")
        if (
            parsed.username
            or parsed.password
            or not parsed.hostname
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("Network source origin must be a bare origin")
        return f"{parsed.scheme}://{parsed.netloc}"

    @staticmethod
    def _paths(allowed_signers: Path, ssh_keygen: Path) -> tuple[Path, Path]:
        allowed = allowed_signers.expanduser().resolve()
        verifier = ssh_keygen.expanduser().resolve()
        if not allowed.is_file():
            raise ValueError("Network source allowed-signers file must exist")
        if not verifier.is_absolute() or not verifier.is_file():
            raise ValueError("Network source ssh-keygen verifier must be an existing absolute executable")
        return allowed, verifier

    @staticmethod
    def _credential_env(value: str | None, publisher_class: str) -> str | None:
        if value is None or not value.strip():
            if publisher_class == "PRIVATE_TEAM":
                raise ValueError("PRIVATE_TEAM Network sources require a credential environment variable")
            return None
        name = value.strip()
        if not _TOKEN_ENV.fullmatch(name):
            raise ValueError("Network credential environment variable name is invalid")
        return name

    def _private_registry_id(self, value: str | None, publisher_class: str) -> str | None:
        if publisher_class != "PRIVATE_TEAM":
            if value is not None and value.strip():
                raise ValueError("Only PRIVATE_TEAM Network sources may declare a private registry id")
            return None
        if value is None or not value.strip():
            raise ValueError("PRIVATE_TEAM Network sources require a registry id")
        return self._id(value.strip(), "Private Network registry id")

    @staticmethod
    def _source_token(source: Mapping[str, Any]) -> str | None:
        name = source.get("credential_env")
        if name is None:
            return None
        if not isinstance(name, str) or not _TOKEN_ENV.fullmatch(name):
            raise ValueError("Network source credential configuration is invalid")
        token = os.environ.get(name, "")
        if not token:
            raise ValueError(f"Network source credential environment variable is not set: {name}")
        if len(token) > 512 or "\r" in token or "\n" in token:
            raise ValueError("Network source credential is invalid")
        return token

    def register_source(
        self,
        *,
        source_id: str,
        publisher_class: str,
        origin: str,
        allowed_signers: Path,
        signer_principal: str,
        ssh_keygen: Path,
        operator_id: str,
        credential_env: str | None = None,
        private_registry_id: str | None = None,
        auto_update_enabled: bool = False,
        allow_insecure_loopback: bool = False,
    ) -> Mapping[str, Any]:
        source = self._id(source_id, "Network source id")
        if publisher_class not in NETWORK_PUBLISHER_CLASSES:
            raise ValueError("Network publisher class is unsupported")
        principal = self._id(signer_principal, "Network signer principal")
        operator = self._id(operator_id, "Network operator id")
        if auto_update_enabled:
            raise ValueError("Automatic Network updates are disabled; activate an exact reviewed version explicitly")
        checked_origin = self._origin(
            origin, allow_insecure_loopback=allow_insecure_loopback
        )
        checked_credential_env = self._credential_env(credential_env, publisher_class)
        checked_private_registry_id = self._private_registry_id(
            private_registry_id, publisher_class
        )
        allowed, verifier = self._paths(allowed_signers, ssh_keygen)
        signer_digest = allowed_signers_digest(allowed)

        active = [
            root
            for root in self.store.list_registry_signer_trust_roots()
            if root["source_label"] == source and root["status"] == "ACTIVE"
        ]
        matching = [
            root
            for root in active
            if root["signer_principal"] == principal
            and root["allowed_signers_digest"] == signer_digest
        ]
        if active and not matching:
            raise ValueError(
                "Network source signer changed; rotate or revoke the existing trust root first"
            )
        trust = matching[0] if matching else self.store.register_registry_signer_trust_root(
            source_label=source,
            signer_principal=principal,
            allowed_signers_digest=signer_digest,
            operator_id=operator,
        )
        registered = self.store.upsert_network_source(
            source_id=source,
            publisher_class=publisher_class,
            origin=checked_origin,
            signer_principal=principal,
            allowed_signers_path=str(allowed),
            ssh_keygen_path=str(verifier),
            credential_env=checked_credential_env,
            private_registry_id=checked_private_registry_id,
            auto_update_enabled=False,
            allow_insecure_loopback=allow_insecure_loopback,
        )
        return {
            "source": registered,
            "trust_root": trust,
            "automatic_update": "DISABLED",
            "credential": "ENVIRONMENT_REFERENCE" if checked_credential_env else "NOT_REQUIRED",
            "runtime_effect": "NONE",
        }

    def list_sources(self) -> tuple[Mapping[str, Any], ...]:
        return self.store.list_network_sources()

    def bootstrap_first_party_source(
        self,
        *,
        allowed_signers: Path,
        operator_id: str,
        ssh_keygen: Path | None = None,
        auto_update_enabled: bool = False,
    ) -> Mapping[str, Any]:
        """Register the official distribution origin without installing anything.

        The bootstrap deliberately has no embedded signer key.  A release
        operator must obtain the published OpenSSH allowed-signers policy by
        an independently reviewed channel and name that local file here.  The
        helper only removes error-prone endpoint/class/source-id typing; it
        cannot discover, stage, or activate a template on its own.
        """

        verifier = ssh_keygen
        if verifier is None:
            resolved = shutil.which("ssh-keygen")
            if not resolved:
                raise ValueError(
                    "OpenSSH ssh-keygen was not found; pass --ssh-keygen with an absolute verifier path"
                )
            verifier = Path(resolved)
        result = self.register_source(
            source_id=FIRST_PARTY_NETWORK_SOURCE_ID,
            publisher_class="FIRST_PARTY",
            origin=FIRST_PARTY_NETWORK_ORIGIN,
            allowed_signers=allowed_signers,
            signer_principal=FIRST_PARTY_NETWORK_SIGNER_PRINCIPAL,
            ssh_keygen=verifier,
            operator_id=operator_id,
            auto_update_enabled=auto_update_enabled,
        )
        return {
            **result,
            "bootstrap": {
                "source_id": FIRST_PARTY_NETWORK_SOURCE_ID,
                "origin": FIRST_PARTY_NETWORK_ORIGIN,
                "signer_principal": FIRST_PARTY_NETWORK_SIGNER_PRINCIPAL,
                "discovery": "EXPLICIT_OPERATOR_ACTION_REQUIRED",
                "installation": "NONE",
                "activation": "NONE",
            },
            "network_effect": "TRUST_ROOT_CONFIGURATION_ONLY",
            "runtime_effect": "NONE",
        }

    def discover(self, source_id: str) -> Mapping[str, Any]:
        source = self.store.get_network_source(self._id(source_id, "Network source id"))
        if source["publisher_class"] == "PRIVATE_TEAM":
            return {
                "source": source,
                "registries": ({
                    "registry_id": source["private_registry_id"],
                    "discovery": "LOCAL_PRIVATE_REGISTRY_REFERENCE_ONLY",
                },),
                "network_effect": "LOCAL_PRIVATE_REGISTRY_REFERENCE_ONLY",
                "runtime_effect": "NONE",
            }
        token = self._source_token(source)
        pointers = discover_artifact_registries(
            str(source["origin"]),
            allow_insecure_loopback=bool(source["allow_insecure_loopback"]),
            bearer_token=token,
        )
        return {
            "source": source,
            "registries": pointers,
            "network_effect": "DISCOVERY_ONLY",
            "runtime_effect": "NONE",
        }

    def search(
        self, query: str = "", *, source_id: str | None = None
    ) -> Mapping[str, Any]:
        """Search locally trusted/cataloged templates without remote execution.

        Remote discovery intentionally only returns immutable pointers.  Search
        becomes content-aware after a registry has been signature-verified and
        staged, preventing untrusted index metadata from becoming a product
        recommendation surface.
        """

        source = None if source_id is None else self._id(source_id, "Network source id")
        needle = query.strip().lower()
        available = self.store.list_network_artifacts(source_id=source)
        staged: list[Mapping[str, Any]] = []
        for snapshot in self.store.list_staged_artifact_registry_snapshots():
            if source is not None and snapshot["source_label"] != source:
                continue
            for artifact in snapshot["artifacts"]:
                staged.append(
                    {
                        "source_id": snapshot["source_label"],
                        "registry_id": snapshot["registry_id"],
                        "snapshot_id": snapshot["snapshot_id"],
                        "snapshot_status": snapshot["status"],
                        **artifact,
                    }
                )

        def matches(item: Mapping[str, Any]) -> bool:
            if not needle:
                return True
            fields = (
                item.get("artifact_id", ""),
                item.get("kind", ""),
                item.get("release_channel", ""),
                item.get("source_id", ""),
                item.get("publisher_class", ""),
            )
            return any(needle in str(value).lower() for value in fields)

        return {
            "query": query,
            "available": tuple(item for item in available if matches(item)),
            "staged": tuple(item for item in staged if matches(item)),
            "network_effect": "LOCAL_CATALOG_SEARCH_ONLY",
            "runtime_effect": "NONE",
        }

    @staticmethod
    def _version_key(value: str) -> tuple[int, int, int]:
        pieces = value.split(".")
        if len(pieces) != 3 or any(not item.isdigit() for item in pieces):
            raise ValueError("Network Artifact version must be semver")
        return tuple(int(item) for item in pieces)  # type: ignore[return-value]

    @staticmethod
    def _manifest(artifact: Mapping[str, Any]) -> Mapping[str, Any]:
        raw = artifact.get("manifest_json")
        if not isinstance(raw, str):
            raise ValueError("Network Artifact manifest is unavailable")
        parsed = json.loads(raw)
        if not isinstance(parsed, dict):
            raise ValueError("Network Artifact manifest is invalid")
        return parsed

    def details(
        self, *, artifact_id: str, source_id: str | None = None
    ) -> Mapping[str, Any]:
        """Return local immutable versions and provenance for one template.

        This is intentionally a local catalog view.  It never contacts the
        source and never treats index metadata as a trusted template detail.
        """

        artifact = self._id(artifact_id, "Network Artifact id")
        source = None if source_id is None else self._id(source_id, "Network source id")
        versions = [
            item for item in self.store.list_network_artifacts(source_id=source)
            if item["artifact_id"] == artifact
        ]
        versions.sort(key=lambda item: self._version_key(str(item["version"])))
        return {
            "artifact_id": artifact,
            "versions": tuple(
                {
                    "artifact_id": item["artifact_id"],
                    "version": item["version"],
                    "kind": item["kind"],
                    "release_channel": item["release_channel"],
                    "manifest_digest": item["manifest_digest"],
                    "provenance": {
                        "source_id": item["source_id"],
                        "publisher_class": item["publisher_class"],
                        "registry_id": item["registry_id"],
                        "snapshot_id": item["snapshot_id"],
                        "provenance_digest": item["provenance_digest"],
                    },
                    "manifest": self._manifest(item),
                    "runtime_effect": self.runtime_effect(str(item["kind"])),
                }
                for item in versions
            ),
            "network_effect": "LOCAL_VERIFIED_DETAIL_ONLY",
            "runtime_effect": "NONE",
        }

    def compare_versions(
        self, *, artifact_id: str, left_version: str, right_version: str
    ) -> Mapping[str, Any]:
        """Diff two installed Network provenance versions without activation."""

        artifact = self._id(artifact_id, "Network Artifact id")
        left = self.store.get_network_artifact_provenance(artifact, left_version)
        right = self.store.get_network_artifact_provenance(artifact, right_version)
        left_artifact = self.store.get_artifact_version(artifact, left_version)
        right_artifact = self.store.get_artifact_version(artifact, right_version)
        left_manifest = self._manifest(left_artifact)
        right_manifest = self._manifest(right_artifact)
        fields = sorted(set(left_manifest) | set(right_manifest))
        changed = tuple(
            {
                "field": field,
                "left": left_manifest.get(field),
                "right": right_manifest.get(field),
            }
            for field in fields
            if left_manifest.get(field) != right_manifest.get(field)
        )
        return {
            "artifact_id": artifact,
            "left": {
                "version": left_version,
                "manifest_digest": left_artifact["manifest_digest"],
                "provenance_digest": left["provenance_digest"],
            },
            "right": {
                "version": right_version,
                "manifest_digest": right_artifact["manifest_digest"],
                "provenance_digest": right["provenance_digest"],
            },
            "changed_fields": changed,
            "network_effect": "LOCAL_MANIFEST_COMPARISON_ONLY",
            "runtime_effect": "NONE",
        }

    def stage_discovered_registry(
        self, *, source_id: str, registry_id: str
    ) -> Mapping[str, Any]:
        source = self.store.get_network_source(self._id(source_id, "Network source id"))
        registry = self._id(registry_id, "Network registry id")
        token = self._source_token(source)
        if source["publisher_class"] == "PRIVATE_TEAM":
            if registry != source["private_registry_id"]:
                raise ValueError("Private Network source can stage only its configured registry id")
            pointer, bundle, signature = self.evolution.fetch_private_network_artifact_registry(
                str(source["origin"]),
                registry,
                allow_insecure_loopback=bool(source["allow_insecure_loopback"]),
                bearer_token=token or "",
            )
        else:
            pointer, bundle, signature = self.evolution.fetch_discovered_artifact_registry(
                str(source["origin"]),
                registry,
                allow_insecure_loopback=bool(source["allow_insecure_loopback"]),
                bearer_token=token,
            )
        snapshot = self.evolution.stage_verified_artifact_registry_bundle(
            bundle,
            source_label=str(source["source_id"]),
            signature=signature,
            allowed_signers=Path(str(source["allowed_signers_path"])),
            principal=str(source["signer_principal"]),
            ssh_keygen=Path(str(source["ssh_keygen_path"])),
        )
        return {
            "source": source,
            "discovered_pointer": pointer,
            "snapshot": snapshot,
            "network_effect": "TRUSTED_STAGED_NOT_INSTALLED",
            "runtime_effect": "NONE",
        }

    def review_snapshot(
        self, *, snapshot_id: str, operator_id: str, decision: str, reason: str
    ) -> Mapping[str, Any]:
        if decision not in {"APPROVE", "REJECT"}:
            raise ValueError("Network snapshot review requires APPROVE or REJECT")
        return self.evolution.review_staged_artifact_registry_snapshot(
            snapshot_id,
            operator_id=self._id(operator_id, "Network operator id"),
            decision=decision,
            reason=reason,
        )

    def install(
        self, *, snapshot_id: str, artifact_id: str, version: str
    ) -> Mapping[str, Any]:
        snapshot = self.store.get_staged_artifact_registry_snapshot(snapshot_id)
        source_id = str(snapshot["source_label"])
        source = self.store.get_network_source(source_id)
        artifact = self.evolution.import_reviewed_staged_artifact(
            snapshot_id, artifact_id, version
        )
        provenance = self.store.record_network_artifact_provenance(
            artifact_id=str(artifact["artifact_id"]),
            version=str(artifact["version"]),
            source_id=source_id,
            registry_id=str(snapshot["registry_id"]),
            snapshot_id=snapshot_id,
        )
        staged = self.evolution.stage_artifact(
            str(artifact["artifact_id"]), str(artifact["version"])
        )
        installed = self.evolution.install_artifact(
            str(artifact["artifact_id"]), str(artifact["version"])
        )
        return {
            "source": source,
            "artifact": artifact,
            "provenance": provenance,
            "installation": installed,
            "staging": staged,
            "network_effect": "INSTALLED_INACTIVE",
            "runtime_effect": "NONE_UNTIL_LOCAL_ACTIVATION",
        }

    def activate(
        self,
        *,
        scope_key: str,
        artifact_id: str,
        version: str,
        allowed_capabilities: tuple[str, ...],
    ) -> Mapping[str, Any]:
        provenance = self.store.get_network_artifact_provenance(artifact_id, version)
        activation = self.evolution.activate_artifact(
            scope_key=scope_key,
            artifact_id=artifact_id,
            version=version,
            allowed_capabilities=allowed_capabilities,
            reason="NORUCT_NETWORK_EXPLICIT_LOCAL_ACTIVATION",
        )
        return {
            "activation": activation,
            "provenance": provenance,
            "network_effect": "NEXT_JOB_PINNED_PROJECTION",
            "runtime_effect": self.runtime_effect(str(activation["artifact"]["kind"])),
        }

    def rollback(
        self, *, scope_key: str, artifact_id: str | None = None, kind: str | None = None
    ) -> Mapping[str, Any]:
        activation = self.evolution.rollback_artifact(
            scope_key=scope_key, artifact_id=artifact_id, kind=kind
        )
        return {
            "activation": activation,
            "network_effect": "PREVIOUS_INSTALLED_VERSION_RESTORED",
            "runtime_effect": self.runtime_effect(str(activation["artifact"]["kind"])),
        }

    def evaluate_registered_benchmark(
        self,
        *,
        scope_key: str,
        benchmark_artifact_id: str,
        evaluator_artifact_id: str,
        blueprint: Mapping[str, Any],
        delta: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        """Run one registered deterministic evaluator through active local pins.

        This does not execute release-supplied code.  The only v1 evaluator is
        the existing public synthetic Blueprint Delta holdout suite, and its
        result remains manual-review-only.  Requiring active local Artifacts
        makes the exact selected versions auditable and keeps evaluation
        distinct from discovery or merely installed catalog content.
        """

        benchmark_id = self._id(benchmark_artifact_id, "Network Benchmark Artifact id")
        evaluator_id = self._id(evaluator_artifact_id, "Network Evaluator Artifact id")
        active = {
            str(item["artifact_id"]): item
            for item in self.evolution.list_active_artifacts(scope_key)
        }
        benchmark_activation = active.get(benchmark_id)
        evaluator_activation = active.get(evaluator_id)
        if benchmark_activation is None or evaluator_activation is None:
            raise ValueError("Registered Network evaluation requires both active local Artifacts")
        if (
            str(benchmark_activation["kind"]) != "BENCHMARK_SUITE"
            or str(evaluator_activation["kind"]) != "EVALUATOR_PROFILE"
        ):
            raise ValueError("Network evaluation requires BENCHMARK_SUITE and EVALUATOR_PROFILE Artifacts")
        # An Evolution Artifact imported through another local path must not
        # gain a Network evaluator surface merely because it has the same
        # manifest shape.  Provenance is already immutable at install time.
        self.store.get_network_artifact_provenance(
            benchmark_id, str(benchmark_activation["version"])
        )
        self.store.get_network_artifact_provenance(
            evaluator_id, str(evaluator_activation["version"])
        )
        benchmark = self.store.get_artifact_version(
            benchmark_id, str(benchmark_activation["version"])
        )["manifest"]
        evaluator = self.store.get_artifact_version(
            evaluator_id, str(evaluator_activation["version"])
        )["manifest"]
        adapter = evaluation_adapter_from_manifests(benchmark, evaluator)
        if adapter is None:
            raise ValueError("The active Network Artifact pair has no registered local evaluator adapter")
        report = evaluate_blueprint_delta_holdout_suite(
            validate_blueprint(blueprint), validate_blueprint_delta(delta)
        )
        return {
            "adapter": {
                "adapter_reference": adapter.adapter_reference,
                "benchmark_artifact_id": adapter.benchmark_artifact_id,
                "benchmark_version": adapter.benchmark_version,
                "evaluator_artifact_id": adapter.evaluator_artifact_id,
                "evaluator_version": adapter.evaluator_version,
                "fixture_ids": adapter.fixture_ids,
            },
            "report": report.to_dict(),
            "network_effect": "EXPLICIT_LOCAL_REGISTERED_EVALUATION",
            "runtime_effect": "MANUAL_REVIEW_ONLY_NO_AUTO_PROMOTION",
        }

    def set_update_mode(
        self,
        *,
        scope_key: str,
        artifact_id: str,
        source_id: str,
        mode: str,
    ) -> Mapping[str, Any]:
        if mode not in NETWORK_UPDATE_MODES:
            raise ValueError("Network update mode is unsupported")
        source = self.store.get_network_source(self._id(source_id, "Network source id"))
        active = next(
            (
                value
                for value in self.evolution.list_active_artifacts(scope_key)
                if value["artifact_id"] == artifact_id
            ),
            None,
        )
        if active is None:
            raise ValueError("A Network Artifact must be active before setting its update mode")
        provenance = self.store.get_network_artifact_provenance(
            artifact_id, str(active["version"])
        )
        if provenance["source_id"] != source["source_id"]:
            raise ValueError("Network update source must match the active Artifact provenance")
        tracker_mode = {
            "PINNED": "PINNED",
            "PROPOSE": "PINNED",
        }[mode]
        subscription = self.evolution.set_artifact_update_subscription(
            scope_key=scope_key,
            kind=str(active["kind"]),
            artifact_id=artifact_id,
            mode=tracker_mode,
        )
        preference = self.store.set_network_update_preference(
            scope_key=scope_key,
            artifact_id=artifact_id,
            source_id=str(source["source_id"]),
            mode=mode,
        )
        return {
            "preference": preference,
            "subscription": subscription,
            "network_effect": "LOCAL_MANUAL_UPDATE_POLICY_RECORDED",
            "runtime_effect": "NEXT_JOB_ONLY",
        }

    def list_updates(self, scope_key: str) -> Mapping[str, Any]:
        preferences = self.store.list_network_update_preferences(scope_key)
        return {
            "scope_key": scope_key,
            "preferences": preferences,
            "subscriptions": self.evolution.list_artifact_update_subscriptions(scope_key),
            "network_effect": "READ_ONLY",
            "runtime_effect": "NONE",
        }

    def sync_first_party_updates(
        self,
        *,
        source_id: str,
        scope_key: str,
        allowed_capabilities: tuple[str, ...],
    ) -> Mapping[str, Any]:
        """Refuse automatic updates; preserve manual capability intake.

        A signed publisher, a local tracker, and a model-generated improvement
        are evidence sources, never activation authority.  The caller must use
        the explicit stage → review → install → activate sequence for one exact
        version.  Keeping this command read-only makes older callers fail safe
        instead of silently changing a working imported tool or skill.
        """

        source = self.store.get_network_source(self._id(source_id, "Network source id"))
        scope = self._id(scope_key, "Network scope key")
        return {
            "source": source,
            "scope_key": scope,
            "snapshots": (),
            "imported": (),
            "updates": (),
            "decision": "AUTOMATIC_NETWORK_UPDATE_DISABLED",
            "network_effect": "EXPLICIT_STAGE_REVIEW_INSTALL_ACTIVATE_REQUIRED",
            "runtime_effect": "NONE",
        }

    @staticmethod
    def runtime_effect(kind: str) -> str:
        """Make the current adapter boundary explicit to every surface."""

        return {
            "SKILL_PACKAGE": "EMPLOYEE_SKILL_SNAPSHOT",
            "AGENT_BLUEPRINT": "FROZEN_ROSTER_SKILL_COMPOSITION",
            "TOOL_PACKAGE": "APPROVED_LOCAL_ADAPTER_OR_MCP_POLICY_ONLY",
            "WORKFLOW_PLAYBOOK": "REGISTERED_COMPILER_PRIOR_OR_DECLARATIVE_ONLY",
            "BENCHMARK_SUITE": "REGISTERED_EXPLICIT_EVALUATOR_OR_DECLARATIVE_ONLY",
            "EVALUATOR_PROFILE": "REGISTERED_EXPLICIT_EVALUATOR_OR_DECLARATIVE_ONLY",
            "MODEL_COMPATIBILITY_PROFILE": "DECLARATIVE_PROVIDER_COMPATIBILITY",
            "RELEASE_ADVISORY": "NOTIFICATION_ONLY",
            "GRAPH_BLUEPRINT": "EXPLICIT_COMMUNITY_GRAPH_LIFECYCLE_ONLY",
        }.get(kind, "DECLARATIVE_ONLY")
