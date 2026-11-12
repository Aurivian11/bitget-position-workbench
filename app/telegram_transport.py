"""Serialized Telegram transport with bounded rate limiting and safe errors."""
from __future__ import annotations

import hashlib
import http.client
import json
import math
import socket
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any


def _ipv4_create_connection(address, timeout=socket._GLOBAL_DEFAULT_TIMEOUT, source_address=None, *args, **kwargs):
    """Connect over IPv4 only: this host's IPv6 route to Telegram intermittently stalls."""
    host, port = address[0], address[1]
    last_error = None
    for family, socktype, proto, _, sockaddr in socket.getaddrinfo(host, port, socket.AF_INET, socket.SOCK_STREAM):
        sock = socket.socket(family, socktype, proto)
        try:
            if timeout is not socket._GLOBAL_DEFAULT_TIMEOUT:
                sock.settimeout(timeout)
            if source_address:
                sock.bind(source_address)
            sock.connect(sockaddr)
            return sock
        except OSError as error:
            last_error = error
            sock.close()
    raise last_error or OSError("no IPv4 address for Telegram API")


class _IPv4HTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._create_connection = _ipv4_create_connection


class _IPv4HTTPSHandler(urllib.request.HTTPSHandler):
    def https_open(self, req):
        return self.do_open(_IPv4HTTPSConnection, req, context=self._context)


_OPENER = urllib.request.build_opener(_IPv4HTTPSHandler())
# A stalled request blocks every queued Telegram message; fail fast and retry.
TELEGRAM_TIMEOUT_CAP = 8


class TelegramError(RuntimeError):
    """An API or transport failure; messages deliberately omit remote details."""

    def __init__(self, message: str, *, delivery_unknown: bool = False):
        self.delivery_unknown = delivery_unknown
        super().__init__(message)


class TelegramRateLimited(TelegramError):
    def __init__(self, retry_after: float):
        self.retry_after = max(0.0, float(retry_after))
        super().__init__(f"Telegram rate limited; retry in {self.retry_after:.3f}s")


class TelegramMessageMissing(TelegramError):
    """The target message is definitively unavailable for editing."""


class Telegram:
    MIN_REQUEST_INTERVAL = 1.0
    MAX_PENDING_SENDS = 8

    def __init__(self, token: str, chat_id: str, timeout: int):
        self.base_url = f"https://api.telegram.org/bot{token}"
        self._path = f"/bot{token}"
        self._conn = None
        self._conn_used_at = 0.0
        self._ssl = ssl.create_default_context()
        # Optional callback(message_id) after each confirmed sendMessage.
        self.on_sent = None
        self.chat_id = chat_id
        self.timeout = min(timeout, TELEGRAM_TIMEOUT_CAP)
        self._lock = threading.RLock()
        self._last_request_started: float | None = None
        self._not_before = 0.0
        # Retain only completed chunk count and ID, never the message content.
        self._send_progress: dict[bytes, tuple[int, Any]] = {}
        # Newest message this process created; lets the board know it is buried.
        self.last_message_id: int | None = None

    def _handle_error(self, method: str, code: int, payload: dict) -> dict:
        description = str(payload.get("description", "")).lower()
        if code == 429:
            parameters = payload.get("parameters")
            retry_after = parameters.get("retry_after") if isinstance(parameters, dict) else None
            try:
                delay = float(retry_after)
                if not math.isfinite(delay) or delay < 0:
                    raise ValueError
                delay = max(1.0, delay)
            except (TypeError, ValueError, OverflowError):
                delay = 30.0
            self._not_before = max(self._not_before, time.monotonic() + delay)
            raise TelegramRateLimited(self._not_before - time.monotonic()) from None
        if code == 400 and method == "editMessageText":
            if "message is not modified" in description:
                return {"ok": True, "result": {}}
            if any(marker in description for marker in (
                "message to edit not found", "message can't be edited",
                "message can not be edited", "message cannot be edited",
            )):
                raise TelegramMessageMissing("Telegram target message is unavailable for editing") from None
        if code == 400 and method == "deleteMessage" and "message to delete not found" in description:
            raise TelegramMessageMissing("Telegram target message was already deleted") from None
        raise TelegramError(
            f"Telegram API request failed (HTTP/API {code})",
            delivery_unknown=(method == "sendMessage" and code >= 500),
        ) from None

    def _close_connection(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            except Exception:
                pass
        self._conn = None

    def _exchange(self, method: str, body: bytes) -> tuple[int, bytes]:
        """One HTTPS round trip on a reused IPv4 keep-alive connection."""
        for attempt in range(2):
            idle = time.monotonic() - self._conn_used_at
            # Never send a message on a long-idle socket the server may have dropped.
            if self._conn is not None and idle > 30:
                self._close_connection()
            reused = self._conn is not None
            if self._conn is None:
                self._conn = _IPv4HTTPSConnection("api.telegram.org", timeout=self.timeout, context=self._ssl)
            try:
                self._conn.request("POST", f"{self._path}/{method}", body=body, headers={
                    "Content-Type": "application/x-www-form-urlencoded",
                    "Connection": "keep-alive",
                })
                response = self._conn.getresponse()
                payload = response.read()
                self._conn_used_at = time.monotonic()
                if response.will_close:
                    self._close_connection()
                return response.status, payload
            except (http.client.RemoteDisconnected, BrokenPipeError, ConnectionResetError, http.client.CannotSendRequest):
                self._close_connection()
                # A dropped reused socket is retried once, except for sends whose
                # delivery could be ambiguous (the caller treats those as unknown).
                if reused and attempt == 0 and method != "sendMessage":
                    continue
                raise
            except BaseException:
                self._close_connection()
                raise
        raise OSError("Telegram connection retry exhausted")

    def _post(self, method: str, data: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            now = time.monotonic()
            if now < self._not_before:
                raise TelegramRateLimited(self._not_before - now)
            if self._last_request_started is not None:
                wait = self._last_request_started + self.MIN_REQUEST_INTERVAL - now
                if wait > 0:
                    time.sleep(wait)
            self._last_request_started = time.monotonic()
            try:
                status, raw = self._exchange(method, urllib.parse.urlencode(data).encode())
            except (OSError, ValueError, UnicodeError, http.client.HTTPException):
                # URLs contain credentials. Never expose exception text or causes.
                raise TelegramError("Telegram transport failed; delivery status may be unknown", delivery_unknown=(method == "sendMessage")) from None
            try:
                payload = json.loads(raw.decode())
            except (ValueError, UnicodeError):
                payload = None
            if status != 200:
                if not isinstance(payload, dict):
                    payload = {}
                code = payload.get("error_code", status)
                try:
                    code = int(code)
                except (TypeError, ValueError):
                    code = status
                return self._handle_error(method, code, payload)
            if not isinstance(payload, dict):
                raise TelegramError("Telegram returned an invalid response", delivery_unknown=(method == "sendMessage"))
            if not payload.get("ok"):
                try:
                    code = int(payload.get("error_code", 0))
                except (TypeError, ValueError):
                    code = 0
                return self._handle_error(method, code, payload)
            return payload

    @staticmethod
    def _chunks(text: str) -> list[str]:
        chunks, current, size = [], [], 0
        for line in text.splitlines() or [""]:
            if len(line) > 3800:
                raise TelegramError("Telegram message contains a line longer than 3800 characters")
            addition = len(line) + (1 if current else 0)
            if current and size + addition > 3800:
                chunks.append("\n".join(current))
                current, size = [], 0
            if current:
                size += 1
            current.append(line)
            size += len(line)
        if current:
            chunks.append("\n".join(current))
        return chunks

    def send(self, text: str):
        chunks = self._chunks(text)
        key = hashlib.sha256(text.encode()).digest()
        with self._lock:
            if len(chunks) > 1 and key not in self._send_progress and len(self._send_progress) >= self.MAX_PENDING_SENDS:
                raise TelegramError("Telegram has too many interrupted multipart sends")
            next_chunk, last_message_id = self._send_progress.get(key, (0, None))
            try:
                for index in range(next_chunk, len(chunks)):
                    payload = self._post("sendMessage", {
                        "chat_id": self.chat_id,
                        "text": chunks[index],
                        "parse_mode": "HTML",
                        "disable_web_page_preview": "true",
                    })
                    result = payload.get("result") or {}
                    if not isinstance(result, dict) or not result.get("message_id"):
                        raise TelegramError("Telegram send response omitted the message ID; delivery status may be unknown", delivery_unknown=True)
                    last_message_id = result["message_id"]
                    next_chunk = index + 1
            except TelegramRateLimited:
                if next_chunk:
                    self._send_progress[key] = (next_chunk, last_message_id)
                raise
            except Exception:
                # Non-429 send failures may have reached Telegram: never retry here.
                if next_chunk:
                    self._send_progress[key] = (next_chunk, last_message_id)
                raise
            self._send_progress.pop(key, None)
            try:
                self.last_message_id = max(int(last_message_id), self.last_message_id or 0)
            except (TypeError, ValueError):
                pass
            if self.on_sent is not None:
                try:
                    self.on_sent(last_message_id)
                except Exception:
                    pass
            return last_message_id

    def edit(self, message_id: int, text: str) -> None:
        if len(text) > 4000:
            raise TelegramError("PnL message is too long to edit")
        self._post("editMessageText", {
            "chat_id": self.chat_id,
            "message_id": str(message_id),
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": "true",
        })

    def delete(self, message_id: int) -> None:
        self._post("deleteMessage", {"chat_id": self.chat_id, "message_id": str(message_id)})
