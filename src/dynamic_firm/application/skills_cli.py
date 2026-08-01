"""Parsed Skill command adapter, separated from the global CLI ingress."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Callable, Mapping, TextIO

from dynamic_firm._vendor.runtime_safety.redact import redact_terminal_output
from dynamic_firm.application.managed_skills_cli import run_managed_skills_command
from dynamic_firm.product.external_skill_settings import (
    remove_external_skill_settings,
    write_external_skill_settings,
)
from dynamic_firm.product.external_skills import (
    discover_external_skills,
    external_skill_directories,
    select_external_skills,
)
from dynamic_firm.foundation.hermes_skill_guard import audit_user_skill


def _skill_primitive(item: object) -> dict[str, object]:
    """Render catalog metadata; instruction text needs explicit inspection."""

    return {
        "name": item.name,  # type: ignore[attr-defined]
        "description": item.description,  # type: ignore[attr-defined]
        "platforms": list(item.platforms),  # type: ignore[attr-defined]
        "root": str(item.root),  # type: ignore[attr-defined]
        "relative_path": item.relative_path,  # type: ignore[attr-defined]
        "content_id": item.snapshot.content_id,  # type: ignore[attr-defined]
        "revision": item.snapshot.revision,  # type: ignore[attr-defined]
        "content_hash": item.snapshot.content_hash,  # type: ignore[attr-defined]
    }


def run_skills_command(
    args: argparse.Namespace,
    settings: Mapping[str, object],
    output: TextIO,
    *,
    state_path_for: Callable[[argparse.Namespace, Mapping[str, object]], Path],
    exit_ok: int,
    exit_runtime: int,
) -> int:
    """Dispatch Skill catalog reads and confirmed local managed-Skill actions."""

    if args.skills_command == "connect":
        target = write_external_skill_settings(args.config, tuple(args.directory))
        catalog = discover_external_skills(external_skill_directories(tuple(args.directory)))
        record = {
            "configuration_changed": True,
            "config_path": str(target),
            "roots": [str(item) for item in catalog.roots],
            "discovered_count": len(catalog.skills),
            "skipped_count": catalog.skipped_count,
            "authority": "user_configured_read_only_skill_discovery_no_copy_or_execution",
        }
        if args.json:
            print(json.dumps(record, ensure_ascii=False, sort_keys=True), file=output)
        else:
            print(
                f"External skill roots connected · {len(catalog.roots)} root(s) · "
                f"{len(catalog.skills)} compatible instruction(s)",
                file=output,
            )
            print(
                "Future Jobs may read selected SKILL.md instructions; scripts never become execution authority.",
                file=output,
            )
        return exit_ok

    if args.skills_command == "disconnect":
        changed = remove_external_skill_settings(args.config)
        record = {
            "configuration_changed": changed,
            "config_path": str(args.config.expanduser().resolve()),
            "authority": "external_skill_discovery_disconnected_user_files_untouched",
        }
        if args.json:
            print(json.dumps(record, ensure_ascii=False, sort_keys=True), file=output)
        else:
            print("External skill roots disconnected; no user-owned skill files were changed.", file=output)
        return exit_ok

    if args.skills_command in {"import", "manage", "package"}:
        state_path = state_path_for(args, settings)
        return run_managed_skills_command(
            args,
            output,
            evolution_state_path=state_path.with_name(
                f"{state_path.stem}.evolution.db"
            ),
            exit_ok=exit_ok,
            exit_runtime=exit_runtime,
        )

    skill_settings = settings.get("skills")
    configured_directories = (
        skill_settings.get("external_dirs")
        if isinstance(skill_settings, Mapping)
        else None
    )
    directories = external_skill_directories(
        args.skills_dir if args.skills_dir is not None else configured_directories
    )
    catalog = discover_external_skills(directories)
    base: dict[str, object] = {
        "mode": "user_configured_read_only_job_local",
        "roots": [str(item) for item in catalog.roots],
        "discovered_count": len(catalog.skills),
        "skipped_count": catalog.skipped_count,
        "skills": [_skill_primitive(item) for item in catalog.skills],
        "execution": "SKILL.md instructions only; linked files and executable content are not loaded",
    }
    if args.skills_command in {"inspect", "audit"}:
        selected = next((item for item in catalog.skills if item.name == args.name), None)
        if selected is None:
            raise ValueError(
                f"Compatible external skill was not found: {args.name}. "
                "Run `noruct skills list` to inspect the configured roots."
            )
        record = {**base, "selected": _skill_primitive(selected)}
        if args.skills_command == "inspect":
            record["instruction"] = redact_terminal_output(
                selected.snapshot.content,
                force=True,
            )
        else:
            target = selected.root / Path(selected.relative_path).parent
            record["audit"] = audit_user_skill(target)
            record["authority"] = "read_only_static_scan_no_install_or_execution"
    elif args.skills_command == "preview":
        selected = select_external_skills(catalog, query=args.goal, limit=3)
        record = {
            **base,
            "goal": args.goal,
            "selected": [_skill_primitive(item) for item in selected],
            "selection_limit": 3,
        }
    else:
        record = base

    if args.json:
        print(json.dumps(record, ensure_ascii=False, sort_keys=True, indent=2), file=output)
        return exit_ok
    if args.skills_command == "inspect":
        selected = record["selected"]
        assert isinstance(selected, dict)
        print(
            f"External skill · {selected['name']} · {selected['revision']} · read-only",
            file=output,
        )
        print(record["instruction"], file=output)
        return exit_ok
    if args.skills_command == "audit":
        audit = record["audit"]
        assert isinstance(audit, dict)
        safe = redact_terminal_output(
            json.dumps(audit, ensure_ascii=False, sort_keys=True, indent=2),
            force=True,
        )
        selected = record["selected"]
        assert isinstance(selected, dict)
        print(
            f"External skill audit · {selected['name']} · {audit['verdict']} · read-only",
            file=output,
        )
        print(safe, file=output)
        return exit_ok
    if args.skills_command == "preview":
        selected = record["selected"]
        assert isinstance(selected, list)
        print(
            f"External skill preview · {len(selected)} selected for this Job · no run created",
            file=output,
        )
        for item in selected:
            assert isinstance(item, dict)
            description = f" · {item['description']}" if item["description"] else ""
            print(f"  {item['name']} · {item['revision']}{description}", file=output)
        return exit_ok
    action = "reloaded" if args.skills_command == "reload" else "listed"
    print(
        f"External skills {action} · {record['discovered_count']} compatible · "
        f"{record['skipped_count']} skipped · no cache or installation",
        file=output,
    )
    if not catalog.roots:
        print(
            "Add a root with `--skills-dir PATH` or [skills].external_dirs in Noruct config.",
            file=output,
        )
    else:
        for item in catalog.skills:
            description = f" · {item.description}" if item.description else ""
            print(f"  {item.name} · {item.relative_path}{description}", file=output)
    return exit_ok
