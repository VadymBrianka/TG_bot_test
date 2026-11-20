import hashlib
import hmac
import json
import os
import sys
import time
import types
import unittest
from unittest.mock import MagicMock
from urllib.parse import urlencode

# Mock supabase & env before importing bot modules
sys.modules.setdefault("supabase", types.SimpleNamespace(create_client=MagicMock(), Client=object))
os.environ.setdefault("BOT_TOKEN", "123456:TEST_TOKEN")
os.environ.setdefault("MINIAPP_URL", "https://briankatgbot.vercel.app")
os.environ.setdefault("SUPABASE_URL", "https://example.supabase.co")
os.environ.setdefault("SUPABASE_KEY", "test_key")

BOT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if BOT_DIR not in sys.path:
    sys.path.insert(0, BOT_DIR)

from middlewares.auth import verify_telegram_init_data
from bot_handlers.quick_entry import INCOME_RE, EXPENSE_RE
from schemas import TransactionCreate, DebtCreate
from services.subscriptions import has_plan, PLANS
from services.trading_bot import RISK_PROFILES


class TestCoreFinTrack(unittest.TestCase):
    def test_telegram_init_data_hmac(self):
        token = "123456:TEST_TOKEN"
        user_json = json.dumps({"id": 464800908, "first_name": "Vadym"})
        params = {"auth_date": str(int(time.time())), "query_id": "AAHd", "user": user_json}
        dcs = "\n".join(f"{k}={v}" for k, v in sorted(params.items()))
        sk = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
        h = hmac.new(sk, dcs.encode(), hashlib.sha256).hexdigest()
        init_data = urlencode({**params, "hash": h})

        self.assertEqual(verify_telegram_init_data(init_data, token), 464800908)
        self.assertIsNone(verify_telegram_init_data(init_data + "tampered", token))

    def test_quick_entry_regexes(self):
        m1 = INCOME_RE.match("+100кава")
        self.assertIsNotNone(m1)
        self.assertEqual(m1.group(1), "100")
        self.assertEqual(m1.group(3), "кава")

        m2 = INCOME_RE.match("+50 USDT Фріланс")
        self.assertIsNotNone(m2)
        self.assertEqual(m2.group(2), "USDT")

        m3 = EXPENSE_RE.match("Кава 80 UAH")
        self.assertIsNotNone(m3)
        self.assertEqual(m3.group(1), "Кава")
        self.assertEqual(m3.group(2), "80")
        self.assertEqual(m3.group(3), "UAH")

    def test_subscription_hierarchy(self):
        self.assertTrue(has_plan("vip", "free"))
        self.assertTrue(has_plan("vip", "pro"))
        self.assertTrue(has_plan("pro", "pro"))
        self.assertFalse(has_plan("pro", "vip"))
        self.assertFalse(has_plan("free", "pro"))
        self.assertGreater(PLANS["pro"]["stars_year"], 0)
        self.assertLess(PLANS["pro"]["usd_year"], PLANS["pro"]["usd_month"] * 12)

    def test_schemas_and_risk_profiles(self):
        tx = TransactionCreate(account_id="acc1", amount=80, currency="UAH", type="expense", note="test")
        self.assertEqual(tx.currency, "UAH")
        debt = DebtCreate(direction="owed_to_me", person_name="Олег", amount=500, currency="UAH")
        self.assertEqual(debt.person_name, "Олег")
        self.assertIn("conservative", RISK_PROFILES)
        self.assertIn("balanced", RISK_PROFILES)
        self.assertIn("aggressive", RISK_PROFILES)


if __name__ == "__main__":
    unittest.main()
