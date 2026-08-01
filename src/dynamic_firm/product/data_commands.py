"""CLI adapter for local runtime data lifecycle commands.

This module is intentionally a thin Product Surface adapter.  The data
management service still owns archive, delete and support-bundle semantics;
the CLI merely supplies its parsed arguments and the configured state path.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Callable, Mapping, TextIO

from dynamic_firm.product.data_management import (
    create_support_bundle,
    delete_state_database,
    export_state_database,
)
from dynamic_firm.runtime.models import to_primitive


def run_data_command(
    args: argparse.Namespace,
    settings: Mapping[str, object],
    config_path: Path,
    output: TextIO,
    *,
    state_path_for: Callable[[argparse.Namespace, Mapping[str, object]], Path],
) -> int:
    """Run one explicit local-data operation without opening a Company Job."""

    state_path = state_path_for(args, settings)
    if args.data_command == "export":
        record = export_state_database(
            state_path,
            args.destination,
            overwrite=args.force,
        )
        warning = (
            "Export contains sensitive runtime/company user data; protect it like the live "
            "state database. The separate Knowledge DB and Vault are not included; use "
            "`noruct knowledge export DESTINATION --state STATE_DB`."
        )
    elif args.data_command == "delete":
        if not args.confirm:
            raise ValueError("Local data deletion requires --confirm")
        record = delete_state_database(state_path)
        warning = record.residual_backup_warning
    else:
        record = create_support_bundle(
            state_path,
            config_path,
            dict(settings),
            args.destination,
            overwrite=args.force,
        )
        warning = "Support bundle excludes raw user content and applies secret redaction."
    if args.json:
        print(
            json.dumps(
                to_primitive(record),
                ensure_ascii=False,
                sort_keys=True,
                indent=2,
            ),
            file=output,
        )
    else:
        if args.data_command == "delete":
            print(
                "Runtime/company state deletion · "
                f"{'deleted' if record.deleted else 'nothing to delete'}",
                file=output,
            )
        elif args.data_command == "export":
            print(
                f"Runtime/company state export · {record.destination} · "
                f"sha256={record.sha256}",
                file=output,
            )
        else:
            print(
                f"{args.data_command.replace('-', ' ').title()} · "
                f"{record.destination} · sha256={record.sha256}",
                file=output,
            )
        print(warning, file=output)
    return 0
