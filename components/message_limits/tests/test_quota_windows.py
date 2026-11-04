"""Quota tests with isolated SQLite and synthetic OneBot events."""
import asyncio
import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from astrbot_plugin_message_limits.personal_limit import PersonalLimiter


class Event:
    def __init__(self, sender="alice", group="", platform="test", bot="bot"):
        self.sender, self.group, self.platform, self.bot = sender, group, platform, bot
        self.extra = {}

    def get_platform_name(self): return "aiocqhttp"
    def get_sender_id(self): return self.sender
    def get_group_id(self): return self.group
    def get_platform_id(self): return self.platform
    def get_self_id(self): return self.bot
    def get_extra(self, key): return self.extra.get(key)
    def set_extra(self, key, value): self.extra[key] = value


class QuotaWindows(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.limiter = PersonalLimiter(Path(self.temp.name) / "quota.sqlite3")

    async def asyncTearDown(self):
        await self.limiter.close()
        self.temp.cleanup()

    async def test_personal_short_window_crosses_group_and_private_boundaries(self):
        limiter = self.limiter
        for group in ("group-a", "group-b", ""):
            self.assertTrue(limiter.admit(Event(group=group), 1000))
        denied = Event(group="group-c")
        self.assertFalse(limiter.admit(denied, 1059.999))
        self.assertEqual(denied.get_extra('_local_codex_quota_blocked'), 'personal_limit')
        self.assertTrue(limiter.admit(Event(), 1060))
        self.assertTrue(limiter.admit(Event(sender="bob"), 1060))
        self.assertTrue(limiter.admit(Event(platform="other"), 1060))

    async def test_ten_minute_window_boundary_and_no_charge_on_denial(self):
        limiter = self.limiter
        # Two admissions each minute, so the shorter window never fills.
        for minute in range(10):
            for second in (0, 1):
                self.assertTrue(limiter.admit(Event(), 1000 + minute * 60 + second))
        denied = Event(group="group-a")
        self.assertFalse(limiter.admit(denied, 1599.9))
        self.assertEqual(denied.get_extra('_local_codex_quota_blocked'), 'personal_long_limit')
        self.assertTrue(limiter.admit(Event(), 1600))
        # The rejected group attempt did not consume any group quota.
        for number in range(5):
            self.assertTrue(limiter.admit(Event(sender=f"other-{number}", group="group-a"), 1600))

    async def test_group_rejection_does_not_spend_personal_quota(self):
        limiter = self.limiter
        for number in range(5):
            self.assertTrue(limiter.admit(Event(sender=f"member-{number}", group="room"), 1000))
        denied = Event(sender="new", group="room")
        self.assertFalse(limiter.admit(denied, 1000))
        self.assertEqual(denied.get_extra('_local_codex_quota_blocked'), 'group_limit')
        for _ in range(3):
            self.assertTrue(limiter.admit(Event(sender="new"), 1000))

    async def test_real_concurrent_transactions_enforce_the_short_limit(self):
        limiter = self.limiter
        scopes = limiter.scope(Event())
        loop = asyncio.get_running_loop()
        with ThreadPoolExecutor(max_workers=12) as pool:
            results = await asyncio.gather(*(
                loop.run_in_executor(pool, limiter.reserve, scopes, 1000)
                for _ in range(24)
            ))
        self.assertEqual(sum(allowed for allowed, _ in results), 3)
        with sqlite3.connect(limiter.path) as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM attempts').fetchone()[0], 3)

    async def test_same_event_is_counted_once_even_with_concurrent_hooks(self):
        event = Event()
        decisions = await asyncio.gather(*(self.limiter.admit_async(event, 1000) for _ in range(12)))
        self.assertTrue(all(decisions))
        with sqlite3.connect(self.limiter.path) as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM attempts').fetchone()[0], 1)

    async def test_missing_identity_is_not_coerced_to_a_shared_scope(self):
        for event in (Event(sender=None), Event(bot=None), Event(platform=None)):
            self.assertIsNone(self.limiter.scope(event))
