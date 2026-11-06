import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'app'))
import bot
from telegram_live import TelegramLiveDispatcher
from discord_push import DiscordDispatcher

class Transport:
    def __init__(self):self.sent=[];self.edited=[];self.deleted=[]
    def send(self,payload):
        self.sent.append(payload);return str(1000+len(self.sent))
    def edit(self,mid,payload):self.edited.append((str(mid),payload))
    def delete(self,mid):self.deleted.append(str(mid))

def position(pid='123',symbol='XAUUSD',cfd=True):
    return {'productType':'CFD' if cfd else 'USDT-FUTURES','symbol':symbol,'holdSide':'long',
        'marginCoin':'USD' if cfd else 'USDT','posId':pid,'cTime':'1793908800000','total':'0.01',
        'openPriceAvg':'4000','markPrice':'4001','unrealizedPL':'1','deductedFee':'0.06',
        'achievedProfits':'0','totalFee':'0','leverage':'20','marginSize':'2',
        '_accountMode':'cfd' if cfd else 'uta','_cfdOpenedAt':'1793908800000',
        '_cfdNetPnl':'0.94','_cfdOpeningOrderId':pid,'_cfdOpeningEvent':True,
        'takeProfit':'','stopLoss':''}

def closed(item):
    h={'positionId':item['posId'],'symbol':item['symbol'],'holdSide':item['holdSide'],
       'marginCoin':item['marginCoin'],'productType':item['productType'],
       'ctime':item['cTime'],'utime':'1793908809000','openAvgPrice':item['openPriceAvg'],
       'closeAvgPrice':'4001','openTotalPos':item['total'],'closeTotalPos':item['total'],
       'netProfit':'0.94','openFee':'-0.06','closeFee':'0','totalFunding':'0',
       '_accountMode':item['_accountMode'],'_cfdOpenedAt':item['_cfdOpenedAt']}
    return dict(item,_history=h)

class Dispatchers(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.stores=[]
        self.patches=[patch('notification_settings.board_enabled',return_value=True),patch('notification_settings.event_enabled',return_value=True)]
        for p in self.patches:p.start()
    def tearDown(self):
        for store in self.stores:store.db.close()
        for p in self.patches:p.stop()
        self.temp.cleanup()
    def setup_channel(self,channel):
        path=Path(self.temp.name)/(channel+'.sqlite3')
        store=bot.Store(path);self.stores.append(store)
        current={};transport=Transport()
        if channel=='telegram':
            worker=TelegramLiveDispatcher(lambda:store,transport,lambda:dict(current),bot.format_event,bot.sync_pnl_message,trade_board_factory=bot.make_telegram_trade_board)
            tick=lambda:worker._cycle(store)
            board=lambda:worker.trade_board or bot.make_telegram_trade_board(store,transport)
        else:
            worker=DiscordDispatcher(path,'',5,1,bot.format_event,bot.format_pnl_report,bot.format_summary,transport=transport,positions_provider=lambda:dict(current),trade_board_factory=bot.make_discord_trade_board)
            tick=worker.deliver_events
            board=worker._get_trade_board
        return store,current,transport,worker,tick,board
    def add(self,store,kind,item,before=None):
        events=[(kind,bot.position_key(item),before,item if kind!='closed' else None)]
        if kind=='closed':events=[(kind,bot.position_key(item),item,None)]
        store.save_cycle({},events,False)
    def test_core_observed_open_never_displayed_gets_one_open_notice(self):
        for channel in ('telegram','discord'):
            with self.subTest(channel=channel):
                store,current,t,w,tick,board=self.setup_channel(channel)
                item=position();self.add(store,'opened',item)
                tick();tick()
                self.assertEqual(len(t.sent),1)
                self.assertIn('已开仓',json.dumps(t.sent,ensure_ascii=False))
    def test_ledger_only_fast_trade_sends_open_and_final_once(self):
        for channel in ('telegram','discord'):
            with self.subTest(channel=channel):
                store,current,t,w,tick,board=self.setup_channel(channel)
                item=dict(position(),_notifyOpening=True)
                self.add(store,'opened',item);self.add(store,'closed',closed(item))
                tick();tick();tick()
                self.assertEqual(len(t.sent),2)
                text=json.dumps(t.sent,ensure_ascii=False)
                self.assertIn('已开仓',text);self.assertIn('已平仓',text)
                self.assertNotIn('待同步',text)
    def test_actual_factory_two_positions_share_one_message(self):
        for channel in ('telegram','discord'):
            with self.subTest(channel=channel):
                store,current,t,w,tick,board=self.setup_channel(channel)
                a=position();b=position('124','BTCUSDT',False)
                positions={bot.position_key(a):a,bot.position_key(b):b}
                manager=board();manager.sync(positions);manager.sync(positions)
                self.assertEqual(len(t.sent),1)
                text=json.dumps(t.sent[0],ensure_ascii=False)
                self.assertIn('XAUUSD',text);self.assertIn('BTCUSDT',text)
                self.assertIn('持仓（2）',text)
    def test_visible_cfd_open_does_not_add_second_open_message(self):
        for channel in ('telegram','discord'):
            with self.subTest(channel=channel):
                store,current,t,w,tick,board=self.setup_channel(channel)
                item=position();current[bot.position_key(item)]=item
                manager=board();manager.sync(current);w.trade_board=manager
                self.add(store,'opened',item);tick();tick()
                self.assertEqual(len(t.sent),1)
    def test_resizes_and_final_stay_separate_from_aggregate(self):
        for channel in ('telegram','discord'):
            with self.subTest(channel=channel):
                store,current,t,w,tick,board=self.setup_channel(channel)
                item=position();current[bot.position_key(item)]=item
                manager=board();manager.sync(current);w.trade_board=manager
                increased=dict(item,total='0.02')
                self.add(store,'increased',increased,before=item);tick()
                self.add(store,'reduced',item,before=increased);tick()
                current.clear();self.add(store,'closed',closed(item));tick();tick()
                self.assertEqual(len(t.sent),4)
                text=json.dumps(t.sent,ensure_ascii=False)
                self.assertIn('加仓',text);self.assertIn('减仓',text);self.assertIn('已平仓',text)

if __name__=='__main__':unittest.main()
