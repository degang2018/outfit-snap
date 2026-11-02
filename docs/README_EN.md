## 👗 OutfitSnap · AI Storyboard Short Video Studio

> Upload character references + scenes + props + garments, arrange into shots: flying-in outfit flips, scene cuts, pose changes, push-in/pan camera moves… MiniMax H3 + ComfyUI renders and stitches everything into a complete product video.

---

[![Status](https://img.shields.io/badge/Status-Development-orange)](#)
[![Python](https://img.shields.io/badge/Python-3.11+-blue.svg)](#)
[![License](https://img.shields.io/badge/License-MIT-green.svg)](#)
[![Docker](https://img.shields.io/badge/Docker-Available-1.svg)](#)
[![Windows](https://img.shields.io/badge/Windows-OneClick-4.svg)](#)

https://github.com/user-attachments/assets/a495e83b-8bb4-4beb-8729-91e3cc6640d2

<p align="center" style="color:#8b949e;font-size:13px;margin-top:8px">
  👆 One garment = one shot — flies in, settles, pushes the previous one out — 4 seconds each
</p>

[中文](../README.md) | [English](#) | [Quick Start](#-quick-start) | [Features](#-core-features) | [Screenshots](#-screenshots) | [FAQ](#-faq) | [License](#-license)

---

## ✨ Core Features

| 🎬 Storyboard Editor | 🪄 Flying-In Outfit Flips | 👤 Identity Lock | 🔄 Scene / Pose / Camera Switch |
| --------------------- | ------------------------ | ----------------- | ------------------------------- |
| A project is a chain of shots — each renders independently, rerun any time | Each new garment = one shot — fly in, settle, swap out | Reference + avatar → same face across every shot in the video | Same character, different scene / pose / push-in / pan |

| 🧠 Zero-Setup Garment Recognition | 🔧 Fine-Grained Garment Controls | 🖼️ Asset Toolkit | 🔌 ComfyUI Ecosystem |
| -------------------------------- | ----------------------------- | ---------------- | --------------------- |
| Upload flat-lay → auto-name / style / button-state detection | Buttons open/closed, jacket half/full zip | Garment split / extract / eraser / shot manager | Direct ComfyUI API calls, fully customizable workflows |

| 💾 Persistent Asset Library | 🧩 Four Video Gen Modes | 🌍 Zero-Build Cross-Platform | 🐳 Docker One-Click |
| --------------------------- | ----------------------- | ---------------------------- | ------------------- |
| Characters / props / scenes / garments — upload once, reuse forever | Reference / first-frame / first+last-frame / text-only | Native on Windows / Linux / macOS | Containerized, GPU passthrough |

---

## 🚀 Quick Start

### Three Steps (Windows) ⭐

```
1) git clone <repo>
2) Double-click start.cmd  →  auto-creates .env, prompts for COMFY_OUTPUT_DIR
3) Edit .env, double-click again  →  browser opens http://127.0.0.1:8000
```

> Prerequisite: ComfyUI already running (default port 8188) with MiniMax H3 model in checkpoints.

### Option A · Docker Compose (no Python install)

```bash
git clone <repo> && cd outfit-snap
cp backend/.env.example backend/.env
# Edit .env — set COMFY_OUTPUT_DIR / COMFY_API
docker compose up -d --build
# Open http://127.0.0.1:8000
docker compose logs -f       # real-time logs
docker compose down          # stop
docker compose down -v       # ⚠️ Wipe volumes (assets + database gone)
```

> Docker → host ComfyUI: set `COMFY_API=http://host.docker.internal:8188` in `.env`. docker-compose.yml already has `extra_hosts` configured.

### Option B · Local Python (recommended for dev)

```bash
git clone <repo> && cd outfit-snap/backend
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt

cp .env.example .env
# Edit .env — at minimum set COMFY_OUTPUT_DIR
python run.py
# Browser opens http://127.0.0.1:8000
```

### Mac / Linux

```bash
git clone <repo> && cd outfit-snap
cp backend/.env.example backend/.env
# Edit .env
cd backend && pip install -r requirements.txt && python run.py
```

---

## 🧩 ComfyUI Model Checklist

Place these files in your ComfyUI `models/` directory (filenames overridable via `.env`):

| Type | Filename | Purpose |
|------|----------|---------|
| Checkpoint | `minimax_h3_fl2va_pruned_int8_convrot.safetensors` | MiniMax H3 video main model |
| VAE (video) | `minimax_h3_video_vae_fp16.safetensors` | H3-specific video VAE |
| VAE (audio) | `minimax_h3_audio_vae_fp32.safetensors` | Optional (omit if no audio) |
| LoRA | `minimax_h3_fl2v_turbo_8step_v1.0_comfyui_bf16.safetensors` | Turbo acceleration |
| CLIP | `qwen3vl_32b_heretic_minimax_h3_nvfp4.safetensors` | Multimodal text encoder |
| Upscale | `realesr-general-x4v3.pth` | Final-video super-resolution (default) |

---

## 📸 Screenshots

### Asset Toolkit (Sidebar)

One-stop panel for assets + utilities — garment split / extract / image eraser / shot manager / render settings / API config.

<img src="./images/assets-panel.png" width="280" alt="Asset sidebar">

### Garment Splitter

Upload a person or model photo — clothes/pants/shoes are automatically separated into individual asset items you can drag into projects.

<img src="./images/split-tool.png" width="480" alt="Garment splitter">

### Editor · Node-Style Flow

Shots in a project are connected like nodes — each with its own reference image, prompt, and duration. Render one, rerun one, or re-render all independently.

<img src="./images/editor-flow.png" alt="Editor node flow">

### Flip Effect

Each shot renders independently → auto color-match / push-in / flash-white transitions → stitched into one complete product-video short. The clip at the top of this page is a real-time render of a single flip shot (~4 seconds).

**Want to see finished videos in action?** Search Douyin for → **[小桃爆爆](https://www.douyin.com/search/%E5%B0%8F%E6%A1%83%E7%88%86%E7%88%86)** — dozens of full product clips made with this tool are posted there.

---

## 🎯 Complete Workflow

```
Step 1: Build your asset library (persistent, upload once, reuse forever)
  ↓ Character reference + avatar crop (identity lock)
  ↓ Garment flat-lays (LLM auto-recognizes name / style / button state)
  ↓ Scene images / props / wear-ref photos
  ↓
Step 2: Arrange into shots
  ↓ One garment per shot?        → flying-in outfit flip (garment_fly)
  ↓ Same character, new scene?   → scene-cut shot
  ↓ Same scene, new pose?        → pose-change shot
  ↓ Push-in / pan camera?        → camera option
  ↓ Anything else?               → 4 gen modes: reference / first-frame /
                                     first+last-frame / text-only
  ↓
Step 3: Render + stitch
  ↓ MiniMax H3 renders each shot independently
  ↓ Auto color-match / push-in / flash transitions / speed ramp
  ↓ One final MP4 (1080×1920)
  ↓ Download or fine-tune one shot and re-render
```

### Automation Comparison

| Manual Pipeline | OutfitSnap |
|------|------|
| Hand-write garment prompts (30-60 min / piece) | Auto garment recognition (1 min / set) |
| Manual generation + manual person cutout per piece | One-garment-per-shot auto gen + identity lock |
| Manual edit / color grade / stitch (2-4 hours / video) | Auto color-match + push-in + transition (1 min) |
| **Total: 4-6 hours** | **Total: 10-30 minutes** |

---

## 🏗️ Directory Layout

```
outfit-snap/
├── backend/                 # FastAPI backend (Python 3.11+)
│   ├── app/                 #   Business logic
│   │   ├── main.py          #   Entry + lifespan + static mount
│   │   ├── config.py        #   Settings driven by .env (no recompile on change)
│   │   ├── routers/         #   API routes (auth/assets/projects/jobs/split/...)
│   │   ├── pipeline.py      #   Render pipeline (calls ComfyUI API)
│   │   ├── worker.py        #   Background task queue (async render + watchdog)
│   │   ├── comfy_client.py  #   ComfyUI REST client
│   │   └── ...
│   ├── run.py               #   python run.py entrypoint
│   ├── requirements.txt
│   ├── .env.example         #   Copy to .env and fill in
│   ├── tests/               #   Unit tests (no ComfyUI required)
│   └── tools/               #   e2e / smoke scripts
│
├── standalone/              # Zero-build frontend (Vue 3 bundled, served by backend)
│   ├── index.html           #   Main UI (templates / projects / editor)
│   ├── auto.html            #   One-click flip standalone page
│   └── vendor/vue.global.prod.js
│
├── frontend/                # Frontend source (Vue 3 + Vite — for UI dev)
│   ├── src/views/
│   ├── package.json
│   └── vite.config.js
│
├── Dockerfile
├── docker-compose.yml
├── .dockerignore
├── .gitignore
├── start.cmd                # ⭐ Windows one-click launcher
├── README.md
└── docs/
    ├── images/              #   Screenshots referenced from README
    └── media/               #   Demo videos
```

---

## 🧠 Architecture

```
┌─────────────────────────────────────────────┐
│            Web UI                            │
│  standalone/ (zero-build)  +  frontend/ (dev)│
└─────────────────────────────────────────────┘
                    ↓ fetch
┌─────────────────────────────────────────────┐
│          FastAPI (backend/app)                │
│  /api/auth   /api/assets   /api/projects     │
│  /api/jobs   /api/split    /api/health       │
│  /media/*   (asset + output static files)    │
└─────────────────────────────────────────────┘
                    ↓
┌─────────────────────────────────────────────┐
│     pipeline.py + worker.py                  │
│  Background queue / async render / watchdog  │
└─────────────────────────────────────────────┘
                    ↓
┌─────────────────────────────────────────────┐
│        ComfyUI API (:8188)                   │
│  MiniMax H3 + Turbo LoRA + RealESRGAN        │
│  (separate process, GPU required)             │
└─────────────────────────────────────────────┘

Optional ↓
┌─────────────────────────────────────────────┐
│  Ollama / DeepSeek API                       │
│  Garment recognition (VLM) + script shot split (LLM) │
└─────────────────────────────────────────────┘
```

---

## 🛠️ Configuration (.env)

Copy from `backend/.env.example`. Key fields:

| Field | Default | Notes |
|-------|---------|-------|
| `COMFY_API` | `http://127.0.0.1:8188` | ComfyUI API URL (Docker → `host.docker.internal`) |
| `COMFY_OUTPUT_DIR` | ⚠️ **Required** | ComfyUI output directory (Windows: `C:/ComfyUI/ComfyUI/output`) |
| `JWT_SECRET` | `dev-secret-change-me` | **Must change in production** — changing invalidates all tokens |
| `DATABASE_URL` | Local SQLite | To switch to MySQL: `mysql+pymysql://user:pass@host/outfit_snap` |
| `GEN_WIDTH` / `GEN_HEIGHT` | `576×1024` | Generation resolution (must be divisible by 32) |
| `OUT_WIDTH` / `OUT_HEIGHT` | `1080×1920` | Final output resolution |
| `UPSCALE_VIDEO` | `1` | Final video uses ESRGAN super-resolution. Set `0` to skip (faster) |
| `LLM_API_STYLE` | `ollama` | `ollama` (CPU/local) or `openai` (cloud) |

Full field list with Chinese comments → [backend/.env.example](../backend/.env.example).

---

## 🛠️ Developer Guide

### Modifying the UI

```bash
cd frontend
npm install
npm run dev          # hot reload at http://127.0.0.1:5173
# When done, sync to standalone/:
#   npm run build  → copy dist/ into standalone/
#   or write a script to do this automatically
```

### Running Tests

```bash
cd backend
# Pure logic tests — no ComfyUI needed
python -m pytest tests/ -v
```

### Hot-Reloading Prompts

After backend starts, visit `http://127.0.0.1:8000/api/dev/reload-prompts` (localhost only). Changes to prompt constants in `quick_start.py` / `script_split.py` take effect immediately — no restart needed.

### Docker Image

```bash
docker build -t outfit-snap:dev .
docker run --rm -it -p 8000:8000 outfit-snap:dev
```

---

## 🔒 Security Notes

- `backend/.env` must never be committed (already in .gitignore)
- Changing `JWT_SECRET` invalidates all tokens — this is expected. Rotate it on every production deploy.
- Frontend and backend serve from the same origin by default — no CORS config needed
- `standalone/` contains no secrets — all credentials are injected through backend API
- `/api/dev/*` endpoints only respond from 127.0.0.1 — public access gets 403

---

## ❓ FAQ

**Q: Docker can't reach ComfyUI on the host?**
A: In `.env`, set `COMFY_API=http://host.docker.internal:8188`. docker-compose.yml already sets `extra_hosts`.

**Q: 16GB GPU runs out of VRAM at 1080×1920?**
A: Set `GEN_WIDTH=448 GEN_HEIGHT=800` (or lower) in `.env`. Must be divisible by 32; the script auto-rounds.

**Q: PyAV compilation error?**
A: Debian/Ubuntu: `apt install ffmpeg libavcodec-dev libavformat-dev pkg-config build-essential`. Windows: install official ffmpeg and add to PATH. Dockerfile handles this for Docker users.

**Q: SQLite lock file errors?**
A: Ensure only one backend instance writes to the same db file. Multiple instances → switch to MySQL.

**Q: Migrate old user data?**
A: Delete `backend/app.db` and `backend/storage/` assets and start fresh. Or switch to MySQL by changing `DATABASE_URL`.

---

## 📋 Tech Stack

| Layer | Technology |
|-------|------------|
| Backend | FastAPI · SQLAlchemy · PyJWT · uvicorn |
| Database | SQLite (default) / MySQL (optional) |
| Frontend | Vue 3 · vue-router · native fetch |
| Video | PyAV · ESRGAN (ComfyUI node) |
| AI Generation | ComfyUI · MiniMax H3 · Turbo LoRA |
| Optional LLM | Ollama / DeepSeek / OpenAI-compatible |

---

## 📜 License

MIT License — use, modify, fork freely.

```
Copyright (c) 2026 OutfitSnap Contributors

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.
```

© 2026 OutfitSnap Contributors.
