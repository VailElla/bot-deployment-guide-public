"""Standalone AstrBot message-rate guards, adapted from the Bot deployment."""
import json
import sqlite3
import sys
from pathlib import Path
from collections.abc import Mapping

from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register
from astrbot.core.pipeline.waking_check.stage import WakingCheckStage
from astrbot.core.utils.astrbot_path import get_astrbot_data_path

from .private_limit import Limiter
from .group_followup import GroupFollowup
from .loop_guard import LoopGuard
from .proactive_guard import ProactiveGuard
from .join_welcome import WelcomeCooldown
from .group_delivery import GroupDelivery
from .reply_pacing import ReplyPacing
from .event_scope import AgentEventScope


@register('astrbot_plugin_message_limits', 'Ella', 'QQ message rate and delivery limits', '0.1.0')
class MessageLimits(Star):
    def __init__(self, context: Context):
        super().__init__(context)
        root = Path(get_astrbot_data_path()) / 'plugin_data/message_limits'
        root.mkdir(parents=True, exist_ok=True)
        self.root = root
        self.followup = GroupFollowup(root / 'followup.json')
        self.loop = LoopGuard(root / 'group_limits.json')
        self.private = Limiter(root / 'private.sqlite3')
        self.welcome = WelcomeCooldown(root / 'welcome.sqlite3')
        self.active = False

    async def initialize(self):
        settings = self.root / 'settings.json'
        if not settings.exists():
            try:
                with settings.open('x', encoding='utf-8') as f:
                    json.dump({'enabled': True, 'disable_random_groups': [], 'reply_pacing': True}, f)
            except FileExistsError:
                pass
        config = json.loads(settings.read_text(encoding='utf-8-sig'))
        disabled = config.get('disable_random_groups', [])
        if not isinstance(disabled, list) or not all(isinstance(x, str) for x in disabled):
            raise ValueError('disable_random_groups must be a list of group ID strings')
        self.active = True
        previous = WakingCheckStage.process
        self.previous = previous

        async def waking(stage, event):
            if not self.active:
                return await previous(stage, event)
            # Retry of one in-process event must not consume another private slot.
            admitted = event.get_extra('_message_limits_private_admitted')
            if admitted is None:
                admitted = self.private.admit(event)
                event.set_extra('_message_limits_private_admitted', admitted)
            if not admitted:
                event.stop_event()
                return
            raw = getattr(event.message_obj, 'raw_message', None)
            join = (event.get_platform_name() == 'aiocqhttp' and event.get_group_id()
                    and event.get_self_id() and event.get_sender_id()
                    and str(event.get_sender_id()) != str(event.get_self_id())
                    and isinstance(raw, Mapping) and raw.get('post_type') == 'notice'
                    and raw.get('notice_type') == 'group_increase'
                    and raw.get('sub_type') in ('approve', 'invite'))
            if join:
                try:
                    welcome_allowed = self.welcome.admit(event)
                except (OSError, sqlite3.Error):
                    self.logger.exception('Welcome cooldown storage failed')
                    welcome_allowed = False
                if not welcome_allowed:
                    event.stop_event()
                    return
            return await previous(stage, event)

        self.waking = waking
        WakingCheckStage.process = waking
        try:
            self.proactive = ProactiveGuard(self.followup, self.logger, self.loop,
                                            disabled_groups=disabled).install()
            self.pacing = ReplyPacing(self.logger).install() if config.get('reply_pacing') is True else None
            self.event_scope = AgentEventScope().install()
            self.delivery = GroupDelivery(settings, self.logger).install()
        except BaseException:
            await self.terminate()
            raise

    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE, priority=sys.maxsize)
    async def continue_group_chat(self, event: AstrMessageEvent):
        if self.loop.blocked_reason(event):
            event.stop_event()
            return
        self.followup.activate(event)
        if event.is_at_or_wake_command:
            self.loop.protect(event)

    @filter.after_message_sent()
    async def remember_group_reply(self, event: AstrMessageEvent):
        try:
            self.loop.remember(event)
            self.followup.remember(event)
        except OSError:
            self.logger.exception('Message limit state could not be persisted')

    async def terminate(self):
        self.active = False
        if WakingCheckStage.process is getattr(self, 'waking', None):
            WakingCheckStage.process = self.previous
        if getattr(self, 'delivery', None):
            await self.delivery.close()
        if getattr(self, 'event_scope', None):
            self.event_scope.restore()
        if getattr(self, 'pacing', None):
            self.pacing.restore()
        if getattr(self, 'proactive', None):
            self.proactive.restore()
