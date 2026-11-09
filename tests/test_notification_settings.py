from contextlib import closing
import copy
import io
import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
os.environ.setdefault('BITGET_WEB_ORIGIN','https://example.com')
import notification_settings as ns
import web_admin as wa

class Handler:
    command='GET'
    client_address=('127.0.0.1',10)
    def __init__(self,command='GET',body=None,headers=None):
        self.command=command; self.headers={'Host':'example.com','X-Forwarded-Proto':'https','Origin':wa.ORIGIN,**(headers or {})}
        raw=json.dumps(body or {}).encode(); self.headers['Content-Length']=str(len(raw)); self.headers['Content-Type']='application/json'
        self.rfile=io.BytesIO(raw);self.wfile=io.BytesIO();self.response_headers={}
    def send_response(self,status):self.status=status
    def security_headers(self):pass
    def send_header(self,key,value):self.response_headers[key]=value
    def end_headers(self):pass
    def json(self):return json.loads(self.wfile.getvalue())

class ConfigurationTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(dir=Path(__file__).parent,prefix='test-state-')
        self.root=Path(self.temp.name);(self.root/'config').mkdir();(self.root/'data').mkdir()
        (self.root/'config/.env').write_text('TELEGRAM_BOT_TOKEN=123456789:'+('a'*35)+'\nTELEGRAM_CHAT_ID=-10012345\nDISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/123456789/'+('b'*60)+'\nBARK_KEY=EXAMPLEKEY123\nBARK_BASE=http://127.0.0.1:8080\n')
        self.environment=patch.dict(os.environ,{},clear=True);self.environment.start()
        self.network=patch('urllib.request.urlopen',side_effect=AssertionError('network forbidden'));self.network.start()
        self.patches=[patch.object(wa,'ORIGIN','https://example.com'),patch.object(wa,'TRUSTED_PROXIES',frozenset({'127.0.0.1','::1'})),patch.object(wa,'_ORIGIN_PARTS',__import__('urllib.parse',fromlist=['urlsplit']).urlsplit('https://example.com')),patch.object(ns,'PROJECT',self.root),patch.object(ns,'SETTINGS',self.root/'data/web-notification-settings.json'),patch.object(ns,'APPLIED',self.root/'data/web-notification-applied.json'),patch.object(ns,'RUNTIME',self.root/'data/web-notification-runtime.json'),patch.object(wa,'AUTH_PATH',self.root/'config/web-admin.json')]
        for p in self.patches:p.start()
        ns._CURRENT=None;wa._RATE.clear();wa._GLOBAL_RATE.clear()
    def tearDown(self):
        ns._CURRENT=None
        for p in self.patches:p.stop()
        self.temp.cleanup()
        self.environment.stop();self.network.stop()
    def test_blank_secrets_preserved_and_safe_projection(self):
        original=ns.load();saved=ns.save({'revision':0,'channels':{'telegram':{'token':'','chat_id':'-10012345'},'discord':{'webhook':''},'bark':{'key':''}}})
        self.assertEqual(ns.load()['channels'],original['channels'])
        for channel,field in ns.SECRET_FIELDS.items():
            self.assertNotIn(field,saved['channels'][channel]);self.assertTrue(saved['channels'][channel][field+'_configured'])
            self.assertNotIn(original['channels'][channel][field],json.dumps(saved))
        self.assertFalse(ns.SETTINGS.exists())
        ns.apply_environment()
        self.assertEqual(ns._CURRENT['channels'],original['channels'])
    def test_validation_atomicity_revision_and_allowlist(self):
        ns.save({'revision':0,'channels':{'bark':{'group':'integration_seed'}}});original=ns.SETTINGS.read_bytes()
        cases=[{'revision':0,'channels':{}},{'revision':1,'channels':{'telegram':{'update_seconds':0}}},{'revision':1,'channels':{'telegram':{'token_configured':True}}},{'revision':1,'channels':{'discord':{'webhook':'https://evil.example/api/webhooks/12345/'+('a'*30)}}},{'revision':1,'channels':{'bark':{'base':'http://169.254.169.254'}}}]
        for case in cases:
            with self.assertRaises(ns.SettingsError):ns.save(case)
            self.assertEqual(original,ns.SETTINGS.read_bytes())
    def test_channel_filter_and_disable(self):
        ns.save({'revision':0,'channels':{'telegram':{'events':['closed'],'board_enabled':False},'bark':{'enabled':False}}})
        ns.apply_environment()
        self.assertTrue(ns.event_enabled('telegram','closed'));self.assertFalse(ns.event_enabled('telegram','increased'));self.assertFalse(ns.board_enabled('telegram'));self.assertFalse(ns.enabled('bark'))
    def test_auth_https_origin_csrf_and_redaction(self):
        password='test-password-for-admin-123';wa.initialize_auth(password)
        self.assertNotIn(password,wa.AUTH_PATH.read_text())
        h=Handler('POST',{'password':password});h.client_address=('198.51.100.1',10)
        wa.handle(h,None,'/api/admin/login');self.assertEqual(h.status,403)
        h=Handler('POST',{'password':password},headers={'Origin':'https://evil.example'})
        wa.handle(h,None,'/api/admin/login');self.assertEqual(h.status,403)
        h=Handler('POST',{'password':password});wa.handle(h,None,'/api/admin/login');self.assertEqual(h.status,200)
        cookie=h.response_headers['Set-Cookie'];csrf=h.json()['csrfToken']
        for flag in ['Secure','HttpOnly','SameSite=Strict','Path=/bitget/']:self.assertIn(flag,cookie)
        h=Handler(headers={'Cookie':cookie.split(';')[0]});wa.handle(h,None,'/api/admin/settings');self.assertEqual(h.status,200);self.assertNotIn('"token":',h.wfile.getvalue().decode())
        h=Handler('POST',{'revision':0,'channels':{}},headers={'Cookie':cookie.split(';')[0]});wa.handle(h,None,'/api/admin/settings');self.assertEqual(h.status,403);self.assertFalse(ns.SETTINGS.exists())
        h=Handler('POST',{'revision':0,'channels':{}},headers={'Cookie':cookie.split(';')[0],'X-CSRF-Token':csrf});wa.handle(h,None,'/api/admin/settings');self.assertEqual(h.status,200);self.assertEqual(h.json()['revision'],0)
        h=Handler('POST',{},headers={'Cookie':cookie.split(';')[0],'X-CSRF-Token':csrf});wa.handle(h,None,'/api/admin/logout');self.assertEqual(h.status,200)
        h=Handler(headers={'Cookie':cookie.split(';')[0]});wa.handle(h,None,'/api/admin/settings');self.assertEqual(h.status,401)
    def test_rate_limit(self):
        h=Handler()
        self.assertEqual([wa._limited(h) for _ in range(11)],[False]*10+[True])
    def test_target_switch_archives_ids_and_skips_old_pending(self):
        path=self.root/'data/positions.sqlite3'
        with closing(sqlite3.connect(path)) as db, db:
            db.executescript("CREATE TABLE position_events(id INTEGER,event_type TEXT,telegram_sent INTEGER); CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT); CREATE TABLE trade_message_delivery(channel TEXT,trade_key TEXT,PRIMARY KEY(channel,trade_key)); CREATE TABLE discord_notices(id INTEGER,complete INTEGER); INSERT INTO position_events VALUES(1,'increased',0); INSERT INTO trade_message_delivery VALUES('telegram','live-position'); INSERT INTO metadata VALUES('pnl_message_id','123');")
        ns.apply_environment();ns.prepare_runtime(path)
        ns.save({'revision':0,'channels':{'telegram':{'chat_id':'-10098765'}}});ns.apply_environment();ns.prepare_runtime(path)
        with closing(sqlite3.connect(path)) as db, db:
            self.assertEqual(db.execute('SELECT telegram_sent FROM position_events').fetchone()[0],2)
            self.assertTrue(db.execute('SELECT channel FROM trade_message_delivery').fetchone()[0].startswith('telegram:archived:'))
            self.assertIsNone(db.execute("SELECT value FROM metadata WHERE key='pnl_message_id'").fetchone())
            self.assertIsNone(db.execute("SELECT value FROM metadata WHERE key='discord_start_event_id'").fetchone())
            db.execute("INSERT INTO position_events VALUES(2,'closed',0)")
        ns.prepare_runtime(path)
        with closing(sqlite3.connect(path)) as db, db:
            self.assertEqual(db.execute('SELECT telegram_sent FROM position_events WHERE id=2').fetchone()[0],0)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM trade_message_delivery').fetchone()[0],1)
    def test_disabled_event_dispatchers_do_not_send(self):
        import bot
        from telegram_live import TelegramLiveDispatcher
        from discord_push import DiscordDispatcher
        path=self.root/'data/positions.sqlite3';store=bot.Store(path)
        store.db.execute("INSERT INTO position_events(event_type,position_key,created_at) VALUES('increased','example','2026-11-02T00:00:00+08:00')");store.db.commit()
        ns.save({'revision':0,'channels':{'telegram':{'events':[],'board_enabled':False},'discord':{'events':[],'board_enabled':False}}});ns.apply_environment()
        class Transport:
            def send(self,*args):raise AssertionError('Unexpected notification')
        def provider():raise AssertionError('Disabled board read live positions')
        tg=TelegramLiveDispatcher(lambda:store,Transport(),provider,lambda *args:'event',lambda *args:None)
        tg._cycle(store)
        self.assertEqual(store.db.execute('SELECT telegram_sent FROM position_events').fetchone()[0],2)
        dc=DiscordDispatcher(path,'',1,1,lambda *args:'event',lambda *args:'board',lambda *args:'summary',transport=Transport(),positions_provider=provider)
        dc._set_meta('discord_start_event_id','0');dc.deliver_events();dc.sync_dashboard()
        self.assertEqual(store.db.execute('SELECT complete FROM discord_event_delivery').fetchone()[0],2)
        store.db.close()

    def test_runtime_connections_close_after_channel_reset(self):
        path=self.root/'data/positions.sqlite3'
        bark_path=self.root/'data/bark_orders.sqlite3'
        with closing(sqlite3.connect(path)) as db, db:
            db.executescript('CREATE TABLE position_events(id INTEGER,event_type TEXT,telegram_sent INTEGER); CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT);')
        with closing(sqlite3.connect(bark_path)) as db, db:
            db.executescript("CREATE TABLE bark_metadata(key TEXT PRIMARY KEY,value TEXT); CREATE TABLE bark_open_orders(state TEXT); INSERT INTO bark_open_orders VALUES('pending');")
        connections=[]
        class TrackedConnection(sqlite3.Connection):
            closed=False
            def close(self):
                self.closed=True
                return super().close()
        real_connect=sqlite3.connect
        def tracked(*args,**kwargs):
            kwargs['factory']=TrackedConnection
            connection=real_connect(*args,**kwargs)
            connections.append(connection)
            return connection
        ns.apply_environment()
        with patch.object(ns.sqlite3,'connect',side_effect=tracked):
            ns.prepare_runtime(path)
            ns.save({'revision':0,'channels':{'bark':{'key':'CHANGED_TEST_KEY_123'}}})
            ns.apply_environment()
            ns.prepare_runtime(path)
        self.assertEqual(len(connections),3)
        self.assertTrue(all(connection.closed for connection in connections))
        with closing(real_connect(bark_path)) as db:
            self.assertEqual(db.execute('SELECT state FROM bark_open_orders').fetchone()[0],'ignored')

if __name__=='__main__':unittest.main(verbosity=2)
