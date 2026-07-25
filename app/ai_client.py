"""
AI 调用模块
===========
统一走 Runway 网关（AWS Bedrock / Anthropic Messages 协议），使用 httpx 直接调 HTTP。
不使用任何厂商 SDK（openai / anthropic 等），避免 SDK 锁定。

认证：api-key: <key>（不是 Authorization: Bearer ...）
请求体：Anthropic Messages 格式，不传 model 字段（key 决定模型）
"""

import json
import base64
import logging

import httpx

from app.config import AI_BASE_URL, AI_API_KEY, is_ai_enabled

logger = logging.getLogger(__name__)

# ai.base_url 已包含 /openai 前缀，拼接时直接接 /bedrock_runtime/...
# 不要再拼 /openai/，否则双前缀导致 Runway 404
_INVOKE_URL = f"{AI_BASE_URL}/bedrock_runtime/model/invoke" if AI_BASE_URL else ""
_STREAM_URL = f"{AI_BASE_URL}/bedrock_runtime/model/invoke-with-response-stream" if AI_BASE_URL else ""
_HEADERS = {"api-key": AI_API_KEY, "Content-Type": "application/json"}


def chat_complete(
    messages: list[dict],
    system: str = "",
    max_tokens: int = 1024,
    temperature: float = 0.1,
    timeout: int = 120,
) -> str:
    """
    非流式 LLM 调用。
    返回模型的文本回复内容；出错时返回空字符串（由调用方兜底处理）。

    messages 格式：[{"role": "user", "content": "..."}, ...]
    只允许 user / assistant 角色，system 放顶层字段。
    """
    if not is_ai_enabled():
        logger.warning("[ai_client] AI 未配置，跳过调用")
        return ""

    body: dict = {
        "anthropic_version": "bedrock-2023-05-31",
        "max_tokens": max_tokens,
        "messages": messages,
        # 不传 model 字段，由 api_key 绑定的模型决定
    }
    if system:
        body["system"] = system

    try:
        r = httpx.post(_INVOKE_URL, headers=_HEADERS, json=body, timeout=timeout)
        r.raise_for_status()
        data = r.json()
        # content 是 list，取 type=="text" 的块
        text = next(
            (b["text"] for b in data.get("content", []) if b.get("type") == "text"),
            "",
        )
        return text
    except Exception:
        # 不把异常原文下发到调用方（可能含 endpoint / key 信息）
        logger.exception("[ai_client] 非流式调用失败")
        return ""


def chat_complete_with_tools(
    messages: list[dict],
    tools: list[dict],
    system: str = "",
    max_tokens: int = 1024,
    timeout: int = 120,
) -> dict:
    """
    带 tool_use 的 LLM 调用（用于 Agent2 的工具调用循环）。
    返回原始响应 dict（含 content、stop_reason 等），出错时返回 {"error": True}。

    Anthropic Messages 格式支持 tools 字段，Bedrock 原生支持 tool_schema。
    """
    if not is_ai_enabled():
        logger.warning("[ai_client] AI 未配置，跳过工具调用")
        return {"error": True}

    body: dict = {
        "anthropic_version": "bedrock-2023-05-31",
        "max_tokens": max_tokens,
        "messages": messages,
        "tools": tools,
    }
    if system:
        body["system"] = system

    try:
        r = httpx.post(_INVOKE_URL, headers=_HEADERS, json=body, timeout=timeout)
        r.raise_for_status()
        return r.json()
    except Exception:
        logger.exception("[ai_client] 工具调用失败")
        return {"error": True}
