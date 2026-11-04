"""Read bounded, already-sent public bot text missing from inbound QQ events."""
import asyncio
import math
import time
from collections.abc import Mapping

from astrbot.api import logger

CACHE_KEY = '_local_codex_recent_self_messages'


def message_time(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if math.isfinite(value) and value > 0 else None


async def recent_self_messages(event, boundary, window):
    """One read per admitted event; history remains temporary untrusted data."""
    cached = event.get_extra(CACHE_KEY)
    if cached is not None:
        return cached
    # Mark before awaiting: failed reads and tool continuations must not retry.
    event.set_extra(CACHE_KEY, [])
    raw = getattr(event.message_obj, 'raw_message', None)
    bot = getattr(event, 'bot', None)
    if (event.get_platform_id() != 'snowluma' or not event.get_group_id()
            or not isinstance(raw, Mapping) or bot is None):
        return []
    cutoff = message_time(raw.get('time'))
    # QQ's server clock can differ slightly from the host clock. Never use
    # request execution time as the cutoff for an event that waited in a queue.
    received = getattr(event, 'created_at', time.time())
    if cutoff is None or not isinstance(received, (int, float)) or abs(cutoff - received) > 30:
        return []
    seq = raw.get('message_seq')
    seq = seq if type(seq) is int and seq > 0 else None
    try:
        params = {'group_id': int(event.get_group_id()), 'count': 20}
    except (TypeError, ValueError):
        return []
    if raw.get('post_type') == 'message' and raw.get('message_id'):
        try:
            params['message_id'] = int(raw['message_id'])
        except (ValueError, TypeError):
            return []
    try:
        result = await asyncio.wait_for(bot.call_action('get_group_msg_history', **params), 2)
    except Exception as exc:
        logger.info('[local_codex] recent_self_context unavailable=%s', type(exc).__name__)
        return []
    messages = result.get('messages', []) if isinstance(result, Mapping) else []
    if not isinstance(messages, list):
        return []
    selected, seen = [], set()
    for item in messages[:20]:
        if not isinstance(item, Mapping):
            continue
        when = message_time(item.get('time'))
        mid = str(item.get('message_id', ''))
        item_seq = item.get('message_seq')
        # Pokes have no sequence: exclude the entire trigger second rather
        # than risk including a same-second message sent after the wake.
        before = (when is not None and (when < cutoff or
                  (when == cutoff and seq is not None and type(item_seq) is int and 0 < item_seq < seq)))
        if (item.get('message_type') != 'group'
                or str(item.get('group_id', '')) != str(event.get_group_id())
                or str(item.get('user_id', '')) != str(event.get_self_id())
                or (item.get('self_id') is not None and str(item['self_id']) != str(event.get_self_id()))
                or not before or not 0 <= cutoff - when <= window
                or not mid or mid == str(raw.get('message_id', '')) or mid in seen):
            continue
        parts = item.get('message')
        if not isinstance(parts, list):
            continue
        # Only actual text components. Never turn JSON/forward/card bodies or
        # unavailable media into a claim that the bot said or saw their text.
        text = ''.join(p.get('data', {}).get('text', '') for p in parts
                       if isinstance(p, Mapping) and p.get('type') == 'text'
                       and isinstance(p.get('data'), Mapping)
                       and isinstance(p['data'].get('text'), str))[:1600]
        if not text.strip():
            continue
        seen.add(mid)
        selected.append((boundary[2] - (cutoff - when), {
            'sender_id': str(event.get_self_id()), 'bot_id': str(event.get_self_id()),
            'message_id': mid, 'sender_name': '', 'text': text,
            'direct_image_count': 0, 'image_count': 0, 'reply_to': [],
            'reply_chain_message_ids': [], 'mentions': [], 'mentions_all': False,
            'context_relation': 'recent_self_message',
        }))
    selected.sort(key=lambda item: item[0])
    event.set_extra(CACHE_KEY, selected[-3:])
    logger.info('[local_codex] recent_self_context selected=%d window_seconds=%d', len(selected[-3:]), window)
    return selected[-3:]
