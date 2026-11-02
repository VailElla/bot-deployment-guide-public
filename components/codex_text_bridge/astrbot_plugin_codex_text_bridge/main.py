"""Register the provider when AstrBot loads this plugin."""
from astrbot.api.star import Context, Star, register
from astrbot.core.provider.register import provider_cls_map

# AstrBot keeps provider registrations across a plugin hot reload. Importing the
# adapter a second time would otherwise fail with a duplicate-type exception.
if "codex_text_bridge" not in provider_cls_map:
    from .provider import CodexTextProvider  # noqa: F401


@register("astrbot_plugin_codex_text_bridge", "Bot Deployment Guide", "Codex CLI 文本桥接", "0.1.0")
class CodexTextBridge(Star):
    def __init__(self, context: Context):
        super().__init__(context)
