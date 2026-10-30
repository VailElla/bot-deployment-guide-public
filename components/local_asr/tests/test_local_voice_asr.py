import asyncio
import base64
import io
import hashlib
import json
import logging
import sys
import tempfile
import unittest
import wave
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from astrbot_plugin_local_codex.local_voice_asr import LocalVoiceASR, MAX_WAV_BYTES
from astrbot_plugin_local_codex import qwen_runtime


def encoded_wav(seconds=1):
    stream = io.BytesIO()
    with wave.open(stream, "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(16000)
        output.writeframes(b"\0\0" * (16000 * seconds))
    return base64.b64encode(stream.getvalue()).decode()


class LocalVoiceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        model=self.root/'model.gguf';model.write_bytes(b'model')
        engine=self.root/'qwen3-asr-cli.exe';engine.write_bytes(b'engine')
        ffmpeg=self.root/'ffmpeg.exe';ffmpeg.write_bytes(b'ffmpeg')
        self.manifest=self.root/'runtime.json'
        self.manifest.write_text(json.dumps({'model':str(model),'model_sha256':hashlib.sha256(model.read_bytes()).hexdigest(),'engine_files':{str(engine):hashlib.sha256(engine.read_bytes()).hexdigest()},'ffmpeg':str(ffmpeg),'lock_file':str(self.root/'shared.lock')}))
        self.cfg = self.root / "asr.json"
        self.cfg.write_text(json.dumps({"enable": True, "backend":"qwen3_asr_gguf", "runtime_manifest":str(self.manifest), "timeout":1}))
        self.engine = LocalVoiceASR(logging.getLogger("local-voice-test"), self.cfg)
        self.event = SimpleNamespace(bot=SimpleNamespace(call_action=AsyncMock(return_value={"base64": encoded_wav()})))
        self.record = SimpleNamespace(file="test.amr", url="")

    async def test_unconfigured_fallback_does_not_touch_audio(self):
        engine = LocalVoiceASR(logging.getLogger("test"), self.root / "missing.json")
        self.assertIsNone(await engine.transcribe(self.event, self.record))
        self.event.bot.call_action.assert_not_awaited()

    async def test_shared_runtime_routing_and_temp_audio_cleanup(self):
        captured = []
        async def recognize(wav, manifest, run, **kwargs):
            captured.append(Path(wav))
            self.assertTrue(Path(wav).exists())
            self.assertEqual(kwargs['language'],'auto')
            self.assertEqual(kwargs['max_seconds'],300)
            return 'hello'
        with patch.object(qwen_runtime,'transcribe_file',side_effect=recognize):
            self.assertEqual(await self.engine.transcribe(self.event,self.record),'hello')
        self.event.bot.call_action.assert_awaited_once_with('get_record',file='test.amr',out_format='wav')
        self.assertFalse(captured[0].exists())

    async def test_invalid_or_oversize_audio_never_starts_child(self):
        for result in [None, {"base64": "not base64"}, {"base64": ""}, {"base64": "a" * (((MAX_WAV_BYTES + 2) // 3 * 4) + 1)}, {"base64": encoded_wav(301)}]:
            self.event.bot.call_action.return_value = result
            with patch("asyncio.create_subprocess_exec", new=AsyncMock()) as launch:
                self.assertIsNone(await self.engine.transcribe(self.event, self.record))
                launch.assert_not_awaited()

    async def test_timeout_cleans_audio_and_preserves_deadline(self):
        wavs=[]
        async def slow(wav,*args,**kwargs):
            wavs.append(Path(wav));await asyncio.sleep(60)
        with patch.object(qwen_runtime,'transcribe_file',side_effect=slow):
            self.assertIsNone(await self.engine.transcribe(self.event,self.record))
        self.assertFalse(wavs[0].exists())

    async def test_cancellation_propagates_and_cleans_audio(self):
        wavs=[]
        async def cancel(wav,*args,**kwargs):
            wavs.append(Path(wav));raise asyncio.CancelledError
        with patch.object(qwen_runtime,'transcribe_file',side_effect=cancel):
            with self.assertRaises(asyncio.CancelledError):await self.engine.transcribe(self.event,self.record)
        self.assertFalse(wavs[0].exists())

    async def test_runtime_tampering_prevents_decoding(self):
        with patch.object(qwen_runtime,'transcribe_file',new=AsyncMock(return_value='safe')):
            self.assertEqual(await self.engine.transcribe(self.event,self.record),'safe')
        self.event.bot.call_action.reset_mock()
        (self.root/'model.gguf').write_bytes(b'changed model')
        self.assertIsNone(await self.engine.transcribe(self.event,self.record))
        self.event.bot.call_action.assert_not_awaited()

    async def test_lock_wait_uses_same_deadline(self):
        await self.engine.lock.acquire()
        try:
            self.assertIsNone(await self.engine.transcribe(self.event, self.record))
            self.event.bot.call_action.assert_not_awaited()
        finally:
            self.engine.lock.release()

    async def test_invalid_config_and_empty_transcript(self):
        self.cfg.write_text('{"enable":true,"python":"relative.exe","model":"relative"}')
        self.assertIsNone(await self.engine.transcribe(self.event, self.record))
        self.event.bot.call_action.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
