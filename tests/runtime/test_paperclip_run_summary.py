from __future__ import annotations

import unittest

from dynamic_firm._vendor.paperclip_runtime.run_summary import summarize_terminal_result
from dynamic_firm.runtime.models import EmployeeRunResult, EventType, RunStatus, Usage, utc_now
from dynamic_firm.runtime.store import RunStore
from tests.runtime.helpers import make_request


class PaperclipTerminalSummaryTests(unittest.TestCase):
    def test_projects_only_bounded_terminal_operator_fields(self) -> None:
        result = summarize_terminal_result(
            {
                "status": "SUCCEEDED",
                "summary": "x" * 600,
                "usage": {
                    "model_calls": 2,
                    "tool_calls": 1,
                    "input_tokens": 20,
                    "cached_input_tokens": 4,
                    "output_tokens": 10,
                    "cost_usd": 0.12,
                    "ignored": "never-project",
                },
                "artifact_refs": ["private-artifact"],
                "messages": ["private transcript"],
                "failure": {"code": "IGNORED_ON_SUCCESS", "message_safe": "private"},
            }
        )

        assert result is not None
        self.assertEqual(result["status"], "SUCCEEDED")
        self.assertEqual(len(result["summary"]), 500)
        self.assertEqual(
            result["usage"],
            {
                "model_calls": 2,
                "tool_calls": 1,
                "input_tokens": 20,
                "cached_input_tokens": 4,
                "output_tokens": 10,
                "cost_usd": 0.12,
            },
        )
        self.assertEqual(result["failure_code"], "IGNORED_ON_SUCCESS")
        self.assertNotIn("artifact_refs", result)
        self.assertNotIn("messages", result)

    def test_rejects_nonfinite_or_negative_usage_and_empty_results(self) -> None:
        self.assertIsNone(summarize_terminal_result(None))
        result = summarize_terminal_result(
            {
                "usage": {
                    "model_calls": -1,
                    "tool_calls": True,
                    "cost_usd": -0.01,
                }
            }
        )
        self.assertIsNone(result)

    def test_store_adds_only_redacted_bounded_summary_to_terminal_event(self) -> None:
        store = RunStore()
        request = make_request(request_id="paperclip-terminal-summary")
        handle, _ = store.create_run(request)
        store.begin_run(handle.run_id)
        result = EmployeeRunResult(
            run_id=handle.run_id,
            request_id=handle.request_id,
            job_id=request.task.job_id,
            task_id=request.task.task_id,
            employee_id=request.employee.employee_id,
            status=RunStatus.SUCCEEDED,
            summary="api_key=sk-terminal-secret-1234567890; completed safely",
            output_artifact_refs=("private-artifact",),
            acceptance_evidence=(),
            unresolved_issues=(),
            observations=(),
            suggested_followups=(),
            signals=(),
            partial_result=False,
            usage=Usage(model_calls=1, tool_calls=2, cost_usd=0.01),
            last_event_seq=0,
            started_at=utc_now(),
            finished_at=utc_now(),
        )

        store.terminalize(result, EventType.RUN_SUCCEEDED, {"summary_bytes": 48})
        payload = store.list_events(handle.run_id)[-1].payload
        terminal = payload["terminal_summary"]

        self.assertEqual(terminal["status"], "SUCCEEDED")
        self.assertEqual(terminal["usage"]["model_calls"], 1)
        self.assertNotIn("private-artifact", str(terminal))
        self.assertNotIn("sk-terminal-secret", str(terminal))
        store.close()
