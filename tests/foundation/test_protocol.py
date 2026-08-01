from __future__ import annotations

import unittest

from dynamic_firm.foundation.protocol import (
    MAX_FRAME_BYTES,
    FoundationFrame,
    FoundationProtocolError,
    FrameSequence,
    decode_frame,
    encode_frame,
)


class FoundationProtocolTests(unittest.TestCase):
    def test_plain_json_frame_round_trips(self) -> None:
        frame = FoundationFrame("execute", "run-1", 1, {"goal": "inspect"})
        self.assertEqual(decode_frame(encode_frame(frame)), frame)

    def test_protocol_rejects_unknown_version_and_out_of_order_frame(self) -> None:
        with self.assertRaisesRegex(FoundationProtocolError, "version mismatch"):
            decode_frame(
                '{"protocol":"other","type":"execute","run_id":"r","seq":1,"payload":{}}'
            )
        sequence = FrameSequence()
        sequence.accept(FoundationFrame("execute", "r", 1, {}))
        with self.assertRaisesRegex(FoundationProtocolError, "expected 2"):
            sequence.accept(FoundationFrame("cancel", "r", 3, {}))

    def test_protocol_rejects_oversized_frame_before_transport(self) -> None:
        with self.assertRaisesRegex(FoundationProtocolError, "byte limit"):
            encode_frame(
                FoundationFrame("execute", "r", 1, {"value": "x" * MAX_FRAME_BYTES})
            )


if __name__ == "__main__":
    unittest.main()
