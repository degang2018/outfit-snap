"""端到端测试:剧本 -> 分析 -> 指定素材 -> 整片生成 -> 校验成片。

用小程序跑完整链路(默认 2 个镜头),把成片的关键帧拼一张图方便肉眼确认。

    python tools/e2e_render.py [镜头数] [总秒数]
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.request
import uuid

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".pylibs"))

BASE = "http://127.0.0.1:8000"
BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOKEN = ""

SHOT_COUNT = int(sys.argv[1]) if len(sys.argv) > 1 else 2
TOTAL_SECONDS = float(sys.argv[2]) if len(sys.argv) > 2 else 8.0
SCRIPT = (
    "清晨,女生林小满推开窗,阳光洒进来,她伸了个懒腰。"
    "她走到厨房,用白色马克杯冲了一杯咖啡,热气缓缓升起,她低头笑了一下。"
)
REF_IMAGE = r"h:\comfyui\video\人物\Gray Loungewear Lookbook Collage.png"


def call(path, method="GET", body=None, timeout=600):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method)
    if data:
        req.add_header("Content-Type", "application/json")
    if TOKEN:
        req.add_header("Authorization", "Bearer " + TOKEN)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read().decode()
    return json.loads(raw) if raw else None


def upload(path: str, category: str) -> dict:
    boundary = "----" + uuid.uuid4().hex
    name = os.path.basename(path)
    with open(path, "rb") as fh:
        blob = fh.read()
    head = (
        f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{name}"\r\n'
        f"Content-Type: image/png\r\n\r\n"
    ).encode()
    tail = (
        f'\r\n--{boundary}\r\nContent-Disposition: form-data; name="category"\r\n\r\n{category}'
        f"\r\n--{boundary}--\r\n"
    ).encode()
    req = urllib.request.Request(BASE + "/api/assets", data=head + blob + tail, method="POST")
    req.add_header("Content-Type", f"multipart/form-data; boundary={boundary}")
    req.add_header("Authorization", "Bearer " + TOKEN)
    with urllib.request.urlopen(req, timeout=180) as r:
        return json.loads(r.read().decode())


def main() -> int:
    global TOKEN
    t_start = time.time()

    print("=" * 62)
    print("0) 游客登录 + 上传素材")
    TOKEN = call("/api/auth/guest", "POST")["access_token"]
    asset = upload(REF_IMAGE, "character")
    print(f"   asset #{asset['id']} {asset['filename']} {asset['width']}x{asset['height']}")

    print("\n1) 分析剧本(LLM 拆镜头 + 识别实体)")
    plan = call("/api/projects/analyze-script", "POST",
                {"script": SCRIPT, "shot_count": SHOT_COUNT, "total_seconds": TOTAL_SECONDS})
    print(f"   engine={plan['engine']} model={plan['engine_detail']}")
    for kind, label in (("characters", "人物"), ("props", "道具"), ("scenes", "场景")):
        items = plan.get(kind) or []
        print(f"   {label}: " + ("、".join(e["name"] for e in items) if items else "无"))
    for i, s in enumerate(plan["shots"], 1):
        print(f"   镜头{i}《{s['title']}》{s['seconds']}s  {s['action'][:60]}")

    print("\n2) 指定素材并创建项目")
    name = plan["characters"][0]["name"] if plan.get("characters") else ""
    assignments = {f"characters:{name}": [f"asset:{asset['id']}"]} if name else {}
    res = call("/api/projects/from-script", "POST", {
        "title": "端到端测试", "total_seconds": TOTAL_SECONDS, "shot_count": SHOT_COUNT,
        "mode": "ref", "plan": plan, "assignments": assignments,
    })
    project = res["project"]
    pid = project["id"]
    print(f"   项目 #{pid}  {len(project['shots'])} 镜头  {project['total_frames']} 帧 "
          f"{project['duration_seconds']}s")
    for s in project["shots"]:
        print(f"   {s['key']} {s['duration_frames']}f refs={s['refs']} 《{s['title']}》")

    print("\n3) 整片生成(逐镜头提交 ComfyUI)")
    job = call(f"/api/projects/{pid}/render", "POST")
    print(f"   job #{job['id']} {job['status']}")

    last = ""
    while time.time() - t_start < 3600:
        job = call(f"/api/jobs/{job['id']}")
        line = f"   [{job['status']}] {job['progress']:3d}%  {job['message']}"
        if line != last:
            print(line, flush=True)
            last = line
        if job["status"] in ("done", "failed", "canceled"):
            break
        time.sleep(10)

    if job["status"] != "done":
        print("\n生成失败:", job.get("error"))
        return 1

    print(f"\n4) 成片 {job['out_url']}")
    out_path = os.path.join(BACKEND, "storage", job["out_rel"] if "out_rel" in job
                            else job["out_url"].replace("/media/", "").replace("/", os.sep))
    import av
    from PIL import Image

    container = av.open(out_path)
    stream = container.streams.video[0]
    frames = [f.to_image().convert("RGB") for f in container.decode(video=0)]
    container.close()
    print(f"   分辨率 {stream.width}x{stream.height}  帧数 {len(frames)}  "
          f"时长 {len(frames)/24:.2f}s  文件 {os.path.getsize(out_path)/1024/1024:.1f} MB")

    picks = [0, len(frames) // 3, len(frames) * 2 // 3, len(frames) - 1]
    th = 360
    w = int(th * frames[0].width / frames[0].height)
    strip = Image.new("RGB", (w * len(picks), th), "white")
    for x, p in enumerate(picks):
        strip.paste(frames[p].resize((w, th), Image.LANCZOS), (x * w, 0))
    strip_path = os.path.join(BACKEND, "storage", "e2e_frames.png")
    strip.save(strip_path)
    print(f"   关键帧拼图 {strip_path}")
    print(f"\n总耗时 {(time.time()-t_start)/60:.1f} 分钟")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
