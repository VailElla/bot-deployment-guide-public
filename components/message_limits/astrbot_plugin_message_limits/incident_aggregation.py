"""Bounded, persistent incident aggregation without message payloads."""
import asyncio
import json
import re
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

LABELS = {
    'capacity': '模型服务暂时满载',
    'network': '模型连接中断',
    'timeout': '模型处理超时',
    'sandbox_repair': '旧沙箱自动修复失败',
    'sandbox': '旧沙箱已失效',
    'quota': '模型额度不足',
    'auth': '模型登录异常',
    'model': '模型请求失败',
    'tts': '语音生成异常',
    'delivery': '回复发送失败',
    'platform': 'QQ或平台连接异常',
    'plugin': '插件运行异常',
    'backend': '机器人运行异常',
    'test': '异常通知自检',
}


REASONS = {
    'sandbox_repair': '确认旧沙箱失效后，自动恢复重试仍未成功。',
    'capacity': '当前模型的服务容量暂时不足，本次请求未完成。',
    'network': '连接模型服务时发生网络中断。',
    'timeout': '等待或处理本次请求超过了允许时间。',
    'sandbox': '机器人保留的旧沙箱记录在沙箱服务中已找不到。',
    'quota': '模型服务报告账号额度不足或请求频率受限。',
    'auth': '模型服务未能通过账号登录验证。',
    'model': '本次模型请求未能完成，具体原因尚未识别。',
    'tts': '语音合成未成功，本轮可能改为发送文字。',
    'delivery': '回复发送被QQ拒绝，本轮回复未确认送达。',
    'platform': 'QQ客户端或消息连接发生错误。',
    'plugin': '插件执行时发生错误，具体原因需查本机日志。',
    'backend': '机器人后台发生错误，具体原因需查本机日志。',
    'test': '这是一条通知链路自检消息，并非真实故障。',
}
SOURCES = {'sandbox_repair': '沙箱自动恢复','capacity': '模型服务', 'network': '模型连接', 'timeout': '模型处理',
           'sandbox': '沙箱服务', 'quota': '模型账号', 'auth': '模型账号',
           'model': '模型处理', 'tts': '语音服务', 'platform': 'QQ消息连接',
           'delivery': 'QQ回复发送', 'plugin': '机器人插件', 'backend': '机器人后台', 'test': '通知自检'}


CODES = {'capacity': 'ModelAtCapacity', 'quota': 'UsageLimitOrRateLimit',
         'auth': 'AuthenticationFailed', 'network': 'ConnectionInterrupted',
         'timeout': 'RequestTimeout', 'sandbox': 'SandboxNotFound',
         'sandbox_repair': 'SandboxNotFound / SandboxRecoveryFailed', 'tts': 'SpeechSynthesisFailed',
         'model': 'ModelRequestFailed', 'platform': 'PlatformConnectionError',
         'delivery': 'ReplyDeliveryFailed', 'plugin': 'PluginRuntimeError', 'backend': 'BackendRuntimeError',
         'test': 'NotificationSelfTest'}


def timestamp(value):
    return datetime.fromtimestamp(value, timezone(timedelta(hours=8))).isoformat(timespec='milliseconds')


def log_reference(source, now, details=None):
    """Only fixed diagnostic fields may cross the log/notification boundary."""
    details = details or {}
    clean = lambda value: re.sub(r'[^a-zA-Z0-9_.:-]', '', str(value))[-80:]
    ref = {'id': 'XA-' + uuid.uuid4().hex, 'time': timestamp(now),
           'source': clean(source) or 'unknown'}
    for key in ('line', 'function', 'level', 'exception'):
        if details.get(key):
            ref[key] = clean(details[key])
    trace = str(details.get('trace', ''))
    if re.fullmatch(r'[a-fA-F0-9]{12,32}', trace):
        ref['trace'] = trace.lower()
    return ref


def classify(level, source, message):
    """Return a fixed category, never a copy of logged chat or credentials."""
    text = message.lower()
    source = source.lower()
    # A skipped @ member whose robot classification is unavailable is a local
    # metadata limitation, not a disconnected QQ platform. Keep fail-closed
    # parsing and the original log; suppress only this exact handled condition.
    if (re.search(r'(?:^|\.)aiocqhttp_platform_adapter(?::\d+)?$', source)
            and text.startswith('获取 @ 用户信息失败:')
            and 'group member robot classification unavailable:' in text
            and '此消息段将被忽略' in text):
        return None
    # Unavailable remote media is logged locally and handled by the caller.
    # Check before network/timeout classification so failed downloads stay quiet.
    if (re.search(r'(?:^|\.)utils\.io(?::\d+)?$', source)
            and text.startswith('failed to download file from ')):
        return None
    if (re.search(r'(?:^|\.)preprocess_stage\.stage(?::\d+)?$', source)
            and text.startswith('image processing failed for ')
            and 'failed to download file from ' in text):
        return None
    # Optional stickers may be skipped on QQ quota, but a rejected reply
    # must be reported even when the model has already completed successfully.
    if re.search(r'send group message rejected:\s*result=299\b.*?err=本群每分钟只能发\d+条消息', text, re.S):
        optional_sticker = (re.search(r'(?:^|\.)mixins\.event_handlers(?::\d+)?$', source)
                            and text.startswith('发送表情图片失败：'))
        return None if optional_sticker else 'delivery'
    if 'runtime_alarm' in source:
        return None
    if '[bot_alert]' in text:
        match = re.search(r'category=(capacity|network|timeout|sandbox|quota|auth|model|test)\b', text)
        return match.group(1) if match else 'model'
    if level < 30:
        return None
    model_error = ('chat model ' in text and 'request error' in text or
                   'agent failure' in text or 'agent error reply suppressed' in text)
    if model_error or level >= 40:
        if any(x in text for x in ('at capacity', 'high demand', 'server overloaded')):
            return 'capacity'
        if any(x in text for x in ('timed out', 'timeout', '超过') ):
            return 'timeout'
        if any(x in text for x in ('connection reset', 'connection refused', 'stream disconnected', '连接中断')):
            return 'network'
        if 'sandbox not found' in text:
            return 'sandbox'
        if model_error:
            return 'model'
    if ('whole reply tts failed' in text or 'tts' in source and level >= 40):
        return 'tts'
    if level < 40:
        return None  # Normal retries/fallback attempts are not incidents.
    if any(x in source for x in ('provider', 'runner', 'persistent', 'agent')):
        return 'model'
    if any(x in source for x in ('aiocqhttp', 'event_bus', 'platform', 'websocket')):
        return 'platform'
    return 'plugin' if 'plugin' in source else 'backend'


class Incidents:
    diagnostic_format = 2
    member_lookup_filter = 1
    muted_categories = frozenset({'capacity'})
    failure_notification_policy = 2

    def __init__(self, path, cooldown=600, global_interval=30):
        self.path = Path(path)
        self.cooldown = cooldown
        self.global_interval = global_interval
        self.last_delivery = 0
        self.next_attempt = 0
        self.items = {}
        self._during_send = {}
        try:
            data = json.loads(self.path.read_text(encoding='utf8'))
            self.items = {k: v for k, v in data.get('items', {}).items()
                          if k in LABELS and isinstance(v, dict)}
            self.last_delivery = float(data.get('last_delivery', 0))
        except (OSError, ValueError, TypeError):
            pass
        for category in self.muted_categories:
            if category in self.items:
                self.items[category]['pending'] = 0

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix('.tmp')
        temporary.write_text(json.dumps({'items': self.items, 'last_delivery': self.last_delivery},
                                        ensure_ascii=False), encoding='utf8')
        temporary.replace(self.path)

    def append_index(self, entry):
        path = self.path.with_name('diagnostics.jsonl')
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists() and path.stat().st_size >= 2 * 1024 * 1024:
            previous = path.with_name(path.name + '.1')
            if previous.exists():
                previous.replace(path.with_name(path.name + '.2'))
            path.replace(previous)
        with path.open('a', encoding='utf8') as stream:
            stream.write(json.dumps(entry, ensure_ascii=False) + '\n')

    def add(self, category, source, now=None, details=None):
        if category not in LABELS:
            return
        now = time.time() if now is None else now
        # A detailed model error and its outer Agent wrapper are one incident.
        if category == 'model':
            for detailed in ('capacity', 'network', 'timeout', 'quota', 'auth'):
                if now - self.items.get(detailed, {}).get('last', 0) < 5:
                    return
        ref = log_reference(source, now, details)
        source = ref['source']
        self.append_index({'kind': 'record', 'category': category, **ref})
        item = self.items.setdefault(category, {'pending': 0, 'sent': 0})
        item.update(last=now, source=source, last_ref=ref)
        if not item['pending']:
            item['first'] = now
            item['first_ref'] = ref
        if category in self._during_send and self._during_send[category] is None:
            self._during_send[category] = (now, ref)
        # Keep the diagnostic index and recent-category wrapper suppression,
        # without queueing owner notifications for muted model failures.
        if category in self.muted_categories:
            item['pending'] = 0
        else:
            item['pending'] += 1
        self.save()
        return ref

    async def deliver(self, sender, now=None):
        now = time.time() if now is None else now
        if now < self.next_attempt or now - self.last_delivery < self.global_interval:
            return None
        for category, item in self.items.items():
            if category == 'sandbox' or category in self.muted_categories:
                continue  # Missing boxes are handled by automatic recovery.
            if not item.get('pending') or now - item.get('sent', 0) < self.cooldown:
                continue
            label = LABELS[category]
            # Existing pending state has no original line/trace; retain its times.
            if not item.get('first_ref'):
                item['first_ref'] = log_reference(item['source'], item['first'])
                item['first_ref']['legacy'] = True
                self.save()
            first = dict(item['first_ref'])
            last = dict(item.get('last_ref') or first)
            stamp = timestamp(item['first'])
            count = item['pending']
            location = lambda ref: ref['source'] + (':' + str(ref['line']) if ref.get('line') else '')
            references = f'日志编号：{first["id"]}\n首次：{stamp}\n'
            if count > 1:
                references += f'末次：{timestamp(item["last"])}\n末条编号：{last["id"]}\n'
            references += f'模块：{location(first)}'
            if location(last) != location(first):
                references += f' → {location(last)}'
            references += '\n'
            traces = list(dict.fromkeys(r['trace'] for r in (first, last) if r.get('trace')))
            if traces:
                references += 'Trace：' + ' / '.join(traces) + '\n'
            exceptions = list(dict.fromkeys(r['exception'] for r in (first, last) if r.get('exception')))
            if exceptions:
                references += '异常类型：' + ' / '.join(exceptions) + '\n'
            if first.get('legacy'):
                references += '记录：升级前待发日志，未采集原始行号和Trace\n'
            text = (f'【Bot 异常通知】{label}\n{references}'
                    f'来源：{SOURCES[category]}\n原因：{REASONS[category]}\n'
                    f'合并日志：{count} 条\n技术信息：{CODES[category]}')
            self.append_index({'kind': 'notification_attempt', 'id': first['id'],
                               'last_id': last['id'], 'category': category,
                               'records': count, 'time': timestamp(now)})
            self._during_send[category] = None
            try:
                response = await asyncio.wait_for(sender(text), timeout=15)
                if not isinstance(response, dict) or not response.get('message_id'):
                    raise RuntimeError('No QQ delivery receipt')
            except asyncio.CancelledError:
                self._during_send.pop(category, None)
                raise
            except Exception:
                self._during_send.pop(category, None)
                self.next_attempt = now + 30
                return False
            # New records may arrive while awaiting QQ; preserve those records.
            item['pending'] -= count
            following = self._during_send.pop(category, None)
            if item['pending'] and following:
                item['first'], item['first_ref'] = following
            item['sent'] = now
            item['receipt'] = str(response['message_id'])
            self.last_delivery = now
            self.next_attempt = 0
            self.save()
            return {'category': category, 'records': count, 'message_id': response['message_id'],
                    'log_id': first['id']}
        return None
