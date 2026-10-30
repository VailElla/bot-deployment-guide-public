"""Throttle native random QQ replies without changing explicit wake routing."""
from copy import copy
from functools import wraps

from astrbot.builtin_stars.astrbot.group_chat_context import GroupChatContext
from astrbot.core.pipeline.result_decorate.stage import ResultDecorateStage


class ProactiveGuard:
    def __init__(self, cache, logger, loop_guard=None, mute_guard=None, disabled_groups=()):
        self.cache = cache
        self.disabled_groups = {str(group) for group in disabled_groups}
        self.logger = logger
        self.loop_guard = loop_guard
        self.mute_guard = mute_guard
        self.original = GroupChatContext.need_active_reply
        self.original_decorate = ResultDecorateStage.process

        @wraps(self.original_decorate)
        async def decorate(stage, event):
            if (event.get_platform_name() == 'aiocqhttp'
                    and event.get_group_id()
                    and event.get_extra('_local_codex_proactive_reply')):
                # Stages are shared by concurrent events; override this event only.
                stage = copy(stage)
                stage.reply_with_quote = False
            stream = self.original_decorate(stage, event)
            try:
                async for item in stream:
                    yield item
            finally:
                await stream.aclose()

        decorate._local_codex_proactive_quote = self
        self.decorate = decorate

        async def guarded(context, event):
            if (event.get_platform_name() != 'aiocqhttp'
                    or not event.get_group_id()):
                return await self.original(context, event)
            if str(event.get_group_id()) in self.disabled_groups:
                return False
            if self.mute_guard and self.mute_guard.muted(event.get_platform_id(), event.get_group_id()):
                return False
            if not self.cache.allow_proactive(event):
                return False
            if self.loop_guard and not self.loop_guard.allow_proactive(event):
                return False
            chosen = await self.original(context, event)
            if chosen:
                # Recheck after the await, before synchronously reserving the group.
                if not self.cache.allow_proactive(event):
                    return False
                if self.loop_guard:
                    reason = self.loop_guard.protect(event)
                    if reason:
                        self.logger.info('QQ loop guard blocked reply: reason=%s', reason)
                        return False
                event.set_extra('_local_codex_proactive_reply', True)
                # Reserve before model queuing so a burst cannot enqueue several
                # random replies while the first one is still generating.
                try:
                    self.cache.reserve_proactive(event)
                except OSError as exc:
                    self.logger.warning('Random-reply cooldown persistence failed: %s', type(exc).__name__)
                self.logger.info('QQ reply trigger=proactive; cooldown_scope=group; cooldown_seconds=%s', self.cache.proactive_ttl)
            return chosen

        self.wrapper = guarded

    def install(self):
        GroupChatContext.need_active_reply = self.wrapper
        ResultDecorateStage.process = self.decorate
        self.logger.info('QQ random replies automatic_quote=False')
        return self

    def restore(self):
        if GroupChatContext.need_active_reply is self.wrapper:
            GroupChatContext.need_active_reply = self.original
        if ResultDecorateStage.process is self.decorate:
            ResultDecorateStage.process = self.original_decorate
