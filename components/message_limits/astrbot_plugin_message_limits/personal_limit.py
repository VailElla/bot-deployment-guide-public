"""One rolling QQ request budget per account and sender across groups and DMs."""
import asyncio
import hashlib
import json
import sqlite3
import time
from contextlib import closing
from pathlib import Path

from .sqlite_worker import SQLiteWorker, finish_submitted


class PersonalLimiter:
    limit = 3
    window = 60
    long_limit = 20
    long_window = 600
    group_limit = 5
    group_window = 60
    marker = '_local_codex_personal_admission_v1'
    pending_marker = '_local_codex_personal_admission_pending_v2'

    def __init__(self, path, *, defer_init=False):
        self.path = Path(path)
        self.worker = SQLiteWorker('qq-personal-quota')
        if not defer_init:
            self.initialize()

    def initialize(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.path, timeout=5)) as db, db:
            db.execute('CREATE TABLE IF NOT EXISTS attempts (scope TEXT NOT NULL, at REAL NOT NULL)')
            db.execute('CREATE INDEX IF NOT EXISTS attempts_scope_at ON attempts(scope, at)')
            db.execute('CREATE INDEX IF NOT EXISTS attempts_at ON attempts(at)')
            db.execute('CREATE TABLE IF NOT EXISTS group_attempts (scope TEXT NOT NULL, at REAL NOT NULL)')
            db.execute('CREATE INDEX IF NOT EXISTS group_attempts_scope_at ON group_attempts(scope, at)')
            db.execute('CREATE INDEX IF NOT EXISTS group_attempts_at ON group_attempts(at)')

    def scope(self, event):
        if event.get_platform_name() != 'aiocqhttp':
            return None
        raw_sender, raw_bot, raw_platform = event.get_sender_id(), event.get_self_id(), event.get_platform_id()
        if raw_sender is None or raw_bot is None or raw_platform is None:
            return None
        sender, bot = str(raw_sender), str(raw_bot)
        if not sender or not bot or not str(raw_platform) or sender == bot:
            return None
        scope = hashlib.sha256(json.dumps([str(raw_platform), bot, sender]).encode()).hexdigest()
        group = str(event.get_group_id() or '')
        group_scope = hashlib.sha256(json.dumps([str(raw_platform), bot, group]).encode()).hexdigest() if group else None
        return scope, group_scope

    def reserve(self, scopes, now):
        scope, group_scope = scopes
        reason = None
        with closing(sqlite3.connect(self.path, timeout=5)) as db, db:
            db.execute('BEGIN IMMEDIATE')
            # Keep one admission history for both personal windows. Check all
            # three limits before inserting so a rejected attempt spends none.
            db.execute('DELETE FROM attempts WHERE at <= ? OR at > ?', (now - self.long_window, now))
            db.execute('DELETE FROM group_attempts WHERE at <= ? OR at > ?', (now - self.group_window, now))
            count = db.execute('SELECT COUNT(*) FROM attempts WHERE scope = ? AND at > ?',
                               (scope, now - self.window)).fetchone()[0]
            if count >= self.limit:
                reason = 'personal_limit'
            elif db.execute('SELECT COUNT(*) FROM attempts WHERE scope = ?', (scope,)).fetchone()[0] >= self.long_limit:
                reason = 'personal_long_limit'
            elif group_scope is not None:
                count = db.execute('SELECT COUNT(*) FROM group_attempts WHERE scope = ?', (group_scope,)).fetchone()[0]
                if count >= self.group_limit:
                    reason = 'group_limit'
            allowed = reason is None
            if allowed:
                db.execute('INSERT INTO attempts VALUES (?, ?)', (scope, now))
                if group_scope is not None:
                    db.execute('INSERT INTO group_attempts VALUES (?, ?)', (group_scope, now))
        return allowed, reason

    def admit(self, event, now=None):
        """Synchronous API for offline tools; live hooks use admit_async."""
        scope = self.scope(event)
        if scope is None:
            return True
        decision = event.get_extra(self.marker)
        if decision is None:
            decision, reason = self.reserve(scope, time.time() if now is None else now)
            if reason:
                event.set_extra('_local_codex_quota_blocked', reason)
            event.set_extra(self.marker, decision)
        return decision

    async def admit_async(self, event, now=None):
        scope = self.scope(event)
        if scope is None:
            return True
        decision = event.get_extra(self.marker)
        if decision is not None:
            return decision
        task = event.get_extra(self.pending_marker)
        if task is None:
            async def reserve():
                # Only primitive values cross into the worker. Event state stays
                # on the event loop, including after a caller is cancelled.
                value, reason = await self.worker.run(self.reserve, scope, time.time() if now is None else now)
                if reason:
                    event.set_extra('_local_codex_quota_blocked', reason)
                event.set_extra(self.marker, value)
                return value
            task = asyncio.create_task(reserve())
            event.set_extra(self.pending_marker, task)
            def finished(done):
                if event.get_extra(self.pending_marker) is done:
                    event.set_extra(self.pending_marker, None)
                if not done.cancelled():
                    done.exception()
            task.add_done_callback(finished)
        return await finish_submitted(task)

    async def close(self):
        await self.worker.close()
