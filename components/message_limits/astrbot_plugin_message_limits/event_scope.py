"""Bind sandbox identity to the native event at agent execution boundaries."""
from functools import wraps

from astrbot.core.platform.astr_message_event import current_message_event


async def _scoped_stream(stream, event):
    # Do not use event_context: an already-active event makes that helper skip
    # binding in a new task. Resource cleanup still belongs to the scheduler.
    # Reset before yielding so a suspended stream cannot leak its sender into
    # the caller (or a different stream advanced by that caller).
    try:
        while True:
            token = current_message_event.set(event)
            try:
                item = await anext(stream)
            except StopAsyncIteration:
                return
            finally:
                current_message_event.reset(token)
            yield item
    finally:
        token = current_message_event.set(event)
        try:
            await stream.aclose()
        finally:
            current_message_event.reset(token)


class AgentEventScope:
    def install(self):
        from astrbot.core.agent.runners.tool_loop_agent_runner import ToolLoopAgentRunner
        from astrbot.core.pipeline.process_stage.method.agent_sub_stages.internal import InternalAgentSubStage

        self.original_process = InternalAgentSubStage.process
        self.original_step = ToolLoopAgentRunner.step
        process, step = self.original_process, self.original_step

        @wraps(process)
        async def scoped_process(stage, event, *args, **kwargs):
            stream = _scoped_stream(process(stage, event, *args, **kwargs), event)
            try:
                async for item in stream:
                    yield item
            finally:
                await stream.aclose()

        @wraps(step)
        async def scoped_step(runner, *args, **kwargs):
            event = getattr(runner.run_context.context, 'event', None)
            stream = _scoped_stream(step(runner, *args, **kwargs), event)
            try:
                async for item in stream:
                    yield item
            finally:
                await stream.aclose()

        self.process, self.step = scoped_process, scoped_step
        InternalAgentSubStage.process = scoped_process
        ToolLoopAgentRunner.step = scoped_step
        return self

    def restore(self):
        from astrbot.core.agent.runners.tool_loop_agent_runner import ToolLoopAgentRunner
        from astrbot.core.pipeline.process_stage.method.agent_sub_stages.internal import InternalAgentSubStage

        if InternalAgentSubStage.process is self.process:
            InternalAgentSubStage.process = self.original_process
        if ToolLoopAgentRunner.step is self.step:
            ToolLoopAgentRunner.step = self.original_step
