"""
title: Message Audit & Guard Filter
author: your-name
version: 0.2.0
description: 演示 inlet/outlet/stream 三个钩子：请求前改写、响应后统计、流中监控。可挂到任意模型上。
required_open_webui_version: 0.6.0
"""

import time

from pydantic import BaseModel, Field


class Filter:
    # 与 Pipe 不同：Filter 没有 pipes()，只有 inlet / outlet / stream 三个钩子
    # 框架通过 inspect.signature 反射注入参数，写哪个就注入哪个（backend/open_webui/utils/filter.py:192）

    class Valves(BaseModel):
        blocked_words: str = Field(
            default="", description="黑名单关键词，逗号分隔；命中则拒绝请求"
        )
        append_system_note: bool = Field(
            default=True, description="是否在 inlet 注入一条系统提示"
        )

    class UserValves(BaseModel):
        max_length: int = Field(default=20000, ge=1, description="用户输入最大字符数")

    def __init__(self):
        self.valves = self.Valves()
        self.file_handler = True  # inlet 跳过文件处理，只看文本（filter.py:184）

    async def inlet(self, body: dict, __user__: dict) -> dict:
        """请求发给模型之前调用。可修改 body（增删消息、改参数），抛异常则拒绝本次请求。"""
        user_valves = __user__.get("valves", self.UserValves())
        last_message = body["messages"][-1]["content"]

        if isinstance(last_message, str) and len(last_message) > user_valves.max_length:
            raise Exception(f"输入超过 {user_valves.max_length} 字符，已被 Guard Filter 拦截")

        blocked = [w.strip().casefold() for w in self.valves.blocked_words.split(",") if w.strip()]
        if isinstance(last_message, str):
            normalized_message = last_message.casefold()
            for word in blocked:
                if word in normalized_message:
                    raise Exception(f"输入包含被拦截的关键词，已被 Guard Filter 拦截")

        if self.valves.append_system_note:
            body["messages"].insert(
                0,
                {
                    "role": "system",
                    "content": "[audit] 此请求已通过本地输入检查，请正常回答。",
                },
            )
        return body

    async def stream(self, event: dict) -> dict:
        """流式过程中调用。注入的参数名**固定为 event**（不是 form_data）：
        filter.py:136 对 stream 类型构造的是 {'event': form_data}，写错名字会直接
        TypeError: stream() missing 1 required positional argument。
        event 是已解析好的 SSE 事件字典（middleware.py:4842 / 6337 调用点）；
        原样 return 即透传，改写 event['data'] 即可修改流式内容。"""
        return event

    async def outlet(self, body: dict, __user__: dict) -> dict:
        """完整响应结束后调用。body 里包含 assistant 的回复，适合做统计/审计/后处理。"""
        messages = body.get("messages", [])
        assistant_text = next(
            (m["content"] for m in reversed(messages) if m.get("role") == "assistant"), ""
        )
        # 真实场景：这里写审计日志、调用审核 API、更新用量统计等
        print(f"[audit] reply_chars={len(str(assistant_text))} took_marker={time.time():.0f}")
        return body
