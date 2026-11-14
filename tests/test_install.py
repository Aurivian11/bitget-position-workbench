"""Fresh-install checks with temporary credentials and no external requests."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class InstallTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="bitget-install-test-")
        self.project = Path(self.temp.name)
        (self.project / "config").mkdir()
        example = (ROOT / "config/.env.example").read_text()
        for key in ("BITGET_API_KEY", "BITGET_API_SECRET", "BITGET_API_PASSPHRASE"):
            example = example.replace(key + "=\n", key + "=fixture-value\n")
        (self.project / "config/.env").write_text(example)
        self.env = {key: value for key, value in os.environ.items()
                    if not key.startswith(("BITGET_", "TELEGRAM_", "DISCORD_", "BARK_", "WEB_", "DATABASE_"))}
        self.env.update(BITGET_PROJECT_ROOT=str(self.project),
                        PYTHONDONTWRITEBYTECODE="1", PYTHONPATH=str(ROOT / "app"))

    def tearDown(self):
        self.temp.cleanup()

    def run_python(self, code, check=True):
        result = subprocess.run([sys.executable, "-B", "-c", code], env=self.env,
                                capture_output=True, text=True, timeout=20)
        if check:
            self.assertEqual(result.returncode, 0, result.stderr)
        return result

    def initialize(self):
        return subprocess.run([sys.executable, "-B", str(ROOT / "tools/init_admin.py"), "--generate"],
                              env=self.env, capture_output=True, text=True, timeout=20)

    def set_origin(self, value):
        path = self.project / "config/.env"
        lines = [line for line in path.read_text().splitlines()
                 if not line.startswith("BITGET_WEB_ORIGIN=")]
        if value is not None:
            lines.append("BITGET_WEB_ORIGIN=" + value)
        path.write_text("\n".join(lines) + "\n")

    def test_fresh_configuration_paths_loopback_and_disabled_notifications(self):
        self.run_python("""
import urllib.request
def forbidden(*a,**k): raise AssertionError('External network forbidden')
urllib.request.urlopen=forbidden
import bot,web_app,notification_settings as settings
config=bot.Config.from_env()
assert config.db_path==settings.PROJECT/'data/positions.sqlite3'
assert web_app.WEB_HOST=='127.0.0.1'
assert not any(settings.enabled(x) for x in ('telegram','discord','bark'))
""")

    def test_generated_password_private_files_and_repeat_refused(self):
        result = self.initialize()
        self.assertEqual(result.returncode, 0, result.stderr)
        access = self.project / "config/web-admin-access.txt"
        auth = self.project / "config/web-admin.json"
        value = json.loads(access.read_text())
        self.assertNotIn(value["password"], result.stdout + result.stderr)
        self.assertGreaterEqual(len(value["password"]), 32)
        self.assertEqual(access.stat().st_mode & 0o777, 0o600)
        self.assertEqual(auth.stat().st_mode & 0o777, 0o600)
        before = auth.read_bytes()
        self.assertNotEqual(self.initialize().returncode, 0)
        self.assertEqual(auth.read_bytes(), before)

    def test_missing_origin_fails_closed(self):
        self.set_origin(None)
        result = self.run_python("import web_admin", check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("BITGET_WEB_ORIGIN", result.stderr)

    def test_invalid_origin_and_port_rejected(self):
        for value in ("http://monitor.example.com", "https://monitor.example.com/path",
                      "https://monitor.example.com:0", "https://monitor.example.com:65536",
                      "https://monitor.example.com:invalid"):
            with self.subTest(origin=value):
                self.set_origin(value)
                self.assertNotEqual(self.run_python("import web_admin", check=False).returncode, 0)

    def test_default_https_port_canonical_and_custom_port_checked(self):
        self.set_origin("https://MONITOR.example.com:443/")
        self.run_python("import web_admin as w; assert w.ORIGIN=='https://monitor.example.com'")
        self.set_origin("https://monitor.example.com:8443")
        self.run_python("""
from types import SimpleNamespace
import web_admin as w
h=SimpleNamespace(client_address=('127.0.0.1',123),headers={'Host':'monitor.example.com:8443','X-Forwarded-Proto':'https'})
assert w._secure(h)
h.headers['Host']='monitor.example.com'
assert not w._secure(h)
h.headers['Host']='monitor.example.com:8443'
h.client_address=('198.51.100.20',123)
assert not w._secure(h)
""")

    def test_bootstrap_handler_private_html_apis_and_health(self):
        result = self.initialize()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.run_python("""
import http.client,threading,urllib.request
from types import SimpleNamespace
def forbidden(*a,**k): raise AssertionError('External network forbidden')
urllib.request.urlopen=forbidden
import bot,web_app as app,web_admin as admin
store=bot.Store(app.CONFIG.db_path);store.db.close()
class FakeData:
 config=SimpleNamespace(db_path=app.CONFIG.db_path)
 def payload(self): return {'fixture':'private-account'}
 def positions_payload(self): return {'fixture':'private-positions'}
app.DATA=FakeData()
app.Handler.log_message=lambda *a:None
server=app.ThreadingHTTPServer(('127.0.0.1',0),app.Handler)
worker=threading.Thread(target=server.serve_forever,daemon=True);worker.start()
def request(path,proxy=True,cookie=None):
 conn=http.client.HTTPConnection('127.0.0.1',server.server_port,timeout=5)
 headers={'Host':'monitor.example.com','X-Forwarded-Proto':'https'} if proxy else {}
 if cookie:headers['Cookie']=cookie
 conn.request('GET',path,headers=headers);r=conn.getresponse();result=(r.status,r.read());conn.close();return result
paths=['/api/dashboard','/api/positions/live','/api/events','/api/notification-status','/api/admin/settings']
try:
 status,body=request('/');assert status==200 and b'assets/login.js' in body and b'assets/app.js' not in body
 assert all(request(path)[0]==401 for path in paths)
 assert all(request(path,proxy=False)[0]==403 for path in paths)
 assert request('/healthz',proxy=False)==(200,b'{"ok":true}')
 token=admin._signed_session(admin._auth());cookie=admin.COOKIE+'='+token
 assert all(request(path,cookie=cookie)[0]==200 for path in paths)
 status,body=request('/',cookie=cookie);assert status==200 and b'assets/app.js' in body
finally:
 server.shutdown();server.server_close();worker.join(5)
""")


if __name__ == "__main__":
    unittest.main()
