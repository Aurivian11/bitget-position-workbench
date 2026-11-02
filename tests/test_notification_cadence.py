import sys
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch
HERE=Path(__file__).resolve().parent
APP=HERE.parent/'app'
sys.path.insert(0,str(APP));sys.path.insert(0,str(HERE))
import telegram_live as fixed
from telegram_transport import TelegramRateLimited,TelegramError

class FakeDB:
    def close(self):pass

class FakeStore:
    def __init__(self):self.rows=[];self.sent=[];self.db=FakeDB()
    def unsent_events(self, closed_after_id=0):return self.rows[:100]
    def mark_sent(self,i):
        self.sent.append(i)
        self.rows=[r for r in self.rows if r['id']!=i]

def scenario(module, interval, *, cycles=4, inject_at=1, events=2, board=True, rate_limit=None, fail_once=False):
    clock=[100.]
    store=FakeStore();created=[];sent=[];attempts=[];waits=[];step=[0];limited=[rate_limit];failed=[fail_once]
    def send(text):
        attempts.append(clock[0])
        if limited[0] is not None:
            delay=limited[0];limited[0]=None;raise TelegramRateLimited(delay)
        if failed[0]:
            failed[0]=False;raise TelegramError("synthetic transient failure")
        sent.append(clock[0]);return len(sent)
    tg=type('FakeTelegram',(),{'send':staticmethod(send)})()
    def provider():created.append(clock[0]);return {}
    worker=module.TelegramLiveDispatcher(lambda:store,tg,provider,lambda *_:"event",lambda *_a,**_k:None,update_seconds=interval)
    def wait(deadline):
        if step[0]>=cycles:return False
        clock[0]=max(clock[0],deadline);waits.append(clock[0])
        if step[0]==inject_at:
            store.rows.extend({'id':i+1,'event_type':'increased','before_json':'{}','after_json':'{}'} for i in range(events))
        step[0]+=1;return True
    worker._wait_until=wait
    with patch.object(module.time,'monotonic',side_effect=lambda:clock[0]),patch.object(module.notification_settings,'event_enabled',return_value=True),patch.object(module.notification_settings,'board_enabled',return_value=board):
        worker._run()
    return {'sent':sent,'provider':created,'attempts':attempts,'waits':waits,'processed':store.sent}

class NotificationCadenceTests(unittest.TestCase):
    def setUp(self):
        self.no_network=patch('urllib.request.urlopen',side_effect=AssertionError('network forbidden in isolation test'))
        self.no_network.start()
    def tearDown(self):self.no_network.stop()
    def test_slow_board_does_not_delay_trade_events(self):
        for interval in (60,3600):
            with self.subTest(interval=interval):
                result=scenario(fixed,interval)
                self.assertEqual(result['sent'],[101.,102.])
                self.assertEqual(result['provider'],[100.])
                self.assertEqual(result['processed'],[1,2])
    def test_board_keeps_requested_sixty_second_interval(self):
        result=scenario(fixed,60,cycles=125,events=0)
        self.assertEqual(result['provider'],[100.,160.,220.])
    def test_existing_one_second_behavior_is_preserved(self):
        result=scenario(fixed,1,cycles=5,events=0)
        self.assertEqual(result['provider'],[100.,101.,102.,103.,104.])
    def test_events_continue_with_board_disabled(self):
        result=scenario(fixed,3600,board=False)
        self.assertEqual(result['sent'],[101.,102.])
        self.assertEqual(result['provider'],[])
    def test_events_are_not_burst_sent(self):
        result=scenario(fixed,60,cycles=8,events=6,inject_at=0)
        self.assertEqual(len(result['sent']),6)
        self.assertTrue(all(b-a>=1 for a,b in zip(result['sent'],result['sent'][1:])))
    def test_rate_limit_is_respected_without_board_interval_delay(self):
        result=scenario(fixed,3600,cycles=4,events=1,inject_at=0,rate_limit=7)
        self.assertEqual(result['attempts'],[100.,107.])
        self.assertEqual(result['sent'],[107.])
    def test_transient_failure_uses_normal_backoff(self):
        result=scenario(fixed,3600,cycles=4,events=1,inject_at=0,fail_once=True)
        self.assertEqual(result['attempts'],[100.,102.])
    def test_stop_interrupts_wait(self):
        store=FakeStore()
        worker=fixed.TelegramLiveDispatcher(lambda:store,None,lambda:{},lambda *_:"",lambda *_a,**_k:None,update_seconds=3600)
        entered=threading.Event();done=threading.Event()
        def waiting():
            entered.set();self.assertFalse(worker._wait_until(time.monotonic()+3600));done.set()
        thread=threading.Thread(target=waiting);worker._thread=thread;thread.start();entered.wait(1)
        started=time.monotonic();worker.stop()
        self.assertTrue(done.is_set())
        self.assertLess(time.monotonic()-started,2)

if __name__=='__main__':unittest.main(verbosity=2)
