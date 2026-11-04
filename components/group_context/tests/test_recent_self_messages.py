import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from astrbot_plugin_group_context.recent_self_messages import recent_self_messages


class Event:
    def __init__(self, items):
        self.extra = {}
        self.created_at = 1000
        self.message_obj = SimpleNamespace(raw_message={
            'time': 1000, 'message_seq': 20, 'post_type': 'message', 'message_id': 200,
        })
        self.bot = SimpleNamespace(call_action=AsyncMock(return_value={'messages': items}))

    def get_extra(self, key): return self.extra.get(key)
    def set_extra(self, key, value): self.extra[key] = value
    def get_platform_id(self): return 'snowluma'
    def get_group_id(self): return '42'
    def get_self_id(self): return 'bot'


def history(mid, when, seq, *, group='42', sender='bot'):
    return {'message_type': 'group', 'group_id': group, 'user_id': sender,
            'self_id': 'bot', 'time': when, 'message_seq': seq, 'message_id': mid,
            'message': [{'type': 'text', 'data': {'text': f'synthetic {mid}'}}]}


class RecentSelfMessages(unittest.IsolatedAsyncioTestCase):
    async def test_filters_group_identity_time_and_reuses_event_snapshot(self):
        event = Event([history(101, 999, 19), history(102, 1000, 19),
                       history(103, 1000, 20), history(104, 1001, 21),
                       history(105, 999, 18, group='43'), history(106, 999, 18, sender='other'),
                       history(107, 300, 1)])
        first = await recent_self_messages(event, ('scope', 1, 50), 600)
        second = await recent_self_messages(event, ('scope', 1, 50), 600)
        self.assertEqual([row[1]['message_id'] for row in first], ['101', '102'])
        self.assertEqual(first, second)
        event.bot.call_action.assert_awaited_once()

    async def test_untrusted_timing_and_invalid_group_fail_closed(self):
        event = Event([history(101, 999, 19)])
        event.created_at = 1100
        self.assertEqual(await recent_self_messages(event, ('scope', 1, 50), 600), [])
        event.bot.call_action.assert_not_awaited()
        event = Event([])
        event.get_group_id = lambda: 'not-a-qq-group'
        self.assertEqual(await recent_self_messages(event, ('scope', 1, 50), 600), [])

    async def test_cancellation_propagates(self):
        event = Event([])
        event.bot.call_action = AsyncMock(side_effect=asyncio.CancelledError())
        with self.assertRaises(asyncio.CancelledError):
            await recent_self_messages(event, ('scope', 1, 50), 600)
