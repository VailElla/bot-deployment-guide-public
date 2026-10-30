"""Optional standalone hook for bounded public group context."""
from astrbot.api.star import Context, Star, register

from .group_context import GroupContextGuard


@register('astrbot_plugin_group_context', 'Ella', 'Bounded QQ group context', '0.1.0')
class GroupContextPlugin(Star):
    def __init__(self, context: Context):
        super().__init__(context)
        self.guard = None

    async def initialize(self):
        if self.guard is None:
            self.guard = GroupContextGuard().install()

    async def terminate(self):
        if self.guard is not None:
            self.guard.restore()
            self.guard = None
