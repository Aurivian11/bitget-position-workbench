import sys
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'app'))
from bark_orders import BarkOrderDispatcher, normalize_cfd_opening, message_for


def opening(**kw):
    result={'_cfdOpeningEvent':True,'_accountMode':'cfd','_cfdOpeningOrderId':'123',
        'posId':'123','symbol':'XAUUSD','holdSide':'short','total':'0.01',
        'openPriceAvg':'4000','_cfdOpenedAt':'2000','cTime':'9999','marginCoin':'USD'}
    result.update(kw)
    return result

class CFDTest(unittest.TestCase):
    def setUp(self):
        self.directory=tempfile.TemporaryDirectory()
        self.path=Path(self.directory.name)/'bark.sqlite3'
        self.calls=[]
        self.cfd=SimpleNamespace(opening_events=lambda:[opening()])
        def get(params,path=None):
            self.calls.append((params,path))
            return {'list':[], 'cursor':None}
        self.client=SimpleNamespace(config=SimpleNamespace(product_types=('USDT-FUTURES',),api_key='fake-test-key'),uta=True,cfd=self.cfd,_get=get)
        self.transport=Mock()
        self.worker=BarkOrderDispatcher(self.path,self.client,self.transport,now_ms=lambda:1000)
        self.worker.bootstrap()
    def tearDown(self):
        self.directory.cleanup()
    def pending(self):
        with self.worker._connect() as db:
            return db.execute('SELECT product_type,order_id,state FROM bark_open_orders ORDER BY order_id').fetchall()
    def test_confirmed_opening_uses_actual_time_and_lots(self):
        event=normalize_cfd_opening(opening())
        self.assertEqual(event['fill_ms'],2000)
        self.assertEqual(event['quantity'],'0.01')
        _,body=message_for(event)
        self.assertIn('0.01 手',body)
        self.assertIn('4000 USD',body)
    def test_invalid_or_unconfirmed_openings_rejected(self):
        for fields in ({'_cfdOpeningEvent':False},{'_accountMode':'uta'},{'_cfdOpenedAt':None},{'holdSide':'unknown'},{'total':'nan'},{'openPriceAvg':'0'}):
            self.assertIsNone(normalize_cfd_opening(opening(**fields)))
    def test_roundtrip_between_polls_still_queues_open(self):
        self.worker.reconcile_cfd()
        self.assertEqual(len(self.pending()),1)
        self.worker.deliver_pending()
        self.transport.send.assert_called_once()
    def test_duplicate_and_restart_send_only_once(self):
        self.worker.reconcile_cfd();self.worker.reconcile_cfd()
        self.worker.deliver_pending()
        restarted=BarkOrderDispatcher(self.path,self.client,self.transport,now_ms=lambda:1000)
        restarted.bootstrap();restarted.reconcile_cfd();restarted.deliver_pending()
        self.transport.send.assert_called_once()
    def test_bark_baseline_suppresses_old_open(self):
        self.cfd.opening_events=lambda:[opening(_cfdOpenedAt='999')]
        self.worker.reconcile_cfd()
        self.assertEqual(len(self.pending()),0)
    def test_crypto_paths_never_receive_cfd_category(self):
        self.worker.now_ms=lambda:3000
        self.worker.reconcile()
        self.assertTrue(self.calls)
        self.assertTrue(all(params.get('category')=='USDT-FUTURES' for params,_ in self.calls))
    def test_cfd_failure_does_not_interrupt_crypto(self):
        self.cfd.opening_events=Mock(side_effect=RuntimeError('synthetic'))
        with self.assertLogs('bitget-bark-orders',level='WARNING'):
            self.worker._poll_cfd()
        self.worker.now_ms=lambda:3000
        self.worker.reconcile()
        self.assertTrue(self.worker._get_meta('last_rest_ok'))
    def test_disabled_cfd_preserves_crypto_products(self):
        self.client.cfd=None
        worker=BarkOrderDispatcher(Path(self.directory.name)/'disabled.sqlite3',self.client,self.transport,now_ms=lambda:1000)
        self.assertEqual(worker.products,('USDT-FUTURES',))
        worker._poll_cfd()
        self.transport.send.assert_not_called()

if __name__=='__main__':unittest.main()
