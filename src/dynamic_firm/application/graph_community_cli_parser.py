"""Parser registration for the data-only Community Graph command family.

The product CLI owns global options, configuration and error rendering.  This
component owns only the stable argument schema for Community Graph commands;
it creates no registry, Evolution, Job, or Network authority.
"""

from __future__ import annotations

import argparse
from pathlib import Path


def add_community_graph_commands(
    graph_commands: argparse._SubParsersAction[argparse.ArgumentParser],
) -> None:
    """Register the complete Community Blueprint command family."""

    community_list = graph_commands.add_parser(
        "community-list",
        help="List local Community Blueprint publication drafts; it never contacts a network.",
    )
    community_list.add_argument("--state", type=Path, default=None)
    community_list.add_argument("--json", action="store_true")

    prepare = graph_commands.add_parser(
        "community-prepare",
        help="Create a private review draft from one local Blueprint while dropping all templates and source content.",
    )
    prepare.add_argument("blueprint_id")
    prepare.add_argument("version", type=int)
    prepare.add_argument("draft_id")
    prepare.add_argument("artifact_id")
    prepare.add_argument(
        "--passport",
        type=Path,
        help="Qualified public/synthetic Community Passport JSON; it is bound into the release but never uploaded.",
    )
    prepare.add_argument("--state", type=Path, default=None)
    prepare.add_argument("--confirm", action="store_true")
    prepare.add_argument("--json", action="store_true")

    publish = graph_commands.add_parser(
        "community-publish",
        help="Move one local Community Blueprint draft to pending review; it does not upload anything.",
    )
    publish.add_argument("draft_id")
    publish.add_argument("--state", type=Path, default=None)
    publish.add_argument("--confirm", action="store_true")
    publish.add_argument("--json", action="store_true")

    withdraw = graph_commands.add_parser(
        "community-withdraw",
        help="Withdraw a pending local publication before any future network handoff.",
    )
    withdraw.add_argument("draft_id")
    withdraw.add_argument("--state", type=Path, default=None)
    withdraw.add_argument("--confirm", action="store_true")
    withdraw.add_argument("--json", action="store_true")

    export = graph_commands.add_parser(
        "community-export",
        help="Write one pending data-only Community Blueprint release for manual review or future opt-in transport.",
    )
    export.add_argument("draft_id")
    export.add_argument("output", type=Path)
    export.add_argument("--state", type=Path, default=None)
    export.add_argument("--confirm", action="store_true")
    export.add_argument("--json", action="store_true")

    artifact_export = graph_commands.add_parser(
        "community-artifact-export",
        help="Wrap one pending Community release in the signed Evolution Artifact contract; it does not upload or activate anything.",
    )
    artifact_export.add_argument("draft_id")
    artifact_export.add_argument("output", type=Path)
    artifact_export.add_argument("--channel", choices=("EXPERIMENTAL", "STABLE"), default="EXPERIMENTAL")
    artifact_export.add_argument("--state", type=Path, default=None)
    artifact_export.add_argument("--confirm", action="store_true")
    artifact_export.add_argument("--json", action="store_true")

    artifact_inspect = graph_commands.add_parser(
        "community-artifact-inspect",
        help="Validate a Community Graph Artifact without saving, trusting, staging, or activating it.",
    )
    artifact_inspect.add_argument("artifact_file", type=Path)
    artifact_inspect.add_argument("--json", action="store_true")

    passport_build = graph_commands.add_parser(
        "community-passport-build",
        help="Derive one qualified Community Passport from 10–512 public/synthetic observations; no Blueprint or network state changes.",
    )
    passport_build.add_argument("observations", type=Path)
    passport_build.add_argument("output", type=Path)
    passport_build.add_argument("--confirm", action="store_true")
    passport_build.add_argument("--json", action="store_true")

    discover = graph_commands.add_parser(
        "community-discover",
        help="Read a public signed-artifact-registry bundle URL and list only valid Community Graph entries; it never stores or trusts it.",
    )
    discover.add_argument("url")
    discover.add_argument("--allow-insecure-loopback", action="store_true")
    discover.add_argument("--json", action="store_true")

    import_reviewed = graph_commands.add_parser(
        "community-import-reviewed",
        help="Materialize one approved, signature-verified Community Graph Artifact snapshot locally; it remains inactive.",
    )
    import_reviewed.add_argument("snapshot_id")
    import_reviewed.add_argument("artifact_id")
    import_reviewed.add_argument("version")
    import_reviewed.add_argument("--state", type=Path, default=None)
    import_reviewed.add_argument("--confirm", action="store_true")
    import_reviewed.add_argument("--json", action="store_true")

    inspect = graph_commands.add_parser(
        "community-inspect",
        help="Strictly inspect a data-only Community Blueprint release without saving or activating it.",
    )
    inspect.add_argument("release_file", type=Path)
    inspect.add_argument("--json", action="store_true")

    stage = graph_commands.add_parser(
        "community-stage",
        help="Stage a reviewed Community Blueprint as a generic local revision; it never activates a Job.",
    )
    stage.add_argument("release_file", type=Path)
    stage.add_argument("--state", type=Path, default=None)
    stage.add_argument("--confirm", action="store_true")
    stage.add_argument("--json", action="store_true")

    activate = graph_commands.add_parser(
        "community-activate",
        help="Pin one staged Community Blueprint for a future Job; use graph preview for Work Order validation.",
    )
    activate.add_argument("blueprint_id")
    activate.add_argument("version", type=int)
    activate.add_argument("--slot", default="default")
    activate.add_argument("--state", type=Path, default=None)
    activate.add_argument("--confirm", action="store_true")
    activate.add_argument("--json", action="store_true")
