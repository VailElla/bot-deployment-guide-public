import tempfile
import unittest
from pathlib import Path

from astrbot_plugin_group_context.image_archive import ImageArchive


class ImageArchiveTests(unittest.IsolatedAsyncioTestCase):
    async def test_scope_sender_and_reset_boundaries_survive_reopen(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'images.sqlite3'
            archive = ImageArchive(path)
            group = ('synthetic', 'group', 'group-a')
            private = ('synthetic', 'private', 'user-a')
            await archive.save(group, 'm1', 'user-a', 10, ['synthetic-image'])
            await archive.save(private, 'm1', 'user-a', 10, ['private-image'])
            other_group = ('synthetic', 'group', 'group-b')
            self.assertIsNone(await archive.read(other_group, 20, mid='m1'))
            self.assertIsNone(await archive.read(group, 20, sender='user-b'))
            self.assertIsNone(await archive.read(group, 9, mid='m1'))
            self.assertEqual((await archive.read(group, 20, mid='m1'))['refs'], ['synthetic-image'])
            await archive.reset(private, 'user-a', 11)
            reopened = ImageArchive(path)
            self.assertIsNone(await reopened.read(private, 20, mid='m1'))
            self.assertEqual((await reopened.read(group, 20, mid='m1'))['sender_id'], 'user-a')
