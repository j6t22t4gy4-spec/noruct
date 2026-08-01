"""Local-first contracts for the optional Evolution Network.

Local Company operation remains independent of the network.  Hosted transport
is explicit opt-in only, and the runtime adapter may inspect or stage trusted
versioned artifacts but never silently mutates Company state.
"""

from .evaluation import (
    BLUEPRINT_ADMISSION_SCHEMA,
    BLUEPRINT_DELTA_HOLDOUT_SCHEMA,
    BLUEPRINT_DELTA_HOLDOUT_SUITE_SCHEMA,
    BlueprintAdmissionDecision,
    BlueprintAdmissionReport,
    BlueprintDeltaHoldoutDecision,
    BlueprintDeltaHoldoutReport,
    BlueprintDeltaHoldoutSuiteReport,
    evaluate_blueprint_admission,
    evaluate_blueprint_delta_holdout,
    evaluate_blueprint_delta_holdout_suite,
)
from .service import (
    EVOLUTION_ARTIFACT_SCHEMA,
    WORKFORCE_PASSPORT_SCHEMA,
    EvolutionNetworkService,
    CANDIDATE_EVALUATION_SCHEMA,
    validate_candidate_evaluation,
    validate_evolution_artifact,
    validate_evolution_proposal,
)
from .network_gate import NETWORK_GATE_SCHEMA, network_gate_status, preview_network_worker
from .capability_gateway import CAPABILITY_GATEWAY_SCHEMA, preview_capability_grant
from .store import EvolutionStore, UnsupportedEvolutionStoreSchemaError
from .registry_bundle import (
    REGISTRY_BUNDLE_SCHEMA,
    REGISTRY_BUNDLE_SIGNING_SCHEMA,
    build_registry_bundle,
    fetch_registry_bundle,
    read_registry_bundle,
    registry_bundle_signing_payload,
    validate_registry_bundle,
)
from .hosted_transport import (
    HostedReceipt,
    HostedTransportError,
    authorize_artifact_registry_publication,
    publish_artifact_registry,
    token_from_environment,
)
from .artifact_bundle import (
    ARTIFACT_REGISTRY_BUNDLE_SCHEMA,
    ARTIFACT_REGISTRY_SIGNING_SCHEMA,
    artifact_registry_bundle_signing_payload,
    build_artifact_registry_bundle,
    fetch_artifact_registry_signature,
    read_artifact_registry_bundle,
    validate_artifact_registry_bundle,
)
from .capsule_builder import (
    CAPSULE_BUILD_PREVIEW_SCHEMA,
    ActiveJobCapsuleEvidence,
    BlueprintDeltaProposalEvidence,
    CapsuleAuthority,
    CapsuleCostBucket,
    CapsuleEvaluatorKind,
    CapsuleEvidenceSource,
    CapsuleExecutionEvidence,
    CapsuleOutcomeEvidence,
    CapsuleOutcomeStatus,
    CapsuleRiskLevel,
    CapsuleTaskEvidence,
    UnsafeCapsuleEvidenceError,
    build_learning_capsule,
    preview_learning_capsule,
)
from .score_contract import canonical_evolution_json, evolution_content_digest
from .artifact_origin import ArtifactOriginKind
from .shadow_evaluation import (
    ARTIFACT_SHADOW_PROJECTION_SCHEMA,
    ARTIFACT_SHADOW_RECEIPT_SCHEMA,
    ARTIFACT_SHADOW_SLOT_SCHEMA,
    ShadowEvaluationIntegrityError,
)
from .store_artifact_regression import (
    ARTIFACT_REGRESSION_PROJECTION_SCHEMA,
    ARTIFACT_REGRESSION_SIGNAL_SCHEMA,
)

__all__ = (
    "BLUEPRINT_ADMISSION_SCHEMA",
    "BLUEPRINT_DELTA_HOLDOUT_SCHEMA",
    "BLUEPRINT_DELTA_HOLDOUT_SUITE_SCHEMA",
    "BlueprintAdmissionDecision",
    "BlueprintAdmissionReport",
    "BlueprintDeltaHoldoutDecision",
    "BlueprintDeltaHoldoutReport",
    "BlueprintDeltaHoldoutSuiteReport",
    "EvolutionNetworkService",
    "ArtifactOriginKind",
    "ARTIFACT_SHADOW_PROJECTION_SCHEMA",
    "ARTIFACT_SHADOW_RECEIPT_SCHEMA",
    "ARTIFACT_SHADOW_SLOT_SCHEMA",
    "ARTIFACT_REGRESSION_PROJECTION_SCHEMA",
    "ARTIFACT_REGRESSION_SIGNAL_SCHEMA",
    "CANDIDATE_EVALUATION_SCHEMA",
    "EVOLUTION_ARTIFACT_SCHEMA",
    "WORKFORCE_PASSPORT_SCHEMA",
    "HostedReceipt",
    "HostedTransportError",
    "authorize_artifact_registry_publication",
    "publish_artifact_registry",
    "NETWORK_GATE_SCHEMA",
    "CAPABILITY_GATEWAY_SCHEMA",
    "ARTIFACT_REGISTRY_BUNDLE_SCHEMA",
    "ARTIFACT_REGISTRY_SIGNING_SCHEMA",
    "CAPSULE_BUILD_PREVIEW_SCHEMA",
    "REGISTRY_BUNDLE_SCHEMA",
    "REGISTRY_BUNDLE_SIGNING_SCHEMA",
    "EvolutionStore",
    "UnsupportedEvolutionStoreSchemaError",
    "ActiveJobCapsuleEvidence",
    "BlueprintDeltaProposalEvidence",
    "CapsuleAuthority",
    "CapsuleCostBucket",
    "CapsuleEvaluatorKind",
    "CapsuleEvidenceSource",
    "CapsuleExecutionEvidence",
    "CapsuleOutcomeEvidence",
    "CapsuleOutcomeStatus",
    "CapsuleRiskLevel",
    "CapsuleTaskEvidence",
    "UnsafeCapsuleEvidenceError",
    "ShadowEvaluationIntegrityError",
    "evaluate_blueprint_admission",
    "evaluate_blueprint_delta_holdout",
    "evaluate_blueprint_delta_holdout_suite",
    "build_registry_bundle",
    "build_artifact_registry_bundle",
    "fetch_artifact_registry_signature",
    "fetch_registry_bundle",
    "network_gate_status",
    "preview_network_worker",
    "preview_capability_grant",
    "read_registry_bundle",
    "read_artifact_registry_bundle",
    "artifact_registry_bundle_signing_payload",
    "validate_artifact_registry_bundle",
    "registry_bundle_signing_payload",
    "validate_registry_bundle",
    "validate_evolution_artifact",
    "validate_evolution_proposal",
    "validate_candidate_evaluation",
    "token_from_environment",
    "build_learning_capsule",
    "preview_learning_capsule",
    "canonical_evolution_json",
    "evolution_content_digest",
)
