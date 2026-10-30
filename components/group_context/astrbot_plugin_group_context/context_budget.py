"""Select optional QQ context without a classifier request or stored-history edits."""
import copy
import re

from astrbot.api.message_components import At, Reply

VISUAL_REFERENCE = re.compile(
    r'图片|照片|截图|看图|上图|图中|图里|这[张幅]|那[张幅]|画面|表情包|头像|'
    r'角色|颜色|长什么样|看起来|正常在哪|正常在[哪里哪]|这是谁|那是谁|这是什么|原图|出处|'
    r'两张|几张|之前.*图|刚才.*图|重画|照着|'
    r'\b(?:images?|pictures?|photos?|screenshots?)\b', re.I)
VISUAL_COMPARISON = re.compile(
    r'对比|比较|相比|区别|不同|一样|两张|几张|上次|上[一张幅]|之前.*图|刚才.*图|参考.*图|照着|重画|'
    r'\b(?:compare|comparison|previous|earlier|reference|redraw)\b', re.I)


def visual_comparison(text, has_current=False):
    return bool(VISUAL_COMPARISON.search(text)
                and (has_current or VISUAL_REFERENCE.search(text)))


def reply_chain_ids(event):
    """Read bounded quote ancestry from adapter components, never message text."""
    pending = [p for p in event.get_messages() if isinstance(p, Reply)]
    ids = set()
    for _ in range(4):
        following = []
        for part in pending[:16]:
            if part.id:
                ids.add(str(part.id))
            following.extend(p for p in (part.chain or []) if isinstance(p, Reply))
        pending = following
        if not pending:
            break
    return ids


def linked_quote_ids(records, event):
    ids = reply_chain_ids(event)
    for _ in range(4):
        linked = {str(q.get('message_id')) for r in records if r.get('message_id') in ids
                  for q in r.get('reply_to', []) if q.get('message_id')}
        linked.update(str(mid) for r in records if r.get('message_id') in ids
                      for mid in r.get('reply_chain_message_ids', []))
        if linked <= ids:
            break
        ids.update(linked)
    return ids


def select_group_records(records, event):
    """Prioritize real quote links, participants and lexical topic matches."""
    quote_ids = linked_quote_ids(records, event)
    query = event.message_str[:1600] + '\n' + '\n'.join(
        str(p.message_str or '')[:800] for p in event.get_messages() if isinstance(p, Reply))[:3200]
    words = set(re.findall(r'[a-zA-Z][a-zA-Z0-9_]{2,}', query.casefold()))
    for run in re.findall(r'[\u4e00-\u9fff]{2,}', query):
        words.update(run[i:i + 2] for i in range(len(run) - 1))
    words.difference_update({'这个', '那个', '什么', '怎么', '帮我', '一下', '可以',
                            '现在', '刚才', '还是', '没有', '是否', '你是', '我是'})
    words = sorted(words, key=lambda word: (-len(word), word))[:128]
    sender = str(event.get_sender_id())
    mentions = {str(p.qq) for p in event.get_messages() if isinstance(p, At)}
    own = [i for i, r in enumerate(records) if r.get('sender_id') == sender][-2:]
    ranked = []
    for i, record in enumerate(records):
        quoted = record.get('message_id') in quote_ids
        text = record.get('text', '').casefold()
        lexical = sum(min(4, len(w)) for w in words if w in text)
        score = (1000 if quoted else 0) + ((40 if quote_ids else 100) if i in own else 0)
        score += (80 if record.get('sender_id') in mentions else 0) + min(60, lexical * 5)
        score += 20 if i >= len(records) - 4 else 0
        if score:
            ranked.append((score, i, record))
    return [(i, {**r, 'text': r.get('text', '')[:600],
                 **({'context_relation': 'explicit_reply_chain'}
                    if r.get('message_id') in quote_ids else {})})
            for _, i, r in sorted(ranked, key=lambda item: (item[0], item[1]), reverse=True)]


def filter_history_images(contexts, *, keep_recent):
    """Copy optional old image parts out of a request while retaining text/tool order."""
    latest = next((i for i in range(len(contexts) - 1, -1, -1)
                   if contexts[i].get('role') == 'user'
                   and isinstance(contexts[i].get('content'), list)
                   and any(isinstance(p, dict) and p.get('type') in {'image_url', 'input_image'}
                           for p in contexts[i]['content'])), None) if keep_recent else None
    result, omitted = [], 0
    for i, message in enumerate(contexts):
        content = message.get('content')
        if not isinstance(content, list) or message.get('role') not in {'user', 'assistant'}:
            result.append(message)
            continue
        parts, retained = [], 0
        for part in content:
            if isinstance(part, dict) and part.get('type') in {'image_url', 'input_image'}:
                if i == latest and retained < 4:
                    parts.append(copy.deepcopy(part))
                    retained += 1
                else:
                    omitted += 1
                    # Preserve the fact an image existed, without implying visibility.
                    if not parts or parts[-1] != {'type': 'text', 'text': '[Earlier image pixels not attached for this request.]'}:
                        parts.append({'type': 'text', 'text': '[Earlier image pixels not attached for this request.]'})
            else:
                parts.append(copy.deepcopy(part))
        result.append({**message, 'content': parts})
    return result, omitted
