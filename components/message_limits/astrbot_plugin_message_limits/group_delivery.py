"""Save group model history before ordered speech and reply delivery."""
import asyncio
from collections import OrderedDict
import copy
from functools import wraps
import hashlib
import json
import re
import time
import uuid
import weakref

ACTIVE = '_group_delivery_active'
TICKET = '_group_delivery_ticket'
NOTICE = '[群回复交付状态]'


def is_group_send_limit(error):
    """Match QQ's explicit group quota rejection, never generic rate limits."""
    return bool(re.search(
        r'send group message rejected:\s*result=299\b.*?err=本群每分钟只能发\d+条消息',
        str(error), re.S))


async def send_with_voice_upload_fallback(send, message_chain, owner, logger):
    """Recover an explicitly rejected QQ voice upload with its original text."""
    try:
        return await send(message_chain)
    except Exception as exc:
        from aiocqhttp.exceptions import ActionFailed
        from astrbot.api.message_components import Plain, Record
        from astrbot.api.event import MessageChain

        parts = getattr(message_chain, 'chain', ())
        detail = str(exc).lower()
        generated = owner.get_extra('_aemeath_seed_record') if owner is not None else None
        pre_upload_failure = (
            ('highway upload transport failed after' in detail
             and 'current chunk unconfirmed' in detail)
            or "wording='httpconn response body missing'" in detail
        )
        eligible = (isinstance(exc, ActionFailed) and owner is not None
                    and owner.get_platform_id() == 'snowluma'
                    and owner.get_extra('_aemeath_whole_voice')
                    and len(parts) == 1 and isinstance(parts[0], Record)
                    and isinstance(generated, Record)
                    and generated.file == parts[0].file and generated.text == parts[0].text
                    and isinstance(parts[0].text, str) and parts[0].text.strip()
                    and pre_upload_failure)
        if not eligible:
            raise
        # Dual-output mode already sends the same text in the remaining chain.
        result = owner.get_result()
        if result and any(isinstance(p, Plain) and p.text == parts[0].text
                          for p in result.chain):
            raise
        logger.warning('[group_delivery] QQ voice upload rejected; sending original text')
        sent = await send(MessageChain([Plain(parts[0].text)]))
        # The text was delivered, but the synthesized audio was not.
        owner.set_extra('_aemeath_seed_record', None)
        return sent


class GroupSendBudget:
    """Admit five reply events per rolling minute, regardless of part count."""
    mode = "reply_round_v1"
    def __init__(self, logger, clock=time.monotonic, sleep=asyncio.sleep):
        self.logger, self.clock, self.sleep = logger, clock, sleep
        self.states = weakref.WeakKeyDictionary()
        # Reply rounds are counted from activation; actual QQ rejection has its own cooldown.
        self.warm_until = 0

    async def send(self, key, call, owner=None, needed=1):
        states = self.states.setdefault(asyncio.get_running_loop(), {})
        state = states.setdefault(key, {'lock': asyncio.Lock(), 'times': [],
                                        'until': self.warm_until})
        async with state['lock']:
            while True:
                if owner is not None and GroupDelivery.stopped(owner):
                    raise asyncio.CancelledError()
                now = self.clock()
                state['times'][:] = [t for t in state['times'] if now-t < 60]
                delay = max(0, state['until']-now)
                admitted = owner.get_extra('_qq_group_reply_admitted') if owner is not None else None
                same_round = admitted is not None and key in admitted
                if not same_round and len(state['times']) >= 5:
                    delay = max(delay, state['times'][0]+60-now)
                if delay <= 0:
                    break
                # Keep waits cancellable and recheck event stop flags.
                await self.sleep(min(delay, .5))
            # One event owns all parts, even if its image finishes much later.
            # Failed attempts consume the round because delivery may be ambiguous.
            if not same_round:
                state['times'].append(self.clock())
                if owner is not None:
                    admitted = set(admitted or ())
                    admitted.add(key)
                    owner.set_extra('_qq_group_reply_admitted', admitted)
            try:
                return await call()
            except Exception as exc:
                if is_group_send_limit(exc):
                    state['until'] = self.clock() + 61
                    if owner is not None:
                        owner.set_extra('_qq_group_rate_rejected', exc)
                    self.logger.info('[group_delivery] QQ group quota rejected; cooldown=61')
                raise


class GroupBudgetBot:
    """Share reply admission across actual sends, including split/forward chains."""
    def __init__(self, bot, budget, account, group, owner):
        self.bot, self.budget = bot, budget
        self.account, self.group, self.owner = account, str(group), owner

    def __getattr__(self, name):
        return getattr(self.bot, name)

    async def _send(self, call, params):
        group = str(params.get('group_id', self.group))
        account = str(params.get('self_id', self.account))
        owner = self.owner if group == self.group else None
        if owner is not None:
            rejected = owner.get_extra('_qq_group_rate_rejected')
            if rejected is not None:
                raise rejected  # Do not send an orphan sticker after failed speech.
        return await self.budget.send((account, group), call, owner)

    async def send_group_msg(self, **params):
        return await self._send(lambda: self.bot.send_group_msg(**params), params)

    async def call_action(self, action, **params):
        if action in {'send_group_msg', 'send_group_forward_msg', 'upload_group_file'}:
            return await self._send(lambda: self.bot.call_action(action, **params), params)
        return await self.bot.call_action(action, **params)


class GroupDelivery:
    def __init__(self, settings, logger):
        self.settings = settings
        self.logger = logger
        self.loops = weakref.WeakKeyDictionary()
        self.runs = set()
        self.closing = False
        self.closed = None
        self.patches = []

    def enabled(self):
        try:
            data = json.loads(self.settings.read_text(encoding='utf8'))
            return isinstance(data, dict) and data.get('enabled') is True
        except (OSError, ValueError):
            return False

    @staticmethod
    def key(event):
        if event is None or event.get_platform_id() != 'snowluma' or not event.get_group_id():
            return None
        return (event.get_platform_id(), str(event.get_self_id()), str(event.get_group_id()))

    def group(self, event):
        key = self.key(event)
        if key is None:
            return None
        groups = self.loops.setdefault(asyncio.get_running_loop(), OrderedDict())
        if key not in groups:
            groups[key] = {'scope': hashlib.sha256(json.dumps(key).encode()).hexdigest()[:10],
                           'tickets': [], 'tail': None, 'sequence': 0, 'last_issue': None}
        groups.move_to_end(key)
        if len(groups) > 128:
            for old_key, old in list(groups.items()):
                if old_key != key and not old['tickets']:
                    del groups[old_key]
                    break
        return groups[key]

    @staticmethod
    def stopped(event):
        return bool(event.is_stopped() or event.get_extra('agent_stop_requested')
                    or event.get_extra('agent_user_aborted'))

    def mark(self, ticket, phase, extra=''):
        elapsed = time.monotonic()-ticket['start']
        self.logger.info(f"[group_delivery] scope={ticket['group']['scope']} trace={ticket['id']} "
                         f"seq={ticket['sequence']} phase={phase} elapsed_seconds={elapsed:.3f} {extra}")

    async def wait_before(self, event, future):
        while future is not None and not future.done():
            if self.stopped(event):
                return False
            # Waiting must never cancel an earlier reply's completion future.
            await asyncio.wait((future,), timeout=.2)
        return not self.stopped(event)

    async def reserve(self, event):
        ticket = event.get_extra(TICKET)
        if ticket is not None:
            return ticket
        group = self.group(event)
        if group is None:
            return None
        # Bounded backpressure runs under the native model lock. Earlier
        # delivery no longer needs that lock, so it can free this capacity.
        while len(group['tickets']) >= 8:
            if not await self.wait_before(event, group['tickets'][0]['done']):
                return None
        group['sequence'] += 1
        ticket = {'id': uuid.uuid4().hex[:12], 'group': group,
                  'sequence': group['sequence'], 'previous': group['tail'],
                  'done': asyncio.get_running_loop().create_future(),
                  'start': time.monotonic(), 'returns': 0, 'failures': 0,
                  'cancelled': False, 'expected_output': False}
        group['tickets'].append(ticket)
        group['tail'] = ticket['done']
        event.set_extra(TICKET, ticket)
        self.mark(ticket, 'model_start')
        return ticket

    async def prepare(self, event, request):
        if not event.get_extra(ACTIVE):
            return
        if not getattr(request, 'conversation', None):
            event.set_extra(ACTIVE, False)
            return
        ticket = await self.reserve(event)
        if ticket is None:
            return
        group = ticket['group']
        earlier = group['tickets'][:group['tickets'].index(ticket)]
        pending = sum(not item['done'].done() for item in earlier)
        note = (f'\n{NOTICE}\n历史中的assistant消息表示已生成内容，不等于对方已收到。'
                f'当前有{pending}条此前的群回复尚未完成发送流程。'
                '不能把待发送内容当作对方已读；必要时补足上下文。对未确认成功的内容，不声称已经发给对方；'
                '本轮工具若给出明确成功回执，可以据实确认该工具交付。'
                '发送流程成功也不代表对方已读。')
        issue = group['last_issue']
        if issue is not None and time.monotonic()-issue['at'] < 600:
            note += '最近有群回复未确认全部发送成功（可能取消、失败或部分送达），不要声称它已完整送达。'
        if NOTICE not in (request.system_prompt or ''):
            request.system_prompt = (request.system_prompt or '')+note+'\n'

    def finish(self, event, status):
        ticket = event.get_extra(TICKET)
        if ticket is None or ticket['done'].done():
            return
        if status == 'cancelled' or ticket['cancelled']:
            outcome = 'cancelled'
        elif status == 'error' or ticket['failures']:
            outcome = 'not_fully_sent'
        elif ticket['returns']:
            outcome = 'send_returned'
        elif ticket['expected_output']:
            outcome = 'not_sent'
        else:
            outcome = 'no_output'
        group = ticket['group']
        if outcome in {'cancelled', 'not_fully_sent', 'not_sent'}:
            group['last_issue'] = {'at': time.monotonic(), 'outcome': outcome}
        self.mark(ticket, 'done', f"status={outcome} send_returns={ticket['returns']} failures={ticket['failures']}")
        ticket['done'].set_result(None)
        group['tickets'].remove(ticket)
        if not group['tickets']:
            group['tail'] = None

    def install(self, stage_cls=None, event_cls=None, registry=None, event_var=None):
        if stage_cls is None:
            from astrbot.core.pipeline.process_stage.method.agent_sub_stages.internal import InternalAgentSubStage
            from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import AiocqhttpMessageEvent
            from astrbot.core.utils.active_event_registry import active_event_registry
            from astrbot.core.platform.astr_message_event import current_message_event
            stage_cls, event_cls = InternalAgentSubStage, AiocqhttpMessageEvent
            registry, event_var = active_event_registry, current_message_event
        original_process = stage_cls.process
        original_save = stage_cls._save_to_history
        original_send_descriptor = event_cls.__dict__['send_message']
        original_send = original_send_descriptor.__func__
        # Retain quota history across ordinary plugin reloads.
        budget = getattr(event_cls, '_local_codex_group_send_budget', None)
        if budget is None or getattr(budget, 'mode', None) != GroupSendBudget.mode:
            previous = budget
            budget = GroupSendBudget(self.logger)
            if previous is not None:
                # Conservatively retain old sends during the migration window.
                budget.states = previous.states
            event_cls._local_codex_group_send_budget = budget

        @wraps(original_save)
        async def save(stage, event, *args, **kwargs):
            try:
                result = await original_save(stage, event, *args, **kwargs)
            except BaseException:
                event.set_extra('_group_delivery_history_failed', True)
                raise
            ticket = event.get_extra(TICKET)
            if event.get_extra(ACTIVE) and ticket:
                self.mark(ticket, 'history_save_returned')
            return result

        @wraps(original_process)
        async def process(stage, event, *args, **kwargs):
            if self.closing:
                await asyncio.shield(self.closed)
            streaming = event.get_extra('enable_streaming')
            streaming = stage.streaming_response if streaming is None else bool(streaming)
            nonstream = not streaming or (stage.unsupported_streaming_strategy == 'turn_off'
                         and not event.platform_meta.support_streaming_message)
            eligible = self.key(event) is not None and nonstream and event.get_extra('action_type') != 'live'
            if not eligible or not self.enabled() or self.closing:
                # Disabling the optimization must not let new replies overtake
                # already queued delivery from the enabled path.
                group = self.group(event) if eligible else None
                if group and not await self.wait_before(event, group['tail']):
                    return
                stream = original_process(stage, event, *args, **kwargs)
                try:
                    async for value in stream:
                        yield value
                finally:
                    await stream.aclose()
                return
            completion = asyncio.get_running_loop().create_future()
            self.runs.add(completion)
            event.set_extra(ACTIVE, True)
            event.set_extra(TICKET, None)
            status, buffered, callback, last_result = 'ok', [], None, None
            event.set_extra('_group_delivery_history_failed', False)
            stream = original_process(stage, event, *args, **kwargs)
            try:
                async for value in stream:
                    if not event.get_extra(ACTIVE):
                        yield value
                        continue
                    result = event.get_result()
                    if result is not None:
                        # Only the non-streaming path reaches here; components
                        # retain their files but not mutable result/chain objects.
                        buffered.append(copy.deepcopy(result))
                        last_result = result
                if event.get_extra(ACTIVE):
                    remaining = event.get_result()
                    if remaining is not None and remaining is not last_result:
                        buffered.append(copy.deepcopy(remaining))
                    if event.get_extra('_group_delivery_history_failed'):
                        buffered.clear()
                        status = 'error'
                    event.clear_result()
                    ticket = event.get_extra(TICKET)
                    if ticket is not None:
                        ticket['expected_output'] = bool(buffered)
                        self.mark(ticket, 'model_ready', f'buffered_results={len(buffered)}')
                        # Model history and the native lock are finalized before
                        # any downstream TTS/result decoration starts.
                        task = asyncio.current_task()
                        callback = lambda: task.cancel()
                        registry.register_agent_stop_callback(event, callback)
                        if not await self.wait_before(event, ticket['previous']):
                            status = 'cancelled'
                            return
                        self.mark(ticket, 'delivery_start')
                    for result in buffered:
                        if self.stopped(event):
                            status = 'cancelled'
                            break
                        event.set_result(result)
                        yield None
                        event.clear_result()
            except (asyncio.CancelledError, GeneratorExit):
                status = 'cancelled'
                raise
            except BaseException:
                status = 'error'
                raise
            finally:
                try:
                    await stream.aclose()
                finally:
                    if callback is not None and registry._agent_stop_callbacks.get(event) is callback:
                        registry.unregister_agent_stop_callback(event)
                    self.finish(event, status)
                    event.set_extra(ACTIVE, False)
                    completion.set_result(None)
                    self.runs.discard(completion)

        @classmethod
        async def send_message(cls, bot, message_chain, event=None, is_group=False, session_id=None):
            owner = event_var.get()
            track = (owner is not None and owner.get_extra(ACTIVE) and is_group
                     and str(session_id) == str(owner.get_group_id()))
            ticket = await self.reserve(owner) if track else None
            if ticket is not None and not await self.wait_before(owner, ticket['previous']):
                ticket['cancelled'] = True
                return None
            send_bot = bot
            if is_group and session_id:
                account = (event or {}).get('self_id', '')
                if owner is not None:
                    account = owner.get_self_id()
                # Without account metadata, scope to this adapter instance.
                account = account or ('adapter-' + str(id(bot)))
                send_owner = owner if owner is not None and str(owner.get_group_id()) == str(session_id) else None
                send_bot = GroupBudgetBot(bot, budget, account, session_id, send_owner)
            try:
                async def dispatch(chain):
                    return await original_send(cls, send_bot, chain, event, is_group, session_id)
                result = await send_with_voice_upload_fallback(dispatch, message_chain, owner, self.logger)
            except BaseException:
                if ticket:
                    ticket['failures'] += 1
                    self.mark(ticket, 'send_failed')
                raise
            if ticket and getattr(message_chain, 'chain', None):
                ticket['returns'] += 1
                self.mark(ticket, 'send_returned')
            return result

        self.patches = [(stage_cls, 'process', original_process, process),
                        (stage_cls, '_save_to_history', original_save, save),
                        (event_cls, 'send_message', original_send_descriptor, send_message)]
        for owner, name, _, replacement in self.patches:
            setattr(owner, name, replacement)
        self.logger.info('[group_delivery] installed mode=model_then_ordered_delivery budget=5_reply_rounds_per_60s voice_upload_text_fallback=httpconn_v2')
        return self

    async def close(self):
        if not self.closing:
            self.closing = True
            async def drain_and_restore():
                if self.runs:
                    await asyncio.gather(*(asyncio.shield(run) for run in tuple(self.runs)))
                for owner, name, original, replacement in reversed(self.patches):
                    if owner.__dict__.get(name) is replacement:
                        setattr(owner, name, original)
            # A cancelled HTTP reload request must not cancel queued delivery or
            # restore the send gate before the old replies have drained.
            self.closed = asyncio.create_task(drain_and_restore())
        await asyncio.shield(self.closed)
