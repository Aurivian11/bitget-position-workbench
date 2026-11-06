"""Serialized Telegram transport with bounded rate limiting and safe errors."""
from __future__ import annotations

import hashlib
import http.client
import json
import math
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any


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
        self.chat_id = chat_id
        self.timeout = timeout
        self._lock = threading.RLock()
        self._last_request_started: float | None = None
        self._not_before = 0.0
        # Retain only completed chunk count and ID, never the message content.
        self._send_progress: dict[bytes, tuple[int, Any]] = {}

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
                body = urllib.parse.urlencode(data).encode()
                request = urllib.request.Request(
                    f"{self.base_url}/{method}", data=body, method="POST"
                )
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    payload = json.loads(response.read().decode())
            except urllib.error.HTTPError as exc:
                try:
                    payload = json.loads(exc.read().decode())
                except (ValueError, UnicodeError, OSError):
                    payload = {}
                finally:
                    exc.close()
                if not isinstance(payload, dict):
                    payload = {}
                code = payload.get("error_code", exc.code)
                try:
                    code = int(code)
                except (TypeError, ValueError):
                    code = exc.code
                return self._handle_error(method, code, payload)
            except (OSError, ValueError, UnicodeError, http.client.HTTPException):
                # URLs contain credentials. Never expose exception text or causes.
                raise TelegramError("Telegram transport failed; delivery status may be unknown", delivery_unknown=(method == "sendMessage")) from None
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
