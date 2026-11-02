"""End-to-end smoke test against a running backend.

Creates a project from the built-in template, preview-renders ONE shot through ComfyUI
(the 单镜生成 path), then exercises 删除镜头. Use it to verify the chain without waiting
for all 15 shots.

    python tools/smoke_render.py [shot_key]      # default v07 (28 frames, no back ref)
"""
from __future__ import annotations

import json
import sys
import time
import urllib.request

BASE = "http://127.0.0.1:8000"
TOKEN: str = ""
KEEP = sys.argv[1] if len(sys.argv) > 1 else "v07"


def call(path: str, method: str = "GET", body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method)
    if data:
        req.add_header("Content-Type", "application/json")
    if TOKEN:
        req.add_header("Authorization", "Bearer " + TOKEN)
    with urllib.request.urlopen(req, timeout=60) as r:
        raw = r.read().decode()
    return json.loads(raw) if raw else None


def main() -> int:
    global TOKEN
    TOKEN = call("/api/auth/guest", "POST")["access_token"]
    print("guest token ok")

    tpl = call("/api/templates")[0]
    project = call("/api/projects", "POST", {"template_id": tpl["id"], "title": "smoke test"})
    pid = project["id"]
    print(f"project #{pid} with {len(project['shots'])} shots")

    target = next(s for s in project["shots"] if s["key"] == KEEP)
    others = [s for s in project["shots"] if s["key"] != KEEP]

    # 单镜生成
    job = call(f"/api/projects/{pid}/shots/{target['id']}/render", "POST")
    print(f"shot job #{job['id']} queued for {KEEP} (shot_id={job['shot_id']})")

    t0 = time.time()
    while time.time() - t0 < 2400:
        job = call(f"/api/jobs/{job['id']}")
        print(f"  [{job['status']}] {job['progress']:3d}%  {job['message']}")
        if job["status"] in ("done", "failed"):
            break
        time.sleep(10)

    if job["status"] != "done":
        print("FAILED:", job.get("error"))
        return 1
    print(f"OK  shot preview: {BASE}{job['out_url']}")

    shot = call(f"/api/projects/{pid}")["shots"]
    hit = next(s for s in shot if s["key"] == KEEP)
    print(f"shot status={hit['status']} clip_url={hit['clip_url']}")

    # 删除镜头
    victim = others[0]
    call(f"/api/projects/{pid}/shots/{victim['id']}", "DELETE")
    after = call(f"/api/projects/{pid}")
    print(f"deleted {victim['key']}: {len(after['shots'])} shots left, "
          f"idx now {[s['idx'] for s in after['shots']]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
