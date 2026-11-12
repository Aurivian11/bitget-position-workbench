"""One durable live dashboard, with independent per-trade settlement receipts."""
import json
import time
from trade_messages import TradeMessageBoard, UncertainDelivery, identity, same_lifetime

RETIRED_TEXT = "<b>📊 BITGET · 旧看板</b>\n此看板已停止更新。"


class AggregateTradeMessageBoard(TradeMessageBoard):
    def __init__(self, connect, channel, send, edit, is_missing, definitely_unsent,
                 format_positions, format_closed, delete, format_opened=None, latest_message_id=None):
        self.format_positions = format_positions
        self.latest_message_id = latest_message_id
        self.delete = delete
        self.format_opened = format_opened
        self._opening = None
        super().__init__(connect, channel, send, edit, is_missing, definitely_unsent,
                         lambda item: format_positions({str(identity(item)): item}), format_closed)
        self._aggregate = TradeMessageBoard(
            connect, channel + ':aggregate', send, edit, is_missing, definitely_unsent,
            self.format_live, format_closed,
        )

    def _aggregate_row(self):
        rows = self._aggregate._rows()
        if len(rows) > 1 or (rows and rows[0]['trade_key'] != 'all'):
            raise ValueError('Invalid aggregate message state')
        return rows[0] if rows else None

    def _legacy_sources(self):
        rows = self._rows()
        if any(r['phase'] != 'closed' and r['sending'] and not r['message_id'] for r in rows):
            raise UncertainDelivery('Previous live message creation needs confirmation')
        protected = {str(r['message_id']) for r in rows if r['phase'] == 'closed' and r['message_id']}
        with self.connect() as db:
            protected.update(str(r[0]) for r in db.execute(
                'SELECT message_id FROM trade_message_delivery WHERE channel=? AND message_id IS NOT NULL',
                (self.channel + ':closed',),
            ))
            keys = ('pnl_message_parts', 'pnl_message_id') if self.channel == 'telegram' else ('discord_pnl_message_ids',)
            metadata = {k: v for k, v in db.execute('SELECT key,value FROM metadata') if k in keys}
        candidates = [str(r['message_id']) for r in sorted(rows, key=lambda r: r['trade_key'])
                      if r['phase'] != 'closed' and r['message_id']]
        if self.channel == 'telegram':
            legacy = json.loads(metadata['pnl_message_parts'])['ids'] if metadata.get('pnl_message_parts') else [metadata.get('pnl_message_id')]
        else:
            legacy = json.loads(metadata.get('discord_pnl_message_ids') or '[]')
        ids = list(dict.fromkeys(candidates + [str(v) for v in legacy if v]))
        return [mid for mid in ids if mid not in protected], list(metadata)

    def _start_aggregate(self, positions):
        ids, legacy_keys = self._legacy_sources()
        if not positions and not ids:
            return None
        snapshot = {'positions': {}, 'pending_delete': ids[1:],
                    'migrate_ids': ids, 'legacy_keys': legacy_keys}
        with self.connect() as db, db:
            db.execute('INSERT INTO trade_message_delivery(channel,trade_key,snapshot,message_id) VALUES(?,?,?,?)',
                       (self.channel + ':aggregate', 'all', json.dumps(snapshot), ids[0] if ids else None))
        return self._aggregate_row()

    def _cleanup(self, row):
        snapshot = json.loads(row['snapshot'])
        # Only detach legacy live references after the aggregate update is confirmed.
        ids = snapshot.get('migrate_ids') or []
        keys = snapshot.get('legacy_keys') or []
        if ids or keys:
            with self.connect() as db, db:
                for mid in ids:
                    db.execute('UPDATE trade_message_delivery SET message_id=NULL,sending=0 WHERE channel=? AND message_id=? AND phase!=?',
                               (self.channel, mid, 'closed'))
                for key in keys:
                    db.execute('DELETE FROM metadata WHERE key=?', (key,))
            snapshot['migrate_ids'], snapshot['legacy_keys'] = [], []
            self._aggregate._write(row, snapshot=json.dumps(snapshot))
        while snapshot.get('pending_delete'):
            mid = snapshot['pending_delete'][0]
            if str(mid) == str(row['message_id']):
                raise ValueError('Refusing to delete the active aggregate message')
            try:
                self.delete(mid)
            except Exception as error:
                if not self.is_missing(error):
                    raise
            snapshot['pending_delete'].pop(0)
            self._aggregate._write(row, snapshot=json.dumps(snapshot))

    def sync(self, positions):
        if not isinstance(positions, dict):
            raise ValueError('Invalid live positions snapshot')
        identities = {}
        active = {}
        fresh = False
        for key, item in positions.items():
            ident = identity(item)
            if ident in identities:
                if not same_lifetime(identities[ident], item):
                    raise ValueError('Conflicting position identifiers for one lifetime')
                raise ValueError('Duplicate live position identity')
            identities[ident] = item
            existing = self._resolve(item, create=False)
            if existing is None:
                fresh = True
            if existing is None or existing['phase'] != 'closed':
                active[key] = item
        # Preflight formatting/limits before any state or network side effects.
        payload = self.format_positions(active)
        row = self._aggregate_row()
        if row is None:
            row = self._start_aggregate(active)
        seen = set()
        for item in active.values():
            trade = self._resolve(item)
            seen.add(trade['trade_key'])
            updated = dict(item)
            if json.loads(trade['snapshot']).get('_aggregateSeen') is True:
                updated['_aggregateSeen'] = True
            else:
                updated.pop('_aggregateSeen', None)
            self._write(trade, snapshot=json.dumps(updated), phase='live')
        for trade in self._rows():
            if trade['phase'] == 'live' and trade['trade_key'] not in seen:
                self._write(trade, phase='waiting')
        if row is None:
            return
        # A newly opened trade gets a fresh board at the bottom of the chat (which
        # notifies) instead of silently editing an older board buried above it.
        if active and row['message_id'] and (fresh or self._buried(row['message_id'])) and any(
                json.loads(self._resolve(item, create=False)['snapshot']).get('_aggregateSeen') is not True
                for item in active.values()):
            snapshot = json.loads(row['snapshot'])
            retire = snapshot.get('retire_ids') or []
            if str(row['message_id']) not in [str(x) for x in retire]:
                retire.append(str(row['message_id']))
            snapshot['retire_ids'] = retire
            self._aggregate._write(row, snapshot=json.dumps(snapshot), message_id=None, sending=0)
        snapshot = json.loads(row['snapshot'])
        snapshot['positions'] = active
        self._aggregate._write(row, snapshot=json.dumps(snapshot))
        if active:
            self._aggregate._deliver(row, payload)
        else:
            # Empty accounts may clear a known board, never create a new blank one.
            confirmed = self._aggregate._confirmed.get(row['trade_key'])
            if confirmed:
                self._aggregate._write(row, message_id=confirmed, sending=0)
                del self._aggregate._confirmed[row['trade_key']]
            if not row['message_id']:
                self._retire(row)
                return
            # No positions: remove the board entirely instead of showing an empty one.
            # The next opening then creates a fresh board at the bottom (with a push).
            snapshot = json.loads(row['snapshot'])
            retire = snapshot.get('retire_ids') or []
            if str(row['message_id']) not in [str(x) for x in retire]:
                retire.append(str(row['message_id']))
            snapshot['retire_ids'] = retire
            self._aggregate._write(row, snapshot=json.dumps(snapshot), message_id=None, sending=0)
            self._retire(row)
            return
        # A persisted visibility marker is only set after the transport confirms.
        for item in active.values():
            trade = self._resolve(item, create=False)
            updated = json.loads(trade['snapshot'])
            updated['_aggregateSeen'] = True
            self._write(trade, snapshot=json.dumps(updated))
        self._cleanup(row)
        self._retire(row)

    def _buried(self, message_id):
        """True when a newer message (receipt, event) sits below the board."""
        try:
            board = int(message_id)
        except (TypeError, ValueError):
            return False
        newer = []
        if self.latest_message_id is not None:
            newer.append(self.latest_message_id())
        with self.connect() as db:
            newer += [r[0] for r in db.execute(
                'SELECT message_id FROM trade_message_delivery WHERE channel IN (?,?) AND message_id IS NOT NULL',
                (self.channel + ':closed', self.channel + ':opened'))]
        for value in newer:
            try:
                if value is not None and int(value) > board:
                    return True
            except (TypeError, ValueError):
                continue
        return False

    def _retire(self, row):
        """Remove boards superseded by a fresh one. Transient failures retry on later
        cycles; after two minutes the old board is marked as moved instead."""
        snapshot = json.loads(row['snapshot'])
        since = snapshot.get('retire_since') or {}
        while snapshot.get('retire_ids'):
            mid = str(snapshot['retire_ids'][0])
            if mid != str(row['message_id']):
                try:
                    self.delete(mid)
                except Exception as error:
                    if not self.is_missing(error):
                        first = since.setdefault(mid, time.time())
                        if time.time() - first < 120:
                            snapshot['retire_since'] = since
                            self._aggregate._write(row, snapshot=json.dumps(snapshot))
                            return
                        try:
                            self.edit(mid, RETIRED_TEXT)
                        except Exception:
                            pass
            snapshot['retire_ids'].pop(0)
            since.pop(mid, None)
            snapshot['retire_since'] = since
            self._aggregate._write(row, snapshot=json.dumps(snapshot))

    def was_visible(self, item):
        row = self._resolve(item, closing=True, create=False)
        return bool(row and json.loads(row['snapshot']).get('_aggregateSeen') is True)

    def open_notice(self, item):
        """Notify a recovered opening that elapsed between live snapshots, once."""
        if self.format_opened is None:
            raise ValueError('Opening notice formatter is unavailable')
        payload = self.format_opened(item)
        if self._opening is None:
            self._opening = TradeMessageBoard(
                self.connect, self.channel + ':opened', self.send, self.edit,
                self.is_missing, self.definitely_unsent, self.format_live, self.format_closed,
            )
        row = self._opening._resolve(item)
        if row['phase'] != 'closed':
            self._opening._write(row, snapshot=json.dumps(item))
            self._opening._deliver(row, payload)
            self._opening._write(row, phase='closed')
        return [row['message_id']]

    def message_ids(self, item):
        final = self._settlement_board()._resolve(item, closing=True, create=False)
        if final is not None and final['message_id']:
            return [final['message_id']]
        row = self._aggregate_row()
        return [row['message_id']] if row and row['message_id'] else []
