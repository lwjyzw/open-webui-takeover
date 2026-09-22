"""
title: Custom OpenAI-Compatible Model
author: your-name
version: 0.2.0
description: 通过 Valves 配置任意 OpenAI 兼容 API（DeepSeek / Kimi / 通义 / vLLM / Ollama 等），流式输出
required_open_webui_version: 0.6.0
"""

import json
from typing import AsyncGenerator, Iterator, Union
from urllib.parse import urlsplit

import httpx

from pydantic import BaseModel, Field


class Pipe:
    class Valves(BaseModel):
        # 管理员设置项：Admin Panel -> Functions -> 本插件 -> Valves（齿轮）
        api_base: str = Field(
            default="https://api.deepseek.com/v1",
            description="OpenAI 兼容 API 地址（不含 /chat/completions）",
        )
        api_key: str = Field(default="", description="API Key")
        model_ids: str = Field(
            default="deepseek-chat, deepseek-reasoner",
            description="暴露给用户的模型 ID，逗号分隔，每个 ID 会生成一个独立模型项",
        )
        request_timeout: float = Field(
            default=120.0, ge=5.0, le=600.0, description="请求超时（秒）"
        )
        allow_insecure_http: bool = Field(
            default=False,
            description="仅连接本地 Ollama/vLLM 时开启；公网 API 应保持关闭",
        )


    class UserValves(BaseModel):
        # 每个用户自己的设置项（可选），用户在模型设置里可见
        temperature: float = Field(default=0.7, ge=0.0, le=2.0, description="生成温度")
        max_tokens: int = Field(default=2048, ge=1, description="单次最大生成 token 数")

    def __init__(self):
        self.valves = self.Valves()
        self.name = "Custom: "  # manifold 名称前缀，直接字符串拼接，无自动分隔符：
        # functions.py:109-110 是 f'{function_module.name}{sub_pipe_name}'，
        # 所以下拉框里显示的就是 "Custom: deepseek-chat"。
        # 注意模型 ID 另有一套拼法：f'{pipe.id}.{p["id"]}'（functions.py:106）→ "<pipe_id>.deepseek-chat"

    def pipes(self):
        """manifold 模式：返回的每个 id 都会成为模型下拉框里的一个独立模型"""
        if not self.valves.api_key:
            return [{"id": "error", "name": "请先在 Valves 中配置 API Key"}]
        return [
            {"id": model_id.strip(), "name": model_id.strip()}
            for model_id in self.valves.model_ids.split(",")
            if model_id.strip()
        ]

    def _endpoint(self) -> str:
        base = self.valves.api_base.strip().rstrip("/")
        parsed = urlsplit(base)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("API 地址必须是完整的 HTTP(S) 地址")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("API 地址不能包含账号、密码、查询参数或片段")
        if parsed.scheme == "http" and not self.valves.allow_insecure_http:
            raise ValueError("HTTP 连接未启用；公网 API 请使用 HTTPS")
        return f"{base}/chat/completions"

    async def pipe(
        self,
        body: dict,
        __user__: dict,
        __event_emitter__=None,
    ) -> Union[str, AsyncGenerator, Iterator]:
        """
        body 是 OpenAI 格式的请求体：{"model": "custom_pipe_id.deepseek-chat", "messages": [...], "stream": True}
        返回 str / Generator / AsyncGenerator 均可，框架自动处理 SSE 包装。
        """
        # body["model"] 形如 "my_pipe.deepseek-chat"，取后半段作为真实模型名
        real_model = str(body.get("model", "")).split(".", 1)[-1].strip()
        messages = body.get("messages")
        if not real_model or not isinstance(messages, list) or not messages:
            return "请求缺少有效的模型或消息"
        if not self.valves.api_key:
            return "请先在 Pipe 的 Valves 中配置 API Key"

        user_valves = __user__.get("valves", self.UserValves())

        payload = {
            "model": real_model,
            "messages": messages,
            "stream": True,
            "temperature": user_valves.temperature,
            "max_tokens": user_valves.max_tokens,
        }

        for key in (
            "top_p",
            "stop",
            "presence_penalty",
            "frequency_penalty",
            "response_format",
            "tools",
            "tool_choice",
        ):
            if key in body:
                payload[key] = body[key]

        if __event_emitter__:
            await __event_emitter__(
                {
                    "type": "status",
                    "data": {
                        "description": f"正在调用 {real_model}...",
                        "done": False,
                    },
                }
            )

        async def _stream() -> AsyncGenerator[str, None]:
            try:
                endpoint = self._endpoint()
                async with httpx.AsyncClient(
                    timeout=self.valves.request_timeout, follow_redirects=False
                ) as client:
                    async with client.stream(
                        "POST",
                        endpoint,
                        headers={
                            "Authorization": f"Bearer {self.valves.api_key}",
                            "Accept": "text/event-stream",
                        },
                        json=payload,
                    ) as resp:
                        if resp.status_code != 200:
                            await resp.aread()
                            yield f"API 请求失败（HTTP {resp.status_code}）"
                            return
                        async for line in resp.aiter_lines():
                            if not line.startswith("data:"):
                                continue
                            data = line[5:].strip()
                            if not data or data == "[DONE]":
                                continue
                            try:
                                chunk = json.loads(data)
                            except json.JSONDecodeError:
                                continue
                            choices = chunk.get("choices") or []
                            delta = choices[0].get("delta", {}) if choices else {}
                            delta = delta.get("content")
                            if delta:
                                yield delta
            except (httpx.HTTPError, ValueError):
                yield "\n\n[模型服务连接失败，请管理员检查 API 地址、TLS 和网络设置]"
            except Exception:
                yield "\n\n[模型调用失败，请稍后重试]"
            finally:
                if __event_emitter__:
                    await __event_emitter__(
                        {"type": "status", "data": {"description": "完成", "done": True}}
                    )

        return _stream()
