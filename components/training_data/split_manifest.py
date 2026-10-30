#!/usr/bin/env python3
"""Split a JSONL audio manifest without leaking sources or duplicate files."""
import argparse
import hashlib
import json
import random
from collections import defaultdict
from pathlib import Path


def split(rows, fraction, seed):
    if not 0 < fraction < 1 or len(rows) < 2:
        raise ValueError('Need at least two rows and a validation fraction between 0 and 1')
    parents = list(range(len(rows)))
    def root(i):
        while parents[i] != i:
            parents[i] = parents[parents[i]]
            i = parents[i]
        return i
    def join(a, b):
        parents[root(a)] = root(b)
    groups, digests = {}, {}
    for i, row in enumerate(rows):
        if not isinstance(row, dict) or not all(isinstance(row.get(k), str) and row[k].strip() for k in ('audio', 'text', 'source')):
            raise ValueError(f'Row {i + 1} needs non-empty audio, text and source strings')
        for value, seen in ((row['source'], groups), (row['sha256'], digests)):
            if value in seen:
                join(i, seen[value])
            else:
                seen[value] = i
    components = defaultdict(list)
    for i, row in enumerate(rows):
        components[root(i)].append(row)
    blocks = list(components.values())
    if len(blocks) < 2:
        raise ValueError('All audio belongs to one source/duplicate group; cannot split safely')
    random.Random(seed).shuffle(blocks)
    target = max(1, round(len(rows) * fraction))
    validation, train = [], []
    for index, block in enumerate(blocks):
        # Reserve a group for training; keep groups indivisible.
        if index < len(blocks) - 1 and len(validation) < target:
            validation.extend(block)
        else:
            train.extend(block)
    return train, validation


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('manifest', type=Path, help='JSONL with audio, text and source fields')
    parser.add_argument('--output-dir', type=Path, required=True, help='Must not already exist')
    parser.add_argument('--validation-fraction', type=float, default=0.1)
    parser.add_argument('--seed', type=int, default=42)
    args = parser.parse_args()
    if args.output_dir.exists():
        parser.error('Output directory already exists')
    rows = []
    for number, line in enumerate(args.manifest.read_text(encoding='utf-8-sig').splitlines(), 1):
        if not line.strip():
            continue
        row = json.loads(line)
        if not isinstance(row, dict) or not isinstance(row.get('audio'), str):
            raise ValueError(f'Row {number} is invalid')
        path = Path(row['audio'])
        if not path.is_absolute():
            path = args.manifest.parent / path
        if not path.is_file():
            raise ValueError(f'Audio missing on row {number}')
        digest = hashlib.sha256()
        with path.open('rb') as audio:
            for block in iter(lambda: audio.read(1024 * 1024), b''):
                digest.update(block)
        row['sha256'] = digest.hexdigest()
        # Resolve paths so output works regardless of its location.
        row['audio'] = str(path.resolve())
        rows.append(row)
    train, validation = split(rows, args.validation_fraction, args.seed)
    args.output_dir.mkdir(parents=True, exist_ok=False)
    for name, items in (('train.jsonl', train), ('validation.jsonl', validation)):
        with (args.output_dir / name).open('x', encoding='utf-8') as f:
            for item in items:
                f.write(json.dumps(item, ensure_ascii=False) + '\n')
    print(json.dumps({'train': len(train), 'validation': len(validation), 'seed': args.seed}))


if __name__ == '__main__':
    main()
