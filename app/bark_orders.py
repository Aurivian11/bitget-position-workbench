"""Read-only Bitget opening-fill observer with a durable Bark outbox."""
from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
import threading
import time
from contextlib import contextmanager
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from zoneinfo import ZoneInfo

from bark_transport import BarkError
from uta_orders import normalize_uta_fill, FILL_PATH as UTA_FILL_PATH, PENDING_PATH as UTA_PENDING_PATH

LOG = logging.getLogger("bitget-bark-orders")
FILL_PATH = "/api/v2/mix/order/fill-history"
PENDING_PATH = "/api/v2/mix/order/orders-pending"
WEEK_MS = 7 * 24 * 60 * 60 * 1000
OVERLAP_MS = 120_000


def number(value):
    try:
        result = Decimal(str(value or "0"))
        return result if result.is_finite() else Decimal(0)
    except (InvalidOperation, ValueError, TypeError):
        return Decimal(0)


def text_number(value):
    result = format(number(value), "f")
    return result.rstrip("0").rstrip(".") if "." in result else result


def millis(value):
    try:
        return max(0, int(value or 0))
    except (ValueError, TypeError, OverflowError):
        return 0


def opening_direction(raw):
    if not isinstance(raw, dict) or str(raw.get("tradeSide", "")).lower() != "open":
        return None
    if str(raw.get("posMode", "")).lower() in {"one_way_mode", "one-way-mode"}:
        return None
    if str(raw.get("reduceOnly", "")).lower() in {"yes", "true", "1"}:
        return None
    side = str(raw.get("posSide", "")).lower()
    if side in {"long", "short"}:
        return side
    return {"buy": "long", "sell": "short"}.get(str(raw.get("side", "")).lower())


def normalize_fill(raw, product_type, source):
    direction = opening_direction(raw)
    if direction is None or source not in {"ws", "rest"}:
        return None
    order_id = str(raw.get("orderId") or "")
    symbol = str(raw.get("instId") or raw.get("symbol") or "")
    if not order_id or not symbol:
        return None
    if source == "ws":
        status = str(raw.get("status") or "").lower()
        if status not in {"partially_filled", "filled", "canceled", "cancelled"}:
            return None
        cumulative = number(raw.get("accBaseVolume"))
        quantity = cumulative if cumulative > 0 else number(raw.get("baseVolume"))
        # cTime is order creation; uTime can be cancellation. Only fillTime proves a new fill.
        fill_ms = millis(raw.get("fillTime"))
        price = number(raw.get("priceAvg")) or number(raw.get("fillPrice"))
        quantity_label = "累计已成交" if cumulative > 0 else "本笔成交"
    else:
        status = "confirmed_fill"
        quantity = number(raw.get("baseVolume"))
        price = number(raw.get("price"))
        fill_ms = millis(raw.get("cTime"))
        quantity_label = "本笔成交"
    if quantity <= 0 or price <= 0 or not fill_ms:
        return None
    return {
        "product_type": product_type, "order_id": order_id,
        "trade_id": str(raw.get("tradeId") or ""), "symbol": symbol,
        "direction": direction, "price": text_number(price),
        "quantity": text_number(quantity), "quantity_label": quantity_label,
        "margin_coin": str(raw.get("marginCoin") or "USDT"),
        "fill_ms": fill_ms, "status": status, "source": source,
    }


def message_for(event):
    symbol = event["symbol"]
    base = symbol
    for suffix in ("USDT", "USDC", "USD"):
        if base.endswith(suffix):
            base = base[:-len(suffix)]
            break
    direction = "做多" if event["direction"] == "long" else "做空"
    status = {
        "partially_filled": "部分成交，开仓已发生",
        "filled": "开仓订单已全部成交",
    }.get(event["status"], "开仓订单已确认有成交")
    stamp = datetime.fromtimestamp(event["fill_ms"] / 1000, ZoneInfo("Asia/Shanghai"))
    title = f"Bitget 已开仓 · {base} {direction}"
    body = (
        f"{symbol} · {direction}\n{status}\n"
        f"成交价：{event['price']} {event['margin_coin']}\n"
        f"{event['quantity_label']}：{event['quantity']} {base}\n"
        f"订单：{event['order_id']}\n"
        f"成交时间：{stamp:%m-%d %H:%M:%S}（北京时间）"
    )
    return title, body


class BarkOrderDispatcher:
    def __init__(self, db_path, client, transport, *, reconcile_seconds=30, now_ms=None):
        self.db_path = Path(db_path)
        self.client = client
        self.transport = transport
        self.products = tuple(client.config.product_types)
        self.account_tag = hashlib.sha256(client.config.api_key.encode()).hexdigest()[:20]
        self.reconcile_seconds = max(5, float(reconcile_seconds))
        self.now_ms = now_ms or (lambda: int(time.time() * 1000))
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._reconcile_requested = threading.Event()
        self._thread = None
        self.baseline = None
        self._prepare()

    @contextmanager
    def _connect(self):
        db = sqlite3.connect(self.db_path, timeout=5)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def _prepare(self):
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("PRAGMA synchronous=FULL")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS bark_metadata (
                    key TEXT PRIMARY KEY, value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS bark_open_orders (
                    event_key TEXT PRIMARY KEY, account_tag TEXT NOT NULL,
                    product_type TEXT NOT NULL, order_id TEXT NOT NULL,
                    symbol TEXT NOT NULL, fill_ms INTEGER NOT NULL,
                    payload_json TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'pending',
                    attempts INTEGER NOT NULL DEFAULT 0, next_attempt_at REAL NOT NULL DEFAULT 0,
                    created_at REAL NOT NULL, sent_at REAL, last_error TEXT NOT NULL DEFAULT ''
                );
                CREATE INDEX IF NOT EXISTS bark_pending
                    ON bark_open_orders(account_tag, state, next_attempt_at, fill_ms);
            """)

    def _meta_key(self, key):
        return self.account_tag + ":" + key

    def _get_meta(self, key):
        with self._connect() as db:
            row = db.execute("SELECT value FROM bark_metadata WHERE key=?", (self._meta_key(key),)).fetchone()
        return row[0] if row else None

    def _set_meta(self, key, value):
        with self._connect() as db:
            db.execute("INSERT INTO bark_metadata(key,value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (self._meta_key(key), str(value)))

    def _event_key(self, product, order_id):
        return f"{self.account_tag}:{product}:{order_id}"

    def _pages(self, product, path, params, list_key):
        uta = bool(getattr(self.client, 'uta', False))
        if uta:
            paths = {FILL_PATH: UTA_FILL_PATH, PENDING_PATH: UTA_PENDING_PATH}
            if path not in paths:
                raise ValueError("Unsupported UTA observation endpoint")
            path, list_key = paths[path], 'list'
            params = {k: v for k, v in params.items() if k != 'status'}
        cursor = None
        cursors = set()
        while not self._stop.is_set():
            query = {('category' if uta else 'productType'): product, "limit": "100", **params}
            if cursor:
                query['cursor' if uta else 'idLessThan'] = cursor
            data = self.client._get(query, path=path)
            if not isinstance(data, dict) or list_key not in data or (data[list_key] is not None and not isinstance(data[list_key], list)):
                raise ValueError("Unexpected Bitget page shape")
            rows = data[list_key] or []
            if not all(isinstance(row, dict) for row in rows):
                raise ValueError("Unexpected Bitget row shape")
            yield rows
            next_cursor = str(data.get('cursor' if uta else 'endId') or '')
            if not rows or (len(rows) < 100 and (not uta or not next_cursor)):
                return
            if not next_cursor or next_cursor in cursors:
                raise ValueError("Incomplete Bitget pagination")
            cursor = next_cursor
            cursors.add(cursor)
            if self._stop.wait(0.15):
                break
        raise InterruptedError("Bark observer stopped")

    def bootstrap(self):
        saved = self._get_meta("baseline_ms")
        if saved is not None:
            self.baseline = int(saved)
            return
        partials = []
        for product in self.products:
            for rows in self._pages(product, PENDING_PATH, {"status": "partially_filled"}, "entrustedList"):
                for raw in rows:
                    if getattr(self.client, 'uta', False):
                        # Suppress pre-existing partial orders even when UTA omits tradeSide.
                        partial = str(raw.get('orderStatus') or '').lower() == 'partially_filled'
                        if partial and number(raw.get('cumExecQty')) > 0 and raw.get('orderId'):
                            partials.append((product, raw))
                    elif opening_direction(raw) and number(raw.get("baseVolume")) > 0 and raw.get("orderId"):
                        partials.append((product, raw))
        boundary = self.now_ms()
        with self._connect() as db:
            db.execute("INSERT OR IGNORE INTO bark_metadata(key,value) VALUES (?,?)", (self._meta_key("baseline_ms"), str(boundary)))
            self.baseline = int(db.execute("SELECT value FROM bark_metadata WHERE key=?", (self._meta_key("baseline_ms"),)).fetchone()[0])
            for product, raw in partials:
                order_id = str(raw["orderId"])
                db.execute("""INSERT OR IGNORE INTO bark_open_orders
                    (event_key,account_tag,product_type,order_id,symbol,fill_ms,payload_json,state,created_at)
                    VALUES (?,?,?,?,?,?,?,'ignored',?)""", (
                        self._event_key(product, order_id), self.account_tag, product, order_id,
                        str(raw.get("symbol") or ""), self.baseline, "{}", time.time(),
                    ))
        LOG.info("Bark opening baseline established; existing partially filled orders ignored=%s", len(partials))

    def record(self, event):
        if not event or self.baseline is None or event["fill_ms"] < self.baseline or event["product_type"] not in self.products:
            return False
        with self._connect() as db:
            inserted = db.execute("""INSERT OR IGNORE INTO bark_open_orders
                (event_key,account_tag,product_type,order_id,symbol,fill_ms,payload_json,created_at)
                VALUES (?,?,?,?,?,?,?,?)""", (
                    self._event_key(event["product_type"], event["order_id"]), self.account_tag,
                    event["product_type"], event["order_id"], event["symbol"], event["fill_ms"],
                    json.dumps(event, ensure_ascii=False), time.time(),
                )).rowcount == 1
        if inserted:
            LOG.info("Bark opening fill queued symbol=%s order=%s source=%s", event["symbol"], event["order_id"], event["source"])
            self._wake.set()
        return inserted

    def on_orders(self, product, rows):
        if product not in self.products or not isinstance(rows, list):
            return
        for raw in rows:
            event = normalize_fill(raw, product, "ws")
            if event:
                self.record(event)
            elif opening_direction(raw) and (number(raw.get("accBaseVolume")) > 0 or number(raw.get("baseVolume")) > 0):
                self.request_reconcile()

    def on_uta_fills(self, rows):
        if not getattr(self.client, 'uta', False) or not isinstance(rows, list):
            return
        for raw in rows:
            normalized = normalize_uta_fill(raw)
            if normalized is None:
                continue
            event = normalize_fill(normalized, normalized['productType'], 'rest')
            if event:
                event['source'] = 'ws'
                self.record(event)

    def request_reconcile(self):
        self._reconcile_requested.set()
        self._wake.set()

    def reconcile(self):
        for product in self.products:
            previous = int(self._get_meta("watermark:" + product) or self.baseline)
            start = max(self.baseline, previous - OVERLAP_MS)
            end = min(self.now_ms(), start + WEEK_MS - 1)
            if end <= start:
                continue
            for rows in self._pages(product, FILL_PATH, {"startTime": str(start), "endTime": str(end)}, "fillList"):
                if getattr(self.client, 'uta', False):
                    rows = [mapped for raw in rows if (mapped := normalize_uta_fill(raw)) is not None
                            and mapped['productType'] == product]
                for raw in sorted(rows, key=lambda item: millis(item.get("cTime"))):
                    self.record(normalize_fill(raw, product, "rest"))
            # Never advance on a failed or incomplete page, or from a websocket event.
            self._set_meta("watermark:" + product, end)
            self._set_meta("last_rest_ok", self.now_ms())

    def deliver_pending(self):
        with self._connect() as db:
            rows = db.execute("""SELECT * FROM bark_open_orders
                WHERE account_tag=? AND state='pending' AND next_attempt_at<=?
                ORDER BY fill_ms,event_key LIMIT 20""", (self.account_tag, time.time())).fetchall()
        for row in rows:
            if self._stop.is_set():
                return
            attempts = row["attempts"] + 1
            try:
                self.transport.send(*message_for(json.loads(row["payload_json"])))
            except Exception as exc:
                permanent = isinstance(exc, BarkError) and exc.permanent
                delay = max(min(300, 2 ** min(attempts, 9)), float(getattr(exc, "retry_after", None) or 0))
                reason = str(exc) if isinstance(exc, BarkError) else type(exc).__name__
                with self._connect() as db:
                    db.execute("UPDATE bark_open_orders SET state=?,attempts=?,next_attempt_at=?,last_error=? WHERE event_key=?", (
                        "failed" if permanent else "pending", attempts, time.time() + delay, reason[:180], row["event_key"],
                    ))
                LOG.warning("Bark delivery %s symbol=%s order=%s (%s)", "failed permanently" if permanent else "will retry", row["symbol"], row["order_id"], reason)
            else:
                with self._connect() as db:
                    db.execute("UPDATE bark_open_orders SET state='sent',attempts=?,sent_at=?,last_error='' WHERE event_key=?", (attempts, time.time(), row["event_key"]))
                self._set_meta("last_delivery_ok", self.now_ms())
                LOG.info("Bark opening push accepted symbol=%s order=%s", row["symbol"], row["order_id"])

    def start(self):
        try:
            self.bootstrap()
        except Exception as exc:
            LOG.warning("Bark observer initializing; baseline will retry (%s)", type(exc).__name__)
        self._thread = threading.Thread(target=self._run, name="bitget-bark-orders", daemon=True)
        self._thread.start()
        if self.baseline is not None:
            LOG.info("Bark opening-fill observer enabled for %s", ",".join(self.products))

    def _run(self):
        next_reconcile = 0
        bootstrap_backoff = 1
        while not self._stop.is_set():
            self._wake.clear()
            if self.baseline is None:
                try:
                    self.bootstrap()
                except Exception as exc:
                    LOG.warning("Bark baseline not ready; retrying (%s)", type(exc).__name__)
                    self._stop.wait(bootstrap_backoff)
                    bootstrap_backoff = min(30, bootstrap_backoff * 2)
                    continue
                LOG.info("Bark opening-fill observer enabled for %s", ",".join(self.products))
            try:
                self.deliver_pending()
                if time.monotonic() >= next_reconcile or self._reconcile_requested.is_set():
                    self._reconcile_requested.clear()
                    try:
                        self.reconcile()
                    except Exception as exc:
                        LOG.warning("Bark fill reconciliation failed; cursor retained (%s)", type(exc).__name__)
                    next_reconcile = time.monotonic() + self.reconcile_seconds
                    self.deliver_pending()
            except Exception as exc:
                LOG.warning("Bark worker cycle failed (%s)", type(exc).__name__)
            self._wake.wait(1)

    def stop(self):
        self._stop.set()
        self._wake.set()
        if self._thread:
            self._thread.join(timeout=12)
