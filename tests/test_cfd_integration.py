import copy,json,sqlite3,sys,unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock,patch
QA=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(QA/'app'))
import bot
from trade_messages import identity
from cfd_adapter import normalize_position,settled_trades


def cfd(pid='10'):
    opening=dict(orderId=pid,symbol='XAUUSD',side='buy',qty='0.01',openPrice='100',fee='-0.06',ts='1000')
    raw=dict(positionId=pid,symbol='XAUUSD',side='buy',qty='0.01',openPrice='100',interest='-0.01',unrealizedPnl='1',totalProfit='0.99')
    return normalize_position(raw,opening,1000)

class Integration(unittest.TestCase):
    def test_crypto_and_cfd_merge(self):
        client=object.__new__(bot.BitgetClient);client._crypto_positions=Mock(return_value={'crypto':{'symbol':'BTCUSDT'}});client.cfd=Mock()
        client.cfd.positions.return_value={'cfd':cfd()}
        self.assertEqual(set(client.positions()),{'crypto','cfd'})
    def test_separate_routing(self):
        client=object.__new__(bot.BitgetClient);client.cfd=Mock();client._get=Mock(side_effect=AssertionError('CFD went to crypto API'))
        client.history_positions('CFD');client.accounts('CFD');client.cfd.history_positions.assert_called_once();client.cfd.accounts.assert_called_once()
    def test_products_do_not_pollute_crypto_subscriptions(self):
        cfg=SimpleNamespace(product_types=('USDT-FUTURES',))
        with patch.dict(bot.os.environ,{'BITGET_CFD_ENABLED':'1'}):
            self.assertEqual(bot.monitored_products(cfg),('USDT-FUTURES','CFD'))
            self.assertEqual(bot.websocket_subscription_args(cfg.product_types,uta=True),[{'instType':'UTA','topic':'position'}])
    def test_cfd_same_symbol_same_ms_are_distinct(self):
        a,b=cfd('10'),cfd('11');self.assertNotEqual(bot.position_key(a),bot.position_key(b));self.assertNotEqual(identity(a),identity(b))
    def test_cash_pnl_does_not_apply_crypto_rebate(self):
        with patch.dict(bot.os.environ,{'FEE_REBATE_RATE':'.6'}):
            p=cfd();self.assertEqual(str(bot.current_net_pnl(p)),'0.93')
            self.assertEqual(bot.history_net_pnl(dict(netProfit='-1.44',openFee='-.06',closeFee='0',_accountMode='cfd')),bot.Decimal('-1.44'))
            self.assertEqual(bot.current_fee_rebate(p),0)
    def test_cfd_unknowns_and_units(self):
        p=cfd();p['deductedFee']=None;p['_cfdNetPnl']=None;p['_cfdOpenedAt']=None
        out='\n'.join(bot.format_position_lines(p));self.assertIn('CFD',out);self.assertIn('0.01 手',out);self.assertIn('返佣：<code>—</code>',out);self.assertIn('手续费：<code>—</code>',out);self.assertIn('可平仓报价',out);self.assertIn('隔夜利息',out);self.assertNotIn('待同步',out)
        self.assertIsNone(bot.net_break_even_price(p));self.assertEqual(bot.holding_duration(p),'-')
    def test_duration_uses_real_opening_time(self):
        p=cfd();p['cTime']='5000';self.assertEqual(bot.holding_duration(p,now_ms=11000),'00:00:10')
    def test_recover_fast_open_and_restart_dedupe(self):
        db=sqlite3.connect(':memory:');self.addCleanup(db.close);db.execute('CREATE TABLE position_events(event_type TEXT, after_json TEXT)');store=SimpleNamespace(db=db)
        client=SimpleNamespace(cfd=SimpleNamespace(opening_events=lambda:[cfd()]))
        events=bot.recover_cfd_openings(client,store,{},[]);self.assertEqual(len(events),1);self.assertTrue(events[0][3]['_notifyOpening'])
        db.execute('INSERT INTO position_events VALUES (?,?)',('opened',json.dumps(events[0][3])))
        self.assertEqual(bot.recover_cfd_openings(client,store,{},[]),[])
    def test_recover_observed_open_does_not_duplicate(self):
        db=sqlite3.connect(':memory:');self.addCleanup(db.close);db.execute('CREATE TABLE position_events(event_type TEXT, after_json TEXT)');store=SimpleNamespace(db=db)
        p=cfd();current={bot.position_key(p):p};events=bot.detect_events({},current)
        client=SimpleNamespace(cfd=SimpleNamespace(opening_events=lambda:[p]))
        self.assertEqual(len(bot.recover_cfd_openings(client,store,current,events)),1)
    def test_cfd_disabled_no_extra_read(self):
        self.assertEqual(bot.recover_cfd_openings(SimpleNamespace(),None,{},[]),[])

if __name__=='__main__':unittest.main()
