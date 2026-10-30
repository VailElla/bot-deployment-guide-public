"""Limit QQ private incoming messages before the normal handler pipeline."""
import hashlib
import json
import sqlite3
import time
from contextlib import closing
from pathlib import Path
from astrbot.api.platform import MessageType

class Limiter:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute('CREATE TABLE IF NOT EXISTS attempts (scope TEXT NOT NULL, at REAL NOT NULL)')
            db.execute('CREATE INDEX IF NOT EXISTS attempts_scope_at ON attempts(scope, at)')

    def admit(self, event, now=None):
        if event.get_platform_name() != 'aiocqhttp' or event.message_obj.type != MessageType.FRIEND_MESSAGE:
            return True
        sender, bot = str(event.get_sender_id() or ''), str(event.get_self_id() or '')
        if not sender or not bot or sender == bot:
            return True
        scope = hashlib.sha256(json.dumps([str(event.get_platform_id()), bot, sender]).encode()).hexdigest()
        now = time.time() if now is None else now
        with closing(sqlite3.connect(self.path, timeout=5)) as db, db:
            db.execute('BEGIN IMMEDIATE')
            db.execute('DELETE FROM attempts WHERE at <= ? OR at > ?', (now - 600, now))
            count = db.execute('SELECT COUNT(*) FROM attempts WHERE scope = ?', (scope,)).fetchone()[0]
            if count >= 30:
                return False
            db.execute('INSERT INTO attempts VALUES (?, ?)', (scope, now))
        return True
