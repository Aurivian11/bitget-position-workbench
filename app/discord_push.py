"""Durable Discord delivery, isolated from the market and Telegram loop."""
import html
import json
import logging
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import datetime
from zoneinfo import ZoneInfo
from pathlib import Path
from close_history import history_complete
import notification_settings

from discord_webhook import DiscordError, DiscordWebhook, message_payloads, board_payloads, event_payloads

LOG = logging.getLogger("bitget-position-discord")
BASELINE_KEY = "discord_start_event_id"
BOARD_KEY = "discord_pnl_message_ids"
CLOSED_TEXT = "<b>📊 BITGET · 当前空仓</b>\n当前没有持仓。\n下次开仓后自动展示新的持仓看板。"
CONNECTED_TEXT = (
    "<b>[BITGET Discord 推送已启用]</b>\n"
    "开仓、加仓、减仓、平仓及持仓参数变更将同步推送到此频道。\n"
    "持仓盈亏看板会自动更新，全部平仓后标记结束。\n"
    "🔵 开仓均价与 🟠 标记价格保留分行显示。"
)


class DiscordDispatcher:
    def __init__(self, db_path, webhook_url, timeout, update_seconds,
                 format_event, format_pnl_report, format_summary,
                 send_startup_summary=False, transport=None, positions_provider=None, trade_board_factory=None):
        self.trade_board_factory = trade_board_factory
        self.trade_board = None
        self.db_path = Path(db_path)
        self.transport = transport if transport is not None else DiscordWebhook(webhook_url, timeout)
        self.timeout = timeout
        self.update_seconds = max(1, float(update_seconds))
        self.format_event = format_event
        self.format_pnl_report = format_pnl_report
        self.format_summary = format_summary
        self.send_startup_summary = send_startup_summary
        self.positions_provider = positions_provider
        self.next_board_at = 0.0
        self._closed_cursor = 0
        self.disabled = False
        self.thread = None
        self._stop = threading.Event()
        self._wake = threading.Event()
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
        with self._connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS discord_event_delivery (
                    event_id INTEGER PRIMARY KEY,
                    html TEXT NOT NULL,
                    message_ids TEXT NOT NULL DEFAULT '[]',
                    complete INTEGER NOT NULL DEFAULT 0,
                    updated_at TEXT NOT NULL DEFAULT ''
                );
                CREATE TABLE IF NOT EXISTS discord_notices (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    dedupe_key TEXT NOT NULL UNIQUE,
                    html TEXT NOT NULL,
                    message_ids TEXT NOT NULL DEFAULT '[]',
                    complete INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_events_discord_live ON position_events(id) WHERE event_type!='closed';
                CREATE INDEX IF NOT EXISTS idx_events_discord_closed ON position_events(id) WHERE event_type='closed';
                CREATE INDEX IF NOT EXISTS idx_discord_notices_pending
                    ON discord_notices(complete, id);
            """)
            # One atomic statement fixes the start boundary before the market loop starts.
            db.execute(
                "INSERT OR IGNORE INTO metadata(key,value) "
                "SELECT ?, CAST(COALESCE(MAX(id),0) AS TEXT) FROM position_events",
                (BASELINE_KEY,),
            )

    def _meta(self, key):
        with self._connect() as db:
            row = db.execute("SELECT value FROM metadata WHERE key=?", (key,)).fetchone()
        return row["value"] if row else None

    def _set_meta(self, key, value):
        with self._connect() as db:
            if value is None:
                db.execute("DELETE FROM metadata WHERE key=?", (key,))
            else:
                db.execute(
                    "INSERT INTO metadata(key,value) VALUES(?,?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value)
                )

    def _snapshot(self):
        with self._connect() as db:
            db.execute("BEGIN")
            rows = db.execute("SELECT position_key,payload FROM position_state ORDER BY position_key").fetchall()
            meta = db.execute("SELECT value FROM metadata WHERE key='initialized'").fetchone()
        return ({r["position_key"]: json.loads(r["payload"]) for r in rows}, meta["value"] if meta else None)

    def enqueue_notice(self, html_text, key=None):
        with self._connect() as db:
            db.execute(
                "INSERT OR IGNORE INTO discord_notices(dedupe_key,html,created_at) VALUES(?,?,?)",
                (key or uuid.uuid4().hex, html_text, datetime.now().astimezone().isoformat(timespec="seconds")),
            )
        self.wake()

    def _send_record(self, table, record_id, record):
        if table not in {"discord_event_delivery", "discord_notices"}:
            raise ValueError("Unknown Discord delivery table")
        column = "event_id" if table == "discord_event_delivery" else "id"
        payloads = (event_payloads if table == "discord_event_delivery" else message_payloads)(record["html"])
        ids = json.loads(record["message_ids"])
        for payload in payloads[len(ids):]:
            message_id = self.transport.send(payload)
            if not message_id:
                raise DiscordError("Discord send was not confirmed")
            ids.append(str(message_id))
            # Save each successful page before attempting the next network request.
            with self._connect() as db:
                db.execute(f"UPDATE {table} SET message_ids=? WHERE {column}=?", (json.dumps(ids), record_id))
        with self._connect() as db:
            db.execute(f"UPDATE {table} SET complete=1 WHERE {column}=?", (record_id,))
            if table == "discord_event_delivery":
                db.execute("UPDATE discord_event_delivery SET updated_at=? WHERE event_id=?",
                           (datetime.now().astimezone().isoformat(timespec="seconds"), record_id))

    def deliver_notices(self):
        with self._connect() as db:
            rows = db.execute("SELECT * FROM discord_notices WHERE complete=0 ORDER BY id LIMIT 10").fetchall()
        for row in rows:
            self._send_record("discord_notices", row["id"], row)

    def deliver_events(self):
        baseline = int(self._meta(BASELINE_KEY) or 0)
        with self._connect() as db:
            base = (
                "SELECT e.* FROM position_events e LEFT JOIN discord_event_delivery d ON d.event_id=e.id "
                "WHERE e.id>? AND COALESCE(d.complete,0)=0 "
            )
            live = db.execute(base + "AND e.event_type!='closed' ORDER BY e.id LIMIT 20", (baseline,)).fetchall()
            closing_sql = base + "AND e.event_type='closed' AND e.id>? ORDER BY e.id LIMIT 20"
            closing = db.execute(closing_sql, (baseline, self._closed_cursor)).fetchall()
            if not closing and self._closed_cursor:
                closing = db.execute(closing_sql, (baseline, 0)).fetchall()
            rows = sorted([*live, *closing], key=lambda row: row["id"])
        completed = 0
        delivered = 0
        for row in rows:
            if delivered >= 20:
                break
            if row["event_type"] == "closed":
                self._closed_cursor = row["id"]
            if row["event_type"] not in {"opened", "updated"} and not notification_settings.event_enabled("discord", row["event_type"]):
                with self._connect() as db:
                    db.execute("INSERT INTO discord_event_delivery(event_id,html,complete,updated_at) VALUES(?,'',2,?) ON CONFLICT(event_id) DO UPDATE SET complete=2,updated_at=excluded.updated_at", (row["id"], datetime.now().astimezone().isoformat(timespec="seconds")))
                completed += 1
                continue
            if row["event_type"] == "closed":
                before = json.loads(row["before_json"]) if row["before_json"] else {}
                if not history_complete(before.get("_history") or {}):
                    continue
                board = self._get_trade_board()
                if board is not None:
                    if not board.close(before):
                        continue
                    text = self.format_event("closed", before, None)
                    with self._connect() as db:
                        db.execute(
                            "INSERT INTO discord_event_delivery(event_id,html,message_ids,complete,updated_at) "
                            "VALUES(?,?,?,1,?) ON CONFLICT(event_id) DO UPDATE SET "
                            "html=excluded.html,message_ids=excluded.message_ids,complete=1,updated_at=excluded.updated_at",
                            (row["id"], text, json.dumps(board.message_ids(before)), datetime.now().astimezone().isoformat(timespec="seconds")),
                        )
                    completed += 1
                    delivered += 1
                    continue
            if row["event_type"] in {"opened", "updated"}:
                # Opening and parameter changes use the live board; size changes notify separately.
                with self._connect() as db:
                    db.execute(
                        "INSERT INTO discord_event_delivery(event_id,html,complete,updated_at) "
                        "VALUES(?,'',1,?) ON CONFLICT(event_id) DO UPDATE SET "
                        "complete=1,updated_at=excluded.updated_at",
                        (row["id"], datetime.now().astimezone().isoformat(timespec="seconds")),
                    )
                completed += 1
                continue
            with self._connect() as db:
                record = db.execute("SELECT * FROM discord_event_delivery WHERE event_id=?", (row["id"],)).fetchone()
            if record is None:
                before = json.loads(row["before_json"]) if row["before_json"] else None
                after = json.loads(row["after_json"]) if row["after_json"] else None
                text = self.format_event(row["event_type"], before, after)
                with self._connect() as db:
                    db.execute("INSERT OR IGNORE INTO discord_event_delivery(event_id,html) VALUES(?,?)", (row["id"], text))
                    record = db.execute("SELECT * FROM discord_event_delivery WHERE event_id=?", (row["id"],)).fetchone()
            self._send_record("discord_event_delivery", row["id"], record)
            completed += 1
            delivered += 1
        return completed

    def _get_trade_board(self):
        if self.trade_board is None and self.trade_board_factory is not None:
            self.trade_board = self.trade_board_factory(self)
        return self.trade_board

    @staticmethod
    def _missing_message(error):
        return isinstance(error, DiscordError) and error.status == 404 and error.code == 10008

    def _save_board_ids(self, ids):
        self._set_meta(BOARD_KEY, json.dumps(ids) if ids else None)

    def sync_dashboard(self, force=False):
        if not notification_settings.board_enabled("discord"):
            return
        update_started = time.monotonic()
        if not force and update_started < self.next_board_at:
            return
        if self.positions_provider is not None:
            # Each board refresh uses authoritative exchange PnL, not the slower
            # event/history snapshot. A failed read must not refresh the clock
            # or falsely close positions; let the worker retry with backoff.
            positions = self.positions_provider()
            if not isinstance(positions, dict):
                raise ValueError("Live positions provider returned an invalid snapshot")
            updated_at = datetime.now().astimezone().isoformat(timespec="seconds")
        else:
            positions, updated_at = self._snapshot()
        board = self._get_trade_board()
        if board is not None:
            if updated_at:
                board.sync(positions)
                self.next_board_at = update_started + self.update_seconds
            return
        ids = json.loads(self._meta(BOARD_KEY) or "[]")
        if not updated_at:
            return
        if not positions:
            payload = message_payloads(CLOSED_TEXT)[0]
            while ids:
                try:
                    self.transport.edit(ids[0], payload)
                except DiscordError as error:
                    if not self._missing_message(error):
                        raise
                ids.pop(0)
                self._save_board_ids(ids)
            self.next_board_at = update_started + self.update_seconds
            return

        text = self.format_pnl_report(positions)
        try:
            source_time = datetime.fromisoformat(updated_at)
            lines = text.splitlines()
            beijing_time = source_time.astimezone(ZoneInfo("Asia/Shanghai"))
            time_index = None
            for index, line in enumerate(lines):
                if line.startswith("更新："):
                    lines[index] = "更新：" + html.escape(beijing_time.strftime("%m-%d %H:%M:%S")) + "（北京时间）"
                    time_index = index
                    break
                if line.startswith("更新时间："):
                    lines[index] = "更新时间：" + html.escape(beijing_time.strftime("%Y-%m-%d %H:%M:%S")) + "（北京时间）"
                    time_index = index
                    break
            if time.time() - source_time.timestamp() > 90:
                lines.insert(time_index + 1 if time_index is not None else 1,
                             "⚠️ 数据同步延迟，以下为最近一次保存的持仓。")
            text = "\n".join(lines)
        except (ValueError, IndexError):
            pass
        payloads = board_payloads(text)
        for index, payload in enumerate(payloads):
            if index < len(ids):
                try:
                    self.transport.edit(ids[index], payload)
                except DiscordError as error:
                    if not self._missing_message(error):
                        raise
                    new_id = self.transport.send(payload)
                    if not new_id:
                        raise DiscordError("Discord board send was not confirmed")
                    ids[index] = str(new_id)
                    self._save_board_ids(ids)
            else:
                new_id = self.transport.send(payload)
                if not new_id:
                    raise DiscordError("Discord board send was not confirmed")
                ids.append(str(new_id))
                self._save_board_ids(ids)
        while len(ids) > len(payloads):
            try:
                self.transport.delete(ids[-1])
            except DiscordError as error:
                if not self._missing_message(error):
                    raise
            ids.pop()
            self._save_board_ids(ids)
        self.next_board_at = update_started + self.update_seconds

    def poll_once(self):
        self.deliver_notices()
        count = self.deliver_events()
        self.sync_dashboard(force=count > 0)
        return count

    def start(self):
        if self.thread is not None:
            return
        with self._connect() as db:
            connected = db.execute("SELECT 1 FROM discord_notices WHERE dedupe_key='connected'").fetchone()
        self.enqueue_notice(CONNECTED_TEXT, key="connected")
        if connected and self.send_startup_summary:
            positions, updated_at = self._snapshot()
            if updated_at:
                self.enqueue_notice(self.format_summary(positions))
        self.thread = threading.Thread(target=self._run, name="bitget-discord", daemon=True)
        self.thread.start()

    def wake(self):
        self._wake.set()

    def stop(self):
        self._stop.set()
        self._wake.set()
        if self.thread is not None and self.thread is not threading.current_thread():
            self.thread.join(timeout=self.timeout + 2)

    def _run(self):
        LOG.info("Discord delivery worker started (board interval %.1fs)", self.update_seconds)
        failures = 0
        next_attempt = 0.0
        while not self._stop.is_set():
            remaining = next_attempt - time.monotonic()
            if remaining > 0:
                self._wake.wait(timeout=min(remaining, 5))
                self._wake.clear()
                continue
            cycle_started = time.monotonic()
            try:
                self.poll_once()
                if failures:
                    LOG.info("Discord delivery recovered")
                failures = 0
                next_attempt = cycle_started + min(1.0, self.update_seconds)
                # If a board deadline narrowly missed this poll, wake at that
                # deadline instead of delaying the edit for another full poll.
                if self.next_board_at > cycle_started:
                    next_attempt = min(next_attempt, self.next_board_at)
                next_attempt = max(next_attempt, time.monotonic())
            except Exception as error:
                failures += 1
                if isinstance(error, DiscordError):
                    if error.status in {401, 403} or (error.status == 404 and error.code == 10015):
                        self.disabled = True
                        LOG.error("Discord webhook unavailable (HTTP %s, code %s); Telegram continues. Update webhook and restart monitor.", error.status, error.code)
                        break
                    delay = error.retry_after if error.retry_after is not None else min(60, 2 ** min(failures, 6))
                    # Only safe numeric fields are logged, never endpoint URLs or response bodies.
                    LOG.warning("Discord delivery deferred (HTTP %s, code %s), retry in %.1fs", error.status, error.code, max(1, delay))
                else:
                    delay = min(60, 2 ** min(failures, 6))
                    LOG.warning("Discord delivery failed (%s), retry in %ss", type(error).__name__, delay)
                next_attempt = time.monotonic() + max(1, delay)
        LOG.info("Discord delivery worker stopped")
