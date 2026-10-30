"""Keep bounded QQ pixels for split messages and explicit quote chains."""
from __future__ import annotations

import asyncio
import json
import re
import time
from collections import OrderedDict
from dataclasses import dataclass

from astrbot.api.message_components import Image, Reply
from astrbot.api import logger
from astrbot.core.agent.message import TextPart, ImageURLPart
from astrbot.core.utils.media_utils import resolve_image_ref_to_base64_data
from .context_budget import VISUAL_REFERENCE, filter_history_images, visual_comparison
from .image_archive import ImageArchive

SENDER_HISTORY_IMAGE_ID = 'local_codex_sender_history_image'
SENDER_IMAGE_REFERENCE = re.compile(r'这[张幅]|那[张幅]|上图|原图|上[张幅]|(?:之前|刚才|前面|上次|最[近后]).{0,12}(?:图|照片|截图)|\b(?:previous|earlier|last|that)\s+(?:image|photo|picture|screenshot)\b', re.I)

IMPLICIT_VISUAL_COMPARISON = re.compile(r'这样看(?:起来|着)?[^，。！？\n]{0,8}像')

@dataclass
class PendingImages:
    created: float
    expires: float
    references: list[str]
    owner: tuple[str, str] | None = None
    retain_pending: bool = False
    source_message_id: str = ""
    auto_event: str | None = None
    source_sender_id: str = ""


class RecentImages:
    """Same-sender pending images; same-chat message IDs for explicit quotes.

    Used images remain quotable until expiry. Public group images are supplied
    only for messages in the trusted public-context snapshot for this request;
    per-user conversation history and private chats remain isolated.
    """

    def __init__(self, ttl=600, max_bytes=80 * 1024 * 1024, max_owners=64,
                 max_messages=256, split_window=60, archive_path=None):
        self.archive = ImageArchive(archive_path) if archive_path else None
        self.ttl = ttl
        self.split_window = split_window
        self.max_bytes = max_bytes
        self.max_owners = max_owners
        self.max_messages = max_messages
        self.pending: OrderedDict[tuple[str, str], PendingImages] = OrderedDict()
        self.messages: OrderedDict[tuple[tuple, str], PendingImages] = OrderedDict()

    @staticmethod
    def key(event):
        sender = str(event.get_sender_id() or "")
        if event.get_platform_name() != "aiocqhttp" or not sender:
            return None
        return event.unified_msg_origin, sender

    @classmethod
    def scope(cls, event):
        if cls.key(event) is None:
            return None
        obj = getattr(event, 'message_obj', None)
        group = str(getattr(obj, 'group_id', '') or '')
        platform = event.unified_msg_origin.split(':', 1)[0]
        # Private chats must remain sender-isolated, including equal message IDs.
        return (platform, 'group', group) if group else (platform, 'private', cls.key(event))

    @staticmethod
    def message_id(event):
        return str(getattr(getattr(event, 'message_obj', None), 'message_id', '') or '')

    def remember(self, event, entry, message_id=None):
        mid = message_id or self.message_id(event)
        scope = self.scope(event)
        if scope is not None and mid:
            self.messages[(scope, str(mid))] = entry
            self.messages.move_to_end((scope, str(mid)))
        self.prune()

    def quoted(self, event):
        scope = self.scope(event)
        for part in event.get_messages():
            if isinstance(part, Reply):
                entry = self.messages.get((scope, str(part.id)))
                if entry and entry.created <= event.created_at and entry.references:
                    return entry
        return None

    def prune(self):
        now = time.monotonic()
        for mapping in (self.pending, self.messages):
            for key, entry in list(mapping.items()):
                if entry.expires <= now and not (mapping is self.pending and entry.retain_pending):
                    mapping.pop(key, None)
        while len(self.pending) > self.max_owners:
            self.pending.popitem(last=False)
        while len(self.messages) > self.max_messages:
            self.messages.popitem(last=False)
        # Aliases share pixels; count each entry once and evict every alias of it.
        entries = {id(e): e for e in [*self.pending.values(), *self.messages.values()]}
        total = sum(sum(map(len, e.references)) for e in entries.values())
        for entry in sorted(entries.values(), key=lambda e: e.created):
            if total <= self.max_bytes:
                break
            for mapping in (self.pending, self.messages):
                for key, value in list(mapping.items()):
                    if value is entry:
                        mapping.pop(key, None)
            total -= sum(map(len, entry.references))

    async def _resolve(self, images, entry):
        async with asyncio.timeout(8):
            for image in images[:12]:
                # Local pixels can remain usable after a signed CDN URL expires.
                for ref in dict.fromkeys([image.url, image.file]):
                    if not ref:
                        continue
                    try:
                        resolved = await resolve_image_ref_to_base64_data(
                            ref, strict=True, default_mime_type=None)
                    except Exception:
                        continue
                    if resolved is not None and len(resolved.base64_data) <= 28 * 1024 * 1024:
                        entry.references.append(resolved.to_data_url())
                        self.prune()
                        break

    async def capture(self, event):
        self.prune()
        key = self.key(event)
        if key is None:
            return
        if re.match(r"^/(?:reset|new|clear|del)(?:\s|$)", event.message_str.strip(), re.I):
            self.discard(event)
            if self.archive:
                try:
                    await self.archive.reset(self.scope(event), str(event.get_sender_id()), event.created_at)
                except Exception as exc:
                    logger.warning('Image archive reset marker failed: %s', type(exc).__name__)
            # A reset must not resurrect this conversation through message aliases.
            for mid, cached in list(self.messages.items()):
                if cached.owner == key:
                    self.messages.pop(mid, None)
            return
        images = [part for part in event.get_messages() if isinstance(part, Image)]
        forward = getattr(event, 'get_extra', lambda key: None)('_local_codex_forward') or {}
        images.extend(Image(file=ref, url=ref) for ref in forward.get('images', []))
        if images:
            previous = self.pending.get(key)
            newer = previous and previous.created > event.created_at
            if len(images) > 12:
                logger.warning("Image archive: only first 12 images in this message can be captured")
            entry = PendingImages(event.created_at, time.monotonic() + self.ttl, [], key,
                                  self.scope(event)[1] == 'group', self.message_id(event),
                                  source_sender_id=str(event.get_sender_id()))
            if not newer:
                self.pending[key] = entry
                self.pending.move_to_end(key)
            self.remember(event, entry)
            try:
                await self._resolve(images, entry)
            except TimeoutError:
                logger.warning('Image capture timed out; retaining resolved images')
            if self.archive and entry.references:
                try:
                    await self.archive.save(self.scope(event), self.message_id(event), str(event.get_sender_id()), entry.created, entry.references)
                except Exception as exc:
                    logger.warning("Image archive write failed: %s", type(exc).__name__)
            return
        # Native OneBot intentionally stops after one quote level. Preserve the
        # image association of intermediate text messages for subsequent quotes.
        entry = self.quoted(event)
        if entry:
            self.remember(event, entry)
            return
        for part in event.get_messages():
            if isinstance(part, Reply) and part.chain:
                images = [p for p in part.chain if isinstance(p, Image)]
                if images:
                    entry = PendingImages(event.created_at, time.monotonic() + self.ttl, [], key,
                                          source_message_id=str(part.id or ''), source_sender_id=str(part.sender_id or ''))
                    self.remember(event, entry, str(part.id))
                    self.remember(event, entry)
                    await self._resolve(images, entry)
                    return

    async def hydrate(self, event, records=()):
        """Restore only explicitly scoped message IDs or the sender's own image."""
        if not self.archive or self.key(event) is None:
            return
        self.prune()
        scope = self.scope(event)
        mids = [str(p.id) for p in event.get_messages() if isinstance(p, Reply) and p.id]
        mids += [str(r.get('message_id')) for r in records if r.get('image_count') and r.get('message_id')]
        try:
            for mid in dict.fromkeys(mids):
                if (scope, mid) in self.messages:
                    continue
                row = await self.archive.read(scope, event.created_at, mid=mid)
                if row:
                    entry = PendingImages(row['created'], time.monotonic() + self.ttl, row['refs'],
                                          (event.unified_msg_origin, row['sender_id']), scope[1] == 'group',
                                          row['message_id'], source_sender_id=row['sender_id'])
                    self.remember(event, entry, mid)
            if not mids and SENDER_IMAGE_REFERENCE.search(event.message_str or '') and self.key(event) not in self.pending:
                row = await self.archive.read(scope, event.created_at, sender=str(event.get_sender_id()))
                if row:
                    entry = PendingImages(row['created'], time.monotonic() + self.ttl, row['refs'],
                                          self.key(event), scope[1] == 'group', row['message_id'],
                                          source_sender_id=row['sender_id'])
                    self.pending[self.key(event)] = entry
                    self.remember(event, entry, row['message_id'])
        except Exception as exc:
            logger.warning('Image archive read failed: %s', type(exc).__name__)

    def discard(self, event):
        entry = self.pending.get(self.key(event))
        if entry and entry.created <= event.created_at:
            self.pending.pop(self.key(event), None)

    def inject(self, event, req):
        count = self._inject_sender(event, req)
        # Our provider hook runs at priority 90; the native group-context hook
        # runs later at priority 0. Let it finish the same request's pixel link
        # once its trusted snapshot is available. No prompt text can set this.
        req._local_codex_public_image_injector = self._inject_public_context
        return count + self._inject_public_context(event, req)

    def _inject_public_context(self, event, req):
        if getattr(req, '_local_codex_public_images_injected', False):
            return 0
        scope = self.scope(event)
        records = getattr(req, '_local_codex_public_records', None)
        if not records or scope is None or scope[1] != 'group':
            return 0
        # Current and explicit quote images have priority over public context.
        extra_images = [p for p in req.extra_user_content_parts
                        if (p.get('type') if isinstance(p, dict) else getattr(p, 'type', None))
                        in {'image_url', 'input_image'}]
        compare = visual_comparison(event.message_str, bool(req.image_urls or extra_images))
        implicit_visual = bool(IMPLICIT_VISUAL_COMPARISON.search(event.message_str))
        if not compare and (req.image_urls or extra_images):
            return 0
        forward_reference = bool(re.search(r'转发|聊天记录|合并消息', event.message_str))
        if not compare and not VISUAL_REFERENCE.search(event.message_str) and not forward_reference and not implicit_visual:
            return 0
        capacity = min(2, max(0, 12 - len(req.image_urls or []) - len(extra_images)))
        if not capacity:
            return 0
        candidates = []
        seen_entries = set()
        for record in reversed(records):
            if not record.get('image_count'):
                continue
            if forward_reference and not VISUAL_REFERENCE.search(event.message_str) and '[合并转发聊天记录' not in record.get('text', ''):
                continue
            entry = self.messages.get((scope, str(record.get('message_id', ''))))
            if (not entry or not entry.references or entry.created > event.created_at
                    or id(entry) in seen_entries):
                continue
            # An unquoted visual-sounding remark is ambiguous. Only offer a
            # very recent group image, explicitly as possible background.
            if implicit_visual and not VISUAL_REFERENCE.search(event.message_str) and event.created_at - entry.created > 30:
                continue
            if (not compare and event.created_at - entry.created > 90
                    and not re.search(r'上图|之前|刚才|那张|上张', event.message_str)):
                continue
            seen_entries.add(id(entry))
            candidates.append((record, entry))
            if len(candidates) == (2 if compare else 1):
                break
        # One picture from each recent message first, then remaining pictures.
        # This prevents one multi-picture post from hiding the prior topic.
        selected = []
        seen_refs = set(req.image_urls or [])
        for index in range(4):
            for record, entry in candidates:
                if index >= len(entry.references):
                    continue
                ref = entry.references[index]
                if ref in seen_refs:
                    continue
                seen_refs.add(ref)
                selected.append((record, index + 1, ref))
                if len(selected) == capacity:
                    break
            if len(selected) == capacity:
                break
        for record, index, ref in selected:
            entry = self.messages[(scope, str(record['message_id']))]
            entry.auto_event = self.message_id(event) or str(id(event))
            req.extra_user_content_parts.append(TextPart(text=json.dumps({
                'public_group_image': {
                    'message_id': record['message_id'], 'sender_id': record['sender_id'],
                    'image_index': index, 'sent_at_unix': entry.created,
                    'age_seconds': max(0, round(event.created_at - entry.created, 3)),
                    'source': 'matching message in recent_group_context; public chat data; relevance is uncertain' if implicit_visual else 'matching message in recent_group_context; public chat data',
                }}, ensure_ascii=False)).mark_as_temp())
            req.extra_user_content_parts.append(ImageURLPart(
                image_url=ImageURLPart.ImageURL(
                    url=ref, id='local_codex_public_group_image')).mark_as_temp())
        if selected:
            if (not compare and isinstance(req.contexts, list)
                    and all(isinstance(m, dict) for m in req.contexts)):
                req.contexts, omitted = filter_history_images(req.contexts, keep_recent=False)
                if omitted:
                    logger.info('[local_codex] public_pixels_supersede_history_images omitted=%d', omitted)
            req._local_codex_public_images_injected = True
            logger.info('[local_codex] public_group_image_count=%d source_message_count=%d',
                        len(selected), len({r['message_id'] for r, _, _ in selected}))
        return len(selected)

    def _inject_sender(self, event, req):
        self.prune()
        key = self.key(event)
        if key is None:
            return 0
        if any(getattr(getattr(p, 'image_url', None), 'id', None) == SENDER_HISTORY_IMAGE_ID
               for p in req.extra_user_content_parts):
            return 0
        forward = getattr(event, 'get_extra', lambda key: None)('_local_codex_forward')
        if forward is not None:
            # A forward in this exact event must never fall back to an older
            # image belonging to the sender, even if its own fetch failed.
            entry = self.messages.get((self.scope(event), self.message_id(event)))
            if not forward.get('images') or not entry or entry.created > event.created_at:
                return 0
            refs = [ref for ref in entry.references if ref not in (req.image_urls or [])]
            refs = refs[:max(0, 12 - len(req.image_urls or []))]
            if not refs:
                return 0
            entry.auto_event = self.message_id(event) or str(id(event))
            req.image_urls = [*(req.image_urls or []), *refs]
            req.extra_user_content_parts.append(TextPart(text=(
                '[Attached images are from the forwarded chat record in this current message. '
                'Use the attached pixels; text placeholders alone do not show image contents.]')))
            return len(refs)
        if req.image_urls:
            if self.scope(event)[1] != 'group':
                self.discard(event)
            else:
                pending = self.pending.get(key)
                if pending and pending.created <= event.created_at:
                    pending.auto_event = self.message_id(event) or str(id(event))
            return 0
        parts = event.get_messages()
        entry = self.quoted(event)
        source = 'the explicitly quoted message in this same chat'
        if entry is None:
            has_quote = any(isinstance(p, Reply) for p in parts)
            has_image = any(isinstance(p, Image) for p in parts)
            pending = self.pending.get(key)
            if self.scope(event)[1] == 'group':
                event_key = self.message_id(event) or str(id(event))
                split = bool(pending and 0 <= event.created_at - pending.created <= self.split_window
                    and getattr(pending, 'auto_event', None) in (None, event_key))
                explicit_reference = bool(SENDER_IMAGE_REFERENCE.search(req.prompt or ''))
                if not (has_image or split or explicit_reference):
                    return 0
            elif not (has_quote or has_image or re.search(
                r"图|照片|画面|表情|这[个是张]|那个|照着|重画|正面|\b(?:image|picture|photo|screenshot)s?\b",
                req.prompt or '', re.I)):
                return 0
            if any(isinstance(p, Reply) and any(isinstance(c, Image) for c in (p.chain or []))
                   for p in parts):
                return 0  # Never substitute another image for an unreadable explicit quote.
            entry = pending
            source = 'same_sender_reference' if self.scope(event)[1] == 'group' and explicit_reference else 'split_message'
        if not entry or entry.created > event.created_at or not entry.references:
            return 0
        if self.scope(event)[1] == 'group':
            event_key = self.message_id(event) or str(id(event))
            entry.auto_event = event_key
            req.extra_user_content_parts.append(TextPart(text=json.dumps({
                'historical_image_source': {
                    'message_id': getattr(entry, 'source_message_id', ''),
                    'sender_id': getattr(entry, 'source_sender_id', None),
                    'sent_at_unix': entry.created,
                    'age_seconds': max(0, round(event.created_at - entry.created, 3)),
                    'relation': 'explicit_quote' if source.startswith('the explicitly') else source,
                }}, ensure_ascii=False)).mark_as_temp())
            for ref in entry.references:
                req.extra_user_content_parts.append(ImageURLPart(image_url=ImageURLPart.ImageURL(
                    url=ref, id=SENDER_HISTORY_IMAGE_ID)).mark_as_temp())
        else:
            req.image_urls = [*entry.references]
            req.extra_user_content_parts.append(TextPart(text=(
                f'[Attached images are from {source}. Use the actual attached pixels; '
                '[Image] in group text is only a placeholder. Earlier claims that no image '
                'was available do not describe the attachments supplied in this request.]')))
        self.remember(event, entry)
        if self.scope(event)[1] != 'group':
            self.discard(event)
        return len(entry.references)

    def clear(self):
        self.pending.clear()
        self.messages.clear()
