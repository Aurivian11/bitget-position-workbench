from contextlib import closing
import io,json,os,sqlite3,stat,tempfile,unittest,sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'app'))
os.environ.setdefault('BITGET_WEB_ORIGIN','https://example.com')
import notification_settings as ns
import web_admin as wa

class Fake:
 def __init__(self,path='/api/admin/settings',method='POST',body=None,headers=None,peer='127.0.0.1'):
  data=json.dumps(body or {}).encode()
  self.path=path;self.command=method;self.client_address=(peer,8000)
  self.headers={'Host':'example.com','X-Forwarded-Proto':'https','Origin':wa.ORIGIN,'Content-Type':'application/json','Content-Length':str(len(data)),'X-Bitget-Proxy-Peer':'198.51.100.1','X-Bitget-Client-IP':'198.51.100.1',**(headers or {})}
  self.rfile=io.BytesIO(data);self.wfile=io.BytesIO();self.status=None;self.out={}
 def send_response(self,value): self.status=value
 def send_header(self,key,value): self.out[key]=value
 def security_headers(self): pass
 def end_headers(self): pass

class Audit(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory(dir=Path(__file__).parent,prefix='isolated-')
  self.root=Path(self.tmp.name);(self.root/'config').mkdir();(self.root/'data').mkdir()
  (self.root/'config/.env').write_text('TELEGRAM_BOT_TOKEN=123456:ABCDEFGHIJKLMNOPQRSTUVWX_1234567\nTELEGRAM_CHAT_ID=12345\nDISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/123456789012345678/fake_token_AAAAAAAAAAAAAAAAAAAAAAAAAAAAAA\nBARK_BASE=http://127.0.0.1:8080\nBARK_KEY=fake_device_12345\n')
  self.environment=patch.dict(os.environ,{},clear=True);self.environment.start()
  self.network=patch('urllib.request.urlopen',side_effect=AssertionError('network forbidden'));self.network.start()
  self.originals={name:getattr(ns,name) for name in ['PROJECT','SETTINGS','APPLIED','RUNTIME','_CURRENT']}
  self.old_auth=wa.AUTH_PATH;self.old_origin=wa.ORIGIN;self.old_proxies=wa.TRUSTED_PROXIES
  wa.ORIGIN='https://example.com';wa.TRUSTED_PROXIES=frozenset({'127.0.0.1','::1'})
  ns.PROJECT=self.root;ns.SETTINGS=self.root/'data/settings.json';ns.APPLIED=self.root/'data/applied.json';ns.RUNTIME=self.root/'data/runtime.json';ns._CURRENT=None
  wa.AUTH_PATH=self.root/'config/admin.json';wa._RATE.clear();wa._GLOBAL_RATE.clear();wa._HASH_SLOTS=__import__('threading').BoundedSemaphore(2)
  self.password='isolated-test-password-not-production'
  wa.initialize_auth(self.password)
 def tearDown(self):
  for name,value in self.originals.items():setattr(ns,name,value)
  wa.AUTH_PATH=self.old_auth;wa.ORIGIN=self.old_origin;wa.TRUSTED_PROXIES=self.old_proxies
  self.environment.stop();self.network.stop();self.tmp.cleanup()
 def token(self):
  token=wa._signed_session(wa._auth());h=Fake(headers={'Cookie':wa.COOKIE+'='+token})
  return token,wa._session(h)['csrfToken']
 def test_expired_post_401_valid_session_bad_csrf_403(self):
  for path in ['/api/admin/settings','/api/admin/logout']:
   h=Fake(path);wa.handle(h,None);self.assertEqual(h.status,401)
  token,csrf=self.token()
  h=Fake(headers={'Cookie':wa.COOKIE+'='+token,'X-CSRF-Token':'wrong'});wa.handle(h,None);self.assertEqual(h.status,403)
  with wa._sessions() as db:db.execute('UPDATE sessions SET expires=0')
  db.close()
  h=Fake(headers={'Cookie':wa.COOKIE+'='+token,'X-CSRF-Token':csrf});wa.handle(h,None);self.assertEqual(h.status,401)
 def test_rate_isolation_and_trusted_header(self):
  a=Fake(headers={'X-Bitget-Proxy-Peer':'198.51.100.1'});b=Fake(headers={'X-Bitget-Proxy-Peer':'198.51.100.2'})
  self.assertTrue(all(not wa._limited(a) for _ in range(10)));self.assertTrue(wa._limited(a));self.assertFalse(wa._limited(b))
  spoof=Fake(headers={'X-Bitget-Client-IP':'198.51.100.99'},peer='203.0.113.5')
  self.assertEqual(wa._client_key(spoof),'203.0.113.5')
  missing=Fake(headers={'X-Bitget-Proxy-Peer':'bad-header'});self.assertEqual(wa._client_key(missing),'127.0.0.1')
 def test_success_login_clears_own_bucket_but_global_remains(self):
  h=Fake('/api/admin/login',body={'password':self.password})
  self.assertTrue(all(not wa._limited(h) for _ in range(9)))
  wa.handle(h,None);self.assertEqual(h.status,200)
  self.assertNotIn(wa._client_key(h),wa._RATE);self.assertEqual(len(wa._GLOBAL_RATE),10)
  self.assertIn('Secure',h.out['Set-Cookie']);self.assertIn('HttpOnly',h.out['Set-Cookie'])
 def test_proxy_origin_and_cloudflare_visitor_trust(self):
  direct=Fake(headers={'X-Bitget-Proxy-Peer':'198.51.100.5','X-Bitget-Client-IP':'203.0.113.66','X-Real-IP':'104.16.0.1'})
  self.assertEqual(wa._client_key(direct),'198.51.100.5')
  external=Fake(peer='198.51.100.8',headers={'X-Bitget-Proxy-Peer':'104.16.0.1','X-Bitget-Client-IP':'203.0.113.66','X-Real-IP':'203.0.113.66'})
  self.assertEqual(wa._client_key(external),'198.51.100.8')
  cloudflare=Fake(headers={'X-Bitget-Proxy-Peer':'104.16.0.1','X-Bitget-Client-IP':'203.0.113.66','X-Real-IP':'1.1.1.1'})
  self.assertEqual(wa._client_key(cloudflare),'203.0.113.66')
  ipv6=Fake(headers={'X-Bitget-Proxy-Peer':'2606:4700::1234','X-Bitget-Client-IP':'2001:db8::5'})
  self.assertEqual(wa._client_key(ipv6),'2001:db8::5')
  malformed=Fake(headers={'X-Bitget-Proxy-Peer':'104.16.0.1','X-Bitget-Client-IP':'203.0.113.5, 1.1.1.1'})
  self.assertEqual(wa._client_key(malformed),'104.16.0.1')
 def test_parallel_updates_have_one_winner(self):
  from concurrent.futures import ThreadPoolExecutor
  import threading
  gate=threading.Barrier(2)
  def change(value):
   gate.wait()
   try:ns.save({'revision':0,'channels':{'bark':{'group':value}}});return 'saved'
   except ns.RevisionConflict:return 'conflict'
  with ThreadPoolExecutor(2) as pool:
   a=pool.submit(change,'first');b=pool.submit(change,'second');results=[a.result(),b.result()]
  self.assertEqual(sorted(results),['conflict','saved']);self.assertEqual(ns.load()['revision'],1)
 def test_global_limit_and_hash_concurrency(self):
  for i in range(60):
   self.assertFalse(wa._limited(Fake(headers={'X-Bitget-Proxy-Peer':'198.51.100.'+str(i+1)})))
  self.assertTrue(wa._limited(Fake(headers={'X-Bitget-Proxy-Peer':'203.0.113.10'})))
  wa._RATE.clear();wa._GLOBAL_RATE.clear()
  self.assertTrue(wa._HASH_SLOTS.acquire(False));self.assertTrue(wa._HASH_SLOTS.acquire(False))
  h=Fake('/api/admin/login',body={'password':self.password})
  try:wa.handle(h,None);self.assertEqual(h.status,429)
  finally:wa._HASH_SLOTS.release();wa._HASH_SLOTS.release()
 def test_noop_save_preserves_revision_and_no_disk_write(self):
  result=ns.save({'revision':0,'channels':{}});self.assertEqual(result['revision'],0);self.assertFalse(ns.SETTINGS.exists())
  result=ns.save({'revision':0,'channels':{'telegram':{'token':''}}});self.assertEqual(result['revision'],0);self.assertFalse(ns.SETTINGS.exists())
  changed=ns.save({'revision':0,'channels':{'bark':{'group':'new_group'}}});self.assertEqual(changed['revision'],1)
  before=ns.SETTINGS.stat().st_mtime_ns
  unchanged=ns.save({'revision':1,'channels':{'bark':{'group':'new_group'}}});self.assertEqual(unchanged['revision'],1);self.assertEqual(before,ns.SETTINGS.stat().st_mtime_ns)
  with self.assertRaises(ns.RevisionConflict):ns.save({'revision':0,'channels':{}})
  self.assertEqual(ns.SETTINGS.stat().st_mode&0o777,0o600)
 def test_discord_integer_and_secret_redaction(self):
  with self.assertRaises(ns.SettingsError):ns.save({'revision':0,'channels':{'discord':{'update_seconds':1.5}}})
  result=ns.save({'revision':0,'channels':{'telegram':{'update_seconds':1.5}}})
  self.assertEqual(result['channels']['telegram']['update_seconds'],1.5)
  raw=json.dumps(result)
  for channel,field in ns.SECRET_FIELDS.items():self.assertNotIn(ns.load()['channels'][channel][field],raw)
 def test_atomic_directory_fsync(self):
  types=[];real=os.fsync
  def record(fd):types.append(stat.S_ISDIR(os.fstat(fd).st_mode));return real(fd)
  with patch.object(ns.os,'fsync',side_effect=record):ns._atomic(self.root/'data/test.json',{'value':1})
  self.assertIn(True,types);self.assertIn(False,types)
 def test_readonly_db_closes_deterministically(self):
  dbfile=self.root/'data/test.sqlite3'
  con=sqlite3.connect(dbfile);con.execute('CREATE TABLE sample(id INTEGER)');con.close()
  with wa._db(dbfile) as current:self.assertEqual(current.execute('SELECT COUNT(*) FROM sample').fetchone()[0],0)
  with self.assertRaises(sqlite3.ProgrammingError):current.execute('SELECT 1')
 def test_private_data_requires_real_login(self):
  path=self.root/'data/positions.sqlite3'
  with closing(sqlite3.connect(path)) as db, db:
   db.executescript('CREATE TABLE position_events(id INTEGER PRIMARY KEY,event_type TEXT,before_json TEXT,after_json TEXT,telegram_sent INTEGER,created_at TEXT);CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT);')
   db.execute('INSERT INTO position_events VALUES(1,?,?,?,?,?)',('opened',None,json.dumps({'symbol':'TESTUSDT','holdSide':'long','total':'1'}),1,'2026-11-02T12:00:00+08:00'))
  db.close();data=SimpleNamespace(config=SimpleNamespace(db_path=path));token,csrf=self.token()
  for endpoint in ['/api/events','/api/notification-status']:
   anonymous=Fake(endpoint,method='GET');wa.handle(anonymous,data);self.assertEqual(anonymous.status,401)
   spoof=Fake(endpoint,method='GET',peer='203.0.113.20',headers={'Cookie':wa.COOKIE+'='+token,'X-Bitget-Proxy-Peer':'104.16.0.1','X-Bitget-Client-IP':'198.51.100.1'})
   wa.handle(spoof,data);self.assertEqual(spoof.status,403)
   authenticated=Fake(endpoint,method='GET',headers={'Cookie':wa.COOKIE+'='+token});wa.handle(authenticated,data);self.assertEqual(authenticated.status,200)
   payload=json.loads(authenticated.wfile.getvalue());self.assertIn('events' if endpoint.endswith('events') else 'channels',payload)
   self.assertNotIn('TESTUSDT',anonymous.wfile.getvalue().decode());self.assertNotIn('TESTUSDT',spoof.wfile.getvalue().decode())
 def test_login_helpers_and_anonymous_handshake(self):
  anonymous=Fake(method='GET');self.assertFalse(wa.is_authenticated(anonymous));self.assertIsNone(anonymous.status)
  self.assertFalse(wa.require_login(anonymous));self.assertEqual(anonymous.status,401)
  token,csrf=self.token();good=Fake(method='GET',headers={'Cookie':wa.COOKIE+'='+token})
  self.assertTrue(wa.is_authenticated(good));self.assertTrue(wa.require_login(good));self.assertIsNone(good.status)
  handshake=Fake('/api/admin/session',method='GET');wa.handle(handshake,None);self.assertEqual(handshake.status,200);self.assertFalse(json.loads(handshake.wfile.getvalue())['authenticated'])
  with patch.object(wa,'_session',side_effect=sqlite3.OperationalError('isolated failure')):
   unavailable=Fake(method='GET');self.assertFalse(wa.is_authenticated(unavailable));self.assertIsNone(unavailable.status)
   self.assertFalse(wa.require_login(unavailable));self.assertEqual(unavailable.status,503)
 def test_origin_and_direct_proxy_spoof_denied(self):
  h=Fake(headers={'Origin':'https://attacker.invalid'});wa.handle(h,None);self.assertEqual(h.status,403)
  h=Fake(peer='203.0.113.10');wa.handle(h,None);self.assertEqual(h.status,403)

if __name__=='__main__':unittest.main(verbosity=2)
