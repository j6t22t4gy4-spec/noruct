from __future__ import annotations

import asyncio
import json
import unittest
from unittest.mock import patch

from dynamic_firm.providers.bedrock import BedrockProvider, BedrockProviderConfig
from dynamic_firm.runtime.models import ModelMessage, ModelRequest, ToolSchema
from dynamic_firm.runtime.ports import CancellationToken


class BedrockProviderTests(unittest.TestCase):
    def test_converse_request_translates_tools_and_response(self) -> None:
        provider = BedrockProvider(BedrockProviderConfig("https://bedrock-runtime.us-east-1.amazonaws.com", "us.anthropic.claude-sonnet-4-6"))
        response = {"output": {"message": {"content": [{"toolUse": {"toolUseId": "call-1", "name": "lookup", "input": {"query": "x"}}}]}}, "stopReason": "tool_use", "usage": {"inputTokens": 3, "outputTokens": 4}}
        request = ModelRequest((ModelMessage("system", "rules"), ModelMessage("user", "find x")), (ToolSchema("lookup", "find", {"type": "object", "properties": {}, "required": [], "additionalProperties": False}),), "bedrock", "run", 1)
        with patch.dict("os.environ", {"AWS_BEARER_TOKEN_BEDROCK": "token"}, clear=False), patch("dynamic_firm.providers.bedrock.urllib.request.urlopen") as open_url:
            open_url.return_value.__enter__.return_value.read.return_value = json.dumps(response).encode()
            result = asyncio.run(provider.complete(request, CancellationToken()))
        self.assertEqual(result.tool_calls[0].name, "lookup")
        body = json.loads(open_url.call_args.args[0].data.decode())
        self.assertEqual(body["messages"][0]["role"], "user")
        self.assertEqual(body["toolConfig"]["tools"][0]["toolSpec"]["name"], "lookup")

    def test_rejects_non_bedrock_endpoint(self) -> None:
        with self.assertRaisesRegex(ValueError, "Bedrock base URL"):
            BedrockProvider(BedrockProviderConfig("https://example.com", "model"))
