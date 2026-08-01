from __future__ import annotations

import argparse
import io
import unittest

from dynamic_firm.product.company_command_renderer import (
    COMPANY_COMMAND_JOB_FAILED,
    COMPANY_COMMAND_OK,
    render_company_command_result,
)


class CompanyCommandRendererTests(unittest.TestCase):
    def test_replay_mismatch_keeps_the_existing_failed_exit_status(self) -> None:
        output = io.StringIO()
        status = render_company_command_result(
            argparse.Namespace(company_command="replay", json=False),
            payload={"patch_id": "patch-1", "replay_matches": False},
            active_playbook_revision=3,
            active_roster_revision=2,
            output=output,
        )
        self.assertEqual(status, COMPANY_COMMAND_JOB_FAILED)
        self.assertIn("patch-1 · replay mismatch", output.getvalue())

    def test_json_renderer_keeps_stdout_as_one_primitive_document(self) -> None:
        output = io.StringIO()
        status = render_company_command_result(
            argparse.Namespace(company_command="organization-metrics", json=True),
            payload={"episode_count": 0, "graph_proposal_decisions": {}},
            active_playbook_revision=1,
            active_roster_revision=1,
            output=output,
        )
        self.assertEqual(status, COMPANY_COMMAND_OK)
        self.assertEqual(
            output.getvalue(),
            '{"episode_count": 0, "graph_proposal_decisions": {}}\n',
        )
