import base64
import hashlib
import sqlite3
from contextlib import closing
from unittest.mock import patch

import pytest
from astrbot_plugin_group_context.image_archive_v2 import ImageArchive


def ref(byte, size=160_000):
    return 'data:image/png;base64,' + base64.b64encode(bytes([byte]) * size).decode()


def archive_for(tmp_path, capacity=600 * 1024):
    archive = ImageArchive(tmp_path / 'images.sqlite3')
    archive.capacity_bytes = capacity
    archive.reserve_bytes = 0
    return archive


def save_at(archive, mid, content, stamp, group=('qq', 'group', 'a')):
    with patch('astrbot_plugin_group_context.image_archive_v2.time.time', return_value=stamp):
        return archive._save(group, mid, 'sender', stamp, [content])


def read_at(archive, mid, stamp, group=('qq', 'group', 'a')):
    with patch('astrbot_plugin_group_context.image_archive_v2.time.time', return_value=stamp):
        return archive._read(group, 999, mid=mid)


def test_eviction_obeys_last_use_and_keeps_shared_content(tmp_path):
    a = archive_for(tmp_path, 600 * 1024)
    shared, c, d = ref(1), ref(2), ref(3, 190_000)
    save_at(a, 'a', shared, 1)
    save_at(a, 'b', shared, 3)
    save_at(a, 'c', c, 2)
    save_at(a, 'd', d, 4)
    assert read_at(a, 'a', 5) is None
    assert read_at(a, 'c', 5) is None
    assert read_at(a, 'b', 5)['refs'] == [shared]
    assert read_at(a, 'd', 5)['refs'] == [d]
    with closing(a._connect()) as db:
        digest = hashlib.sha256(bytes([1])*160_000).hexdigest()
        assert db.execute('SELECT count(*) FROM blobs WHERE digest=?', (digest,)).fetchone()[0] == 1
        assert db.execute('PRAGMA page_count').fetchone()[0] * db.execute('PRAGMA page_size').fetchone()[0] <= a.capacity_bytes


def test_read_updates_recency_and_group_scope(tmp_path):
    a = archive_for(tmp_path, 600 * 1024)
    g, other = ('qq', 'group', 'a'), ('qq', 'group', 'b')
    save_at(a, 'old', ref(1), 1, g)
    save_at(a, 'middle', ref(2), 2, other)
    assert read_at(a, 'old', 10, g)['refs'] == [ref(1)]
    assert read_at(a, 'old', 11, other) is None
    save_at(a, 'incoming', ref(3, 250_000), 12, g)
    assert read_at(a, 'middle', 13, other) is None
    assert read_at(a, 'old', 13, g)['refs'] == [ref(1)]
    assert read_at(a, 'incoming', 13, g)['refs'] == [ref(3, 250_000)]


def test_failed_insert_rolls_back_all_evictions(tmp_path):
    a = archive_for(tmp_path, 600 * 1024)
    save_at(a, 'one', ref(1), 1)
    save_at(a, 'two', ref(2), 2)
    with closing(a._connect()) as db, db:
        db.execute("CREATE TRIGGER reject_incoming BEFORE INSERT ON images WHEN NEW.message_id='reject' BEGIN SELECT RAISE(ABORT, 'reject'); END")
    with pytest.raises(sqlite3.IntegrityError, match='reject'):
        save_at(a, 'reject', ref(3, 250_000), 3)
    assert read_at(a, 'one', 4)['refs'] == [ref(1)]
    assert read_at(a, 'two', 4)['refs'] == [ref(2)]
    with closing(a._connect()) as db:
        assert db.execute('select count(*) from images').fetchone()[0] == 2
        assert db.execute('select count(*) from blobs').fetchone()[0] == 2


def test_incoming_can_reuse_blob_of_evicted_record(tmp_path):
    a = archive_for(tmp_path, 600 * 1024)
    shared = ref(7)
    save_at(a, 'old', shared, 1)
    save_at(a, 'other', ref(8), 2)
    # The oldest record's blob must remain available to the incoming record.
    with patch('astrbot_plugin_group_context.image_archive_v2.time.time', return_value=3):
        a._save(('qq','group','a'), 'new', 'sender', 3, [shared, ref(9, 220_000)])
    assert read_at(a, 'old', 4) is None
    assert read_at(a, 'new', 4)['refs'] == [shared, ref(9, 220_000)]


def test_many_evictions_keep_accounting_and_references_consistent(tmp_path):
    a = archive_for(tmp_path, 512 * 1024)
    for i in range(60):
        group = ('qq', 'group', str(i % 3))
        save_at(a, str(i), ref(i % 11, 18_000), i + 1, group)
        if i % 8 == 0:
            read_at(a, str(i), i + 1000, group)
    with closing(a._connect()) as db:
        rows = db.execute('SELECT refs FROM images').fetchall()
        wanted = set()
        encoded_size = 0
        for (encoded,) in rows:
            encoded_size += len(encoded.encode('utf8'))
            for entry in __import__('json').loads(encoded):
                wanted.add(entry['digest'])
            assert a._decode(db, encoded)
        blobs = dict(db.execute('SELECT digest,size FROM blobs'))
        assert set(blobs) == wanted
        assert int(db.execute("SELECT value FROM archive_meta WHERE key='stored_bytes'").fetchone()[0]) == encoded_size + sum(blobs.values())
        assert db.execute('PRAGMA quick_check').fetchone()[0] == 'ok'
        assert db.execute('PRAGMA page_count').fetchone()[0] * db.execute('PRAGMA page_size').fetchone()[0] <= a.capacity_bytes


def test_legacy_copy_is_verified_before_normal_access(tmp_path):
    from astrbot_plugin_group_context.image_archive import ImageArchive as LegacyArchive
    path = tmp_path / 'images.sqlite3'
    old = LegacyArchive(path)
    old._save(('qq', 'group', 'a'), 'old', 'sender', 1, [ref(4, 1000)])
    new = archive_for(tmp_path, 1024 * 1024)
    with pytest.raises(RuntimeError, match='migration has not been verified'):
        new._read(('qq', 'group', 'a'), 100, mid='old')
    report = new.migrate_legacy(finalize=True)
    assert report['finalized'] and report['verification']['all_references_equal']
    assert new._read(('qq', 'group', 'a'), 100, mid='old')['refs'] == [ref(4, 1000)]
    assert path.exists()
