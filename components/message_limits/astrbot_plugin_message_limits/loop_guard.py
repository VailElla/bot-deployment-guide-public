"""Bound QQ group reply attempts regardless of sender identity or wake method."""
from __future__ import annotations

import hashlib
import json
import math
import time
from pathlib import Path

from astrbot.api.message_components import Plain, Reply


class LoopGuard:
    MARKER = '_local_codex_loop_admitted'

    def __init__(self, path: Path, *, window=120, sender_window=120, sender_limit=3, group_limit=6,
                 duplicate_ttl=300, proactive_pause=120,
                 max_entries=2048):
        self.path = path
        self.window = window
        self.sender_window = sender_window
        self.sender_limit = sender_limit
        self.group_limit = group_limit
        self.duplicate_ttl = duplicate_ttl
        self.proactive_pause = proactive_pause
        self.max_entries = max_entries
        self.attempts = {}
        self.duplicates = {}
        self.proactive_cooldowns = {}
        try:
            data = json.loads(path.read_text(encoding='utf-8'))
            if isinstance(data, dict) and data.get('version') == 1:
                # Ignore all superseded personal pause timers on upgrade.
                for name in ('attempts', 'duplicates', 'proactive_cooldowns'):
                    if isinstance(data.get(name), dict):
                        setattr(self, name, data[name])
        except (OSError, ValueError):
            pass
        self.prune(time.time())

    @staticmethod
    def digest(value):
        return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                         default=str).encode()).hexdigest()

    def keys(self, event):
        if event.get_platform_name() != 'aiocqhttp' or not event.get_group_id():
            return None
        group = [str(event.get_platform_id()), str(event.get_self_id()), str(event.get_group_id())]
        return self.digest(['group', *group]), self.digest(['sender', *group, str(event.get_sender_id())])

    def signature(self, event, sender):
        # Quote IDs change on every turn of a bot echo loop. Ignore that envelope
        # but retain actual content, mentions and attachment identities.
        parts = [{'type': 'Plain', 'text': ' '.join(part.text.split()).casefold()}
                 if isinstance(part, Plain) else part.dict()
                 for part in event.get_messages() if not isinstance(part, Reply)]
        if not parts:
            parts = [' '.join(event.message_str.split()).casefold()]
        return self.digest([sender, parts])

    @staticmethod
    def number(value):
        return isinstance(value, (float, int)) and not isinstance(value, bool) and math.isfinite(value)

    def prune(self, now):
        self.attempts = {
            k: [t for t in values if self.number(t) and now - max(self.window, self.sender_window) < t <= now]
            for k, values in self.attempts.items()
            if isinstance(k, str) and len(k) == 64 and isinstance(values, list)
        }
        self.attempts = dict(sorted(((k, v) for k, v in self.attempts.items() if v),
                                    key=lambda item: item[1][-1])[-self.max_entries:])
        for name, ttl in [('duplicates', self.duplicate_ttl),
                          ('proactive_cooldowns', self.proactive_pause)]:
            entries = getattr(self, name)
            setattr(self, name, dict(sorted(
                ((k, t) for k, t in entries.items() if isinstance(k, str) and len(k) == 64
                 and self.number(t) and now < t <= now + ttl),
                key=lambda item: item[1])[-self.max_entries:]))

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix('.tmp')
        temporary.write_text(json.dumps({'version': 1, 'attempts': self.attempts,
            'duplicates': self.duplicates,
            'proactive_cooldowns': self.proactive_cooldowns}), encoding='utf-8')
        temporary.replace(self.path)

    def blocked_reason(self, event):
        keys = self.keys(event)
        if keys is None:
            return None
        if str(event.get_sender_id()) == str(event.get_self_id()):
            return 'self'
        self.prune(time.time())
        return None

    def admit(self, event):
        """Reserve synchronously before queuing; repeated hooks are idempotent."""
        keys = self.keys(event)
        if keys is None:
            return None
        marker = event.get_extra(self.MARKER)
        if marker is not None:
            return marker or None
        reason = self.blocked_reason(event)
        group, sender = keys
        now = time.time()
        signature = self.signature(event, sender)
        if reason is None and signature in self.duplicates:
            reason = 'duplicate'
        if reason is None:
            if len(self.attempts.get(group, ())) >= self.group_limit:
                # No extra group-wide pause: capacity returns as entries age out.
                reason = 'group_limit'
            elif sum(now - self.sender_window < t <= now
                     for t in self.attempts.get(sender, ())) >= self.sender_limit:
                reason = 'sender_limit'
        if reason is None:
            for key in keys:
                self.attempts.setdefault(key, []).append(now)
            self.duplicates[signature] = now + self.duplicate_ttl
            # All reply attempts suppress random interruptions across the group,
            # including while an explicit reply is still queued/generating.
            self.proactive_cooldowns[group] = now + self.proactive_pause
        event.set_extra(self.MARKER, reason or '')
        self.prune(now)
        # In-memory reservations remain active even if the disk is unavailable.
        try:
            self.save()
        except OSError:
            event.set_extra('_local_codex_loop_persistence_failed', True)
        return reason

    def allow_proactive(self, event):
        keys = self.keys(event)
        if keys is None:
            return True
        if self.blocked_reason(event):
            return False
        return keys[0] not in self.proactive_cooldowns

    def remember(self, event):
        keys = self.keys(event)
        result = event.get_result()
        if (keys is None or not result or not result.is_model_result()
                or not getattr(event, '_has_send_oper', False) or event.is_stopped()):
            return False
        # Generation can take minutes: also begin the quiet period at delivery.
        now = time.time()
        self.prune(now)
        self.proactive_cooldowns[keys[0]] = now + self.proactive_pause
        self.save()
        return True

    def protect(self, event):
        reason = self.admit(event)
        if reason:
            event.set_extra('_local_codex_loop_blocked', reason)
            event.stop_event()
        return reason
