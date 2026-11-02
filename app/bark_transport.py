"""Single-attempt Bark V2 delivery; queueing and retries belong to the caller.

Protocol: https://github.com/Finb/bark-server/blob/master/docs/API_V2.md
A successful response confirms server acceptance, not playback on the device.
"""

import datetime as dt
import email.utils
import http.client
import json
import math
import urllib.error
import urllib.parse
import urllib.request


class BarkError(Exception):
    """Safe-to-log failure with explicit retry classification."""

    def __init__(self, message, permanent=False, retry_after=None):
        super().__init__(message)
        self.permanent = bool(permanent)
        self.retry_after = retry_after


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _retry_delay(value):
    if value is None:
        return None
    try:
        delay = float(value)
        if math.isfinite(delay) and delay >= 0:
            return delay
        return None
    except (TypeError, ValueError):
        try:
            target = email.utils.parsedate_to_datetime(value)
            if target.tzinfo is None:
                target = target.replace(tzinfo=dt.timezone.utc)
            return max(0.0, (target - dt.datetime.now(dt.timezone.utc)).total_seconds())
        except (TypeError, ValueError, OverflowError):
            return None


def _document(raw):
    try:
        value = json.loads(raw.decode("utf-8"))
        return value if isinstance(value, dict) else None
    except (UnicodeError, ValueError):
        return None


def _permanent(status, document):
    # Bark can wrap an APNs device/credential rejection in HTTP 500. Inspect
    # known reasons internally, but never copy upstream text into an error.
    message = document.get("message", "") if document else ""
    if isinstance(message, str):
        compact = "".join(message.casefold().split())
        invalid_reasons = (
            "baddevicetoken", "devicetokennotfortopic", "unregistered",
            "invalidprovidertoken", "expiredprovidertoken", "missingprovidertoken",
            "badcertificate", "topicdisallowed", "invaliddevicekey",
            "devicekeyisempty", "devicekeyisinvalid",
        )
        if any(reason in compact for reason in invalid_reasons):
            return True
    return 300 <= status < 500 and status not in (408, 425, 429)


class BarkTransport:
    """POST a notification without following redirects or using system proxies."""

    _MAX_RESPONSE = 65536

    def __init__(self, base, key, sound="123", call=True, timeout=10, group="bitget-orders"):
        try:
            parsed = urllib.parse.urlsplit(base)
            valid = (
                isinstance(base, str) and parsed.scheme in ("http", "https")
                and bool(parsed.hostname) and not parsed.username and not parsed.password
                and not parsed.query and not parsed.fragment
                and isinstance(key, str) and bool(key.strip())
                and isinstance(sound, str) and isinstance(group, str)
                and math.isfinite(float(timeout)) and float(timeout) > 0
            )
            # Accessing port validates malformed or out-of-range port values.
            parsed.port
        except (TypeError, ValueError, AttributeError):
            valid = False
        if not valid:
            raise BarkError("Invalid Bark transport configuration", permanent=True) from None
        self._endpoint = base.rstrip("/") + "/push"
        self._key = key
        self._sound = sound
        self._call = bool(call)
        self._timeout = float(timeout)
        self._group = group
        self._opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}), _NoRedirect()
        )

    def send(self, title, body):
        if not isinstance(title, str) or not isinstance(body, str):
            raise BarkError("Bark title and body must be text", permanent=True)
        payload = {
            "device_key": self._key,
            "title": title,
            "body": body,
            "sound": self._sound,
            "group": self._group,
        }
        if self._call:
            payload["call"] = "1"
        try:
            encoded = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8")
            request = urllib.request.Request(
                self._endpoint, data=encoded, method="POST",
                headers={"Content-Type": "application/json; charset=utf-8", "Accept": "application/json"},
            )
            try:
                response = self._opener.open(request, timeout=self._timeout)
            except urllib.error.HTTPError as error:
                response = error
            with response:
                status = response.status
                retry_after = _retry_delay(response.headers.get("Retry-After"))
                raw = response.read(self._MAX_RESPONSE + 1)
        except (urllib.error.URLError, TimeoutError, OSError, http.client.HTTPException):
            # Exceptions may contain a URL, key or server response. Suppress
            # exception chaining to keep standard traceback logging safe too.
            raise BarkError("Bark network request failed") from None
        except (ValueError, TypeError, UnicodeError):
            raise BarkError("Invalid Bark request", permanent=True) from None

        document = _document(raw) if len(raw) <= self._MAX_RESPONSE else None
        if not 200 <= status < 300:
            raise BarkError(
                "Bark HTTP request rejected (status %d)" % status,
                permanent=_permanent(status, document), retry_after=retry_after,
            )
        if document is None or type(document.get("code")) is not int:
            raise BarkError("Bark returned an invalid confirmation", retry_after=retry_after)
        if document["code"] != 200:
            code = document["code"]
            raise BarkError(
                "Bark rejected notification (code %d)" % code,
                permanent=_permanent(code, document), retry_after=retry_after,
            )
        return document
