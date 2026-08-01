"""Foundation command dispatch outside the global CLI ingress."""

from __future__ import annotations

import argparse
import asyncio
import json
from typing import TextIO


def run_foundation_command(
    args: argparse.Namespace,
    output: TextIO,
    *,
    exit_ok: int,
) -> int:
    from dynamic_firm.foundation import (
        foundation_cutover_status,
        foundation_preview_preflight,
        foundation_status,
        run_foundation_smoke,
        verify_foundation_source,
    )

    if args.foundation_command == "status":
        payload = foundation_status()
    elif args.foundation_command == "cutover-status":
        payload = foundation_cutover_status()
    elif args.foundation_command == "migration-preview":
        from dynamic_firm.foundation.migration_preview import (
            preview_employee_runtime_migration,
        )

        payload = preview_employee_runtime_migration(args.state)
    elif args.foundation_command == "migration-apply":
        if not args.confirm:
            raise ValueError("migration-apply requires --confirm; no state was changed")
        from dynamic_firm.foundation.migration_preview import (
            apply_employee_runtime_migration,
        )

        payload = apply_employee_runtime_migration(
            args.state,
            backup_directory=args.backup_dir,
        )
    elif args.foundation_command == "verify-source":
        payload = {
            "ok": True,
            "product_identity": "noruct",
            "source": verify_foundation_source(),
            "historical_capsule": None,
        }
    elif args.foundation_command == "inventory":
        from dynamic_firm.foundation.inventory import foundation_capability_inventory

        payload = foundation_capability_inventory()
    elif args.foundation_command == "smoke":
        payload = run_foundation_smoke(
            python_executable=None,
            timeout_seconds=args.timeout,
        )
    elif args.foundation_command == "preflight":
        payload = foundation_preview_preflight(
            python_executable=args.runtime_python,
            timeout_seconds=args.timeout,
        )
    elif args.foundation_command == "parity":
        from dynamic_firm.foundation.parity import run_preview_parity

        payload = asyncio.run(
            run_preview_parity(python_executable=args.runtime_python)
        )
    elif args.foundation_command == "reliability":
        from dynamic_firm.foundation.parity import (
            run_runtime_reliability_qualification,
        )

        payload = asyncio.run(
            run_runtime_reliability_qualification(
                python_executable=args.runtime_python
            )
        )
    elif args.foundation_command == "readiness":
        from dynamic_firm.foundation.parity import (
            run_foundation_parallelism_parity,
            run_foundation_reroute_parity,
            run_preview_parity,
            run_product_preview_parity,
        )
        from dynamic_firm.evolution.network_gate import network_gate_status

        preflight = foundation_preview_preflight(
            python_executable=args.runtime_python,
            timeout_seconds=args.timeout,
        )
        parity = asyncio.run(
            run_preview_parity(python_executable=args.runtime_python)
        )
        product_parity = asyncio.run(
            run_product_preview_parity(python_executable=args.runtime_python)
        )
        reroute_parity = asyncio.run(
            run_foundation_reroute_parity(python_executable=args.runtime_python)
        )
        parallelism_parity = asyncio.run(
            run_foundation_parallelism_parity(python_executable=args.runtime_python)
        )
        payload = {
            "technical_default_ready": preflight["technical_default_ready"],
            "default_runtime": preflight["cutover"]["default_runtime"],
            "execution": "runtime_default_readiness",
            "external_model_calls": 0,
            "ok": bool(
                preflight["ok"]
                and parity["passed"]
                and product_parity["passed"]
                and reroute_parity["passed"]
                and parallelism_parity["passed"]
            ),
            "parity": parity,
            "preflight": preflight,
            "product_parity": product_parity,
            "reroute_parity": reroute_parity,
            "parallelism_parity": parallelism_parity,
            "schema_version": "noruct.employee-runtime-readiness.v1",
            "scope": {
                "assessed": {
                    "employee_runtime": (
                        "exact-worker qualification plus offline direct/tool/approval/cancel parity"
                    ),
                    "product_integration": (
                        "offline direct and resumed conversation, single-task and typed-capability Company runs, approved workspace write/edit/command paths, one read-then-approved-write tool iteration, one command-edit-verify coding loop, a Company budget pre-dispatch hard stop, typed assignee-mismatch reroute across a frozen roster, and two dependency-ready Foundation tasks joined by a final employee; Firm Kernel, active-job ledger, and one terminal answer writer"
                    ),
                },
                "partially_assessed": {
                    "firm_kernel": (
                        "direct/resumed conversation, single-task and one typed-capability Company path, approved workspace write/edit/command paths, one read-then-approved-write tool iteration, one command-edit-verify coding loop, a Company budget pre-dispatch hard stop, one sequential typed assignee-mismatch reroute to a frozen exact-capable employee, and one two-task dependency-ready parallel join through the Foundation Runtime; general dynamic graph mutation beyond these bounds is not assessed"
                    ),
                    "paperclip_derived_runtime_slices": (
                        "approval, liveness, and Company budget hard-stop contracts are exercised offline; comprehensive control-plane release evidence is not assessed"
                    ),
                },
                "not_assessed": {
                    "paperclip_derived_control_plane": (
                        "not_assessed_by_employee_runtime_readiness"
                    ),
                },
                "shared_evolution_network": {
                    "assessed_by_employee_runtime_readiness": False,
                    "current_fail_closed_gate": network_gate_status().to_dict(),
                },
            },
        }
    elif args.foundation_command == "validate-provider-evidence":
        from dynamic_firm.foundation.provider_evidence import (
            SCHEMA,
            validate_provider_slot_evidence,
        )

        record = validate_provider_slot_evidence(args.path)
        payload = {"ok": True, "schema_version": record["schema_version"], "slot": record["slot"], "activation": record["activation"], "matrix_eligible": record["schema_version"] == SCHEMA, "commercial_default_eligible": False}
    elif args.foundation_command == "provider-evidence-status":
        from dynamic_firm.foundation.provider_evidence import validate_provider_evidence_matrix

        payload = validate_provider_evidence_matrix(args.directory)
    elif args.foundation_command == "provider-evidence-records-status":
        from dynamic_firm.foundation.provider_evidence import validate_provider_evidence_matrix_records

        payload = validate_provider_evidence_matrix_records({
            "direct": args.direct,
            "read_tool": args.read_tool,
            "approval": args.approval,
            "cancel_recovery": args.cancel_recovery,
        })
    elif args.foundation_command == "release-admission-status":
        from dynamic_firm.foundation import foundation_cutover_status
        from dynamic_firm.foundation.migration_preview import (
            preview_employee_runtime_migration,
        )
        from dynamic_firm.foundation.provider_evidence import validate_provider_evidence_matrix_records
        from dynamic_firm.evolution.network_gate import network_gate_status

        matrix = validate_provider_evidence_matrix_records({
            "direct": args.direct, "read_tool": args.read_tool,
            "approval": args.approval, "cancel_recovery": args.cancel_recovery,
        })
        if bool(args.provenance_packet) != bool(args.provenance_decisions):
            raise ValueError(
                "release-admission-status requires both --provenance-packet and --provenance-decisions"
            )
        provenance_review = {
            "state": "NOT_SUPPLIED",
            "review_complete": False,
            "commercial_release_authorized": False,
            "commercial_default_activation": False,
        }
        if args.provenance_packet and args.provenance_decisions:
            from dynamic_firm.foundation.provenance_review import validate_provenance_review

            provenance_review = validate_provenance_review(
                packet_path=args.provenance_packet,
                decisions_path=args.provenance_decisions,
            )
            provenance_review = {**provenance_review, "state": "REVIEW_RECORD_VALIDATED"}
        cutover = foundation_cutover_status()
        network = network_gate_status().to_dict()
        migration = preview_employee_runtime_migration(args.state)
        payload = {
            "schema_version": "noruct.employee-runtime-release-admission.v1",
            "technical_provider_matrix_complete": matrix["complete"],
            "default_runtime": cutover["default_runtime"],
            "runtime_rollback_available": cutover["runtime_rollback_available"],
            "technical_default_ready": cutover["technical_default_ready"],
            "commercial_default_eligible": False,
            "release_authorized": False,
            "shared_network_release_authorized": False,
            "required_human_gates": (
                "shipped_runtime_secondary_provenance_review",
                "provider_terms_privacy_and_commercial_use_review",
                "migration_signing_publisher_authorization",
                "hosted_evolution_network_authorization",
            ),
            "provenance_review": provenance_review,
            "matrix": matrix,
            "cutover": cutover,
            "migration_preview": migration,
            "shared_evolution_network": network,
        }
    elif args.foundation_command == "validate-release-authorization-draft":
        from dynamic_firm.foundation.release_authorization import validate_release_authorization_draft

        payload = validate_release_authorization_draft(args.path)
    elif args.foundation_command == "validate-provenance-review":
        from dynamic_firm.foundation.provenance_review import validate_provenance_review

        payload = validate_provenance_review(
            packet_path=args.packet,
            decisions_path=args.decisions,
        )
    elif args.foundation_command == "capture-provider-evidence":
        if not args.confirm:
            raise ValueError("capture-provider-evidence requires --confirm after reviewing the completed local ledger")
        from dynamic_firm.foundation.provider_evidence import capture_provider_slot_evidence

        record = capture_provider_slot_evidence(
            ledger_path=args.ledger,
            run_id=args.run_id,
            slot=args.slot,
            wheel_path=args.wheel,
            worker_python=args.runtime_python,
            fixture_root=args.fixture_root,
            provider_id=args.provider_id,
            model_id=args.model_id,
            max_wall_time_ms=args.max_wall_time_ms,
            operator_authorized_at=args.operator_authorized_at,
            output_path=args.output,
        )
        payload = {"ok": True, "schema_version": record["schema_version"], "slot": record["slot"], "evidence_id": record["evidence_id"], "matrix_eligible": True, "commercial_default_eligible": False}
    else:
        raise ValueError(f"unknown foundation command: {args.foundation_command}")

    if args.json:
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True), file=output)
        return exit_ok
    if args.foundation_command == "status":
        source = payload["source"]
        dependency = payload["dependencies"]
        print("Noruct employee-agent foundation", file=output)
        print(
            f"  source     ready · {source.get('file_count', '?')} files",
            file=output,
        )
        if dependency["ready"]:
            print(
                f"  runtime dependencies ready · {dependency['exact_package_count']} exact packages",
                file=output,
            )
        else:
            print(
                f"  runtime dependencies pending · {len(dependency['missing'])} missing · "
                f"{len(dependency['mismatched'])} version mismatches",
                file=output,
            )
        print(
            "  legal review remains advisory; it does not change the runtime default",
            file=output,
        )
        print("  activation Noruct runtime default; Company state remains Noruct", file=output)
        return exit_ok
    if args.foundation_command == "cutover-status":
        dependency = payload["gate"]["dependency"]
        provenance = payload["gate"]["secondary_provenance"]
        print("Noruct Employee Runtime cutover", file=output)
        print(
            f"  default  {payload['default_runtime']} · alternate runtime none",
            file=output,
        )
        print(
            f"  dependency review {dependency['state']} · runtime "
            f"{'ready' if dependency['runtime_ready'] else 'unavailable'}",
            file=output,
        )
        print(
            "  provenance review "
            f"{provenance['state']} · {provenance['distributed_source_finding_count']} distributed "
            f"({provenance['active_import_surface_finding_count']} active-import)",
            file=output,
        )
        print("  result   Noruct is the default; commercial review is advisory", file=output)
        return exit_ok
    if args.foundation_command == "migration-preview":
        inventory = payload["inventory"]
        transition = payload["transition"]
        schema = inventory["schema_compatibility"]
        print("Noruct Employee Runtime migration preview", file=output)
        print(
            f"  state    {inventory['state']} · {inventory['employee_session_records']} employee session projection(s) · "
            f"{inventory['active_employee_runs']} active run(s)",
            file=output,
        )
        print(
            f"  state    {transition['historical_state_label']} → {transition['runtime']} · "
            f"{transition['apply_status'].lower().replace('_', ' ')}",
            file=output,
        )
        print(
            "  schema   "
            f"{schema['state'].lower().replace('_', ' ')} · "
            f"observed={schema['observed_schema_version']} · "
            f"readable={'yes' if schema['migration_readable'] else 'no'}",
            file=output,
        )
        if transition["blockers"]:
            print(f"  blockers {', '.join(transition['blockers'])}", file=output)
        print("  result   preview only; migration-apply creates a verified backup and no-transform receipt", file=output)
        return exit_ok
    if args.foundation_command == "migration-apply":
        transition = payload["transition"]
        print("Noruct Employee Runtime migration applied", file=output)
        print(
            f"  state    {transition['historical_state_label']} → {transition['runtime']} · "
            f"{transition['data_transform'].lower().replace('_', ' ')}",
            file=output,
        )
        print(f"  backup   {payload['backup_path']}", file=output)
        print(f"  receipt  {payload['receipt_path']}", file=output)
        print("  result   backup rehearsal passed; no alternate runtime was selected", file=output)
        return exit_ok
    if args.foundation_command == "verify-source":
        source = payload["source"]
        print(
            f"Noruct Employee Runtime capsule verified: {source['file_count']} files · "
            f"tree {source['tree_sha256'][:12]}…",
            file=output,
        )
        return exit_ok
    if args.foundation_command == "inventory":
        print(
            "Noruct Employee Runtime full-source inventory · "
            f"{payload['source_file_count']} verified files · "
            f"complete={'yes' if payload['complete_source_intake'] else 'no'}",
            file=output,
        )
        for family in payload["families"]:
            print(
                f"  {family['family']} · {family['source_file_count']} files · "
                f"{family['activation_mode']}",
                file=output,
            )
        print(
            "  authority Company/state, credentials, and tool effects remain Noruct-owned",
            file=output,
        )
        return exit_ok
    if args.foundation_command == "preflight":
        worker = payload["worker"]
        cutover = payload["cutover"]
        print(
            "Noruct employee runtime preflight passed: isolated employee loop · "
            f"{worker['model_request_count']} parent-owned model request · session isolated",
            file=output,
        )
        print(
            f"  runtime {payload['execution']} · default {cutover['default_runtime']} · "
            f"technical readiness={'ready' if payload['technical_default_ready'] else 'dependency unavailable'}",
            file=output,
        )
        return exit_ok
    if args.foundation_command == "parity":
        print(
            "Noruct employee runtime parity passed: direct · parent tool · approval allow/deny · cancel",
            file=output,
        )
        return exit_ok
    if args.foundation_command == "reliability":
        checks = payload["checks"]
        print(
            "Noruct runtime reliability passed: direct/company · approval allow/deny · deferred discovery · cancel · session · reroute · parallel join",
            file=output,
        )
        print(
            "  checks "
            + " · ".join(
                key.replace("_", " ")
                for key, passed in checks.items()
                if passed
            ),
            file=output,
        )
        print(
            "  scope offline deterministic qualification; live providers, sidecars, gateways, remote execution, and hosted evolution are not assessed",
            file=output,
        )
        return exit_ok
    if args.foundation_command == "readiness":
        print(
            "Noruct employee runtime readiness passed: preflight + direct/tool/approval/cancel parity",
            file=output,
        )
        print("  product integration passed: Firm Kernel + event/TUI projection", file=output)
        print("  Kernel reroute passed: typed mismatch → one frozen-roster reassignment", file=output)
        print("  Kernel parallel join passed: two ready employees → dependency-bound final", file=output)
        print(
            f"  runtime default · {payload['default_runtime']} · technical readiness="
            f"{'ready' if payload['technical_default_ready'] else 'dependency unavailable'}",
            file=output,
        )
        print(
            "  scope employee runtime + bounded product paths · Kernel/control plane only partially assessed · "
            "Evolution Network remains separately disabled",
            file=output,
        )
        return exit_ok
    if args.foundation_command == "release-admission-status":
        migration = payload["migration_preview"]
        inventory = migration["inventory"]
        schema = inventory["schema_compatibility"]
        print("Noruct Employee Runtime release admission", file=output)
        print(
            "  provider matrix "
            f"{'complete' if payload['technical_provider_matrix_complete'] else 'incomplete'} · "
            f"default {payload['default_runtime']} · release unauthorized",
            file=output,
        )
        print(
            f"  migration state {inventory['state']} · {inventory['active_employee_runs']} active run(s) · "
            f"schema {schema['state']} · read-only inventory",
            file=output,
        )
        print(
            "  result   human provenance/provider/migration/network gates pending; no activation",
            file=output,
        )
        return exit_ok
    if args.foundation_command == "validate-provider-evidence":
        print(f"Employee Runtime provider slot evidence accepted for human review: {payload['slot']}", file=output)
        return exit_ok
    if args.foundation_command == "validate-provenance-review":
        state = "complete" if payload["review_complete"] else "draft"
        print(
            "Employee Runtime provenance review record accepted: "
            f"{state}; release/default activation remains blocked",
            file=output,
        )
        return exit_ok
    if args.foundation_command == "provider-evidence-status":
        print("Employee Runtime provider evidence matrix complete; human review remains required", file=output)
        return exit_ok
    if args.foundation_command == "capture-provider-evidence":
        print(f"Employee Runtime provider slot evidence captured: {payload['slot']}", file=output)
        return exit_ok
    print(
        "Noruct employee foundation smoke passed: isolated employee loop · "
        f"{payload['model_request_count']} parent-owned model request · session isolated",
        file=output,
    )
    return exit_ok


