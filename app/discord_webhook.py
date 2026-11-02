"""Small dependency-free Discord webhook transport and TG HTML formatter."""
from __future__ import annotations

import copy
import http.client
import json
import math
import re
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from html.parser import HTMLParser
from decimal import Decimal, InvalidOperation
from typing import Any

DESCRIPTION_LIMIT = 3900
EMBED_COLOR = 0x78974E
_TICK = chr(96)
_MARKDOWN = frozenset("\\*_{}[]()#+-.!>|~" + _TICK)


class DiscordError(RuntimeError):
    """A safe error that never includes an endpoint, response body, or token."""

    def __init__(
        self,
        message: str = "Discord request failed",
        *,
        status: int | None = None,
        code: int | None = None,
        retry_after: float | None = None,
    ):
        self.status = status
        self.code = code
        self.retry_after = retry_after
        super().__init__(message)


def _utf16_len(value: str) -> int:
    return sum(2 if ord(char) > 0xFFFF else 1 for char in value)


def _escape(value: str) -> str:
    return "".join("\\" + char if char in _MARKDOWN else char for char in value)


class _HTMLLines(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.lines: list[list[tuple[str, bool, bool]]] = [[]]
        self.bold = 0
        self.code = 0

    def handle_starttag(self, tag: str, attrs: list) -> None:
        if tag in {"b", "strong"}:
            self.bold += 1
        elif tag == "code":
            self.code += 1
        elif tag == "br":
            self.lines.append([])

    def handle_endtag(self, tag: str) -> None:
        if tag in {"b", "strong"}:
            self.bold = max(0, self.bold - 1)
        elif tag == "code":
            self.code = max(0, self.code - 1)

    def handle_data(self, data: str) -> None:
        for index, part in enumerate(data.split("\n")):
            if index:
                self.lines.append([])
            if part:
                self.lines[-1].append((part, bool(self.bold), bool(self.code)))


def _render_line(spans: list[tuple[str, bool, bool]]) -> str:
    rendered, bold_open = [], False
    for value, bold, code in spans:
        if bold != bold_open:
            rendered.append("**")
            bold_open = bold
        # Literal backticks in data cannot close an inline code delimiter.
        text = _TICK + value + _TICK if code and _TICK not in value else _escape(value)
        rendered.append(text)
    if bold_open:
        rendered.append("**")
    return "".join(rendered)


def _split_plain_line(value: str, limit: int) -> list[str]:
    """Split escaped text only between complete characters/escape sequences."""
    pieces, current, size = [], [], 0
    for char in value:
        token = _escape(char)
        units = _utf16_len(token)
        if current and size + units > limit:
            pieces.append("".join(current))
            current, size = [], 0
        current.append(token)
        size += units
    if current:
        pieces.append("".join(current))
    return pieces or [""]


def message_payloads(html_text: str) -> list[dict[str, Any]]:
    """Convert existing TG HTML into individually valid Discord embed pages."""
    parser = _HTMLLines()
    parser.feed(str(html_text))
    parser.close()
    lines: list[str] = []
    for spans in parser.lines:
        rendered = _render_line(spans)
        if _utf16_len(rendered) <= DESCRIPTION_LIMIT:
            lines.append(rendered)
        else:
            plain = "".join(value for value, _bold, _code in spans)
            lines.extend(_split_plain_line(plain, DESCRIPTION_LIMIT))

    descriptions, current, size = [], [], 0
    for line in lines:
        units = _utf16_len(line)
        separator = 1 if current else 0
        if current and size + separator + units > DESCRIPTION_LIMIT:
            description = "\n".join(current).strip()
            if description:
                descriptions.append(description)
            current, size = [], 0
            separator = 0
        current.append(line)
        size += separator + units
    description = "\n".join(current).strip()
    if description:
        descriptions.append(description)
    if not descriptions:
        descriptions = ["—"]

    return [
        {
            "embeds": [{"description": description, "color": EMBED_COLOR}],
            "allowed_mentions": {"parse": []},
        }
        for description in descriptions
    ]


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        return None


def _seconds(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) and number >= 0 else None


def _error_code(value: Any) -> int | None:
    # Never accidentally include a response string in a logged exception.
    return value if isinstance(value, int) and not isinstance(value, bool) else None


class DiscordWebhook:
    def __init__(self, url: str, timeout: int | float):
        try:
            parsed = urllib.parse.urlsplit(url.strip())
            valid = (
                parsed.scheme == "https"
                and parsed.netloc == "discord.com"
                and not parsed.query
                and not parsed.fragment
                and re.fullmatch(
                    r"/api/(?:v10/)?webhooks/[0-9]+/[A-Za-z0-9_-]+/?",
                    parsed.path,
                )
            )
        except (TypeError, ValueError, AttributeError):
            valid = False
        if not valid:
            raise DiscordError("Discord webhook URL is invalid")
        self._url = urllib.parse.urlunsplit(
            (parsed.scheme, parsed.netloc, parsed.path.rstrip("/"), "", "")
        )
        self.timeout = timeout
        self._opener = urllib.request.build_opener(_NoRedirect())
        self._blocked_until = 0.0

    def _cooldown(self, seconds: float) -> None:
        self._blocked_until = max(
            self._blocked_until, time.monotonic() + seconds
        )

    def _check_cooldown(self) -> None:
        remaining = self._blocked_until - time.monotonic()
        if remaining > 0:
            raise DiscordError(
                "Discord rate limit cooldown",
                status=429,
                retry_after=remaining,
            )

    def _observe_headers(self, headers) -> None:
        if headers is None:
            return
        if str(headers.get("X-RateLimit-Remaining", "")).strip() == "0":
            delay = _seconds(headers.get("X-RateLimit-Reset-After"))
            if delay is not None:
                self._cooldown(delay)

    def _request(
        self,
        method: str,
        *,
        message_id: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        self._check_cooldown()
        url = self._url
        if message_id is not None:
            if not re.fullmatch(r"[0-9]+", str(message_id)):
                raise DiscordError("Discord message ID is invalid")
            url += "/messages/" + str(message_id)
        elif method == "POST":
            url += "?wait=true"

        body = None
        if payload is not None:
            safe_payload = copy.deepcopy(payload)
            # Discord edits do not inherit this setting from the original send.
            safe_payload["allowed_mentions"] = {"parse": []}
            try:
                body = json.dumps(
                    safe_payload, ensure_ascii=False, allow_nan=False
                ).encode("utf-8")
            except (TypeError, ValueError, UnicodeError):
                raise DiscordError("Discord payload cannot be encoded") from None
        request = urllib.request.Request(
            url,
            data=body,
            method=method,
            headers={
                "Content-Type": "application/json",
                "User-Agent": "BitgetPositionMonitor/1.0",
            },
        )

        try:
            with self._opener.open(request, timeout=self.timeout) as response:
                status = response.status
                self._observe_headers(response.headers)
                raw = response.read()
        except urllib.error.HTTPError as exc:
            status = exc.code
            self._observe_headers(exc.headers)
            try:
                details = json.loads(exc.read(65536).decode("utf-8"))
                details = details if isinstance(details, dict) else {}
            except (ValueError, UnicodeError, OSError, http.client.HTTPException):
                details = {}
            finally:
                exc.close()
            code = _error_code(details.get("code"))
            if status == 429:
                candidates = [
                    _seconds(details.get("retry_after")),
                    _seconds(exc.headers.get("Retry-After")) if exc.headers else None,
                ]
                delays = [delay for delay in candidates if delay is not None]
                delay = max(delays) if delays else 1.0
                # A zero response still defers to avoid an immediate hot loop.
                delay = max(delay, 0.1)
                self._cooldown(delay)
                raise DiscordError(
                    "Discord HTTP 429; retry later",
                    status=status,
                    code=code,
                    retry_after=max(
                        delay, self._blocked_until - time.monotonic()
                    ),
                ) from None
            label = f"Discord HTTP {status}"
            if code is not None:
                label += f" (code {code})"
            raise DiscordError(label, status=status, code=code) from None
        except (
            urllib.error.URLError, socket.timeout, TimeoutError, OSError,
            http.client.HTTPException,
        ):
            raise DiscordError("Discord network request failed") from None

        if status == 204 and method == "DELETE":
            return {}
        try:
            result = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeError):
            raise DiscordError(
                "Discord response did not confirm a message", status=status
            ) from None
        if not isinstance(result, dict):
            raise DiscordError(
                "Discord response did not confirm a message", status=status
            )
        if method != "DELETE" and not re.fullmatch(
            r"[0-9]+", str(result.get("id") or "")
        ):
            raise DiscordError(
                "Discord response did not confirm a message", status=status
            )
        return result

    def send(self, payload: dict[str, Any]) -> str:
        return str(self._request("POST", payload=payload)["id"])

    def edit(self, message_id: str, payload: dict[str, Any]) -> str:
        return str(
            self._request("PATCH", message_id=message_id, payload=payload)["id"]
        )

    def delete(self, message_id: str) -> None:
        try:
            self._request("DELETE", message_id=message_id)
        except DiscordError as exc:
            if exc.status == 404 and exc.code == 10008:
                return
            raise

    def get(self, message_id: str) -> dict[str, Any]:
        return self._request("GET", message_id=message_id)


BOARD_POSITIVE = 0x22C55E
BOARD_NEGATIVE = 0xEF4444
BOARD_NEUTRAL = 0x94A3B8


def _inline_value(value: str) -> str:
    return _TICK + value + _TICK if _TICK not in value else _escape(value)


def _embed_size(embed: dict[str, Any]) -> int:
    parts = [embed.get("title", ""), embed.get("description", ""),
             embed.get("footer", {}).get("text", "")]
    for field in embed.get("fields", []):
        parts.extend((field["name"], field["value"]))
    return sum(_utf16_len(part) for part in parts)


def _valid_board_embed(embed: dict[str, Any]) -> bool:
    fields = embed.get("fields", [])
    return (
        _utf16_len(embed.get("title", "")) <= 256
        and _utf16_len(embed.get("description", "")) <= 4096
        and _utf16_len(embed.get("footer", {}).get("text", "")) <= 2048
        and len(fields) <= 25
        and all(_utf16_len(field["name"]) <= 256
                and 0 < _utf16_len(field["value"]) <= 1024 for field in fields)
        and _embed_size(embed) <= 6000
    )


def _without_rebate_note(html_text: str) -> str:
    # Older durable events can still contain the retired explanatory footer.
    return re.sub(r"(?m)^[ \t]*(?:<i>)?净盈亏已计手续费、资金费及[ \t]*[0-9]+(?:\.[0-9]+)?%[ \t]*返佣。[ \t]*(?:</i>)?[ \t]*(?:\n|$)", "", str(html_text))


def _position_lines(html_text: str) -> list[tuple[str, str]]:
    parser = _HTMLLines()
    parser.feed(html_text)
    parser.close()
    return [(plain, _render_line(spans)) for spans in parser.lines
            if (plain := "".join(value for value, _b, _c in spans).strip())]


def _legacy_closed_lines(lines: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """Normalize already queued October 15 closes without inventing amounts."""
    if not lines or lines[0][0] != "✅ BITGET · 已平仓":
        return lines
    if lines[-1][0] == "成交记录尚未完整同步，最终盈亏与费用以成交记录为准。":
        lines = lines[:-1]
    if len(lines) not in {7, 8}:
        return lines
    title, pnl, opening, closing, quantity = lines[1:6]
    pending = pnl[0] == "⏳ 最终盈亏：待结算"
    if len(lines) != (7 if pending else 8) or not quantity[0].startswith("平仓数量："):
        return lines
    time_line = lines[-1]
    if not re.fullmatch(r"平仓时间：.+（北京时间）", time_line[0]):
        return lines
    pnl_plain = "⏳ 净盈亏：待结算" if pending else pnl[0]
    pnl_rendered = "**⏳ 净盈亏：待结算**" if pending else pnl[1]
    fees = (("手续费：待同步  ·  返佣：待同步  ·  资金费：待同步",
             "手续费：`待同步`  ·  返佣：`待同步`  ·  资金费：`待同步`") if pending else lines[6])
    return [("📊 BITGET · 已平仓", "**📊 BITGET · 已平仓**"), time_line, title,
            (pnl_plain + "  ·  净收益率：—", pnl_rendered + "  ·  净收益率：`—`"),
            opening, closing, ("止盈/止损：— / —", "止盈/止损：`—` / `—`"),
            (quantity[0].replace("平仓数量：", "数量：", 1),
             quantity[1].replace("平仓数量：", "数量：", 1)), fees]


def _position_card(block: list[tuple[str, str]], status: str, footer: dict,
                   warnings: list[str]) -> dict[str, Any] | None:
    """One field layout serves open events, live positions and closed events."""
    if len(block) == 7:
        block = block[:4] + [("🟣 盈亏持平价格：—", "🟣 盈亏持平价格：`—`")] + block[4:]
    if len(block) != 8:
        return None
    title, pnl, opening, current, breakeven, stops, size, fees = [item[0] for item in block]
    if not breakeven.startswith("🟣 盈亏持平价格：") or not breakeven[len("🟣 盈亏持平价格："):]:
        return None
    if not re.fullmatch(r".+ · .+(?: · .+×)?", title):
        return None
    match = re.fullmatch(
        r"[🟢🔴⚪] 净盈亏：([+−-]?[0-9][0-9,]*(?:\.[0-9]+)?(?:[Ee][+−-]?[0-9]+)?) (.+?)  ·  净收益率：(.+)", pnl)
    pending = pnl == "⏳ 净盈亏：待结算  ·  净收益率：—"
    if not match and not (status == "已平仓" and pending):
        return None
    current_name = "🟠 平仓均价" if status == "已平仓" else "🟠 标记价格"
    if not opening.startswith("🔵 开仓均价：") or not current.startswith(current_name + "："):
        return None
    if not stops.startswith("止盈/止损：") or not size.startswith("数量："):
        return None
    quantity = size[len("数量："):].split("  ·  持仓：")
    stop_values = stops[len("止盈/止损："):].split(" / ")
    if (len(quantity) != 2 or not all(quantity) or len(stop_values) != 2 or not all(stop_values)
            or not re.fullmatch(r"手续费：.+  ·  返佣：.+  ·  资金费：.+", fees)):
        return None
    amount = Decimal(0)
    if match:
        try:
            amount = Decimal(match[1].replace(",", "").replace("−", "-"))
        except InvalidOperation:
            return None
        if not amount.is_finite():
            return None
    description = block[1][1].replace("  ·  净收益率：", "\n净收益率：")
    if warnings:
        description = "\n".join(warnings) + "\n\n" + description
    return {
        "title": _escape(title) + " · " + status,
        "description": description,
        "color": BOARD_POSITIVE if amount > 0 else BOARD_NEGATIVE if amount < 0 else BOARD_NEUTRAL,
        "fields": [
            {"name": "🔵 开仓均价", "value": _inline_value(opening[len("🔵 开仓均价："):]), "inline": False},
            {"name": current_name, "value": _inline_value(current[len(current_name) + 1:]), "inline": False},
            {"name": "🟣 盈亏持平价格", "value": _inline_value(breakeven[len("🟣 盈亏持平价格："):]), "inline": False},
            {"name": "止盈 / 止损", "value": " / ".join(_inline_value(value) for value in stop_values), "inline": False},
            {"name": "数量", "value": _inline_value(quantity[0]), "inline": True},
            {"name": "持仓时长", "value": _inline_value(quantity[1]), "inline": True},
            {"name": "费用明细", "value": block[7][1], "inline": False},
        ],
        "footer": footer,
    }


def _position_payloads(html_text: str, *, event: bool) -> list[dict[str, Any]]:
    cleaned = _without_rebate_note(html_text)
    fallback = lambda: message_payloads(cleaned)
    lines = _position_lines(cleaned)
    if event:
        lines = _legacy_closed_lines(lines)
    if not lines:
        return fallback()
    heading = re.fullmatch(r"📊 BITGET · 持仓（([0-9]+)）", lines[0][0])
    if not event and heading and int(heading[1]) > 0:
        count, status, time_prefix = int(heading[1]), "持仓", ("更新：", "更新时间：")
    elif event and lines[0][0] in {"📊 BITGET · 已开仓", "📊 BITGET · 已平仓"}:
        status = lines[0][0].split(" · ", 1)[1]
        count, time_prefix = 1, (("开仓时间：",) if status == "已开仓" else ("平仓时间：",))
    else:
        return fallback()
    times, warnings, totals, body = [], [], [], []
    for plain, rendered in lines[1:]:
        if plain.startswith(time_prefix):
            times.append(plain)
        elif not event and plain.startswith("⚠️ 数据同步延迟"):
            warnings.append(rendered)
        elif not event and plain.startswith("合计净盈亏："):
            totals.append(rendered)
        else:
            body.append((plain, rendered))
    if len(times) != 1 or len(body) not in {count * 7, count * 8}:
        return fallback()
    block_size = len(body) // count
    if event and not times[0].endswith("（北京时间）"):
        return fallback()
    if (count == 1 and totals) or (count > 1 and not totals):
        return fallback()
    footer = {"text": times[0]}
    embeds = []
    if count > 1:
        embeds.append({"title": f"📊 BITGET · 持仓总览（{count}）",
                       "description": "\n".join(warnings + totals),
                       "color": BOARD_NEUTRAL, "footer": footer})
    for index in range(count):
        embed = _position_card(body[index * block_size:(index + 1) * block_size], status, footer,
                               warnings if count == 1 else [])
        if embed is None:
            return fallback()
        embeds.append(embed)
    if not all(_valid_board_embed(embed) for embed in embeds):
        return fallback()
    payloads, page, size = [], [], 0
    for embed in embeds:
        length = _embed_size(embed)
        if page and (len(page) >= 10 or size + length > 6000):
            payloads.append({"embeds": page, "allowed_mentions": {"parse": []}})
            page, size = [], 0
        page.append(embed)
        size += length
    if page:
        payloads.append({"embeds": page, "allowed_mentions": {"parse": []}})
    return payloads


def board_payloads(html_text: str) -> list[dict[str, Any]]:
    return _position_payloads(html_text, event=False)


def event_payloads(html_text: str) -> list[dict[str, Any]]:
    return _position_payloads(html_text, event=True)
