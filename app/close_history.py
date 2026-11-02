"""Match final settlement to its original close event without creating duplicates."""
import json
from decimal import Decimal, InvalidOperation


def _number(value):
    try:
        result = Decimal(str(value))
        return result if result.is_finite() else None
    except (InvalidOperation, ValueError, TypeError):
        return None


def _timestamp(item, lower, upper):
    value = _number(item.get(lower) or item.get(upper))
    return int(value) if value is not None and value > 0 and value == value.to_integral_value() else None


def _coin(item):
    value = str(item.get('marginCoin') or item.get('settleCoin') or '').upper()
    if value:
        return value
    symbol = str(item.get('symbol') or '').upper()
    return next((suffix for suffix in ('USDT', 'USDC', 'USD') if symbol.endswith(suffix)), '')


def history_complete(history):
    """Only a full, finite final settlement may become a closed notification."""
    if not isinstance(history, dict):
        return False
    if not history.get('symbol') or str(history.get('holdSide') or '').lower() not in {'long', 'short'}:
        return False
    for key in ('netProfit', 'openFee', 'closeFee', 'totalFunding'):
        if _number(history.get(key)) is None:
            return False
    quantities = {}
    for key in ('openAvgPrice', 'closeAvgPrice', 'openTotalPos', 'closeTotalPos'):
        value = _number(history.get(key))
        if value is None or value <= 0:
            return False
        quantities[key] = value
    if quantities['openTotalPos'] != quantities['closeTotalPos']:
        return False
    opened = _timestamp(history, 'ctime', 'cTime')
    closed = _timestamp(history, 'utime', 'uTime')
    return opened is not None and closed is not None and closed >= opened


def history_matches(snapshot, history, product_type):
    """Require one contract, side, settlement coin and position lifetime."""
    if not isinstance(snapshot, dict) or not isinstance(history, dict):
        return False
    product = str(product_type or '').upper()
    if not product:
        return False
    if str(snapshot.get('productType') or '').upper() != product:
        return False
    if history.get('productType') and str(history['productType']).upper() != product:
        return False
    if not snapshot.get('symbol') or str(snapshot['symbol']).upper() != str(history.get('symbol') or '').upper():
        return False
    side = str(snapshot.get('holdSide') or '').lower()
    if side not in {'long', 'short'} or side != str(history.get('holdSide') or '').lower():
        return False
    coin = _coin(snapshot)
    if not coin or coin != _coin(history):
        return False
    snapshot_id = str(snapshot.get('posId') or snapshot.get('positionId') or '')
    history_id = str(history.get('positionId') or history.get('posId') or '')
    if snapshot_id and history_id and snapshot_id != history_id:
        return False
    opened = _timestamp(snapshot, 'cTime', 'ctime')
    history_opened = _timestamp(history, 'ctime', 'cTime')
    closed = _timestamp(history, 'utime', 'uTime')
    return (opened is not None and history_opened is not None and closed is not None
            and abs(opened - history_opened) <= 1000 and closed >= max(opened, history_opened))


def _snapshot(raw):
    try:
        value = json.loads(raw) if raw else None
        return value if isinstance(value, dict) else None
    except (ValueError, TypeError):
        return None


def reconcile_closed_records(db, product_type, records, start_ms, now, history_key_func):
    """Fill the original event in place; insert only genuinely missed trades.

    Returns the number of newly inserted fallback events. Delivery flags remain
    unchanged, so a pending original is sent only when its settlement is ready.
    """
    added = 0
    with db:
        # Serialise ledger decisions, including the read preceding an insertion.
        if not db.in_transaction:
            db.execute('BEGIN IMMEDIATE')
        rows = [(row[0], row[1], _snapshot(row[2])) for row in db.execute(
            "SELECT id,position_key,before_json FROM position_events "
            "WHERE event_type='closed' AND before_json IS NOT NULL"
        ).fetchall()]
        known = set()
        for _, _, snapshot in rows:
            history = (snapshot or {}).get('_history')
            if history_complete(history):
                known.add(history_key_func(str(snapshot.get('productType') or product_type), history))
        valid = [dict(h) for h in records if history_complete(h)]
        for history in sorted(valid, key=lambda h: _timestamp(h, 'utime', 'uTime')):
            if _timestamp(history, 'utime', 'uTime') < start_ms:
                continue
            key = history_key_func(product_type, history)
            candidates = [(event_id, snapshot) for event_id, position_key, snapshot in rows
                          if not position_key.startswith('history:')
                          and history_matches(snapshot, history, product_type)]
            history_id = str(history.get('positionId') or history.get('posId') or '')
            if len(candidates) > 1 and history_id:
                exact = [(event_id, snapshot) for event_id, snapshot in candidates
                         if str(snapshot.get('posId') or snapshot.get('positionId') or '') == history_id]
                if exact:
                    candidates = exact
            if len(candidates) > 1:
                # Do not attach a settlement to a guessed event or duplicate it.
                continue
            if candidates:
                event_id, snapshot = candidates[0]
                previous = snapshot.get('_history')
                if history_complete(previous) and history_key_func(product_type, previous) != key:
                    continue
                enriched = dict(snapshot, _history=history)
                if previous != history:
                    db.execute('UPDATE position_events SET before_json=? WHERE id=?',
                               (json.dumps(enriched, ensure_ascii=False), event_id))
                    rows = [(eid, pkey, enriched if eid == event_id else snap) for eid, pkey, snap in rows]
                known.add(key)
            processed = db.execute('SELECT 1 FROM processed_history_positions WHERE history_key=?', (key,)).fetchone()
            if not candidates and not processed and key not in known:
                snapshot = {
                    'productType': product_type, 'symbol': history['symbol'],
                    'holdSide': history['holdSide'], 'marginCoin': _coin(history),
                    'total': history['closeTotalPos'], 'openPriceAvg': history['openAvgPrice'],
                    'cTime': str(_timestamp(history, 'ctime', 'cTime')), '_history': history,
                }
                result = db.execute(
                    'INSERT INTO position_events(event_type,position_key,before_json,after_json,created_at) '
                    "VALUES('closed',?,?,NULL,?)",
                    ('history:' + key, json.dumps(snapshot, ensure_ascii=False), now),
                )
                rows.append((result.lastrowid, 'history:' + key, snapshot))
                known.add(key)
                added += 1
            db.execute('INSERT OR IGNORE INTO processed_history_positions(history_key,processed_at) VALUES(?,?)',
                       (key, now))
    return added
