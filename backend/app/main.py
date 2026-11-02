"""FastAPI entry point."""
from __future__ import annotations

import importlib
import logging
import traceback
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles

from . import worker
from .config import BASE_DIR, STORAGE_DIR, settings
from .db import Base, engine
from .migrate import check_integrity, ensure_schema
from .routers import assets, auth, erase, extract, imgedit, jobs, playbooks, projects, shots, split, sysconfig, templates
from .seed import run as seed_run

# 未捕获异常落盘:后端默认把 traceback 打在启动它的控制台里,窗口一关就没了。
# 写一份到 server_error.log,500 之后可以直接看是哪个接口、哪一行。
_error_logger = logging.getLogger("app.errors")
_error_logger.setLevel(logging.ERROR)
if not _error_logger.handlers:
    _h = logging.FileHandler(BASE_DIR / "server_error.log", encoding="utf-8")
    _h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    _error_logger.addHandler(_h)
    _error_logger.propagate = False

# zero-build UI (Vue 3 vendored locally) - works without Node/npm installed.
# The Vite SPA in web/frontend is the same feature set for a real dev workflow.
STANDALONE_DIR = BASE_DIR.parent / "standalone"


@asynccontextmanager
async def lifespan(app: FastAPI):
    Base.metadata.create_all(bind=engine)
    ensure_schema(engine)          # additive migration - never delete the DB to change schema
    check_integrity(engine)        # 表丢主键这类隐蔽问题,启动时就报出来
    seed_run()
    print(f"[app] database : {settings.database_url}", flush=True)
    print(f"[app] storage  : {STORAGE_DIR}", flush=True)
    reaped = worker.reap_orphans()   # 上次中断的任务(服务重启)标记为失败,别让它们永远卡在「生成中」
    if reaped:
        print(f"[app] 已清理 {reaped} 个中断的渲染任务", flush=True)
    worker.start()
    yield
    worker.stop()


app = FastAPI(title=settings.app_name, version="0.1.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.exception_handler(Exception)
async def log_unhandled_exception(request: Request, exc: Exception):
    """未捕获异常:traceback 写到 server_error.log,再返回原来的 500 文本。"""
    _error_logger.error(
        "UNHANDLED %s %s\n%s", request.method, request.url, traceback.format_exc()
    )
    return PlainTextResponse("Internal Server Error", status_code=500)


@app.middleware("http")
async def no_cache_ui(request: Request, call_next):
    """零构建界面就是一个 index.html,浏览器缓存它会让改动看不见 —— 页面永远给最新版。
    素材图同理:换图后路径不变,必须让浏览器每次回源校验,否则看到的还是旧图。"""
    response = await call_next(request)
    ct = response.headers.get("content-type", "")
    is_thumb = request.url.path.startswith("/media/thumbs/")
    if ct.startswith("text/html") or (request.url.path.startswith("/media/") and not is_thumb):
        response.headers["Cache-Control"] = "no-cache, must-revalidate"
    return response


@app.get("/media/thumbs/{name}")
def media_thumb(name: str):
    """片段首帧缩略图:第一次请求时按需生成,之后直接返回缓存。

    这条路必须注册在 /media 静态挂载之前,否则会被 StaticFiles 抢走。
    """
    from .thumb import ensure_thumb

    path = ensure_thumb(name)
    if path is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "thumbnail not found")
    return FileResponse(str(path), media_type="image/jpeg",
                        headers={"Cache-Control": "public, max-age=86400"})


app.mount("/media", StaticFiles(directory=str(STORAGE_DIR)), name="media")

for r in (auth.router, assets.router, templates.router, playbooks.router, shots.router, projects.router, jobs.router, split.router, erase.router, extract.router, imgedit.router, sysconfig.router):
    app.include_router(r, prefix=settings.api_prefix)


@app.get("/api/health")
def health() -> dict:
    return {"ok": True, "app": settings.app_name}


# 开发用:改完 quick_start / script_split 里的提示词常量后,不用重启后端,
# 打开这个地址(或 POST)就能让新提示词生效。仅允许本机调用。
@app.get("/api/dev/reload-prompts")
@app.post("/api/dev/reload-prompts")
def reload_prompts(request: Request) -> dict:
    host = request.client.host if request.client else ""
    if host not in ("127.0.0.1", "::1"):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "仅限本机调用")
    from . import quick_start, script_split

    done: list[str] = []
    for mod in (quick_start, script_split):
        importlib.reload(mod)
        done.append(mod.__name__)

    # script_split 的函数被 projects 路由用 `from ... import` 绑进了它的命名空间,
    # 只 reload 模块不会更新那批旧引用(改函数体时尤其明显),所以重新绑一次。
    from .routers import projects as projects_router

    for name in ("KINDS", "analyze_script", "build_prompt", "optimize_prompt", "validate_optimized"):
        setattr(projects_router, name, getattr(script_split, name))

    print(f"[dev] 已热重载提示词模块: {', '.join(done)}", flush=True)
    return {"ok": True, "reloaded": done, "styles": [s["t"] for s in quick_start.STYLES]}


# mounted last so /api/* and /media/* keep priority
if STANDALONE_DIR.exists():
    app.mount("/", StaticFiles(directory=str(STANDALONE_DIR), html=True), name="ui")
