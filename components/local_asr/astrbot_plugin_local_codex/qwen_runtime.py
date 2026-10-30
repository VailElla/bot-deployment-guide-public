"""Shared Qwen ASR mechanics; standard library only, no resident model."""
from __future__ import annotations

import array
import asyncio
import errno
import hashlib
import json
import os
import sys
import tempfile
import time
import wave
from pathlib import Path

RATE = 16000
MAX_BYTES = 32 * 1024 * 1024
CHUNK_SAMPLES = 25 * RATE
SILENCE_RMS_PCM16 = 32768 * 0.0001


def verify_runtime(manifest):
    expected = dict(manifest['engine_files'])
    expected[manifest['model']] = manifest['model_sha256']
    stamps = {}
    for name, want in expected.items():
        p = Path(name)
        if not p.is_absolute() or not p.is_file():
            raise ValueError('ASR runtime unavailable')
        h = hashlib.sha256()
        with p.open('rb') as f:
            for block in iter(lambda: f.read(8 * 1024 * 1024), b''):
                h.update(block)
        if h.hexdigest() != want:
            raise ValueError('ASR runtime integrity check failed')
        st = p.stat()
        stamps[str(p)] = (st.st_size, st.st_mtime_ns)
    ffmpeg = Path(manifest['ffmpeg'])
    if not ffmpeg.is_absolute() or not ffmpeg.is_file():
        raise ValueError('ASR converter unavailable')
    st = ffmpeg.stat()
    stamps[str(ffmpeg)] = (st.st_size, st.st_mtime_ns)
    if not Path(manifest['lock_file']).is_absolute():
        raise ValueError('ASR shared lock must be absolute')
    return stamps


def check_stamps(stamps):
    for name, expected in stamps.items():
        st = Path(name).stat()
        if (st.st_size, st.st_mtime_ns) != tuple(expected):
            raise ValueError('ASR runtime changed')


class ModelLease:
    """One kernel-backed lease across both plugins, reloads and processes."""
    def __init__(self, file):
        self.path = Path(file)
        self.file = None

    async def __aenter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        f = self.path.open('a+b')
        self.file = f
        try:
            while True:
                try:
                    f.seek(0)
                    if os.name == 'nt':
                        import msvcrt
                        msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
                    else:
                        import fcntl
                        fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    # Windows permits locking beyond EOF. Initialize only
                    # after acquiring the byte lock so concurrent creators
                    # cannot write into a region already held by another PID.
                    if f.seek(0, 2) == 0:
                        f.write(b'0')
                        f.flush()
                    return self
                except OSError as exc:
                    if exc.errno not in (errno.EACCES, errno.EAGAIN, errno.EDEADLK) and getattr(exc, 'winerror', None) != 33:
                        raise
                    await asyncio.sleep(0.05)
        except BaseException:
            f.close()
            self.file = None
            raise

    async def __aexit__(self, *unused):
        if self.file is not None:
            try:
                self.file.seek(0)
                if os.name == 'nt':
                    import msvcrt
                    msvcrt.locking(self.file.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(self.file.fileno(), fcntl.LOCK_UN)
            finally:
                self.file.close()
                self.file = None


def inspect_audio(file, max_seconds):
    if Path(file).stat().st_size > MAX_BYTES:
        raise ValueError('ASR audio too large')
    with wave.open(str(file), 'rb') as w:
        duration = w.getnframes() / w.getframerate()
        if not 8000 <= w.getframerate() <= 96000 or not 0 < duration <= max_seconds:
            raise ValueError('ASR audio duration or sample rate invalid')
        if not 1 <= w.getnchannels() <= 8 or w.getcomptype() != 'NONE':
            raise ValueError('ASR audio format invalid')
    return duration


def split_audio(file, folder):
    with wave.open(str(file), 'rb') as w:
        if (w.getframerate(), w.getnchannels(), w.getsampwidth()) != (RATE, 1, 2):
            raise ValueError('ASR converter returned an invalid format')
        count = w.getnframes()
        payload = w.readframes(count)
        if len(payload) != count * 2:
            raise ValueError('ASR audio truncated')
    samples = array.array('h', payload)
    if sys.byteorder != 'little':
        samples.byteswap()
    chunks = []
    start = 0
    while start < count:
        end = min(start + CHUNK_SAMPLES, count)
        if end < count:
            end = min(range(end - 5 * RATE, end, 1600),
                      key=lambda i: sum(x*x for x in samples[i:i+800])) + 400
        part = samples[start:end]
        silent = sum(x*x for x in part) / len(part) < SILENCE_RMS_PCM16**2
        row = {'start_sample': start, 'end_sample': end, 'silent': silent,
               'duration_seconds': round((end-start)/RATE, 3)}
        if not silent:
            target = Path(folder) / f'chunk-{len(chunks)}.wav'
            if sys.byteorder != 'little':
                part.byteswap()
            with wave.open(str(target), 'wb') as w:
                w.setnchannels(1)
                w.setsampwidth(2)
                w.setframerate(RATE)
                w.writeframes(part.tobytes())
            row['file'] = str(target)
        chunks.append(row)
        start = end
    return chunks


async def _complete_thread(function, *args):
    # Cancellation must not remove the temp directory while a writer still runs.
    task = asyncio.create_task(asyncio.to_thread(function, *args))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        await asyncio.gather(task, return_exceptions=True)
        raise


async def transcribe_file(source, manifest, run, *, language='auto', max_seconds=300,
                          advance=None, details=None):
    if language not in ('auto', 'zh'):
        raise ValueError('Unsupported ASR language setting')
    inspect_audio(source, max_seconds)
    engines = [p for p in manifest['engine_files'] if Path(p).name.lower() == 'qwen3-asr-cli.exe']
    if len(engines) != 1:
        raise ValueError('ASR engine manifest invalid')
    info = {} if details is None else details.setdefault('shared_asr', {})
    def stage(name):
        if advance is not None:
            advance(name)
    stage('shared_queue')
    queued = time.perf_counter()
    async with ModelLease(manifest['lock_file']):
        info['shared_queue_seconds'] = round(time.perf_counter()-queued, 3)
        with tempfile.TemporaryDirectory(prefix='qwen-shared-asr-') as folder:
            wav = Path(folder) / 'audio.wav'
            stage('conversion')
            await run(manifest['ffmpeg'], '-nostdin', '-hide_banner', '-loglevel', 'error',
                      '-i', source, '-ar', '16000', '-ac', '1', '-c:a', 'pcm_s16le', '-y', wav)
            stage('preprocessing')
            chunks = await _complete_thread(split_audio, wav, folder)
            info['chunks'] = [{k:v for k,v in c.items() if k != 'file'} for c in chunks]
            texts = []
            for i, chunk in enumerate(chunks):
                if chunk['silent']:
                    continue
                stage('recognition')
                info['chunk_index'] = i
                out = Path(folder) / f'result-{i}.txt'
                args = [engines[0], '-m', manifest['model'], '-f', chunk['file'],
                        '--max-tokens', '256', '-t', '4', '-o', out]
                if language != 'auto':
                    args += ['-l', language]
                await run(*args)
                stage('output')
                if not out.is_file() or out.stat().st_size > 16384:
                    raise ValueError('Invalid ASR chunk output')
                text = out.read_text(encoding='utf-8-sig').strip()
                if not text:
                    # Do not silently discard an unrecognized non-silent chunk.
                    raise ValueError('Empty ASR chunk output')
                texts.append(text)
            text = ''.join(texts).strip()
            if len(text) > 12000:
                raise ValueError('ASR text too large')
            info['silent_input'] = not texts
            return text
