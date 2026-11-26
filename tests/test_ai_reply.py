from __future__ import annotations

import unittest
from unittest.mock import patch

from tg_manager import ai_reply


class FakeResponse:
    status_code = 200

    def json(self):
        return {"choices": [{"message": {"content": "  A concise reply.  "}}]}


class FakeAsyncClient:
    calls: list[dict[str, object]] = []

    def __init__(self, *args: object, **kwargs: object) -> None:
        self.timeout = kwargs.get("timeout")

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None

    async def post(self, url: str, *, headers: dict[str, str], json: dict[str, object]):
        self.calls.append({"url": url, "headers": headers, "json": json})
        return FakeResponse()


class AIReplyTests(unittest.IsolatedAsyncioTestCase):
    async def test_request_uses_bounded_context_fast_model_and_disabled_thinking(self) -> None:
        FakeAsyncClient.calls.clear()
        messages = [
            {"role": "user" if index % 2 == 0 else "assistant", "content": f"message-{index}-" + "x" * 900}
            for index in range(12)
        ]
        with patch.object(ai_reply.httpx, "AsyncClient", FakeAsyncClient):
            result = await ai_reply.generate_contextual_reply(messages, api_key="private-test-key")

        self.assertEqual(result, "A concise reply.")
        request = FakeAsyncClient.calls[0]
        self.assertEqual(request["url"], "https://api.deepseek.com/chat/completions")
        self.assertEqual(request["headers"]["Authorization"], "Bearer private-test-key")
        payload = request["json"]
        self.assertEqual(payload["model"], "deepseek-flash")
        self.assertEqual(payload["thinking"], {"type": "disabled"})
        self.assertEqual(len(payload["messages"]), 11)
        self.assertTrue(all(len(item["content"]) <= 800 for item in payload["messages"][1:]))
        self.assertIn("message-11-", payload["messages"][-1]["content"])
        self.assertNotIn("message-0-", str(payload["messages"]))

    async def test_empty_key_returns_without_network_request(self) -> None:
        with patch.object(ai_reply.httpx, "AsyncClient") as client:
            result = await ai_reply.generate_contextual_reply(
                [{"role": "user", "content": "Hello"}], api_key=""
            )
        self.assertIsNone(result)
        client.assert_not_called()


if __name__ == "__main__":
    unittest.main()
