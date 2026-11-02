#!/usr/bin/env python3
import json
import logging
import os
import sqlite3
import threading
import time
from datetime import datetime
from decimal import Decimal, InvalidOperation
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, urlencode
from urllib.request import Request, urlopen

import bot
import web_admin

ROOT = Path(__file__).resolve().parent
STATIC_ROOT = ROOT / "web"
LOG = logging.getLogger("bitget-position-web")


def decimal_text(value) -> str:
    return str(bot.decimal(value))


def coin_for(item) -> str:
    coin = str(item.get("marginCoin") or item.get("settleCoin") or "")
    if coin:
        return coin
    symbol = str(item.get("symbol") or "")
    for suffix in ("USDT", "USDC", "USD"):
        if symbol.endswith(suffix):
            return suffix
    return ""


def timestamp_iso(value) -> str | None:
    try:
        millis = int(str(value or "0"))
    except ValueError:
        return None
    if millis <= 0:
        return None
    return datetime.fromtimestamp(millis / 1000).astimezone().isoformat(timespec="seconds")


def iso_milliseconds(timestamp=None):
    moment = datetime.now().astimezone() if timestamp is None else datetime.fromtimestamp(timestamp / 1000).astimezone()
    return moment.isoformat(timespec="milliseconds")


def finite_decimal(value):
    if value is None or str(value).strip() == "":
        return None
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return result if result.is_finite() else None


class PublicMarkPrices:
    """One shared public request per contract per second, including failures."""
    MAX_AGE_MS = 10000

    def __init__(self, api_base):
        self.api_base = api_base.rstrip("/")
        self._quotes = {}
        self._locks = {}
        self._guard = threading.Lock()

    def _fetch(self, product_type, symbol):
        query = urlencode({"productType": product_type, "symbol": symbol})
        request = Request(
            self.api_base + "/api/v2/mix/market/symbol-price?" + query,
            headers={"Accept": "application/json", "User-Agent": "bitget-position-web/1.0"},
        )
        with urlopen(request, timeout=3) as response:
            payload = json.loads(response.read().decode("utf-8"))
        if payload.get("code") != "00000" or not isinstance(payload.get("data"), list):
            raise ValueError("invalid public price response")
        row = next((item for item in payload["data"] if str(item.get("symbol")) == symbol), None)
        if row is None:
            raise ValueError("requested symbol missing")
        price = finite_decimal(row.get("markPrice"))
        timestamp = int(str(row.get("ts") or "0"))
        age = int(time.time() * 1000) - timestamp
        if price is None or price <= 0 or timestamp <= 0 or age < -5000 or age > self.MAX_AGE_MS:
            raise ValueError("invalid or stale mark price")
        return {"price": str(price), "timestamp": timestamp}

    def get(self, product_type, symbol):
        key = (product_type, symbol)
        with self._guard:
            lock = self._locks.setdefault(key, threading.Lock())
        with lock:
            cached = self._quotes.get(key)
            request_bucket = int(time.monotonic())
            if cached is None or cached.get("request_bucket") != request_bucket:
                try:
                    quote = self._fetch(product_type, symbol)
                    if cached and cached.get("timestamp") and quote["timestamp"] < cached["timestamp"]:
                        raise ValueError("out-of-order public price")
                    cached = {**quote, "healthy": True, "request_bucket": request_bucket}
                except Exception as exc:
                    if cached is None or cached.get("healthy"):
                        LOG.warning("public mark price unavailable for %s/%s (%s)", product_type, symbol, type(exc).__name__)
                    cached = {**(cached or {}), "healthy": False, "request_bucket": request_bucket}
                self._quotes[key] = cached
            result = dict(cached)
            timestamp = result.get("timestamp", 0)
            age = int(time.time() * 1000) - timestamp
            result["healthy"] = bool(result.get("healthy") and timestamp > 0 and -5000 <= age <= self.MAX_AGE_MS)
            return result


class DashboardData:
    def __init__(self, config: bot.Config):
        self.config = config
        self.client = bot.BitgetClient(config)
        self.mark_prices = PublicMarkPrices(config.api_base)
        self.history_limit = max(20, min(100, int(os.getenv("WEB_HISTORY_LIMIT", "100"))))
        self.history_ttl = max(30, int(os.getenv("WEB_HISTORY_TTL", "60")))
        start_raw = os.getenv(
            "WEB_HISTORY_START",
            "1970-01-01T00:00:00+00:00",
        ).strip()
        self.history_start = datetime.fromisoformat(start_raw)
        self.history_start_ms = int(self.history_start.timestamp() * 1000)
        self.history_start_label = self.history_start.strftime("%Y-%m-%d")
        opened_after_raw = os.getenv("WEB_HISTORY_OPENED_AFTER", "").strip()
        self.history_opened_after = (
            datetime.fromisoformat(opened_after_raw) if opened_after_raw else None
        )
        self.history_opened_after_ms = (
            int(self.history_opened_after.timestamp() * 1000)
            if self.history_opened_after is not None else None
        )
        self.excluded_history_position_ids = {
            item.strip()
            for item in os.getenv("WEB_EXCLUDED_HISTORY_POSITION_IDS", "").split(",")
            if item.strip()
        }
        self.retained_history_position_keys = {
            item.strip()
            for item in os.getenv("WEB_RETAINED_HISTORY_POSITION_KEYS", "").split(",")
            if item.strip()
        }
        self._history = []
        self._history_at = 0.0
        self._history_error = None
        self._lock = threading.Lock()
        self.account_ttl = max(10, int(os.getenv("WEB_ACCOUNT_TTL", "30")))
        self._accounts = []
        self._accounts_at = 0.0
        self._account_error = None
        self._account_lock = threading.Lock()

    def current_state(self):
        db_uri = f"file:{self.config.db_path}?mode=ro"
        with sqlite3.connect(db_uri, uri=True, timeout=5) as db:
            db.row_factory = sqlite3.Row
            db.execute("BEGIN")
            rows = db.execute(
                "SELECT position_key,payload,updated_at FROM position_state ORDER BY position_key"
            ).fetchall()
            meta = db.execute(
                "SELECT value FROM metadata WHERE key='initialized'"
            ).fetchone()
        positions = [json.loads(row["payload"]) for row in rows]
        updated_at = meta["value"] if meta else None
        return positions, updated_at

    def history_record_in_window(self, item):
        try:
            closed_ms = int(item.get("utime") or 0)
            opened_ms = int(item.get("ctime") or item.get("cTime") or 0)
        except (TypeError, ValueError):
            return False
        position_id = str(item.get("positionId") or "")
        if position_id in self.excluded_history_position_ids:
            return False
        position_key = bot.history_position_key(str(item.get("productType") or ""), item)
        if position_key in self.retained_history_position_keys:
            return True
        if closed_ms < self.history_start_ms:
            return False
        # A reset starts a new ledger: an older position closing later stays excluded.
        if (self.history_opened_after_ms is not None
                and opened_ms < self.history_opened_after_ms):
            return False
        return True

    def history(self):
        now = time.monotonic()
        with self._lock:
            if self._history_at and now - self._history_at < self.history_ttl:
                return list(self._history), self._history_error
            try:
                records = []
                if self.client.uta:
                    # Classic history is not returned by UTA after an account upgrade.
                    # Retain already-confirmed local settlements; remote data wins below.
                    with sqlite3.connect(f"file:{self.config.db_path}?mode=ro", uri=True, timeout=5) as db:
                        saved = db.execute("SELECT before_json FROM position_events WHERE event_type='closed'").fetchall()
                    for (raw,) in saved:
                        try:
                            before = json.loads(raw) if raw else {}
                            history = before.get("_history") if isinstance(before, dict) else None
                            product = before.get("productType") if isinstance(before, dict) else None
                            if product in self.config.product_types and bot.history_complete(history):
                                records.append(dict(history, productType=product))
                        except (TypeError, ValueError):
                            continue
                for product_type in self.config.product_types:
                    if self.client.uta:
                        data = self.client._get({"category": product_type, "limit": str(self.history_limit)}, "/api/v3/position/history-position")
                        rows = [bot.normalize_history(item, product_type) for item in bot.unwrap_list(data)]
                    else:
                        data = self.client._get(
                            {"productType": product_type, "limit": str(self.history_limit)},
                            self.client.HISTORY_PATH,
                        )
                        rows = (data or {}).get("list") or []
                    for item in rows:
                        record = dict(item)
                        record["productType"] = product_type
                        records.append(record)
                if self.client.uta:
                    records = list({bot.history_position_key(item["productType"], item): item for item in records}.values())
                records = [
                    item for item in records
                    if self.history_record_in_window(item)
                ]
                records.sort(key=lambda item: int(item.get("utime") or 0), reverse=True)
                self._history = records[: self.history_limit]
                self._history_at = now
                self._history_error = None
            except Exception as exc:
                LOG.warning("history refresh failed: %s", exc)
                self._history_error = "历史数据暂时不可用"
                if not self._history:
                    self._history_at = now
            return list(self._history), self._history_error

    def accounts(self):
        now = time.monotonic()
        with self._account_lock:
            if self._accounts_at and now - self._accounts_at < self.account_ttl:
                return list(self._accounts), self._account_error
            try:
                records = []
                for product_type in self.config.product_types:
                    for item in self.client.accounts(product_type):
                        record = dict(item)
                        record["productType"] = product_type
                        records.append(record)
                self._accounts = records
                self._accounts_at = now
                self._account_error = None
            except Exception as exc:
                LOG.warning("account refresh failed: %s", exc)
                self._account_error = "账户余额暂时不可用"
                self._accounts_at = now
            return list(self._accounts), self._account_error

    def positions_payload(self):
        # Capture the observation watermark before network work, so a delayed
        # response cannot replace a newer position observation in the browser.
        positions_updated_at = iso_milliseconds()
        positions, monitor_updated_at = self.current_state()
        monitor_age = None
        snapshot_ms = 0
        if monitor_updated_at:
            try:
                monitor_time = datetime.fromisoformat(monitor_updated_at)
                snapshot_ms = int(monitor_time.timestamp() * 1000)
                monitor_age = max(0, int(time.time() - monitor_time.timestamp()))
            except (TypeError, ValueError, OverflowError):
                pass
        projected = []
        for raw in positions:
            item = dict(raw)
            item["_quoteUpdatedAt"] = iso_milliseconds(snapshot_ms) if snapshot_ms else None
            item["_quoteHealthy"] = False
            product_type = str(item.get("productType") or "")
            settlement = {"USDT-FUTURES": "USDT", "USDC-FUTURES": "USDC"}.get(product_type)
            baseline_price = finite_decimal(item.get("markPrice"))
            baseline_pnl = finite_decimal(item.get("unrealizedPL"))
            quantity = finite_decimal(item.get("total"))
            side = str(item.get("holdSide") or "")
            # Inverse/unknown contracts retain the exchange snapshot. Applying
            # a linear formula to a coin-settled contract would be incorrect.
            supported = settlement is not None and coin_for(item) == settlement
            valid = (baseline_price is not None and baseline_price > 0 and
                     baseline_pnl is not None and quantity is not None and quantity > 0 and
                     side in {"long", "short"})
            if supported and valid:
                quote = self.mark_prices.get(product_type, str(item.get("symbol") or ""))
                price = finite_decimal(quote.get("price"))
                timestamp = quote.get("timestamp", 0)
                # Never replace a newer account snapshot with an older quote,
                # even when the quote remains within its freshness threshold.
                usable = price is not None and price > 0 and timestamp > 0 and timestamp >= snapshot_ms
                if usable:
                    direction = Decimal("1") if side == "long" else Decimal("-1")
                    item["markPrice"] = str(price)
                    item["unrealizedPL"] = str(baseline_pnl + direction * quantity * (price - baseline_price))
                    item["_quoteUpdatedAt"] = iso_milliseconds(timestamp)
                    item["_quoteHealthy"] = bool(quote.get("healthy"))
            projected.append(item)
        positions = projected
        now_ms = int(time.time() * 1000)
        current = []
        current_totals = {}
        total_margin = {}
        for item in positions:
            coin = coin_for(item)
            net = bot.current_net_pnl(item)
            gross = bot.decimal(item.get("unrealizedPL"))
            margin = bot.decimal(item.get("marginSize"))
            previous = current_totals.get(coin, Decimal("0"))
            current_totals[coin] = None if net is None or previous is None else previous + net
            total_margin[coin] = total_margin.get(coin, Decimal("0")) + margin
            fee = -abs(bot.decimal(item.get("deductedFee")))
            rebate = bot.current_fee_rebate(item)
            current.append({
                "symbol": str(item.get("symbol") or "-"),
                "productType": str(item.get("productType") or ""),
                "quoteUpdatedAt": item.get("_quoteUpdatedAt"),
                "quoteHealthy": bool(item.get("_quoteHealthy")),
                "side": bot.side_name(item.get("holdSide")),
                "sideRaw": str(item.get("holdSide") or ""),
                "size": decimal_text(item.get("total")),
                "leverage": decimal_text(item.get("leverage")),
                "marginMode": str(item.get("marginMode") or "-"),
                "openPrice": decimal_text(item.get("openPriceAvg")),
                "markPrice": decimal_text(item.get("markPrice")),
                "holdingTime": bot.holding_duration(item, now_ms=now_ms),
                "unrealizedPnl": str(gross),
                "realizedPnl": decimal_text(item.get("achievedProfits")),
                "transactionFee": str(fee),
                "fundingFee": decimal_text(item.get("totalFee")),
                "rebate": str(rebate),
                "netPnl": str(net) if net is not None else "—",
                "grossRoi": bot.roi(item),
                "netRoi": bot.roi(item, net=True),
                "margin": str(margin),
                "coin": coin,
                "takeProfit": str(item.get("takeProfit") or "-"),
                "stopLoss": str(item.get("stopLoss") or "-"),
                "openedAt": timestamp_iso(item.get("cTime") or item.get("ctime")),
            })

        return {
            "generatedAt": iso_milliseconds(),
            "positionsUpdatedAt": positions_updated_at,
            "accountMode": "unified" if self.client.uta else "classic",
            "monitorUpdatedAt": monitor_updated_at,
            "monitorAgeSeconds": monitor_age,
            "monitorHealthy": monitor_age is not None and monitor_age <= 90,
            "quotesHealthy": all(item["quoteHealthy"] for item in current),
            "positions": current,
            "currentTotals": [
                {"coin": coin, "netPnl": str(value) if value is not None else "—", "margin": str(total_margin.get(coin, Decimal("0")))}
                for coin, value in sorted(current_totals.items())
            ],
        }

    def payload(self):
        history, history_error = self.history()
        accounts, account_error = self.accounts()
        live = self.positions_payload()

        profit = Decimal("0")
        loss = Decimal("0")
        net_history = Decimal("0")
        wins = 0
        recent = []
        for item in history:
            net = bot.history_net_pnl(item)
            transaction_fee = -(
                abs(bot.decimal(item.get("openFee")))
                + abs(bot.decimal(item.get("closeFee")))
            )
            funding_fee = bot.decimal(item.get("totalFunding"))
            rebate = bot.history_fee_rebate(item)
            net_history += net
            if net > 0:
                profit += net
                wins += 1
            elif net < 0:
                loss += abs(net)
            recent.append({
                "symbol": str(item.get("symbol") or "-"),
                "side": bot.side_name(item.get("holdSide")),
                "sideRaw": str(item.get("holdSide") or ""),
                "size": decimal_text(item.get("closeTotalPos")),
                "openPrice": decimal_text(item.get("openAvgPrice")),
                "closePrice": decimal_text(item.get("closeAvgPrice")),
                "holdingTime": bot.holding_duration(item, closed=True),
                "pnl": decimal_text(item.get("pnl")),
                "transactionFee": str(transaction_fee),
                "fundingFee": str(funding_fee),
                "rebate": str(rebate),
                "fees": str(transaction_fee + funding_fee),
                "exchangeNetPnl": decimal_text(item.get("netProfit")),
                "netPnl": str(net) if net is not None else "—",
                "coin": coin_for(item),
                "closedAt": timestamp_iso(item.get("utime")),
            })

        trades = len(history)
        if loss > 0:
            factor = f"{profit / loss:.2f}"
        elif profit > 0:
            factor = "∞"
        else:
            factor = "—"
        win_rate = Decimal(wins * 100) / Decimal(trades) if trades else Decimal("0")
        average_net = net_history / Decimal(trades) if trades else Decimal("0")

        return {
            **live,
            "refreshSeconds": 30,
            "rebateRate": str(bot.fee_rebate_rate()),
            "historyStart": self.history_start_label,
            "historyStartAt": self.history_start.isoformat(timespec="milliseconds"),
            "historyOpenedAfter": (
                self.history_opened_after.isoformat(timespec="milliseconds")
                if self.history_opened_after is not None else None
            ),
            "accounts": [
                {
                    "productType": str(item.get("productType") or ""),
                    "coin": str(item.get("marginCoin") or ""),
                    "accountEquity": decimal_text(item.get("accountEquity")),
                    "available": decimal_text(item.get("available")),
                    "locked": decimal_text(item.get("locked")),
                    "unrealizedPnl": decimal_text(item.get("unrealizedPL")),
                    "assetMode": str(item.get("assetMode") or ""),
                }
                for item in accounts
            ],
            "stats": {
                "profitFactor": factor,
                "trades": trades,
                "wins": wins,
                "winRate": f"{win_rate:.2f}",
                "grossProfit": str(profit),
                "grossLoss": str(loss),
                "netProfit": str(net_history),
                "averageNet": str(average_net),
            },
            "history": recent,
            "historyError": history_error,
            "accountError": account_error,
        }


bot.load_env_file(bot.notification_settings.PROJECT / "config/.env")
CONFIG = bot.Config.from_env()
WEB_HOST = os.getenv("WEB_HOST", "127.0.0.1").strip()
WEB_PORT = int(os.getenv("WEB_PORT", "8091"))
DATA = DashboardData(CONFIG)


class Handler(BaseHTTPRequestHandler):
    server_version = "BitgetDashboard/1.0"

    def log_message(self, fmt, *args):
        LOG.info("%s %s", self.address_string(), fmt % args)

    def security_headers(self):
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self'; style-src 'self'; "
            "img-src 'self' data:; connect-src 'self'; object-src 'none'; "
            "base-uri 'none'; frame-ancestors 'none'",
        )

    def write_bytes(self, status, body, content_type, cache_control="no-store"):
        self.send_response(status)
        self.security_headers()
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", cache_control)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        path = urlparse(self.path).path
        if path.startswith("/bitget/"): path = path[len("/bitget"): ]
        if not web_admin.handle(self, DATA, path):
            self.write_bytes(404, b"Not found", "text/plain; charset=utf-8")

    def do_GET(self):
        path = urlparse(self.path).path
        if path.startswith("/bitget/"): path = path[len("/bitget"): ]
        if path in {"/", "/index.html"}:
            if not web_admin._secure(self):
                self.send_response(302)
                self.security_headers()
                self.send_header("Location", web_admin.ORIGIN + "/bitget/")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            if not web_admin.is_authenticated(self):
                try:
                    page = (STATIC_ROOT / "login.html").read_bytes()
                except OSError:
                    self.write_bytes(503, b"Login temporarily unavailable", "text/plain; charset=utf-8")
                    return
                self.write_bytes(200, page, "text/html; charset=utf-8")
                return
        if path in {"/api/dashboard", "/api/positions/live"}:
            if not web_admin.require_login(self):
                return
        if web_admin.handle(self, DATA, path):
            return
        if path == "/healthz":
            self.write_bytes(
                200,
                b'{"ok":true}',
                "application/json; charset=utf-8",
                "no-store",
            )
            return
        if path == "/api/positions/live":
            try:
                body = json.dumps(DATA.positions_payload(), ensure_ascii=False).encode("utf-8")
                self.write_bytes(200, body, "application/json; charset=utf-8")
            except Exception:
                LOG.exception("live positions request failed")
                body = json.dumps({"error": "持仓行情暂时不可用，请稍后重试。"}, ensure_ascii=False).encode("utf-8")
                self.write_bytes(503, body, "application/json; charset=utf-8")
            return
        if path == "/api/dashboard":
            try:
                body = json.dumps(DATA.payload(), ensure_ascii=False).encode("utf-8")
                self.write_bytes(200, body, "application/json; charset=utf-8")
            except Exception:
                LOG.exception("dashboard request failed")
                body = json.dumps(
                    {"error": "数据读取失败，请稍后重试。"},
                    ensure_ascii=False,
                ).encode("utf-8")
                self.write_bytes(503, body, "application/json; charset=utf-8")
            return

        files = {
            "/": ("index.html", "text/html; charset=utf-8"),
            "/index.html": ("index.html", "text/html; charset=utf-8"),
            "/styles.css": ("assets/css/dashboard.css", "text/css; charset=utf-8"),
            "/app.js": ("assets/js/dashboard.js", "application/javascript; charset=utf-8"),
            "/lucide.js": ("assets/vendor/lucide.js", "application/javascript; charset=utf-8"),
            "/brand.ico": ("assets/brand.ico", "image/x-icon"),
            "/favicon.ico": ("assets/brand.ico", "image/x-icon"),
        }
        files.update({
            "/assets/css/access.css": ("assets/css/access.css", "text/css; charset=utf-8"),
            "/assets/js/access.js": ("assets/js/access.js", "application/javascript; charset=utf-8"),
            "/assets/js/session.js": ("assets/js/session.js", "application/javascript; charset=utf-8"),
            "/assets/css/dashboard.css": ("assets/css/dashboard.css", "text/css; charset=utf-8"),
            "/assets/css/workspace.css": ("assets/css/workspace.css", "text/css; charset=utf-8"),
            "/assets/js/dashboard.js": ("assets/js/dashboard.js", "application/javascript; charset=utf-8"),
            "/assets/js/workspace.js": ("assets/js/workspace.js", "application/javascript; charset=utf-8"),
            "/assets/vendor/lucide.js": ("assets/vendor/lucide.js", "application/javascript; charset=utf-8"),
            "/assets/brand.ico": ("assets/brand.ico", "image/x-icon"),
        })
        target = files.get(path)
        if not target:
            self.write_bytes(404, b"Not found", "text/plain; charset=utf-8")
            return
        file_name, content_type = target
        try:
            body = (STATIC_ROOT / file_name).read_bytes()
        except OSError:
            self.write_bytes(404, b"Not found", "text/plain; charset=utf-8")
            return
        cache = "no-store" if file_name == "index.html" else "public, max-age=300"
        self.write_bytes(200, body, content_type, cache)


def main():
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(message)s",
    )
    server = ThreadingHTTPServer((WEB_HOST, WEB_PORT), Handler)
    LOG.info("web dashboard listening on %s:%s", WEB_HOST, WEB_PORT)
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
