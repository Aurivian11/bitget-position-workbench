"""CFD dashboard regressions using synthetic trades and an isolated project."""
from contextlib import ExitStack, closing
from dataclasses import replace
from datetime import datetime
from decimal import Decimal
import importlib
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch


APP = Path(__file__).resolve().parents[1] / "app"


def millis(day):
    return int(datetime.fromisoformat(day + "T00:00:00+00:00").timestamp() * 1000)


class FakeClient:
    uta = True

    def __init__(self):
        self.cfd_failed = False
        self._get = Mock(return_value={"list": []})
        self.history_positions = Mock(side_effect=AssertionError(
            "Web history must not use the notification activation filter"))

    def accounts(self, product):
        if product == "CFD" and self.cfd_failed:
            raise RuntimeError("synthetic CFD outage")
        return [{"marginCoin": "USD" if product == "CFD" else "USDT",
                 "accountEquity": "50" if product == "CFD" else "100",
                 "available": "40", "locked": "10", "unrealizedPL": "0"}]


def app_modules():
    return {name: module for name, module in sys.modules.copy().items()
            if getattr(module, "__file__", None)
            and Path(module.__file__).resolve().is_relative_to(APP)}


class CfdWebTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        stack = ExitStack()
        cls.addClassCleanup(stack.close)
        cls.project = Path(stack.enter_context(tempfile.TemporaryDirectory()))
        stack.enter_context(patch.dict(os.environ, {
            "BITGET_PROJECT_ROOT": str(cls.project),
            "BITGET_API_KEY": "test-key", "BITGET_API_SECRET": "test-secret",
            "BITGET_API_PASSPHRASE": "test-passphrase",
            "BITGET_API_BASE": "https://example.invalid",
            "TELEGRAM_BOT_TOKEN": "test-token", "TELEGRAM_CHAT_ID": "test-chat",
            "BITGET_WEB_ORIGIN": "https://example.invalid",
            "BITGET_ACCOUNT_MODE": "uta", "BITGET_CFD_ENABLED": "1",
            "BITGET_PRODUCT_TYPES": "USDT-FUTURES", "BITGET_MARGIN_COIN": "USDT",
            "FEE_REBATE_RATE": "0.5", "SEND_STARTUP_SUMMARY": "0",
            "WEB_HISTORY_START": "2025-01-10T00:00:00+00:00",
            "WEB_HISTORY_OPENED_AFTER": "2025-01-12T00:00:00+00:00",
        }, clear=True))
        stack.enter_context(patch.object(sys, "path", [str(APP), *sys.path]))
        previous = app_modules()
        for name in previous:
            sys.modules.pop(name)

        def restore_modules():
            for name in app_modules():
                sys.modules.pop(name, None)
            sys.modules.update(previous)

        stack.callback(restore_modules)
        stack.enter_context(patch("urllib.request.urlopen", side_effect=AssertionError(
            "Tests must not access external APIs or notifications")))
        cls.bot = importlib.import_module("bot")
        # Import web_app normally, but never load a real .env or construct its
        # writable CFD adapter. Config paths remain inside the temporary project.
        with patch.object(cls.bot, "load_env_file"), patch.object(
                cls.bot, "BitgetClient", return_value=FakeClient()):
            cls.web = importlib.import_module("web_app")
        assert cls.bot.notification_settings.PROJECT == cls.project

    def setUp(self):
        directory = tempfile.TemporaryDirectory(dir=self.project)
        self.addCleanup(directory.cleanup)
        self.db_path = Path(directory.name) / "positions.sqlite3"
        with closing(sqlite3.connect(self.db_path)) as db, db:
            db.executescript("""
                CREATE TABLE position_events (id INTEGER PRIMARY KEY,
                    event_type TEXT, before_json TEXT);
                CREATE TABLE position_state (position_key TEXT PRIMARY KEY,
                    payload TEXT, updated_at TEXT);
                CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT);
                CREATE TABLE cfd_financial_records (id TEXT PRIMARY KEY,
                    ts INTEGER, payload TEXT);
                CREATE TABLE cfd_metadata (key TEXT PRIMARY KEY, value TEXT);
            """)
            db.execute("INSERT INTO cfd_metadata VALUES ('activation', ?)",
                       (str(millis("2025-01-20")),))
        self.client = FakeClient()
        self.config = replace(self.web.CONFIG, db_path=self.db_path)
        self.data = self.dashboard()

    def dashboard(self):
        with patch.object(self.bot, "BitgetClient", return_value=self.client):
            return self.web.DashboardData(self.config)

    def add_cfd_trade(self, order="cfd-1", opened="2025-01-13", closed="2025-01-14"):
        records = [
            {"id": order + "-open", "orderId": order, "symbol": order.replace("-", "").upper() + "USD",
             "side": "buy", "qty": "1", "openPrice": "100", "closePrice": "0",
             "fee": "-2", "swap": "0", "cashFlow": "0", "balanceChange": "-2",
             "ts": str(millis(opened))},
            {"id": order + "-close", "orderId": order + "-exit", "symbol": order.replace("-", "").upper() + "USD",
             "side": "sell", "qty": "1", "openPrice": "100", "closePrice": "105",
             "fee": "0", "swap": "0", "cashFlow": "5", "balanceChange": "5",
             "ts": str(millis(closed) + 1000)},
        ]
        with closing(sqlite3.connect(self.db_path)) as db, db:
            db.executemany("INSERT INTO cfd_financial_records VALUES (?, ?, ?)",
                           [(row["id"], int(row["ts"]), json.dumps(row)) for row in records])

    def add_crypto_trade(self):
        history = {"positionId": "crypto-1", "symbol": "TESTUSDT", "holdSide": "long",
                   "marginCoin": "USDT", "ctime": str(millis("2025-01-13")),
                   "utime": str(millis("2025-01-14")), "openAvgPrice": "100",
                   "closeAvgPrice": "120", "openTotalPos": "1", "closeTotalPos": "1",
                   "openFee": "-2", "closeFee": "0", "totalFunding": "0",
                   "netProfit": "18", "pnl": "20"}
        before = {"productType": "USDT-FUTURES", "_history": history}
        with closing(sqlite3.connect(self.db_path)) as db, db:
            db.execute("INSERT INTO position_events VALUES (1, 'closed', ?)",
                       (json.dumps(before),))

    def position(self, position_id, *, complete=True):
        return {"productType": "CFD", "symbol": "TESTUSD", "holdSide": "long",
                "marginCoin": "USD", "posId": position_id, "total": "1",
                "openPriceAvg": "100", "markPrice": "110" if complete else None,
                "unrealizedPL": "10", "achievedProfits": "0", "totalFee": "-1",
                "deductedFee": "2" if complete else None, "marginSize": None,
                "leverage": None, "cTime": str(millis("2025-01-15")),
                "_cfdOpenedAt": str(millis("2025-01-13")),
                "_accountMode": "cfd", "_cfdNetPnl": "7" if complete else None}

    def test_history_before_notification_activation_is_read_only(self):
        self.add_cfd_trade()
        with closing(sqlite3.connect(self.db_path)) as db:
            before = list(db.iterdump())
        records, error = self.data.history()
        self.assertIsNone(error)
        self.assertEqual([row["positionId"] for row in records], ["cfd-1"])
        self.assertLess(int(records[0]["utime"]), millis("2025-01-20"))
        self.client.history_positions.assert_not_called()
        with closing(sqlite3.connect(self.db_path)) as db:
            self.assertEqual(list(db.iterdump()), before)

    def test_cfd_still_obeys_web_reset_retention_and_exclusion(self):
        self.add_cfd_trade("boundary", opened="2025-01-12")
        self.add_cfd_trade("old-open", opened="2025-01-11")
        self.add_cfd_trade("old-close", opened="2025-01-08", closed="2025-01-09")
        self.add_cfd_trade("retained", opened="2025-01-07", closed="2025-01-08")
        self.add_cfd_trade("excluded")
        with patch.dict(os.environ, {
                "WEB_RETAINED_HISTORY_POSITION_KEYS": "CFD:retained,CFD:excluded",
                "WEB_EXCLUDED_HISTORY_POSITION_IDS": "excluded"}):
            data = self.dashboard()
        records, error = data.history()
        self.assertIsNone(error)
        self.assertEqual({row["positionId"] for row in records}, {"boundary", "retained"})

    def test_usd_and_usdt_accounts_and_profit_are_not_added_together(self):
        self.add_cfd_trade()
        self.add_crypto_trade()
        result = self.data.payload()
        accounts = {row["coin"]: row for row in result["accounts"]}
        self.assertEqual(set(accounts), {"USD", "USDT"})
        self.assertEqual(accounts["USD"]["productType"], "CFD")
        self.assertEqual(Decimal(accounts["USD"]["accountEquity"]), Decimal("50"))
        self.assertEqual(Decimal(accounts["USDT"]["accountEquity"]), Decimal("100"))
        history = {row["coin"]: row for row in result["history"]}
        self.assertEqual(Decimal(history["USD"]["netPnl"]), Decimal("3"))
        self.assertEqual(Decimal(history["USD"]["rebate"]), Decimal("0"))
        self.assertEqual(history["USD"]["quantityUnit"], "手")
        self.assertEqual(history["USD"]["feeRebateRate"], "0")
        self.assertEqual(Decimal(history["USDT"]["netPnl"]), Decimal("19"))
        self.assertEqual(result["stats"]["coin"], "USDT")
        self.assertEqual(result["stats"]["trades"], 1)
        self.assertEqual(Decimal(result["stats"]["netProfit"]), Decimal("19"))

    def test_unknowns_stay_unknown_and_same_contract_tickets_keep_ids(self):
        unknown = self.position("ticket-a", complete=False)
        zero = self.position("ticket-b")
        zero.update(_cfdNetPnl="0", deductedFee="0", marginSize="0")
        self.data.current_state = Mock(return_value=([unknown, zero], "2025-01-15T00:00:00+00:00"))
        result = self.data.positions_payload()
        positions = result["positions"]
        self.assertEqual([row["positionId"] for row in positions], ["ticket-a", "ticket-b"])
        self.assertEqual(positions[0]["openedAt"], positions[1]["openedAt"])
        self.assertEqual(datetime.fromisoformat(positions[0]["openedAt"]).timestamp() * 1000,
                         millis("2025-01-13"))
        for field in ("markPrice", "margin", "leverage", "transactionFee"):
            self.assertIsNone(positions[0][field], field)
        self.assertEqual(positions[0]["netPnl"], "—")
        self.assertEqual(positions[1]["netPnl"], "0")
        self.assertEqual(positions[1]["transactionFee"], "0")
        self.assertEqual(positions[1]["margin"], "0")
        self.assertEqual(result["currentTotals"][0]["netPnl"], "—")
        self.assertIsNone(result["currentTotals"][0]["margin"])

    def test_cfd_uses_exchange_pnl_not_crypto_price_projection(self):
        item = self.position("ticket-a")
        self.data.current_state = Mock(return_value=([item], "2025-01-15T00:00:00+00:00"))
        self.data.mark_prices.get = Mock(side_effect=AssertionError("CFD used crypto quotes"))
        row = self.data.positions_payload()["positions"][0]
        self.data.mark_prices.get.assert_not_called()
        self.assertEqual(row["markPrice"], "110")
        self.assertEqual(row["netPnl"], "7")
        self.assertEqual(row["quoteMode"], "snapshot")
        self.assertEqual(row["quantityUnit"], "手")

    def test_cfd_outage_does_not_hide_crypto_or_clear_cached_cfd(self):
        self.add_cfd_trade()
        self.add_crypto_trade()
        self.data.history()
        self.data.accounts()
        self.client.cfd_failed = True
        fresh = self.dashboard()
        with self.assertLogs("bitget-position-web", level="WARNING"):
            history, history_error = fresh.history()
            accounts, account_error = fresh.accounts()
        self.assertTrue(history_error)
        self.assertTrue(account_error)
        self.assertEqual({row["productType"] for row in history}, {"USDT-FUTURES"})
        self.assertEqual({row["productType"] for row in accounts}, {"USDT-FUTURES"})
        self.data._history_at = self.data._accounts_at = 0
        with self.assertLogs("bitget-position-web", level="WARNING"):
            cached_history, history_error = self.data.history()
            cached_accounts, account_error = self.data.accounts()
        self.assertTrue(history_error)
        self.assertTrue(account_error)
        self.assertEqual({row["productType"] for row in cached_history}, {"USDT-FUTURES", "CFD"})
        self.assertEqual({row["productType"] for row in cached_accounts}, {"USDT-FUTURES", "CFD"})


if __name__ == "__main__":
    unittest.main()
