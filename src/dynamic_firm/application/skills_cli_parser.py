"""Argument schema for the bounded external-Skill command family."""

from __future__ import annotations

import argparse
from pathlib import Path


def add_skills_commands(commands: argparse._SubParsersAction) -> None:
    """Register Skill discovery and explicit local management commands."""

    skills = commands.add_parser(
        "skills",
        help="Connect and inspect compatible user-owned SKILL.md instructions without executing their scripts.",
    )
    skill_commands = skills.add_subparsers(dest="skills_command", required=True)

    def add_skill_source_options(item: argparse.ArgumentParser) -> None:
        item.add_argument(
            "--skills-dir",
            type=Path,
            action="append",
            default=None,
            metavar="PATH",
            help="Read this compatible SKILL.md root instead of configured [skills].external_dirs; repeatable.",
        )
        item.add_argument("--json", action="store_true")

    skill_list = skill_commands.add_parser(
        "list", help="List compatible instructions visible to the next Job."
    )
    add_skill_source_options(skill_list)
    skill_inspect = skill_commands.add_parser(
        "inspect", help="Show one bounded compatible instruction without running it."
    )
    skill_inspect.add_argument("name", help="Exact SKILL.md name from `noruct skills list`.")
    add_skill_source_options(skill_inspect)
    skill_preview = skill_commands.add_parser(
        "preview", help="Show up to three instructions a Job goal would receive."
    )
    skill_preview.add_argument("goal", help="Goal text to evaluate against the local skill catalog.")
    add_skill_source_options(skill_preview)
    skill_reload = skill_commands.add_parser(
        "reload", help="Fresh-scan user-owned roots; Noruct never keeps a skill cache."
    )
    add_skill_source_options(skill_reload)
    skill_audit = skill_commands.add_parser(
        "audit",
        help="Run the vendored read-only static scanner on one discovered compatible skill.",
    )
    skill_audit.add_argument("name", help="Exact SKILL.md name from `noruct skills list`.")
    add_skill_source_options(skill_audit)
    skill_connect = skill_commands.add_parser(
        "connect",
        help="Make one or more existing local SKILL.md roots available to future Jobs; no skill is copied or executed.",
    )
    skill_connect.add_argument("directory", type=Path, nargs="+", metavar="PATH")
    skill_connect.add_argument("--json", action="store_true")
    skill_disconnect = skill_commands.add_parser(
        "disconnect",
        help="Stop discovering globally connected external skill roots; user files remain untouched.",
    )
    skill_disconnect.add_argument("--json", action="store_true")
    skill_manage = skill_commands.add_parser(
        "manage",
        help="Create, edit, patch, move supporting files, or delete a skill in one explicit user-owned root.",
    )
    skill_manage.add_argument(
        "action",
        choices=("create", "edit", "patch", "delete", "write_file", "remove_file"),
    )
    skill_manage.add_argument("name", help="Bounded local skill name.")
    skill_manage.add_argument(
        "--skills-root",
        type=Path,
        required=True,
        metavar="PATH",
        help="Explicit local root Noruct may manage; this is never inferred from read-only skill roots.",
    )
    skill_manage.add_argument("--content-file", type=Path, default=None)
    skill_manage.add_argument("--category", default=None)
    skill_manage.add_argument("--file-path", default=None)
    skill_manage.add_argument("--file-content-file", type=Path, default=None)
    skill_manage.add_argument("--old-text", default=None)
    skill_manage.add_argument("--new-text", default=None)
    skill_manage.add_argument("--replace-all", action="store_true")
    skill_manage.add_argument("--absorbed-into", default=None)
    skill_manage.add_argument(
        "--confirm",
        action="store_true",
        help="Confirm the requested local skill mutation after reviewing its arguments.",
    )
    skill_manage.add_argument("--json", action="store_true")
    skill_import = skill_commands.add_parser(
        "import",
        help="Explicitly import or roll back one audited local SKILL.md tree; no hub, download, or automatic update.",
    )
    skill_import_commands = skill_import.add_subparsers(
        dest="skill_import_command", required=True
    )
    skill_import_local = skill_import_commands.add_parser(
        "local", help="Copy one user-selected local skill through a staged receipt-bound import."
    )
    skill_import_local.add_argument("source_dir", type=Path, help="Existing local skill directory containing SKILL.md.")
    skill_import_local.add_argument("--skills-root", type=Path, required=True, metavar="PATH")
    skill_import_local.add_argument("--name", required=True, help="Destination managed-skill identifier.")
    skill_import_local.add_argument("--replace", action="store_true", help="Back up and replace an existing managed skill of the same name.")
    skill_import_local.add_argument("--receipt-out", type=Path, required=True, metavar="PATH")
    skill_import_local.add_argument("--confirm", action="store_true")
    skill_import_local.add_argument("--json", action="store_true")
    skill_import_rollback = skill_import_commands.add_parser(
        "rollback", help="Restore the prior tree or remove an untouched import using its receipt."
    )
    skill_import_rollback.add_argument("--skills-root", type=Path, required=True, metavar="PATH")
    skill_import_rollback.add_argument("--receipt-file", type=Path, required=True, metavar="PATH")
    skill_import_rollback.add_argument("--confirm", action="store_true")
    skill_import_rollback.add_argument("--json", action="store_true")
    skill_package = skill_commands.add_parser(
        "package",
        help=(
            "Preview or register a reviewed semantic Skill Package bound to one "
            "source-managed local skill receipt; it never executes the skill."
        ),
    )
    skill_package_commands = skill_package.add_subparsers(
        dest="skill_package_command", required=True
    )
    for name, help_text in (
        ("preview", "Preview an EXPERIMENTAL receipt-bound semantic Skill Package."),
        ("register", "Register the package locally; stage/install/activate remain explicit."),
    ):
        command = skill_package_commands.add_parser(name, help=help_text)
        command.add_argument("--skills-root", type=Path, required=True, metavar="PATH")
        command.add_argument("--name", required=True, help="Existing source-managed skill name.")
        command.add_argument("--artifact-id", required=True)
        command.add_argument("--version", required=True)
        command.add_argument("--skill-key", required=True)
        command.add_argument(
            "--applies-to", action="append", required=True, metavar="IDENTIFIER",
            help="Employee id, role key, or capability that this reviewed procedure may assist. Repeatable.",
        )
        command.add_argument(
            "--step", action="append", required=True, metavar="TEXT",
            help="One reviewed, bounded procedure step. Raw SKILL.md content is never copied automatically. Repeatable.",
        )
        command.add_argument(
            "--required-capability", action="append", default=[], metavar="IDENTIFIER",
            help="Existing local capability required by this semantic procedure. Repeatable.",
        )
        command.add_argument("--state", type=Path, default=None)
        command.add_argument("--json", action="store_true")
        if name == "register":
            command.add_argument("--confirm", action="store_true")
