import json
import sqlite3
import sys
import unittest
from contextlib import nullcontext
from pathlib import Path
QA = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(QA / 'app'))
from aggregate_board import AggregateTradeMessageBoard
from trade_messages import UncertainDelivery
class Missing(Exception): pass
class Definite(Exception): pass

def position(symbol='BTCUSDT', stamp=100000):
    return dict(productType='USDT-FUTURES', symbol=symbol, holdSide='long',
                marginCoin='USDT', cTime=str(stamp), posId=symbol+str(stamp), total='1',
                openPriceAvg='100', markPrice='101', unrealizedPL='1', marginSize='10',
                leverage='10', achievedProfits='0', totalFee='0', deductedFee='0.02')
def closed(item):
    result = dict(item)
    result['_history'] = dict(positionId=item['posId'], symbol=item['symbol'], holdSide='long',
        marginCoin='USDT', openAvgPrice='100', closeAvgPrice='101', openTotalPos='1',
        closeTotalPos='1', netProfit='0.92', openFee='-0.02', closeFee='-0.06',
        totalFunding='0', ctime=item['cTime'], utime=str(int(item['cTime'])+10000))
    return result

class AggregateTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(':memory:')
        self.db.execute('CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT)')
        self.log = []; self.serial = 100
        self.send_error = self.edit_error = self.delete_error = None
        self.a, self.b = position(), position('ETHUSDT')
        self.positions = {'a': self.a, 'b': self.b}
        self.board = self.make()
    def tearDown(self): self.db.close()
    def send(self, payload):
        self.log.append(('send', payload))
        if self.send_error: raise self.send_error
        self.serial += 1
        return str(self.serial)
    def edit(self, mid, payload):
        self.log.append(('edit',mid,payload))
        if self.edit_error: raise self.edit_error
    def delete(self, mid):
        self.log.append(('delete',mid))
        if self.delete_error: raise self.delete_error
    def make(self, channel='telegram', formatter=None):
        return AggregateTradeMessageBoard(lambda:nullcontext(self.db), channel,
            self.send, self.edit, lambda e:isinstance(e,Missing), lambda e:isinstance(e,Definite),
            formatter or (lambda p:'live '+','.join(x['symbol'] for x in p.values())),
            lambda p:'closed '+p['symbol'], self.delete, lambda p:'opened '+p['symbol'])
    def actions(self, name): return [x for x in self.log if x[0]==name]
    def legacy(self, item, mid, phase='live'):
        row=self.board._resolve(item)
        self.board._write(row,message_id=mid,phase=phase)
        return row
    def test_two_positions_one_message(self):
        self.board.sync(self.positions)
        self.assertEqual(self.actions('send'), [('send','live BTCUSDT,ETHUSDT')])
        self.board.sync(self.positions)
        self.assertEqual(len(self.actions('send')),1)
        self.assertEqual(self.actions('edit')[-1],('edit','101','live BTCUSDT,ETHUSDT'))
    def test_restart_updates_same_message(self):
        self.board.sync(self.positions);self.make().sync(self.positions)
        self.assertEqual(len(self.actions('send')),1)
        self.assertEqual(self.actions('edit')[-1][1],'101')
    def test_migration_edits_before_deleting_and_detaches(self):
        self.legacy(self.a,'11');self.legacy(self.b,'12');self.board.sync(self.positions)
        self.assertEqual(self.log,[('edit','11','live BTCUSDT,ETHUSDT'),('delete','12')])
        self.assertTrue(all(r['message_id'] is None for r in self.board._rows()))
        self.make().sync(self.positions)
        self.assertEqual(len(self.actions('delete')),1)
    def test_migration_never_touches_closed_receipts(self):
        self.legacy(position('OLDUSDT'),'9',phase='closed')
        self.legacy(self.a,'11');self.legacy(self.b,'12')
        final=self.board._settlement_board()._resolve(position('PASTUSDT'))
        self.board._settlement_board()._write(final,message_id='8',phase='closed')
        self.board.sync(self.positions)
        self.assertFalse(any('9' in x[1:] or '8' in x[1:] for x in self.log))
    def test_failed_edit_never_deletes(self):
        self.legacy(self.a,'11');self.legacy(self.b,'12');self.edit_error=RuntimeError('offline')
        with self.assertRaises(RuntimeError): self.board.sync(self.positions)
        self.assertEqual(self.actions('delete'),[]);self.edit_error=None;self.make().sync(self.positions)
        self.assertEqual(self.actions('delete'),[('delete','12')])
    def test_failed_delete_retried_after_restart_without_new_send(self):
        self.legacy(self.a,'11');self.legacy(self.b,'12');self.delete_error=RuntimeError('offline')
        with self.assertRaises(RuntimeError):self.board.sync(self.positions)
        self.delete_error=None;self.make().sync(self.positions)
        self.assertEqual(len(self.actions('send')),0);self.assertEqual(len(self.actions('delete')),2)
    def test_delete_confirmed_missing_is_complete(self):
        self.legacy(self.a,'11');self.legacy(self.b,'12');self.delete_error=Missing()
        self.board.sync(self.positions);self.make().sync(self.positions)
        self.assertEqual(len(self.actions('delete')),1)
    def test_close_one_preserves_other_and_final_sent_once(self):
        self.board.sync(self.positions);self.assertTrue(self.board.close(closed(self.a)))
        self.board.sync({'b':self.b});self.make().close(closed(self.a))
        self.assertEqual(self.actions('send'),[('send','live BTCUSDT,ETHUSDT'),('send','closed BTCUSDT')])
        self.assertEqual(self.actions('edit')[-1],('edit','101','live ETHUSDT'))
        self.assertEqual(self.board.message_ids(closed(self.a)),['102'])
    def test_delayed_history_retains_single_live_board(self):
        self.board.sync(self.positions);self.assertFalse(self.board.close(self.a))
        self.board.sync({'b':self.b});self.assertEqual(len(self.actions('send')),1)
        self.assertTrue(self.board.close(closed(self.a)));self.assertEqual(len(self.actions('send')),2)
    def test_empty_does_not_create_but_existing_is_cleared(self):
        self.board.sync({});self.assertEqual(self.log,[])
        self.board.sync(self.positions);self.board.sync({})
        # An emptied account deletes the board instead of showing an empty one.
        self.assertEqual(self.actions('delete')[-1],('delete','101'));self.assertEqual(len(self.actions('send')),1)
    def test_stale_closed_position_does_not_reappear(self):
        self.board.sync(self.positions);self.board.close(closed(self.a));self.board.sync(self.positions)
        self.assertEqual(self.actions('edit')[-1][2],'live ETHUSDT')
    def test_unknown_send_latched_across_restart(self):
        self.send_error=RuntimeError('unknown delivery')
        with self.assertRaises(RuntimeError):self.board.sync(self.positions)
        self.send_error=None
        with self.assertRaises(UncertainDelivery):self.make().sync(self.positions)
        self.assertEqual(len(self.actions('send')),1)
    def test_known_unsent_retries(self):
        self.send_error=Definite()
        with self.assertRaises(Definite):self.board.sync(self.positions)
        self.send_error=None;self.make().sync(self.positions)
        self.assertEqual(len(self.actions('send')),2)
    def test_migration_uncertain_original_blocks_create(self):
        row=self.board._resolve(self.a);self.board._write(row,sending=1)
        with self.assertRaises(UncertainDelivery):self.board.sync(self.positions)
        self.assertEqual(self.log,[])
    def test_invalid_snapshot_does_not_mutate(self):
        self.board.sync(self.positions);before=list(self.log)
        with self.assertRaises(ValueError):self.board.sync({'bad':dict(self.a,cTime='0')})
        self.assertEqual(self.log,before)
    def test_size_guard_before_state_change_or_network(self):
        def overlimit(p): raise ValueError('size limit')
        board=self.make(formatter=overlimit)
        with self.assertRaises(ValueError):board.sync(self.positions)
        self.assertEqual(board._rows(),[]);self.assertIsNone(board._aggregate_row());self.assertEqual(self.log,[])
    def test_recovered_open_sent_once_across_restart(self):
        self.assertEqual(self.board.open_notice(self.a),['101'])
        self.assertEqual(self.make().open_notice(self.a),['101'])
        self.assertEqual(self.actions('send'),[('send','opened BTCUSDT')])
    def test_open_unknown_delivery_is_latched(self):
        self.send_error=RuntimeError('unknown')
        with self.assertRaises(RuntimeError):self.board.open_notice(self.a)
        self.send_error=None
        with self.assertRaises(UncertainDelivery):self.make().open_notice(self.a)
        self.assertEqual(len(self.actions('send')),1)
    def test_discord_uses_own_aggregate_channel(self):
        self.board.sync(self.positions)
        self.make('discord').sync(self.positions);self.make('discord').sync(self.positions)
        self.assertEqual(len(self.actions('send')),2);self.assertEqual(self.actions('edit')[-1][1],'102')

    def test_visibility_only_after_confirmed_delivery_and_survives_restart(self):
        self.assertFalse(self.board.was_visible(self.a))
        self.board.sync(self.positions)
        self.assertTrue(self.make().was_visible(self.a))
        self.board.sync({'b':self.b})
        self.board.close(closed(self.a))
        self.assertTrue(self.make().was_visible(closed(self.a)))
    def test_failed_delivery_does_not_mark_visible(self):
        self.send_error=RuntimeError('offline')
        with self.assertRaises(RuntimeError):self.board.sync(self.positions)
        self.assertFalse(self.board.was_visible(self.a))
        self.assertFalse(self.make().was_visible(self.a))
    def test_already_visible_stays_visible_when_later_edit_fails(self):
        self.board.sync(self.positions);self.edit_error=RuntimeError('offline')
        with self.assertRaises(RuntimeError):self.board.sync(self.positions)
        self.assertTrue(self.make().was_visible(self.a))
    def test_close_before_first_live_sync_does_not_hide_other_position(self):
        self.board.close(closed(self.a));self.board.sync(self.positions)
        self.assertEqual(self.actions('send'),[('send','closed BTCUSDT'),('send','live ETHUSDT')])

    def test_manually_deleted_empty_board_not_recreated(self):
        self.board.sync(self.positions);self.edit_error=Missing()
        self.board.sync({});self.assertEqual(len(self.actions('send')),1)
        self.make().sync({});self.assertEqual(len(self.actions('send')),1)
        self.edit_error=None;self.make().sync(self.positions)
        self.assertEqual(len(self.actions('send')),2)
    def test_send_ack_db_failure_same_process_does_not_resend(self):
        original=self.board._aggregate._write
        fail=[True]
        def write(row, **updates):
            if updates.get('message_id') and fail[0]:
                fail[0]=False
                raise sqlite3.OperationalError('busy')
            return original(row, **updates)
        self.board._aggregate._write=write
        with self.assertRaises(sqlite3.OperationalError):self.board.sync(self.positions)
        self.board.sync(self.positions)
        self.assertEqual(len(self.actions('send')),1)
        self.assertTrue(self.board.was_visible(self.a))

if __name__=='__main__': unittest.main()
