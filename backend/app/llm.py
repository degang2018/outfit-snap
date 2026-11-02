"""极简 LLM 客户端,用于把剧本切成镜头。

默认走本机 Ollama 的**原生** /api/chat,并且默认强制 CPU 推理(num_gpu=0):
这台机器的显卡平时被 ComfyUI 占满(16GB),走 GPU 会直接 cudaMalloc 失败;
切分镜头是很小的任务,CPU 跑几秒就够,且不会和出片抢显存。

也支持 OpenAI 兼容接口(LLM_API_STYLE=openai),换成 vLLM / OpenAI / DeepSeek 只改环境变量。
"""
from __future__ import annotations

import json
import re
import urllib.error
import urllib.request

from .config import settings

_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.S)
_JSON_OBJ = re.compile(r"\{.*\}", re.S)
_THINK = re.compile(r"<think(?:ing)?>.*?</think(?:ing)?>", re.S | re.I)


def _base() -> str:
    base = settings.llm_base_url.rstrip("/")
    # 两种后端都在下面自己拼路径(ollama: /api/chat,openai: /v1/chat/completions),
    # 所以 base 里带不带 /v1 都统一去掉,避免拼成 /v1/v1/...
    if base.endswith("/v1"):
        base = base[:-3].rstrip("/")
    return base


def _headers() -> dict:
    headers = {"Content-Type": "application/json"}
    if settings.llm_api_key and settings.llm_api_style == "openai":
        headers["Authorization"] = "Bearer " + settings.llm_api_key
    return headers


def _url(path: str) -> str:
    return _base() + path


def available(timeout: int = 4) -> bool:
    if not settings.llm_enabled:
        return False
    path = "/api/tags" if settings.llm_api_style == "ollama" else "/v1/models"
    try:
        req = urllib.request.Request(_url(path), headers=_headers())
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return getattr(r, "status", 200) == 200
    except Exception:
        return False


def _extract_json(text: str) -> dict | None:
    if not text:
        return None
    text = _THINK.sub("", text).strip()
    for candidate in (text, *(_FENCE.findall(text))):
        try:
            data = json.loads(candidate)
            if isinstance(data, dict):
                return data
        except Exception:
            pass
    match = _JSON_OBJ.search(text)
    if match:
        try:
            data = json.loads(match.group(0))
            if isinstance(data, dict):
                return data
        except Exception:
            pass
    return None


def _messages(system: str, user: str, images: list[tuple[str, str]] | None) -> list[dict]:
    """有图片时用 OpenAI 多模态的 content 块数组,否则就是普通纯文本。"""
    if not images:
        return [{"role": "system", "content": system}, {"role": "user", "content": user}]
    blocks: list[dict] = [{"type": "text", "text": user}]
    for b64, mime in images:
        blocks.append({"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}", "detail": "low"}})
    return [{"role": "system", "content": system}, {"role": "user", "content": blocks}]


def _call(messages: list[dict], temperature: float, json_mode: bool, model: str,
          timeout: int | None = None) -> str:
    if settings.llm_api_style == "ollama":
        url = _url("/api/chat")
        payload = {
            "model": model,
            "messages": messages,
            "stream": False,
            "options": {
                "temperature": temperature,
                "num_gpu": settings.llm_num_gpu,    # 0 = 纯 CPU,避免和 ComfyUI 抢显存
            },
        }
        if json_mode:
            payload["format"] = "json"              # Ollama 侧强制 JSON 输出
    else:
        url = _url("/v1/chat/completions")
        payload = {"model": model, "messages": messages, "temperature": temperature, "stream": False}
        if json_mode:
            payload["response_format"] = {"type": "json_object"}

    req = urllib.request.Request(url, data=json.dumps(payload).encode(), headers=_headers(), method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout or settings.llm_timeout) as r:
            body = json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"HTTP {e.code} {e.read().decode()[:300]}") from e

    if settings.llm_api_style == "ollama":
        return (body.get("message") or {}).get("content") or ""
    return (((body.get("choices") or [{}])[0].get("message") or {}).get("content")) or ""


def chat_json(system: str, user: str, temperature: float = 0.3,
              timeout: int | None = None) -> dict | None:
    """让模型输出一个 JSON 对象;拿不到可解析内容时返回 None。

    timeout 覆盖 settings.llm_timeout:排分镜这种小任务用一个更短的硬上限 ——
    后端过载(连接建起来却不回,实测 DeepSeek 那阵)时会一直挂着,UI 上就一直停在
    「生成提示词 —— 编排换装顺序与动作」不动(用户报"怎么一直在这个状态不动啊")。
    """
    return _extract_json(_call(_messages(system, user, None), temperature, True,
                               settings.llm_model, timeout))


def vision_model() -> str:
    """读图用的视觉模型(DeepSeek 是 deepseek-flash)。"""
    return settings.llm_vision_model or settings.llm_model


def chat_json_image(
    system: str,
    user: str,
    images: list[tuple[str, str]],
    *,
    model: str | None = None,
    temperature: float = 0.2,
) -> dict | None:
    """带图片的 JSON 调用。

    这里**不**设置 response_format:部分多模态后端对「传图 + 强制 JSON」同时使用支持不好,
    直接靠 _extract_json 从返回文本里把 JSON 抠出来更稳。
    """
    if settings.llm_api_style != "openai":
        return None
    out = _call(_messages(system, user, images), temperature, False, model or settings.llm_model)
    return _extract_json(out)


def chat_text(
    system: str,
    user: str,
    *,
    images: list[tuple[str, str]] | None = None,
    model: str | None = None,
    temperature: float = 0.3,
) -> str | None:
    """返回模型的纯文本输出(读图描述、写剧本这类不是 JSON 的场景)。"""
    if images and settings.llm_api_style != "openai":
        return None                                  # 传图只有 OpenAI 兼容后端支持
    out = _call(_messages(system, user, images), temperature, False, model or settings.llm_model)
    out = _THINK.sub("", out).strip()
    return out or None
