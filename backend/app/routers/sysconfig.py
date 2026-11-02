"""全局接口配置:ComfyUI 地址 + LLM(文本 / 视觉)接口与 Key。

这一页改的是**全局**配置,所有项目共用;值会写回 web/backend/.env,
同时立刻更新内存里的 settings,不用重启。Key 只保存在本机,GET 只回掩码。
"""
from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from ..config import _write_env, settings
from ..deps import get_current_user
from ..models import User

router = APIRouter(prefix="/sysconfig", tags=["sysconfig"])

_STYLES = ("ollama", "openai")


def _mask(key: str) -> str:
    if not key:
        return ""
    if len(key) <= 8:
        return "****"
    return key[: 3] + "…" + key[-4:]


def _public() -> dict:
    return {
        "comfy_api": settings.comfy_api,
        "llm_enabled": bool(settings.llm_enabled),
        "llm_api_style": settings.llm_api_style,
        "llm_base_url": settings.llm_base_url,
        "llm_model": settings.llm_model,
        "llm_vision_model": settings.llm_vision_model,
        "llm_timeout": int(settings.llm_timeout),
        "has_api_key": bool(settings.llm_api_key),
        "api_key_hint": _mask(settings.llm_api_key),
    }


def _clean_url(value: str, name: str) -> str:
    value = (value or "").strip().rstrip("/")
    p = urllib.parse.urlparse(value)
    if p.scheme not in ("http", "https") or not p.netloc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"{name} 必须是 http(s) 地址")
    return value


def _llm_base(value: str) -> str:
    """OpenAI 兼容接口的 base 统一不带 /v1 —— 客户端会自己拼 /v1/chat/completions。"""
    value = (value or "").strip().rstrip("/")
    if value.endswith("/v1"):
        value = value[:-3].rstrip("/")
    return value


class SysConfigIn(BaseModel):
    comfy_api: str = Field(min_length=1, max_length=500)
    llm_enabled: bool = True
    llm_api_style: Literal["ollama", "openai"] = "ollama"
    llm_base_url: str = Field(min_length=1, max_length=500)
    # 空 = 保持原 Key 不变;要清空请把 clear_api_key 设为 true
    llm_api_key: Optional[str] = Field(default=None, max_length=500)
    clear_api_key: bool = False
    llm_model: str = Field(min_length=1, max_length=120)
    llm_vision_model: str = Field(default="", max_length=120)
    llm_timeout: int = Field(default=300, ge=10, le=1800)


class TestComfyIn(BaseModel):
    comfy_api: str = ""


class TestLlmIn(BaseModel):
    llm_api_style: Literal["ollama", "openai"] = "ollama"
    llm_base_url: str = ""
    llm_api_key: Optional[str] = None
    llm_model: str = ""


@router.get("")
def get_config(user: User = Depends(get_current_user)) -> dict:
    return _public()


@router.put("")
def save_config(payload: SysConfigIn, user: User = Depends(get_current_user)) -> dict:
    comfy_api = _clean_url(payload.comfy_api, "ComfyUI 地址")
    base = _llm_base(_clean_url(payload.llm_base_url, "LLM Base URL"))
    updates = {
        "COMFY_API": comfy_api,
        "LLM_ENABLED": "1" if payload.llm_enabled else "0",
        "LLM_API_STYLE": payload.llm_api_style,
        "LLM_BASE_URL": base,
        "LLM_MODEL": payload.llm_model.strip(),
        "LLM_VISION_MODEL": (payload.llm_vision_model or "").strip(),
        "LLM_TIMEOUT": str(int(payload.llm_timeout)),
    }
    key = (payload.llm_api_key or "").strip()
    if payload.clear_api_key:
        updates["LLM_API_KEY"] = ""
    elif key and not key.startswith("****"):
        updates["LLM_API_KEY"] = key

    _write_env(updates)
    settings.comfy_api = comfy_api
    settings.llm_enabled = bool(payload.llm_enabled)
    settings.llm_api_style = payload.llm_api_style
    settings.llm_base_url = base
    settings.llm_model = payload.llm_model.strip()
    settings.llm_vision_model = (payload.llm_vision_model or "").strip()
    settings.llm_timeout = int(payload.llm_timeout)
    if "LLM_API_KEY" in updates:
        settings.llm_api_key = updates["LLM_API_KEY"]
    return _public()


def _get_json(url: str, headers: dict | None = None, timeout: int = 8):
    req = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode() or "{}")


@router.post("/test-comfy")
def test_comfy(payload: TestComfyIn, user: User = Depends(get_current_user)) -> dict:
    url = _clean_url(payload.comfy_api or settings.comfy_api, "ComfyUI 地址")
    try:
        data = _get_json(url + "/system_stats")
        sysinfo = (data.get("system") or {}) if isinstance(data, dict) else {}
        detail = "ComfyUI 连接成功"
        if sysinfo:
            detail += f" · {sysinfo.get('os', '')}".rstrip()
        return {"ok": True, "detail": detail}
    except Exception as exc:
        return {"ok": False, "detail": f"连接失败:{exc}"}


@router.post("/test-llm")
def test_llm(payload: TestLlmIn, user: User = Depends(get_current_user)) -> dict:
    base = _llm_base(payload.llm_base_url or settings.llm_base_url)
    try:
        base = _clean_url(base, "LLM Base URL")
    except HTTPException as exc:
        return {"ok": False, "detail": exc.detail}
    key = (payload.llm_api_key or "").strip()
    if not key or key.startswith("****"):
        key = settings.llm_api_key
    headers = {"Content-Type": "application/json"}
    if payload.llm_api_style == "openai" and key:
        headers["Authorization"] = "Bearer " + key
    path = "/api/tags" if payload.llm_api_style == "ollama" else "/v1/models"
    try:
        data = _get_json(base + path, headers=headers)
        names: list[str] = []
        if payload.llm_api_style == "ollama":
            names = [str(m.get("name") or "") for m in (data.get("models") or [])]
        else:
            names = [str(m.get("id") or "") for m in (data.get("data") or [])]
        model = (payload.llm_model or settings.llm_model or "").strip()
        found = any(n == model or n.split(":")[0] == model for n in names) if model else False
        detail = f"LLM 连接成功 · 发现 {len(names)} 个模型"
        if model:
            detail += " · " + ("模型已找到" if found else f"但没找到 {model}")
            if not found and names:
                detail += ";可用:" + " / ".join(names[:6])
        return {"ok": True, "detail": detail, "models": names[:20]}
    except urllib.error.HTTPError as exc:
        return {"ok": False, "detail": f"连接失败:HTTP {exc.code}"}
    except Exception as exc:
        return {"ok": False, "detail": f"连接失败:{exc}"}
