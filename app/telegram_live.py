"""Single Telegram writer with independent, fresh position polling."""
import json
import logging
import threading
import time
from collections import deque

from telegram_transport import TelegramError, TelegramRateLimited
from close_history import history_complete
import notification_settings

LOG = logging.getLogger("bitget-position-tg")


class TelegramLiveDispatcher:
    def __init__(self, store_factory, telegram, positions_provider, format_event,
                 sync_pnl_message, update_seconds=1.0, trade_board_factory=None):
        self.trade_board_factory = trade_board_factory
        self.trade_board = None
        self.store_factory = store_factory
        self.telegram = telegram
        self.positions_provider = positions_provider
        self.format_event = format_event
        self.sync_pnl_message = sync_pnl_message
        self.update_seconds = max(1.0, float(update_seconds))
        self.next_board_at = 0.0
        self._closed_cursor = 0
        self._condition = threading.Condition()
        self._stopping = False
        self._thread = None
        self._ready = threading.Event()
        self._startup_error = None
        self._notices = deque()
        self._notice_full = False
        self._event_text = None
        self._event_sent_id = None
        self._last_warning = float("-inf")

    def start(self):
        with self._condition:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stopping = False
            self._ready.clear()
            self._startup_error = None
            self._thread = threading.Thread(
                target=self._run, name="telegram-live", daemon=True,
            )
            self._thread.start()
        if not self._ready.wait(timeout=10):
            self.stop()
            raise RuntimeError("Telegram worker startup timed out")
        if self._startup_error is not None:
            self.stop()
            raise RuntimeError("Telegram worker database initialization failed")

    def stop(self):
        with self._condition:
            self._stopping = True
            self._condition.notify_all()
            thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=17)
            if thread.is_alive():
                LOG.warning("Telegram worker still completing an in-flight request")

    def wake(self):
        with self._condition:
            self._condition.notify_all()

    def enqueue_notice(self, text):
        with self._condition:
            if len(self._notices) >= 128:
                if not self._notice_full:
                    LOG.warning("Telegram notice queue full; new notice rejected")
                    self._notice_full = True
                return False
            self._notices.append(text)
            self._condition.notify_all()
            return True

    def _wait_until(self, deadline):
        with self._condition:
            while not self._stopping:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return True
                self._condition.wait(timeout=remaining)
            return False

    def _warn(self, kind, delay):
        now = time.monotonic()
        if now - self._last_warning >= 30:
            LOG.warning("Telegram worker delayed (%s); retry in %.1fs", kind, delay)
            self._last_warning = now

    def _is_stopping(self):
        with self._condition:
            return self._stopping

    def _cycle(self, store):
        if self._is_stopping():
            return
        # A successful HTTP send must not be repeated after a local DB lock.
        if self._event_sent_id is not None:
            store.mark_sent(self._event_sent_id)
            self._event_sent_id = None
            self._event_text = None
            return
        if self.trade_board is None and self.trade_board_factory is not None:
            self.trade_board = self.trade_board_factory(store, self.telegram)
        events = store.unsent_events(closed_after_id=self._closed_cursor)
        for row in events:
            if row["event_type"] == "closed":
                self._closed_cursor = row["id"]
            if row["event_type"] not in {"opened", "updated"} and not notification_settings.event_enabled("telegram", row["event_type"]):
                with store.db:
                    store.db.execute("UPDATE position_events SET telegram_sent=2 WHERE id=?", (row["id"],))
                continue
            # Opening and parameter changes use the editable board; size changes notify separately.
            if row["event_type"] in {"opened", "updated"}:
                store.mark_sent(row["id"])
                continue
            if row["event_type"] == "closed":
                before = json.loads(row["before_json"]) if row["before_json"] else {}
                if not history_complete(before.get("_history") or {}):
                    continue
                if self.trade_board is not None:
                    if not self.trade_board.close(before):
                        continue
                    self._event_sent_id = row["id"]
                    store.mark_sent(row["id"])
                    self._event_sent_id = None
                    return
            if self._event_text is None or self._event_text[0] != row["id"]:
                before = json.loads(row["before_json"]) if row["before_json"] else None
                after = json.loads(row["after_json"]) if row["after_json"] else None
                self._event_text = (
                    row["id"], self.format_event(row["event_type"], before, after),
                )
            if not self.telegram.send(self._event_text[1]):
                raise TelegramError("Telegram send returned no message ID")
            self._event_sent_id = row["id"]
            store.mark_sent(row["id"])
            self._event_sent_id = None
            self._event_text = None
            return
        with self._condition:
            notice = self._notices[0] if self._notices else None
        if notice is not None:
            if not self.telegram.send(notice):
                raise TelegramError("Telegram send returned no message ID")
            with self._condition:
                self._notices.popleft()
                self._notice_full = False
            return
        if not notification_settings.board_enabled("telegram"):
            return
        board_started = time.monotonic()
        if board_started < self.next_board_at:
            return
        positions = self.positions_provider()
        if self._is_stopping():
            return
        if self.trade_board is not None:
            self.trade_board.sync(positions)
        else:
            self.sync_pnl_message(
                store, self.telegram, positions, force_new=False,
            )
        self.next_board_at = board_started + self.update_seconds

    def _run(self):
        store, deadline, failures = None, 0.0, 0
        delayed = False
        try:
            try:
                store = self.store_factory()
            except Exception as error:
                self._startup_error = type(error).__name__
                LOG.error("Telegram database initialization failed (%s)", type(error).__name__)
                return
            finally:
                self._ready.set()
            while self._wait_until(deadline):
                started = time.monotonic()
                try:
                    self._cycle(store)
                    if delayed:
                        LOG.info("Telegram worker recovered")
                    failures, delayed = 0, False
                    # Events remain prompt even when the user slows board refreshes.
                    # The transport independently enforces its one-request/second limit.
                    deadline = max(started + 1.0, time.monotonic())
                except TelegramRateLimited as error:
                    delay = max(1.0, float(error.retry_after))
                    deadline = time.monotonic() + delay
                    delayed = True
                    self._warn("rate limit", delay)
                except Exception as error:
                    failures += 1
                    delay = min(30.0, 2.0 ** min(failures, 5))
                    if getattr(error, "delivery_unknown", False):
                        delay = 30.0
                    deadline = time.monotonic() + max(1.0, delay)
                    delayed = True
                    self._warn(type(error).__name__, delay)
        finally:
            if store is not None:
                try:
                    store.db.close()
                except Exception as error:
                    LOG.warning("Telegram database close failed (%s)", type(error).__name__)
