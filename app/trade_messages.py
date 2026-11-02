"""A live position message and a separate final settlement, each delivered once."""
import json
from close_history import history_complete, history_matches

class UncertainDelivery(RuntimeError):
    """A create request may have succeeded; never create a duplicate blindly."""

def identity(item):
    history = item.get('_history') or {}
    symbol = str(item.get('symbol') or history.get('symbol') or '')
    side = str(item.get('holdSide') or history.get('holdSide') or '').lower()
    product = str(item.get('productType') or '').upper()
    coin = str(item.get('marginCoin') or history.get('marginCoin') or history.get('settleCoin') or '')
    if not coin:
        coin = next((s for s in ('USDT', 'USDC', 'USD') if symbol.endswith(s)), '')
    created = int(item.get('cTime') or item.get('ctime') or history.get('ctime') or 0)
    if not product or not symbol or side not in {'long', 'short'} or not coin or created <= 0:
        raise ValueError('Position lifetime identity is incomplete')
    return (product, symbol, side, coin, created)

def same_lifetime(left, right):
    a, b = identity(left), identity(right)
    if a[:4] != b[:4]: return False
    left_id = str(left.get('posId') or left.get('positionId') or (left.get('_history') or {}).get('positionId') or '')
    right_id = str(right.get('posId') or right.get('positionId') or (right.get('_history') or {}).get('positionId') or '')
    if left_id and right_id and left_id != right_id: return False
    if a[4] == b[4]: return True
    return bool(left_id and right_id and left_id == right_id and abs(a[4] - b[4]) <= 1000)

class TradeMessageBoard:
    def __init__(self, connect, channel, send, edit, is_missing, definitely_unsent,
                 format_live, format_closed):
        self.connect, self.channel = connect, channel
        self.send, self.edit = send, edit
        self.is_missing, self.definitely_unsent = is_missing, definitely_unsent
        self.format_live, self.format_closed = format_live, format_closed
        self._confirmed = {}
        self._settlement = None
        with self.connect() as db, db:
            db.execute('''CREATE TABLE IF NOT EXISTS trade_message_delivery (
                channel TEXT NOT NULL, trade_key TEXT NOT NULL,
                snapshot TEXT NOT NULL, message_id TEXT,
                phase TEXT NOT NULL DEFAULT 'live', sending INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY(channel,trade_key))''')

    def _rows(self):
        with self.connect() as db:
            rows = db.execute('SELECT trade_key,snapshot,message_id,phase,sending FROM trade_message_delivery WHERE channel=?', (self.channel,)).fetchall()
        return [dict(zip(('trade_key','snapshot','message_id','phase','sending'), r)) for r in rows]

    def _resolve(self, item, *, closing=False, create=True):
        key = json.dumps(identity(item), separators=(',', ':'))
        rows = self._rows()
        exact = [r for r in rows if r['trade_key'] == key]
        if exact and not same_lifetime(json.loads(exact[0]['snapshot']), item):
            raise ValueError('Conflicting position identifiers for one lifetime')
        candidates = exact or [r for r in rows if same_lifetime(json.loads(r['snapshot']), item)]
        if not candidates and closing and item.get('_history'):
            candidates = [r for r in rows if history_matches(json.loads(r['snapshot']), item['_history'], item['productType'])]
            history_id = str(item['_history'].get('positionId') or '')
            if len(candidates) > 1 and history_id:
                matches = [r for r in candidates if str(json.loads(r['snapshot']).get('posId') or json.loads(r['snapshot']).get('positionId') or '') == history_id]
                if matches: candidates = matches
        if len(candidates) > 1: raise ValueError('Ambiguous position lifetime')
        if candidates: return candidates[0]
        if not create: return None
        with self.connect() as db, db:
            db.execute('INSERT INTO trade_message_delivery(channel,trade_key,snapshot) VALUES(?,?,?)',
                       (self.channel, key, json.dumps(item)))
        return {'trade_key': key, 'snapshot': json.dumps(item), 'message_id': None, 'phase': 'live', 'sending': 0}

    def _write(self, row, **updates):
        allowed = {'snapshot', 'message_id', 'phase', 'sending'}
        if not updates or not set(updates) <= allowed: raise ValueError('Invalid message state update')
        with self.connect() as db, db:
            db.execute('UPDATE trade_message_delivery SET ' + ','.join(k+'=?' for k in updates) + ' WHERE channel=? AND trade_key=?',
                       (*updates.values(), self.channel, row['trade_key']))
        row.update(updates)

    def _deliver(self, row, payload):
        key = row['trade_key']
        if key in self._confirmed:
            self._write(row, message_id=self._confirmed[key], sending=0)
            del self._confirmed[key]
        if row['message_id']:
            try:
                self.edit(row['message_id'], payload)
                return
            except Exception as error:
                if not self.is_missing(error): raise
                self._write(row, message_id=None, sending=0)
        if row['sending']: raise UncertainDelivery('Previous message creation needs confirmation')
        # Record intent first. Interrupted/ambiguous creation must not be retried.
        self._write(row, sending=1)
        try:
            message_id = self.send(payload)
            if not message_id: raise UncertainDelivery('Message creation omitted its identifier')
        except Exception as error:
            if self.definitely_unsent(error): self._write(row, sending=0)
            raise
        self._confirmed[key] = str(message_id)
        self._write(row, message_id=str(message_id), sending=0)
        del self._confirmed[key]

    def _adopt_legacy(self, positions):
        if not positions: return
        legacy_keys = ('pnl_message_parts', 'pnl_message_id') if self.channel == 'telegram' else ('discord_pnl_message_ids',)
        with self.connect() as db:
            values = {k: v for k, v in db.execute('SELECT key,value FROM metadata') if k in legacy_keys}
        if not values: return
        if self.channel == 'telegram':
            ids = json.loads(values['pnl_message_parts'])['ids'] if values.get('pnl_message_parts') else [values.get('pnl_message_id')]
        else: ids = json.loads(values.get('discord_pnl_message_ids') or '[]')
        ids = [str(x) for x in ids if x]
        if len(ids) != 1: raise ValueError('Legacy multipart board needs explicit migration')
        item = positions[sorted(positions)[0]]
        row = self._resolve(item)
        if row['message_id'] and row['message_id'] != ids[0]: raise ValueError('Conflicting legacy board migration')
        with self.connect() as db, db:
            db.execute('UPDATE trade_message_delivery SET message_id=?,sending=0 WHERE channel=? AND trade_key=?', (ids[0], self.channel, row['trade_key']))
            for k in legacy_keys: db.execute('DELETE FROM metadata WHERE key=?', (k,))

    def sync(self, positions):
        if not isinstance(positions, dict): raise ValueError('Invalid live positions snapshot')
        for item in positions.values(): identity(item)
        self._adopt_legacy(positions)
        seen = set()
        for item in positions.values():
            row = self._resolve(item)
            seen.add(row['trade_key'])
            if row['phase'] == 'closed': continue
            self._write(row, snapshot=json.dumps(item), phase='live')
            self._deliver(row, self.format_live(item))
        # Keep the last confirmed content while awaiting complete settlement.
        for row in self._rows():
            if row['trade_key'] not in seen and row['phase'] == 'live': self._write(row, phase='waiting')

    def _settlement_board(self):
        if self._settlement is None:
            self._settlement = TradeMessageBoard(
                self.connect, self.channel + ':closed', self.send, self.edit,
                self.is_missing, self.definitely_unsent, self.format_live, self.format_closed,
            )
        return self._settlement

    def close(self, item, *, separate_legacy=False):
        if not history_complete(item.get('_history') or {}): return False
        row = self._resolve(item, closing=True)
        # Already-delivered historical trades are not replayed by a policy change.
        # A targeted, user-requested replay uses the same durable settlement record.
        if row['phase'] == 'closed' and not separate_legacy: return True
        merged = json.loads(row['snapshot'])
        merged.update(item)
        self._write(row, snapshot=json.dumps(merged), phase=row['phase'] if row['phase'] == 'closed' else 'waiting')
        settlement = self._settlement_board()
        final_row = settlement._resolve(merged, closing=True)
        if final_row['phase'] != 'closed':
            settlement._write(final_row, snapshot=json.dumps(merged), phase='waiting')
            settlement._deliver(final_row, self.format_closed(merged))
            settlement._write(final_row, phase='closed')
        self._write(row, phase='closed')
        return True

    def message_ids(self, item):
        final_row = self._settlement_board()._resolve(item, closing=True, create=False)
        if final_row is not None and final_row['message_id']:
            return [final_row['message_id']]
        row = self._resolve(item, closing=True)
        return [row['message_id']] if row['message_id'] else []
