"""Shared exchange clock for signing, independent of the host wall clock."""
import json
import threading
import time
import urllib.request


class ClockSyncError(RuntimeError):
    """Clock unavailable; deliberately contains no endpoint or response details."""


class BitgetClock:
    TTL_SECONDS = 60.0
    MAX_STALE_SECONDS = 15 * 60.0
    RETRY_SECONDS = 5.0

    def __init__(self, api_base, timeout):
        self.api_base = str(api_base).rstrip("/")
        self.timeout = min(5.0, max(0.1, float(timeout)))
        self._lock = threading.Lock()
        self._server_ms = None
        self._anchor = None
        self._generation = 0
        self._retry_at = 0.0

    def _cached(self, now):
        if self._anchor is None or now - self._anchor > self.MAX_STALE_SECONDS:
            raise ClockSyncError("Bitget signing clock is unavailable")
        return int(self._server_ms + (now - self._anchor) * 1000), self._generation

    def _refresh(self):
        started = time.monotonic()
        try:
            request = urllib.request.Request(
                self.api_base + "/api/v2/public/time",
                headers={"User-Agent": "bitget-position-clock/1.0"},
            )
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                raw = response.read(16385)
            ended = time.monotonic()
            if len(raw) > 16384 or ended < started:
                raise ValueError("Invalid clock response")
            payload = json.loads(raw)
            if not isinstance(payload, dict) or payload.get("code") != "00000":
                raise ValueError("Invalid clock response")
            data = payload.get("data")
            value = data.get("serverTime") if isinstance(data, dict) else None
            if isinstance(value, bool) or not isinstance(value, (int, str)):
                raise ValueError("Invalid server timestamp")
            if isinstance(value, str) and not value.isdigit():
                raise ValueError("Invalid server timestamp")
            server_ms = int(value)
            # Reject seconds, empty/negative values and corrupt timestamp widths.
            if not 1_000_000_000_000 <= server_ms <= 9_999_999_999_999:
                raise ValueError("Invalid server timestamp")
        except Exception:
            self._retry_at = time.monotonic() + self.RETRY_SECONDS
            raise ClockSyncError("Bitget signing clock synchronization failed") from None
        self._server_ms = server_ms
        self._anchor = (started + ended) / 2.0
        self._generation += 1
        self._retry_at = 0.0

    def stamp(self):
        """Return (milliseconds, generation), refreshing an aging anchor safely."""
        with self._lock:
            now = time.monotonic()
            if self._anchor is not None and now - self._anchor < self.TTL_SECONDS:
                return self._cached(now)
            if now >= self._retry_at:
                try:
                    self._refresh()
                except ClockSyncError:
                    # A short time-endpoint failure must not break a good anchor.
                    return self._cached(time.monotonic())
            return self._cached(time.monotonic())

    def resync(self, generation):
        """Refresh a rejected generation once; coalesce concurrent callers."""
        with self._lock:
            if generation != self._generation:
                return self._cached(time.monotonic())
            if time.monotonic() < self._retry_at:
                raise ClockSyncError("Bitget signing clock synchronization deferred")
            self._refresh()
            return self._cached(time.monotonic())


_CLOCKS = {}
_CLOCKS_LOCK = threading.Lock()


def get_clock(api_base, timeout):
    """Share one synchronized clock per API base across delivery workers."""
    base = str(api_base).rstrip("/")
    with _CLOCKS_LOCK:
        if base not in _CLOCKS:
            _CLOCKS[base] = BitgetClock(base, timeout)
        return _CLOCKS[base]
