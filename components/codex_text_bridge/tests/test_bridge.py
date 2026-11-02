import asyncio
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

from astrbot_plugin_codex_text_bridge.runner import BridgeError, CodexRunner, Job, Result, build_prompt, parse_events
from astrbot_plugin_codex_text_bridge.provider import CodexTextProvider


FAKE_CLI = r'''
import json, sys, time
payload = json.loads(sys.stdin.read())
if payload['messages'][-1]['content'] == 'wait':
    time.sleep(30)
flags = '--ignore-user-config' in sys.argv and '--ephemeral' in sys.argv and '--sandbox' in sys.argv
print(json.dumps({'type':'thread.started','thread_id':'synthetic'}))
print(json.dumps({'type':'item.completed','item':{'type':'agent_message','text':'ok' if flags else 'missing flags'}}))
print(json.dumps({'type':'turn.completed','usage':{'input_tokens':10,'cached_input_tokens':2,'output_tokens':3}}))
'''


class PromptTests(unittest.TestCase):
    def test_context_is_bounded_by_whole_turn_and_preserves_current(self):
        contexts = [
            {'role': 'system', 'content': 'context policy'},
            {'role': 'user', 'content': 'old ' * 100},
            {'role': 'assistant', 'content': 'old answer'},
            {'role': 'user', 'content': 'recent'},
            {'role': 'assistant', 'content': 'recent answer'},
        ]
        out = json.loads(build_prompt(contexts, 'system', 'current', limit=260))['messages']
        self.assertEqual([m['content'] for m in out], ['context policy', 'recent', 'recent answer', 'current'])
        self.assertEqual(contexts[1]['content'], 'old ' * 100)

    def test_assembled_turn_and_non_text_rejected(self):
        context = [{'role': 'user', 'content': 'question'}]
        out = json.loads(build_prompt(context, '', None, [{'type': 'text', 'text': 'extra'}]))
        self.assertEqual(out['messages'], [{'role': 'user', 'content': 'question\nextra'}])
        with self.assertRaises(BridgeError):
            build_prompt([{'role': 'user', 'content': [{'type': 'image_url', 'image_url': 'x'}]}], '', None)
        with self.assertRaises(BridgeError):
            build_prompt([{'role': 'tool', 'content': 'data'}], '', None)
        with self.assertRaises(BridgeError):
            build_prompt([], '', 'x' * 300, limit=100)

    def test_event_parser_requires_completed_answer_and_does_not_echo_errors(self):
        for raw, code in [('', 0), ('{"type":"turn.completed"}', 0), ('{"type":"error","message":"private details"}', 1)]:
            with self.assertRaises(BridgeError) as caught:
                parse_events(raw, code)
            self.assertNotIn('private details', str(caught.exception))


class RunnerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.script = root / 'fake_cli.py'
        self.script.write_text(FAKE_CLI, encoding='utf-8')
        self.runner = CodexRunner([sys.executable, str(self.script)], root / 'work')

    async def test_real_subprocess_receives_stdin_and_returns_usage(self):
        result = await asyncio.to_thread(self.runner.run, build_prompt([], '', 'hello'), '', Job())
        self.assertEqual(result.text, 'ok')
        self.assertEqual(result.usage['input_tokens'], 10)
        self.assertEqual(list(self.runner.workspace.iterdir()), [])

    async def test_cancellation_kills_and_joins_real_subprocess(self):
        job = Job()
        task = asyncio.create_task(asyncio.to_thread(self.runner.run, build_prompt([], '', 'wait'), '', job))
        for _ in range(100):
            if job.process is not None:
                break
            await asyncio.sleep(.01)
        self.assertIsNotNone(job.process)
        proc = job.process
        await asyncio.to_thread(job.cancel)
        with self.assertRaises(BridgeError):
            await asyncio.wait_for(task, timeout=5)
        self.assertIsNotNone(proc.poll())
        self.assertEqual(list(self.runner.workspace.iterdir()), [])


class ProviderTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        config = {'id': 'test', 'type': 'codex_text_bridge', 'codex_executable': sys.executable,
                  'workspace': self.tmp.name, 'model': 'default', 'timeout': 2}
        self.providers = [CodexTextProvider(config, {}) for _ in range(5)]

    async def test_provider_round_trip_through_fake_cli(self):
        provider = self.providers[0]
        script = Path(self.tmp.name) / 'fake_cli.py'
        script.write_text(FAKE_CLI, encoding='utf-8')
        provider.runner.command = [sys.executable, str(script)]
        result = await provider.text_chat(prompt='synthetic message')
        self.assertEqual(result.completion_text, 'ok')
        self.assertEqual(result.usage.input_other, 8)
        self.assertEqual(result.usage.input_cached, 2)

    async def test_two_instances_share_four_slots(self):
        lock, release = threading.Lock(), threading.Event()
        active, peak, started = [0], [0], [0]
        class BlockingRunner:
            def run(self, payload, instructions, job, model):
                with lock:
                    active[0] += 1
                    started[0] += 1
                    peak[0] = max(peak[0], active[0])
                release.wait(2)
                with lock:
                    active[0] -= 1
                return Result('ok', {}, 'test')
        for provider in self.providers:
            provider.runner = BlockingRunner()
        tasks = [asyncio.create_task(p.text_chat(prompt='hello')) for p in self.providers]
        try:
            for _ in range(100):
                if started[0] >= 4:
                    break
                await asyncio.sleep(.01)
            self.assertEqual(started[0], 4)
            self.assertEqual(peak[0], 4)
        finally:
            release.set()
            await asyncio.gather(*tasks)
        self.assertEqual(started[0], 5)
        self.assertEqual(peak[0], 4)

    async def test_timeout_waits_for_cancel_before_retry(self):
        provider = self.providers[0]
        provider.timeout = .05
        started, finished = [], []
        class SlowRunner:
            def run(self, payload, instructions, job, model):
                started.append(len(finished))
                job.cancelled.wait(2)
                finished.append(True)
                raise BridgeError('cancelled')
        provider.runner = SlowRunner()
        with self.assertRaisesRegex(BridgeError, '两次超时'):
            await provider.text_chat(prompt='hello')
        self.assertEqual(started, [0, 1])
        self.assertEqual(len(finished), 2)
        self.assertFalse(provider._jobs)

    async def test_unsupported_tools_are_rejected_and_stream_returns_final(self):
        provider = self.providers[0]
        with self.assertRaises(BridgeError):
            await provider.text_chat(prompt='hello', image_urls=['image'])
        with self.assertRaises(BridgeError):
            await provider.text_chat(prompt='hello', func_tool=object())
        provider.runner.run = lambda *args: Result('reply', {'input_tokens': 7, 'cached_input_tokens': 3}, 'x')
        parts = [part async for part in provider.text_chat_stream(prompt='hello')]
        self.assertTrue(parts[0].is_chunk)
        self.assertEqual(parts[-1].completion_text, 'reply')
        self.assertEqual(parts[-1].usage.input_other, 4)


if __name__ == '__main__':
    unittest.main()
