"""Explicitly confirmed, source-backed local Skill mutations."""

from __future__ import annotations

import argparse
import json
import os
import uuid
from pathlib import Path
from typing import TextIO

from dynamic_firm.evolution import EvolutionNetworkService, EvolutionStore
from dynamic_firm.evolution.managed_skill_package import build_managed_skill_artifact
from dynamic_firm.foundation.hermes_skill_guard import audit_user_skill
from dynamic_firm.runtime.models import to_primitive


def _read_bounded(path: Path | None, label: str, maximum: int) -> str | None:
    if path is None:
        return None
    try:
        value = path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError(f"{label} must be UTF-8 text") from exc
    if len(value.encode("utf-8")) > maximum:
        raise ValueError(f"{label} exceeds the byte limit")
    return value


def run_managed_skills_command(
    args: argparse.Namespace,
    output: TextIO,
    *,
    evolution_state_path: Path,
    exit_ok: int,
    exit_runtime: int,
) -> int:
    """Run the confirmed mutation branch without owning global CLI config."""

    if args.skills_command == "import":
        from dynamic_firm.foundation.hermes_skill_manager import (
            import_local_skill,
            rollback_local_skill_import,
        )

        if not args.confirm:
            raise ValueError("Managed skill import and rollback require --confirm")
        if args.skill_import_command == "local":
            receipt = import_local_skill(
                skills_root=args.skills_root,
                source_dir=args.source_dir,
                name=str(args.name),
                replace=bool(args.replace),
            )
            receipt_path = args.receipt_out.expanduser().resolve()
            if receipt_path.exists() and receipt_path.is_symlink():
                raise ValueError("Managed skill import receipt path cannot be a symbolic link")
            receipt_path.parent.mkdir(parents=True, exist_ok=True)
            temporary = receipt_path.with_name(
                f".{receipt_path.name}.{uuid.uuid4().hex}.tmp"
            )
            try:
                temporary.write_text(
                    json.dumps(receipt, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
                    encoding="utf-8",
                )
                os.chmod(temporary, 0o600)
                os.replace(temporary, receipt_path)
            finally:
                try:
                    temporary.unlink()
                except FileNotFoundError:
                    pass
            record = {
                **receipt,
                "receipt_file": str(receipt_path),
                "authority": "explicit_local_source_copy_static_scan_atomic_replace_and_receipt_bound_rollback",
            }
        else:
            try:
                raw_receipt = args.receipt_file.expanduser().resolve().read_text(
                    encoding="utf-8"
                )
                receipt = json.loads(raw_receipt)
            except (OSError, json.JSONDecodeError) as exc:
                raise ValueError("Managed skill import rollback receipt must be valid JSON") from exc
            record = {
                **rollback_local_skill_import(
                    skills_root=args.skills_root,
                    receipt=receipt,
                ),
                "authority": "explicit_receipt_bound_rollback_no_source_download_or_execution",
            }
        if args.json:
            print(json.dumps(record, ensure_ascii=False, sort_keys=True), file=output)
        else:
            print(
                f"Managed skill import · {record['name']} · "
                f"{record.get('status', 'imported')} · receipt-bound",
                file=output,
            )
        return exit_ok

    if args.skills_command == "manage":
        from dynamic_firm.foundation.hermes_skill_manager import manage_local_skill

        if not args.confirm:
            raise ValueError("Managed skill mutations require --confirm")
        result = manage_local_skill(
            skills_root=args.skills_root,
            action=str(args.action),
            name=str(args.name),
            content=_read_bounded(args.content_file, "Skill content", 100_000),
            category=args.category,
            file_path=args.file_path,
            file_content=_read_bounded(
                args.file_content_file,
                "Supporting file content",
                1_048_576,
            ),
            old_string=args.old_text,
            new_string=args.new_text,
            replace_all=bool(args.replace_all),
            absorbed_into=args.absorbed_into,
        )
        source = result["source"]
        if args.json:
            print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2), file=output)
        elif source.get("success"):
            print(
                f"Managed skill {result['action']} · {result['name']} · "
                f"changed={'yes' if result['changed'] else 'no'} · "
                f"receipt={result['after']['tree_sha256'][:16]}",
                file=output,
            )
            print(str(source.get("message", "Completed")), file=output)
        else:
            print(
                f"Managed skill rejected · {source.get('error', 'unknown source error')}",
                file=output,
            )
            return exit_runtime
        return exit_ok

    if args.skills_command != "package":
        raise ValueError(f"unknown managed skills command: {args.skills_command}")
    from dynamic_firm.foundation.hermes_skill_manager import managed_skill_receipt

    source = managed_skill_receipt(skills_root=args.skills_root, name=str(args.name))
    audit = audit_user_skill(Path(str(source["skill_dir"])))
    if audit.get("verdict") == "dangerous":
        raise ValueError(
            "Managed skill package is blocked because the registered static scanner marked it dangerous"
        )
    manifest = build_managed_skill_artifact(
        artifact_id=str(args.artifact_id),
        version=str(args.version),
        skill_key=str(args.skill_key),
        applies_to=tuple(args.applies_to),
        steps=tuple(args.step),
        required_capabilities=tuple(args.required_capability),
        receipt=source["receipt"],
    )
    with EvolutionStore(evolution_state_path) as store:
        service = EvolutionNetworkService(store)
        base_record = {
            "source_skill": source["name"],
            "source_receipt": source["receipt"],
            "static_audit": {
                "scanner_revision": audit.get("scanner_revision"),
                "content_hash": audit.get("content_hash"),
                "verdict": audit.get("verdict"),
            },
            "authority": (
                "explicit_reviewed_semantic_steps_bound_to_local_skill_receipt_"
                "no_raw_skill_copy_or_execution"
            ),
        }
        if args.skill_package_command == "preview":
            record = {**base_record, **service.preview_artifact_manifest(manifest)}
        else:
            if not args.confirm:
                raise ValueError("Managed skill package registration requires --confirm")
            record = {
                **base_record,
                "artifact": service.register_artifact_manifest(
                    manifest, ingress="MANAGED_SKILL_REGISTRATION"
                ),
                "next_actions": (
                    "noruct evolution artifact stage <artifact-id> <version> --confirm",
                    "noruct evolution artifact install <artifact-id> <version> --confirm",
                    "noruct evolution artifact activate <scope> <artifact-id> <version> --confirm",
                ),
            }
    if args.json:
        print(json.dumps(to_primitive(record), ensure_ascii=False, sort_keys=True), file=output)
    else:
        action = "registered" if args.skill_package_command == "register" else "previewed"
        print(
            f"Managed skill package {action} · scanner={record['static_audit']['verdict']} · "
            "no raw instruction was added to Company memory",
            file=output,
        )
    return exit_ok
