from __future__ import annotations

import asyncio
import io
import json
import os
import tempfile
import unittest
from pathlib import Path

from dynamic_firm.cli import EXIT_OK, main
from dynamic_firm.product.inbound_channel import InboundMessageStore, inbound_state_path
from dynamic_firm.product.matrix_inbound import MatrixInboundConfig, MatrixInboundCursorStore, matrix_inbound_state_path, run_matrix_inbound


class MatrixInboundTests(unittest.TestCase):
    def test_first_sync_primes_then_dispatches_one_allowlisted_plaintext_event(self) -> None:
        case = self
        class Client:
            def __init__(self) -> None: self.calls = 0
            def read(self, *, since: str | None):
                self.calls += 1
                if self.calls == 1: return {"next_batch": "one", "rooms": {"join": {}}}
                case.assertEqual(since, "one")
                return {"next_batch": "two", "rooms": {"join": {"!room:example.org": {"timeline": {"events": [
                    {"event_id": "$event-1", "sender": "@operator:example.org", "type": "m.room.message", "content": {"msgtype": "m.text", "body": "inspect"}},
                    {"event_id": "$event-2", "sender": "@other:example.org", "type": "m.room.message", "content": {"msgtype": "m.text", "body": "ignore"}},
                ]}}}}}
        async def dispatch(_message): return ("job-1", "SUCCEEDED")
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            config = MatrixInboundConfig(workspace, "http://127.0.0.1:8008", "!room:example.org", ("@operator:example.org",))
            client = Client()
            with MatrixInboundCursorStore(matrix_inbound_state_path(workspace / "state.sqlite3")) as cursor, InboundMessageStore(inbound_state_path(workspace / "state.sqlite3")) as messages:
                first = asyncio.run(run_matrix_inbound(config, cursor_store=cursor, message_store=messages, dispatch=dispatch, client=client))
                second = asyncio.run(run_matrix_inbound(config, cursor_store=cursor, message_store=messages, dispatch=dispatch, client=client))
        self.assertTrue(first.primed)
        self.assertEqual(second.accepted_count, 1)
        self.assertEqual(second.ignored_count, 1)

    def test_cli_configuration_and_capability_status_are_local_only(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "config.toml"; output = io.StringIO()
            result = main(["--config", str(config), "channel", "matrix-inbox-configure", "--workspace", directory, "--homeserver-url", "http://127.0.0.1:8008", "--room-id", "!room:example.org", "--allow-sender", "@operator:example.org", "--json"], stdout=output, stderr=io.StringIO())
            capabilities = io.StringIO(); status = main(["--config", str(config), "capabilities", "status", "--json"], stdout=capabilities, stderr=io.StringIO())
        self.assertEqual(result, EXIT_OK); self.assertEqual(status, EXIT_OK)
        self.assertTrue(json.loads(output.getvalue())["configuration_changed"])
        self.assertTrue(json.loads(capabilities.getvalue())["matrix_inbound"]["enabled"])


if __name__ == "__main__":
    unittest.main()
