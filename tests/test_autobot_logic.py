import importlib.util
import pathlib
import unittest

BOT_PATH = pathlib.Path(__file__).resolve().parents[1] / "autobot.py"
spec = importlib.util.spec_from_file_location("autobot_mod", BOT_PATH)
autobot = importlib.util.module_from_spec(spec)
spec.loader.exec_module(autobot)


class BotLogicTests(unittest.TestCase):
    def test_parse_bool(self):
        self.assertTrue(autobot.parse_bool("true"))
        self.assertTrue(autobot.parse_bool("1"))
        self.assertFalse(autobot.parse_bool("false"))
        self.assertFalse(autobot.parse_bool("0"))

    def test_should_retry_http(self):
        self.assertTrue(autobot.should_retry_http(429))
        self.assertTrue(autobot.should_retry_http(500))
        self.assertFalse(autobot.should_retry_http(400))

    def test_backoff_growth(self):
        self.assertLess(autobot.backoff_delay(0), autobot.backoff_delay(1))

    def test_job_open_check(self):
        self.assertTrue(autobot.is_job_open({"state": "open"}))
        self.assertFalse(autobot.is_job_open({"state": "judging"}))
        self.assertTrue(autobot.is_job_open({}))

    def test_state_shape(self):
        s = autobot._ensure_state_shape({"bid_jobs": "bad"})
        self.assertIsInstance(s["bid_jobs"], list)
        self.assertIsInstance(s["bid_statuses"], dict)
        self.assertIsInstance(s["operation_statuses"], dict)

    def test_score_job_prefers_better_candidate(self):
        low = {
            "budget_amount": "1",
            "bid_count": 50,
            "tags": ["misc"],
            "job_type": "standard",
        }
        high = {
            "budget_amount": "40",
            "bid_count": 2,
            "tags": ["python", "near"],
            "job_type": "standard",
        }
        self.assertGreater(autobot.score_job(high), autobot.score_job(low))

    def test_choose_bid_terms_scales_with_score(self):
        job = {"budget_amount": "60", "tags": ["python"], "job_type": "standard"}
        low_amount, low_eta = autobot.choose_bid_terms(job, 0.5)
        hi_amount, hi_eta = autobot.choose_bid_terms(job, 0.95)
        self.assertNotEqual(low_amount, hi_amount)
        self.assertLess(hi_eta, low_eta)

    def test_operation_state_helpers(self):
        state = autobot._ensure_state_shape({})
        key = autobot.op_key("bid", "job123")
        self.assertEqual("", autobot.op_status(state, key))
        autobot.set_op_status(state, key, "done")
        self.assertEqual("done", autobot.op_status(state, key))


if __name__ == "__main__":
    unittest.main()
