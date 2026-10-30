import asyncio
import copy
import json
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from astrbot.api.message_components import Plain, Reply
from astrbot.api.platform import AstrBotMessage, MessageMember, MessageType, PlatformMetadata
from astrbot.api.provider import ProviderRequest
from astrbot.builtin_stars.astrbot.group_chat_context import GroupChatContext
from astrbot.core.config.default import DEFAULT_CONFIG
from astrbot.core.platform.astr_message_event import AstrMessageEvent
from astrbot_plugin_group_context.group_context import GroupContextGuard


def event(text, sender='u1', group='g1', mid='m1', parts=None):
    msg = AstrBotMessage()
    msg.sender = MessageMember(user_id=sender, nickname='synthetic')
    msg.self_id = 'bot'
    msg.type = MessageType.GROUP_MESSAGE
    msg.group_id = group
    msg.session_id = sender + '_' + group
    msg.message_id = mid
    msg.message = parts if parts is not None else [Plain(text)]
    msg.message_str = text
    return AstrMessageEvent(text, msg, PlatformMetadata(name='aiocqhttp', description='test', id='test'), msg.session_id)


class ContextTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        if hasattr(GroupChatContext, '_local_codex_public_state'):
            delattr(GroupChatContext, '_local_codex_public_state')
        cfg = copy.deepcopy(DEFAULT_CONFIG)
        cfg['provider_ltm_settings'].update(group_icl_enable=True, image_caption=False)
        self.native = GroupChatContext(None, SimpleNamespace(get_config=lambda **kw: cfg))
        self.guard = GroupContextGuard().install()
        self.addCleanup(self.guard.restore)

    async def request(self, ev):
        req = ProviderRequest(prompt=ev.message_str, contexts=[])
        await self.native.on_req_llm(ev, req)
        return json.loads(req.extra_user_content_parts[-1].text)

    async def test_later_events_and_other_groups_do_not_enter_snapshot(self):
        prior = event('prior text', mid='before')
        current = event('current text', sender='u2', mid='current')
        await self.native.handle_message(prior)
        await self.native.handle_message(current)
        await self.native.handle_message(event('future text', mid='future'))
        await self.native.handle_message(event('other group', group='g2', mid='other'))
        records = (await self.request(current))['recent_group_context']
        self.assertEqual([r['message_id'] for r in records], ['before'])

    async def test_quote_from_other_sender_is_not_reassigned(self):
        await self.native.handle_message(event('original task', sender='u2', mid='origin'))
        current = event('continue', sender='u1', mid='follow', parts=[
            Reply(id='bot-reply', sender_id='bot', chain=[Reply(id='origin')]), Plain('continue')])
        await self.native.handle_message(current)
        block = await self.request(current)
        self.assertEqual(block['current_group_turn']['sender_id'], 'u1')
        self.assertEqual(block['current_quoted_thread'][0]['sender_id'], 'u2')
        self.assertEqual(block['current_quoted_thread'][0]['message_id'], 'origin')

    async def test_pending_formatting_keeps_capture_order_and_does_not_pull_future(self):
        started, proceed = asyncio.Event(), asyncio.Event()
        original = self.native._format_message
        async def delayed(ev, cfg):
            if ev.message_obj.message_id == 'slow':
                started.set()
                await proceed.wait()
            return await original(ev, cfg)
        self.native._format_message = delayed
        first = asyncio.create_task(self.native.handle_message(event('slow', mid='slow')))
        await started.wait()
        current = event('trigger', sender='u2', mid='trigger')
        await self.native.handle_message(current)
        await self.native.handle_message(event('later', mid='later'))
        proceed.set()
        await first
        block = await self.request(current)
        self.assertEqual([r['message_id'] for r in block['recent_group_context']], ['slow'])

    async def test_restore_does_not_overwrite_later_wrapper(self):
        wrapped = GroupChatContext.handle_message
        async def later(context, ev):
            return await wrapped(context, ev)
        GroupChatContext.handle_message = later
        self.guard.restore()
        self.assertIs(GroupChatContext.handle_message, later)
        try:
            await self.native.handle_message(event('ignored after restore'))
            self.assertEqual(len(self.guard.groups), 0)
        finally:
            GroupChatContext.handle_message = self.guard.original_handle


class PluginLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_plugin_installs_once_and_restores_hooks(self):
        from astrbot_plugin_group_context.main import GroupContextPlugin
        original_handle, original_request = GroupChatContext.handle_message, GroupChatContext.on_req_llm
        plugin = GroupContextPlugin(SimpleNamespace())
        try:
            await plugin.initialize()
            guard = plugin.guard
            await plugin.initialize()
            self.assertIs(plugin.guard, guard)
            self.assertIs(GroupChatContext.handle_message, guard.capture)
        finally:
            await plugin.terminate()
        self.assertIs(GroupChatContext.handle_message, original_handle)
        self.assertIs(GroupChatContext.on_req_llm, original_request)

    async def test_context_and_delivery_plugins_can_install_together(self):
        from astrbot_plugin_group_context.main import GroupContextPlugin
        from astrbot_plugin_message_limits.main import MessageLimits
        from astrbot.core.pipeline.process_stage.method.agent_sub_stages.internal import InternalAgentSubStage
        original_capture = GroupChatContext.handle_message
        original_process = InternalAgentSubStage.process
        with tempfile.TemporaryDirectory() as folder, patch(
            'astrbot_plugin_message_limits.main.get_astrbot_data_path', return_value=folder
        ):
            context_plugin = GroupContextPlugin(SimpleNamespace())
            limits_plugin = MessageLimits(SimpleNamespace())
            try:
                await context_plugin.initialize()
                await limits_plugin.initialize()
                self.assertIs(GroupChatContext.handle_message, context_plugin.guard.capture)
                self.assertIsNot(InternalAgentSubStage.process, original_process)
            finally:
                await limits_plugin.terminate()
                await context_plugin.terminate()
        self.assertIs(GroupChatContext.handle_message, original_capture)
        self.assertIs(InternalAgentSubStage.process, original_process)


if __name__ == '__main__':
    unittest.main()
