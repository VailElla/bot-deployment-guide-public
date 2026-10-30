"""Remove the initial delay from segmented QQ model replies."""
import random
from contextvars import ContextVar
from functools import wraps

from astrbot.core.message.components import Plain, Record
from astrbot.core.pipeline.respond.stage import RespondStage


class ReplyPacing:
    def __init__(self, logger):
        self.logger = logger
        self.original_process = RespondStage.process
        self.original_interval = RespondStage._calc_comp_interval
        index = ContextVar('local_codex_reply_segment_index', default=None)

        @wraps(self.original_process)
        async def process(stage, event):
            result = event.get_result()
            enabled = (event.get_platform_id() == 'snowluma'
                       and event.get_platform_name() == 'aiocqhttp'
                       and result is not None and result.is_model_result()
                       and any(isinstance(c, Plain) for c in result.chain)
                       and not any(isinstance(c, Record) for c in result.chain))
            token = index.set(0 if enabled else None)
            try:
                return await self.original_process(stage, event)
            finally:
                index.reset(token)

        async def interval(stage, component):
            current = index.get()
            if current is None:
                return await self.original_interval(stage, component)
            index.set(current + 1)
            return 0.0 if current == 0 else random.uniform(.4, .8)

        process._local_codex_reply_pacing = self
        self.process = process
        self.interval = interval

    def install(self):
        existing = getattr(RespondStage.process, '_local_codex_reply_pacing', None)
        if existing is not None:
            return existing
        RespondStage.process = self.process
        RespondStage._calc_comp_interval = self.interval
        self.logger.info('[local_codex] reply_pacing first_delay=0 subsequent_interval=0.4,0.8 scope=snowluma_model_text')
        return self

    def restore(self):
        if RespondStage.process is self.process:
            RespondStage.process = self.original_process
        if RespondStage._calc_comp_interval is self.interval:
            RespondStage._calc_comp_interval = self.original_interval
