# ==============================================================================
#  AI Outfit Video Studio — Backend Docker Image
# ==============================================================================
#
#  构建:
#    docker build -t outfit-video-studio .
#
#  运行:
#    docker run -d --name ovs -p 8000:8000 \
#      -v $(pwd)/backend/.env:/app/backend/.env:ro \
#      -v $(pwd)/backend/storage:/app/backend/storage \
#      outfit-video-studio
#
#  然后打开 http://127.0.0.1:8000
#  (前端已打包在 standalone/ 里,后端会一起 serve)
# ==============================================================================

# ---------- 构建阶段 ----------
FROM python:3.11-slim AS builder

# PyAV / Pillow 需要系统库编译
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      ffmpeg               \
      libavcodec-dev       \
      libavformat-dev      \
      libavdevice-dev      \
      libswscale-dev       \
      libswresample-dev    \
      libavutil-dev        \
      libavfilter-dev      \
      pkg-config           \
      build-essential      \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /build

# 先装依赖层,利用 Docker 缓存 —— requirements.txt 没变就不重装
COPY backend/requirements.txt ./
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt \
 && find /install -name '*.pyc' -delete \
 && find /install -name '__pycache__' -type d -exec rm -rf {} + 2>/dev/null || true

# ---------- 运行阶段 ----------
FROM python:3.11-slim AS runtime

# 运行时只需要 ffmpeg (PyAV 调用它的 .so) + curl (健康检查)
RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg curl ca-certificates \
 && rm -rf /var/lib/apt/lists/* \
 && groupadd -r app && useradd -r -g app -d /app -s /sbin/nologin app

# 拷贝已装完的 pip 包
COPY --from=builder /install /usr/local

WORKDIR /app

# 后端源码 + 零构建前端 (两者相对位置必须保持: backend/../standalone)
COPY backend/ ./backend/
COPY standalone/ ./standalone/

# 创建持久化目录并修正权限
RUN mkdir -p /app/backend/storage \
 && chown -R app:app /app

USER app

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
  CMD curl -fsS http://127.0.0.1:8000/api/health || exit 1

WORKDIR /app/backend

# exec 形式:Ctrl+C / docker stop 能正确传递 SIGTERM
CMD ["python", "run.py"]
