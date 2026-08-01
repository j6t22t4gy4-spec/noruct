from __future__ import annotations

import json
import unittest

from dynamic_firm.providers.wire_safety import (
    parse_tool_arguments,
    sanitize_wire_payload,
)


class ProviderWireSafetyTests(unittest.TestCase):
    def test_surrogates_are_replaced_through_nested_keys_and_values(self) -> None:
        payload = {
            "messages": [
                {"content": "clipboard \udce2 value"},
                {"reasoning": {"part\ud800": ["clean", "dirty \udfff"]}},
            ]
        }

        sanitize_wire_payload(payload)
        encoded = json.dumps(payload, ensure_ascii=False).encode("utf-8")

        self.assertIn("�", payload["messages"][0]["content"])
        self.assertIn("part�", payload["messages"][1]["reasoning"])
        self.assertGreater(len(encoded), 0)

    def test_common_malformed_json_is_repaired_only_when_it_is_an_object(self) -> None:
        cases = (
            ('{"key": "value",}', {"key": "value"}),
            ('{"items": [1, 2', {"items": [1, 2]}),
            ('{"summary": "line one\nline two"}', {"summary": "line one\nline two"}),
            ("", {}),
            ("None", {}),
        )
        for raw, expected in cases:
            with self.subTest(raw=raw):
                self.assertEqual(parse_tool_arguments(raw, "fixture_tool"), expected)

        self.assertIsNone(parse_tool_arguments('["not", "an", "object"]', "fixture_tool"))

    def test_unrepairable_arguments_are_rejected_without_logging_raw_content(self) -> None:
        raw = "customer-token-never-log-this"

        with self.assertLogs(
            "dynamic_firm._vendor.runtime_safety.message_safety",
            level="WARNING",
        ) as captured:
            parsed = parse_tool_arguments(raw, "fixture_tool")

        self.assertIsNone(parsed)
        self.assertNotIn(raw, "\n".join(captured.output))
        self.assertNotIn("fixture_tool", "\n".join(captured.output))

    def test_surrogate_key_collision_rejects_tool_arguments(self) -> None:
        raw = '{"\\ud800": 1, "\\ufffd": 2}'

        with self.assertLogs(
            "dynamic_firm._vendor.runtime_safety.message_safety",
            level="WARNING",
        ):
            parsed = parse_tool_arguments(raw, "fixture_tool")

        self.assertIsNone(parsed)


if __name__ == "__main__":
    unittest.main()
