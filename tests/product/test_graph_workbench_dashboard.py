from __future__ import annotations

import json
import threading
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from dynamic_firm.product.graph_workbench_dashboard import serve_graph_workbench_dashboard


class GraphWorkbenchDashboardTests(unittest.TestCase):
    def test_loopback_dashboard_projects_graph_and_jobs_without_write_route(self) -> None:
        ready = threading.Event()
        received: dict[str, object] = {}

        def announce(host: str, port: int) -> None:
            received.update(host=host, port=port)
            ready.set()

        thread = threading.Thread(
            target=serve_graph_workbench_dashboard,
            kwargs={
                "graph_snapshot": lambda: {"selection": {"mutation_policy": "PROPOSE"}, "blueprints": ()},
                "job_catalog": lambda: {"schema": "noruct.job-audit-catalog.v1", "jobs": ({"job_id": "job-a"},)},
                "job_snapshot": lambda job_id: {"schema": "noruct.job-audit-surface.v1", "job": {"job_id": job_id}, "graph": {}, "checkpoints": ()},
                "operator_snapshot": lambda: {
                    "schema": "noruct.operator-surface.v1",
                    "manager": {"status": "active"},
                    "execution": {"decision": "SOLO_JOB"},
                    "hold": {"reason": "none"},
                    "approval": {"status": "none pending"},
                    "budget": {"summary": "within frozen limits"},
                    "attention": {"summary": "1 item requires operator review"},
                    "next_action": "Inspect the existing review item.",
                },
                "maximum_requests": 5,
                "on_ready": announce,
            },
            daemon=True,
        )
        thread.start()
        self.assertTrue(ready.wait(2.0))
        base = f"http://{received['host']}:{received['port']}"
        with urlopen(base + "/api/graph", timeout=2.0) as response:
            self.assertEqual(response.status, 200)
            self.assertEqual(json.loads(response.read())["selection"]["mutation_policy"], "PROPOSE")
            self.assertEqual(response.headers["Cache-Control"], "no-store")
            self.assertIn("frame-ancestors", response.headers["Content-Security-Policy"])
        with urlopen(base + "/api/jobs/job-a", timeout=2.0) as response:
            self.assertEqual(json.loads(response.read())["job"]["job_id"], "job-a")
        with urlopen(base + "/api/operator", timeout=2.0) as response:
            payload = json.loads(response.read())
        self.assertEqual(payload["attention"]["summary"], "1 item requires operator review")
        self.assertNotIn("candidate_body", json.dumps(payload))
        with urlopen(base + "/", timeout=2.0) as response:
            page = response.read()
        self.assertIn(b"Graph Workbench", page)
        self.assertIn(b"Company operator state", page)
        with self.assertRaises(HTTPError) as rejected:
            urlopen(Request(base + "/api/graph", method="POST"), timeout=2.0)
        self.assertEqual(rejected.exception.code, 405)
        rejected.exception.close()
        thread.join(2.0)
        self.assertFalse(thread.is_alive())

    def test_invalid_port_and_request_cap_are_rejected(self) -> None:
        kwargs = {
            "graph_snapshot": lambda: {},
            "job_catalog": lambda: {},
            "job_snapshot": lambda _job: {},
        }
        with self.assertRaisesRegex(ValueError, "port"):
            serve_graph_workbench_dashboard(**kwargs, port=-1, maximum_requests=1)
        with self.assertRaisesRegex(ValueError, "requests"):
            serve_graph_workbench_dashboard(**kwargs, maximum_requests=0)

    def test_pending_proposal_requires_local_token_and_uses_exact_callback(self) -> None:
        ready = threading.Event()
        received: dict[str, object] = {}
        calls: list[tuple[str, str, bool]] = []
        proposal_id = "graph-proposal-1234567890abcdef12345678"

        def announce(host: str, port: int) -> None:
            received.update(host=host, port=port)
            ready.set()

        def snapshot(job_id: str | None) -> dict[str, object]:
            return {
                "job": {"job_id": job_id},
                "graph": {"proposals": ({"proposal_id": proposal_id, "status": "PENDING"},)},
                "checkpoints": (),
            }

        thread = threading.Thread(
            target=serve_graph_workbench_dashboard,
            kwargs={
                "graph_snapshot": lambda: {},
                "job_catalog": lambda: {"jobs": ()},
                "job_snapshot": snapshot,
                "resolve_proposal": lambda job, proposal, approve: calls.append((job, proposal, approve)) or {"job_status": "SUCCEEDED"},
                "session_token": "test-local-graph-workbench-token",
                "maximum_requests": 3,
                "on_ready": announce,
            },
            daemon=True,
        )
        thread.start()
        self.assertTrue(ready.wait(2.0))
        base = f"http://{received['host']}:{received['port']}"
        body = json.dumps({"job_id": "job-a", "proposal_id": proposal_id, "decision": "approve"}).encode()
        with self.assertRaises(HTTPError) as forbidden:
            urlopen(Request(base + "/api/proposals/resolve", data=body, method="POST"), timeout=2.0)
        self.assertEqual(forbidden.exception.code, 403)
        forbidden.exception.close()
        with urlopen(Request(base + "/api/proposals/resolve", data=body, method="POST", headers={"X-Noruct-Local-Token": "test-local-graph-workbench-token"}), timeout=2.0) as response:
            self.assertEqual(response.status, 200)
            self.assertEqual(json.loads(response.read())["job_status"], "SUCCEEDED")
        with urlopen(base + "/", timeout=2.0) as response:
            self.assertIn(b"Explicit proposal decision", response.read())
        thread.join(2.0)
        self.assertFalse(thread.is_alive())
        self.assertEqual(calls, [("job-a", proposal_id, True)])

    def test_future_constraints_require_local_token_and_use_the_shared_callback(self) -> None:
        ready = threading.Event()
        received: dict[str, object] = {}
        calls: list[dict[str, object]] = []

        def announce(host: str, port: int) -> None:
            received.update(host=host, port=port)
            ready.set()

        thread = threading.Thread(
            target=serve_graph_workbench_dashboard,
            kwargs={
                "graph_snapshot": lambda: {"selection": {}},
                "job_catalog": lambda: {"jobs": ()},
                "job_snapshot": lambda _job: {"job": None},
                "save_future_constraints": lambda payload: calls.append(dict(payload)) or dict(payload),
                "session_token": "a" * 32,
                "maximum_requests": 2,
                "on_ready": announce,
            },
            daemon=True,
        )
        thread.start()
        self.assertTrue(ready.wait(2.0))
        base = f"http://{received['host']}:{received['port']}"
        payload = json.dumps(
            {
                "max_concurrency": 3,
                "max_cost_usd": 2.5,
                "max_wall_time_ms": 30000,
                "mutation_policy": "PROPOSE",
            }
        ).encode()
        with self.assertRaises(HTTPError) as rejected:
            urlopen(
                Request(
                    base + "/api/future-constraints",
                    data=payload,
                    headers={"Content-Type": "application/json", "X-Noruct-Local-Token": "wrong"},
                    method="POST",
                ),
                timeout=2.0,
            )
        self.assertEqual(rejected.exception.code, 403)
        rejected.exception.close()
        with urlopen(
            Request(
                base + "/api/future-constraints",
                data=payload,
                headers={"Content-Type": "application/json", "X-Noruct-Local-Token": "a" * 32},
                method="POST",
            ),
            timeout=2.0,
        ) as response:
            self.assertEqual(response.status, 200)
            self.assertEqual(json.loads(response.read())["selection"]["max_cost_usd"], 2.5)
        self.assertEqual(
            calls,
            [
                {
                    "max_concurrency": 3,
                    "max_cost_usd": 2.5,
                    "max_wall_time_ms": 30000,
                    "mutation_policy": "PROPOSE",
                }
            ],
        )
        thread.join(2.0)
        self.assertFalse(thread.is_alive())

    def test_dashboard_renders_revision_rationale_and_job_budget_evidence(self) -> None:
        ready = threading.Event()
        received: dict[str, object] = {}

        def announce(host: str, port: int) -> None:
            received.update(host=host, port=port)
            ready.set()

        thread = threading.Thread(
            target=serve_graph_workbench_dashboard,
            kwargs={
                "graph_snapshot": lambda: {
                    "selection": {},
                    "blueprints": ({
                        "blueprint_id": "planning",
                        "version": 2,
                        "origin": "USER_REVISION",
                        "execution_profiles": (),
                        "task_count": 1,
                        "execution_replica_count": 0,
                        "editor_tasks": (),
                        "revision_receipts": ({"rationale": "Separate evidence collection before synthesis."},),
                        "revision_diff": {},
                    },),
                },
                "job_catalog": lambda: {"jobs": ()},
                "job_snapshot": lambda _job: {"job": None},
                "maximum_requests": 1,
                "on_ready": announce,
            },
            daemon=True,
        )
        thread.start()
        self.assertTrue(ready.wait(2.0))
        base = f"http://{received['host']}:{received['port']}"
        with urlopen(base + "/", timeout=2.0) as response:
            page = response.read()
        self.assertIn(b"user rationale", page)
        self.assertIn(b"Frozen budget envelope", page)
        self.assertIn(b"Accepted Graph revisions", page)
        thread.join(2.0)
        self.assertFalse(thread.is_alive())


if __name__ == "__main__":
    unittest.main()
