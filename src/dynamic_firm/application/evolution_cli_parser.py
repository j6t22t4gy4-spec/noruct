"""Argument schema for the opt-in Evolution command family."""
from __future__ import annotations

import argparse
from pathlib import Path


def add_evolution_paths(item: argparse.ArgumentParser) -> None:
    """Add the shared local Evolution state options used by two command families."""
    item.add_argument("--state", type=Path, default=None)
    item.add_argument(
        "--evolution-state",
        type=Path,
        default=None,
        help="Separate local Evolution Network SQLite path (default: sibling of --state).",
    )
    item.add_argument("--json", action="store_true")


def add_evolution_commands(commands: argparse._SubParsersAction) -> None:
    evolution = commands.add_parser(
        "evolution",
        help="Manage opt-in Evolution state and explicit, minimized hosted intake.",
    )
    evolution_commands = evolution.add_subparsers(dest="evolution_command", required=True)

    evolution_status = evolution_commands.add_parser(
        "status", help="Show local consent, capsule, and Blueprint catalog status without any network call."
    )
    add_evolution_paths(evolution_status)
    consent = evolution_commands.add_parser(
        "consent", help="Grant or withdraw explicit, purpose-bound contribution consent."
    )
    consent_commands = consent.add_subparsers(dest="evolution_consent_command", required=True)
    consent_grant = consent_commands.add_parser(
        "grant", help="Record explicit local consent; it does not transmit any data."
    )
    consent_grant.add_argument("--purpose", default="BLUEPRINT_IMPROVEMENT")
    consent_grant.add_argument("--allowed-reuse", default="EVALUATE_AND_PROMOTE_BLUEPRINT")
    consent_grant.add_argument("--retention-days", type=int, required=True)
    consent_grant.add_argument("--authority", required=True, choices=("INDIVIDUAL", "ORGANIZATION_OWNER"))
    consent_grant.add_argument("--confirm", action="store_true")
    add_evolution_paths(consent_grant)
    consent_withdraw = consent_commands.add_parser(
        "withdraw", help="Withdraw one consent and delete its locally queued capsule payloads."
    )
    consent_withdraw.add_argument("consent_id")
    consent_withdraw.add_argument("--confirm", action="store_true")
    add_evolution_paths(consent_withdraw)
    capsule = evolution_commands.add_parser(
        "capsule", help="Preview, locally queue, or withdraw a minimized Learning Capsule."
    )
    capsule_commands = capsule.add_subparsers(dest="evolution_capsule_command", required=True)
    capsule_preview = capsule_commands.add_parser(
        "preview", help="Validate and show the sanitized capsule without writing state."
    )
    capsule_preview.add_argument("source", type=Path)
    add_evolution_paths(capsule_preview)
    capsule_build_job = capsule_commands.add_parser(
        "build-job",
        help=(
            "Build a strict data-only Capsule from one verified terminal ACTIVE JOB; "
            "the command writes locally and never submits it."
        ),
    )
    capsule_build_job.add_argument("job_id")
    capsule_build_job.add_argument("destination", type=Path)
    capsule_build_job.add_argument("--capability", required=True)
    capsule_build_job.add_argument("--domain", required=True)
    capsule_build_job.add_argument("--operation", required=True)
    capsule_build_job.add_argument("--input-field", action="append", required=True)
    capsule_build_job.add_argument("--tool-class", action="append", required=True)
    capsule_build_job.add_argument("--metric-name", action="append", required=True)
    capsule_build_job.add_argument(
        "--risk-level", choices=("LOW", "MEDIUM", "HIGH"), required=True
    )
    capsule_build_job.add_argument("--quality-score", type=float, required=True)
    capsule_build_job.add_argument(
        "--cost-bucket", choices=("LOW", "MEDIUM", "HIGH"), required=True
    )
    capsule_build_job.add_argument(
        "--evaluator-kind",
        choices=("LOCAL_TEST", "USER_REVIEW", "OFFLINE_FIXTURE"),
        required=True,
    )
    capsule_build_job.add_argument(
        "--authority",
        choices=("INDIVIDUAL", "ORGANIZATION_OWNER"),
        required=True,
    )
    capsule_build_job.add_argument(
        "--proposal",
        type=Path,
        default=None,
        help=(
            "Optional strict typed Proposal JSON (Blueprint Delta or versioned "
            "Skill/Workflow/Roster/Tool Artifact); raw text is rejected."
        ),
    )
    capsule_build_job.add_argument("--force", action="store_true")
    add_evolution_paths(capsule_build_job)
    capsule_submit = capsule_commands.add_parser(
        "submit", help="Queue a validated capsule locally; outbound transport remains disabled."
    )
    capsule_submit.add_argument("source", type=Path)
    capsule_submit.add_argument("--consent-id", required=True)
    capsule_submit.add_argument("--confirm", action="store_true")
    add_evolution_paths(capsule_submit)
    capsule_withdraw = capsule_commands.add_parser(
        "withdraw", help="Delete one locally queued capsule payload and keep a minimal withdrawal receipt."
    )
    capsule_withdraw.add_argument("capsule_id")
    capsule_withdraw.add_argument("--confirm", action="store_true")
    add_evolution_paths(capsule_withdraw)
    blueprint = evolution_commands.add_parser(
        "blueprint", help="Inspect the local, catalog-only Employee Blueprint registry."
    )
    blueprint_commands = blueprint.add_subparsers(dest="evolution_blueprint_command", required=True)
    blueprint_preview = blueprint_commands.add_parser(
        "preview", help="Validate a Blueprint manifest without writing state."
    )
    blueprint_preview.add_argument("source", type=Path)
    add_evolution_paths(blueprint_preview)
    blueprint_import = blueprint_commands.add_parser(
        "import", help="Import an immutable Blueprint manifest into the local catalog."
    )
    blueprint_import.add_argument("source", type=Path)
    blueprint_import.add_argument("--confirm", action="store_true")
    add_evolution_paths(blueprint_import)
    blueprint_list = blueprint_commands.add_parser("list", help="List local catalog Blueprints.")
    add_evolution_paths(blueprint_list)
    blueprint_select = blueprint_commands.add_parser(
        "select", help="Select a catalog Blueprint for a role; it does not create or run an employee."
    )
    blueprint_select.add_argument("blueprint_id")
    blueprint_select.add_argument("version")
    blueprint_select.add_argument("--confirm", action="store_true")
    add_evolution_paths(blueprint_select)
    blueprint_rollback = blueprint_commands.add_parser(
        "rollback", help="Restore the immediately previous catalog selection for one role."
    )
    blueprint_rollback.add_argument("role")
    blueprint_rollback.add_argument("--confirm", action="store_true")
    add_evolution_paths(blueprint_rollback)
    artifact = evolution_commands.add_parser(
        "artifact",
        help="Manage versioned local Skill, Agent, Tool, Playbook, and Benchmark artifacts; no remote update is implicit.",
    )
    artifact_commands = artifact.add_subparsers(dest="evolution_artifact_command", required=True)
    artifact_preview = artifact_commands.add_parser("preview", help="Validate an Artifact manifest without writing local state.")
    artifact_preview.add_argument("source", type=Path); add_evolution_paths(artifact_preview)
    artifact_register = artifact_commands.add_parser("register", help="Add an immutable Artifact version to the local catalog; it is not installed or active.")
    artifact_register.add_argument("source", type=Path); artifact_register.add_argument("--confirm", action="store_true"); add_evolution_paths(artifact_register)
    artifact_list = artifact_commands.add_parser("list", help="List local Artifact versions.")
    artifact_list.add_argument("--artifact-id"); artifact_list.add_argument("--kind"); add_evolution_paths(artifact_list)
    artifact_stage = artifact_commands.add_parser("stage", help="Stage one cataloged Artifact version without installing or activating it.")
    artifact_stage.add_argument("artifact_id"); artifact_stage.add_argument("version"); artifact_stage.add_argument("--confirm", action="store_true"); add_evolution_paths(artifact_stage)
    artifact_install = artifact_commands.add_parser("install", help="Install one staged Artifact version without activating it.")
    artifact_install.add_argument("artifact_id"); artifact_install.add_argument("version"); artifact_install.add_argument("--confirm", action="store_true"); add_evolution_paths(artifact_install)
    artifact_activate = artifact_commands.add_parser("activate", help="Explicitly activate an installed Artifact for one local scope.")
    artifact_activate.add_argument("scope_key"); artifact_activate.add_argument("artifact_id"); artifact_activate.add_argument("version")
    artifact_activate.add_argument("--allowed-capability", action="append", default=[]); artifact_activate.add_argument("--confirm", action="store_true"); add_evolution_paths(artifact_activate)
    artifact_active = artifact_commands.add_parser("active", help="List the active Artifact snapshot for one local scope.")
    artifact_active.add_argument("scope_key"); add_evolution_paths(artifact_active)
    artifact_shadow = artifact_commands.add_parser(
        "shadow-receipts",
        help=(
            "List immutable local shadow-evaluation receipts without running a "
            "provider, contacting the Network, or changing activation."
        ),
    )
    artifact_shadow.add_argument("--scope-key")
    artifact_shadow.add_argument("--artifact-id")
    add_evolution_paths(artifact_shadow)
    artifact_regressions = artifact_commands.add_parser(
        "regressions",
        help="List content-free post-activation regression signals and any exact rollback proposal.",
    )
    artifact_regressions.add_argument("--scope-key")
    artifact_regressions.add_argument("--artifact-id")
    add_evolution_paths(artifact_regressions)
    artifact_report_regression = artifact_commands.add_parser(
        "report-regression",
        help="Append one operator-observed regression signal; it does not deactivate or execute an Artifact.",
    )
    artifact_report_regression.add_argument("scope_key")
    artifact_report_regression.add_argument("artifact_id")
    artifact_report_regression.add_argument(
        "--signal-kind",
        required=True,
        choices=("QUALITY_REGRESSION", "SAFETY_REGRESSION", "EFFECT_FAILURE", "OPERATOR_INTERVENTION"),
    )
    artifact_report_regression.add_argument("--evidence-digest", required=True)
    artifact_report_regression.add_argument("--confirm", action="store_true")
    add_evolution_paths(artifact_report_regression)
    artifact_rollback = artifact_commands.add_parser(
        "rollback",
        help=(
            "Restore the immediately prior local Artifact activation. Use "
            "--artifact-id when multiple active Artifacts share a kind."
        ),
    )
    artifact_rollback.add_argument("scope_key")
    artifact_rollback.add_argument("kind", nargs="?")
    artifact_rollback.add_argument("--artifact-id")
    artifact_rollback.add_argument("--confirm", action="store_true")
    add_evolution_paths(artifact_rollback)
    artifact_subscribe = artifact_commands.add_parser("subscribe", help="Set PINNED, latest Stable tracking, or experimental staging for an Artifact.")
    artifact_subscribe.add_argument("scope_key"); artifact_subscribe.add_argument("kind"); artifact_subscribe.add_argument("artifact_id")
    artifact_subscribe.add_argument("--mode", choices=("PINNED", "TRACK_STABLE", "TRACK_EXPERIMENTAL"), required=True); artifact_subscribe.add_argument("--confirm", action="store_true"); add_evolution_paths(artifact_subscribe)
    artifact_update = artifact_commands.add_parser("update", help="Apply explicit local update subscriptions from the existing catalog; never fetches the network.")
    artifact_update.add_argument("scope_key"); artifact_update.add_argument("--allowed-capability", action="append", default=[]); artifact_update.add_argument("--confirm", action="store_true"); add_evolution_paths(artifact_update)
    artifact_pin = artifact_commands.add_parser("pin-job", help="Freeze the current local Artifact snapshot for a Job id.")
    artifact_pin.add_argument("job_id"); artifact_pin.add_argument("scope_key"); artifact_pin.add_argument("--confirm", action="store_true"); add_evolution_paths(artifact_pin)
    artifact_registry = evolution_commands.add_parser("artifact-registry", help="Build or inspect a portable signed Artifact catalog; it never installs or activates an Artifact.")
    artifact_registry_commands = artifact_registry.add_subparsers(dest="evolution_artifact_registry_command", required=True)
    artifact_registry_build = artifact_registry_commands.add_parser("build")
    artifact_registry_build.add_argument("destination", type=Path); artifact_registry_build.add_argument("--registry-id", required=True); artifact_registry_build.add_argument("--force", action="store_true"); add_evolution_paths(artifact_registry_build)
    artifact_registry_inspect = artifact_registry_commands.add_parser("inspect")
    artifact_registry_inspect.add_argument("source", type=Path); add_evolution_paths(artifact_registry_inspect)
    artifact_registry_discover = artifact_registry_commands.add_parser(
        "discover",
        help="List up to 100 public Artifact registry pointers from one HTTPS origin; this does not fetch, trust, stage, or install a bundle.",
    )
    artifact_registry_discover.add_argument("origin")
    artifact_registry_discover.add_argument("--allow-insecure-loopback", action="store_true")
    add_evolution_paths(artifact_registry_discover)
    artifact_registry_fetch_stage_discovered = artifact_registry_commands.add_parser(
        "fetch-stage-discovered",
        help=(
            "Resolve one public index pointer, require matching bundle/signature digests, "
            "then verify and stage it locally. It never imports, installs, or activates Artifacts."
        ),
    )
    artifact_registry_fetch_stage_discovered.add_argument("origin")
    artifact_registry_fetch_stage_discovered.add_argument("registry_id")
    artifact_registry_fetch_stage_discovered.add_argument("--source-label", required=True)
    artifact_registry_fetch_stage_discovered.add_argument("--allowed-signers", type=Path, required=True)
    artifact_registry_fetch_stage_discovered.add_argument("--principal", required=True)
    artifact_registry_fetch_stage_discovered.add_argument("--ssh-keygen", type=Path, required=True)
    artifact_registry_fetch_stage_discovered.add_argument("--allow-insecure-loopback", action="store_true")
    artifact_registry_fetch_stage_discovered.add_argument("--confirm", action="store_true")
    add_evolution_paths(artifact_registry_fetch_stage_discovered)
    artifact_registry_fetch = artifact_registry_commands.add_parser("fetch", help="Fetch and validate an Artifact bundle without credentials or local mutation.")
    artifact_registry_fetch.add_argument("url"); artifact_registry_fetch.add_argument("--allow-insecure-loopback", action="store_true"); add_evolution_paths(artifact_registry_fetch)
    artifact_registry_fetch_stage = artifact_registry_commands.add_parser("fetch-stage", help="Fetch an HTTPS Artifact bundle, verify its trusted signature, and stage it without import.")
    artifact_registry_fetch_stage.add_argument("url"); artifact_registry_fetch_stage.add_argument("--source-label", required=True)
    artifact_registry_fetch_stage_signature = artifact_registry_fetch_stage.add_mutually_exclusive_group(required=True)
    artifact_registry_fetch_stage_signature.add_argument("--signature", type=Path)
    artifact_registry_fetch_stage_signature.add_argument("--signature-url")
    artifact_registry_fetch_stage.add_argument("--allowed-signers", type=Path, required=True); artifact_registry_fetch_stage.add_argument("--principal", required=True); artifact_registry_fetch_stage.add_argument("--ssh-keygen", type=Path, required=True); artifact_registry_fetch_stage.add_argument("--allow-insecure-loopback", action="store_true"); artifact_registry_fetch_stage.add_argument("--confirm", action="store_true"); add_evolution_paths(artifact_registry_fetch_stage)
    artifact_registry_staged_list = artifact_registry_commands.add_parser("staged-list", help="List verified Artifact bundle snapshots; entries remain unimported until review.")
    add_evolution_paths(artifact_registry_staged_list)
    artifact_registry_payload = artifact_registry_commands.add_parser("signing-payload")
    artifact_registry_payload.add_argument("source", type=Path); add_evolution_paths(artifact_registry_payload)
    artifact_registry_stage = artifact_registry_commands.add_parser("stage", help="Verify a signed Artifact bundle against a local trust root; never imports or activates it.")
    artifact_registry_stage.add_argument("source", type=Path); artifact_registry_stage.add_argument("--source-label", required=True); artifact_registry_stage.add_argument("--signature", type=Path, required=True); artifact_registry_stage.add_argument("--allowed-signers", type=Path, required=True); artifact_registry_stage.add_argument("--principal", required=True); artifact_registry_stage.add_argument("--ssh-keygen", type=Path, required=True); artifact_registry_stage.add_argument("--confirm", action="store_true"); add_evolution_paths(artifact_registry_stage)
    artifact_registry_review = artifact_registry_commands.add_parser("review")
    artifact_registry_review_commands = artifact_registry_review.add_subparsers(dest="evolution_artifact_registry_review_command", required=True)
    for name in ("preview", "approve", "reject"):
        item = artifact_registry_review_commands.add_parser(name); item.add_argument("snapshot_id")
        if name != "preview": item.add_argument("--operator-id", required=True); item.add_argument("--reason", required=True); item.add_argument("--confirm", action="store_true")
        add_evolution_paths(item)
    artifact_registry_import = artifact_registry_commands.add_parser("import", help="Explicitly import one reviewed staged Artifact into the local catalog; it remains inactive.")
    artifact_registry_import.add_argument("snapshot_id"); artifact_registry_import.add_argument("artifact_id"); artifact_registry_import.add_argument("version"); artifact_registry_import.add_argument("--confirm", action="store_true"); add_evolution_paths(artifact_registry_import)
    artifact_registry_import_tracked = artifact_registry_commands.add_parser("import-tracked", help="Import only opted-in tracked Artifact releases from an approved snapshot, then apply local tracker policy for the next Job.")
    artifact_registry_import_tracked.add_argument("snapshot_id"); artifact_registry_import_tracked.add_argument("scope_key")
    artifact_registry_import_tracked.add_argument("--allowed-capability", action="append", default=[])
    artifact_registry_import_tracked.add_argument("--confirm", action="store_true"); add_evolution_paths(artifact_registry_import_tracked)
    evolution_export = evolution_commands.add_parser(
        "export", help="Export the separate Evolution Network state as JSON."
    )
    evolution_export.add_argument("destination", type=Path)
    evolution_export.add_argument("--force", action="store_true")
    add_evolution_paths(evolution_export)
    evolution_delete = evolution_commands.add_parser(
        "delete", help="Delete the separate local Evolution Network database and sidecars."
    )
    evolution_delete.add_argument("--confirm", action="store_true")
    add_evolution_paths(evolution_delete)
    evolution_evaluate = evolution_commands.add_parser(
        "evaluate",
        help="Run a provider-free, non-promoting Blueprint admission screen on public/synthetic capsules.",
    )
    evolution_evaluate.add_argument("blueprint", type=Path)
    evolution_evaluate.add_argument("capsules", type=Path, nargs="+")
    evolution_evaluate.add_argument("--json", action="store_true")
    evolution_delta = evolution_commands.add_parser(
        "delta",
        help="Validate a reversible Blueprint Delta or run its public/synthetic holdout; no catalog mutation occurs.",
    )
    evolution_delta_commands = evolution_delta.add_subparsers(
        dest="evolution_delta_command", required=True
    )
    evolution_delta_preview = evolution_delta_commands.add_parser(
        "preview", help="Validate a bounded reversible Blueprint Delta without changing state."
    )
    evolution_delta_preview.add_argument("delta", type=Path)
    evolution_delta_preview.add_argument("--json", action="store_true")
    evolution_delta_holdout = evolution_delta_commands.add_parser(
        "holdout", help="Compare base Blueprint and Delta on the public synthetic capability-routing holdout."
    )
    evolution_delta_holdout.add_argument("blueprint", type=Path)
    evolution_delta_holdout.add_argument("delta", type=Path)
    evolution_delta_holdout.add_argument("--json", action="store_true")
    evolution_delta_suite = evolution_delta_commands.add_parser(
        "holdout-suite", help="Run both public synthetic quality and safety holdouts; still review-only."
    )
    evolution_delta_suite.add_argument("blueprint", type=Path)
    evolution_delta_suite.add_argument("delta", type=Path)
    evolution_delta_suite.add_argument("--json", action="store_true")
    release_candidate = evolution_commands.add_parser(
        "release-candidate", help="Create or inspect local provenance-bound release candidates; never releases a Blueprint."
    )
    release_candidate_commands = release_candidate.add_subparsers(dest="evolution_release_candidate_command", required=True)
    release_create = release_candidate_commands.add_parser("create", help="Bind a holdout-eligible Delta to active Capsule receipts.")
    release_create.add_argument("blueprint", type=Path)
    release_create.add_argument("delta", type=Path)
    release_create.add_argument("--capsule-id", action="append", required=True)
    release_create.add_argument("--confirm", action="store_true")
    add_evolution_paths(release_create)
    release_list = release_candidate_commands.add_parser("list", help="List local release candidates and revocation status.")
    add_evolution_paths(release_list)
    for name, decision, help_text in (
        ("approve", "APPROVE", "Approve a candidate for signature review; it cannot release a Blueprint."),
        ("reject", "REJECT", "Reject a pending candidate with an append-only operator review."),
    ):
        item = release_candidate_commands.add_parser(name, help=help_text)
        item.add_argument("candidate_id")
        item.add_argument("--operator-id", required=True)
        item.add_argument("--reason", required=True)
        item.add_argument("--confirm", action="store_true")
        item.set_defaults(evolution_review_decision=decision)
        add_evolution_paths(item)
    signing_payload = release_candidate_commands.add_parser("signing-payload", help="Print canonical bytes for an external user-managed detached signature.")
    signing_payload.add_argument("candidate_id")
    add_evolution_paths(signing_payload)
    verify_signature = release_candidate_commands.add_parser("verify-signature", help="Verify an external OpenSSH detached signature and record its receipt.")
    verify_signature.add_argument("candidate_id")
    verify_signature.add_argument("--signature", type=Path, required=True)
    verify_signature.add_argument("--allowed-signers", type=Path, required=True)
    verify_signature.add_argument("--principal", required=True)
    verify_signature.add_argument("--ssh-keygen", type=Path, required=True)
    verify_signature.add_argument("--confirm", action="store_true")
    add_evolution_paths(verify_signature)
    publish_release = release_candidate_commands.add_parser("publish-local", help="Publish a signature-verified candidate to the local registry; no tenant adoption occurs.")
    publish_release.add_argument("candidate_id")
    publish_release.add_argument("--confirm", action="store_true")
    add_evolution_paths(publish_release)
    registry_list = release_candidate_commands.add_parser("registry-list", help="List local registry releases without adopting them.")
    add_evolution_paths(registry_list)
    registry = evolution_commands.add_parser(
        "registry",
        help="Build or verify a portable read-only Blueprint registry bundle; it has no Capsule intake or worker execution.",
    )
    registry_commands = registry.add_subparsers(dest="evolution_registry_command", required=True)
    registry_build = registry_commands.add_parser(
        "build", help="Write a public, read-only bundle from signature-verified local registry releases."
    )
    registry_build.add_argument("destination", type=Path)
    registry_build.add_argument("--registry-id", required=True)
    registry_build.add_argument("--force", action="store_true")
    add_evolution_paths(registry_build)
    registry_inspect = registry_commands.add_parser(
        "inspect", help="Validate a local portable registry bundle without changing local state."
    )
    registry_inspect.add_argument("source", type=Path)
    add_evolution_paths(registry_inspect)
    registry_fetch = registry_commands.add_parser(
        "fetch", help="Fetch and validate a read-only registry bundle without credentials or local mutation."
    )
    registry_fetch.add_argument("url")
    registry_fetch.add_argument("--allow-insecure-loopback", action="store_true")
    add_evolution_paths(registry_fetch)
    registry_fetch_stage = registry_commands.add_parser(
        "fetch-stage", help="Fetch an HTTPS public registry bundle, verify its user-managed signature, and stage it locally without adoption."
    )
    registry_fetch_stage.add_argument("url")
    registry_fetch_stage.add_argument("--source-label", required=True)
    registry_fetch_stage.add_argument("--signature", type=Path, required=True)
    registry_fetch_stage.add_argument("--allowed-signers", type=Path, required=True)
    registry_fetch_stage.add_argument("--principal", required=True)
    registry_fetch_stage.add_argument("--ssh-keygen", type=Path, required=True)
    registry_fetch_stage.add_argument("--allow-insecure-loopback", action="store_true")
    registry_fetch_stage.add_argument("--confirm", action="store_true")
    add_evolution_paths(registry_fetch_stage)
    registry_payload = registry_commands.add_parser(
        "signing-payload", help="Print canonical bytes that an external registry operator signs."
    )
    registry_payload.add_argument("source", type=Path)
    add_evolution_paths(registry_payload)
    registry_stage = registry_commands.add_parser(
        "stage", help="Verify a bundle with a user-managed signer and stage it locally without adoption or execution."
    )
    registry_stage.add_argument("source", type=Path)
    registry_stage.add_argument("--source-label", required=True)
    registry_stage.add_argument("--signature", type=Path, required=True)
    registry_stage.add_argument("--allowed-signers", type=Path, required=True)
    registry_stage.add_argument("--principal", required=True)
    registry_stage.add_argument("--ssh-keygen", type=Path, required=True)
    registry_stage.add_argument("--confirm", action="store_true")
    add_evolution_paths(registry_stage)
    registry_staged_list = registry_commands.add_parser(
        "staged-list", help="List locally staged verified remote bundles; entries remain non-adoptable."
    )
    add_evolution_paths(registry_staged_list)
    registry_review = registry_commands.add_parser("review", help="Preview or resolve a staged remote registry snapshot; never adopts it.")
    registry_review_commands = registry_review.add_subparsers(dest="evolution_registry_review_command", required=True)
    for name in ("preview", "approve", "reject"):
        item = registry_review_commands.add_parser(name); item.add_argument("snapshot_id")
        if name != "preview": item.add_argument("--operator-id", required=True); item.add_argument("--reason", required=True); item.add_argument("--confirm", action="store_true")
        add_evolution_paths(item)
    registry_candidate = registry_commands.add_parser("tenant-candidate", help="Create or resolve a non-applied tenant candidate from a reviewed remote release.")
    candidate_commands = registry_candidate.add_subparsers(dest="evolution_registry_candidate_command", required=True)
    candidate_preview = candidate_commands.add_parser("preview"); candidate_preview.add_argument("tenant_id"); candidate_preview.add_argument("snapshot_id"); candidate_preview.add_argument("remote_release_id"); add_evolution_paths(candidate_preview)
    candidate_propose = candidate_commands.add_parser("propose"); candidate_propose.add_argument("tenant_id"); candidate_propose.add_argument("snapshot_id"); candidate_propose.add_argument("remote_release_id"); candidate_propose.add_argument("--operator-id", required=True); candidate_propose.add_argument("--reason", required=True); candidate_propose.add_argument("--confirm", action="store_true"); add_evolution_paths(candidate_propose)
    for name in ("approve", "reject"):
        item = candidate_commands.add_parser(name); item.add_argument("candidate_id"); item.add_argument("--operator-id", required=True); item.add_argument("--reason", required=True); item.add_argument("--confirm", action="store_true"); add_evolution_paths(item)
    registry_trust = registry_commands.add_parser("trust", help="Register, rotate, or revoke local trust roots for public registry signers.")
    registry_trust_commands = registry_trust.add_subparsers(dest="evolution_registry_trust_command", required=True)
    trust_register = registry_trust_commands.add_parser("register")
    trust_register.add_argument("--source-label", required=True); trust_register.add_argument("--allowed-signers", type=Path, required=True)
    trust_register.add_argument("--principal", required=True); trust_register.add_argument("--operator-id", required=True); trust_register.add_argument("--confirm", action="store_true"); add_evolution_paths(trust_register)
    trust_list = registry_trust_commands.add_parser("list"); add_evolution_paths(trust_list)
    for action in ("retire", "revoke"):
        trust_action = registry_trust_commands.add_parser(action)
        trust_action.add_argument("trust_root_id"); trust_action.add_argument("--operator-id", required=True); trust_action.add_argument("--confirm", action="store_true")
        if action == "revoke": trust_action.add_argument("--reason", required=True)
        add_evolution_paths(trust_action)
    tenant = evolution_commands.add_parser("tenant", help="Preview, adopt, or roll back tenant-local registry preferences without runtime mutation.")
    tenant_commands = tenant.add_subparsers(dest="evolution_tenant_command", required=True)
    tenant_preview = tenant_commands.add_parser("preview")
    tenant_preview.add_argument("tenant_id"); tenant_preview.add_argument("release_id"); add_evolution_paths(tenant_preview)
    tenant_adopt = tenant_commands.add_parser("adopt")
    tenant_adopt.add_argument("tenant_id"); tenant_adopt.add_argument("release_id"); tenant_adopt.add_argument("--confirm", action="store_true"); add_evolution_paths(tenant_adopt)
    tenant_rollback = tenant_commands.add_parser("rollback")
    tenant_rollback.add_argument("tenant_id"); tenant_rollback.add_argument("role"); tenant_rollback.add_argument("--confirm", action="store_true"); add_evolution_paths(tenant_rollback)
    network = evolution_commands.add_parser("network", help="Use explicit hosted Capsule intake or inspect the still-disabled remote-worker gate.")
    network_commands = network.add_subparsers(dest="evolution_network_command", required=True)
    network_status = network_commands.add_parser("status")
    network_status.add_argument("--json", action="store_true")
    network_probe = network_commands.add_parser(
        "probe",
        help="Read public Worker health and Artifact registry index without a token, consent, or local write.",
    )
    network_probe.add_argument("--endpoint", required=True)
    network_probe.add_argument("--allow-insecure-loopback", action="store_true")
    network_probe.add_argument("--json", action="store_true")
    network_preview = network_commands.add_parser("worker-preview")
    network_preview.add_argument("tenant_id"); network_preview.add_argument("release_id"); network_preview.add_argument("--json", action="store_true")
    capability_preview = network_commands.add_parser("capability-preview")
    capability_preview.add_argument("tenant_id"); capability_preview.add_argument("release_id"); capability_preview.add_argument("job_id")
    capability_preview.add_argument("--capability", action="append", required=True); capability_preview.add_argument("--json", action="store_true")
    authorization_preview = network_commands.add_parser("authorization-preview")
    authorization_preview.add_argument("--json", action="store_true")
    operator_enrollment_preview = network_commands.add_parser(
        "operator-enrollment-preview",
        help="Create one merge-only Worker role allowlist fragment without exposing or writing its token.",
    )
    operator_enrollment_preview.add_argument("role", choices=("contributor", "finalizer", "reviewer", "publisher"))
    operator_enrollment_preview.add_argument("--token-env", required=True)
    operator_enrollment_preview.add_argument("--identity")
    operator_enrollment_preview.add_argument(
        "--authority", choices=("INDIVIDUAL", "ORGANIZATION_OWNER"), default="INDIVIDUAL"
    )
    operator_enrollment_preview.add_argument("--json", action="store_true")
    hosted_submit = network_commands.add_parser(
        "submit",
        help="Transmit one already-consented minimized Capsule over HTTPS; no background sync, employee execution, or workspace upload.",
    )
    hosted_submit.add_argument("capsule_id")
    hosted_submit.add_argument("--endpoint", required=True)
    hosted_submit.add_argument("--token-env", default="NORUCT_EVOLUTION_ACCESS_TOKEN")
    hosted_submit.add_argument(
        "--withdrawal-capability-env",
        default="NORUCT_EVOLUTION_WITHDRAWAL_CAPABILITY",
        help="Environment variable containing a caller-generated 64-hex pending-withdrawal capability.",
    )
    hosted_submit.add_argument("--allow-insecure-loopback", action="store_true")
    hosted_submit.add_argument("--confirm", action="store_true")
    add_evolution_paths(hosted_submit)
    hosted_withdraw = network_commands.add_parser(
        "withdraw",
        help="Withdraw one hosted Capsule before deleting its local payload; token is read only from the named environment variable.",
    )
    hosted_withdraw.add_argument("capsule_id")
    hosted_withdraw.add_argument("--endpoint", required=True)
    hosted_withdraw.add_argument("--token-env", default="NORUCT_EVOLUTION_ACCESS_TOKEN")
    hosted_withdraw.add_argument("--withdrawal-capability-env", default="NORUCT_EVOLUTION_WITHDRAWAL_CAPABILITY")
    hosted_withdraw.add_argument("--allow-insecure-loopback", action="store_true")
    hosted_withdraw.add_argument("--confirm", action="store_true")
    add_evolution_paths(hosted_withdraw)
    operator_candidates = network_commands.add_parser(
        "candidates",
        help="List bounded remote Candidate summaries through the finalizer-only operator API.",
    )
    operator_candidates.add_argument("--endpoint", required=True)
    operator_candidates.add_argument("--token-env", default="NORUCT_EVOLUTION_FINALIZER_TOKEN")
    operator_candidates.add_argument("--limit", type=int, default=25)
    operator_candidates.add_argument("--cursor")
    operator_candidates.add_argument("--allow-insecure-loopback", action="store_true")
    operator_candidates.add_argument("--confirm", action="store_true")
    add_evolution_paths(operator_candidates)
    operator_evaluate = network_commands.add_parser(
        "evaluate-candidate",
        help="Submit one bounded public/synthetic Candidate evaluation; it can only reach operator review, never publish a release.",
    )
    operator_evaluate.add_argument("candidate_id")
    operator_evaluate.add_argument("evaluation", type=Path)
    operator_evaluate.add_argument("--endpoint", required=True)
    operator_evaluate.add_argument("--token-env", default="NORUCT_EVOLUTION_FINALIZER_TOKEN")
    operator_evaluate.add_argument("--allow-insecure-loopback", action="store_true")
    operator_evaluate.add_argument("--confirm", action="store_true")
    add_evolution_paths(operator_evaluate)
    operator_expire = network_commands.add_parser(
        "expire-pending",
        help="Purge up to 100 expired pending Capsules and retain only content-free expiry receipts.",
    )
    operator_expire.add_argument("--endpoint", required=True)
    operator_expire.add_argument("--token-env", default="NORUCT_EVOLUTION_FINALIZER_TOKEN")
    operator_expire.add_argument("--allow-insecure-loopback", action="store_true")
    operator_expire.add_argument("--confirm", action="store_true")
    add_evolution_paths(operator_expire)
    operator_finalize = network_commands.add_parser(
        "finalize-capsule",
        help="Purge one pending raw Capsule into a bounded finalized signal; this never publishes a release.",
    )
    operator_finalize.add_argument("contribution_id")
    operator_finalize.add_argument("--endpoint", required=True)
    operator_finalize.add_argument("--token-env", default="NORUCT_EVOLUTION_FINALIZER_TOKEN")
    operator_finalize.add_argument("--allow-insecure-loopback", action="store_true")
    operator_finalize.add_argument("--confirm", action="store_true")
    add_evolution_paths(operator_finalize)
    operator_assemble = network_commands.add_parser(
        "assemble-candidates",
        help="Deterministically aggregate finalized typed Proposals into evaluation-ready Candidate records.",
    )
    operator_assemble.add_argument("--endpoint", required=True)
    operator_assemble.add_argument("--token-env", default="NORUCT_EVOLUTION_FINALIZER_TOKEN")
    operator_assemble.add_argument("--allow-insecure-loopback", action="store_true")
    operator_assemble.add_argument("--confirm", action="store_true")
    add_evolution_paths(operator_assemble)
    operator_authorize = network_commands.add_parser(
        "authorize-artifact-registry",
        help="Authorize one exact signed-registry digest from accepted Candidate and evaluation evidence; this cannot publish it.",
    )
    operator_authorize.add_argument("registry_id")
    operator_authorize.add_argument("bundle", type=Path)
    operator_authorize.add_argument("--candidate-evidence-digest", action="append", required=True)
    operator_authorize.add_argument("--evaluation-evidence-digest", action="append", required=True)
    operator_authorize.add_argument("--reviewer-id", required=True)
    operator_authorize.add_argument("--reason-code", required=True)
    operator_authorize.add_argument("--endpoint", required=True)
    operator_authorize.add_argument("--token-env", default="NORUCT_EVOLUTION_REVIEWER_TOKEN")
    operator_authorize.add_argument("--allow-insecure-loopback", action="store_true")
    operator_authorize.add_argument("--confirm", action="store_true")
    add_evolution_paths(operator_authorize)
    operator_publish = network_commands.add_parser(
        "publish-artifact-registry",
        help="Verify then publish a detached-signed Artifact registry bundle by consuming one exact reviewer authorization.",
    )
    operator_publish.add_argument("registry_id")
    operator_publish.add_argument("bundle", type=Path)
    operator_publish.add_argument("--authorization-id", required=True)
    operator_publish.add_argument("--signature", type=Path, required=True)
    operator_publish.add_argument("--allowed-signers", type=Path, required=True)
    operator_publish.add_argument("--principal", required=True)
    operator_publish.add_argument("--ssh-keygen", type=Path, required=True)
    operator_publish.add_argument("--endpoint", required=True)
    operator_publish.add_argument("--token-env", default="NORUCT_EVOLUTION_PUBLISHER_TOKEN")
    operator_publish.add_argument("--allow-insecure-loopback", action="store_true")
    operator_publish.add_argument("--confirm", action="store_true")
    add_evolution_paths(operator_publish)
    operator_retire = network_commands.add_parser(
        "retire-artifact-registry",
        help="Retire one public Artifact registry bundle through the separated publisher role.",
    )
    operator_retire.add_argument("registry_id")
    operator_retire.add_argument("--reason-code", required=True)
    operator_retire.add_argument("--endpoint", required=True)
    operator_retire.add_argument("--token-env", default="NORUCT_EVOLUTION_PUBLISHER_TOKEN")
    operator_retire.add_argument("--allow-insecure-loopback", action="store_true")
    operator_retire.add_argument("--confirm", action="store_true")
    add_evolution_paths(operator_retire)

    # Noruct Network is the product-level template distribution plane.  The
    # older `evolution` command remains the compatibility surface for consented
    # Shared Evolution intake; it is one first-party publisher, not the
    # generic Network authority.
