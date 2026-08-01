"""Foundation evidence command schema, isolated from global CLI ingress."""

from __future__ import annotations

import argparse
from pathlib import Path


def add_foundation_evidence_commands(commands: argparse._SubParsersAction, *, default_state_path: Path) -> None:
    """Register evidence/release commands without dispatching authority."""

    parser = commands.add_parser
    evidence = parser("validate-provider-evidence", help="Validate one secret-free provider-slot evidence record without contacting a provider.")
    evidence.add_argument("path", type=Path); evidence.add_argument("--json", action="store_true")
    matrix = parser("provider-evidence-status", help="Validate the complete four-slot provider evidence matrix without contacting a provider.")
    matrix.add_argument("directory", type=Path); matrix.add_argument("--json", action="store_true")
    records = parser("provider-evidence-records-status", help="Validate four canonical provider evidence paths without copying or renaming them.")
    for name in ("--direct", "--read-tool", "--approval", "--cancel-recovery"):
        records.add_argument(name, type=Path, required=True)
    records.add_argument("--json", action="store_true")
    admission = parser("release-admission-status", help="Report fail-closed release admission from canonical provider evidence and current runtime gates.")
    for name in ("--direct", "--read-tool", "--approval", "--cancel-recovery"):
        admission.add_argument(name, type=Path, required=True)
    admission.add_argument("--provenance-packet", type=Path)
    admission.add_argument("--provenance-decisions", type=Path)
    admission.add_argument("--state", type=Path, default=default_state_path)
    admission.add_argument("--json", action="store_true")
    draft = parser("validate-release-authorization-draft", help="Validate an inert unsigned release-authorization draft without authorizing release.")
    draft.add_argument("path", type=Path); draft.add_argument("--json", action="store_true")
    review = parser("validate-provenance-review", help="Validate an explicitly supplied human provenance record; never activates a release.")
    review.add_argument("--packet", type=Path, required=True)
    review.add_argument("--decisions", type=Path, required=True)
    review.add_argument("--json", action="store_true")
    capture = parser("capture-provider-evidence", help="Capture one current v2 provider-slot record from a completed local ledger without contacting a provider.")
    capture.add_argument("--ledger", type=Path, required=True); capture.add_argument("--run-id", required=True)
    capture.add_argument("--slot", choices=("direct", "read_tool", "approval", "cancel_recovery"), required=True)
    capture.add_argument("--wheel", type=Path, required=True); capture.add_argument("--runtime-python", required=True)
    capture.add_argument("--fixture-root", type=Path, required=True); capture.add_argument("--provider-id", required=True)
    capture.add_argument("--model-id", required=True); capture.add_argument("--max-wall-time-ms", type=int, required=True)
    capture.add_argument("--operator-authorized-at", required=True); capture.add_argument("--output", type=Path, required=True)
    capture.add_argument("--confirm", action="store_true"); capture.add_argument("--json", action="store_true")
