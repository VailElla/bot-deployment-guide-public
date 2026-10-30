"""Reserve one welcome per account and group every two minutes."""
import sqlite3
import time
from contextlib import closing
from pathlib import Path


class WelcomeCooldown:
    seconds = 120

    def __init__(self, path, clock=time.time):
        self.path = Path(path)
        self.clock = clock

    def admit(self, event):
        # Reprocessing an event must neither consume nor extend another window.
        decision = event.get_extra('_local_codex_welcome_admitted')
        if decision is not None:
            return decision
        key = (str(event.get_platform_id()), str(event.get_self_id()),
               str(event.get_group_id()))
        now = self.clock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.path, timeout=5)) as db, db:
            db.execute('CREATE TABLE IF NOT EXISTS welcome_cooldown '
                       '(platform TEXT, account TEXT, group_id TEXT, until REAL, '
                       'PRIMARY KEY(platform, account, group_id))')
            # Reserve synchronously before model queuing; also safe across reloads.
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT until FROM welcome_cooldown WHERE '
                             'platform=? AND account=? AND group_id=?', key).fetchone()
            admitted = row is None or now >= row[0]
            if admitted:
                db.execute('INSERT OR REPLACE INTO welcome_cooldown VALUES (?,?,?,?)',
                           (*key, now + self.seconds))
                db.execute('DELETE FROM welcome_cooldown WHERE until<=?', (now,))
        event.set_extra('_local_codex_welcome_admitted', admitted)
        return admitted
