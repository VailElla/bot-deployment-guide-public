#!/usr/bin/env python3
"""Create a local ASR manifest or transcribe an existing WAV file."""
import argparse
import asyncio
import hashlib
import json
import logging
from pathlib import Path

from astrbot_plugin_local_codex.local_voice_asr import LocalVoiceASR
from astrbot_plugin_local_codex import qwen_runtime


def existing_file(value):
    path = Path(value).expanduser().resolve(strict=True)
    if not path.is_file():
        raise ValueError(f'Not a regular file: {path}')
    return path


def sha256(path):
    digest = hashlib.sha256()
    with path.open('rb') as source:
        for block in iter(lambda: source.read(8 * 1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def make_manifest(args):
    engine, model, ffmpeg = map(existing_file, (args.engine, args.model, args.ffmpeg))
    if engine.name.lower() != 'qwen3-asr-cli.exe':
        raise ValueError('The tested engine executable must be named qwen3-asr-cli.exe')
    lock = Path(args.lock).expanduser().resolve()
    manifest = {'engine_files': {str(engine): sha256(engine)}, 'model': str(model),
                'model_sha256': sha256(model), 'ffmpeg': str(ffmpeg), 'lock_file': str(lock)}
    output = Path(args.output).expanduser()
    # Do not overwrite a deployment's existing manifest.
    with output.open('x', encoding='utf-8') as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)
        f.write('\n')
    print(f'Manifest created: {output}')


async def transcribe(args):
    manifest = json.loads(existing_file(args.manifest).read_text(encoding='utf-8-sig'))
    source = existing_file(args.wav)
    adapter = LocalVoiceASR(logging.getLogger('local-asr'), Path('__unused__'))
    # Integrity checks are part of the same overall deadline as recognition.
    async with asyncio.timeout(args.timeout):
        await asyncio.to_thread(qwen_runtime.verify_runtime, manifest)
        text = await qwen_runtime.transcribe_file(source, manifest, adapter._run,
                                                 language=args.language)
    print(json.dumps({'text': text}, ensure_ascii=False))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    manifest = sub.add_parser('manifest', help='Build a manifest using local file hashes')
    for option in ('engine', 'model', 'ffmpeg', 'lock', 'output'):
        manifest.add_argument('--' + option, required=True)
    run = sub.add_parser('transcribe', help='Recognize a local WAV')
    run.add_argument('--manifest', required=True)
    run.add_argument('--wav', required=True)
    run.add_argument('--language', choices=('auto', 'zh'), default='auto')
    run.add_argument('--timeout', type=float, default=45)
    args = parser.parse_args()
    if args.command == 'manifest':
        make_manifest(args)
    else:
        if not 0 < args.timeout <= 3600:
            parser.error('--timeout must be in (0, 3600]')
        asyncio.run(transcribe(args))


if __name__ == '__main__':
    main()
