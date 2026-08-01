#!/usr/bin/env python3
"""Run the R0 recovery regression slice.

This is not the full release suite. It is the small cross-component smoke set
used before larger Firm Engineering edits so regressions are quick to localize.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from pathlib import Path


_MODERN_TUI_TESTS = (
    "tests.product.test_modern_tui.ModernTerminalLazyImportTests.test_secondary_modal_components_keep_textual_as_a_lazy_dependency",
    "tests.product.test_modern_tui.ModernTerminalLazyImportTests.test_settings_modal_component_keeps_textual_as_a_lazy_dependency",
    "tests.product.test_modern_tui.SessionInputHistoryTests.test_session_history_deduplicates_bounds_and_restores_unsent_draft",
    "tests.product.test_modern_tui.SessionInputHistoryTests.test_session_history_is_bounded_and_rejects_unknown_direction",
    "tests.product.test_modern_tui.ModernTerminalTests.test_width_matrix_keeps_one_composer_and_compact_company_identity",
    "tests.product.test_modern_tui.ModernTerminalTests.test_slash_opens_a_visible_palette_and_tab_completes_a_command",
    "tests.product.test_modern_tui.ModernTerminalTests.test_settings_modal_keeps_selection_open_until_done",
    "tests.product.test_modern_tui.ModernTerminalTests.test_settings_category_button_recomposes_without_closing",
    "tests.product.test_modern_tui.ModernTerminalTests.test_settings_reset_discards_staged_changes_without_closing_or_applying",
    "tests.product.test_modern_tui.ModernTerminalTests.test_settings_registry_entries_are_selectable_dashboard_controls",
    "tests.product.test_modern_tui.ModernTerminalTests.test_messaging_app_picker_changes_fields_without_closing_or_saving",
    "tests.product.test_modern_tui.ModernTerminalTests.test_settings_all_pages_use_live_pickers_and_stage_one_typed_change",
    "tests.product.test_modern_tui.ModernTerminalTests.test_company_settings_stage_manager_and_skill_proposals_without_applying_them",
    "tests.product.test_modern_tui.ModernTerminalTests.test_connection_settings_stage_one_atomic_non_secret_change",
    "tests.product.test_modern_tui.ModernTerminalTests.test_connection_settings_exposes_account_login_and_model_picker",
    "tests.product.test_modern_tui.ModernTerminalTests.test_model_command_opens_picker_and_applies_selected_model",
    "tests.product.test_modern_tui.ModernTerminalTests.test_settings_and_command_shortcuts_keep_operator_controls_discoverable",
    "tests.product.test_modern_tui.ModernTerminalTests.test_graph_workbench_authors_a_draft_then_reopens_for_explicit_selection",
    "tests.product.test_modern_tui.ModernTerminalTests.test_graph_workbench_requests_a_provider_free_saved_selection_preview",
    "tests.product.test_modern_tui.ModernTerminalTests.test_graph_workbench_can_save_constraints_then_preview_in_one_step",
    "tests.product.test_modern_tui.ModernTerminalTests.test_graph_workbench_submits_typed_multi_task_topology",
    "tests.product.test_modern_tui.ModernTerminalTests.test_job_audit_is_read_only_and_displays_graph_lineage",
    "tests.product.test_modern_tui.ModernTerminalTests.test_pending_graph_proposal_is_explicitly_resolved_from_job_audit",
    "tests.product.test_modern_tui.ModernTerminalTests.test_job_command_opens_the_same_audit_after_command_dispatch",
    "tests.product.test_modern_tui.ModernTerminalTests.test_job_audit_catalog_reopens_the_selected_retained_job",
    "tests.product.test_modern_tui.ModernTerminalTests.test_specific_job_command_opens_the_requested_read_only_audit",
    "tests.product.test_modern_tui.ModernTerminalTests.test_terminal_crash_record_excludes_exception_text",
    "tests.product.test_modern_tui.ModernTerminalTests.test_streamed_answer_has_one_surface_and_commands_stay_controller_owned",
    "tests.product.test_modern_tui.ModernTerminalTests.test_company_watch_projects_the_same_final_report_contract",
    "tests.product.test_modern_tui.ModernTerminalTests.test_company_watch_and_run_pulse_follow_controller_events_without_owning_state",
    "tests.product.test_modern_tui.ModernTerminalTests.test_session_scoped_input_history_restores_draft_without_submitting",
    "tests.product.test_modern_tui.ModernTerminalTests.test_approval_is_requested_from_the_controller_and_resolved_in_modal",
)


DEFAULT_TESTS = [
    "tests.company.test_operating",
    "tests.company.test_frontdoor",
    "tests.company.test_manager",
    "tests.company.test_coordinator",
    "tests.company.test_graph_blueprints",
    "tests.compiler.test_service",
    "tests.compiler.test_parser",
    "tests.kernel.test_graph",
    "tests.kernel.test_service",
    "tests.evaluation.test_manager_value_contract",
    "tests.evaluation.test_manager_value_live",
    "tests.knowledge.test_folder_runtime",
    "tests.knowledge.test_bridge",
    "tests.product.test_global_settings",
    "tests.product.test_settings_dashboard",
    "tests.product.test_company_commands",
    "tests.product.test_company_command_renderer",
    *_MODERN_TUI_TESTS,
    "tests.application.test_company_cli",
    # Component-level seams established during the CLI/runtime split.  Keep
    # these as separate modules so a recovery run identifies the broken
    # composition boundary without rerunning the whole product suite.
    "tests.application.test_foundation_cli",
    "tests.application.test_goal_runtime_assembly",
    "tests.application.test_graph_cli",
    "tests.test_component_import_boundaries",
    "tests.application.test_modern_terminal_controller",
    "tests.runtime.test_company_budget",
    "tests.runtime.test_runtime_loop",
    "tests.runtime.test_manager_tools",
    "tests.test_cli",
]


def repo_root() -> Path:
    result = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    return Path(result.stdout.strip())


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("tests", nargs="*", help="Override the default recovery regression modules.")
    parser.add_argument(
        "--timeout-seconds",
        type=float,
        default=90.0,
        help="Fail one test module if it exceeds this bounded recovery time.",
    )
    args = parser.parse_args()

    root = repo_root()
    env = os.environ.copy()
    env["PYTHONPATH"] = str(root / "src")
    tests = args.tests or DEFAULT_TESTS
    if args.timeout_seconds <= 0:
        parser.error("--timeout-seconds must be positive")
    # Keep framework-owned event loops (notably Textual's) inside each module
    # process.  The recovery slice is an integration boundary, not a benchmark;
    # isolating modules makes a leaked timer or loop deterministic to localize
    # and cannot hold the remaining smoke checks hostage.
    for test_module in tests:
        command = [sys.executable, "-m", "unittest", test_module]
        print(f"[recovery] start {test_module}", flush=True)
        started = time.monotonic()
        try:
            result = subprocess.run(
                command,
                cwd=root,
                env=env,
                timeout=args.timeout_seconds,
            )
        except subprocess.TimeoutExpired:
            elapsed = time.monotonic() - started
            print(
                f"[recovery] timeout {test_module} after {elapsed:.2f}s "
                f"(limit {args.timeout_seconds:g}s)",
                file=sys.stderr,
                flush=True,
            )
            return 124
        elapsed = time.monotonic() - started
        print(
            f"[recovery] end {test_module} exit={result.returncode} elapsed={elapsed:.2f}s",
            flush=True,
        )
        if result.returncode:
            return result.returncode
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
