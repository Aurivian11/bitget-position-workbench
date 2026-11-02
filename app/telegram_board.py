"""Persistent, resumable Telegram position boards for reports needing multiple messages."""
import json

from telegram_transport import Telegram, TelegramError, TelegramMessageMissing

PARTS_META = "pnl_message_parts"
LEGACY_META = "pnl_message_id"
CLOSED_TEXT = "<b>📊 BITGET · 当前空仓</b>\n当前没有持仓。\n下次开仓后自动展示新的持仓看板。"
MERGED_TEXT = "<b>📊 BITGET · 持仓已合并</b>\n本分段已结束，请查看本组前面的持仓看板。"


def _save(store, state):
    # Parts are authoritative. Write them before the legacy compatibility pointer.
    store.set_meta(PARTS_META, json.dumps(state, separators=(",", ":")))
    first_id = next((item for item in state["ids"] if item is not None), None)
    if first_id is not None:
        store.set_meta(LEGACY_META, str(first_id))
    else:
        store.delete_meta(LEGACY_META)


def _load(store):
    raw = store.get_meta(PARTS_META)
    if raw:
        state = json.loads(raw)
        ids, cursor = state["ids"], state["cursor"]
        if (not isinstance(ids, list) or not all(item is None or (type(item) is int and item > 0) for item in ids)
                or type(cursor) is not int or cursor < 0):
            raise TelegramError("Invalid Telegram board state")
        return {"ids": ids, "cursor": min(cursor, len(ids))}
    legacy = store.get_meta(LEGACY_META)
    state = {"ids": [int(legacy)] if legacy else [], "cursor": 0}
    _save(store, state)
    return state


def _send(telegram, text):
    message_id = telegram.send(text)
    if not message_id:
        raise TelegramError("Telegram board send was not confirmed", delivery_unknown=True)
    return int(message_id)


def _close(telegram, message_id, text):
    if message_id is None:
        return
    try:
        telegram.edit(message_id, text)
    except TelegramMessageMissing:
        # There is nothing left to close; all transient failures must propagate.
        pass


def sync_multipart_board(store, telegram, text_or_none):
    """Resume the unfinished part; keep each confirmed message ID across failures."""
    state = _load(store)
    ids = state["ids"]
    if text_or_none is None:
        while ids:
            _close(telegram, ids[-1], CLOSED_TEXT)
            ids.pop()
            state["cursor"] = 0
            _save(store, state)
        store.delete_meta(LEGACY_META)
        store.delete_meta(PARTS_META)
        return

    chunks = Telegram._chunks(text_or_none)
    for index in range(min(state["cursor"], len(chunks)), len(chunks)):
        chunk = chunks[index]
        if index < len(ids):
            if ids[index] is not None:
                try:
                    telegram.edit(ids[index], chunk)
                except TelegramMessageMissing:
                    # Remember definitive absence before attempting replacement.
                    ids[index] = None
                    state["cursor"] = index
                    _save(store, state)
            if ids[index] is None:
                ids[index] = _send(telegram, chunk)
        else:
            ids.append(_send(telegram, chunk))
        state["cursor"] = index + 1
        _save(store, state)

    # Retire surplus messages one at a time; never forget a failed closure.
    while len(ids) > len(chunks):
        _close(telegram, ids[-1], MERGED_TEXT)
        ids.pop()
        state["cursor"] = min(state["cursor"], len(chunks))
        _save(store, state)
    state["cursor"] = 0
    _save(store, state)
