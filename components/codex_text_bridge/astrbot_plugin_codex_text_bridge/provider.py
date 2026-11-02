"""A deliberately small text-only provider for AstrBot 4.29.x."""
from __future__ import annotations

import asyncio
import os
import weakref
from pathlib import Path

from astrbot.core.provider.entities import LLMResponse, TokenUsage
from astrbot.core.provider.provider import Provider
from astrbot.core.provider.register import register_provider_adapter
from astrbot.core.utils.astrbot_path import get_astrbot_data_path

from .runner import BridgeError, CodexRunner, Job, build_prompt


@register_provider_adapter(
    "codex_text_bridge",
    "通过本机已登录的 Codex CLI 提供文字聊天，不支持 AstrBot 工具和多模态。",
    provider_display_name="Codex CLI（文字示例）",
    default_config_tmpl={
        "id": "codex-text", "type": "codex_text_bridge", "enable": True,
        "provider_type": "chat_completion", "model": "default", "key": [],
        "modalities": ["text"], "timeout": 60,
        "codex_executable": "", "workspace": "", "model_reasoning_effort": "",
    },
)
class CodexTextProvider(Provider):
    # Shared by provider instances in one event loop. This does not limit any
    # other process or a separate bridge implementation.
    REQUEST_CONCURRENCY = 4
    _pools = weakref.WeakKeyDictionary()

    def __init__(self, provider_config: dict, provider_settings: dict):
        config = dict(provider_config)
        config["modalities"] = ["text"]
        super().__init__(config, provider_settings)
        executable = Path(config.get("codex_executable") or "")
        if not executable.is_absolute() or not executable.is_file() or (os.name == 'nt' and executable.suffix.lower() != '.exe'):
            raise BridgeError("请设置可执行 Codex CLI 文件的绝对路径。")
        workspace = config.get("workspace") or str(Path(get_astrbot_data_path()) / "plugin_data" / "codex_text_bridge" / "workspace")
        if not Path(workspace).is_absolute():
            raise BridgeError("工作目录必须为绝对路径。")
        self.set_model(config.get("model") or "default")
        try:
            self.timeout = max(1, min(int(config.get("timeout", 60)), 600))
        except (TypeError, ValueError) as exc:
            raise BridgeError("timeout 必须是秒数。") from exc
        self.runner = CodexRunner([str(executable)], Path(workspace), self.get_model(), config.get("model_reasoning_effort") or "")
        self._jobs = set()

    def get_current_key(self):
        return ""

    def get_keys(self):
        return []

    def set_key(self, key):
        if key:
            raise BridgeError("此桥接使用本机 CLI 登录，不接受 API Key。")

    async def get_models(self):
        return list(dict.fromkeys(["default", self.get_model()]))

    @classmethod
    def _pool(cls):
        loop = asyncio.get_running_loop()
        if loop not in cls._pools:
            cls._pools[loop] = asyncio.Semaphore(cls.REQUEST_CONCURRENCY)
        return cls._pools[loop]

    async def _attempt(self, contexts, system_prompt, prompt, parts, model):
        async with self._pool():
            payload = build_prompt(contexts, system_prompt, prompt, parts)
            job = Job()
            self._jobs.add(job)
            task = asyncio.create_task(asyncio.to_thread(self.runner.run, payload, system_prompt or "", job, model))
            try:
                return await asyncio.shield(task)
            except asyncio.CancelledError:
                # A Python thread cannot be cancelled. Stop and join its actual
                # subprocess before releasing this slot or retrying the request.
                await asyncio.to_thread(job.cancel)
                await asyncio.gather(task, return_exceptions=True)
                raise
            finally:
                self._jobs.discard(job)

    async def text_chat(
        self, prompt=None, session_id=None, image_urls=None, audio_urls=None,
        func_tool=None, contexts=None, system_prompt=None, tool_calls_result=None,
        model=None, extra_user_content_parts=None, tool_choice="auto", **kwargs,
    ):
        if image_urls or audio_urls or func_tool or tool_calls_result or tool_choice == "required":
            raise BridgeError("文字桥接不支持图片、音频或工具调用。")
        messages = self._ensure_message_to_dicts(contexts)
        # The per-attempt deadline covers queueing, prompt preparation and the
        # CLI. Only a timeout retries once; no tool or send is performed here.
        for attempt in range(2):
            try:
                result = await asyncio.wait_for(
                    self._attempt(messages, system_prompt, prompt, extra_user_content_parts, model),
                    timeout=self.timeout,
                )
                break
            except asyncio.TimeoutError as exc:
                if attempt:
                    raise BridgeError("Codex 请求两次超时，已结束本轮。") from exc
        usage = result.usage
        try:
            total = max(0, int(usage.get("input_tokens", 0)))
            cached = max(0, min(total, int(usage.get("cached_input_tokens", 0))))
            output = max(0, int(usage.get("output_tokens", 0)))
        except (TypeError, ValueError):
            total, cached, output = 0, 0, 0
        return LLMResponse(
            role="assistant", completion_text=result.text, id=result.thread_id,
            usage=TokenUsage(input_other=total - cached, input_cached=cached, output=output),
        )

    async def text_chat_stream(self, **kwargs):
        # Compatible with AstrBot's stream protocol, but not a live token stream.
        result = await self.text_chat(**kwargs)
        yield LLMResponse(role="assistant", completion_text=result.completion_text, is_chunk=True, id=result.id)
        yield result

    async def terminate(self):
        await asyncio.gather(*(asyncio.to_thread(job.cancel) for job in tuple(self._jobs)))
