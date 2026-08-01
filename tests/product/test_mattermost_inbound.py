from __future__ import annotations

import asyncio
import io
import json
import tempfile
import unittest
from pathlib import Path

from dynamic_firm.cli import EXIT_OK, main
from dynamic_firm.product.inbound_channel import InboundMessageStore, inbound_state_path
from dynamic_firm.product.mattermost_inbound import MattermostInboundConfig, MattermostInboundCursorStore, mattermost_inbound_state_path, run_mattermost_inbound


class MattermostInboundTests(unittest.TestCase):
    def test_first_poll_primes_then_dispatches_only_an_allowlisted_plaintext_post(self) -> None:
        case = self

        class Client:
            def __init__(self) -> None: self.calls = 0
            def read(self, *, since: int | None):
                self.calls += 1
                if self.calls == 1:
                    return {"posts": {"old": {"id": "old", "user_id": "operator1", "channel_id": "channel1", "message": "historical", "create_at": 1000}}}
                case.assertEqual(since, 1000)
                return {"posts": {
                    "post1": {"id": "post1", "user_id": "operator1", "channel_id": "channel1", "message": "inspect", "create_at": 2000},
                    "post2": {"id": "post2", "user_id": "other", "channel_id": "channel1", "message": "ignore", "create_at": 2001},
                }}

        async def dispatch(_message): return ("job-1", "SUCCEEDED")
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            config = MattermostInboundConfig(workspace, "http://127.0.0.1:8065", "channel1", ("operator1",))
            client = Client()
            state = workspace / "state.sqlite3"
            with MattermostInboundCursorStore(mattermost_inbound_state_path(state)) as cursor, InboundMessageStore(inbound_state_path(state)) as messages:
                first = asyncio.run(run_mattermost_inbound(config, cursor_store=cursor, message_store=messages, dispatch=dispatch, client=client))
                second = asyncio.run(run_mattermost_inbound(config, cursor_store=cursor, message_store=messages, dispatch=dispatch, client=client))
        self.assertTrue(first.primed)
        self.assertEqual(second.accepted_count, 1)
        self.assertEqual(second.ignored_count, 1)

    def test_cli_configuration_and_capability_status_are_local_only(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "config.toml"
            output = io.StringIO()
            result = main(["--config", str(config), "channel", "mattermost-inbox-configure", "--workspace", directory, "--base-url", "http://127.0.0.1:8065", "--channel-id", "channel1", "--allow-sender", "operator1", "--json"], stdout=output, stderr=io.StringIO())
            capabilities = io.StringIO()
            status = main(["--config", str(config), "capabilities", "status", "--json"], stdout=capabilities, stderr=io.StringIO())
        self.assertEqual(result, EXIT_OK)
        self.assertEqual(status, EXIT_OK)
        self.assertTrue(json.loads(output.getvalue())["configuration_changed"])
        self.assertTrue(json.loads(capabilities.getvalue())["mattermost_inbound"]["enabled"])


if __name__ == "__main__":
    unittest.main()
