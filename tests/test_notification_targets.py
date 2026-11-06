"""A changed destination must not inherit any live or event message identifier."""
from contextlib import closing, ExitStack
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

import notification_settings as settings


class TargetIsolationTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.stack.enter_context(patch.dict(os.environ, {}, clear=True))
        self.stack.enter_context(patch('urllib.request.urlopen', side_effect=AssertionError('Network forbidden')))
        for field, value in {'PROJECT': self.root, 'SETTINGS': self.root/'settings.json',
                             'APPLIED': self.root/'applied.json', 'RUNTIME': self.root/'runtime.json',
                             '_CURRENT': None}.items():
            self.stack.enter_context(patch.object(settings, field, value))
        self.path = self.root/'state.sqlite3'
        with closing(sqlite3.connect(self.path)) as db, db:
            db.executescript('''CREATE TABLE position_events(id INTEGER, event_type TEXT, telegram_sent INTEGER);
                CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT);
                CREATE TABLE trade_message_delivery(channel TEXT,trade_key TEXT,message_id TEXT,
                    PRIMARY KEY(channel,trade_key));
                INSERT INTO position_events VALUES(1,'closed',0);''')
            for channel in ('telegram', 'discord'):
                for suffix in ('', ':closed', ':aggregate', ':opened'):
                    db.execute('INSERT INTO trade_message_delivery VALUES(?,?,?)',
                               (channel+suffix, 'fixture-lifetime', channel+suffix+'-id'))
        settings.save({'revision': 0, 'channels': {
            'telegram': {'enabled': True, 'token': '123456789:'+('a'*35), 'chat_id': '-10012345'},
            'discord': {'enabled': True, 'webhook': 'https://discord.com/api/webhooks/123456789/'+('b'*60)},
        }})
        settings.apply_environment()
        settings.prepare_runtime(self.path)

    def check_target_change(self, channel, changes):
        other = 'discord' if channel == 'telegram' else 'telegram'
        settings.save({'revision': 1, 'channels': {channel: changes}})
        settings.apply_environment()
        settings.prepare_runtime(self.path)
        with closing(sqlite3.connect(self.path)) as db:
            rows = dict(db.execute('SELECT message_id,channel FROM trade_message_delivery'))
            self.assertEqual(len(rows), 8)
            for suffix in ('', ':closed', ':aggregate', ':opened'):
                self.assertTrue(rows[channel+suffix+'-id'].startswith(channel+suffix+':archived:'), suffix)
                self.assertEqual(rows[other+suffix+'-id'], other+suffix)
        settings.prepare_runtime(self.path)
        with closing(sqlite3.connect(self.path)) as db:
            self.assertEqual(dict(db.execute('SELECT message_id,channel FROM trade_message_delivery')), rows)

    def test_telegram_target_change_archives_aggregate_and_recovered_open(self):
        self.check_target_change('telegram', {'chat_id': '-10098765'})

    def test_discord_target_change_archives_aggregate_and_recovered_open(self):
        self.check_target_change('discord', {'webhook': 'https://discord.com/api/webhooks/987654321/'+('c'*60)})


if __name__ == '__main__':
    unittest.main()
