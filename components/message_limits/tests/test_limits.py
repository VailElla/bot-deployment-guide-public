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
from astrbot_plugin_message_limits.personal_limit import PersonalLimiter
from astrbot_plugin_message_limits.join_welcome import WelcomeCooldown
from astrbot_plugin_message_limits.group_delivery import GroupSendBudget, is_group_send_limit, send_with_voice_upload_fallback
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

    def test_personal_window_is_shared_between_groups_and_private(self):
        limiter = PersonalLimiter(self.path / 'personal.db')
        for n in range(3):
            self.assertTrue(limiter.admit(event(group=f'g{n}'), 1000))
        denied = event(group='')
        self.assertFalse(limiter.admit(denied, 1000))
        self.assertEqual(denied.get_extra('_local_codex_quota_blocked'), 'personal_limit')
        self.assertTrue(limiter.admit(event(group=''), 1060))

    def test_group_window_does_not_charge_rejected_personal_attempt(self):
        limiter = PersonalLimiter(self.path / 'personal.db')
        for n in range(5):
            self.assertTrue(limiter.admit(event(sender=str(200+n)), 1000))
        denied = event(sender='other')
        self.assertFalse(limiter.admit(denied, 1000))
        self.assertEqual(denied.get_extra('_local_codex_quota_blocked'), 'group_limit')
        self.assertTrue(limiter.admit(event(sender='other', group=''), 1000))

    def test_group_routing_deduplicates_without_spending_model_quota(self):
        guard = LoopGuard(self.path / 'group.json')
        with patch('astrbot_plugin_message_limits.loop_guard.time.time', return_value=1000):
            first = event(text='first')
            self.assertIsNone(guard.admit(first))
            self.assertIsNone(guard.admit(first))
            self.assertIsNone(guard.admit(event(text='second')))
            self.assertIsNone(guard.admit(event(text='third')))
            self.assertIsNone(guard.admit(event(text='fourth')))
            for n in range(3):
                self.assertIsNone(guard.admit(event(sender=str(200+n), text=str(n))))
            self.assertIsNone(guard.admit(event(sender='444', text='overflow')))
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
    async def test_voice_upload_fallback_requires_matching_event_markers(self):
        from aiocqhttp.exceptions import ActionFailed
        from astrbot.api.message_components import Record
        from astrbot.api.event import MessageChain
        owner = event(platform='snowluma')
        record = Record(file='synthetic.wav', url='synthetic.wav', text='synthetic text')
        owner.set_extra('_voice_whole_voice', True)
        owner.set_extra('_voice_seed_record', record)
        calls = []
        async def send(chain):
            calls.append(chain)
            if len(calls) == 1:
                raise ActionFailed({'retcode': 1, 'wording': 'httpconn response body missing'})
            return 'ok'
        self.assertEqual(await send_with_voice_upload_fallback(send, MessageChain([record]), owner, logging.getLogger('test')), 'ok')
        self.assertEqual(calls[1].chain[0].text, 'synthetic text')
        self.assertIsNone(owner.get_extra('_voice_seed_record'))

    async def test_immediate_sending_and_no_automatic_retry(self):
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
        self.assertEqual(sent, [100.0]*6)
        async def reject():
            raise ValueError('send group message rejected: result=299 err=本群每分钟只能发5条消息')
        with self.assertRaises(ValueError):
            await gate.send(('bot', 'other'), reject)
        self.assertTrue(is_group_send_limit('send group message rejected: result=299 err=本群每分钟只能发5条消息'))
        await gate.send(('bot', 'other'), send)
        self.assertEqual(sent[-1], 100.0)

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
    async def test_async_admission_is_idempotent_and_hook_stops_sixth(self):
        from types import SimpleNamespace
        from astrbot_plugin_message_limits.main import MessageLimits
        with tempfile.TemporaryDirectory() as folder, patch('astrbot_plugin_message_limits.main.get_astrbot_data_path', return_value=folder):
            plugin = MessageLimits(SimpleNamespace())
            try:
                await plugin.initialize()
                first = event(sender='first', group='room')
                await asyncio.gather(*(plugin.enforce_model_request(first, None) for _ in range(3)))
                self.assertFalse(first.is_stopped())
                for n in range(4):
                    admitted = event(sender=f'user{n}', group='room')
                    await plugin.enforce_model_request(admitted, None)
                    self.assertFalse(admitted.is_stopped())
                denied = event(sender='sixth', group='room')
                await plugin.enforce_model_request(denied, None)
                self.assertTrue(denied.is_stopped())
                self.assertEqual(denied.get_extra('_local_codex_quota_blocked'), 'group_limit')
            finally:
                await plugin.terminate()

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
        self.assertEqual(sent[-1], 100.0)

    async def test_cancelled_send_does_not_stall_following_requests(self):
        started = asyncio.Event()
        gate = GroupSendBudget(logging.getLogger('test'))
        async def blocked():
            started.set()
            await asyncio.Event().wait()
        async def call():
            return 'sent'
        waiting = asyncio.create_task(gate.send(('bot', 'group'), blocked))
        await started.wait()
        waiting.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await waiting
        self.assertEqual(await gate.send(('bot', 'other'), call), 'sent')
