import importlib.util
from pathlib import Path
import unittest

spec = importlib.util.spec_from_file_location('split_manifest', Path(__file__).resolve().parents[1] / 'split_manifest.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class SplitTests(unittest.TestCase):
    def test_source_and_duplicate_links_never_cross(self):
        rows = [dict(audio=str(i), text='test', source=source, sha256=digest)
                for i, (source, digest) in enumerate([('a', '1'), ('a', '2'), ('b', '2'), ('c', '3'), ('d', '4')])]
        for seed in range(20):
            train, validation = module.split(rows, .4, seed)
            self.assertFalse({x['source'] for x in train} & {x['source'] for x in validation})
            self.assertFalse({x['sha256'] for x in train} & {x['sha256'] for x in validation})
            self.assertEqual(len(train) + len(validation), len(rows))
            self.assertEqual((train, validation), module.split(rows, .4, seed))

    def test_unsplittable_input_fails(self):
        rows = [dict(audio=str(i), text='test', source='a', sha256=str(i)) for i in range(3)]
        with self.assertRaises(ValueError):
            module.split(rows, .2, 42)


if __name__ == '__main__':
    unittest.main()
