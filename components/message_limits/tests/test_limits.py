import asyncio
import logging
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from astrbot.api.message_components import At, Plain, Reply
from astrbot.api.platform import AstrBotMessage, MessageType, PlatformMetadata, MessageMember
from astrbot.core.platform.astr_message_event import AstrMessageEvent
from astrbot.core.message.message_event_result import MessageEventResult, ResultContentType
from astrbot_plugin_message_limits.loop_guard import LoopGuard
from astrbot_plugin_message_limits.group_followup import GroupFollowup
from astrbot_plugin_message_limits.private_limit import Limiter
from astrbot_plugin_message_limits.join_welcome import WelcomeCooldown
from astrbot_plugin_message_limits.group_delivery import GroupSendBudget, is_group_send_limit
from astrbot_plugin_message_limits.incident_aggregation import Incidents, classify


def event(sender='111', group='900', platform='test', text='hello', parts=None):
    msg = AstrBotMessage()
    msg.sender = MessageMember(user_id=sender, nickname='synthetic')
    msg.self_id = '999'
    msg.type = MessageType.GROUP_MESSAGE if group else MessageType.FRIEND_MESSAGE
    msg.group_id = group
    msg.session_id = sender + '_' + group
    msg.message = parts if parts is not None else [Plain(text)]
    msg.message_str = text
    return AstrMessageEvent(text, msg, PlatformMetadata(name='aiocqhttp', description='test', id=platform), msg.session_id)


class InboundTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name)

    def test_private_window_reload_and_scopes(self):
        limiter = Limiter(self.path / 'private.db')
        for n in range(30):
            self.assertTrue(limiter.admit(event(group=''), 1000+n))
        self.assertFalse(Limiter(self.path / 'private.db').admit(event(group=''), 1599))
        self.assertTrue(limiter.admit(event(group=''), 1600))
        self.assertTrue(limiter.admit(event(sender='222', group=''), 1600))
        self.assertTrue(limiter.admit(event(group='different'), 1600))

    def test_group_limits_repeated_event_and_rolling_expiry(self):
        guard = LoopGuard(self.path / 'group.json')
        with patch('astrbot_plugin_message_limits.loop_guard.time.time', return_value=1000):
            first = event(text='first')
            self.assertIsNone(guard.admit(first))
            self.assertIsNone(guard.admit(first))
            self.assertIsNone(guard.admit(event(text='second')))
            self.assertIsNone(guard.admit(event(text='third')))
            self.assertEqual(guard.admit(event(text='fourth')), 'sender_limit')
            for n in range(3):
                self.assertIsNone(guard.admit(event(sender=str(200+n), text=str(n))))
            self.assertEqual(guard.admit(event(sender='444', text='overflow')), 'group_limit')
            self.assertIsNone(guard.admit(event(group='901', text='other group')))
        with patch('astrbot_plugin_message_limits.loop_guard.time.time', return_value=1120):
            guard = LoopGuard(self.path / 'group.json')
            self.assertIsNone(guard.admit(event(text='fresh')))

    def test_duplicate_ignores_quote_and_whitespace(self):
        guard = LoopGuard(self.path / 'group.json')
        a = event(parts=[Reply(id='1', sender_id='999'), Plain(' Hello   world ')])
        b = event(parts=[Reply(id='2', sender_id='999'), Plain('hello world')])
        self.assertIsNone(guard.admit(a))
        self.assertEqual(guard.admit(b), 'duplicate')

    def test_followup_once_and_random_cooldown_reset(self):
        cache = GroupFollowup(self.path / 'followup.json')
        e = event(parts=[At(qq='999'), Plain('hi')])
        e.is_at_or_wake_command = True
        cache.activate(e)
        self.assertFalse(cache.allow_proactive(event(sender='222')))
        e._has_send_oper = True
        e.set_result(MessageEventResult().message('reply').set_result_content_type(ResultContentType.LLM_RESULT))
        self.assertTrue(cache.remember(e))
        cache = GroupFollowup(self.path / 'followup.json')
        self.assertTrue(cache.activate(event(text='continue')))
        self.assertFalse(cache.activate(event(text='again')))
        self.assertTrue(cache.allow_proactive(event(group='901')))
        with patch('astrbot_plugin_message_limits.group_followup.time.time', return_value=__import__('time').time()+301):
            self.assertTrue(cache.allow_proactive(event(sender='222')))

    def test_welcome_cooldown_is_idempotent_and_persistent(self):
        now = [1000]
        cooldown = WelcomeCooldown(self.path / 'welcome.db', clock=lambda: now[0])
        e = event()
        self.assertTrue(cooldown.admit(e))
        now[0] += 30
        self.assertTrue(cooldown.admit(e))
        self.assertFalse(WelcomeCooldown(self.path / 'welcome.db', clock=lambda: now[0]).admit(event()))
        self.assertTrue(cooldown.admit(event(group='901')))
        now[0] = 1120
        self.assertTrue(cooldown.admit(event()))


class OutboundTests(unittest.IsolatedAsyncioTestCase):
    async def test_round_budget_and_rejection_cooldown(self):
        now = [100.0]
        async def sleep(seconds):
            now[0] += seconds
            await asyncio.sleep(0)
        gate = GroupSendBudget(logging.getLogger('test'), clock=lambda: now[0], sleep=sleep)
        sent = []
        async def send():
            sent.append(now[0])
        for _ in range(6):
            await gate.send(('bot', 'group'), send)
        self.assertEqual(sent[:5], [100.0]*5)
        self.assertGreaterEqual(sent[-1], 160)
        async def reject():
            raise ValueError('send group message rejected: result=299 err=本群每分钟只能发5条消息')
        with self.assertRaises(ValueError):
            await gate.send(('bot', 'other'), reject)
        self.assertTrue(is_group_send_limit('send group message rejected: result=299 err=本群每分钟只能发5条消息'))
        await gate.send(('bot', 'other'), send)
        self.assertGreaterEqual(sent[-1], 221)

    async def test_incident_aggregation_does_not_mute_delivery_failure(self):
        with tempfile.TemporaryDirectory() as folder:
            incidents = Incidents(Path(folder) / 'state.json')
            incidents.add('capacity', 'provider', now=1000)
            incidents.add('delivery', 'respond.stage', now=1000)
            received = []
            async def send(text):
                received.append(text)
                return {'message_id': 'synthetic'}
            result = await incidents.deliver(send, now=1000)
            self.assertEqual(result['category'], 'delivery')
            self.assertEqual(incidents.items['capacity']['pending'], 0)
            self.assertEqual(classify(40, 'respond.stage', 'send group message rejected: result=299 err=本群每分钟只能发5条消息'), 'delivery')
            incidents.add('delivery', 'respond.stage', now=1001)
            self.assertIsNone(await incidents.deliver(send, now=1030))
            self.assertEqual(len(received), 1)

class PluginLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_framework_hooks_install_and_restore(self):
        from types import SimpleNamespace
        from astrbot_plugin_message_limits.main import MessageLimits
        from astrbot.core.pipeline.waking_check.stage import WakingCheckStage
        from astrbot.core.pipeline.process_stage.method.agent_sub_stages.internal import InternalAgentSubStage
        with tempfile.TemporaryDirectory() as folder, patch('astrbot_plugin_message_limits.main.get_astrbot_data_path', return_value=folder):
            wake, process = WakingCheckStage.process, InternalAgentSubStage.process
            plugin = MessageLimits(SimpleNamespace())
            try:
                await plugin.initialize()
                self.assertIs(WakingCheckStage.process, plugin.waking)
                self.assertIsNot(InternalAgentSubStage.process, process)
            finally:
                await plugin.terminate()
            self.assertIs(WakingCheckStage.process, wake)
            self.assertIs(InternalAgentSubStage.process, process)

    async def test_event_scope_does_not_leak_during_yield(self):
        from astrbot.core.platform.astr_message_event import current_message_event
        from astrbot_plugin_message_limits.event_scope import _scoped_stream
        original, actual = event(sender='222'), event(sender='111')
        token = current_message_event.set(original)
        try:
            async def inner():
                self.assertIs(current_message_event.get(), actual)
                yield 1
                self.assertIs(current_message_event.get(), actual)
            stream = _scoped_stream(inner(), actual)
            self.assertEqual(await anext(stream), 1)
            self.assertIs(current_message_event.get(), original)
            await stream.aclose()
            self.assertIs(current_message_event.get(), original)
        finally:
            current_message_event.reset(token)

class MoreBudgetTests(unittest.IsolatedAsyncioTestCase):
    async def test_same_reply_round_parts_and_group_independence(self):
        now = [100.0]
        async def sleep(seconds):
            now[0] += seconds
            await asyncio.sleep(0)
        gate = GroupSendBudget(logging.getLogger('test'), clock=lambda: now[0], sleep=sleep)
        sent = []
        async def call():
            sent.append(now[0])
        owner = event()
        for _ in range(8):
            await gate.send(('bot', 'group'), call, owner)
        self.assertEqual(sent, [100.0]*8)
        for _ in range(4):
            await gate.send(('bot', 'group'), call, event())
        await gate.send(('bot', 'other'), call, event(group='other'))
        self.assertEqual(sent[-1], 100.0)
        await gate.send(('bot', 'group'), call, event())
        self.assertGreaterEqual(sent[-1], 160.0)

    async def test_cancel_wait_does_not_stall_following_requests(self):
        started = asyncio.Event()
        async def sleep(_):
            started.set()
            await asyncio.Event().wait()
        gate = GroupSendBudget(logging.getLogger('test'), clock=lambda: 100.0, sleep=sleep)
        async def call():
            return 'sent'
        for _ in range(5):
            await gate.send(('bot', 'group'), call)
        waiting = asyncio.create_task(gate.send(('bot', 'group'), call))
        await started.wait()
        waiting.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await waiting
        self.assertEqual(await gate.send(('bot', 'other'), call), 'sent')
