"""AI Outfit Video Studio —— Backend entry point.

启动方式:
    python run.py

默认监听 0.0.0.0:8000,浏览器打开 http://127.0.0.1:8000 即可。
HOST / PORT 可通过环境变量覆盖。

依赖安装:
    pip install -r requirements.txt

也支持 vendored 模式 —— 如果 backend/.pylibs 目录存在,会优先从中加载依赖,
无需 pip 安装 (可通过 `pip install --target .pylibs -r requirements.txt` 生成)。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent

# 支持 vendored 依赖 (可选)
_pylibs = ROOT / ".pylibs"
if _pylibs.exists():
    sys.path.insert(0, str(_pylibs))

import uvicorn  # noqa: E402

if __name__ == "__main__":
    host = os.getenv("HOST", "0.0.0.0")
    port = int(os.getenv("PORT", "8000"))
    uvicorn.run("app.main:app", host=host, port=port, reload=False)
