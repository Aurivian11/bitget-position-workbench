#!/usr/bin/env python3
import base64
import hashlib
import hmac
import html
import json
import logging
import os
import queue
import signal
import sqlite3
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP, ROUND_CEILING, ROUND_FLOOR, localcontext
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import notification_settings
from bitget_clock import get_clock
from position_costs import PositionCostCache
from uta_adapter import normalize_position, normalize_history, unwrap_list, apply_tpsl, UTAPositionCostCache
from close_history import history_complete, history_matches, reconcile_closed_records

from telegram_transport import Telegram, TelegramError, TelegramMessageMissing
from version import __version__
from telegram_board import sync_multipart_board

ROOT = Path(__file__).resolve().parent
STOP = False
REALTIME_SIGNALS = None
LOG = logging.getLogger("bitget-position-tg")
EVENT_LABELS = {
    "opened": "开仓",
    "increased": "加仓",
    "reduced": "减仓",
    "closed": "平仓",
    "updated": "持仓参数更新",
}


def load_env_file(path: Path) -> None:
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        os.environ.setdefault(key, value)


@dataclass(frozen=True)
class Config:
    api_key: str
    api_secret: str
    api_passphrase: str
    telegram_token: str
    telegram_chat_id: str
    product_types: tuple[str, ...]
    margin_coin: str
    poll_seconds: int
    snapshot_seconds: int
    request_timeout: int
    send_startup_summary: bool
    db_path: Path
    api_base: str
    discord_webhook_url: str = field(default="", repr=False)
    discord_update_seconds: int = 10
    telegram_update_seconds: float = 1.0

    @classmethod
    def from_env(cls) -> "Config":
        load_env_file(notification_settings.PROJECT / "config/.env")
        notification_settings.apply_environment()
        values = {
            "BITGET_API_KEY": os.getenv("BITGET_API_KEY", "").strip(),
            "BITGET_API_SECRET": os.getenv("BITGET_API_SECRET", "").strip(),
            "BITGET_API_PASSPHRASE": os.getenv("BITGET_API_PASSPHRASE", "").strip(),
            "TELEGRAM_BOT_TOKEN": os.getenv("TELEGRAM_BOT_TOKEN", "").strip(),
            "TELEGRAM_CHAT_ID": os.getenv("TELEGRAM_CHAT_ID", "").strip(),
        }
        missing = [key for key, value in values.items() if not value and (not key.startswith("TELEGRAM_") or notification_settings.enabled("telegram"))]
        if missing:
            raise ValueError("缺少配置: " + ", ".join(missing))
        products = tuple(
            item.strip().upper()
            for item in os.getenv("BITGET_PRODUCT_TYPES", "USDT-FUTURES").split(",")
            if item.strip()
        )
        allowed = {"USDT-FUTURES", "USDC-FUTURES", "COIN-FUTURES"}
        invalid = set(products) - allowed
        if not products or invalid:
            raise ValueError(f"BITGET_PRODUCT_TYPES 无效: {sorted(invalid)}")
        return cls(
            api_key=values["BITGET_API_KEY"],
            api_secret=values["BITGET_API_SECRET"],
            api_passphrase=values["BITGET_API_PASSPHRASE"],
            telegram_token=values["TELEGRAM_BOT_TOKEN"],
            telegram_chat_id=values["TELEGRAM_CHAT_ID"],
            product_types=products,
            margin_coin=os.getenv("BITGET_MARGIN_COIN", "").strip().upper(),
            poll_seconds=max(2, int(os.getenv("POLL_SECONDS", "5"))),
            snapshot_seconds=max(30, int(os.getenv("SNAPSHOT_SECONDS", "60"))),
            request_timeout=max(5, int(os.getenv("REQUEST_TIMEOUT", "15"))),
            send_startup_summary=os.getenv("SEND_STARTUP_SUMMARY", "1").lower() in {"1", "true", "yes"},
            db_path=notification_settings.PROJECT / os.getenv("DATABASE_PATH", "data/positions.sqlite3"),
            api_base=os.getenv("BITGET_API_BASE", "https://api.bitget.com").rstrip("/"),
            discord_webhook_url=os.getenv("DISCORD_WEBHOOK_URL", "").strip(),
            discord_update_seconds=max(1, int(os.getenv("DISCORD_UPDATE_SECONDS", "10"))),
            telegram_update_seconds=max(1.0, float(os.getenv("TELEGRAM_UPDATE_SECONDS", "1"))),
        )


def decimal(value: Any) -> Decimal:
    try:
        return Decimal(str(value or "0"))
    except (InvalidOperation, ValueError):
        return Decimal("0")


def sign(secret: str, timestamp: str, method: str, path: str, query: str = "", body: str = "") -> str:
    suffix = f"?{query}" if query else ""
    payload = f"{timestamp}{method.upper()}{path}{suffix}{body}"
    digest = hmac.new(secret.encode(), payload.encode(), hashlib.sha256).digest()
    return base64.b64encode(digest).decode()


def position_key(item: dict[str, Any]) -> str:
    if item.get("productType") == "CFD":
        return ":".join(str(item.get(k) or "") for k in ("productType", "symbol", "holdSide", "marginCoin", "posId"))
    return ":".join((
        str(item.get("productType", "")),
        str(item.get("symbol", "")),
        str(item.get("holdSide", "")),
        str(item.get("marginCoin", "")),
    ))


def history_position_key(product_type: str, item: dict[str, Any]) -> str:
    position_id = str(item.get("positionId") or "")
    if position_id:
        return f"{product_type}:{position_id}"
    return ":".join((
        product_type,
        str(item.get("symbol") or ""),
        str(item.get("holdSide") or ""),
        str(item.get("ctime") or ""),
        str(item.get("utime") or ""),
    ))


def format_timestamp_ms(value: Any) -> str:
    try:
        millis = int(str(value or "0"))
    except ValueError:
        return "-"
    if millis <= 0:
        return "-"
    return datetime.fromtimestamp(millis / 1000).astimezone().strftime(
        "%Y-%m-%d %H:%M:%S %Z"
    )


class BitgetTimestampExpired(RuntimeError):
    """A WebSocket login rejected because its request timestamp expired."""


def cfd_enabled():
    return os.getenv("BITGET_CFD_ENABLED", "0").lower() in {"1", "true", "yes"}


def monitored_products(config):
    return (*config.product_types, "CFD") if cfd_enabled() else config.product_types


def use_uta_api():
    mode = os.getenv("BITGET_ACCOUNT_MODE", "classic").strip().lower()
    if mode not in {"classic", "uta"}:
        raise ValueError("BITGET_ACCOUNT_MODE must be classic or uta")
    return mode == "uta"


class BitgetClient:
    PATH = "/api/v2/mix/position/all-position"
    HISTORY_PATH = "/api/v2/mix/position/history-position"
    ACCOUNTS_PATH = "/api/v2/mix/account/accounts"

    def __init__(self, config: Config):
        self.config = config
        self.clock = get_clock(config.api_base, config.request_timeout)
        self.uta = use_uta_api()
        self.position_costs = (UTAPositionCostCache if self.uta else PositionCostCache)(self._get)
        self._tpsl_warning_at = float("-inf")
        self.cfd = None
        if cfd_enabled():
            from cfd_adapter import get_cfd_adapter
            self.cfd = get_cfd_adapter(self)

    def _get(self, params: dict[str, str], path: str | None = None) -> Any:
        path = path or self.PATH
        query = urllib.parse.urlencode(params)
        for attempt in range(2):
            timestamp_ms, generation = self.clock.stamp()
            timestamp = str(timestamp_ms)
            headers = {
                "ACCESS-KEY": self.config.api_key,
                "ACCESS-SIGN": sign(self.config.api_secret, timestamp, "GET", path, query),
                "ACCESS-TIMESTAMP": timestamp,
                "ACCESS-PASSPHRASE": self.config.api_passphrase,
                "Content-Type": "application/json",
                "locale": "zh-CN",
                "User-Agent": "bitget-position-tg/1.0",
            }
            request = urllib.request.Request(f"{self.config.api_base}{path}?{query}", headers=headers)
            try:
                with urllib.request.urlopen(request, timeout=self.config.request_timeout) as response:
                    payload = json.loads(response.read().decode())
            except urllib.error.HTTPError as exc:
                try:
                    detail = exc.read(65536).decode(errors="replace")
                finally:
                    exc.close()
                try:
                    error = json.loads(detail)
                except (ValueError, TypeError):
                    error = {}
                if attempt == 0 and isinstance(error, dict) and str(error.get("code")) == "40008":
                    self.clock.resync(generation)
                    LOG.warning("Bitget request clock resynchronized; retrying the read once")
                    continue
                raise RuntimeError(f"Bitget HTTP {exc.code}: {detail[:500]}") from exc
            if attempt == 0 and str(payload.get("code")) == "40008":
                self.clock.resync(generation)
                LOG.warning("Bitget request clock resynchronized; retrying the read once")
                continue
            if payload.get("code") != "00000":
                raise RuntimeError(f"Bitget API {payload.get('code')}: {payload.get('msg')}")
            return payload.get("data") or []

    def positions(self) -> dict[str, dict[str, Any]]:
        result = self._crypto_positions()
        if getattr(self, "cfd", None) is not None:
            result.update(self.cfd.positions())
        return result

    def _crypto_positions(self) -> dict[str, dict[str, Any]]:
        if self.uta:
            return self.uta_positions()
        result = {}
        for product_type in self.config.product_types:
            params = {"productType": product_type}
            if self.config.margin_coin:
                params["marginCoin"] = self.config.margin_coin
            for raw in self._get(params):
                if decimal(raw.get("total")) <= 0:
                    continue
                item = dict(raw)
                item["productType"] = product_type
                item = self.position_costs.annotate(item, product_type)
                result[position_key(item)] = item
        return result

    def uta_positions(self):
        result = {}
        for product in self.config.product_types:
            rows = unwrap_list(self._get({"category": product}, "/api/v3/position/current-position"))
            category_positions = {}
            for raw in rows:
                item = normalize_position(raw, product)
                if decimal(item["total"]) <= 0:
                    continue
                if self.config.margin_coin and item["marginCoin"] != self.config.margin_coin:
                    continue
                if not item.get("_utaFinancialsComplete"):
                    raise ValueError("UTA position financial fields are incomplete")
                item = self.position_costs.annotate(item, product)
                category_positions[position_key(item)] = item
            if category_positions:
                try:
                    plans = self._get({"category": product, "type": "tpsl"}, "/api/v3/trade/unfilled-strategy-orders")
                    category_positions = apply_tpsl(category_positions, plans)
                except Exception as error:
                    now = time.monotonic()
                    if now - self._tpsl_warning_at >= 60:
                        LOG.warning("UTA stop-order read delayed (%s); prices remain available", type(error).__name__)
                        self._tpsl_warning_at = now
            result.update(category_positions)
        return result

    def history_positions(self, product_type: str) -> list[dict[str, Any]]:
        if product_type == "CFD":
            if self.cfd is None: raise ValueError("CFD monitoring is disabled")
            return self.cfd.history_positions()
        if self.uta:
            rows = unwrap_list(self._get({"category": product_type, "limit": "100"}, "/api/v3/position/history-position"))
            return [normalize_history(row, product_type) for row in rows]
        data = self._get(
            {"productType": product_type, "limit": "20"},
            self.HISTORY_PATH,
        )
        return (data or {}).get("list") or []

    def accounts(self, product_type: str) -> list[dict[str, Any]]:
        if product_type == "CFD":
            if self.cfd is None: raise ValueError("CFD monitoring is disabled")
            return self.cfd.accounts()
        if self.uta:
            data = self._get({}, "/api/v3/account/assets")
            if not isinstance(data, dict) or not isinstance(data.get("assets"), list):
                raise ValueError("Unexpected UTA account assets")
            coin = self.config.margin_coin or {"USDT-FUTURES": "USDT", "USDC-FUTURES": "USDC"}.get(product_type)
            records = []
            for asset in data["assets"]:
                if coin and asset.get("coin") != coin:
                    continue
                asset_coin = asset.get("coin")
                pnl = data.get("usdtUnrealisedPnl") if asset_coin == "USDT" else data.get("btcUnrealizedPnl") if asset_coin == "BTC" else None
                records.append({"marginCoin": asset_coin, "accountEquity": asset.get("equity"),
                                "available": asset.get("available"), "locked": asset.get("locked"), "usdtEquity": asset.get("usdValue"), "unrealizedPL": pnl})
            return records
        return self._get(
            {"productType": product_type},
            self.ACCOUNTS_PATH,
        )


def enrich_closed_events(client: BitgetClient, events):
    cache, enriched = {}, []
    for kind, key, before, after in events:
        if kind == "closed" and before:
            product = str(before.get("productType") or "")
            if product not in cache:
                cache[product] = client.history_positions(product)
            matches = [h for h in cache[product] if history_complete(h) and history_matches(before, h, product)]
            if len(matches) == 1:
                before = dict(before, _history=matches[0])
        enriched.append((kind, key, before, after))
    return enriched


def detect_events(previous, current):
    events = []
    for key in sorted(set(previous) | set(current)):
        before, after = previous.get(key), current.get(key)
        if before is None:
            events.append(("opened", key, None, after))
            continue
        if after is None:
            events.append(("closed", key, before, None))
            continue
        # A rapid close/reopen on the same contract is a new position lifetime.
        old_time, new_time = before.get("cTime"), after.get("cTime")
        if old_time and new_time and str(old_time) != str(new_time):
            events.extend([("closed", key, before, None), ("opened", key, None, after)])
            continue
        old_size, new_size = decimal(before.get("total")), decimal(after.get("total"))
        if new_size > old_size:
            kind = "increased"
        elif new_size < old_size:
            kind = "reduced"
        else:
            notify_fields = ("openPriceAvg", "leverage", "marginMode", "autoMargin")
            if not any(str(before.get(k, "")) != str(after.get(k, "")) for k in notify_fields):
                continue
            kind = "updated"
        events.append((kind, key, before, after))
    return events


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=NORMAL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS position_state (
                position_key TEXT PRIMARY KEY, payload TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS position_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT, event_type TEXT NOT NULL,
                position_key TEXT NOT NULL, before_json TEXT, after_json TEXT,
                telegram_sent INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS position_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT, position_key TEXT NOT NULL,
                payload TEXT NOT NULL, captured_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_events_unsent ON position_events(telegram_sent, id);
            CREATE INDEX IF NOT EXISTS idx_events_pending_live ON position_events(id) WHERE telegram_sent=0 AND event_type!='closed';
            CREATE INDEX IF NOT EXISTS idx_events_pending_closed ON position_events(id) WHERE telegram_sent=0 AND event_type='closed';
            CREATE INDEX IF NOT EXISTS idx_snapshots_time ON position_snapshots(captured_at);
            CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS processed_history_positions (
                history_key TEXT PRIMARY KEY, processed_at TEXT NOT NULL
            );
        """)

    def initialized(self) -> bool:
        return self.db.execute("SELECT 1 FROM metadata WHERE key='initialized'").fetchone() is not None

    def load_state(self):
        rows = self.db.execute("SELECT position_key,payload FROM position_state").fetchall()
        return {row["position_key"]: json.loads(row["payload"]) for row in rows}

    def save_cycle(self, positions, events, snapshot: bool) -> None:
        now = datetime.now().astimezone().isoformat(timespec="seconds")
        with self.db:
            self.db.execute("DELETE FROM position_state")
            self.db.executemany(
                "INSERT INTO position_state(position_key,payload,updated_at) VALUES(?,?,?)",
                [(key, json.dumps(value, ensure_ascii=False), now) for key, value in positions.items()],
            )
            for kind, key, before, after in events:
                self.db.execute(
                    "INSERT INTO position_events(event_type,position_key,before_json,after_json,created_at) VALUES(?,?,?,?,?)",
                    (kind, key,
                     json.dumps(before, ensure_ascii=False) if before else None,
                     json.dumps(after, ensure_ascii=False) if after else None,
                     now),
                )
            if snapshot:
                self.db.executemany(
                    "INSERT INTO position_snapshots(position_key,payload,captured_at) VALUES(?,?,?)",
                    [(key, json.dumps(value, ensure_ascii=False), now) for key, value in positions.items()],
                )
            self.db.execute(
                "INSERT INTO metadata(key,value) VALUES('initialized',?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (now,)
            )

    def get_meta(self, key: str):
        row = self.db.execute("SELECT value FROM metadata WHERE key=?", (key,)).fetchone()
        return row["value"] if row else None

    def set_meta(self, key: str, value: str) -> None:
        with self.db:
            self.db.execute(
                "INSERT INTO metadata(key,value) VALUES(?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value),
            )

    def delete_meta(self, key: str) -> None:
        with self.db:
            self.db.execute("DELETE FROM metadata WHERE key=?", (key,))

    def unsent_events(self, closed_after_id=0):
        # A delayed settlement must not hide newer size-change notifications.
        # Bound both queues independently; rotate pending closes without discarding them.
        ready = self.db.execute(
            "SELECT * FROM position_events WHERE telegram_sent=0 AND event_type!='closed' ORDER BY id LIMIT 100"
        ).fetchall()
        sql = "SELECT * FROM position_events WHERE telegram_sent=0 AND event_type='closed' AND id>? ORDER BY id LIMIT 100"
        closing = self.db.execute(sql, (closed_after_id,)).fetchall()
        if not closing and closed_after_id:
            closing = self.db.execute(sql, (0,)).fetchall()
        return sorted([*ready, *closing], key=lambda row: row["id"])

    def mark_sent(self, event_id: int) -> None:
        with self.db:
            self.db.execute("UPDATE position_events SET telegram_sent=1 WHERE id=?", (event_id,))

    def record_history_positions(self, product_type, records, start_ms) -> int:
        now = datetime.now().astimezone().isoformat(timespec="seconds")
        return reconcile_closed_records(self.db, product_type, records, start_ms, now, history_position_key)


def recover_cfd_openings(client, store, positions, events):
    """Recover ledger-only rapid opens without replaying an observed lifetime."""
    if getattr(client, "cfd", None) is None:
        return events
    from trade_messages import identity
    known = set()
    for (raw,) in store.db.execute("SELECT after_json FROM position_events WHERE event_type='opened' AND after_json IS NOT NULL"):
        item = json.loads(raw)
        if item.get('productType') == 'CFD': known.add(identity(item))
    for kind, _, _, after in events:
        if kind == 'opened' and after and after.get('productType') == 'CFD': known.add(identity(after))
    for item in client.cfd.opening_events():
        key = identity(item)
        if key in known: continue
        item = dict(item)
        item['_notifyOpening'] = position_key(item) not in positions
        events.append(('opened', position_key(item), None, item))
        known.add(key)
    return events


def reconcile_history_positions(
    client: BitgetClient,
    store: Store,
    start_ms: int,
) -> int:
    added = 0
    for product_type in monitored_products(client.config):
        added += store.record_history_positions(
            product_type,
            client.history_positions(product_type),
            start_ms,
        )
    return added


def h(value: Any) -> str:
    return html.escape(str(value if value not in (None, "") else "-"))


def side_name(value: Any) -> str:
    return {"long": "多", "short": "空"}.get(str(value).lower(), str(value))


def fee_rebate_rate() -> Decimal:
    rate = decimal(os.getenv("FEE_REBATE_RATE", "0"))
    return min(Decimal("1"), max(Decimal("0"), rate))


def fee_rebate_percent() -> str:
    return format((fee_rebate_rate() * 100).normalize(), "f")


def current_fee_rebate(item) -> Decimal:
    return abs(decimal(item.get("deductedFee"))) * (Decimal("0") if item.get("_accountMode") == "cfd" else fee_rebate_rate())


def history_fee_rebate(item) -> Decimal:
    transaction_fee = (
        abs(decimal(item.get("openFee")))
        + abs(decimal(item.get("closeFee")))
    )
    return transaction_fee * (Decimal("0") if item.get("_accountMode") == "cfd" else fee_rebate_rate())


def history_net_pnl(item) -> Decimal:
    return decimal(item.get("netProfit")) + history_fee_rebate(item)


def current_net_pnl(item) -> Decimal | None:
    if item.get("_accountMode") == "cfd":
        value = item.get("_cfdNetPnl")
        return Decimal(str(value)) if value is not None else None
    if item.get("_utaPnlAmbiguous"):
        return None
    transaction_fee = abs(decimal(item.get("deductedFee")))
    return (
        decimal(item.get("unrealizedPL"))
        + decimal(item.get("achievedProfits"))
        + decimal(item.get("totalFee"))
        - transaction_fee
        + current_fee_rebate(item)
    )


def roi(item, net: bool = False) -> str:
    margin = decimal(item.get("marginSize"))
    value = current_net_pnl(item) if net else decimal(item.get("unrealizedPL"))
    return "-" if margin == 0 or value is None else f"{value / margin * 100:.2f}%"


def holding_duration(item, closed: bool = False, now_ms: int | None = None) -> str:
    start = decimal(item.get("_cfdOpenedAt")) if item.get("_accountMode") == "cfd" else decimal(item.get("ctime") or item.get("cTime"))
    if start <= 0:
        return "-"
    if closed:
        end = decimal(item.get("utime") or item.get("uTime"))
    else:
        end = Decimal(now_ms if now_ms is not None else int(time.time() * 1000))
    if end <= 0:
        return "-"
    seconds = max(0, int((end - start) / 1000))
    days, seconds = divmod(seconds, 86400)
    hours, seconds = divmod(seconds, 3600)
    minutes, seconds = divmod(seconds, 60)
    clock = f"{hours:02d}:{minutes:02d}:{seconds:02d}"
    return f"{days}天 {clock}" if days else clock


def display_closed_time(value) -> str:
    try:
        millis = int(str(value or "0"))
        if millis <= 0:
            return "待同步"
        return datetime.fromtimestamp(millis / 1000, ZoneInfo("Asia/Shanghai")).strftime("%m-%d %H:%M:%S")
    except (ValueError, TypeError, OverflowError, OSError):
        return "待同步"


def display_closed_net_roi(item) -> str:
    """Use the last matched full-position margin, as the live board does."""
    history = item.get("_history") or {}
    try:
        margin = Decimal(str(item.get("marginSize")))
        quantity = Decimal(str(item.get("total")))
        opened = Decimal(str(history.get("openTotalPos")))
        closed = Decimal(str(history.get("closeTotalPos")))
        entry = Decimal(str(item.get("openPriceAvg")))
        history_entry = Decimal(str(history.get("openAvgPrice")))
        settlement = [Decimal(str(history.get(key))) for key in ("netProfit", "openFee", "closeFee")]
        numbers = [margin, quantity, opened, closed, entry, history_entry, *settlement]
        if not all(value.is_finite() for value in numbers):
            return "—"
        if margin <= 0 or quantity <= 0 or entry <= 0 or not (quantity == opened == closed and entry == history_entry):
            return "—"
        if not item.get("symbol") or str(item["symbol"]) != str(history.get("symbol")):
            return "—"
        side = str(item.get("holdSide") or "").lower()
        if side not in {"long", "short"} or side != str(history.get("holdSide") or "").lower():
            return "—"
        coin = str(history.get("marginCoin") or history.get("settleCoin") or "")
        if coin and str(item.get("marginCoin") or "") != coin:
            return "—"
        snapshot_id = str(item.get("posId") or item.get("positionId") or "")
        history_id = str(history.get("positionId") or "")
        if snapshot_id and history_id and snapshot_id != history_id:
            return "—"
        snapshot_time = int(str(item.get("cTime") or item.get("ctime") or "0"))
        history_time = int(str(history.get("ctime") or history.get("cTime") or "0"))
        close_time = int(str(history.get("utime") or history.get("uTime") or "0"))
        # Bitget's live/history creation timestamps can differ by milliseconds.
        if min(snapshot_time, history_time) <= 0 or abs(snapshot_time - history_time) > 1000 or close_time < max(snapshot_time, history_time):
            return "—"
        result = Decimal(f"{history_net_pnl(history) / margin * 100:.2f}")
        prefix = "+" if result > 0 else "−" if result < 0 else ""
        return prefix + format(result.copy_abs(), ".2f") + "%"
    except (InvalidOperation, ValueError, TypeError, OverflowError):
        return "—"


def net_break_even_price(item) -> Decimal | None:
    """Price for zero estimated net PnL after a taker close, including rebates."""
    if item.get("_utaPnlAmbiguous"):
        return None
    if str(item.get("productType") or "").upper() not in {"USDT-FUTURES", "USDC-FUTURES"}:
        return None
    side = str(item.get("holdSide") or "").lower()
    if side not in {"long", "short"}:
        return None
    try:
        quantity = Decimal(str(item.get("total")))
        entry = Decimal(str(item.get("openPriceAvg")))
        closing_rate = Decimal(str(item.get("_closeTakerFeeRate")))
        tick = Decimal(str(item.get("_priceTick")))
        realized = Decimal(str(item.get("achievedProfits") or "0"))
        funding = Decimal(str(item.get("totalFee") or "0"))
        paid_fees = Decimal(str(item.get("deductedFee") or "0")).copy_abs()
        rebate_rate = fee_rebate_rate()
        values = [quantity, entry, closing_rate, tick, realized, funding, paid_fees, rebate_rate]
        if not all(value.is_finite() for value in values):
            return None
        if quantity <= 0 or entry <= 0 or tick <= 0 or not 0 <= closing_rate < 1:
            return None
        with localcontext() as context:
            context.prec = 50
            direction = Decimal(1) if side == "long" else Decimal(-1)
            fee_share = Decimal(1) - rebate_rate
            carried_net = realized + funding - paid_fees * fee_share
            price = (direction * entry - carried_net / quantity) / (direction - closing_rate * fee_share)
            if not price.is_finite() or price <= 0:
                return None
            rounding = ROUND_CEILING if side == "long" else ROUND_FLOOR
            price = (price / tick).to_integral_value(rounding=rounding) * tick
            return price if price > 0 else None
    except (InvalidOperation, ValueError, TypeError, OverflowError, ZeroDivisionError):
        return None


def format_position_lines(item, *, closed=False) -> list[str]:
    """One presentation layout for opened, current and closed positions."""
    history = (item.get("_history") or {}) if closed else {}
    source = history if closed else item
    symbol = source.get("symbol") or item.get("symbol") or "—"
    raw_side = source.get("holdSide") or item.get("holdSide") or "—"
    side = {"long": "做多", "short": "做空"}.get(str(raw_side).lower(), side_name(raw_side))
    coin = item.get("marginCoin") or source.get("marginCoin") or "—"
    leverage = source.get("leverage") or item.get("leverage")
    leverage_text = display_number(leverage)
    title = f"{h(symbol)} · {h(side)}"
    if item.get("_accountMode") == "cfd" or source.get("_accountMode") == "cfd":
        title += " · CFD"
    if leverage_text != "—":
        title += f" · {h(leverage_text)}×"
    if closed:
        try:
            settled = bool(history) and all(Decimal(str(history.get(key))).is_finite() for key in ("netProfit", "openFee", "closeFee"))
        except (InvalidOperation, ValueError, TypeError):
            settled = False
        net = history_net_pnl(history) if settled else None
        net_roi = display_closed_net_roi(item) if settled else "—"
        opening = history.get("openAvgPrice") or item.get("openPriceAvg")
        second_price = display_number(history.get("closeAvgPrice"))
        quantity = display_number(history.get("closeTotalPos"))
        second_price = "待同步" if second_price == "—" else second_price
        quantity = "待同步" if quantity == "—" else quantity
        try:
            duration = holding_duration(history, closed=True)
        except (InvalidOperation, ValueError, OverflowError):
            duration = "-"
        duration = "待同步" if duration == "-" else duration
        second_label = "平仓均价"
        fee = display_number(decimal(history.get("openFee")) + decimal(history.get("closeFee")), money=True, signed=True) if settled else "待同步"
        rebate = display_number(history_fee_rebate(history), money=True, signed=True) if settled else "待同步"
        funding = display_number(history.get("totalFunding"), money=True, signed=True)
        funding = "待同步" if funding == "—" else funding
    else:
        net = current_net_pnl(item)
        net_roi = display_net_roi(item)
        opening = item.get("openPriceAvg")
        second_price = display_number(item.get("markPrice"))
        quantity = display_number(item.get("total"))
        duration = holding_duration(item)
        duration = "—" if duration == "-" else duration
        second_label = "标记价格"
        fee = display_number(-abs(decimal(item.get("deductedFee"))), money=True, signed=True)
        rebate = display_number(current_fee_rebate(item), money=True, signed=True)
        funding = display_number(item.get("totalFee"), money=True, signed=True)
    is_cfd = item.get("_accountMode") == "cfd" or source.get("_accountMode") == "cfd"
    if is_cfd:
        rebate = "—"
        quantity += " 手"
        if not closed and item.get("deductedFee") is None: fee = "—"
        if not closed: second_label = "可平仓报价"
    funding_label = "隔夜利息" if is_cfd else "资金费"
    if net is None:
        pnl_line = "<b>⏳ 净盈亏：待结算</b>" if closed else "<b>⚪ 净盈亏：</b><code>—</code>"
    else:
        tone = "🟢" if net > 0 else "🔴" if net < 0 else "⚪"
        pnl_line = f"<b>{tone} 净盈亏：</b><code>{h(display_number(net, money=True, signed=True))} {h(coin)}</code>"
    pnl_line += f"  ·  净收益率：<code>{h(net_roi)}</code>"
    take_profit = "—" if closed and "takeProfit" not in item else display_trigger(item.get("takeProfit"))
    stop_loss = "—" if closed and "stopLoss" not in item else display_trigger(item.get("stopLoss"))
    if item.get("_utaFinancialsComplete") is not None:
        if item.get("takeProfit") is None: take_profit = "—"
        if item.get("stopLoss") is None: stop_loss = "—"
    return [
        f"<b>{title}</b>",
        pnl_line,
        f"🔵 开仓均价：<code>{h(display_number(opening))}</code>",
        f"🟠 {second_label}：<code>{h(second_price)}</code>",
        f"🟣 盈亏持平价格：<code>{h(display_number(net_break_even_price(item)))}</code>",
        f"止盈/止损：<code>{h(take_profit)}</code> / <code>{h(stop_loss)}</code>",
        f"数量：<code>{h(quantity)}</code>  ·  持仓：<code>{h(duration)}</code>",
        f"手续费：<code>{h(fee)}</code>  ·  返佣：<code>{h(rebate)}</code>  ·  {funding_label}：<code>{h(funding)}</code>",
    ]


def format_opened_event(item) -> str:
    opened_at = display_closed_time(item.get("_cfdOpenedAt")) if item.get("_accountMode") == "cfd" else display_closed_time(item.get("cTime") or item.get("ctime"))
    return "\n".join([
        "<b>📊 BITGET · 已开仓</b>",
        f"开仓时间：{h(opened_at)}（北京时间）",
        "",
        *format_position_lines(item),
    ])


def format_closed_event(item) -> str:
    history = item.get("_history") or {}
    closed_at = display_closed_time(history.get("utime") or history.get("uTime"))
    return "\n".join([
        "<b>📊 BITGET · 已平仓</b>",
        f"平仓时间：{h(closed_at)}（北京时间）",
        "",
        *format_position_lines(item, closed=True),
    ])


def format_event(kind, before, after) -> str:
    item = after or before or {}
    if kind == "closed":
        return format_closed_event(item)
    if kind == "opened":
        return format_opened_event(item)

    lines = [f"<b>📊 BITGET · {h(EVENT_LABELS[kind])}</b>"]
    if before and after and kind in {"increased", "reduced"}:
        arrow = "⬆️" if kind == "increased" else "⬇️"
        lines.append(f"{arrow} 数量：<code>{h(display_number(before.get('total')))}</code> → <code>{h(display_number(after.get('total')))}</code>")
    lines.append(f"时间：{h(datetime.now(ZoneInfo('Asia/Shanghai')).strftime('%m-%d %H:%M:%S'))}（北京时间）")
    lines += ["", *format_position_lines(item)]
    return "\n".join(lines)

def format_summary(positions) -> str:
    lines = ["<b>[BITGET 持仓监控已启动]</b>"]
    if not positions:
        return "\n".join(lines + ["当前没有持仓。"])
    lines.append(f"当前持仓：{len(positions)} 个")
    for item in positions.values():
        lines += [
            "",
            f"<code>{h(item.get('symbol'))}</code> {h(side_name(item.get('holdSide')))} "
            f"{h(item.get('total'))} @ {h(item.get('openPriceAvg'))}",
            f"未实现盈亏：{h(item.get('unrealizedPL'))} {h(item.get('marginCoin'))}",
            f"持仓时间：{h(holding_duration(item))}",
        ]
    return "\n".join(lines)


def display_number(value, *, money=False, signed=False) -> str:
    """Readable display only; price/quantity precision and PnL math stay intact."""
    if value in (None, "", "-"):
        return "—"
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return "—"
    if not amount.is_finite():
        return "—"
    absolute = amount.copy_abs()
    if money and absolute:
        places = 2 if absolute >= 1 else min(12, max(4, 3 - absolute.adjusted()))
        with localcontext() as context:
            context.prec = max(28, len(amount.as_tuple().digits) + places + max(amount.adjusted(), 0) + 4)
            rounded = absolute.quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP)
        # A tiny non-zero amount must never be presented as zero.
        rendered = format(absolute, ".3E") if not rounded else format(rounded, ",f")
    else:
        rendered = format(absolute, ",f")
    if "." in rendered and "E" not in rendered:
        rendered = rendered.rstrip("0").rstrip(".")
    prefix = "−" if amount < 0 else "+" if signed and amount > 0 else ""
    return prefix + rendered


def display_trigger(value) -> str:
    if value in (None, "", "-"):
        return "未设"
    try:
        if Decimal(str(value)) == 0:
            return "未设"
    except (InvalidOperation, ValueError, TypeError):
        pass
    return display_number(value)


def display_net_roi(item) -> str:
    value = roi(item, net=True)
    if value == "-":
        return "—"
    number = Decimal(value.rstrip("%"))
    prefix = "+" if number > 0 else "−" if number < 0 else ""
    return prefix + format(abs(number), ".2f") + "%"


def format_pnl_report(positions, compact: bool = False) -> str:
    now = datetime.now(ZoneInfo("Asia/Shanghai"))
    lines = [
        f"<b>📊 BITGET · 持仓（{len(positions)}）</b>",
        "更新：" + now.strftime("%m-%d %H:%M:%S") + "（北京时间）",
    ]
    if not positions:
        return "\n".join(lines + ["", "当前没有持仓。"])

    net_totals = {}
    for item in positions.values():
        coin = str(item.get("marginCoin") or "—")
        value, previous = current_net_pnl(item), net_totals.get(coin, Decimal("0"))
        net_totals[coin] = None if value is None or previous is None else previous + value
    if len(positions) > 1:
        for coin in sorted(net_totals):
            lines.append(f"<b>合计净盈亏：</b><code>{h(display_number(net_totals[coin], money=True, signed=True))} {h(coin)}</code>")

    for item in positions.values():
        detail = format_position_lines(item)
        # Compact layout keeps many simultaneous positions inside one message.
        lines += ["", *([detail[0], detail[1], detail[3], detail[6]] if compact else detail)]
    return "\n".join(lines)


PNL_MESSAGE_META = "pnl_message_id"


def sync_pnl_message(store: Store, telegram: Telegram, positions, force_new: bool = False) -> None:
    """Update the existing board; force_new is retained only for caller compatibility."""
    raw_message_id = store.get_meta(PNL_MESSAGE_META)
    text = format_pnl_report(positions) if positions else None
    if store.get_meta("pnl_message_parts") or (text is not None and len(text) > 3800):
        sync_multipart_board(store, telegram, text)
        return
    if positions:
        if raw_message_id:
            try:
                telegram.edit(int(raw_message_id), text)
            except TelegramMessageMissing:
                # Only a confirmed missing/uneditable message can be replaced.
                store.delete_meta(PNL_MESSAGE_META)
                LOG.info("Telegram position message no longer editable; replacing it")
            else:
                # Keep the saved board even after an event or process restart.
                return
        message_id = telegram.send(text)
        if not message_id:
            raise TelegramError("Telegram position message was not confirmed")
        store.set_meta(PNL_MESSAGE_META, str(message_id))
        return

    if raw_message_id:
        closed_text = (
            "<b>📊 BITGET · 当前空仓</b>\n"
            "当前没有持仓。\n下次开仓后自动展示新的持仓看板。"
        )
        try:
            telegram.edit(int(raw_message_id), closed_text)
        except TelegramMessageMissing:
            pass
        # A transient error must retain the id so closing can be retried.
        store.delete_meta(PNL_MESSAGE_META)


def deliver_pending(store: Store, telegram: Telegram) -> None:
    for row in store.unsent_events():
        before = json.loads(row["before_json"]) if row["before_json"] else None
        after = json.loads(row["after_json"]) if row["after_json"] else None
        telegram.send(format_event(row["event_type"], before, after))
        store.mark_sent(row["id"])


def websocket_login_payload(config: Config, now_ms: int | None = None) -> dict[str, Any]:
    timestamp = str(now_ms if now_ms is not None else get_clock(config.api_base, config.request_timeout).stamp()[0])
    return {
        "op": "login",
        "args": [{
            "apiKey": config.api_key,
            "passphrase": config.api_passphrase,
            "timestamp": timestamp,
            "sign": sign(config.api_secret, timestamp, "GET", "/user/verify"),
        }],
    }


def websocket_subscription_args(product_types, *, uta=False) -> list[dict[str, str]]:
    if uta:
        return [{"instType": "UTA", "topic": "position"}]
    channels = []
    for product_type in product_types:
        channels.extend([
            {
                "instType": product_type,
                "channel": "positions",
                "instId": "default",
            },
            {
                "instType": product_type,
                "channel": "positions-history",
                "instId": "default",
            },
        ])
    return channels


def signal_realtime_cycle(signals: queue.Queue, channel: str) -> None:
    try:
        signals.put_nowait(channel)
    except queue.Full:
        pass


def realtime_websocket_listener(config: Config, signals: queue.Queue, bark=None) -> None:
    import websocket

    uta = use_uta_api()
    url = os.getenv(
        "BITGET_WEBSOCKET_URL",
        "wss://ws.bitget.com/v3/ws/private" if uta else "wss://ws.bitget.com/v2/ws/private",
    ).strip()
    backoff = 1
    clock = get_clock(config.api_base, config.request_timeout)
    time_resync_retried = False
    while not STOP:
        connection = None
        try:
            connection = websocket.create_connection(
                url,
                timeout=config.request_timeout,
                enable_multithread=False,
            )
            login_ms, clock_generation = clock.stamp()
            connection.send(json.dumps(websocket_login_payload(config, now_ms=login_ms)))
            while not STOP:
                raw = connection.recv()
                if raw == "pong":
                    continue
                payload = json.loads(raw)
                event = str(payload.get("event") or "")
                if event == "error":
                    if str(payload.get("code")) == "30014":
                        raise BitgetTimestampExpired("Bitget WebSocket timestamp expired")
                    raise RuntimeError(
                        f"Bitget WebSocket {payload.get('code')}: {payload.get('msg')}"
                    )
                if event == "login":
                    if str(payload.get("code")) not in {"0", "00000"}:
                        if str(payload.get("code")) == "30014":
                            raise BitgetTimestampExpired("Bitget WebSocket timestamp expired")
                        raise RuntimeError(
                            f"Bitget WebSocket login failed: {payload}"
                        )
                    break
            if STOP:
                return

            subscriptions = websocket_subscription_args(config.product_types, uta=uta)
            if bark is not None:
                if uta:
                    subscriptions.append({"instType": "UTA", "topic": "fill"})
                else:
                    subscriptions.extend(
                        {"instType": product_type, "channel": "orders", "instId": "default"}
                        for product_type in config.product_types
                    )
            connection.send(json.dumps({
                "op": "subscribe",
                "args": subscriptions,
            }))
            request_bark_reconcile(bark)
            connection.settimeout(1)
            last_ping = time.monotonic()
            last_pong = last_ping
            backoff = 1
            time_resync_retried = False
            LOG.info(
                "realtime websocket connected for %s",
                ",".join(config.product_types),
            )

            while not STOP:
                now = time.monotonic()
                if now - last_ping >= 25:
                    connection.send("ping")
                    last_ping = now
                if now - last_pong > 65:
                    raise RuntimeError("Bitget WebSocket heartbeat timed out")
                try:
                    raw = connection.recv()
                except websocket.WebSocketTimeoutException:
                    continue
                if not raw:
                    raise RuntimeError("Bitget WebSocket connection closed")
                if raw == "pong":
                    last_pong = time.monotonic()
                    continue

                payload = json.loads(raw)
                event = str(payload.get("event") or "")
                argument = payload.get("arg") or {}
                channel = str(argument.get("topic" if uta else "channel") or "")
                if uta and channel == "fill" and bark is not None:
                    if event == "error":
                        LOG.warning("UTA fill subscription rejected; REST reconciliation continues")
                        request_bark_reconcile(bark)
                    elif event == "subscribe":
                        LOG.info("UTA fills websocket subscription confirmed")
                    elif payload.get("data") is not None:
                        try:
                            bark.on_uta_fills(payload["data"])
                        except Exception as error:
                            LOG.warning("UTA fill update delayed (%s)", type(error).__name__)
                            request_bark_reconcile(bark)
                        signal_realtime_cycle(signals, "uta-fill")
                    continue
                if bark is not None and channel == "orders":
                    if event == "error":
                        LOG.warning("Bark orders subscription rejected; REST reconciliation continues")
                        request_bark_reconcile(bark)
                    elif event == "subscribe":
                        LOG.info("Bark orders websocket subscription confirmed")
                    elif payload.get("data") is not None:
                        try:
                            bark.on_orders(str(argument.get("instType") or ""), payload["data"])
                        except Exception as error:
                            LOG.warning("Bark order update failed (%s); positions continue", type(error).__name__)
                            request_bark_reconcile(bark)
                    continue
                if event == "error":
                    if str(payload.get("code")) == "30014":
                        raise BitgetTimestampExpired("Bitget WebSocket timestamp expired")
                    raise RuntimeError(
                        f"Bitget WebSocket {payload.get('code')}: {payload.get('msg')}"
                    )
                if channel in {"position", "positions", "positions-history"} and payload.get("data") is not None:
                    LOG.info("realtime trigger received: %s", channel)
                    signal_realtime_cycle(signals, channel)
        except Exception as exc:
            if not STOP:
                if isinstance(exc, BitgetTimestampExpired) and not time_resync_retried:
                    time_resync_retried = True
                    try:
                        clock.resync(clock_generation)
                    except Exception as sync_error:
                        LOG.warning("Bitget WebSocket clock sync failed (%s)", type(sync_error).__name__)
                    else:
                        backoff = 1
                        LOG.warning("Bitget WebSocket clock resynchronized; reconnecting immediately")
                        continue
                LOG.warning(
                    "realtime websocket disconnected; retrying in %ss: %s",
                    backoff,
                    exc,
                )
                time.sleep(backoff)
                backoff = min(30, backoff * 2)
        finally:
            if connection is not None:
                try:
                    connection.close()
                except Exception:
                    pass


def stop_handler(signum, frame) -> None:
    del signum, frame
    global STOP
    STOP = True
    if REALTIME_SIGNALS is not None:
        signal_realtime_cycle(REALTIME_SIGNALS, "stop")



def start_bark_dispatcher(config):
    if not notification_settings.enabled("bark"):
        return None
    key = os.getenv("BARK_KEY", "").strip()
    if not key:
        return None
    dispatcher = None
    try:
        from bark_orders import BarkOrderDispatcher
        from bark_transport import BarkTransport
        transport = BarkTransport(
            base=os.getenv("BARK_BASE", "http://127.0.0.1:8080").strip(),
            key=key,
            sound=os.getenv("BARK_SOUND", "123").strip(),
            call=os.getenv("BARK_CALL", "1").strip().lower() in {"1", "true", "yes"},
            timeout=min(config.request_timeout, 10),
            group=os.getenv("BARK_GROUP", "bitget-orders").strip(),
        )
        dispatcher = BarkOrderDispatcher(
            config.db_path.parent / "bark_orders.sqlite3",
            BitgetClient(config), transport,
            reconcile_seconds=float(os.getenv("BARK_RECONCILE_SECONDS", "30")),
        )
        dispatcher.start()
        return dispatcher
    except Exception as error:
        LOG.error("Bark setup failed (%s); position notifications continue", type(error).__name__)
        if dispatcher is not None:
            try:
                dispatcher.stop()
            except Exception as stop_error:
                LOG.warning("Bark setup cleanup failed (%s)", type(stop_error).__name__)
        return None


def request_bark_reconcile(bark):
    if bark is None:
        return
    try:
        bark.request_reconcile()
    except Exception as error:
        LOG.warning("Bark reconciliation request failed (%s); positions continue", type(error).__name__)


def make_telegram_trade_board(store, telegram):
    from contextlib import nullcontext
    from aggregate_board import AggregateTradeMessageBoard
    def checked(text):
        if len(text) > 3800:
            raise ValueError("Combined positions exceed Telegram's message size limit")
        return text
    def board_text(positions):
        text = format_pnl_report(positions)
        return checked(text if len(text) <= 3800 else format_pnl_report(positions, compact=True))
    def latest_message_id():
        try:
            saved = int(store.get_meta("telegram_last_message_id") or 0)
        except (TypeError, ValueError):
            saved = 0
        return max(saved, getattr(telegram, "last_message_id", None) or 0) or None
    def remember(message_id):
        if int(message_id) > (latest_message_id() or 0):
            store.set_meta("telegram_last_message_id", str(int(message_id)))
    telegram.on_sent = remember
    return AggregateTradeMessageBoard(
        lambda: nullcontext(store.db), "telegram", telegram.send,
        lambda mid, text: telegram.edit(int(mid), text),
        lambda error: isinstance(error, TelegramMessageMissing),
        lambda error: isinstance(error, TelegramError) and not error.delivery_unknown,
        board_text,
        lambda item: checked(format_closed_event(item)),
        lambda mid: telegram.delete(int(mid)),
        format_opened=lambda item: checked(format_opened_event(item)),
        latest_message_id=latest_message_id,
    )


def make_discord_trade_board(dispatcher):
    from aggregate_board import AggregateTradeMessageBoard
    from discord_webhook import board_payloads, event_payloads, DiscordError
    def single(payloads):
        if len(payloads) != 1:
            raise ValueError("Combined positions exceed Discord's message size limit")
        return payloads[0]
    return AggregateTradeMessageBoard(
        dispatcher._connect, "discord", dispatcher.transport.send, dispatcher.transport.edit,
        dispatcher._missing_message,
        lambda error: isinstance(error, DiscordError) and error.status in {400,401,403,404,405,413,429},
        lambda positions: single(board_payloads(format_pnl_report(positions))),
        lambda item: single(event_payloads(format_closed_event(item))),
        dispatcher.transport.delete,
        format_opened=lambda item: single(event_payloads(format_opened_event(item))),
    )


def start_discord_dispatcher(config):
    if not notification_settings.enabled("discord") or not config.discord_webhook_url:
        return None
    try:
        from discord_push import DiscordDispatcher
        dispatcher = DiscordDispatcher(
            config.db_path, config.discord_webhook_url, config.request_timeout,
            config.discord_update_seconds, format_event, format_pnl_report, format_summary,
            send_startup_summary=config.send_startup_summary,
            positions_provider=BitgetClient(config).positions,
            trade_board_factory=make_discord_trade_board,
        )
        dispatcher.start()
        LOG.info("Discord PnL source: live Bitget positions (%.1fs target)", config.discord_update_seconds)
        return dispatcher
    except Exception as error:
        LOG.error("Discord setup failed (%s); Telegram continues", type(error).__name__)
        return None


def queue_discord_notice(dispatcher, text):
    if dispatcher is None:
        return
    try:
        dispatcher.enqueue_notice(text)
    except Exception as error:
        LOG.warning("Discord notice could not be queued (%s)", type(error).__name__)


def start_telegram_dispatcher(config):
    if not notification_settings.enabled("telegram"):
        return None
    from telegram_live import TelegramLiveDispatcher
    dispatcher = TelegramLiveDispatcher(
        store_factory=lambda: Store(config.db_path),
        telegram=Telegram(config.telegram_token, config.telegram_chat_id, config.request_timeout),
        positions_provider=BitgetClient(config).positions,
        format_event=format_event,
        sync_pnl_message=sync_pnl_message,
        update_seconds=config.telegram_update_seconds,
        trade_board_factory=make_telegram_trade_board,
    )
    dispatcher.start()
    LOG.info("Telegram PnL source: live Bitget positions (%.1fs target)", config.telegram_update_seconds)
    return dispatcher


def queue_telegram_notice(dispatcher, text):
    if dispatcher is None:
        return
    try:
        dispatcher.enqueue_notice(text)
    except Exception as error:
        LOG.warning("Telegram notice could not be queued (%s)", type(error).__name__)


def run() -> None:
    config = Config.from_env()
    store = Store(config.db_path)
    notification_settings.prepare_runtime(config.db_path)
    client = BitgetClient(config)
    telegram_live = start_telegram_dispatcher(config)
    discord = start_discord_dispatcher(config)
    history_start = datetime.fromisoformat(
        os.getenv("HISTORY_RECONCILE_START", "1970-01-01T00:00:00+00:00")
    )
    history_start_ms = int(history_start.timestamp() * 1000)
    signal.signal(signal.SIGTERM, stop_handler)
    signal.signal(signal.SIGINT, stop_handler)
    signals = queue.Queue(maxsize=1)
    global REALTIME_SIGNALS
    REALTIME_SIGNALS = signals
    notification_settings.watch_changes(lambda: stop_handler(signal.SIGTERM, None))
    bark = start_bark_dispatcher(config)
    realtime_thread = threading.Thread(
        target=realtime_websocket_listener,
        args=(config, signals, bark),
        name="bitget-realtime",
        daemon=True,
    )
    realtime_thread.start()
    failures, last_snapshot, first_loop = 0, 0.0, True
    LOG.info("monitor v%s started for %s (account API: %s)", __version__, ",".join(config.product_types), "UTA" if client.uta else "classic")
    while not STOP:
        started = time.monotonic()
        try:
            positions = client.positions()
            previous = store.load_state()
            events = detect_events(previous, positions) if store.initialized() else []
            events = recover_cfd_openings(client, store, positions, events)
            if any(kind == "closed" for kind, _, _, _ in events):
                events = enrich_closed_events(client, events)
            snapshot = time.monotonic() - last_snapshot >= config.snapshot_seconds
            store.save_cycle(positions, events, snapshot)
            notification_settings.mark_applied()
            try:
                added = reconcile_history_positions(client, store, history_start_ms)
                if added:
                    LOG.info("queued %s missed history position(s)", added)
            except Exception:
                LOG.exception("history position reconciliation failed")
            if discord is not None:
                discord.wake()
            if snapshot:
                last_snapshot = time.monotonic()
            if first_loop and config.send_startup_summary:
                queue_telegram_notice(telegram_live, format_summary(positions))
            if telegram_live is not None:
                telegram_live.wake()
            if failures >= 3:
                queue_discord_notice(discord, "<b>[BITGET 监控恢复]</b>\n持仓监控循环已恢复。")
            failures, first_loop = 0, False
        except Exception:
            failures += 1
            LOG.exception("monitor cycle failed (%s)", failures)
            if failures == 3:
                queue_discord_notice(discord, "<b>[BITGET 监控异常]</b>\n连续 3 次监控循环失败，请检查服务日志。")
        interval = min(config.poll_seconds, 2) if client.cfd is not None else config.poll_seconds
        remaining = interval - (time.monotonic() - started)
        if remaining > 0 and not STOP:
            try:
                signals.get(timeout=remaining)
            except queue.Empty:
                pass
    if telegram_live is not None:
        telegram_live.stop()
    if bark is not None:
        try:
            bark.stop()
        except Exception as error:
            LOG.warning("Bark shutdown failed (%s)", type(error).__name__)
    if discord is not None:
        discord.stop()
    REALTIME_SIGNALS = None
    LOG.info("monitor stopped")


def main() -> int:
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(message)s",
    )
    try:
        run()
    except Exception as exc:
        LOG.error("%s", exc)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
