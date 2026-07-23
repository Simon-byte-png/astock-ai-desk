import os
import sys
import tempfile
import unittest
from unittest.mock import patch

APP_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, APP_ROOT)

from lib import agents, llm, portfolio  # noqa: E402


class GateCheckTests(unittest.TestCase):
    def test_rejects_perfunctory_answers(self):
        result = agents.gate_check({
            "why_company": "想买",
            "evidence_against": "不知道",
            "loss_plan": "随便",
        })
        self.assertFalse(result["passed"])
        self.assertTrue(result["follow_up"])

    def test_accepts_specific_answers_without_model_key(self):
        answers = {
            "why_company": "我想核验公司最近三年的经营现金流是否持续改善",
            "evidence_against": "如果利润增长但经营现金流连续恶化，我会推翻判断",
            "loss_plan": "只用模拟资金的百分之十，并在复盘里记录证据错在哪里",
        }
        with patch.object(llm, "DEEPSEEK_KEY", ""), patch.object(llm, "STEP_KEY", ""):
            result = agents.gate_check(answers)
        self.assertTrue(result["passed"])


class PortfolioTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        portfolio.DATA_DIR = self.temp.name
        portfolio.DB_PATH = os.path.join(self.temp.name, "test.db")
        portfolio._INITIALIZED = False
        self.user_id = portfolio.ensure_user()
        self.quote = {
            "code": "600519", "name": "测试公司", "price": 10.0, "as_of": "2026-07-23",
        }

    def tearDown(self):
        self.temp.cleanup()

    def test_buy_and_sell_are_recorded(self):
        answers = {
            "why_company": "研究现金流",
            "evidence_against": "现金流恶化",
            "loss_plan": "限制模拟仓位",
        }
        with patch("lib.portfolio.market.quote", return_value=self.quote):
            after_buy = portfolio.place_order(
                self.user_id, "600519", "buy", 100, answers, "🙂"
            )
            self.assertEqual(after_buy["cash"], 99000.0)
            self.assertEqual(after_buy["positions"][0]["qty"], 100)

            after_sell = portfolio.place_order(
                self.user_id, "600519", "sell", 40, answers, "😌"
            )
            self.assertEqual(after_sell["cash"], 99400.0)
            self.assertEqual(after_sell["positions"][0]["qty"], 60)
            self.assertEqual(len(after_sell["decisions"]), 2)

    def test_buy_requires_board_lot(self):
        with patch("lib.portfolio.market.quote", return_value=self.quote):
            with self.assertRaisesRegex(ValueError, "100股"):
                portfolio.place_order(self.user_id, "600519", "buy", 1, {}, "")


class LlmFallbackTests(unittest.TestCase):
    def test_primary_failure_falls_back_to_step(self):
        calls = []

        def fake_stream(provider, *args, **kwargs):
            calls.append(provider)
            if provider == "deepseek":
                raise RuntimeError("primary unavailable")
            return "备用通道正常"

        with patch.object(llm, "DEEPSEEK_KEY", "test"), \
                patch.object(llm, "STEP_KEY", "test"), \
                patch.object(llm, "_stream_chat", side_effect=fake_stream), \
                patch.object(llm.time, "sleep", return_value=None):
            text = llm.chat("system", "user", retries=1)
        self.assertEqual(text, "备用通道正常")
        self.assertEqual(calls, ["deepseek", "step"])

    def test_status_never_contains_api_key(self):
        status = str(llm.provider_status())
        self.assertNotIn("api_key", status.lower())
        self.assertNotIn("token", status.lower())


if __name__ == "__main__":
    unittest.main()
