from __future__ import annotations

import json
import threading
import unittest
from urllib.request import urlopen

from dynamic_firm.product.gateway_dashboard import serve_gateway_dashboard


class GatewayDashboardTests(unittest.TestCase):
    def test_loopback_dashboard_serves_only_status_projection(self) -> None:
        ready = threading.Event()
        received: dict[str, object] = {}

        def announce(host: str, port: int) -> None:
            received.update(host=host, port=port)
            ready.set()

        thread = threading.Thread(
            target=serve_gateway_dashboard,
            kwargs={
                "snapshot": lambda: {"authority": "projection", "token": "not-a-secret-value"},
                "maximum_requests": 2,
                "on_ready": announce,
            },
            daemon=True,
        )
        thread.start()
        self.assertTrue(ready.wait(2.0))
        base = f"http://{received['host']}:{received['port']}"
        with urlopen(base + "/api/status", timeout=2.0) as response:
            self.assertEqual(response.status, 200)
            self.assertEqual(json.loads(response.read()), {"authority": "projection", "token": "not-a-secret-value"})
            self.assertEqual(response.headers["Cache-Control"], "no-store")
        with urlopen(base + "/", timeout=2.0) as response:
            self.assertEqual(response.status, 200)
            self.assertIn(b"Local read-only operator projection", response.read())
        thread.join(2.0)
        self.assertFalse(thread.is_alive())

    def test_invalid_port_and_request_cap_are_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "port"):
            serve_gateway_dashboard(snapshot=lambda: {}, port=-1, maximum_requests=1)
        with self.assertRaisesRegex(ValueError, "requests"):
            serve_gateway_dashboard(snapshot=lambda: {}, maximum_requests=0)


if __name__ == "__main__":
    unittest.main()
