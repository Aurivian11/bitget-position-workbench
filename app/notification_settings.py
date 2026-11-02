"""Validated notification settings, secret-safe projection and monitor reload."""
import copy
from contextlib import closing
import hashlib
import json
import os
import re
import sqlite3
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit

PROJECT = Path(os.getenv('BITGET_PROJECT_ROOT', str(Path(__file__).resolve().parents[1]))).resolve()
SETTINGS = PROJECT / 'data/web-notification-settings.json'
APPLIED = PROJECT / 'data/web-notification-applied.json'
RUNTIME = PROJECT / 'data/web-notification-runtime.json'
_LOCK = threading.RLock()
_CURRENT = None
_LAST_APPLIED_WRITE = 0.0
EVENTS = ('increased', 'reduced', 'closed')
SECRET_FIELDS = {'telegram': 'token', 'discord': 'webhook', 'bark': 'key'}

class SettingsError(ValueError): pass
class RevisionConflict(SettingsError): pass

def _atomic(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name('.' + path.name + '.' + str(os.getpid()) + '.tmp')
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, 'w') as f:
            json.dump(value, f, ensure_ascii=False, separators=(',', ':'))
            f.flush(); os.fsync(f.fileno())
        os.replace(temp, path)
        os.chmod(path, 0o600)
        directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try: os.fsync(directory_fd)
        finally: os.close(directory_fd)
    finally:
        if temp.exists(): temp.unlink()

def base_env():
    result = dict(os.environ)
    path = PROJECT / 'config/.env'
    if path.exists():
        for raw in path.read_text().splitlines():
            line = raw.strip()
            if not line or line.startswith('#') or '=' not in line: continue
            k, v = line.split('=', 1); k, v = k.strip(), v.strip()
            if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'": v = v[1:-1]
            result[k] = v
    return result

def _bool(value): return str(value).lower() in {'1', 'true', 'yes'}

def defaults():
    e = base_env()
    return {'revision': 0, 'updatedAt': None, 'channels': {
        'telegram': {'enabled': bool(e.get('TELEGRAM_BOT_TOKEN') and e.get('TELEGRAM_CHAT_ID')), 'token': e.get('TELEGRAM_BOT_TOKEN',''), 'chat_id': e.get('TELEGRAM_CHAT_ID',''), 'board_enabled': True, 'events': list(EVENTS), 'update_seconds': float(e.get('TELEGRAM_UPDATE_SECONDS','1'))},
        'discord': {'enabled': bool(e.get('DISCORD_WEBHOOK_URL')), 'webhook': e.get('DISCORD_WEBHOOK_URL',''), 'board_enabled': True, 'events': list(EVENTS), 'update_seconds': int(e.get('DISCORD_UPDATE_SECONDS','10'))},
        'bark': {'enabled': bool(e.get('BARK_KEY')), 'base': e.get('BARK_BASE','http://127.0.0.1:8080'), 'key': e.get('BARK_KEY',''), 'sound': e.get('BARK_SOUND','123'), 'call': _bool(e.get('BARK_CALL','1')), 'group': e.get('BARK_GROUP','bitget-orders'), 'reconcile_seconds': float(e.get('BARK_RECONCILE_SECONDS','30'))},
    }}

def load():
    result = defaults()
    if SETTINGS.exists():
        saved = json.loads(SETTINGS.read_text())
        result['revision'] = int(saved.get('revision',0))
        result['updatedAt'] = saved.get('updatedAt')
        for channel in result['channels']:
            result['channels'][channel].update(saved.get('channels',{}).get(channel,{}))
    return result

def safe_view(document=None):
    result = copy.deepcopy(document if document is not None else load())
    for channel, secret in SECRET_FIELDS.items():
        result['channels'][channel][secret + '_configured'] = bool(result['channels'][channel].pop(secret, ''))
    applied = {}
    try: applied = json.loads(APPLIED.read_text())
    except (OSError, ValueError): pass
    result['appliedRevision'] = applied.get('revision')
    result['appliedAt'] = applied.get('appliedAt')
    result['monitorHeartbeatAt'] = applied.get('heartbeatAt')
    result['applying'] = result['revision'] != applied.get('revision')
    result['eventOptions'] = list(EVENTS)
    result['semantics'] = '看板展示开仓和参数变更；事件选项控制加仓、减仓、平仓消息。关闭通道期间历史事件不补发。Bark仅推送开仓成交（含加仓成交）。'
    return result

def _text(value, name, maximum=256):
    if not isinstance(value,str) or len(value)>maximum or any(ord(x)<32 or ord(x)==127 for x in value): raise SettingsError(name + '格式无效')
    return value.strip()

def _number(value, name, minimum, maximum):
    if isinstance(value,bool) or not isinstance(value,(int,float)) or not minimum <= value <= maximum: raise SettingsError(name + '超出允许范围')
    return value

def _validate(channels):
    for name, c in channels.items():
        if type(c['enabled']) is not bool: raise SettingsError('启用状态必须是布尔值')
        if name in {'telegram','discord'}:
            if type(c['board_enabled']) is not bool: raise SettingsError('看板状态必须是布尔值')
            if not isinstance(c['events'],list) or any(x not in EVENTS for x in c['events']) or len(c['events']) != len(set(c['events'])): raise SettingsError('通知事件类型无效')
            _number(c['update_seconds'],'看板刷新间隔',1,3600)
        if name=='telegram':
            if c['token'] and not re.fullmatch(r'\d{5,20}:[A-Za-z0-9_-]{20,120}', c['token']): raise SettingsError('Telegram Bot Token 格式无效')
            if c['chat_id'] and not re.fullmatch(r'-?\d{1,22}|@[A-Za-z][A-Za-z0-9_]{3,63}', c['chat_id']): raise SettingsError('Telegram Chat ID 格式无效')
            if c['enabled'] and not (c['token'] and c['chat_id']): raise SettingsError('启用 Telegram 前请填写 Token 和 Chat ID')
        if name=='discord':
            if not float(c['update_seconds']).is_integer(): raise SettingsError('Discord 刷新间隔必须是整数秒')
            if c['webhook']:
                u=urlsplit(c['webhook'])
                if u.scheme!='https' or u.hostname not in {'discord.com','discordapp.com','canary.discord.com','ptb.discord.com'} or u.port not in (None,443) or u.username or u.password or u.query or u.fragment or not re.fullmatch(r'/api(?:/v\d+)?/webhooks/\d{5,25}/[A-Za-z0-9._-]{20,200}',u.path): raise SettingsError('Discord Webhook 地址格式无效')
            if c['enabled'] and not c['webhook']: raise SettingsError('启用 Discord 前请填写 Webhook')
        if name=='bark':
            u=urlsplit(c['base']); local=u.hostname in {'127.0.0.1','localhost','::1'} and u.port==8080
            if not u.hostname or u.username or u.password or u.query or u.fragment or (u.path not in {'','/'}) or not (u.scheme=='https' or (u.scheme=='http' and local)): raise SettingsError('Bark 地址须为 HTTPS 根地址，或本机 http://127.0.0.1:8080')
            if c['key'] and not re.fullmatch(r'[A-Za-z0-9_-]{5,200}',c['key']): raise SettingsError('Bark Key 格式无效')
            if c['enabled'] and not c['key']: raise SettingsError('启用 Bark 前请填写设备 Key')
            if type(c['call']) is not bool: raise SettingsError('Bark 持续响铃开关无效')
            _number(c['reconcile_seconds'],'Bark 成交核对间隔',5,3600)
            if not re.fullmatch(r'[A-Za-z0-9_.-]{0,64}',c['sound']): raise SettingsError('Bark 提示音名称格式无效')

def save(payload):
    if not isinstance(payload,dict) or set(payload)-{'revision','channels'}: raise SettingsError('配置字段无效')
    with _LOCK:
        current=load()
        original_channels=copy.deepcopy(current["channels"])
        if type(payload.get('revision')) is not int or payload['revision'] != current['revision']: raise RevisionConflict('配置已被其他页面修改，请重新载入后再保存')
        changes=payload.get('channels')
        if not isinstance(changes,dict) or set(changes)-set(current['channels']): raise SettingsError('通知通道无效')
        for name, patch in changes.items():
            c=current['channels'][name]
            if not isinstance(patch,dict) or set(patch)-set(c): raise SettingsError('通知配置包含不支持的字段')
            for field,value in patch.items():
                if isinstance(c[field],str): value=_text(value,field,2048 if field=='webhook' else 256)
                if field==SECRET_FIELDS[name] and value=='': continue
                c[field]=value
        try: _validate(current['channels'])
        except (TypeError,ValueError) as error:
            if isinstance(error,SettingsError): raise
            raise SettingsError('配置格式无效') from None
        if current['channels'] == original_channels:
            return safe_view(current)
        current['revision']+=1; current['updatedAt']=time.time()
        _atomic(SETTINGS,current)
        return safe_view(current)

def apply_environment():
    global _CURRENT
    _CURRENT=load(); c=_CURRENT['channels']
    mapping={'TELEGRAM_BOT_TOKEN':c['telegram']['token'],'TELEGRAM_CHAT_ID':c['telegram']['chat_id'],'TELEGRAM_UPDATE_SECONDS':c['telegram']['update_seconds'],'DISCORD_WEBHOOK_URL':c['discord']['webhook'],'DISCORD_UPDATE_SECONDS':int(c['discord']['update_seconds']),'BARK_BASE':c['bark']['base'],'BARK_KEY':c['bark']['key'],'BARK_SOUND':c['bark']['sound'],'BARK_CALL':'1' if c['bark']['call'] else '0','BARK_GROUP':c['bark']['group'],'BARK_RECONCILE_SECONDS':c['bark']['reconcile_seconds']}
    os.environ.update({k:str(v) for k,v in mapping.items()})
    return _CURRENT['revision']

def options(channel): return (_CURRENT or load())['channels'][channel]
def enabled(channel): return bool(options(channel)['enabled'])
def board_enabled(channel): return enabled(channel) and bool(options(channel).get('board_enabled',False))
def event_enabled(channel,event): return enabled(channel) and event in options(channel).get('events',[])

def _fingerprint(channel, c):
    raw = (c['token']+'\0'+c['chat_id']) if channel=='telegram' else c['webhook'] if channel=='discord' else c['base']+'\0'+c['key']
    return hashlib.sha256(raw.encode()).hexdigest()

def prepare_runtime(db_path):
    """Archive old target message IDs; resume only new events after disabled periods."""
    current=_CURRENT or load(); previous={}
    try: previous=json.loads(RUNTIME.read_text())
    except (OSError,ValueError): pass
    state={'channels':{}}
    with closing(sqlite3.connect(db_path,timeout=10)) as db, db:
        tables={r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        saved=db.execute("SELECT value FROM metadata WHERE key='web_notification_runtime'").fetchone()
        if saved: previous=json.loads(saved[0])
        state['revision']=current['revision']
        state['updatedAt']=time.time()
        high=db.execute('SELECT COALESCE(MAX(id),0) FROM position_events').fetchone()[0]
        for name,c in current['channels'].items():
            fingerprint=_fingerprint(name,c); old=previous.get('channels',{}).get(name)
            changed=old is not None and old.get('fingerprint')!=fingerprint
            reset=changed or (old is not None and not old.get('enabled') and c['enabled'])
            state['channels'][name]={'fingerprint':fingerprint,'enabled':c['enabled'],'transitionAt':time.time() if reset else (old or {}).get('transitionAt')}
            if name in {'telegram','discord'} and changed:
                if 'trade_message_delivery' in tables:
                    suffix=':archived:'+str(time.time_ns())
                    db.execute('UPDATE trade_message_delivery SET channel=channel||? WHERE channel IN (?,?)',(suffix,name,name+':closed'))
                keys=('pnl_message_parts','pnl_message_id') if name=='telegram' else ('discord_pnl_message_ids',)
                for key in keys: db.execute('DELETE FROM metadata WHERE key=?',(key,))
            if name=='telegram' and (reset or not c['enabled']):
                db.execute('UPDATE position_events SET telegram_sent=2 WHERE telegram_sent=0 AND id<=?',(high,))
            if name=='discord' and (reset or not c['enabled']):
                db.execute("INSERT INTO metadata(key,value) VALUES('discord_start_event_id',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",(str(high),))
                if 'discord_notices' in tables: db.execute('UPDATE discord_notices SET complete=2 WHERE complete=0')
            if name=='bark' and reset:
                bark_path=Path(db_path).parent/'bark_orders.sqlite3'
                if bark_path.exists():
                    with closing(sqlite3.connect(bark_path,timeout=10)) as bark, bark:
                        marker='web_config_reset_revision'
                        applied=bark.execute('SELECT value FROM bark_metadata WHERE key=?',(marker,)).fetchone()
                        if applied is None or applied[0]!=str(current['revision']):
                            bark.execute("UPDATE bark_open_orders SET state='ignored' WHERE state='pending'")
                            bark.execute("UPDATE bark_metadata SET value=? WHERE key LIKE '%:baseline_ms'",(str(int(time.time()*1000)),))
                            bark.execute('INSERT INTO bark_metadata(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',(marker,str(current['revision'])))
        db.execute("INSERT INTO metadata(key,value) VALUES('web_notification_runtime',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",(json.dumps(state),))
    _atomic(RUNTIME,state)

def mark_applied():
    global _LAST_APPLIED_WRITE
    monotonic=time.monotonic()
    if _LAST_APPLIED_WRITE and monotonic-_LAST_APPLIED_WRITE<5:
        return
    revision=(_CURRENT or load())['revision']; now=time.time()
    try: previous=json.loads(APPLIED.read_text())
    except (OSError,ValueError): previous={}
    _atomic(APPLIED,{'revision':revision,'appliedAt':previous.get('appliedAt') if previous.get('revision')==revision else now,'heartbeatAt':now,'pid':os.getpid()})
    _LAST_APPLIED_WRITE=monotonic

def watch_changes(on_change):
    revision=(_CURRENT or load())['revision']
    def watch():
        while True:
            time.sleep(1)
            try: changed=load()['revision']!=revision
            except (OSError,ValueError): continue
            if changed:
                on_change(); return
    threading.Thread(target=watch,name='notification-settings-watch',daemon=True).start()
