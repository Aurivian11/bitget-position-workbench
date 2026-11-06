"""Read-only CFD positions and ledger; separate from UTA crypto futures."""
import copy
import json
import sqlite3
import threading
import time
from decimal import Decimal, InvalidOperation


def number(value):
    try:
        d = Decimal(str(value))
        return d if d.is_finite() else None
    except (InvalidOperation, ValueError, TypeError):
        return None


def rows(data):
    if isinstance(data, dict) and 'list' in data:
        data = data['list']
    if data is None:
        return []
    if not isinstance(data, list) or any(not isinstance(r, dict) for r in data):
        raise ValueError('Malformed CFD response')
    return data


def trade_record(raw):
    """Ignore deposits/withdrawals; never infer trade direction from numeric enums."""
    if not raw.get('symbol') or str(raw.get('orderId') or '0') == '0':
        return None
    if raw.get('side') not in {'buy', 'sell'}:
        return None
    required = ('qty', 'openPrice', 'closePrice', 'fee', 'swap', 'cashFlow', 'balanceChange', 'ts')
    parsed = {k: number(raw.get(k)) for k in required}
    if any(v is None for v in parsed.values()):
        raise ValueError('Incomplete CFD financial record')
    if parsed['qty'] <= 0 or parsed['openPrice'] <= 0 or parsed['ts'] <= 0:
        return None
    if parsed['balanceChange'] != parsed['cashFlow'] + parsed['swap'] + parsed['fee']:
        raise ValueError('CFD financial record balance mismatch')
    return dict(raw, **{k: str(v) for k, v in parsed.items()})


def settled_trades(records):
    """Pair only a unique compatible opening; ambiguous hedged fills stay unresolved."""
    opened, complete = [], []
    unique = {str(r['id']): r for r in records if r.get('id')}
    for raw in sorted(unique.values(), key=lambda r: (int(r.get('ts') or 0), str(r['id']))):
        r = trade_record(raw)
        if r is None:
            continue
        qty, price = number(r['qty']), number(r['openPrice'])
        if number(r['closePrice']) == 0:
            if number(r['cashFlow']) != 0:
                continue
            opened.append({'row': r, 'remaining': qty, 'closes': [], 'blocked': False})
            continue
        candidates = [o for o in opened if o['remaining'] > 0 and
                      o['row']['symbol'] == r['symbol'] and o['row']['side'] != r['side'] and
                      number(o['row']['openPrice']) == price and int(o['row']['ts']) <= int(r['ts'])]
        if len(candidates) != 1 or candidates[0]['remaining'] < qty:
            for candidate in candidates:
                candidate['blocked'] = True
            continue
        o = candidates[0]
        o['remaining'] -= qty
        o['closes'].append(r)
        if o['remaining'] != 0 or o['blocked']:
            continue
        opening, closes = o['row'], o['closes']
        total = number(opening['qty'])
        weighted = sum(number(c['qty']) * number(c['closePrice']) for c in closes) / total
        close_fee = sum(number(c['fee']) for c in closes)
        funding = number(opening['swap']) + sum(number(c['swap']) for c in closes)
        net = number(opening['balanceChange']) + sum(number(c['balanceChange']) for c in closes)
        complete.append({'productType': 'CFD', 'symbol': opening['symbol'],
            'holdSide': 'long' if opening['side'] == 'buy' else 'short', 'marginCoin': 'USD',
            'positionId': str(opening['orderId']), 'ctime': opening['ts'], 'utime': closes[-1]['ts'],
            'openAvgPrice': opening['openPrice'], 'closeAvgPrice': str(weighted),
            'openTotalPos': str(total), 'closeTotalPos': str(total),
            'openFee': opening['fee'], 'closeFee': str(close_fee), 'totalFunding': str(funding),
            'netProfit': str(net), 'pnl': str(sum(number(c['cashFlow']) for c in closes)),
            '_accountMode': 'cfd', '_feeRebateRate': '0', '_cfdOpeningOrderId': str(opening['orderId']),
            '_cfdRecordIds': [str(opening['id'])] + [str(c['id']) for c in closes]})
    return complete


def opening_records(records):
    groups = {}
    for r in records:
        if trade_record(r) is not None and number(r.get('closePrice')) == 0:
            groups.setdefault(str(r['orderId']), []).append(r)
    return {key: values[0] for key, values in groups.items() if len(values) == 1}


def normalize_position(raw, opening, first_seen, instrument=None, ticker=None, currency='USD'):
    instrument, ticker = instrument or {}, ticker or {}
    pid, symbol, side = str(raw.get('positionId') or ''), str(raw.get('symbol') or ''), raw.get('side')
    qty, price = number(raw.get('qty')), number(raw.get('openPrice'))
    if not pid or not symbol or side not in {'buy', 'sell'} or qty is None or qty <= 0 or price is None or price <= 0:
        raise ValueError('Invalid CFD position identity')
    unrealized, funding = number(raw.get('unrealizedPnl')), number(raw.get('interest'))
    total_profit = number(raw.get('totalProfit'))
    if unrealized is None or funding is None or total_profit != unrealized + funding:
        raise ValueError('Incomplete CFD position profit')
    known = opening is not None and str(opening.get('orderId')) == pid and opening.get('symbol') == symbol and opening.get('side') == side and number(opening.get('openPrice')) == price
    opening_time = str(opening['ts']) if known else None
    fees = abs(number(opening['fee'])) if known and number(opening['qty']) == qty else None
    mark = number(ticker.get('bid1' if side == 'buy' else 'ask1'))
    leverage = instrument.get('leverage')
    return {'productType': 'CFD', 'symbol': symbol, 'holdSide': 'long' if side == 'buy' else 'short',
        'marginCoin': currency, 'posId': pid, 'total': str(qty), 'openPriceAvg': str(price),
        'cTime': str(first_seen), 'uTime': str(int(time.time()*1000)),
        'markPrice': str(mark) if mark is not None and mark > 0 else None,
        'takeProfit': raw.get('takeProfit'), 'stopLoss': raw.get('stopLoss'),
        'unrealizedPL': str(unrealized), 'totalFee': str(funding), 'achievedProfits': '0',
        'deductedFee': str(fees) if fees is not None else None, 'marginSize': None,
        'leverage': leverage, '_accountMode': 'cfd', '_feeRebateRate': '0',
        '_cfdOpenedAt': opening_time, '_cfdOpeningOrderId': str(opening['orderId']) if known else None,
        '_cfdFinancialsComplete': fees is not None, '_cfdQuantityUnit': '手',
        '_cfdNetPnl': str(unrealized + funding - fees) if fees is not None else None, '_utaPnlAmbiguous': fees is None, '_cfdInstrument': {k: instrument.get(k) for k in
          ('contractSize', 'tickSize', 'commissionType', 'commissionRate', 'exchangeRate')}}


class CFDAdapter:
    """One shared reader per process avoids tripling the account endpoint rate."""
    def __init__(self, get, db_path, *, clock=time.time, monotonic=time.monotonic):
        self.get, self.clock, self.monotonic = get, clock, monotonic
        self.lock = threading.RLock()
        self.db = sqlite3.connect(str(db_path), timeout=30, check_same_thread=False)
        self.db.execute('CREATE TABLE IF NOT EXISTS cfd_financial_records (id TEXT PRIMARY KEY, ts INTEGER NOT NULL, payload TEXT NOT NULL)')
        self.db.execute('CREATE TABLE IF NOT EXISTS cfd_position_identity (position_id TEXT PRIMARY KEY, first_seen INTEGER NOT NULL, snapshot TEXT)')
        self.db.execute('CREATE TABLE IF NOT EXISTS cfd_metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)')
        self.db.execute("INSERT OR IGNORE INTO cfd_metadata VALUES ('activation',?)", (str(int(self.clock()*1000)),))
        self.db.commit()
        self.cache = {}

    def _cached(self, key, ttl, fetch):
        now = self.monotonic()
        if key not in self.cache or now - self.cache[key][0] >= ttl:
            value = fetch()
            self.cache[key] = (self.monotonic(), value)
        return copy.deepcopy(self.cache[key][1])

    def _financial(self):
        def fetch():
            now = int(self.clock()*1000)
            saved = self.db.execute("SELECT value FROM cfd_metadata WHERE key='watermark'").fetchone()
            # Web and monitor use separate processes; reuse the durable fresh ledger.
            if saved and 0 <= now - int(saved[0]) < 4500:
                return True
            start = max(now - 29*86400000, int(saved[0])-60000) if saved else now-29*86400000
            params = {'limit': '50', 'startTime': str(start), 'endTime': str(now)}
            collected, seen_cursors = [], set()
            for page in range(40):
                if page:
                    time.sleep(1.05)
                data = self.get(params, '/api/v3/cfd/account/financial-records')
                batch = rows(data)
                collected.extend(batch)
                if len(batch) < 50:
                    break
                cursor = str(data.get('cursor') or '') if isinstance(data, dict) else str(batch[-1].get('id') or '')
                if not cursor or cursor in seen_cursors:
                    raise ValueError('CFD financial pagination stalled')
                seen_cursors.add(cursor); params['cursor'] = cursor
            else:
                raise ValueError('CFD financial pagination limit reached')
            for r in collected:
                if not r.get('id') or number(r.get('ts')) is None:
                    raise ValueError('CFD financial identity missing')
                trade_record(r)
            with self.db:
                self.db.executemany('INSERT OR REPLACE INTO cfd_financial_records VALUES (?,?,?)',
                    [(str(r['id']), int(r['ts']), json.dumps(r)) for r in collected])
                self.db.execute("INSERT OR REPLACE INTO cfd_metadata VALUES ('watermark',?)", (str(now),))
            return True
        self._cached('financial', 5, fetch)
        return [json.loads(r[0]) for r in self.db.execute('SELECT payload FROM cfd_financial_records ORDER BY ts')]

    def _identity(self, pid, preferred):
        with self.db:
            self.db.execute('INSERT OR IGNORE INTO cfd_position_identity VALUES (?,?,NULL)', (pid, int(preferred)))
        return self.db.execute('SELECT first_seen FROM cfd_position_identity WHERE position_id=?', (pid,)).fetchone()[0]

    def _account(self):
        def fetch():
            data = self.get({}, '/api/v3/cfd/account/fund-detail')
            if not isinstance(data, dict) or not data.get('currency'):
                raise ValueError('Malformed CFD account')
            return data
        return self._cached('account', 60, fetch)

    def _instruments(self):
        return self._cached('instruments', 300, lambda: {r['symbol']: r for r in rows(self.get({}, '/api/v3/cfd/account/instruments'))})

    def positions(self):
        with self.lock:
            def fetch():
                raw_positions = rows(self.get({}, '/api/v3/cfd/trade/current-positions'))
                if not raw_positions:
                    return {}
                records = self._financial()
                openings = opening_records(records)
                account = self._account()
                try:
                    instruments = self._instruments()
                except Exception:
                    instruments = {}
                result = {}
                for raw in raw_positions:
                    pid, symbol = str(raw.get('positionId') or ''), str(raw.get('symbol') or '')
                    opening = openings.get(pid)
                    preferred = opening['ts'] if opening else int(self.clock()*1000)
                    identity_time = self._identity(pid, preferred)
                    try:
                        tickers = rows(self.get({'symbol': symbol}, '/api/v3/cfd/market/tickers'))
                    except Exception:
                        tickers = []
                    matching = [r for r in tickers if r.get('symbol') == symbol]
                    ticker = matching[0] if len(matching) == 1 else {}
                    item = normalize_position(raw, opening, identity_time, instruments.get(symbol), ticker, account['currency'])
                    key = ':'.join(('CFD', item['symbol'], item['holdSide'], item['marginCoin'], pid))
                    if key in result:
                        raise ValueError('Duplicate CFD position identity')
                    result[key] = item
                with self.db:
                    self.db.executemany('UPDATE cfd_position_identity SET snapshot=? WHERE position_id=?',
                        [(json.dumps(v),v['posId']) for v in result.values()])
                return result
            return self._cached('positions', 1, fetch)

    def history_positions(self):
        with self.lock:
            histories = settled_trades(self._financial())
            baseline = int(self.db.execute("SELECT value FROM cfd_metadata WHERE key='activation'").fetchone()[0])
            result = []
            currency = self._account()['currency'] if any(int(h['utime']) >= baseline for h in histories) else 'USD'
            for h in histories:
                if int(h['utime']) < baseline:
                    continue
                identity = self.db.execute('SELECT first_seen,snapshot FROM cfd_position_identity WHERE position_id=?', (h['positionId'],)).fetchone()
                h['marginCoin'] = currency
                h['_cfdOpenedAt'] = h['ctime']
                if identity:
                    h['ctime'] = str(identity[0])
                    if identity[1]:
                        snapshot = json.loads(identity[1])
                        h['marginCoin'] = snapshot['marginCoin']
                result.append(h)
            return result

    def opening_events(self):
        with self.lock:
            records = self._financial()
            baseline = int(self.db.execute("SELECT value FROM cfd_metadata WHERE key='activation'").fetchone()[0])
            result = []
            for r in opening_records(records).values():
                valid = trade_record(r)
                if valid is None or number(r.get('closePrice')) != 0 or int(r['ts']) < baseline:
                    continue
                raw = {'positionId': str(r['orderId']), 'symbol': r['symbol'], 'side': r['side'],
                    'qty': r['qty'], 'openPrice': r['openPrice'], 'interest': '0', 'unrealizedPnl': '0', 'totalProfit': '0'}
                identity = self._identity(raw['positionId'], r['ts'])
                item = normalize_position(raw, valid, identity, currency=self._account()['currency'])
                item['_cfdOpeningEvent'] = True
                result.append(item)
            return result

    def accounts(self):
        with self.lock:
            d = self._account()
            return [{'productType': 'CFD', 'marginCoin': d['currency'], 'accountEquity': d.get('equity'),
                'available': d.get('marginFree'), 'locked': d.get('marginUsed'), 'unrealizedPL': d.get('pnl')}]


_SHARED = {}
_SHARED_LOCK = threading.Lock()


def get_cfd_adapter(client):
    import hashlib
    key = (str(client.config.db_path), client.config.api_base, hashlib.sha256(client.config.api_key.encode()).hexdigest())
    with _SHARED_LOCK:
        if key not in _SHARED:
            _SHARED[key] = CFDAdapter(client._get, client.config.db_path)
        return _SHARED[key]
