## 👗 OutfitSnap · AI 分镜短视频工坊

> 上传人物参考图 + 场景 + 道具 + 衣服，按分镜编排：衣服飞入换装、场景切换、人物动作变化、推拉摇移机位…… MiniMax H3 + ComfyUI 一键渲染拼接成一条带货视频。

---

[![状态](https://img.shields.io/badge/状态-开发中-orange)](#)
[![Python](https://img.shields.io/badge/Python-3.11+-blue.svg)](#)
[![License](https://img.shields.io/badge/License-MIT-green.svg)](#)
[![Docker](https://img.shields.io/badge/Docker-可用-1.svg)](#)
[![Windows](https://img.shields.io/badge/Windows-一键启动-4.svg)](#)

https://github.com/user-attachments/assets/a495e83b-8bb4-4beb-8729-91e3cc6640d2

<p align="center" style="color:#8b949e;font-size:13px;margin-top:8px">
  👆 一件衣服 = 一镜，衣服从画外飞来落到身上，原来那件被顶掉 — 4 秒一条分镜
</p>

[中文](#) | [English](./docs/README_EN.md) | [快速上手](#-快速上手) | [核心特性](#-核心特性) | [界面截图](#-界面截图) | [FAQ](#-常见问题) | [LICENSE](#-开源协议)

---

## ✨ 核心特性

| 🎬 分镜式编排                 | 🪄 衣服飞入换装                     | 👤 人物身份锁死                  | 🔄 场景/动作/机位切换          |
| -------------------------- | ------------------------------- | --------------------------- | ----------------------- |
| 项目 = 一串分镜，每镜独立生成、可单独重跑 | 每件新衣服占一镜，悬浮飞入/落定/换下一套 | 参考图 + 头像 → 整片所有分镜同一张脸 | 同人物，换场景 / 换动作 / 推拉摇移 |

| 🧠 读图认件开箱即用              | 🔧 服装属性精细控制 | 🖼️ 资源管理工具箱              | 🔌 ComfyUI 生态无缝集成        |
| ----------------------------- | -------------- | --------------------------- | -------------------------- |
| 衣服平铺图上传即认件名、款式、开合属性          | 扣子扣上/敞开、外套半开/全扣      | 衣服分割 / 提取衣服 / 图片橡皮 / 分镜管理 | 直接调 ComfyUI API，工作流可自定义 |

| 💾 可持久化素材库               | 🧩 多模式生视频            | 🌍 跨平台零构建                | 🐳 Docker 一键部署 |
| -------------------------- | ------------------- | -------------------------- | ----------------- |
| 人物/道具/场景/衣服图库，下次复用免重传 | 参考图 / 首帧 / 首尾帧 / 纯文本 | Windows / Linux / macOS 原生可用 | 容器化部署，GPU 直通   |

---

## 🚀 快速上手

### 三步就能跑（Windows 用户）⭐

```
1) git clone <repo>
2) 双击 start.cmd  →  自动生成 .env,提示填 COMFY_OUTPUT_DIR
3) 改完 .env 再双击一次  →  浏览器自动打开 http://127.0.0.1:8000
```

> 前置:ComfyUI 已经在跑 (默认端口 8188),MiniMax H3 模型放在 checkpoints 下。

### 路线 A · Docker Compose（不想装 Python）

```bash
git clone <repo> && cd outfit-snap
cp backend/.env.example backend/.env
# 编辑 .env,改 COMFY_OUTPUT_DIR / COMFY_API
docker compose up -d --build
# 打开 http://127.0.0.1:8000
docker compose logs -f       # 看实时日志
docker compose down          # 停止
docker compose down -v      # ⚠️ 删数据卷(素材/数据库全清)
```

> Docker 里访问宿主机 ComfyUI → `.env` 里 `COMFY_API=http://host.docker.internal:8188`,docker-compose.yml 已配好。

### 路线 B · 本地 Python（推荐开发者）

```bash
git clone <repo> && cd outfit-snap/backend
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt

cp .env.example .env
# 编辑 .env,至少填 COMFY_OUTPUT_DIR
python run.py
# 浏览器打开 http://127.0.0.1:8000
```

### Mac / Linux

```bash
git clone <repo> && cd outfit-snap
cp backend/.env.example backend/.env
# 编辑 .env
cd backend && pip install -r requirements.txt && python run.py
```

---

## 🧩 ComfyUI 模型清单

在 ComfyUI 的 `models/` 目录下放好这些文件（文件名可在 `.env` 里覆盖）：

| 类型 | 文件名 | 说明 |
|------|--------|------|
| Checkpoint | `minimax_h3_fl2va_pruned_int8_convrot.safetensors` | MiniMax H3 视频主模型 |
| VAE (视频) | `minimax_h3_video_vae_fp16.safetensors` | H3 专用 VAE |
| VAE (音频) | `minimax_h3_audio_vae_fp32.safetensors` | 可选（不开音频可不放） |
| LoRA | `minimax_h3_fl2v_turbo_8step_v1.0_comfyui_bf16.safetensors` | Turbo 加速 LoRA |
| CLIP | `qwen3vl_32b_heretic_minimax_h3_nvfp4.safetensors` | 多模态文本编码器 |
| Upscale | `realesr-general-x4v3.pth` | 成片超分（默认） |

---

## 📸 界面截图

### 资源管理工具箱

侧边栏一站式管理素材和工具 — 衣服分割 / 提取衣服 / 图片橡皮 / 分镜管理 / 成片设置 / 接口配置。

<img src="./docs/images/assets-panel.png" width="280" alt="资源管理侧边栏">

### 衣服分割

上传一张人物图或模特实穿图，自动把衣服/裤子/鞋子拆成独立素材，直接拖进项目就能用。

<img src="./docs/images/split-tool.png" width="480" alt="衣服分割">

### 编辑器 · 节点式编排

项目里所有分镜像节点一样连起来，每个节点有自己的参考图、提示词、时长。点哪个渲染哪个，重跑不影响已经好的镜。

<img src="./docs/images/editor-flow.png" alt="编辑器节点式编排">

### 换装效果

每个分镜独立生成 → 自动调色 / 推镜 / 闪白过渡 → 拼成一条完整的带货短视频。上面首屏那个视频就是一条 **换装分镜** 的实时渲染结果（约 4 秒）。

**想看更多实际成片？去抖音搜 →** 👉 **[小桃爆爆](https://www.douyin.com/search/%E5%B0%8F%E6%A1%83%E7%88%86%E7%88%86)** — 那里发了几十条用本工具生成的完整带货视频，每条都是好几件衣服依次换上去的成片。

---

## 🎯 完整创作流程

```
第 1 步:攒素材 (素材库可持久化,下次复用免重传)
  ↓ 人物参考图 + 头像 (身份锁死)
  ↓ 衣服平铺图 (LLM 自动读图认件名/款式/开合属性)
  ↓ 场景图 / 道具图 / 实拍参考图
  ↓
第 2 步:编排分镜
  ↓ 每件衣服占一镜? → 衣服飞入换装 (garment_fly)
  ↓ 同人物换场景? → 场景切换镜
  ↓ 同场景换动作? → 动作变化镜
  ↓ 推拉摇移机位? → camera 选项
  ↓ 其他素材自由组合 → 参考图/首帧/首尾帧/纯文本 4 种模式
  ↓
第 3 步:渲染 + 拼接
  ↓ MiniMax H3 独立生成每个分镜
  ↓ 自动调色 / 推镜 / 闪白过渡 / 变速
  ↓ 拼成一条完整 MP4 (1080×1920)
  ↓ 下载 或 再微调某一镜
```

### 自动化程度对比

| 传统方式 | 衣秒成片 |
|------|------|
| 手写服装提示词（30-60 分钟/件） | 自动读图认件（1 分钟/套） |
| 逐件手动生成 + 手动抠人物 | 一件一镜自动生成 + 人物锁死 |
| 手动剪辑拼接（2-4 小时/条） | 自动调色+推镜+闪白过渡拼接（1 分钟） |
| **总耗时：4-6 小时** | **总耗时：10-30 分钟** |

---

## 🏗️ 目录结构

```
outfit-snap/
├── backend/                 # FastAPI 后端 (Python 3.11+)
│   ├── app/                 #   业务代码
│   │   ├── main.py          #   入口 + Lifespan + 静态文件挂载
│   │   ├── config.py        #   环境变量驱动的 Settings (.env 改了不用重编译)
│   │   ├── routers/         #   API 路由 (auth/assets/projects/jobs/split/...)
│   │   ├── pipeline.py     #   渲染主流程 (调 ComfyUI API)
│   │   ├── worker.py        #   后台任务 (异步渲染 + 看门狗)
│   │   ├── comfy_client.py  #   ComfyUI REST 客户端
│   │   └── ...
│   ├── run.py               #   python run.py 启动入口
│   ├── requirements.txt
│   ├── .env.example         #   复制成 .env 填配置
│   ├── tests/               #   单元测试 (不需要 ComfyUI)
│   └── tools/               #   e2e / smoke 脚本
│
├── standalone/              # 零构建前端 (Vue 3 打包产物,后端直接 serve)
│   ├── index.html           #   主界面 (模板 / 项目列表 / 编辑器)
│   ├── auto.html            #   一键换装独立页
│   └── vendor/vue.global.prod.js
│
├── frontend/                # 前端源码 (Vue 3 + Vite,想改 UI 用)
│   ├── src/views/
│   ├── package.json
│   └── vite.config.js
│
├── Dockerfile
├── docker-compose.yml
├── .dockerignore
├── .gitignore
├── start.cmd                # ⭐ Windows 一键启动 (双击即可)
├── README.md
└── docs/
    └── images/              #   README 引用的截图
```

---

## 🧠 核心架构

```
┌─────────────────────────────────────────────┐
│            Web UI (前端)                      │
│  standalone/ 零构建版  +  frontend/ 开发版    │
└─────────────────────────────────────────────┘
                    ↓ fetch
┌─────────────────────────────────────────────┐
│          FastAPI (backend/app)                │
│  /api/auth   /api/assets   /api/projects     │
│  /api/jobs   /api/split    /api/health       │
│  /media/*   (素材/成片静态文件)               │
└─────────────────────────────────────────────┘
                    ↓
┌─────────────────────────────────────────────┐
│     pipeline.py + worker.py                  │
│  后台任务队列 / 异步渲染 / 看门狗              │
└─────────────────────────────────────────────┘
                    ↓
┌─────────────────────────────────────────────┐
│        ComfyUI API (:8188)                   │
│  MiniMax H3 + Turbo LoRA + RealESRGAN        │
│  (独立进程, 需 GPU)                           │
└─────────────────────────────────────────────┘

可选 ↓
┌─────────────────────────────────────────────┐
│  Ollama / DeepSeek API                       │
│  读图认件 (VLM) + 剧本切镜头 (LLM)            │
└─────────────────────────────────────────────┘
```

---

## 🛠️ 配置详解 (.env)

复制自 `backend/.env.example`，关键字段：

| 字段 | 默认值 | 说明 |
|------|--------|------|
| `COMFY_API` | `http://127.0.0.1:8188` | ComfyUI API 地址（Docker 里改成 `host.docker.internal`） |
| `COMFY_OUTPUT_DIR` | ⚠️ **必须填** | ComfyUI 的 output 目录（Windows: `C:/ComfyUI/ComfyUI/output`） |
| `JWT_SECRET` | `dev-secret-change-me` | **生产必须换**，改了旧 Token 全失效 |
| `DATABASE_URL` | SQLite 本地文件 | 想切 MySQL：`mysql+pymysql://user:pass@host/outfit_video` |
| `GEN_WIDTH` / `GEN_HEIGHT` | `576×1024` | 生成分辨率（必须能被 32 整除） |
| `OUT_WIDTH` / `OUT_HEIGHT` | `1080×1920` | 成片分辨率 |
| `UPSCALE_VIDEO` | `1` | 成片是否用 ESRGAN 超分（关掉写 `0`，省时间） |
| `LLM_API_STYLE` | `ollama` | `ollama` (CPU) 或 `openai` (云端) |

完整字段见 [backend/.env.example](backend/.env.example)（中文注释）。

---

## 🛠️ 开发者指南

### 改前端 UI

```bash
cd frontend
npm install
npm run dev          # 热更新,浏览器打开 http://127.0.0.1:5173
# 改完想同步到 standalone/:
#   npm run build  → dist/ 产物拷到 standalone/
#   或写个脚本自动做
```

### 跑测试

```bash
cd backend
# 不需要 ComfyUI 就能跑的纯逻辑测试
python -m pytest tests/ -v
```

### 热重载提示词

后端启动后访问 `http://127.0.0.1:8000/api/dev/reload-prompts`（仅限本机调用），改完 `quick_start.py` / `script_split.py` 里的提示词常量后，直接点一下就生效，不用重启。

### Docker 镜像

```bash
docker build -t outfit-snap:dev .
docker run --rm -it -p 8000:8000 outfit-snap:dev
```

---

## 🔒 安全说明

- `backend/.env` **绝不能**提交到 Git（已在 .gitignore 里）
- 改了 `JWT_SECRET` 后旧 Token 会全部失效 — 正常，生产环境每次部署都换一次
- 前端和后端默认在同一 origin，不需要额外 CORS 配置
- `standalone/` 里没有任何密钥，全部通过后端 API 注入
- `/api/dev/*` 仅限 127.0.0.1 调用，公网访问自动 403

---

## ❓ 常见问题

**Q: Docker 里访问不了 ComfyUI？**
A: `.env` 里 `COMFY_API` 写 `http://host.docker.internal:8188`，docker-compose.yml 已配了 `extra_hosts`。

**Q: 16GB 显卡跑 1080×1920 爆显存？**
A: `.env` 里把 `GEN_WIDTH=448 GEN_HEIGHT=800`（或更低）。必须能被 32 整除，脚本会自动算。

**Q: PyAV 编译报错？**
A: Debian/Ubuntu：`apt install ffmpeg libavcodec-dev libavformat-dev pkg-config build-essential`。Windows：装官方 ffmpeg 加 PATH。Docker 版的 Dockerfile 里已处理。

**Q: SQLite 锁文件报错？**
A: 确保只有一个后端实例在写同一个 db 文件；多实例请切 MySQL。

**Q: 旧用户数据要迁移？**
A: 删掉 `backend/app.db` 和 `backend/storage/` 里的素材重新来；或者直接用 MySQL 把 DATABASE_URL 切过去。

---

## 📋 技术栈

| 层 | 技术 |
|----|------|
| 后端 | FastAPI · SQLAlchemy · PyJWT · uvicorn |
| 数据库 | SQLite（默认）/ MySQL（可选） |
| 前端 | Vue 3 · vue-router · 原生 fetch |
| 视频 | PyAV · ESRGAN（ComfyUI 插件） |
| AI 生成 | ComfyUI · MiniMax H3 · Turbo LoRA |
| 可选 LLM | Ollama / DeepSeek / OpenAI 兼容接口 |

---

## 📜 开源协议

MIT License — 随便用、随便改、随便 fork。

```
Copyright (c) 2026 Outfit Video Studio Contributors

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.
```

© 2026 Outfit Video Studio Contributors.
