#!/usr/bin/env python3
import json
import os
from unittest.mock import patch
import tempfile
import unittest
from pathlib import Path
import bot


def pos(size="1", price="100", leverage="10", side="long"):
    return {
        "productType": "USDT-FUTURES", "symbol": "BTCUSDT", "holdSide": side,
        "marginCoin": "USDT", "total": size, "openPriceAvg": price,
        "markPrice": "101", "leverage": leverage, "marginMode": "crossed",
        "unrealizedPL": "1", "achievedProfits": "0.5",
        "deductedFee": "0.2", "totalFee": "-0.1",
        "marginSize": "10", "takeProfit": "", "stopLoss": "", "autoMargin": "off",
    }


class EventTests(unittest.TestCase):
    def setUp(self):
        self.env=patch.dict(os.environ,{"FEE_REBATE_RATE":"0"})
        self.env.start()

    def tearDown(self):
        self.env.stop()

    def test_open_and_close(self):
        item = pos()
        key = bot.position_key(item)
        self.assertEqual(bot.detect_events({}, {key: item})[0][0], "opened")
        self.assertEqual(bot.detect_events({key: item}, {})[0][0], "closed")

    def test_increase_reduce_and_update(self):
        before, after = pos("1"), pos("2")
        key = bot.position_key(before)
        self.assertEqual(bot.detect_events({key: before}, {key: after})[0][0], "increased")
        self.assertEqual(bot.detect_events({key: after}, {key: before})[0][0], "reduced")
        changed = pos("1", leverage="20")
        self.assertEqual(bot.detect_events({key: before}, {key: changed})[0][0], "updated")

    def test_market_move_does_not_notify(self):
        before, after = pos(), pos()
        after["markPrice"], after["unrealizedPL"] = "110", "10"
        key = bot.position_key(before)
        self.assertEqual(bot.detect_events({key: before}, {key: after}), [])

    def test_take_profit_stop_loss_only_updates_dashboard(self):
        before, after = pos(), pos()
        after["takeProfit"], after["stopLoss"] = "110", "90"
        key = bot.position_key(before)
        self.assertEqual(bot.detect_events({key: before}, {key: after}), [])
        text = bot.format_pnl_report({key: after})
        self.assertIn("止盈/止损", text)
        self.assertIn("110", text)
        self.assertIn("90", text)

    def test_position_event_reuses_dashboard(self):
        class TelegramStub:
            def __init__(self):
                self.edits, self.sends = [], []
            def edit(self, message_id, text):
                self.edits.append((message_id, text))
            def send(self, text):
                self.sends.append(text)
                return 22

        with tempfile.TemporaryDirectory() as temp:
            store = bot.Store(Path(temp) / "test.sqlite3")
            store.set_meta(bot.PNL_MESSAGE_META, "11")
            telegram = TelegramStub()
            item = pos()
            key = bot.position_key(item)
            bot.sync_pnl_message(store, telegram, {key: item}, force_new=True)
            self.assertEqual(telegram.edits[0][0], 11)
            self.assertEqual(len(telegram.sends), 0)
            self.assertEqual(store.get_meta(bot.PNL_MESSAGE_META), "11")
            store.db.close()

    def test_store(self):
        with tempfile.TemporaryDirectory() as temp:
            store = bot.Store(Path(temp) / "test.sqlite3")
            item = pos()
            key = bot.position_key(item)
            store.save_cycle({key: item}, [("opened", key, None, item)], True)
            self.assertTrue(store.initialized())
            self.assertEqual(store.load_state()[key]["symbol"], "BTCUSDT")
            event_id = store.unsent_events()[0]["id"]
            store.mark_sent(event_id)
            self.assertEqual(store.unsent_events(), [])

    def test_history_reconciliation_catches_fast_position_once(self):
        with tempfile.TemporaryDirectory() as temp:
            store = bot.Store(Path(temp) / "test.sqlite3")
            history = {
                "positionId": "fast-1",
                "symbol": "SPCXUSDT",
                "holdSide": "long",
                "ctime": "2000",
                "utime": "15091",
                "openTotalPos": "15.02",
                "closeTotalPos": "15.02",
                "openAvgPrice": "127.87",
                "closeAvgPrice": "129.07",
                "pnl": "17.9",
                "openFee": "-1.1",
                "closeFee": "-1.2",
                "totalFunding": "0",
                "netProfit": "15.6",
            }
            self.assertEqual(
                store.record_history_positions("USDT-FUTURES", [history], 1000),
                1,
            )
            event = store.unsent_events()[0]
            before = json.loads(event["before_json"])
            self.assertEqual(event["event_type"], "closed")
            self.assertEqual(before["_history"]["positionId"], "fast-1")
            self.assertEqual(
                store.record_history_positions("USDT-FUTURES", [history], 1000),
                0,
            )
            self.assertEqual(len(store.unsent_events()), 1)

    def test_realtime_websocket_payloads(self):
        config = type("ConfigStub", (), {
            "api_key": "key",
            "api_secret": "secret",
            "api_passphrase": "pass",
        })()
        payload = bot.websocket_login_payload(config, now_ms=1234567890)
        self.assertEqual(payload["op"], "login")
        self.assertEqual(payload["args"][0]["timestamp"], "1234567890")
        self.assertTrue(payload["args"][0]["sign"])
        channels = bot.websocket_subscription_args(("USDT-FUTURES",))
        self.assertEqual(
            [item["channel"] for item in channels],
            ["positions", "positions-history"],
        )

    def test_holding_duration(self):
        start = 1000000
        item = {"cTime": str(start)}
        self.assertEqual(
            bot.holding_duration(item, now_ms=start + 93784000),
            "1天 02:03:04",
        )
        history = {"ctime": str(start), "utime": str(start + 3661000)}
        self.assertEqual(bot.holding_duration(history, closed=True), "01:01:01")

    def test_message(self):
        text = bot.format_event("opened", None, pos())
        self.assertIn("BTCUSDT", text)
        self.assertIn("📊 BITGET · 已开仓", text)
        self.assertIn("🔵 开仓均价", text)
        self.assertIn("🟠 标记价格", text)
        self.assertIn("手续费：", text)
        self.assertIn("返佣：", text)
        self.assertIn("净盈亏：</b><code>+1.2 USDT</code>", text)
        self.assertNotIn("净盈亏已计", text)

    def test_closed_message_uses_final_history(self):
        item = pos()
        item["_history"] = {
            "symbol": "BTCUSDT", "holdSide": "long", "closeTotalPos": "1",
            "openAvgPrice": "100", "closeAvgPrice": "102", "pnl": "2",
            "openFee": "-0.05", "closeFee": "-0.06",
            "totalFunding": "-0.01", "netProfit": "1.88",
        }
        text = bot.format_event("closed", item, None)
        self.assertIn("手续费：<code>−0.11</code>", text)
        self.assertIn("返佣：<code>0</code>", text)
        self.assertIn("净盈亏：</b><code>+1.88 USDT</code>", text)
        self.assertEqual(bot.history_net_pnl(item["_history"]), bot.Decimal("1.88"))

    def test_fee_rebate_calculation(self):
        self.assertEqual(bot.current_fee_rebate(pos()), bot.Decimal("0"))
        with patch.dict(os.environ, {"FEE_REBATE_RATE":"0.60"}):
            self.assertEqual(str(bot.current_fee_rebate(pos())), "0.120")
            history = {
                "openFee": "-0.05",
                "closeFee": "-0.06",
                "netProfit": "1.88",
            }
            self.assertEqual(str(bot.history_fee_rebate(history)), "0.0660")
            self.assertEqual(str(bot.history_net_pnl(history)), "1.9460")


if __name__ == "__main__":
    unittest.main()
