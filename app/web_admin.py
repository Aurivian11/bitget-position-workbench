"""HTTPS-only notification administration and sanitized observation endpoints."""
import base64
import hashlib
import hmac
import ipaddress
from contextlib import contextmanager
import json
import logging
import os
import secrets
import sqlite3
import threading
import time
from http.cookies import SimpleCookie, CookieError
from urllib.parse import urlparse, urlsplit
import notification_settings as settings

_WEB_ENV=settings.base_env()
ORIGIN=_WEB_ENV.get('BITGET_WEB_ORIGIN','').strip().rstrip('/')
_ORIGIN_PARTS=urlsplit(ORIGIN)
if (_ORIGIN_PARTS.scheme!='https' or not _ORIGIN_PARTS.hostname or
        _ORIGIN_PARTS.username or _ORIGIN_PARTS.password or _ORIGIN_PARTS.path or
        _ORIGIN_PARTS.query or _ORIGIN_PARTS.fragment):
    raise ValueError('Set BITGET_WEB_ORIGIN to your HTTPS origin, for example https://monitor.example.com')
try:
    _origin_port=_ORIGIN_PARTS.port
    if _origin_port is not None and not 1 <= _origin_port <= 65535:
        raise ValueError('port out of range')
except ValueError:
    raise ValueError('BITGET_WEB_ORIGIN has an invalid HTTPS port') from None
_origin_host=_ORIGIN_PARTS.hostname.encode('idna').decode('ascii').lower()
_origin_host=('['+_origin_host+']') if ':' in _origin_host else _origin_host
ORIGIN='https://'+_origin_host+(':'+str(_origin_port) if _origin_port not in (None,443) else '')
_ORIGIN_PARTS=urlsplit(ORIGIN)
try:
    TRUSTED_PROXIES=frozenset(str(ipaddress.ip_address(v.strip())) for v in
        _WEB_ENV.get('BITGET_WEB_TRUSTED_PROXIES','127.0.0.1,::1').split(',') if v.strip())
except ValueError:
    raise ValueError('BITGET_WEB_TRUSTED_PROXIES accepts exact IP addresses only') from None
if not TRUSTED_PROXIES:
    raise ValueError('Configure at least one trusted HTTPS reverse proxy')
COOKIE='bitget_admin'
AUTH_PATH=settings.PROJECT/'config/web-admin.json'
_RATE={}
_RATE_LOCK=threading.Lock()
_GLOBAL_RATE=[]
_HASH_SLOTS=threading.BoundedSemaphore(2)
LOG=logging.getLogger('bitget-web-admin')
# Cloudflare official ips-v4/ips-v6 checked 2026-11-02; fail closed for unknown proxies.
_CF_NETWORKS=tuple(ipaddress.ip_network(value) for value in '173.245.48.0/20 103.21.244.0/22 103.22.200.0/22 103.31.4.0/22 141.101.64.0/18 108.162.192.0/18 190.93.240.0/20 188.114.96.0/20 197.234.240.0/22 198.41.128.0/17 162.158.0.0/15 104.16.0.0/13 104.24.0.0/14 172.64.0.0/13 131.0.72.0/22 2400:cb00::/32 2606:4700::/32 2803:f800::/32 2405:b500::/32 2405:8100::/32 2a06:98c0::/29 2c0f:f248::/32'.split())

def initialize_auth(password):
    if not isinstance(password,str) or not 16<=len(password)<=256: raise ValueError('管理密码须为16至256位')
    if AUTH_PATH.exists(): raise ValueError('管理凭据已经存在')
    salt=secrets.token_hex(24)
    settings._atomic(AUTH_PATH,{'salt':salt,'iterations':310000,'password_hash':hashlib.pbkdf2_hmac('sha256',password.encode(),bytes.fromhex(salt),310000).hex(),'signing_key':secrets.token_hex(32)})

def _auth(): return json.loads(AUTH_PATH.read_text())
def _b64(raw): return base64.urlsafe_b64encode(raw).decode().rstrip('=')

def _sessions():
    path=settings.PROJECT/'data/web-admin-sessions.sqlite3'
    fd=os.open(path,os.O_WRONLY|os.O_CREAT,0o600);os.close(fd);os.chmod(path,0o600)
    db=sqlite3.connect(path,timeout=5)
    db.execute('CREATE TABLE IF NOT EXISTS sessions(id_hash TEXT PRIMARY KEY,expires INTEGER NOT NULL)')
    return db

def _session_id(value):return hashlib.sha256(value.encode()).hexdigest()


def _signed_session(auth):
    document={'sid':secrets.token_urlsafe(24),'exp':int(time.time())+28800}
    with _sessions() as db:
        db.execute('DELETE FROM sessions WHERE expires<=?',(int(time.time()),))
        db.execute('INSERT INTO sessions(id_hash,expires) VALUES(?,?)',(_session_id(document['sid']),document['exp']))
    db.close()
    payload=_b64(json.dumps(document,separators=(',',':')).encode())
    signature=hmac.new(bytes.fromhex(auth['signing_key']),payload.encode(),hashlib.sha256).hexdigest()
    return payload+'.'+signature

def _session(h):
    try:
        cookies=SimpleCookie(); cookies.load(h.headers.get('Cookie',''))
        if COOKIE not in cookies: return None
        token=cookies[COOKIE].value
        if len(token)>1024: return None
        payload,signature=token.split('.',1); auth=_auth()
        expected=hmac.new(bytes.fromhex(auth['signing_key']),payload.encode(),hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature,expected): return None
        document=json.loads(base64.urlsafe_b64decode(payload+'='*(-len(payload)%4)))
        if document['exp']<=time.time(): return None
        with _sessions() as db:
            active=db.execute('SELECT 1 FROM sessions WHERE id_hash=? AND expires>?',(_session_id(document['sid']),int(time.time()))).fetchone()
        db.close()
        if not active:return None
        csrf=hmac.new(bytes.fromhex(auth['signing_key']),('csrf:'+document['sid']).encode(),hashlib.sha256).hexdigest()
        return {'authenticated':True,'csrfToken':csrf,'expiresAt':document['exp'],'_idHash':_session_id(document['sid'])}
    except (OSError,ValueError,KeyError,TypeError,CookieError): return None

def _secure(h):
    peer=h.client_address[0]
    return peer in TRUSTED_PROXIES and h.headers.get('X-Forwarded-Proto')=='https' and h.headers.get('Host')==_ORIGIN_PARTS.netloc

def _reply(h,status,value,cookie=None):
    body=json.dumps(value,ensure_ascii=False,allow_nan=False).encode()
    h.send_response(status); h.security_headers()
    h.send_header('Content-Type','application/json; charset=utf-8')
    h.send_header('Cache-Control','no-store'); h.send_header('Content-Length',str(len(body)))
    if cookie: h.send_header('Set-Cookie',cookie)
    h.end_headers(); h.wfile.write(body)

def _body(h):
    if h.headers.get('Transfer-Encoding'): raise settings.SettingsError('不支持此请求格式')
    if h.headers.get('Content-Type','').split(';')[0].strip()!='application/json': raise settings.SettingsError('请使用 JSON 请求')
    try: size=int(h.headers.get('Content-Length','0'))
    except ValueError: raise settings.SettingsError('请求长度无效') from None
    if size<=0 or size>16384: raise settings.SettingsError('请求内容过大或为空')
    value=json.loads(h.rfile.read(size))
    if not isinstance(value,dict): raise settings.SettingsError('请求格式无效')
    return value

def _ip(value):
    try:
        address=ipaddress.ip_address(value.strip())
        return getattr(address,'ipv4_mapped',None) or address
    except (ValueError,AttributeError): return None


def _client_key(h):
    peer=h.client_address[0]
    if peer not in TRUSTED_PROXIES: return str(_ip(peer) or peer)
    # Both headers MUST be overwritten by the trusted /bitget/ reverse-proxy location.
    # $realip_remote_addr preserves the original peer despite inherited realip rewriting.
    original=_ip(h.headers.get('X-Bitget-Proxy-Peer',''))
    if original is None: return peer
    if any(original.version==network.version and original in network for network in _CF_NETWORKS):
        visitor=_ip(h.headers.get('X-Bitget-Client-IP',''))
        if visitor is not None: return str(visitor)
    return str(original)


def _limited(h):
    peer=_client_key(h); now=time.monotonic()
    with _RATE_LOCK:
        for key in list(_RATE):
            _RATE[key]=[t for t in _RATE[key] if now-t<300]
            if not _RATE[key]: del _RATE[key]
        _GLOBAL_RATE[:]=[t for t in _GLOBAL_RATE if now-t<60]
        timestamps=_RATE.get(peer,[])
        if len(timestamps)>=10 or len(_GLOBAL_RATE)>=60: return True
        if peer not in _RATE and len(_RATE)>=2048: return True
        timestamps.append(now); _RATE[peer]=timestamps; _GLOBAL_RATE.append(now)
        return False


def _login_succeeded(h):
    with _RATE_LOCK: _RATE.pop(_client_key(h),None)


@contextmanager
def _db(path):
    db=sqlite3.connect('file:'+str(path)+'?mode=ro',uri=True,timeout=5)
    db.row_factory=sqlite3.Row
    try: yield db
    finally: db.close()


def events(db_path):
    with _db(db_path) as db:
        rows=db.execute('SELECT id,event_type,before_json,after_json,telegram_sent,created_at FROM position_events ORDER BY id DESC LIMIT 100').fetchall()
    result=[]
    for row in rows:
        raw=json.loads(row['after_json'] or row['before_json'] or '{}')
        result.append({'id':row['id'],'type':row['event_type'],'symbol':str(raw.get('symbol','—')),'side':str(raw.get('holdSide','')),'size':str(raw.get('total','—')),'createdAt':row['created_at'],'telegramState':{0:'pending',1:'processed',2:'skipped'}.get(row['telegram_sent'],'unknown')})
    return {'events':result,'limit':100}

def notification_status(db_path):
    config=settings.safe_view(); channels={}
    for name, c in config['channels'].items():
        secret=settings.SECRET_FIELDS[name]
        channels[name]={'enabled':c['enabled'],'configured':c[secret+'_configured'],'pending':None,'failed':None,'skipped':None,'lastAcceptedAt':None}
    with _db(db_path) as db:
        tables={r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        channels['telegram']['pending']=db.execute('SELECT COUNT(*) FROM position_events WHERE telegram_sent=0 AND event_type IN (\'increased\',\'reduced\',\'closed\')').fetchone()[0]
        channels['telegram']['skipped']=db.execute('SELECT COUNT(*) FROM position_events WHERE telegram_sent=2').fetchone()[0]
        if 'discord_event_delivery' in tables:
            r=db.execute("SELECT value FROM metadata WHERE key='discord_start_event_id'").fetchone(); baseline=int(r[0]) if r else 0
            channels['discord']['pending']=db.execute("SELECT COUNT(*) FROM position_events e LEFT JOIN discord_event_delivery d ON d.event_id=e.id WHERE e.id>? AND COALESCE(d.complete,0)=0 AND event_type IN ('increased','reduced','closed')",(baseline,)).fetchone()[0]
            channels['discord']['skipped']=db.execute('SELECT COUNT(*) FROM discord_event_delivery WHERE complete=2').fetchone()[0]
        if 'trade_message_delivery' in tables:
            for name in ('telegram','discord'):
                channels[name]['uncertain']=db.execute('SELECT COUNT(*) FROM trade_message_delivery WHERE channel IN (?,?) AND sending=1',(name,name+':closed')).fetchone()[0]
    bark=PathLike(db_path).parent/'bark_orders.sqlite3'
    if bark.exists():
        with _db(bark) as db:
            for state,key in [('pending','pending'),('failed','failed'),('ignored','skipped')]:
                channels['bark'][key]=db.execute('SELECT COUNT(*) FROM bark_open_orders WHERE state=?',(state,)).fetchone()[0]
            channels['bark']['lastAcceptedAt']=db.execute('SELECT MAX(sent_at) FROM bark_open_orders WHERE state=\'sent\'').fetchone()[0]
    heartbeat=config.get('monitorHeartbeatAt')
    return {'channels':channels,'revision':config['revision'],'appliedRevision':config['appliedRevision'],'applying':config['applying'],'monitorHeartbeatAt':heartbeat,'monitorHealthy':bool(heartbeat and time.time()-heartbeat<90),'deliveryNote':'待处理来自本机队列；Telegram/Discord 未记录完整失败次数，未知显示为 —。Bark 接受时间仅表示服务器接受，不保证手机已收到。'}

from pathlib import Path as PathLike

def is_authenticated(h):
    """Pure response-wise check for selecting an authenticated HTML view."""
    try: return bool(_secure(h) and _session(h))
    except sqlite3.Error: return False


def require_login(h):
    """Return True when trusted HTTPS plus a live session permits private data."""
    if not _secure(h):
        _reply(h,403,{'error':'请通过 HTTPS 私密入口访问','managementUrl':ORIGIN+'/bitget/'}); return False
    try: session=_session(h)
    except sqlite3.Error:
        _reply(h,503,{'error':'登录验证暂时不可用，请稍后重试'}); return False
    if not session:
        _reply(h,401,{'error':'登录已失效，请重新登录后查看持仓'}); return False
    return True


def handle(h,data,path=None):
    path=path or urlparse(h.path).path
    if path not in {'/api/events','/api/notification-status','/api/admin/session','/api/admin/login','/api/admin/logout','/api/admin/settings'}: return False
    try:
        if path in {'/api/events','/api/notification-status'}:
            if not require_login(h): return True
            if h.command!='GET': _reply(h,405,{'error':'不支持此请求方法'}); return True
            _reply(h,200,events(data.config.db_path) if path=='/api/events' else notification_status(data.config.db_path)); return True
        if not _secure(h): _reply(h,403,{'error':'请通过 HTTPS 管理入口访问','managementUrl':ORIGIN+'/bitget/#settings'}); return True
        session=_session(h)
        if path=='/api/admin/session' and h.command=='GET':
            _reply(h,200,{k:v for k,v in session.items() if not k.startswith('_')} if session else {'authenticated':False,'configured':AUTH_PATH.exists()}); return True
        if h.command=='POST':
            if h.headers.get('Origin')!=ORIGIN: _reply(h,403,{'error':'请求来源无效'}); return True
            if path!='/api/admin/login' and not session:
                _reply(h,401,{'error':'管理会话已失效，请重新登录'}); return True
            if path!='/api/admin/login' and not hmac.compare_digest(h.headers.get('X-CSRF-Token',''),session['csrfToken']):
                _reply(h,403,{'error':'请求验证失败，请刷新后重试'}); return True
        if path=='/api/admin/login' and h.command=='POST':
            if _limited(h): _reply(h,429,{'error':'登录尝试过于频繁，请稍后再试'}); return True
            body=_body(h); password=body.get('password')
            if not isinstance(password,str) or len(password)>256: _reply(h,400,{'error':'密码格式无效'}); return True
            auth=_auth()
            if not _HASH_SLOTS.acquire(blocking=False):
                _reply(h,429,{'error':'管理登录繁忙，请稍后重试'}); return True
            try: digest=hashlib.pbkdf2_hmac('sha256',password.encode(),bytes.fromhex(auth['salt']),auth['iterations']).hex()
            finally: _HASH_SLOTS.release()
            if not hmac.compare_digest(digest,auth['password_hash']): _reply(h,401,{'error':'管理密码不正确'}); return True
            _login_succeeded(h)
            token=_signed_session(auth)
            payload=token.split('.',1)[0]; value=json.loads(base64.urlsafe_b64decode(payload+'='*(-len(payload)%4)))
            csrf=hmac.new(bytes.fromhex(auth['signing_key']),('csrf:'+value['sid']).encode(),hashlib.sha256).hexdigest()
            _reply(h,200,{'authenticated':True,'csrfToken':csrf,'expiresAt':value['exp']},f'{COOKIE}={token}; Path=/bitget/; Max-Age=28800; Secure; HttpOnly; SameSite=Strict'); return True
        if not session: _reply(h,401,{'error':'请先登录通知管理'}); return True
        if path=='/api/admin/logout' and h.command=='POST':
            with _sessions() as db:db.execute('DELETE FROM sessions WHERE id_hash=?',(session['_idHash'],))
            db.close()
            _reply(h,200,{'authenticated':False},f'{COOKIE}=; Path=/bitget/; Max-Age=0; Secure; HttpOnly; SameSite=Strict'); return True
        if path=='/api/admin/settings' and h.command=='GET': _reply(h,200,settings.safe_view()); return True
        if path=='/api/admin/settings' and h.command=='POST': _reply(h,200,settings.save(_body(h))); return True
        _reply(h,405,{'error':'不支持此请求方法'}); return True
    except settings.RevisionConflict as error: _reply(h,409,{'error':str(error)})
    except settings.SettingsError as error: _reply(h,400,{'error':str(error)})
    except (ValueError,UnicodeError): _reply(h,400,{'error':'配置格式无效，请检查填写内容'})
    except (OSError,sqlite3.Error,KeyError,TypeError):
        LOG.warning('Administration request unavailable at %s',path)
        _reply(h,503,{'error':'配置服务暂时不可用，请稍后重试'})
    return True
