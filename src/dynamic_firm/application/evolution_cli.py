"""Execution handler for the opt-in Evolution command family."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Mapping, TextIO

from dynamic_firm.evolution import (
    EvolutionNetworkService,
    EvolutionStore,
    UnsupportedEvolutionStoreSchemaError,
)
from dynamic_firm.runtime.models import to_primitive
from dynamic_firm.runtime.store import RunStore


def run_evolution_command(
    args: argparse.Namespace,
    settings: dict,
    output: TextIO,
    *,
    evolution_state_path,
    runtime_state_path,
    human_summary,
    exit_ok: int,
) -> int:
    state_path = evolution_state_path(args, settings)
    command = args.evolution_command
    if command == "evaluate":
        payload: object = EvolutionNetworkService.evaluate_admission_files(
            args.blueprint, tuple(args.capsules)
        )
    elif command == "delta":
        if args.evolution_delta_command == "preview":
            payload = EvolutionNetworkService.preview_delta_file(args.delta)
        elif args.evolution_delta_command == "holdout":
            payload = EvolutionNetworkService.evaluate_delta_holdout_files(
                args.blueprint, args.delta
            )
        else:
            payload = EvolutionNetworkService.evaluate_delta_holdout_suite_files(
                args.blueprint, args.delta
            )
    elif command == "release-candidate":
        with EvolutionStore(state_path) as store:
            service = EvolutionNetworkService(store)
            if args.evolution_release_candidate_command == "create":
                if not args.confirm:
                    raise ValueError("Release candidate creation requires --confirm")
                payload = service.create_release_candidate_files(
                    args.blueprint, args.delta, tuple(args.capsule_id)
                )
            elif args.evolution_release_candidate_command == "list":
                payload = {"release_candidates": service.list_release_candidates()}
            elif args.evolution_release_candidate_command == "signing-payload":
                payload = {"payload": service.release_candidate_signing_payload(args.candidate_id).decode("utf-8")}
            elif args.evolution_release_candidate_command == "verify-signature":
                if not args.confirm:
                    raise ValueError("Signature verification receipt requires --confirm")
                payload = service.verify_release_candidate_signature(args.candidate_id, signature=args.signature, allowed_signers=args.allowed_signers, principal=args.principal, ssh_keygen=args.ssh_keygen)
            elif args.evolution_release_candidate_command == "publish-local":
                if not args.confirm:
                    raise ValueError("Local registry publication requires --confirm")
                payload = service.publish_release_candidate(args.candidate_id)
            elif args.evolution_release_candidate_command == "registry-list":
                payload = {"registry_releases": service.list_registry_releases()}
            else:
                if not args.confirm:
                    raise ValueError("Release candidate review requires --confirm")
                payload = service.review_release_candidate(
                    args.candidate_id,
                    operator_id=args.operator_id,
                    decision=args.evolution_review_decision,
                    reason=args.reason,
                )
    elif command == "artifact":
        with EvolutionStore(state_path) as store:
            service = EvolutionNetworkService(store)
            action = args.evolution_artifact_command
            if action == "preview":
                payload = service.preview_artifact_file(args.source)
            elif action == "register":
                if not args.confirm:
                    raise ValueError("Artifact catalog registration requires --confirm")
                payload = service.register_artifact_file(args.source)
            elif action == "list":
                payload = {"artifacts": service.list_artifacts(artifact_id=args.artifact_id, kind=args.kind)}
            elif action == "stage":
                if not args.confirm:
                    raise ValueError("Artifact staging requires --confirm")
                payload = service.stage_artifact(args.artifact_id, args.version)
            elif action == "install":
                if not args.confirm:
                    raise ValueError("Artifact installation requires --confirm")
                payload = service.install_artifact(args.artifact_id, args.version)
            elif action == "activate":
                if not args.confirm:
                    raise ValueError("Artifact activation requires --confirm")
                payload = service.activate_artifact(
                    scope_key=args.scope_key, artifact_id=args.artifact_id, version=args.version,
                    allowed_capabilities=tuple(args.allowed_capability),
                )
            elif action == "active":
                payload = {"active_artifacts": service.list_active_artifacts(args.scope_key)}
            elif action == "shadow-receipts":
                payload = service.artifact_shadow_evaluation_projection(
                    scope_key=args.scope_key,
                    artifact_id=args.artifact_id,
                )
            elif action == "regressions":
                payload = service.artifact_regression_projection(
                    scope_key=args.scope_key,
                    artifact_id=args.artifact_id,
                )
            elif action == "report-regression":
                if not args.confirm:
                    raise ValueError("Artifact regression report requires --confirm")
                payload = service.report_artifact_regression(
                    scope_key=args.scope_key,
                    artifact_id=args.artifact_id,
                    signal_kind=args.signal_kind,
                    evidence_digest=args.evidence_digest,
                )
            elif action == "rollback":
                if not args.confirm:
                    raise ValueError("Artifact rollback requires --confirm")
                payload = service.rollback_artifact(
                    scope_key=args.scope_key,
                    kind=args.kind,
                    artifact_id=args.artifact_id,
                )
            elif action == "subscribe":
                if not args.confirm:
                    raise ValueError("Artifact update subscription requires --confirm")
                payload = service.set_artifact_update_subscription(
                    scope_key=args.scope_key, kind=args.kind, artifact_id=args.artifact_id, mode=args.mode,
                )
            elif action == "update":
                if not args.confirm:
                    raise ValueError("Artifact update application requires --confirm")
                payload = {"updates": service.apply_artifact_update_subscriptions(
                    scope_key=args.scope_key, allowed_capabilities=tuple(args.allowed_capability),
                )}
            else:
                if not args.confirm:
                    raise ValueError("Job Artifact pinning requires --confirm")
                payload = {"job_id": args.job_id, "pins": service.pin_active_artifacts_for_job(
                    job_id=args.job_id, scope_key=args.scope_key,
                )}
    elif command == "artifact-registry":
        action = args.evolution_artifact_registry_command
        if action == "build":
            destination = args.destination.expanduser().resolve()
            if destination.exists() and not args.force:
                raise ValueError("Artifact registry bundle destination already exists; pass --force to replace it")
            with EvolutionStore(state_path) as store:
                bundle = EvolutionNetworkService(store).build_artifact_registry_bundle(args.registry_id)
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text(json.dumps(bundle, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
            payload = {"destination": str(destination), "bundle": bundle}
        elif action in {"inspect", "signing-payload"}:
            bundle = EvolutionNetworkService.inspect_artifact_registry_bundle(args.source)
            if action == "inspect":
                payload = bundle
            else:
                payload = {"payload": EvolutionNetworkService.artifact_registry_bundle_signing_payload(bundle).decode("utf-8"), "bundle_digest": bundle["bundle_digest"], "registry_id": bundle["registry_id"]}
        elif action == "discover":
            payload = {
                "registries": EvolutionNetworkService.discover_artifact_registries(
                    args.origin,
                    allow_insecure_loopback=args.allow_insecure_loopback,
                )
            }
        elif action == "fetch":
            payload = EvolutionNetworkService.fetch_artifact_registry_bundle(args.url, allow_insecure_loopback=args.allow_insecure_loopback)
        elif action == "fetch-stage-discovered":
            if not args.confirm:
                raise ValueError(
                    "Discovered Artifact registry fetch and staging requires --confirm"
                )
            pointer, bundle, signature_bytes = (
                EvolutionNetworkService.fetch_discovered_artifact_registry(
                    args.origin,
                    args.registry_id,
                    allow_insecure_loopback=args.allow_insecure_loopback,
                )
            )
            with EvolutionStore(state_path) as store:
                staged = EvolutionNetworkService(store).stage_verified_artifact_registry_bundle(
                    bundle,
                    source_label=args.source_label,
                    signature=signature_bytes,
                    allowed_signers=args.allowed_signers,
                    principal=args.principal,
                    ssh_keygen=args.ssh_keygen,
                )
            payload = {**staged, "discovered_pointer": pointer}
        elif action == "fetch-stage":
            if not args.confirm: raise ValueError("Artifact registry fetch and staging requires --confirm")
            bundle = EvolutionNetworkService.fetch_artifact_registry_bundle(args.url, allow_insecure_loopback=args.allow_insecure_loopback)
            if args.signature_url:
                signature_bytes = EvolutionNetworkService.fetch_artifact_registry_signature(args.signature_url, allow_insecure_loopback=args.allow_insecure_loopback)
            else:
                if not args.signature.is_file() or not 0 < args.signature.stat().st_size <= 32 * 1024:
                    raise ValueError("Artifact registry signature must be an existing detached file up to 32 KiB")
                signature_bytes = args.signature.read_bytes()
            with EvolutionStore(state_path) as store:
                payload = EvolutionNetworkService(store).stage_verified_artifact_registry_bundle(
                    bundle,
                    source_label=args.source_label,
                    signature=signature_bytes,
                    allowed_signers=args.allowed_signers,
                    principal=args.principal,
                    ssh_keygen=args.ssh_keygen,
                )
        elif action == "staged-list":
            with EvolutionStore(state_path) as store:
                payload = {"snapshots": EvolutionNetworkService(store).list_staged_artifact_registry_snapshots()}
        elif action == "stage":
            if not args.confirm: raise ValueError("Artifact registry staging requires --confirm")
            bundle = EvolutionNetworkService.inspect_artifact_registry_bundle(args.source)
            if not args.signature.is_file() or not 0 < args.signature.stat().st_size <= 32 * 1024:
                raise ValueError("Artifact registry signature must be an existing detached file up to 32 KiB")
            signature_bytes = args.signature.read_bytes()
            with EvolutionStore(state_path) as store:
                payload = EvolutionNetworkService(store).stage_verified_artifact_registry_bundle(
                    bundle,
                    source_label=args.source_label,
                    signature=signature_bytes,
                    allowed_signers=args.allowed_signers,
                    principal=args.principal,
                    ssh_keygen=args.ssh_keygen,
                )
        elif action == "review":
            with EvolutionStore(state_path) as store:
                service = EvolutionNetworkService(store)
                if args.evolution_artifact_registry_review_command == "preview": payload = service.preview_staged_artifact_registry_compatibility(args.snapshot_id)
                else:
                    if not args.confirm: raise ValueError("Artifact registry review requires --confirm")
                    payload = service.review_staged_artifact_registry_snapshot(args.snapshot_id, operator_id=args.operator_id, decision=args.evolution_artifact_registry_review_command.upper(), reason=args.reason)
        elif action == "import":
            if not args.confirm: raise ValueError("Artifact registry import requires --confirm")
            with EvolutionStore(state_path) as store:
                payload = EvolutionNetworkService(store).import_reviewed_staged_artifact(args.snapshot_id, args.artifact_id, args.version)
        else:
            if not args.confirm: raise ValueError("Tracked Artifact registry import requires --confirm")
            with EvolutionStore(state_path) as store:
                payload = EvolutionNetworkService(store).import_tracked_artifacts_from_reviewed_snapshot(
                    snapshot_id=args.snapshot_id, scope_key=args.scope_key,
                    allowed_capabilities=tuple(args.allowed_capability),
                )
    elif command == "tenant":
        with EvolutionStore(state_path) as store:
            service = EvolutionNetworkService(store)
            if args.evolution_tenant_command == "preview":
                payload = service.preview_tenant_adoption(args.tenant_id, args.release_id)
            elif args.evolution_tenant_command == "adopt":
                if not args.confirm:
                    raise ValueError("Tenant adoption requires --confirm")
                payload = service.adopt_registry_release(args.tenant_id, args.release_id)
            else:
                if not args.confirm:
                    raise ValueError("Tenant adoption rollback requires --confirm")
                payload = service.rollback_tenant_adoption(args.tenant_id, args.role)
    elif command == "registry":
        if args.evolution_registry_command == "build":
            destination = args.destination.expanduser().resolve()
            if destination.exists() and not args.force:
                raise ValueError("Registry bundle destination already exists; pass --force to replace it")
            with EvolutionStore(state_path) as store:
                bundle = EvolutionNetworkService(store).build_public_registry_bundle(args.registry_id)
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text(
                json.dumps(bundle, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
                encoding="utf-8",
            )
            payload = {"destination": str(destination), "bundle": bundle}
        elif args.evolution_registry_command == "inspect":
            payload = EvolutionNetworkService.inspect_public_registry_bundle(args.source)
        elif args.evolution_registry_command == "fetch":
            payload = EvolutionNetworkService.fetch_public_registry_bundle(
                args.url, allow_insecure_loopback=args.allow_insecure_loopback
            )
        elif args.evolution_registry_command == "fetch-stage":
            if not args.confirm:
                raise ValueError("Remote registry fetch and staging requires --confirm")
            bundle = EvolutionNetworkService.fetch_public_registry_bundle(
                args.url, allow_insecure_loopback=args.allow_insecure_loopback
            )
            if not args.signature.is_file() or not 0 < args.signature.stat().st_size <= 32 * 1024:
                raise ValueError("Registry signature must be an existing detached file up to 32 KiB")
            signature_bytes = args.signature.read_bytes()
            with EvolutionStore(state_path) as store:
                payload = EvolutionNetworkService(store).stage_verified_public_registry_bundle(
                    bundle,
                    source_label=args.source_label,
                    signature=signature_bytes,
                    allowed_signers=args.allowed_signers,
                    principal=args.principal,
                    ssh_keygen=args.ssh_keygen,
                )
        elif args.evolution_registry_command == "stage":
            if not args.confirm:
                raise ValueError("Registry bundle staging requires --confirm")
            bundle = EvolutionNetworkService.inspect_public_registry_bundle(args.source)
            if not args.signature.is_file() or not 0 < args.signature.stat().st_size <= 32 * 1024:
                raise ValueError("Registry signature must be an existing detached file up to 32 KiB")
            signature_bytes = args.signature.read_bytes()
            with EvolutionStore(state_path) as store:
                payload = EvolutionNetworkService(store).stage_verified_public_registry_bundle(
                    bundle,
                    source_label=args.source_label,
                    signature=signature_bytes,
                    allowed_signers=args.allowed_signers,
                    principal=args.principal,
                    ssh_keygen=args.ssh_keygen,
                )
        elif args.evolution_registry_command == "staged-list":
            with EvolutionStore(state_path) as store:
                payload = {"snapshots": EvolutionNetworkService(store).list_staged_public_registry_bundles()}
        elif args.evolution_registry_command == "trust":
            with EvolutionStore(state_path) as store:
                service = EvolutionNetworkService(store)
                if args.evolution_registry_trust_command == "list":
                    payload = {"trust_roots": service.list_public_registry_signers()}
                else:
                    if not args.confirm:
                        raise ValueError("Registry signer trust changes require --confirm")
                    if args.evolution_registry_trust_command == "register":
                        payload = service.register_public_registry_signer(source_label=args.source_label, allowed_signers=args.allowed_signers, principal=args.principal, operator_id=args.operator_id)
                    elif args.evolution_registry_trust_command == "retire":
                        payload = service.retire_public_registry_signer(args.trust_root_id, operator_id=args.operator_id)
                    else:
                        payload = service.revoke_public_registry_signer(args.trust_root_id, operator_id=args.operator_id, reason=args.reason)
        elif args.evolution_registry_command == "review":
            with EvolutionStore(state_path) as store:
                service = EvolutionNetworkService(store)
                if args.evolution_registry_review_command == "preview": payload = service.preview_staged_registry_compatibility(args.snapshot_id)
                else:
                    if not args.confirm: raise ValueError("Registry snapshot review requires --confirm")
                    payload = service.review_staged_registry_snapshot(args.snapshot_id, operator_id=args.operator_id, decision=args.evolution_registry_review_command.upper(), reason=args.reason)
        elif args.evolution_registry_command == "tenant-candidate":
            with EvolutionStore(state_path) as store:
                service = EvolutionNetworkService(store); action = args.evolution_registry_candidate_command
                if action == "preview": payload = service.preview_remote_tenant_candidate(args.tenant_id, args.snapshot_id, args.remote_release_id)
                elif action == "propose":
                    if not args.confirm: raise ValueError("Remote tenant candidate proposal requires --confirm")
                    payload = service.propose_remote_tenant_candidate(args.tenant_id, args.snapshot_id, args.remote_release_id, operator_id=args.operator_id, reason=args.reason)
                else:
                    if not args.confirm: raise ValueError("Remote tenant candidate resolution requires --confirm")
                    payload = service.resolve_remote_tenant_candidate(args.candidate_id, operator_id=args.operator_id, decision=action.upper(), reason=args.reason)
        else:
            bundle = EvolutionNetworkService.inspect_public_registry_bundle(args.source)
            payload = {
                "payload": EvolutionNetworkService.public_registry_bundle_signing_payload(bundle).decode("utf-8"),
                "bundle_digest": bundle["bundle_digest"],
            }
    elif command == "network":
        if args.evolution_network_command == "status":
            payload = EvolutionNetworkService.network_gate_status()
        elif args.evolution_network_command == "probe":
            from dynamic_firm.evolution.hosted_transport import probe_public_service
            payload = probe_public_service(
                args.endpoint,
                allow_insecure_loopback=args.allow_insecure_loopback,
            )
        elif args.evolution_network_command == "worker-preview":
            payload = EvolutionNetworkService.preview_network_worker(args.tenant_id, args.release_id)
        elif args.evolution_network_command == "capability-preview":
            payload = EvolutionNetworkService.preview_capability_grant(args.tenant_id, args.release_id, args.job_id, tuple(args.capability))
        elif args.evolution_network_command == "authorization-preview":
            payload = EvolutionNetworkService.hosted_release_authorization_preview()
        elif args.evolution_network_command == "operator-enrollment-preview":
            payload = EvolutionNetworkService.operator_enrollment_preview(
                role=args.role,
                token_env=args.token_env,
                identity=args.identity,
                authority=args.authority,
            )
        elif args.evolution_network_command in {
            "candidates",
            "finalize-capsule",
            "assemble-candidates",
            "evaluate-candidate",
            "expire-pending",
            "authorize-artifact-registry",
            "publish-artifact-registry",
            "retire-artifact-registry",
        }:
            if not args.confirm:
                raise ValueError("Hosted Evolution Network operator transport requires --confirm")
            from dynamic_firm.evolution.hosted_transport import (
                list_operator_candidates,
                expire_pending_contributions,
                finalize_pending_contribution,
                assemble_candidates,
                authorize_artifact_registry_publication,
                publish_artifact_registry,
                record_candidate_evaluation,
                retire_artifact_registry,
                token_from_environment,
            )

            token = token_from_environment(args.token_env)
            if args.evolution_network_command == "candidates":
                payload = list_operator_candidates(
                    endpoint=args.endpoint, token=token,
                    cursor=args.cursor, limit=args.limit,
                    allow_insecure_loopback=args.allow_insecure_loopback,
                )
            elif args.evolution_network_command == "finalize-capsule":
                payload = finalize_pending_contribution(
                    endpoint=args.endpoint, token=token, contribution_id=args.contribution_id,
                    allow_insecure_loopback=args.allow_insecure_loopback,
                )
            elif args.evolution_network_command == "assemble-candidates":
                payload = assemble_candidates(
                    endpoint=args.endpoint, token=token,
                    allow_insecure_loopback=args.allow_insecure_loopback,
                )
            elif args.evolution_network_command == "expire-pending":
                payload = expire_pending_contributions(
                    endpoint=args.endpoint, token=token,
                    allow_insecure_loopback=args.allow_insecure_loopback,
                )
            elif args.evolution_network_command == "evaluate-candidate":
                if not args.evaluation.is_file() or args.evaluation.stat().st_size > 32 * 1024:
                    raise ValueError("Candidate evaluation must be an existing JSON file up to 32 KiB")
                try:
                    evaluation_value = json.loads(args.evaluation.read_text(encoding="utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                    raise ValueError("Candidate evaluation must be valid UTF-8 JSON") from exc
                payload = record_candidate_evaluation(
                    endpoint=args.endpoint, token=token, candidate_id=args.candidate_id,
                    evaluation=EvolutionNetworkService.validate_candidate_evaluation(evaluation_value),
                    allow_insecure_loopback=args.allow_insecure_loopback,
                )
            elif args.evolution_network_command == "authorize-artifact-registry":
                bundle = EvolutionNetworkService.inspect_artifact_registry_bundle(args.bundle)
                if bundle["registry_id"] != args.registry_id:
                    raise ValueError("Artifact registry id must match the bundle registry_id")
                payload = authorize_artifact_registry_publication(
                    endpoint=args.endpoint,
                    token=token,
                    registry_id=args.registry_id,
                    bundle_digest=bundle["bundle_digest"],
                    candidate_evidence_digests=tuple(args.candidate_evidence_digest),
                    evaluation_evidence_digests=tuple(args.evaluation_evidence_digest),
                    artifact_manifest_digests=tuple(
                        str(item["manifest_digest"])
                        for item in bundle["artifacts"]
                    ),
                    reviewer_id=args.reviewer_id,
                    reason_code=args.reason_code,
                    allow_insecure_loopback=args.allow_insecure_loopback,
                )
            elif args.evolution_network_command == "publish-artifact-registry":
                bundle = EvolutionNetworkService.inspect_artifact_registry_bundle(args.bundle)
                if bundle["registry_id"] != args.registry_id:
                    raise ValueError("Artifact registry id must match the signed bundle registry_id")
                if not args.signature.is_file() or args.signature.stat().st_size > 32 * 1024:
                    raise ValueError("Artifact registry signature must be an existing detached file up to 32 KiB")
                signature_bytes = args.signature.read_bytes()
                signature_receipt = EvolutionNetworkService.verify_artifact_registry_bundle_signature_bytes(
                    bundle,
                    signature=signature_bytes,
                    allowed_signers=args.allowed_signers,
                    principal=args.principal,
                    ssh_keygen=args.ssh_keygen,
                )
                publication = publish_artifact_registry(
                    endpoint=args.endpoint, token=token, registry_id=args.registry_id,
                    authorization_id=args.authorization_id,
                    bundle=bundle, signature=signature_bytes,
                    allow_insecure_loopback=args.allow_insecure_loopback,
                )
                payload = {"publication": publication, "local_signature_receipt": signature_receipt}
            else:
                payload = retire_artifact_registry(
                    endpoint=args.endpoint,
                    token=token,
                    registry_id=args.registry_id,
                    reason_code=args.reason_code,
                    allow_insecure_loopback=args.allow_insecure_loopback,
                )
        else:
            if not args.confirm:
                raise ValueError("Hosted Evolution Network transport requires --confirm")
            from dynamic_firm.evolution.hosted_transport import token_from_environment

            token = token_from_environment(args.token_env)
            with EvolutionStore(state_path) as store:
                service = EvolutionNetworkService(store)
                if args.evolution_network_command == "submit":
                    withdrawal_capability = token_from_environment(
                        args.withdrawal_capability_env
                    )
                    payload = service.submit_hosted_capsule(
                        args.capsule_id,
                        endpoint=args.endpoint,
                        token=token,
                        withdrawal_capability=withdrawal_capability,
                        allow_insecure_loopback=args.allow_insecure_loopback,
                    )
                elif args.evolution_network_command == "withdraw":
                    withdrawal_capability = token_from_environment(args.withdrawal_capability_env)
                    payload = service.withdraw_hosted_capsule(
                        args.capsule_id,
                        endpoint=args.endpoint,
                        token=token,
                        withdrawal_capability=withdrawal_capability,
                        allow_insecure_loopback=args.allow_insecure_loopback,
                    )
                else:
                    raise ValueError("withdraw-consent is unavailable for capability-only hosted intake; withdraw each pending Capsule with its own capability")
    elif command == "capsule" and args.evolution_capsule_command == "build-job":
        from dynamic_firm.evolution import (
            ActiveJobCapsuleEvidence,
            CapsuleAuthority,
            CapsuleCostBucket,
            CapsuleEvaluatorKind,
            CapsuleEvidenceSource,
            CapsuleExecutionEvidence,
            CapsuleOutcomeEvidence,
            CapsuleOutcomeStatus,
            CapsuleRiskLevel,
            CapsuleTaskEvidence,
            build_learning_capsule,
            preview_learning_capsule,
            validate_evolution_proposal,
        )
        from dynamic_firm.company.models import content_digest
        from dynamic_firm.evolution.service import validate_capsule
        from dynamic_firm.runtime.job_ledger import (
            ActiveJobAuditStatus,
            ActiveJobInspector,
        )

        runtime_state = runtime_state_path(args, settings)
        if not runtime_state.is_file():
            raise ValueError("ACTIVE JOB state does not exist")
        runtime_store = RunStore(runtime_state)
        try:
            inspection = ActiveJobInspector(runtime_store).inspect(args.job_id)
        finally:
            runtime_store.close()
        if (
            inspection.audit_status is not ActiveJobAuditStatus.TERMINAL
            or inspection.errors
            or not inspection.replay_matches
            or inspection.terminal is None
        ):
            raise ValueError(
                "Learning Capsule requires one valid, replay-matching terminal ACTIVE JOB"
            )
        outcome_status = {
            "SUCCEEDED": CapsuleOutcomeStatus.SUCCEEDED,
            "FAILED": CapsuleOutcomeStatus.FAILED,
        }.get(str(inspection.job_status), CapsuleOutcomeStatus.PARTIAL)
        metrics = inspection.terminal.get("metrics", {})
        maximum_parallelism = (
            int(metrics.get("maximum_parallelism", 1))
            if isinstance(metrics, Mapping)
            else 1
        )
        task_count = len(inspection.reconstructed_tasks)
        workflow_shape = (
            ("solo",)
            if task_count <= 1
            else ("parallel_join",)
            if maximum_parallelism > 1
            else ("sequential",)
        )
        evidence = ActiveJobCapsuleEvidence(
            source=CapsuleEvidenceSource.ACTIVE_JOB_LEDGER,
            source_record_digest=inspection.chain_head,
            capability=args.capability,
            authority=CapsuleAuthority[args.authority],
            task=CapsuleTaskEvidence(
                domain=args.domain,
                operation=args.operation,
                input_fields=tuple(args.input_field),
                risk_level=CapsuleRiskLevel[args.risk_level],
            ),
            execution=CapsuleExecutionEvidence(
                workflow_shape=workflow_shape,
                tool_classes=tuple(args.tool_class),
                decision_count=inspection.attempt_count + inspection.mutation_count,
            ),
            outcome=CapsuleOutcomeEvidence(
                status=outcome_status,
                quality_score=args.quality_score,
                cost_bucket=CapsuleCostBucket[args.cost_bucket],
                evaluator_kind=CapsuleEvaluatorKind[args.evaluator_kind],
                metric_names=tuple(args.metric_name),
            ),
        )
        capsule_payload = build_learning_capsule(evidence)
        capsule_preview = dict(preview_learning_capsule(evidence))
        if args.proposal is not None:
            proposal_path = args.proposal.expanduser().resolve()
            if (
                not proposal_path.is_file()
                or proposal_path.stat().st_size > 64 * 1024
            ):
                raise ValueError(
                    "Typed Evolution Proposal must be an existing JSON file up to 64 KiB"
                )
            try:
                proposal_value = json.loads(
                    proposal_path.read_text(encoding="utf-8")
                )
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ValueError(
                    "Typed Evolution Proposal must be valid UTF-8 JSON"
                ) from exc
            proposal = validate_evolution_proposal(proposal_value)
            capsule_payload = validate_capsule(
                {
                    **capsule_payload,
                    "schema": "noruct.learning-capsule.v2",
                    "proposal": proposal,
                }
            )
            capsule_preview.update(
                {
                    "payload_digest": content_digest(capsule_payload),
                    "proposal": {
                        "kind": proposal["kind"],
                        "digest": content_digest(proposal),
                    },
                }
            )
        destination = args.destination.expanduser().resolve()
        if destination.exists() and not args.force:
            raise ValueError(
                "Learning Capsule destination already exists; pass --force to replace it"
            )
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(
            json.dumps(capsule_payload, ensure_ascii=False, sort_keys=True, indent=2)
            + "\n",
            encoding="utf-8",
        )
        payload = {
            "destination": str(destination),
            "preview": capsule_preview,
            "network_request_performed": False,
            "queued": False,
        }
    elif command == "delete":
        if not args.confirm:
            raise ValueError("Evolution Network local deletion requires --confirm")
        for candidate in (state_path, Path(f"{state_path}-wal"), Path(f"{state_path}-shm")):
            candidate.unlink(missing_ok=True)
        payload: object = {"deleted": True, "state_path": str(state_path)}
    elif command == "export":
        destination = args.destination.expanduser().resolve()
        if destination.exists() and not args.force:
            raise ValueError("Evolution export destination already exists; pass --force to replace it")
        with EvolutionStore(state_path) as store:
            payload_data = EvolutionNetworkService(store).export_payload()
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(
            json.dumps(payload_data, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
            encoding="utf-8",
        )
        payload = {"destination": str(destination), "export": payload_data}
    else:
        with EvolutionStore(state_path) as store:
            service = EvolutionNetworkService(store)
            if command == "status":
                payload = service.status()
            elif command == "consent":
                if not args.confirm:
                    raise ValueError("Evolution consent changes require --confirm")
                if args.evolution_consent_command == "grant":
                    payload = service.grant_consent(
                        purpose=args.purpose,
                        allowed_reuse=args.allowed_reuse,
                        retention_days=args.retention_days,
                        authority=args.authority,
                    )
                else:
                    payload = service.withdraw_consent(args.consent_id)
            elif command == "capsule":
                if args.evolution_capsule_command == "preview":
                    payload = service.preview_capsule_file(args.source)
                elif args.evolution_capsule_command == "submit":
                    if not args.confirm:
                        raise ValueError("Learning Capsule submission requires --confirm")
                    payload = service.submit_capsule_file(args.source, args.consent_id)
                else:
                    if not args.confirm:
                        raise ValueError("Learning Capsule withdrawal requires --confirm")
                    payload = service.withdraw_capsule(args.capsule_id)
            elif command == "blueprint":
                if args.evolution_blueprint_command == "preview":
                    payload = service.preview_blueprint_file(args.source)
                elif args.evolution_blueprint_command == "import":
                    if not args.confirm:
                        raise ValueError("Employee Blueprint import requires --confirm")
                    payload = service.import_blueprint_file(args.source)
                elif args.evolution_blueprint_command == "list":
                    payload = {"blueprints": service.list_blueprints(), "execution": "CATALOG_ONLY"}
                elif args.evolution_blueprint_command == "select":
                    if not args.confirm:
                        raise ValueError("Employee Blueprint selection requires --confirm")
                    payload = service.select_blueprint(args.blueprint_id, args.version)
                else:
                    if not args.confirm:
                        raise ValueError("Employee Blueprint rollback requires --confirm")
                    payload = service.rollback_selection(args.role)
            else:
                raise ValueError(f"unknown evolution command: {command}")
    if args.json:
        print(json.dumps(to_primitive(payload), ensure_ascii=False, sort_keys=True, indent=2), file=output)
    else:
        print(human_summary(command, payload), file=output)
    return exit_ok
