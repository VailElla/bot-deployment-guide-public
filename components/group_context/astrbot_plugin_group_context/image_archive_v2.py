"""Conversation-scoped image references with content-addressed binary storage."""
from __future__ import annotations

import base64
import hashlib
import json
import shutil
import sqlite3
import time
from collections import Counter
from contextlib import closing
from pathlib import Path

from .sqlite_worker import SQLiteWorker


class ImageArchive:
    format_version = 2
    capacity_bytes = 10 * 1024**3
    reserve_bytes = 1024**3

    def __init__(self, path):
        self.legacy_path = Path(path)
        self.path = self.legacy_path.with_name(self.legacy_path.stem + '.v2.sqlite3')
        self.worker = SQLiteWorker('qq-image-archive')
        self._initialized = False

    def _connect(self, *, migration=False):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(str(self.path), timeout=10)
        try:
            db.execute('PRAGMA busy_timeout=10000')
            db.execute('PRAGMA synchronous=FULL')
            db.execute('PRAGMA foreign_keys=ON')
            pages = max(16, self.capacity_bytes // db.execute('PRAGMA page_size').fetchone()[0])
            db.execute('PRAGMA max_page_count=' + str(pages))
            if not self._initialized:
                db.execute('PRAGMA journal_mode=WAL')
                db.executescript('''
                    CREATE TABLE IF NOT EXISTS blobs (
                        digest TEXT PRIMARY KEY, data BLOB NOT NULL, size INTEGER NOT NULL);
                    CREATE TABLE IF NOT EXISTS images (
                        scope TEXT NOT NULL, message_id TEXT NOT NULL, sender_id TEXT NOT NULL,
                        created REAL NOT NULL, refs TEXT NOT NULL, last_used REAL,
                        PRIMARY KEY(scope,message_id));
                    CREATE INDEX IF NOT EXISTS images_sender ON images(scope,sender_id,created DESC);
                    CREATE TABLE IF NOT EXISTS resets (scope TEXT NOT NULL, sender_id TEXT NOT NULL,
                        created REAL NOT NULL, PRIMARY KEY(scope,sender_id));
                    CREATE TABLE IF NOT EXISTS archive_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                    INSERT OR IGNORE INTO archive_meta VALUES ('format','2');
                    INSERT OR IGNORE INTO archive_meta VALUES ('stored_bytes','0');
                ''')
                db.commit()
                assert db.execute("SELECT value FROM archive_meta WHERE key='format'").fetchone()[0] == '2'
                if not migration and self.legacy_path.exists():
                    done = db.execute("SELECT value FROM archive_meta WHERE key='legacy_complete'").fetchone()
                    if not done or done[0] != '1':
                        raise RuntimeError('Image archive migration has not been verified')
                if 'last_used' not in {row[1] for row in db.execute('PRAGMA table_info(images)')}:
                    # At the old hard limit even a schema change may need pages.
                    # Reserve a small amount atomically using legacy creation
                    # time until actual access timestamps can be recorded.
                    with db:
                        db.execute('BEGIN IMMEDIATE')
                        # Another instance may have completed the upgrade while
                        # this connection was waiting for the write lock.
                        if 'last_used' not in {row[1] for row in db.execute('PRAGMA table_info(images)')}:
                            if migration and self._free_bytes(db) < 64 * 1024:
                                raise OSError('Incomplete legacy migration has insufficient capacity')
                            self._evict_until_free(db, 64 * 1024, legacy=True)
                            db.execute('ALTER TABLE images ADD COLUMN last_used REAL')
                self._initialized = True
            if not migration and self.legacy_path.exists():
                done = db.execute("SELECT value FROM archive_meta WHERE key='legacy_complete'").fetchone()
                if not done or done[0] != '1':
                    raise RuntimeError('Image archive migration has not been verified')
            return db
        except BaseException:
            db.close()
            raise

    @staticmethod
    def scope(scope):
        return json.dumps(scope, ensure_ascii=True, separators=(',', ':'))

    @staticmethod
    def _encode(refs):
        entries, blobs = [], {}
        for ref in refs:
            if not isinstance(ref, str):
                raise TypeError('Image reference must be a string')
            raw = ref.encode('utf8')
            prefix = None
            if ref.startswith('data:') and ',' in ref:
                head, encoded = ref.split(',', 1)
                if head.endswith(';base64'):
                    try:
                        decoded = base64.b64decode(encoded, validate=True)
                        # Keep unusual/noncanonical encodings byte-for-byte too.
                        if base64.b64encode(decoded).decode('ascii') == encoded:
                            raw, prefix = decoded, head + ','
                    except ValueError:
                        pass
            digest = hashlib.sha256(raw).hexdigest()
            entries.append({'digest': digest, 'prefix': prefix})
            blobs[digest] = raw
        return json.dumps(entries, separators=(',', ':')), blobs

    @staticmethod
    def _decode(db, encoded):
        refs, cache = [], {}
        for entry in json.loads(encoded):
            digest = entry['digest']
            if digest not in cache:
                row = db.execute('SELECT data FROM blobs WHERE digest=?', (digest,)).fetchone()
                if row is None or hashlib.sha256(row[0]).hexdigest() != digest:
                    raise ValueError('Image archive blob integrity failure')
                cache[digest] = row[0]
            raw = cache[digest]
            refs.append(entry['prefix'] + base64.b64encode(raw).decode('ascii')
                        if entry['prefix'] is not None else raw.decode('utf8'))
        return refs

    def _free_bytes(self, db):
        page_size = db.execute('PRAGMA page_size').fetchone()[0]
        page_count = db.execute('PRAGMA page_count').fetchone()[0]
        free_pages = db.execute('PRAGMA freelist_count').fetchone()[0]
        return self.capacity_bytes - (page_count - free_pages) * page_size

    def _evict_until_free(self, db, required, *, protected=(), legacy=False, keep=None):
        """Evict the least recently used records within the caller's transaction.

        Shared blobs remain until their final image reference is removed. The
        temporary reference counts are rebuilt from authoritative image rows;
        no large secondary index needs to be maintained or migrated.
        """
        if self._free_bytes(db) >= required:
            return 0
        order = 'created' if legacy else 'COALESCE(last_used, created)'
        rows = db.execute(f'SELECT scope,message_id,refs FROM images ORDER BY {order},created,rowid').fetchall()
        decoded = []
        counts = Counter()
        for scope, mid, encoded in rows:
            digests = {entry['digest'] for entry in json.loads(encoded)}
            counts.update(digests)
            decoded.append((scope, mid, encoded, digests))
        protected = set(protected)
        removed_bytes = removed = 0
        for scope, mid, encoded, digests in decoded:
            if keep == (scope, mid):
                continue
            db.execute('DELETE FROM images WHERE scope=? AND message_id=?', (scope, mid))
            removed += 1
            removed_bytes += len(encoded.encode('utf8'))
            for digest in digests:
                counts[digest] -= 1
                if counts[digest] == 0 and digest not in protected:
                    blob = db.execute('SELECT size FROM blobs WHERE digest=?', (digest,)).fetchone()
                    if blob is not None:
                        db.execute('DELETE FROM blobs WHERE digest=?', (digest,))
                        removed_bytes += int(blob[0])
            if self._free_bytes(db) >= required:
                break
        if self._free_bytes(db) < required:
            raise OSError('Image archive cannot free enough space for this operation')
        used = int(db.execute("SELECT value FROM archive_meta WHERE key='stored_bytes'").fetchone()[0])
        db.execute("UPDATE archive_meta SET value=? WHERE key='stored_bytes'", (str(max(0, used - removed_bytes)),))
        return removed

    def _save(self, scope, mid, sender, created, refs, *, migration=False):
        scope_text = self.scope(scope)
        with closing(self._connect(migration=migration)) as db, db:
            db.execute('BEGIN IMMEDIATE')
            # Duplicate messages consume neither capacity nor encoding work.
            if db.execute('SELECT 1 FROM images WHERE scope=? AND message_id=?', (scope_text,mid)).fetchone():
                return False
            encoded, blobs = self._encode(refs)
            new = {key: raw for key, raw in blobs.items()
                   if not db.execute('SELECT 1 FROM blobs WHERE digest=?', (key,)).fetchone()}
            added = sum(map(len,new.values())) + len(encoded.encode('utf8'))
            # Page and B-tree overhead also consumes the SQLite limit. Reserve
            # headroom before writing so eviction and insertion stay atomic.
            needed = added + max(64 * 1024, added // 50)
            if needed > self.capacity_bytes or shutil.disk_usage(self.path.parent).free < added + self.reserve_bytes + 65536:
                raise OSError('Image archive capacity limit or insufficient disk space')
            if migration:
                if self._free_bytes(db) < needed:
                    raise OSError('Image archive migration has insufficient capacity')
            else:
                self._evict_until_free(db, needed, protected=blobs)
            used = int(db.execute("SELECT value FROM archive_meta WHERE key='stored_bytes'").fetchone()[0])
            db.executemany('INSERT INTO blobs VALUES (?,?,?)', [(k,v,len(v)) for k,v in new.items()])
            db.execute('INSERT INTO images(scope,message_id,sender_id,created,refs,last_used) VALUES (?,?,?,?,?,?)',
                       (scope_text,mid,sender,created,encoded,time.time()))
            db.execute("UPDATE archive_meta SET value=? WHERE key='stored_bytes'", (str(used+added),))
            return True

    async def save(self, scope, mid, sender, created, refs):
        if scope and mid and sender and refs:
            await self.worker.run(self._save, scope, mid, sender, created, list(refs))

    def _read(self, scope, before, mid=None, sender=None):
        with closing(self._connect()) as db:
            if mid:
                query = 'SELECT message_id,sender_id,created,refs FROM images WHERE scope=? AND message_id=? AND created<=?'
                if scope[1] == 'private':
                    query += ' AND created>COALESCE((SELECT created FROM resets WHERE resets.scope=images.scope AND resets.sender_id=images.sender_id),-1)'
                row = db.execute(query, (self.scope(scope),mid,before)).fetchone()
            elif sender:
                row = db.execute('SELECT message_id,sender_id,created,refs FROM images '
                    'WHERE scope=? AND sender_id=? AND created<=? '
                    'AND created>COALESCE((SELECT created FROM resets WHERE scope=? AND sender_id=?),-1) '
                    'ORDER BY created DESC LIMIT 1',
                    (self.scope(scope),sender,before,self.scope(scope),sender)).fetchone()
            else:
                return None
            if not row:
                return None
            refs = self._decode(db,row[3])
            with db:
                db.execute('BEGIN IMMEDIATE')
                self._evict_until_free(db, 16 * 1024, keep=(self.scope(scope), row[0]))
                updated = db.execute('UPDATE images SET last_used=? WHERE scope=? AND message_id=?',
                                     (time.time(), self.scope(scope), row[0])).rowcount
            if not updated:
                return None
            return {'message_id':row[0], 'sender_id':row[1], 'created':row[2], 'refs':refs}

    async def read(self, scope, before, *, mid=None, sender=None):
        return await self.worker.run(self._read, scope, before, mid, sender)

    def _reset(self, scope, sender, created):
        with closing(self._connect()) as db, db:
            db.execute('INSERT INTO resets VALUES (?,?,?) ON CONFLICT(scope,sender_id) '
                'DO UPDATE SET created=MAX(created,excluded.created)', (self.scope(scope),sender,created))

    async def reset(self, scope, sender, created):
        await self.worker.run(self._reset, scope, sender, created)

    def migrate_legacy(self, *, finalize=False, progress=None):
        """Copy immutable legacy rows, verify each exact reference, then checkpoint.

        Run finalization only after old writers have stopped. The old database
        remains untouched for recovery; resumed copies recheck duplicate rows.
        """
        if not self.legacy_path.exists():
            raise FileNotFoundError(self.legacy_path)
        with closing(sqlite3.connect(self.legacy_path.as_uri()+'?mode=ro', uri=True)) as source:
            with closing(self._connect(migration=True)) as dest:
                row = dest.execute("SELECT value FROM archive_meta WHERE key='legacy_cursor'").fetchone()
                cursor = int(row[0]) if row else 0
            maximum, count = source.execute('SELECT COALESCE(MAX(rowid),0),COUNT(*) FROM images').fetchone()
            processed = 0
            while cursor < maximum:
                # One large image message at a time bounds migration memory.
                row = source.execute('SELECT rowid,scope,message_id,sender_id,created,refs FROM images '
                                     'WHERE rowid>? AND rowid<=? ORDER BY rowid LIMIT 1', (cursor,maximum)).fetchone()
                if row is None:
                    break
                rid, scope, mid, sender, created, refs = row
                originals = json.loads(refs)
                self._save(json.loads(scope), mid, sender, created, originals, migration=True)
                with closing(self._connect(migration=True)) as dest, dest:
                    actual = dest.execute('SELECT sender_id,created,refs FROM images WHERE scope=? AND message_id=?', (scope,mid)).fetchone()
                    if actual is None or tuple(actual[:2]) != (sender,created) or self._decode(dest,actual[2]) != originals:
                        raise ValueError('Legacy image migration verification failed')
                    dest.execute("INSERT INTO archive_meta VALUES ('legacy_cursor',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (str(rid),))
                cursor = rid
                processed += 1
                if processed % 100 == 0:
                    with closing(self._connect(migration=True)) as dest:
                        dest.execute('PRAGMA wal_checkpoint(TRUNCATE)')
                    if progress:
                        progress({'cursor':cursor,'maximum':maximum,'processed':processed})
            with closing(self._connect(migration=True)) as dest, dest:
                for scope, sender, created in source.execute('SELECT scope,sender_id,created FROM resets'):
                    dest.execute('INSERT INTO resets VALUES (?,?,?) ON CONFLICT(scope,sender_id) '
                                 'DO UPDATE SET created=MAX(created,excluded.created)', (scope,sender,created))
                if finalize:
                    current = source.execute('SELECT COALESCE(MAX(rowid),0),COUNT(*) FROM images').fetchone()
                    if current != (maximum,count) or cursor != maximum:
                        raise RuntimeError('Legacy writer changed during finalization')
                    if dest.execute('SELECT COUNT(*) FROM images').fetchone()[0] < count:
                        raise ValueError('Incomplete image archive copy')
            verification = {}
            if finalize:
                with closing(self._connect(migration=True)) as dest:
                    verification = self._verify_legacy(source, dest, progress)
                if source.execute('SELECT COALESCE(MAX(rowid),0),COUNT(*) FROM images').fetchone() != (maximum,count):
                    raise RuntimeError('Legacy writer changed during verification')
            with closing(self._connect(migration=True)) as dest:
                dest.execute('PRAGMA wal_checkpoint(TRUNCATE)')
                check = dest.execute('PRAGMA quick_check').fetchone()[0]
                if check != 'ok':
                    raise ValueError('Image archive SQLite verification failed')
                unique, raw_bytes = dest.execute('SELECT COUNT(*),COALESCE(SUM(size),0) FROM blobs').fetchone()
                if finalize:
                    with dest:
                        dest.execute("INSERT INTO archive_meta VALUES ('legacy_complete','1') ON CONFLICT(key) DO UPDATE SET value='1'")
                        dest.execute("INSERT INTO archive_meta VALUES ('legacy_count',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (str(count),))
                    dest.execute('PRAGMA wal_checkpoint(TRUNCATE)')
            return {'legacy_rows':count,'cursor':cursor,'copied_this_run':processed,
                    'unique_blobs':unique,'raw_blob_bytes':raw_bytes,'finalized':finalize,
                    'legacy_bytes':self.legacy_path.stat().st_size,'new_bytes':self.path.stat().st_size,
                    'verification':verification}

    def _verify_legacy(self, source, dest, progress=None):
        digest = hashlib.sha256()
        count = references = 0
        for scope, mid, sender, created, encoded in source.execute(
                'SELECT scope,message_id,sender_id,created,refs FROM images ORDER BY rowid'):
            row = dest.execute('SELECT sender_id,created,refs FROM images WHERE scope=? AND message_id=?', (scope,mid)).fetchone()
            expected = json.loads(encoded)
            if row is None or tuple(row[:2]) != (sender,created) or self._decode(dest,row[2]) != expected:
                raise ValueError('Full legacy image verification failed')
            digest.update(json.dumps([scope,mid,sender,created,expected],ensure_ascii=True,separators=(',',':')).encode('utf8'))
            count += 1
            references += len(expected)
            if progress and count % 500 == 0:
                progress({'verified_rows':count})
        for scope, sender, created in source.execute('SELECT scope,sender_id,created FROM resets'):
            row = dest.execute('SELECT created FROM resets WHERE scope=? AND sender_id=?',(scope,sender)).fetchone()
            if row is None or row[0] < created:
                raise ValueError('Legacy reset verification failed')
        return {'rows':count,'references':references,'content_sha256':digest.hexdigest(),'all_references_equal':True}

    def export_legacy(self):
        """Backfill new v2 records before a source rollback; never restore an old DB."""
        if not self.legacy_path.exists():
            raise FileNotFoundError(self.legacy_path)
        inserted = 0
        with closing(self._connect()) as source, closing(sqlite3.connect(self.legacy_path,timeout=10)) as dest:
            for scope,mid,sender,created,encoded in source.execute('SELECT scope,message_id,sender_id,created,refs FROM images'):
                old = dest.execute('SELECT sender_id,created FROM images WHERE scope=? AND message_id=?',(scope,mid)).fetchone()
                if old:
                    if tuple(old) != (sender,created):
                        raise ValueError('Legacy rollback record conflict')
                    continue
                refs = self._decode(source,encoded)
                with dest:
                    dest.execute('INSERT INTO images VALUES (?,?,?,?,?)',(scope,mid,sender,created,json.dumps(refs)))
                    inserted += 1
            with dest:
                for row in source.execute('SELECT scope,sender_id,created FROM resets'):
                    dest.execute('INSERT INTO resets VALUES (?,?,?) ON CONFLICT(scope,sender_id) '
                        'DO UPDATE SET created=MAX(created,excluded.created)',tuple(row))
        return {'exported_rows':inserted}

    async def close(self):
        await self.worker.close()
