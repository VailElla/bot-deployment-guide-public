"""Persistent, conversation-scoped image bytes for QQ message context."""
from __future__ import annotations

import asyncio
import json
import sqlite3
import shutil
from contextlib import closing
from pathlib import Path


class ImageArchive:
    def __init__(self, path):
        self.path = Path(path)

    def _connect(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(str(self.path), timeout=10)
        db.execute('PRAGMA busy_timeout=10000')
        db.execute('PRAGMA journal_mode=WAL')
        db.execute('''CREATE TABLE IF NOT EXISTS images (
            scope TEXT NOT NULL, message_id TEXT NOT NULL, sender_id TEXT NOT NULL,
            created REAL NOT NULL, refs TEXT NOT NULL,
            PRIMARY KEY(scope, message_id))''')
        db.execute('CREATE INDEX IF NOT EXISTS images_sender ON images(scope,sender_id,created DESC)')
        db.execute('CREATE TABLE IF NOT EXISTS resets (scope TEXT NOT NULL, sender_id TEXT NOT NULL, created REAL NOT NULL, PRIMARY KEY(scope,sender_id))')
        return db

    @staticmethod
    def scope(scope):
        return json.dumps(scope, ensure_ascii=True, separators=(',', ':'))

    def _save(self, scope, mid, sender, created, refs):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(refs)
        used = sum(p.stat().st_size for p in (self.path, Path(str(self.path)+'-wal')) if p.exists())
        if used + len(payload) > 10 * 1024**3 or shutil.disk_usage(self.path.parent).free < len(payload) + 1024**3:
            raise OSError('Image archive capacity limit or insufficient disk space')
        with closing(self._connect()) as db:
            with db:
                db.execute('INSERT OR IGNORE INTO images VALUES (?,?,?,?,?)',
                           (self.scope(scope), mid, sender, created, payload))

    async def save(self, scope, mid, sender, created, refs):
        if scope and mid and sender and refs:
            await asyncio.to_thread(self._save, scope, mid, sender, created, list(refs))

    def _read(self, scope, before, mid=None, sender=None):
        with closing(self._connect()) as db:
            if mid:
                query = 'SELECT message_id,sender_id,created,refs FROM images WHERE scope=? AND message_id=? AND created<=?'
                if scope[1] == 'private':
                    query += ' AND created>COALESCE((SELECT created FROM resets WHERE resets.scope=images.scope AND resets.sender_id=images.sender_id),-1)'
                row = db.execute(query, (self.scope(scope), mid, before)).fetchone()
            elif sender:
                row = db.execute('SELECT message_id,sender_id,created,refs FROM images '
                                 'WHERE scope=? AND sender_id=? AND created<=? '
                                 'AND created>COALESCE((SELECT created FROM resets WHERE scope=? AND sender_id=?),-1) '
                                 'ORDER BY created DESC LIMIT 1',
                                 (self.scope(scope), sender, before, self.scope(scope), sender)).fetchone()
            else:
                return None
        if not row:
            return None
        return {'message_id': row[0], 'sender_id': row[1], 'created': row[2], 'refs': json.loads(row[3])}

    def _reset(self, scope, sender, created):
        with closing(self._connect()) as db:
            with db:
                db.execute('INSERT INTO resets VALUES (?,?,?) ON CONFLICT(scope,sender_id) '
                           'DO UPDATE SET created=MAX(created,excluded.created)',
                           (self.scope(scope), sender, created))

    async def reset(self, scope, sender, created):
        await asyncio.to_thread(self._reset, scope, sender, created)

    async def read(self, scope, before, *, mid=None, sender=None):
        return await asyncio.to_thread(self._read, scope, before, mid, sender)
