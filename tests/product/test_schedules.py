from __future__ import annotations

import io
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from dynamic_firm.cli import EXIT_OK, main
from dynamic_firm.product.schedules import ScheduleStore


class ScheduleStoreTests(unittest.TestCase):
    def test_due_claim_is_atomic_and_completion_links_one_company_job(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with ScheduleStore(root / "runtime.db") as store:
                item = store.create(
                    name="repository report",
                    goal="report repository health",
                    workspace=root,
                    interval_minutes=5,
                )
                claimed = store.claim_due(now=item.next_run_at + timedelta(seconds=1))
                self.assertEqual(tuple(entry.schedule_id for entry in claimed), (item.schedule_id,))
                self.assertEqual(store.claim_due(now=item.next_run_at + timedelta(seconds=1)), ())
                completed = store.complete(
                    item.schedule_id, job_id="job-fixture", status="SUCCEEDED"
                )
                self.assertEqual(completed.last_job_id, "job-fixture")
                self.assertEqual(completed.last_status, "SUCCEEDED")
                self.assertEqual(completed.run_count, 1)

    def test_due_claim_limit_does_not_mark_unclaimed_schedules_as_run(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with ScheduleStore(root / "runtime.db") as store:
                first = store.create(name="first", goal="first goal", workspace=root, interval_minutes=5)
                second = store.create(name="second", goal="second goal", workspace=root, interval_minutes=5)
                now = max(first.next_run_at, second.next_run_at) + timedelta(seconds=1)
                claimed = store.claim_due(now=now, limit=1)
                self.assertEqual(len(claimed), 1)
                remaining = store.claim_due(now=now, limit=1)
                self.assertEqual(len(remaining), 1)
                self.assertNotEqual(claimed[0].schedule_id, remaining[0].schedule_id)

    def test_cron_schedule_uses_utc_five_field_next_run_and_existing_claim_path(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with ScheduleStore(root / "runtime.db") as store:
                item = store.create_cron(name="weekday report", goal="report", workspace=root, expression="30 9 * * 1-5")
                self.assertEqual(item.schedule_type, "cron")
                self.assertEqual(item.cron_expression, "30 9 * * 1-5")
                now = datetime(2026, 7, 27, 9, 30, tzinfo=timezone.utc)  # Monday
                store._conn.execute("UPDATE scheduled_jobs SET next_run_at = ? WHERE schedule_id = ?", (now.isoformat(), item.schedule_id)); store._conn.commit()
                claimed = store.claim_due(now=now)
                self.assertEqual(len(claimed), 1)
                refreshed = store.get(item.schedule_id)
                assert refreshed is not None
                self.assertEqual(refreshed.next_run_at, datetime(2026, 7, 28, 9, 30, tzinfo=timezone.utc))

    def test_cli_create_and_list_do_not_start_a_background_scheduler(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = root / "runtime.db"
            created = io.StringIO()
            self.assertEqual(
                main(
                    [
                        "schedule", "create", "report repository health",
                        "--every-minutes", "15", "--workspace", str(root),
                        "--state", str(state), "--confirm",
                    ],
                    stdout=created,
                    stderr=io.StringIO(),
                ),
                EXIT_OK,
            )
            self.assertIn("Schedule created", created.getvalue())
            self.assertIn("schedule tick --confirm", created.getvalue())

            listed = io.StringIO()
            self.assertEqual(
                main(["schedule", "list", "--state", str(state)], stdout=listed, stderr=io.StringIO()),
                EXIT_OK,
            )
            self.assertIn("every 15m", listed.getvalue())

            status = io.StringIO()
            self.assertEqual(
                main(["schedule", "status", "--state", str(state)], stdout=status, stderr=io.StringIO()),
                EXIT_OK,
            )
            self.assertIn("manual tick only", status.getvalue())

    def test_cli_creates_cron_schedule_without_starting_a_daemon(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); output = io.StringIO()
            result = main(["schedule", "cron-create", "weekday report", "--cron", "30 9 * * 1-5", "--workspace", str(root), "--state", str(root / "runtime.db"), "--confirm"], stdout=output, stderr=io.StringIO())
        self.assertEqual(result, EXIT_OK)
        self.assertIn("cron 30 9 * * 1-5 UTC", output.getvalue())

    def test_explicit_run_delegates_to_the_ordinary_company_job_path(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = root / "runtime.db"
            config = root / "config.toml"
            config.write_text('[provider]\nkind = "ollama"\nmodel = "fixture"\n', encoding="utf-8")
            with ScheduleStore(state) as store:
                item = store.create(
                    name="single run",
                    goal="inspect repository",
                    workspace=root,
                    interval_minutes=15,
                )
            result = SimpleNamespace(
                job_id="job-scheduled-fixture",
                status=SimpleNamespace(value="SUCCEEDED"),
                summary="Scheduled Company Job completed.",
            )
            fake_run = AsyncMock(return_value=result)
            output = io.StringIO()
            with patch("dynamic_firm.cli.run_goal", fake_run):
                self.assertEqual(
                    main(
                        [
                            "--config", str(config), "schedule", "run", item.schedule_id,
                            "--state", str(state), "--confirm",
                        ],
                        provider_factory=lambda _: object(),
                        stdout=output,
                        stderr=io.StringIO(),
                    ),
                    EXIT_OK,
                )
            self.assertEqual(fake_run.await_count, 1)
            with ScheduleStore(state) as store:
                completed = store.get(item.schedule_id)
            assert completed is not None
            self.assertEqual(completed.last_job_id, "job-scheduled-fixture")
            self.assertEqual(completed.last_status, "SUCCEEDED")
            self.assertIn("1 Job(s) claimed", output.getvalue())

    def test_foreground_daemon_requires_explicit_start_and_uses_the_same_job_path(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = root / "runtime.db"
            config = root / "config.toml"
            config.write_text('[provider]\nkind = "ollama"\nmodel = "fixture"\n', encoding="utf-8")
            with ScheduleStore(state) as store:
                item = store.create(name="daemon run", goal="inspect repository", workspace=root, interval_minutes=15)
                store._conn.execute("UPDATE scheduled_jobs SET next_run_at = ? WHERE schedule_id = ?", ("2000-01-01T00:00:00+00:00", item.schedule_id))
                store._conn.commit()
            result = SimpleNamespace(job_id="job-daemon-fixture", status=SimpleNamespace(value="SUCCEEDED"), summary="Daemon Company Job completed.")
            output = io.StringIO()
            fake_run = AsyncMock(return_value=result)
            with patch("dynamic_firm.cli.run_goal", fake_run):
                self.assertEqual(
                    main([
                        "--config", str(config), "schedule", "daemon", "--state", str(state),
                        "--max-cycles", "1", "--confirm",
                    ], provider_factory=lambda _: object(), stdout=output, stderr=io.StringIO()),
                    EXIT_OK,
                )
            self.assertEqual(fake_run.await_count, 1)
            self.assertIn("foreground only", output.getvalue())

    def test_schedule_service_starts_one_local_child_and_never_enables_auto_restart(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / "config.toml"
            config.write_text('[provider]\nkind = "ollama"\nmodel = "fixture"\n', encoding="utf-8")
            state = root / "runtime.db"
            output = io.StringIO()
            with patch("dynamic_firm.cli.subprocess.Popen", return_value=SimpleNamespace(pid=43220)) as spawn:
                code = main([
                    "--config", str(config), "schedule", "service", "start", "--state", str(state),
                    "--poll-seconds", "15", "--limit", "8", "--confirm", "--json",
                ], stdout=output, stderr=io.StringIO())
        import json
        payload = json.loads(output.getvalue())
        self.assertEqual(code, EXIT_OK)
        self.assertTrue(payload["background_service"])
        self.assertFalse(payload["automatic_restart"])
        self.assertFalse(payload["automatic_learning_apply"])
        self.assertEqual(payload["record"]["poll_seconds"], 15.0)
        command = spawn.call_args.args[0]
        self.assertIn("schedule", command)
        self.assertIn("daemon", command)


if __name__ == "__main__":
    unittest.main()
