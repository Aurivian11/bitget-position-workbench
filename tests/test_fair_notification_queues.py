import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
HERE=Path(__file__).resolve().parent
APP=HERE.parent/'app'
sys.path.insert(0,str(APP));sys.path.insert(0,str(HERE))
import bot
import telegram_live
import discord_push

def record(kind, marker, complete=False):
    item={'marker':marker}
    if complete:item['_history']={'symbol':'FAKEUSDT','holdSide':'long','netProfit':'1','openFee':'-0.1','closeFee':'-0.1','totalFunding':'0','openAvgPrice':'10','closeAvgPrice':'11','openTotalPos':'1','closeTotalPos':'1','ctime':'1000','utime':'2000'}
    return (kind,'fake-'+str(marker),json.dumps(item),json.dumps(item),'2000-01-01T00:00:00+00:00')

class FakeTransport:
    def __init__(self):self.calls=[]
    def send(self,text):self.calls.append(text);return str(len(self.calls))

class FakeBoard:
    def __init__(self):self.closed=[]
    def close(self,item):self.closed.append(item['marker']);return True
    def message_ids(self,item):return ['closed-'+str(item['marker'])]
    def sync(self,positions):pass

class FairQueueTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(dir=HERE)
        self.dbfile=Path(self.temp.name)/'queue.sqlite3'
        self.store=bot.Store(self.dbfile)
        self.network=patch('urllib.request.urlopen',side_effect=AssertionError('network forbidden'))
        self.network.start();self.transport=FakeTransport();self.board=FakeBoard()
    def tearDown(self):
        self.network.stop();self.store.db.close();self.temp.cleanup()
    def insert(self, rows):
        with self.store.db:self.store.db.executemany('INSERT INTO position_events(event_type,position_key,before_json,after_json,created_at) VALUES(?,?,?,?,?)',rows)
    def telegram(self):
        return telegram_live.TelegramLiveDispatcher(lambda:self.store,self.transport,lambda:{},lambda kind,b,a:kind,lambda *_a,**_k:None,trade_board_factory=lambda *_:self.board)
    def discord(self):
        return discord_push.DiscordDispatcher(self.dbfile,'https://discord.com/api/webhooks/00000/FAKE_TOKEN_FOR_ISOLATED_TEST',1,1,lambda kind,b,a:kind,lambda *_:'',lambda *_:'',transport=self.transport,trade_board_factory=lambda *_:self.board)
    def set_ready(self,event_id):
        with self.store.db:self.store.db.execute('UPDATE position_events SET before_json=? WHERE id=?',(record('closed',event_id,True)[2],event_id))
    def test_telegram_new_events_bypass_old_incomplete_settlements(self):
        self.insert([record('closed',i+1) for i in range(201)]+[record('increased',202)])
        worker=self.telegram()
        with patch.object(telegram_live.notification_settings,'event_enabled',return_value=True),patch.object(telegram_live.notification_settings,'board_enabled',return_value=False):
            worker._cycle(self.store)
        self.assertEqual(self.transport.calls,['increased'])
        self.assertEqual(self.store.db.execute('SELECT telegram_sent FROM position_events WHERE id=202').fetchone()[0],1)
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM position_events WHERE event_type='closed' AND telegram_sent=0").fetchone()[0],201)
        self.assertLessEqual(len(self.store.unsent_events()),200)
    def test_telegram_pending_closes_rotate_and_retry_when_settlement_arrives(self):
        self.insert([record('closed',i+1) for i in range(201)])
        worker=self.telegram()
        with patch.object(telegram_live.notification_settings,'event_enabled',return_value=True),patch.object(telegram_live.notification_settings,'board_enabled',return_value=False):
            worker._cycle(self.store);self.set_ready(1)
            for _ in range(3):worker._cycle(self.store)
        self.assertEqual(self.board.closed,[1])
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM position_events').fetchone()[0],201)
    def test_telegram_ready_order_is_stable(self):
        self.insert([record('increased',1),record('reduced',2),record('increased',3)])
        worker=self.telegram()
        with patch.object(telegram_live.notification_settings,'event_enabled',return_value=True),patch.object(telegram_live.notification_settings,'board_enabled',return_value=False):
            for _ in range(3):worker._cycle(self.store)
        self.assertEqual(self.transport.calls,['increased','reduced','increased'])
    def test_discord_new_events_bypass_incomplete_settlements(self):
        worker=self.discord()
        self.insert([record('closed',i+1) for i in range(41)]+[record('increased',42)])
        with patch.object(discord_push.notification_settings,'event_enabled',return_value=True):worker.deliver_events()
        self.assertEqual(len(self.transport.calls),1)
        self.assertEqual(self.store.db.execute('SELECT complete FROM discord_event_delivery WHERE event_id=42').fetchone()[0],1)
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM position_events WHERE event_type='closed'").fetchone()[0],41)
    def test_discord_closed_cursor_wraps_and_keeps_retryable_rows(self):
        worker=self.discord()
        self.insert([record('closed',i+1) for i in range(41)])
        with patch.object(discord_push.notification_settings,'event_enabled',return_value=True):
            worker.deliver_events();self.set_ready(1)
            for _ in range(3):worker.deliver_events()
        self.assertEqual(self.board.closed,[1])
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM discord_event_delivery').fetchone()[0],1)
    def test_discord_keeps_twenty_send_batch_limit(self):
        worker=self.discord();self.insert([record('increased',i+1) for i in range(50)])
        with patch.object(discord_push.notification_settings,'event_enabled',return_value=True):
            worker.deliver_events();self.assertEqual(len(self.transport.calls),20)
            worker.deliver_events();self.assertEqual(len(self.transport.calls),40)
            worker.deliver_events();self.assertEqual(len(self.transport.calls),50)
    def test_muted_closes_do_not_starve_enabled_size_changes(self):
        worker=self.discord()
        self.insert([record('closed',i+1) for i in range(41)]+[record('increased',42)])
        with patch.object(discord_push.notification_settings,'event_enabled',side_effect=lambda c,e:e!='closed'):worker.deliver_events()
        self.assertEqual(len(self.transport.calls),1)
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM discord_event_delivery WHERE complete=2').fetchone()[0],20)

if __name__=='__main__':unittest.main(verbosity=2)
