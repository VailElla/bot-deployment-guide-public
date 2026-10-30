"""Keep a short, sender-scoped QQ reply window across backend reloads."""
from __future__ import annotations

import hashlib
import json
import math
import time
from pathlib import Path

from astrbot.api.message_components import At, AtAll, Plain, Reply


class GroupFollowup:
    def __init__(self, state_path: Path, ttl=60, max_entries=512):
        self.path = state_path
        self.ttl = ttl
        self.proactive_ttl = 300
        self.max_entries = max_entries
        self.windows = {}
        self.proactive_cooldowns = {}
        self.proactive_group_cooldowns = {}
        try:
            state = json.loads(self.path.read_text(encoding="utf-8"))
            now = time.time()
            # Legacy rolling windows have no reliable trigger provenance.
            if (isinstance(state, dict) and state.get("version") in (2, 3, 4)
                    and isinstance(state.get("windows"), dict)):
                for name in ['windows', 'proactive_cooldowns', 'proactive_group_cooldowns']:
                    if name == 'windows' and state['version'] == 2:
                        continue
                    entries = state.get(name, {})
                    if isinstance(entries, dict):
                        setattr(self, name, {
                            key: (min(until, now + self.proactive_ttl)
                                  if name == 'proactive_group_cooldowns' else until)
                            for key, until in entries.items()
                            if isinstance(key, str) and len(key) == 64
                            and isinstance(until, (float, int)) and math.isfinite(until)
                            and now < until <= now + (self.ttl if name == 'windows' else (120 if name == 'proactive_cooldowns' else max(600, self.proactive_ttl)))
                        })
        except (OSError, ValueError):
            pass
        self.prune()

    @staticmethod
    def key(event):
        sender = str(event.get_sender_id() or "")
        group = str(event.get_group_id() or "")
        if (event.get_platform_name() != "aiocqhttp" or not group or not sender
                or sender == str(event.get_self_id())):
            return None
        scope = [event.get_platform_id(), group, sender]
        return hashlib.sha256(json.dumps(scope).encode()).hexdigest()

    @classmethod
    def proactive_key(cls, event):
        if cls.key(event) is None:
            return None
        scope = [event.get_platform_id(), str(event.get_group_id())]
        return hashlib.sha256(json.dumps(scope).encode()).hexdigest()

    def prune(self):
        now = time.time()
        for name in ['windows', 'proactive_cooldowns', 'proactive_group_cooldowns']:
            setattr(self, name, dict(sorted(
                ((key, until) for key, until in getattr(self, name).items() if until > now),
                key=lambda item: item[1],
            )[-self.max_entries:]))

    def save(self):
        self.prune()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps({"version": 4, "windows": self.windows,
                                        "proactive_cooldowns": self.proactive_cooldowns,
                                        "proactive_group_cooldowns": self.proactive_group_cooldowns}), encoding="utf-8")
        temporary.replace(self.path)

    def remember(self, event):
        key = self.key(event)
        result = event.get_result()
        # AstrBot also calls after_message_sent when an adapter raised an error.
        # Its send flag is set only after the QQ adapter successfully returns.
        if (key is None or not result or not result.is_model_result()
                or not getattr(event, "_has_send_oper", False)
                or event.is_stopped()):
            return False
        if event.get_extra('_local_codex_proactive_reply'):
            self.reserve_proactive(event)
            return False
        if event.get_extra('_local_codex_group_trigger') != 'explicit':
            return False
        self.windows[key] = time.time() + self.ttl
        self.save()
        return True

    def allow_proactive(self, event):
        if str(event.get_sender_id()) == str(event.get_self_id()):
            return False
        # Explicit wakes and real follow-ups use the normal agent path. The
        # native random-reply predicate already excludes those events.
        if event.is_at_or_wake_command:
            return True
        if event.message_str.strip().startswith(('/', '／')):
            return False
        if any(isinstance(part, AtAll)
               or isinstance(part, At) and str(part.qq) != str(event.get_self_id())
               or isinstance(part, Reply) and str(part.sender_id) != str(event.get_self_id())
               for part in event.get_messages()):
            return False
        self.prune()
        return (self.proactive_key(event) is not None
                and self.key(event) not in self.proactive_cooldowns
                and self.proactive_key(event) not in self.proactive_group_cooldowns)

    def reserve_proactive(self, event):
        key = self.proactive_key(event)
        if key is not None:
            self.proactive_group_cooldowns[key] = time.time() + self.proactive_ttl
            self.save()

    def activate(self, event):
        key = self.key(event)
        if key is None or event.is_stopped():
            return False
        # A real mention restarts the group random-reply cooldown, without
        # delaying normal explicit replies or treating plain wake words as mentions.
        if (not event.get_extra("_local_codex_mention_cooldown_reset")
                and any(isinstance(part, At) and not isinstance(part, AtAll)
                        and str(part.qq) == str(event.get_self_id())
                        for part in event.get_messages())):
            event.set_extra("_local_codex_mention_cooldown_reset", True)
            try:
                self.reserve_proactive(event)
            except OSError:
                # The in-memory reservation is still active if persistence fails.
                pass
        # Capture provenance before this or later handlers mutate framework
        # flags. Re-entering a listener must not relabel a continuation.
        if event.get_extra("_local_codex_group_trigger") is not None:
            return False
        event.set_extra("_local_codex_group_trigger", "passive")
        self.prune()
        parts = event.get_messages()
        text = event.message_str.strip()
        addressed_elsewhere = any(
            isinstance(part, AtAll)
            or isinstance(part, At) and str(part.qq) != str(event.get_self_id())
            or isinstance(part, Reply) and str(part.sender_id) != str(event.get_self_id())
            for part in parts
        )
        if text.startswith(("/", "／")) or addressed_elsewhere:
            if self.windows.pop(key, None) is not None:
                self.save()
            return False
        if event.is_at_or_wake_command:
            event.set_extra("_local_codex_group_trigger", "explicit")
            return False
        if key not in self.windows:
            return False
        # Only new text messages can consume a window. Attachment-only messages
        # retain the existing explicit image-question flow.
        if not any(isinstance(part, Plain) and part.text.strip() for part in parts):
            return False
        created = getattr(event, "created_at", time.time())
        opened = self.windows[key] - self.ttl
        if not opened <= created <= self.windows[key]:
            return False
        # Reserve the only follow-up before dispatch, including across reloads.
        self.windows.pop(key)
        self.save()
        event.is_wake = True
        event.is_at_or_wake_command = True
        event.set_extra("_local_codex_group_trigger", "followup")
        event.set_extra("_local_codex_group_followup", True)
        return True
