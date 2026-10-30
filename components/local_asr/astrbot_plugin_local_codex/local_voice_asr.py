"""Offline Qwen recognition for QQ voices rejected by native STT."""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import math
import os
import subprocess
import tempfile
from pathlib import Path

from . import qwen_runtime

MAX_WAV_BYTES = qwen_runtime.MAX_BYTES
RESULT_PREFIX = 'LOCAL_ASR_RESULT='  # Historical protocol; no worker is launched.


class LocalVoiceASR:
    def __init__(self, logger, config_path=None):
        if config_path is None:
            from astrbot.core.utils.astrbot_path import get_astrbot_data_path
            config_path = Path(get_astrbot_data_path()) / 'plugin_data/local_codex/voice_asr.json'
        self.config_path = Path(config_path)
        self.logger = logger
        self.lock = asyncio.Lock()
        self._manifest_key = None
        self._stamps = {}

    async def transcribe(self, event, record):
        if not self.config_path.is_file():
            return None
        try:
            cfg = json.loads(self.config_path.read_text(encoding='utf-8-sig'))
            if cfg.get('enable') is not True:
                return None
            if cfg.get('backend') != 'qwen3_asr_gguf':
                raise ValueError('Local ASR backend is not Qwen')
            manifest_file = Path(cfg['runtime_manifest'])
            if not manifest_file.is_absolute() or not manifest_file.is_file():
                raise ValueError('Local ASR manifest unavailable')
            timeout = float(cfg.get('timeout', 45))
            if not math.isfinite(timeout):
                raise ValueError('Invalid local ASR timeout')
            timeout = min(90, max(1, timeout))
            # Verification, queueing, decoding and recognition share one deadline.
            async with asyncio.timeout(timeout):
                async with self.lock:
                    raw = manifest_file.read_bytes()
                    manifest = json.loads(raw.decode('utf-8-sig'))
                    key = hashlib.sha256(raw).hexdigest()
                    if key != self._manifest_key:
                        self._stamps = await asyncio.to_thread(qwen_runtime.verify_runtime, manifest)
                        self._manifest_key = key
                    qwen_runtime.check_stamps(self._stamps)
                    return await self._recognize(event, record, manifest, cfg.get('language', 'auto'))
        except Exception as exc:
            self.logger.warning('Local Qwen voice recognition failed: %s', type(exc).__name__)
            return None

    async def _run(self, *args):
        options = {'creationflags': subprocess.CREATE_NO_WINDOW} if os.name == 'nt' else {}
        process = await asyncio.create_subprocess_exec(
            *map(str, args), stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, **options)
        tails = [bytearray(), bytearray()]
        async def drain(stream, tail):
            while True:
                block = await stream.read(4096)
                if not block:
                    return
                tail.extend(block)
                del tail[:-4096]
        readers = [asyncio.create_task(drain(process.stdout, tails[0])),
                   asyncio.create_task(drain(process.stderr, tails[1]))]
        try:
            if await process.wait():
                raise RuntimeError('Local Qwen ASR subprocess failed')
        finally:
            if process.returncode is None:
                try:
                    process.kill()
                    await process.wait()
                except ProcessLookupError:
                    pass
            try:
                await asyncio.wait_for(asyncio.gather(*readers), timeout=1)
            finally:
                for task in readers:
                    if not task.done():
                        task.cancel()

    async def _recognize(self, event, record, manifest, language):
        # SnowLuma handles QQ SILK; system ffmpeg often cannot.
        result = await event.bot.call_action('get_record', file=record.file or record.url, out_format='wav')
        encoded = result.get('base64') if isinstance(result, dict) else None
        if not isinstance(encoded, str) or len(encoded) > (MAX_WAV_BYTES + 2) // 3 * 4:
            raise ValueError('Invalid converted audio')
        payload = base64.b64decode(encoded, validate=True)
        if not payload or len(payload) > MAX_WAV_BYTES:
            raise ValueError('Invalid converted audio size')
        with tempfile.TemporaryDirectory(prefix='qq-qwen-asr-') as folder:
            wav = Path(folder) / 'voice.wav'
            wav.write_bytes(payload)
            qwen_runtime.inspect_audio(wav, 300)
            text = await qwen_runtime.transcribe_file(
                wav, manifest, self._run, language=language, max_seconds=300)
            if text:
                self.logger.info('Local Qwen voice recognition succeeded; chars=%d', len(text))
                return text
            return None
