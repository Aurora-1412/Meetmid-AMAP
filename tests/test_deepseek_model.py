import unittest
from unittest.mock import patch

import app_v2


class _FakeMessage:
    content = '{"keyword":"精品咖啡馆","keyword_fallbacks":[],"min_rating":4.0,"top_n":20}'


class _FakeChoice:
    message = _FakeMessage()


class _FakeResponse:
    choices = [_FakeChoice()]


class _ModelCheckingCompletions:
    def create(self, **kwargs):
        if kwargs.get("model") != "deepseek-v4-flash":
            raise ValueError("unsupported model")
        return _FakeResponse()


class _FakeClient:
    class _Chat:
        completions = _ModelCheckingCompletions()

    chat = _Chat()


class DeepSeekModelTest(unittest.TestCase):
    def test_agent_plan_uses_current_supported_flash_model(self):
        with patch.object(app_v2, "llm_client", _FakeClient()):
            plan = app_v2.agent_plan("咖啡馆")

        self.assertEqual(plan["keyword"], "精品咖啡馆")


if __name__ == "__main__":
    unittest.main()
