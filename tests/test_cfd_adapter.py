import copy
import json
import sqlite3
import sys
import unittest
from pathlib import Path
from unittest.mock import patch
QA=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(QA/'app'))
import cfd_adapter as c


def record(id='1',order='10',side='buy',qty='1',entry='100',close='0',fee='-2',cash='0',ts='1000'):
    return dict(id=id,orderId=order,side=side,qty=qty,openPrice=entry,closePrice=close,fee=fee,cashFlow=cash,swap='0',balanceChange=str(c.number(fee)+c.number(cash)),ts=ts,symbol='XAUUSD')


def raw(pid='10',qty='1'):
    return dict(positionId=pid,symbol='XAUUSD',side='buy',qty=qty,openPrice='100',interest='-1',unrealizedPnl='10',totalProfit='9',takeProfit='0',stopLoss='95')


class Tests(unittest.TestCase):
    def test_documented_and_actual_envelopes(self):
        self.assertEqual(c.rows([]),[])
        self.assertEqual(c.rows({'list':None}),[])
        with self.assertRaises(ValueError):c.rows({})
        with self.assertRaises(ValueError):c.rows([None])
    def test_transfers_not_trades(self):
        r=record();r.update(symbol='',orderId='0');self.assertIsNone(c.trade_record(r))
    def test_invalid_balance_rejected(self):
        r=record();r['balanceChange']='5'
        with self.assertRaises(ValueError):c.trade_record(r)
    def test_unknown_numeric_side_not_inferred(self):
        r=record();r['side']=1;self.assertIsNone(c.trade_record(r))
    def test_roundtrip_fees_are_net_once(self):
        a=record();b=record('2','20','sell',close='103',fee='0',cash='3',ts='2000')
        h=c.settled_trades([a,b])[0]
        self.assertEqual(h['netProfit'],'1');self.assertEqual(h['openFee'],'-2');self.assertEqual(h['_feeRebateRate'],'0')
    def test_short_closing_buy_means_short(self):
        a=record(side='sell');b=record('2','20','buy',close='99',fee='0',cash='1',ts='2000')
        self.assertEqual(c.settled_trades([a,b])[0]['holdSide'],'short')
    def test_partial_close_waits_then_weighted(self):
        a=record(qty='2');b=record('2','20','sell',close='103',fee='0',cash='3',ts='2000')
        self.assertEqual(c.settled_trades([a,b]),[])
        d=record('3','30','sell',close='105',fee='0',cash='5',ts='3000')
        h=c.settled_trades([a,b,d])[0];self.assertEqual(h['closeAvgPrice'],'104');self.assertEqual(h['netProfit'],'6')
    def test_ambiguous_equal_openings_never_guess(self):
        a=record();b=record('2','11',ts='1100');d=record('3','20','sell',close='105',fee='0',cash='5',ts='3000')
        self.assertEqual(c.settled_trades([a,b,d]),[])
    def test_duplicate_records_dont_double_count(self):
        a=record();b=record('2','20','sell',close='103',fee='0',cash='3',ts='2000')
        self.assertEqual(len(c.settled_trades([a,a,b,b])),1)
    def test_nonmatching_price_not_paired(self):
        a=record();b=record('2','20','sell',entry='101',close='103',fee='0',cash='2',ts='2000')
        self.assertEqual(c.settled_trades([a,b]),[])
    def test_current_fee_and_swap(self):
        x=c.normalize_position(raw(),record(),1000,{'leverage':'500'},{'bid1':'110','ask1':'111'})
        self.assertEqual(x['_cfdNetPnl'],'7');self.assertEqual(x['markPrice'],'110');self.assertEqual(x['cTime'],'1000')
    def test_unknown_opening_keeps_position_net_unknown(self):
        x=c.normalize_position(raw(),None,1234)
        self.assertIsNone(x['_cfdNetPnl']);self.assertIsNone(x['_cfdOpenedAt']);self.assertEqual(x['total'],'1')
    def test_partial_quantity_not_claim_complete_financials(self):
        x=c.normalize_position(raw(qty='0.5'),record(),1000)
        self.assertFalse(x['_cfdFinancialsComplete']);self.assertIsNone(x['_cfdNetPnl'])
    def test_mismatched_opening_never_used(self):
        x=c.normalize_position(raw(),record(order='99'),1000)
        self.assertIsNone(x['_cfdOpenedAt']);self.assertIsNone(x['deductedFee'])
    def test_nonfinite_position_rejected(self):
        r=raw();r['qty']='NaN'
        with self.assertRaises(ValueError):c.normalize_position(r,record(),1000)
    def test_duplicate_open_order_fee_unknown(self):
        a=record(); b=record('2','10')
        self.assertEqual(c.opening_records([a,b]),{})
    def test_multiple_roundtrips_keep_independent_settlements(self):
        records = [record(id='1', order='10', ts='1000'),
                   record(id='2', order='11', side='sell', close='101', fee='-1', cash='10', ts='2000'),
                   record(id='3', order='20', side='sell', entry='200', fee='-3', ts='3000'),
                   record(id='4', order='21', side='buy', entry='200', close='199', fee='-1', cash='8', ts='4000')]
        result = c.settled_trades(records)
        self.assertEqual(len(result), 2)
        self.assertEqual([r['positionId'] for r in result], ['10', '20'])
        self.assertEqual([r['netProfit'] for r in result], ['7', '4'])
        self.assertEqual([r['holdSide'] for r in result], ['long', 'short'])
    def test_activation_no_replay_and_ledger_catches_fast_trade(self):
        clock=[10.0];calls=[]
        data=[record(ts='9000'),record('2','20','sell',close='103',fee='0',cash='3',ts='9500')]
        def get(params,path):
            if path.endswith('fund-detail'):return {'currency':'USD'}
            calls.append(path);return {'list':copy.deepcopy(data),'cursor':None}
        a=c.CFDAdapter(get,':memory:',clock=lambda:clock[0],monotonic=lambda:clock[0]);self.addCleanup(a.db.close)
        self.assertEqual(a.history_positions(),[]);self.assertEqual(a.opening_events(),[])
        data.extend([record('3','30',ts='11000'),record('4','40','sell',close='104',fee='0',cash='4',ts='12000')]);clock[0]=16
        self.assertEqual(len(a.history_positions()),1);self.assertEqual(len(a.opening_events()),1)
        self.assertEqual(len(calls),2)
    def test_failed_financial_fetch_does_not_advance_watermark(self):
        def get(params,path):raise RuntimeError('unavailable')
        a=c.CFDAdapter(get,':memory:',clock=lambda:10);self.addCleanup(a.db.close)
        with self.assertRaises(RuntimeError):a.history_positions()
        self.assertIsNone(a.db.execute("SELECT value FROM cfd_metadata WHERE key='watermark'").fetchone())
    def test_shared_readers_same_key(self):
        from types import SimpleNamespace
        x=SimpleNamespace(config=SimpleNamespace(db_path=':memory:',api_base='test',api_key='test'),_get=lambda p,q:[])
        self.assertIs(c.get_cfd_adapter(x),c.get_cfd_adapter(x))

if __name__=='__main__':unittest.main()
