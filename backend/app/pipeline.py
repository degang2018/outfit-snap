"""Render pipeline: shots -> ComfyUI -> trimmed clips -> one upscaled 1080x1920 film."""
from __future__ import annotations

import math
import os
import random
import re
import time
from datetime import datetime, timezone
from pathlib import Path

import av
import numpy as np
from PIL import Image, ImageChops, ImageDraw, ImageEnhance, ImageFilter
from sqlalchemy.orm import Session

from . import comfy_client as comfy
from . import upscale
from .camera import apply as apply_camera
from .comfy_workflow import build_detail_workflow, build_workflow
from .config import DEFAULT_SEED, CLIP_DIR, RENDER_DIR, STORAGE_DIR, derive_gen_size, settings
from .db import SessionLocal
from .models import (
    Asset,
    CAT_CHARACTER,
    MODE_FIRST,
    MODE_FIRST_LAST,
    MODE_REF,
    Project,
    ProjectShot,
    RenderJob,
    ST_CANCELED,
    ST_DONE,
    ST_FAILED,
    ST_QUEUED,
    ST_RUNNING,
)
from .quick_start import is_full_body_shot
from .script_split import insert_into_person, is_object_shot
from .thumb import write_thumb


class Canceled(Exception):
    """Raised inside the pipeline when the user pressed 终止生成."""


# 每镜最多留这么多张抽卡候选(超出的最老的自动删掉,免得磁盘越堆越多)
_MAX_CANDIDATES = 12


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _job_row(db: Session, job_id: int) -> RenderJob | None:
    """Re-read the job so a 终止生成 committed by the API is visible here."""
    db.rollback()
    return db.get(RenderJob, job_id)


def _is_canceled(db: Session, job_id: int) -> bool:
    job = _job_row(db, job_id)
    return job is not None and job.status == ST_CANCELED


# 悬浮变装:所有参考图先"缩小 + 居中留白"再喂给模型。
_SCALED_DIR = STORAGE_DIR / "refs_scaled"

# 换装镜里「正要飞进来的那一件」的缩放档。**已恢复成道具档(0.33)**:
# 之前试过压到 0.22 没有改善 —— 巨布的真正原因是飞行说明被塞进了 defs(见 piece_prompt),
# 不是参考图大小。留着这个常量只是别再走回头路。
_FLY_REF_SCALE = 0.33


def _edge_fill(im: Image.Image) -> tuple[int, int, int]:
    """这张图四边的底色(取中位数,避开四角可能有的杂色)。

    以前垫边用的是**写死的米色 (247,246,244)**:白底商品图是 (254,255,255),差一点就在
    图四周压出一道看得见的矩形边 —— 模型会把这圈"衬卡片"照抄进视频,成片里就是"画中画":
    中间一块画面、外面一圈纯色带(实测四边 spread=0)。用户报"里面有个框、衣服不是从最边
    出来的"就是这个。改成"用这张图自己的底色"后,白底图填白、灰底图填灰,边界看不出来。
    """
    w, h = im.size
    sx, sy = max(1, w // 40), max(1, h // 40)
    pts = ([ (x, 0) for x in range(0, w, sx)] + [(x, h - 1) for x in range(0, w, sx)]
           + [(0, y) for y in range(0, h, sy)] + [(w - 1, y) for y in range(0, h, sy)])
    vals = [im.getpixel(p) for p in pts]

    def med(i: int) -> int:
        s = sorted(v[i] for v in vals)
        return s[len(s) // 2]

    return (med(0), med(1), med(2))


def _scaled_refs(paths: list[str], scales: list[float] | None = None) -> list[str]:
    """悬浮变装:把参考图缩小留白再喂给模型。

    参考图都是"内容占满整张图"的图片 —— 人物图是紧裁全身照、服装图是特写裁片。
    MiniMax 会连构图和比例一起抄走:实测人物占 93% 画面高(参考片只有 67%),
    飞过来的衣服更是被画成占满整屏、还不会贴到人身上。写在提示词里也压不住。
    所以统一把图内容缩放、居中贴在同样大小的底上。

    **缩放倍数可以逐张不同**(scales):人物图维持 float_ref_scale,道具/服装图用更小的
    float_ref_scale_prop —— 商品图是"衣服摊开占满整幅",占屏比例会被一起抄走(实测 0.5 时
    飞进来的衣服是正确大小的 1.5~3.7 倍),但人物图缩了会影响脸和身材,所以只缩道具那几张。

    垫边的颜色**必须用这张图自己的底色**(见 _edge_fill):写死颜色会在白底图上压出
    一圈矩形,模型照抄成"画中画"内框。
    """
    def _one(src: str, scale: float) -> str:
        if not src or not os.path.exists(src) or not (0.1 < scale < 1.0):
            return src
        _SCALED_DIR.mkdir(parents=True, exist_ok=True)
        # 缓存键必须带上源文件的 mtime —— 只按文件名缓存的话,素材图原地覆盖后(asset 换图但
        # 文件名不变)会永远命中旧缓存,新衣服怎么都出不来(踩过,查了很久)。
        # v2 = 垫边色改成"按图取底色"那一版;改名是为了让旧的(米色垫边)缓存全部失效。
        stamp = os.stat(src).st_mtime_ns
        dst = _SCALED_DIR / f"{os.path.splitext(os.path.basename(src))[0]}_s{scale:g}v2_{stamp}.png"
        if not dst.exists():
            im = Image.open(src).convert("RGB")
            small = im.resize((max(1, round(im.width * scale)), max(1, round(im.height * scale))),
                              Image.LANCZOS)
            canvas = Image.new("RGB", im.size, _edge_fill(im))
            canvas.paste(small, ((im.width - small.width) // 2, (im.height - small.height) // 2))
            canvas.save(dst)
        return str(dst)

    if scales is None:
        scales = [settings.float_ref_scale] * len(paths)
    return [_one(src, sc) for src, sc in zip(paths, scales)]


# 小件配饰:它们的商品图在镜头里会被模型当成"场景物件"再画一遍,和鞋一样压到很小的比例。
_SMALL_ACC_HINTS = (
    "墨镜", "太阳镜", "眼镜", "帽", "项链", "锁骨链", "耳环", "耳钉",
    "围巾", "丝巾", "腰带", "皮带", "手表", "手链", "发夹", "发卡", "发箍",
)


def _ref_scales(db: Session, shot) -> dict[str, float]:
    """这一镜里**每张参考图各自**该缩多少(按绝对路径查)。
    人物图 = float_ref_scale(脸和身材要紧,不动);道具 / 服装 / 场景 = float_ref_scale_prop
    (商品图是摊开占满整幅的特写裁片,模型会照抄它的占屏比例 → 飞进来的衣服巨大)。
    """
    # 换装镜里「正要飞进来的那一件」:参考图再压小一档 —— 文案里的尺寸锚模型经常当耳旁风
    # (2026-10-19 用户反复报「衣服飘的时候怎么这么大」),参考图的占屏比例是更硬的杠杆。
    _fly_name = ""
    if (shot.options or {}).get("anchor_last") and not (shot.options or {}).get("state_only"):
        _m = re.search(r"在「([^」]+)」碰到", shot.prompt or "")
        _fly_name = _m.group(1).strip() if _m else ""
    out: dict[str, float] = {}
    for ref in (shot.refs or []):
        if not isinstance(ref, str):
            continue
        cat, path = "", ""
        asset = None
        if ref.startswith("asset:"):
            try:
                asset = db.get(Asset, int(ref.split(":", 1)[1]))
            except (ValueError, TypeError):
                continue
            if asset:
                cat, path = asset.category, str(STORAGE_DIR / asset.rel_path)
        elif ref.startswith("file:"):
            path = ref[5:]
        if not path:
            continue
        if cat == CAT_CHARACTER:
            scale = settings.float_ref_scale
        elif asset is not None and (
                (asset.ai_kind or "").strip() == "脚"
                or any(k in (asset.ai_name or "") for k in ("鞋", "靴"))):
            # 鞋图是「俯视一双鞋占满整幅」:通用分镜里按普通道具档会被整幅抄成巨鞋,
            # 所以压到很低的 0.15;但**换装链上的镜头**(鞋子是穿在脚上的)太小会丢掉
            # 鞋型细节 —— 实测 s01 把包头半拖画成了露趾凉拖。链上镜头用道具档(0.33)。
            scale = (settings.float_ref_scale_shoe
                     if (shot.options or {}).get("universal")
                     else settings.float_ref_scale_prop)
        elif asset is not None and (
                (asset.ai_kind or "").strip() in ("头", "配饰")
                or any(k in (asset.ai_name or "") for k in _SMALL_ACC_HINTS)):
            # 小件配饰(墨镜 / 帽子 / 项链)同理:整幅商品图会被当成场景里的物件再画一遍
            # (用户 2026-10-23 p349 第 12 镜:脚边多出一副墨镜),通用分镜压到很小的比例。
            scale = (settings.float_ref_scale_small
                     if (shot.options or {}).get("universal")
                     else settings.float_ref_scale_prop)
        else:
            scale = settings.float_ref_scale_prop
        # 飞进来的那一件再压一档(道具档 0.33 实测仍会被画成半幅宽的巨布)
        if _fly_name and asset is not None and (asset.ai_name or "").strip() == _fly_name:
            scale = min(scale, _FLY_REF_SCALE)
        out[os.path.normpath(path)] = scale
    return out


# ---------------- 悬浮变装:跨镜人物大小归一(只动后期,不重新生成) ----------------
# 同一套素材,模型每镜自己取景:实测背心镜人占画面 0.812、长裤镜 0.701 —— 并排看就是
# "人忽大忽小"。这里把每个换装镜 / 展示镜重新取景到同一个固定目标(.env 可配)。
# 阈值 200 是实测定出来的:近白背景(≈237)和脚下的接地阴影(≈215+)都进不来,
# 而黑打底(0)和浅灰衣服(≈150)都进得来,所以量出来的人物框很稳(±1px)。
_ALIGN_L_THRESHOLD = 200


def _person_box(im: Image.Image) -> tuple[int, int, int, int] | None:
    """在近白背景里量出人物的外框 (左, 上, 右, 下);量不到返回 None。"""
    g = im.convert("L").point(lambda v: 255 if v < _ALIGN_L_THRESHOLD else 0)
    box = g.getbbox()
    if box is None:
        return None
    l, t, r, b = box
    if (b - t) < im.height * 0.2 or (r - l) < im.width * 0.05:
        return None                      # 框太小 = 其实没量到人,别乱动画面
    return l, t, r, b


def _head_center_x(im: Image.Image, box: tuple[int, int, int, int]) -> float:
    """头部那一横条的水平中心(用整幅阈值量的框)。"""
    l, t, r, b = box
    band = max(1, int((b - t) * 0.15))
    g = im.convert("L").point(lambda v: 255 if v < _ALIGN_L_THRESHOLD else 0)
    hb = g.crop((0, t, im.width, t + band)).getbbox()
    return (hb[0] + hb[2]) / 2 if hb else (l + r) / 2


# 逐帧量「头」用的阈值:只认深色(头发 / 眉眼)。换装镜里衣服是从画外飞进来的,
# 用"整幅算"的阈值会把浅灰的衣服也当成"人",头的位置就量歪了。
_ALIGN_DARK = 120


def _head_center_dark(im: Image.Image) -> float | None:
    """逐帧量「头」的水平中心:只取**从头顶往下固定一小条**(画面高的 ~10%)。

    以前是取「深色像素整体框」的上 30% —— 但换装镜里飞在半空的深色衣服会把这个框整体
    往下拉长,那一条也跟着变高,把半空的衣服一起圈进来,量到的"头中心"其实是「人+衣服」
    的中点 → 归一后把人推到一边(用户报"人都跑到旁边站着了")。
    改成固定一小条之后,只有头发 / 脸落得进这条,半空的衣服够不到。
    """
    g = im.convert("L").point(lambda v: 255 if v < _ALIGN_DARK else 0)
    box = g.getbbox()
    if box is None:
        return None
    l, t, r, b = box
    band = min(t + max(3, round(im.height * 0.10)), im.height)
    hb = g.crop((0, t, im.width, band)).getbbox()
    if hb is None:
        return None
    return (hb[0] + hb[2]) / 2


def _whiten_bg_neutral(im: Image.Image) -> Image.Image:
    """把「中性浅灰」刷成纯白:背景地面阴影 / 缩放后留下的垫边都会被一起抹平。

    只碰「三通道都够亮且几乎没有色差(chroma<=12)」的像素 ——
    米色衣服 / 皮肤 / 拖鞋色差都明显大于 12,不会被抹掉。

    用户 2026-10-23:以前是**硬阈值**(min>=195 直接刷 255),阴影边缘会被切成
    锯齿状的灰块(压缩后更明显,像画面里多了一条灰色斜带)。现在改成
    **渐变蒙版 + 羽化**:亮度从 170 到 240 平滑过渡到白,边缘再高斯模糊一下,
    阴影会淡成柔和的渐变,不会出现方块。
    """
    arr = np.asarray(im.convert("RGB")).astype(np.float32)
    mn = arr.min(axis=2)
    chroma = arr.max(axis=2) - mn
    strength = np.clip((mn - 160.0) / 60.0, 0.0, 1.0)
    strength[chroma > 12] = 0.0
    mask = Image.fromarray((strength * 255).astype(np.uint8), mode="L")
    mask = mask.filter(ImageFilter.GaussianBlur(2.5))
    white = Image.new("RGB", im.size, (255, 255, 255))
    return Image.composite(white, im.convert("RGB"), mask)


def _align_person(frames: list[Image.Image]) -> list[Image.Image]:
    """把这一镜重新取景,让「人物高度 / 头顶位置 / 头部水平位置」落到 .env 配的目标上。

    缩放和上下位置用**最后一帧**定(换装镜到最后一帧已经穿好、站定,量得最准),整段共用;
    **水平位置逐帧单独算** —— 实测模型会在片段中途把人从偏右自己平移到正中
    (开衫镜实测:第 0~21 帧头中心 0.62、第 28 帧后 0.50),一个固定变换治不了这种
    「段内自己漂移」,只有每帧都把她拉回中线才能压平。背景是近白,横移看不出来。
    """
    if not settings.float_align or not frames:
        return frames
    box = _person_box(frames[-1])
    if box is None:
        print("[pipeline] 人物归一:量不到人物框,本镜跳过", flush=True)
        return frames
    l, t, r, b = box
    W, H = frames[0].size
    scale = max(1, round(settings.float_align_height * H)) / (b - t)
    off_y = round(round(settings.float_align_top * H) - t * scale)

    # 逐帧的「头中心」:量不到就用上一个有效值。
    # **必须先做中值滤波再做限幅** —— 换装镜里飞进来的衣服会被当成"深色头部",
    # 头中心在某些帧突然跳几十像素;直接按帧横移就会变成"人物左右晃"(用户报)。
    raw = [_head_center_dark(f) for f in frames]
    fallback = next((v for v in reversed(raw) if v is not None), None) or _head_center_x(frames[-1], box)
    vals, prev = [], fallback
    for v in raw:
        prev = v if v is not None else prev
        vals.append(prev)

    def _median(seq, i, w=4):
        seg = sorted(seq[max(0, i - w):i + w + 1])
        return seg[len(seg) // 2]

    med = [_median(vals, i) for i in range(len(vals))]
    # 再限制逐帧变化:全片横移只能平滑地走,不许一帧跳一下
    limited, cur = [], med[0]
    for v in med:
        cur = max(cur - 1.0, min(cur + 1.0, v))
        limited.append(cur)
    offs = [round(W / 2 - x * scale) for x in limited]

    if abs(scale - 1.0) < 0.02 and max(abs(o) for o in offs) < 3 and abs(off_y) < 3:
        print(f"[pipeline] 人物归一:本镜 {(b - t) / H:.3f} 已在目标附近,跳过取景", flush=True)
        return [_whiten_bg_neutral(f.convert("RGB")) for f in frames]
    nw, nh = max(1, round(W * scale)), max(1, round(H * scale))
    pad = frames[-1].convert("RGB").getpixel((0, 0))
    out: list[Image.Image] = []
    for im, ox in zip(frames, offs):
        canvas = Image.new("RGB", (W, H), pad)
        canvas.paste(im.convert("RGB").resize((nw, nh), Image.LANCZOS), (ox, off_y))
        # 缩放后画面底部会出现"原片地面阴影 + 纯色垫边"的灰色块,这里一并抹成纯白
        out.append(_whiten_bg_neutral(canvas))
    print(f"[pipeline] 人物归一:人高 {(b - t) / H:.3f} → {settings.float_align_height:.2f}"
          f"(缩放 {scale:.3f},逐帧横移 {min(offs)}~{max(offs)}px,纵移 {off_y}px)", flush=True)
    return out


def _align_top(frames: list[Image.Image]) -> list[Image.Image]:
    """半身 / 近景镜:只做上下平移,把头顶压到「头顶留白」的目标位置。

    **不缩放** —— 近景镜缩放会把脸和衣服一起改大小;这里只把画面整体上下挪,
    让上沿留白和全身镜统一(默认 15%)。量不到"人像"的镜头(比如只拍脚的近景)原样返回。
    """
    if not settings.float_align or not frames:
        return frames
    box = _person_box(frames[-1])
    if box is None:
        return frames
    _l, t, _r, b = box
    H = frames[0].height
    # 框太小 / 顶部太靠下 = 不是"头在上沿的半身像"(比如脚部特写),不动
    if (b - t) < H * 0.25 or t > H * 0.45:
        return frames
    want = round(settings.float_align_top * H)
    off = want - t
    if abs(off) < 3:
        return [_whiten_bg_neutral(f.convert("RGB")) for f in frames]
    pad = frames[-1].convert("RGB").getpixel((0, 0))
    out: list[Image.Image] = []
    for im in frames:
        canvas = Image.new("RGB", im.size, pad)
        canvas.paste(im.convert("RGB"), (0, off))
        out.append(_whiten_bg_neutral(canvas))
    print(f"[pipeline] 半身取景:头顶 {t / H:.3f} → {settings.float_align_top:.2f}"
          f"(纵向平移 {off}px)", flush=True)
    return out


def _lift_shadows(frames: list[Image.Image]) -> list[Image.Image]:
    """暗部轻提亮:把黑色面料从「一片死黑」里拉回来,让褶皱 / 纹理重新可见。

    纯白棚拍 + 黑裙的对比太大,生成端会把暗部压到 0~8,肉眼看就是一块黑布。
    只在**低亮度区**加增益(越暗抬得越多、y≥120 完全不碰),所以白背景、肤色、
    衣服颜色本身都不变 —— 只是暗部细节不再糊死。
    """
    k = float(getattr(settings, "shadow_lift", 0) or 0)
    if k <= 0 or not frames:
        return frames
    import numpy as np

    out: list[Image.Image] = []
    for im in frames:
        arr = np.asarray(im.convert("RGB")).astype(np.float32)
        y = arr.max(axis=2)
        w = np.clip((120.0 - y) / 120.0, 0.0, 1.0) ** 2 * k
        arr = arr + w[..., None]
        out.append(Image.fromarray(np.clip(arr, 0, 255).astype(np.uint8)))
    return out


def _align_shot(frames: list[Image.Image], shot) -> list[Image.Image]:
    """这一镜是不是要参与人物归一:悬浮变装的换装镜 / 全身展示镜 / 全身通用分镜。"""
    return _lift_shadows(_align_shot_framing(frames, shot))


def _align_shot_framing(frames: list[Image.Image], shot) -> list[Image.Image]:
    if (shot.options or {}).get("bg"):
        # 有「人物背景图」时背景不是近白 —— _person_box 会把整幅画都当成"人",
        # 量出来的框是整张图,归一之后人物会被缩小 / 挪位。整镜跳过。
        return frames
    if not (shot.options or {}).get("float"):
        return frames
    p = shot.prompt or ""
    # 带「本镜不用标准取景框」标记的镜(俯拍只拍到嘴、中景礼炮这类):
    # 它们的景别由动作自己写死,这里只把背景刷白,**不做任何缩放 / 位移** ——
    # 否则会因为提示词里出现「人物全身参考图」之类的字眼被当成全身镜重新缩放,
    # 看起来就像整段在不停放大。
    if "本镜不用标准取景框" in p:
        return [_whiten_bg_neutral(f.convert("RGB")) for f in frames]
    # 换装链的措辞 + 通用分镜自己写的「全身构图 / 从头到脚」都算全身镜,
    # 统一按同一套取景重新框一遍,人物大小才一致。
    full = is_full_body_shot(p) or "全身" in p or "从头到脚" in p
    return _align_person(frames) if full else _align_top(frames)


def _bg_is_whitish(im: Image.Image, thresh: float = 0.55) -> bool:
    """这一帧是不是「近白背景」——决定取景归一能不能靠补白边实现。

    取景归一是「整段缩放 + 四周补白」;背景不是白的时候,缩小会在四周留一圈纯白,
    一眼就穿帮(用户 2026-10-22「如果我视频是有背景的,那不是有问题」)。
    只量四条边:近白 = 三通道都够亮且几乎无色差。
    """
    arr = np.asarray(im.convert("RGB"))
    h, w = arr.shape[:2]
    band = np.concatenate([
        arr[:6].reshape(-1, 3), arr[max(0, h - 6):].reshape(-1, 3),
        arr[:, :6].reshape(-1, 3), arr[:, max(0, w - 6):].reshape(-1, 3),
    ]).astype(np.int16)
    bright = band.min(axis=1) >= 228
    chroma = band.max(axis=1) - band.min(axis=1)
    return float((bright & (chroma <= 16)).mean()) >= thresh


def _match_chain_frame(frames: list[Image.Image], shot) -> list[Image.Image]:
    """把换装链镜头的**取景**统一:头顶留白 / 脚底留白落到同一个值。

    模型每一镜画的人大小都不一样(实测同一条链里有 88% 也有 69%),硬切过去会突然变大变小。
    这里按**最后一帧**量出人物框,整段用同一个缩放倍数 + 同一个纵向偏移,把人物缩到
    「占画面高 70%、头顶留白 15%、脚底留白 15%」;背景本来就是纯白,补边看不出来。
    **只对换装链的全身镜生效**(有 state_out / anchor_last / state_first 的那几镜);
    通用 / 自定义分镜不参与 —— 它们的取景靠提示词里的「远景镜头」描述(用户 2026-10-22)。
    """
    if not settings.frame_match or not frames:
        return frames
    opts = shot.options or {}
    # 通用 / 自定义分镜不做后期缩放(用户 2026-10-22「不要做归一」)——
    # 取景交给生成端的「远景镜头」描述,后期硬拉会破坏背景和构图。
    if opts.get("universal") or opts.get("bg"):
        return frames
    if not (opts.get("state_out") or opts.get("anchor_last") or opts.get("state_first")):
        return frames
    box = _person_box(frames[-1])
    if box is None:
        return frames
    _l, t, _r, b = box
    W, H = frames[0].size
    if (b - t) < H * 0.30:                  # 半身 / 局部镜不参与
        return frames
    # 头顶 / 脚底已经贴边 = 人像被裁掉了一块,再怎么缩放也补不回来(只会更糊 / 更怪)
    if t <= 2 or b >= H - 3:
        print(f"[pipeline] 镜头 {shot.key} 人物被画框裁切(t={t},b={b}),跳过取景对齐", flush=True)
        return frames
    scale = (settings.float_align_height * H) / max(1, b - t)
    # 缩放超过一定幅度就是模型本身画错了,硬拉只会更糊;通用分镜偏差更大,给宽一点的区间
    _lo, _hi = (0.72, 1.38) if opts.get("frame_pp") else (0.80, 1.20)
    scale = max(_lo, min(_hi, scale))
    # 有背景(墙面 / 地面 / 实景)时不能靠「缩小 + 补白边」,否则四周会露出一圈白。
    # 需要放大时改成**裁切**放大(裁掉外围、不补边);需要缩小时直接跳过,保住背景。
    if not _bg_is_whitish(frames[-1]):
        if scale < 1.0:
            print(f"[pipeline] 镜头 {shot.key} 有背景且需要缩小(scale={scale:.3f}),跳过取景对齐",
                  flush=True)
            return frames
        win_h = min(H, max(1, round((b - t) / settings.float_align_height)))
        win_w = min(W, max(1, round(win_h * W / H)))
        x0 = int(round((_l + _r) / 2 - win_w / 2))
        x0 = max(0, min(W - win_w, x0))
        y0 = int(round(t - settings.float_align_top * win_h))
        y0 = max(0, min(H - win_h, y0))
        if win_w == W and win_h == H:
            return frames
        out = [
            im.convert("RGB").crop((x0, y0, x0 + win_w, y0 + win_h)).resize((W, H), Image.LANCZOS)
            for im in frames
        ]
        print(f"[pipeline] 镜头 {shot.key} 取景对齐(有背景,裁切):人高 {(b - t) / H:.3f} → "
              f"{settings.float_align_height:.2f}(窗口 {win_w}x{win_h})", flush=True)
        return out
    off_y = round(settings.float_align_top * H - t * scale)
    if abs(scale - 1.0) < 0.008 and abs(off_y) < 3:
        return [_whiten_bg_neutral(f.convert("RGB")) for f in frames]
    nw, nh = max(1, round(W * scale)), max(1, round(H * scale))
    off_x = round((W - nw) / 2)
    out: list[Image.Image] = []
    for im in frames:
        canvas = Image.new("RGB", (W, H), (255, 255, 255))
        canvas.paste(im.convert("RGB").resize((nw, nh), Image.LANCZOS), (off_x, off_y))
        out.append(_whiten_bg_neutral(canvas))
    print(f"[pipeline] 镜头 {shot.key} 取景对齐:人高 {(b - t) / H:.3f} → "
          f"{settings.float_align_height:.2f}(缩放 {scale:.3f},纵移 {off_y}px)", flush=True)
    return out


def _feet_anchor(im: Image.Image) -> tuple[float, int] | None:
    """量「鞋 / 脚」这一块的水平中心 + 鞋底那一行(状态图对齐用)。

    只取脚踝以下那一条(人高的 16%),阈值 235 —— 和 `_stance_score` 同一套取法:
    白底 + 柔和光晕,阈值收太紧会把鞋边当背景,量出来会左右跳。
    """
    box = _person_box(im)
    if box is None:
        return None
    l, t, r, b = box
    ph = b - t
    if ph < 40:
        return None
    y0 = max(0, int(b - ph * 0.16))
    a = np.asarray(im.convert("RGB"))[y0:b + 1, l:r + 1].astype(int)
    m = a.min(axis=2) < 235
    ys, xs = np.where(m)
    if len(xs) < 60:
        return None
    return float(xs.mean()) + l, int(b)


def _align_state_feet(im: Image.Image, project_id: int) -> Image.Image:
    """把这张状态图的**脚的水平位置**对齐到链头 S0 —— 同一双鞋落在同一个横向像素。

    **只做横移,不动纵向**(用户 2026-10-20:展示图顶部离头的距离必须一样):
    纵向取景已经由 `_align_state_image` 锁成「头顶 15%、人高 70%」,脚底自然落在 85%;
    以前这里连纵移也一起对齐 S0(实测 s4 被上抬 15px / 1024),结果同一批展示图
    头顶留白在 14.1%~15.3% 之间跳,并排看就是"人忽高忽低"。
    横向仍要对齐的根因:头部朝向每张略有不同 → 脚会落在差 5~8px 的位置
    (s2 鞋中心 292.9 / s3 287.3),换装镜只有首尾两个硬锚,模型就得演一次
    「换重心 / 挪半步」(用户报「人会挪步」)。
    """
    if not settings.state_feet_align:
        return im
    src_path = STORAGE_DIR / "states" / f"p{project_id}_s0.png"
    try:
        if not src_path.exists():
            return im
        src = Image.open(src_path).convert("RGB")
    except Exception:  # noqa: BLE001
        return im
    if src.size != im.size:
        return im
    a, b = _feet_anchor(src), _feet_anchor(im)
    if a is None or b is None:
        return im
    W, H = im.size
    dx = int(round(a[0] - b[0]))
    # 只修「几像素级」的差:差太多说明量的不是同一双脚(近景 / 裁切),宁可不碰
    if abs(dx) > W * 0.06:
        return im
    if dx == 0:
        return im
    canvas = Image.new("RGB", (W, H), (255, 255, 255))
    canvas.paste(im.convert("RGB"), (dx, 0))
    print(f"[pipeline] 状态图脚部横向对齐 S0:横移 {dx}px", flush=True)
    return _whiten_bg_neutral(canvas)


def _align_state_image(im: Image.Image) -> Image.Image:
    """把一张**静帧状态图**重新取景到「人物 70% 高、头顶留白 15%」。

    只动这一张锚图,不动视频里的帧:换装链每一镜的首/尾帧锚都是上一镜落盘的状态图,
    链头那张定成 70% 之后,后面的镜头会照着锚图抄,人天然就是小的,不需要后期逐帧缩放
    (逐帧缩放会被飞进来的衣服带偏 → 人物左右晃)。给人物参考图四周加白边试过 ——
    模型不抄参考图的取景(实测打底展示镜仍是 85% 高、头顶 9%)。
    """
    if not settings.state_align:
        return im
    box = _person_box(im)
    if box is None:
        return im
    l, t, r, b = box
    W, H = im.size
    ph = b - t
    if ph < H * 0.2:
        return im
    want = settings.float_align_height * H
    k = want / ph
    nw, nh = max(1, round(W * k)), max(1, round(H * k))
    hx = _head_center_x(im, box) * k
    ox = round(W / 2 - hx)
    oy = round(settings.float_align_top * H - t * k)
    # 已经落在这套取景上(±2px)才跳过重采样;以前用「身高差 2% 以内就跳过」,
    # 结果每张状态图的身高在 68%~72% 之间随机,
    # 头顶留白也就跟着在 14%~16% 之间漂(用户 2026-10-20:展示图顶部离头的距离要一样)。
    # 现在只有 k≈1 且位移 <2px 才跳过 —— 每张状态图都精确落在 70% / 15%。
    if abs(k - 1.0) < 0.002 and abs(ox) < 2 and abs(oy) < 2:
        return _whiten_bg_neutral(im.convert("RGB"))
    scaled = im.convert("RGB").resize((nw, nh), Image.LANCZOS)
    canvas = Image.new("RGB", (W, H), (255, 255, 255))
    canvas.paste(scaled, (ox, oy))
    print(f"[pipeline] 状态图重新取景:人高 {ph / H:.3f} → {settings.float_align_height:.2f}"
          f"(缩放 {k:.3f},横移 {ox}px,纵移 {oy}px)", flush=True)
    return _whiten_bg_neutral(canvas)


def _head_geom(im: Image.Image, box: tuple[int, int, int, int]) -> tuple[float, int, int, int]:
    """头的位置/大小:水平中心、头顶 y、头的半宽、头+脖子高度(都按人高比例)。"""
    l, t, r, b = box
    ph = b - t
    return _head_center_x(im, box), t, int(ph * 0.115), int(ph * 0.19)


def _face_lock_state(im: Image.Image, project_id: int) -> Image.Image:
    """把状态图里的头换成链头 S0 的 —— 每换一次衣服模型都会把脸重画一遍,
    几跳之后嘴就会糊 / 变形(实测 S2)。状态图的头部位置和大小是固定的,
    把 S0 的头按「头部中心 + 头顶」对齐贴回来,嘴就永远是第一张那个嘴。
    """
    if not settings.face_lock:
        return im
    src_path = STORAGE_DIR / "states" / f"p{project_id}_s0.png"
    if not src_path.exists():
        return im
    try:
        src = Image.open(src_path).convert("RGB")
    except Exception:  # noqa: BLE001
        return im
    if src.size != im.size:
        return im
    b1, b2 = _person_box(src), _person_box(im)
    if b1 is None or b2 is None:
        return im
    sx, st, sw, sh = _head_geom(src, b1)
    dx, dt, dw, dh = _head_geom(im, b2)
    # 头大小差太多 = 不是同一套取景,别硬贴
    if abs(sw - dw) > max(6, dw * 0.12):
        return im
    pad = 12
    crop = src.crop((int(sx - sw - pad), int(st - pad),
                     int(sx + sw + pad), int(st + sh + pad)))
    w, h = crop.size
    mask = Image.new("L", (w, h), 0)
    ImageDraw.Draw(mask).ellipse((6, 6, w - 6, h - 6), fill=255)
    mask = mask.filter(ImageFilter.GaussianBlur(6))
    out = im.copy()
    out.paste(crop, (int(dx - sw - pad), int(dt - pad)), mask)
    return out


def _changed_kind(shot) -> str | None:
    """这一镜换的是哪一类件(top/outer/bottom/...);解析不到返回 None。"""
    m = re.search(r"在「([^」]+)」碰到", shot.prompt or "")
    if not m:
        return None
    from .piece_prompt import kind_of
    return kind_of(m.group(1))


def _lock_feet(frames: list[Image.Image]) -> list[Image.Image]:
    """把**脚踝以下**锁死成「整段里脚最正的那一帧」—— 换装镜里衣服飞进来的那一下,
    模型会把脚转成外八。机位锁死、人站定,脚本来就不该动;拿整段里站姿最好的一帧盖住全段。
    只换画面最下面一条(约 79.5% 高度以下),接缝上下各 8px 羽化;纯白背景上看不出来。
    """
    if not frames:
        return frames
    W, H = frames[0].size
    cut = int(H * 0.795)
    scored = [(_stance_score(f), f) for f in frames]
    scored = [(s, f) for s, f in scored if s is not None]
    src, best = (min(scored, key=lambda x: x[0])[1], min(s for s, _ in scored)) if scored else (frames[-1], None)
    if best is not None:
        print(f"[pipeline] 脚部锁死:取整段最正的一帧({best:.1f}°)", flush=True)
    src = src.convert("RGB")
    strip = src.crop((0, cut, W, H))
    top_src = src.crop((0, cut - 8, W, cut + 8))
    grad = Image.linear_gradient("L").resize((W, 16), Image.BILINEAR)
    out: list[Image.Image] = []
    for im in frames:
        im = im.convert("RGB")
        canvas = im.copy()
        seam = Image.composite(top_src, im.crop((0, cut - 8, W, cut + 8)), grad)
        canvas.paste(seam, (0, cut - 8))
        canvas.paste(strip, (0, cut))
        out.append(canvas)
    return out


def _maybe_lock_feet(frames: list[Image.Image], shot) -> list[Image.Image]:
    """脚踝以下锁死。

    * 换装镜(换的是上装 / 外套):整段用同一条脚 —— 衣服飞进来时模型会把脚转成外八;
    * 展示镜(产状态图的 state_only):用户 2026-10-19 要求「腿和脚不要动」,
      也锁踝以下(它只换衣服,脚本来就不该动);
    * 换下装 / 通用镜跳过(裤脚 / 裙摆会经过脚踝)。
    """
    opts = shot.options or {}
    if not settings.feet_lock or opts.get("universal"):
        return frames
    if not opts.get("state_only") and not (opts.get("anchor0") and opts.get("anchor_last")):
        return frames
    if not opts.get("state_only") and _changed_kind(shot) == "bottom":
        return frames
    print(f"[pipeline] 镜头 {shot.key} 脚部锁死:踝以下全段用同一帧", flush=True)
    return _lock_feet(frames)


def _lock_lateral(frames: list[Image.Image], shot) -> list[Image.Image]:
    """把整段人物的**水平位置**锁到第一帧(只做左右平移,不缩放、不改取景)。

    换装镜的首尾锚图中心只差几像素,模型却会在衣服飞进来时把整个人横着滑一段
    (项目 316 的 s03:头中心 381 → 338,约 43px)。纯白背景下平移看不出来,
    所以按逐帧「头部中心」把画面推回去;每帧最多 1.5px 的斜坡,避免抖动。
    """
    opts = shot.options or {}
    if not settings.lateral_lock or opts.get("universal") or opts.get("bg"):
        return frames
    # 首尾帧硬锚关掉时不锁:这套锁位是为了抵消「两张锚图中心差几像素 → 模型横滑」,
    # 没有锚就没有这个横滑;而且它量的「上部深色中心」会被飞进来的深色道具(包)带偏
    # (实测 319 s11 头中心 388→270→416,锁位跟着来回推 30px,画面左右晃)。
    if not settings.chain_anchor:
        return frames
    if not (opts.get("anchor0") and opts.get("anchor_last")):
        return frames
    if len(frames) < 3:
        return frames
    W, H = frames[0].size
    raw = [_head_center_dark(f) for f in frames]
    fallback = next((v for v in raw if v is not None), None)
    if fallback is None:
        return frames
    vals, prev = [], fallback
    for v in raw:
        prev = v if v is not None else prev
        vals.append(prev)

    def _median(seq, i, w=4):
        seg = sorted(seq[max(0, i - w): i + w + 1])
        return seg[len(seg) // 2]

    med = [_median(vals, i) for i in range(len(vals))]
    target = med[0]
    offs, cur = [], 0.0
    for v in med:
        want = target - v
        # 每帧最多 3px 的斜坡:316 s03 的横滑是在 24 帧里走完 44px(约 2px/帧),
        # 限 1.5px 会追不上、留下 20px 余量;3px 仍够平滑,不会抖。
        cur = max(cur - 3.0, min(cur + 3.0, want))
        offs.append(int(round(cur)))
    if max(abs(o) for o in offs) < 2:
        return frames
    out: list[Image.Image] = []
    for im, dx in zip(frames, offs):
        if dx == 0:
            out.append(im)
            continue
        canvas = Image.new("RGB", (W, H), (255, 255, 255))
        canvas.paste(im.convert("RGB"), (dx, 0))
        out.append(canvas)
    print(f"[pipeline] 镜头 {shot.key} 水平锁位:最大横移 {max(offs)}px", flush=True)
    return out


def _stance_score(im: Image.Image) -> float | None:
    """量「站姿有多外八」:从脚踝中心到脚尖中心连一条线,左右脚各算水平偏角,取较差值。

    0° = 两只脚都正对镜头(立正);角度越大越外八。**取两只脚里较差的那只** ——
    只外八一只脚时,取平均会把它冲淡(实测 S2 右脚 17.9°、平均只有 9.4°),必须按最差脚算。
    量不到脚(半身镜 / 画面里没有鞋)返回 None,调用方就跳过自动挑选。

    **不能用「上下两条横带的 bbox 中心」**:拖鞋是宽扁的,旋转后 bbox 中心几乎不动,
    实测 S2 明显外八却只量出 4.9°。改成「脚踝(脚踝那一条的像素中心) → 脚尖(最低
    那一条的像素中心)」之后,S2 量出 9.4°(右脚 17.9°),和人眼判断一致。
    """
    box = _person_box(im)
    if box is None:
        return None
    l, t, r, b = box
    ph = b - t
    y0 = max(0, int(b - ph * 0.16))          # 只取脚踝以下
    a = np.asarray(im.convert("RGB"))[y0:b, l:r + 1].astype(int)
    m = a.min(axis=2) < 235
    H, W = m.shape
    if W < 20 or H < 20:
        return None
    cx = W // 2
    angles: list[float] = []
    for xa, xb in ((0, cx), (cx, W)):
        sub = m[:, xa:xb]
        ys, xs = np.where(sub)
        if len(xs) < 80:
            continue
        ymin, ymax = int(ys.min()), int(ys.max())
        top = ys <= ymin + max(2, int((ymax - ymin) * 0.08))   # 脚踝
        bot = ys >= ymax - max(2, int((ymax - ymin) * 0.06))   # 脚尖
        ax, ay = xs[top].mean(), ys[top].mean()
        tx, ty = xs[bot].mean(), ys[bot].mean()
        angles.append(abs(math.degrees(math.atan2(tx - ax, max(1e-6, ty - ay)))))
    if not angles:
        return None
    return max(angles)


def _beat(job_id: int) -> None:
    """给任务打一次心跳 —— 看门狗靠它判断这个任务是不是还在推进。

    用独立的短连接,免得搅乱主流程的事务;心跳写失败也绝不能影响渲染。
    """
    try:
        db = SessionLocal()
        try:
            db.query(RenderJob).filter(RenderJob.id == job_id).update(
                {RenderJob.heartbeat_at: utcnow()}
            )
            db.commit()
        finally:
            db.close()
    except Exception:  # noqa: BLE001
        pass


def _ref_path(db: Session, ref: str) -> str | None:
    """单个引用 -> 绝对路径(和 resolve_refs 同一套规则)。"""
    ref = (ref or "").strip()
    if ref.startswith("asset:"):
        try:
            asset = db.get(Asset, int(ref.split(":", 1)[1]))
        except (ValueError, TypeError):
            return None
        return str(STORAGE_DIR / asset.rel_path) if asset else None
    if ref.startswith("file:"):
        return ref[5:]
    return None


def resolve_refs(db: Session, shot) -> list[str]:
    """Turn ["asset:3", "file:H:/x.png"] into existing absolute paths."""
    paths: list[str] = []
    for ref in shot.refs or []:
        if not isinstance(ref, str):
            continue
        if ref.startswith("asset:"):
            try:
                asset = db.get(Asset, int(ref.split(":", 1)[1]))
            except ValueError:
                continue
            if asset:
                paths.append(str(STORAGE_DIR / asset.rel_path))
        elif ref.startswith("file:"):
            paths.append(ref[5:])
    return [p for p in paths if os.path.exists(p)]


def load_frames(path) -> list[Image.Image]:
    container = av.open(str(path))
    frames = [f.to_image().convert("RGB") for f in container.decode(video=0)]
    container.close()
    return frames


def segment(frames: list[Image.Image], need: int, options: dict | None) -> list[Image.Image]:
    """Frames for one shot slot.

    options.source_frames lets a shot play faster than real time: it samples `need` frames
    evenly out of the first `source_frames` of the generated clip (e.g. 84 out of 120 ~ 1.4x).
    """
    src_n = int((options or {}).get("source_frames") or 0)
    if src_n and len(frames) >= src_n > need:
        src = frames[:src_n]
        idx = [round(i * (len(src) - 1) / (need - 1)) for i in range(need)]
        return [src[i] for i in idx]
    seg = frames[:need]
    if 0 < len(seg) < need:
        seg = seg + [seg[-1]] * (need - len(seg))
    return seg


def _skin_median_rgb(im: Image.Image):
    """量一张图里皮肤像素的中位 RGB(用 YCrCb 肤色范围做 mask)。"""
    try:
        import cv2
        import numpy as np
    except Exception:  # noqa: BLE001
        return None, None
    rgb = np.asarray(im.convert("RGB"))
    yc = cv2.cvtColor(rgb, cv2.COLOR_RGB2YCrCb)
    y, cr, cb = cv2.split(yc)
    m = ((cr > 130) & (cr < 180) & (cb > 75) & (cb < 135) & (y > 60)).astype(np.uint8)
    m = cv2.morphologyEx(m, cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))
    if int(m.sum()) < im.width * im.height * 0.003:
        return None, None
    px = rgb[m.astype(bool)]
    return np.median(px, axis=0).astype(np.float32), m


def _lock_state_tone(im: Image.Image, project_id: int) -> Image.Image:
    """把状态图的**肤色**拉回链头 S0(只做肤色增益,不动大小 / 取景 / 画面内容)。

    每换一次衣服,模型都会把肤色画深一点点;状态图又是下一镜的锚图,
    于是整条链一路加深(p314:s0 [191,163,156] → s6 [172,135,118])。
    这里按 S0 的中位肤色算一个逐通道增益(夹在 0.85~1.15),只在肤色 mask 里生效。
    """
    if not settings.state_tone_lock:
        return im
    src = STORAGE_DIR / "states" / f"p{project_id}_s0.png"
    if not src.exists():
        return im
    try:
        import cv2
        import numpy as np
        ref_im = Image.open(src).convert("RGB")
        ref, _ = _skin_median_rgb(ref_im)
        cur, mask = _skin_median_rgb(im)
        if ref is None or cur is None or mask is None:
            return im
        gains = np.clip(ref / np.maximum(cur, 1.0), 0.85, 1.15)
        if float(np.abs(gains - 1.0).max()) < 0.01:
            return im
        soft = cv2.GaussianBlur(mask * 255, (0, 0), 4).astype(np.float32) / 255.0
        arr = np.asarray(im.convert("RGB")).astype(np.float32)
        arr = arr * (1.0 + soft[..., None] * (gains - 1.0))
        print(f"[pipeline] 肤色锁定 S0:增益 {np.round(gains, 3).tolist()}", flush=True)
        return Image.fromarray(np.clip(arr, 0, 255).astype(np.uint8))
    except Exception as exc:  # noqa: BLE001
        print(f"[pipeline] 肤色锁定跳过:{exc}", flush=True)
        return im


def _lock_clip_tone(frames: list[Image.Image], project_id: int) -> list[Image.Image]:
    """把整段视频的**肤色**也拉回链头 S0(只动肤色像素,不动大小 / 取景 / 衣物)。

    只锁状态图是不够的:状态图被拉亮了,成片里这一镜的最后一帧还是暗的,
    下一镜从亮锚图起手 —— 换镜处肤色会「跳一下」。所以镜头落盘前先按同一套增益把画面压回 S0。
    增益只算一次(取中间帧),整段用同一个值,避免逐帧变化导致肤色闪。
    """
    if not settings.state_tone_lock or not frames:
        return frames
    src = STORAGE_DIR / "states" / f"p{project_id}_s0.png"
    if not src.exists():
        return frames
    try:
        import cv2
        import numpy as np
        ref_im = Image.open(src).convert("RGB")
        ref, _ = _skin_median_rgb(ref_im)
        cur, _ = _skin_median_rgb(frames[len(frames) // 2])
        if ref is None or cur is None:
            return frames
        gains = np.clip(ref / np.maximum(cur, 1.0), 0.88, 1.15)
        if float(np.abs(gains - 1.0).max()) < 0.02:
            return frames
        out: list[Image.Image] = []
        for im in frames:
            _, mask = _skin_median_rgb(im)
            if mask is None:
                out.append(im)
                continue
            soft = cv2.GaussianBlur(mask * 255, (0, 0), 4).astype(np.float32) / 255.0
            arr = np.asarray(im.convert("RGB")).astype(np.float32)
            arr = arr * (1.0 + soft[..., None] * (gains - 1.0))
            out.append(Image.fromarray(np.clip(arr, 0, 255).astype(np.uint8)))
        print(f"[pipeline] 片段肤色对齐 S0:增益 {np.round(gains, 3).tolist()}", flush=True)
        return out
    except Exception as exc:  # noqa: BLE001
        print(f"[pipeline] 片段肤色对齐跳过:{exc}", flush=True)
        return frames


def _clean_state_skin(im: Image.Image) -> Image.Image:
    """状态图落盘前压掉皮肤色块 / 彩噪(只动这张静态锚图,不动视频)。

    色块 / 膝盖红印本质是**低频色度偏移**,不是亮度细节问题。所以:
      · 转 YCrCb,先按肤色范围做一张 mask(只处理皮肤,衣服和背景不碰);
      · 色度 Cr/Cb 用「缩小 4 倍 → 高斯 → 放大」拿到低频底,再按 mask 混回去 ——
        这一步专门抹掉大块的泛红 / 发紫,双边滤波小半径对付不了这种几十像素的斑;
      · Y(亮度)只做一次 3×3 中值,去压缩噪点,不动皮肤纹理和五官。
    """
    if not settings.state_skin_clean:
        return im
    try:
        import cv2
        import numpy as np
        rgb = np.asarray(im.convert("RGB"))
        h, w = rgb.shape[:2]
        ycrcb = cv2.cvtColor(rgb, cv2.COLOR_RGB2YCrCb)
        y, cr, cb = cv2.split(ycrcb)

        # 肤色 mask:YCrCb 经典范围 + 形态学去碎点,再羽化,避免硬边
        m = ((cr > 130) & (cr < 180) & (cb > 75) & (cb < 135) & (y > 40)).astype(np.uint8) * 255
        m = cv2.morphologyEx(m, cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))
        m = cv2.morphologyEx(m, cv2.MORPH_CLOSE, np.ones((9, 9), np.uint8))
        if int((m > 0).sum()) < w * h * 0.005:
            return im
        soft = cv2.GaussianBlur(m, (0, 0), 4).astype(np.float32) / 255.0

        # 低频色度:缩小→模糊→放大(去掉几十像素级的色块),再按 mask 混回 85%
        sw, sh = max(1, w // 4), max(1, h // 4)
        def _low(a):
            s = cv2.resize(a, (sw, sh), interpolation=cv2.INTER_AREA)
            s = cv2.GaussianBlur(s, (0, 0), 3)
            return cv2.resize(s, (w, h), interpolation=cv2.INTER_LINEAR)
        cr2 = cr.astype(np.float32) * (1 - 0.85 * soft) + _low(cr).astype(np.float32) * (0.85 * soft)
        cb2 = cb.astype(np.float32) * (1 - 0.85 * soft) + _low(cb).astype(np.float32) * (0.85 * soft)

        y2 = cv2.medianBlur(y, 3)
        out = cv2.cvtColor(
            cv2.merge([y2, np.clip(cr2, 0, 255).astype(np.uint8),
                       np.clip(cb2, 0, 255).astype(np.uint8)]),
            cv2.COLOR_YCrCb2RGB)
        print("[pipeline] 状态图皮肤色块 / 彩噪已压平", flush=True)
        return Image.fromarray(out)
    except Exception as exc:  # noqa: BLE001
        print(f"[pipeline] 状态图皮肤清理跳过:{exc}", flush=True)
        return im


def _save_state_image(shot, frames: list[Image.Image]) -> None:
    """把这一镜的**末帧**落成一张「状态图」(shot.options.state_out),给换装镜当锚图。

    「按件排镜」的状态图链靠它串起来:展示镜拍完 → 末帧落盘 → 换装镜的尾帧锚就是它
    (换装镜照着它把衣服穿好,「穿不上 / 停在身前」这类问题就是这么解的)。
    落盘前先按「人物 70% 高、头顶 15%」重新取景(_align_state_image):状态图就是后续
    镜头的取景基准,这里定小,整条链都小,不用再对视频逐帧缩放。

    **链头例外**:options 里带 `state_first` 的那一镜(打底那套展示,它没有首帧锚)存的是
    **第 1 帧**。展示镜都会在 1 秒里自己漂一点,而链头的第 1 帧 = 模型照着人物图 + 文字画的
    第一版,最接近"她这会儿穿着什么";末帧已经把漂移带进去了。其余展示镜不能用第 1 帧 ——
    它们的第 1 帧被 anchor0 钉着,内容就是**上一张状态图**,存下来链会卡住不动。
    """
    dst = (shot.options or {}).get("state_out")
    if not dst or not frames:
        return
    first = bool((shot.options or {}).get("state_first"))
    try:
        parent = os.path.dirname(dst)
        if parent:
            os.makedirs(parent, exist_ok=True)
        state_im = _align_state_image(frames[0] if first else frames[-1])
        if not first:
            # 先把脚对齐到 S0(换装镜首/尾锚必须落在同一双鞋的位置上),
            # 再换脸 —— 顺序不能反:平移会把贴好的脸一起挪走。
            state_im = _align_state_feet(state_im, shot.project_id)
            # 肤色先拉回链头 S0(只做肤色增益):每换一件都会暗一点,不锁会沿链一路加深。
            # 放在换脸之前 —— 换上来的是 S0 的头,不该再被整体增益提亮一次。
            state_im = _lock_state_tone(state_im, shot.project_id)
            # 链头(S0)自己是脸的基准;后面的状态图都把脸换成 S0 的
            state_im = _face_lock_state(state_im, shot.project_id)
        else:
            state_im = _lock_state_tone(state_im, shot.project_id)
        # 最后压一遍皮肤色块 / 彩噪:状态图是下一镜的锚图 / 参考图,瑕疵会沿链传播。
        state_im = _clean_state_skin(state_im)
        state_im.save(dst)
        print(f"[pipeline] 镜头 {shot.key} 状态图已落盘({('首' if first else '末')}帧):"
              f"{os.path.basename(dst)}", flush=True)
    except Exception as exc:  # noqa: BLE001
        print(f"[pipeline] 镜头 {shot.key} 状态图落盘失败:{exc}", flush=True)


# ---------------------------------------------------------------- 换装镜的「过渡帧净化」
# **已废弃(2026-10-15 用户否掉:"你这方法不靠谱,先不要这样,到时候我自己手动搞")。**
# 当时的问题:模型在「旧件 → 新件」之间会漏出 1~N 帧"旧件已经脱掉、新件还只是一块搭在她身前的布"
# (185 基准 1 帧 / 189 2 帧 / 188 旧版 10 帧)。做法是把这几帧用前一帧顶掉 —— 属于后期改画面,
# 用户不要这种"程序替模型修"的路子。保留这段注释只为记下"为什么不做"和当时的实测数字,
# 别再把它加回来。


def _gen_size(project: Project) -> tuple[int, int]:
    """项目级生成分辨率:按成片比例推导,避免 16GB 卡跑不动交付分辨率。"""
    return derive_gen_size(project.out_width, project.out_height)


def _upscale(im: Image.Image, size: tuple[int, int]) -> Image.Image:
    big = im.resize(size, Image.LANCZOS)
    return big.filter(ImageFilter.UnsharpMask(radius=1.6, percent=68, threshold=3))


def _detail_pass_clip(clip_path: Path, shot, *, width: int, height: int, length: int,
                      seed: int, fps: int, on_tick=None) -> Path:
    """把已生成的片段当 guide,用第二 LoRA 跑一遍同分辨率 v2v 细节修复。

    失败 / 没有输出就退回原片 —— 不让细节修复把整条镜头挂掉。
    """
    try:
        from .threeview import _upload
        name = _upload(clip_path)
        in_path = upscale._comfy_input_dir() / name
        prefix = f"detail_{shot.key}_{int(time.time()) % 10 ** 6}"
        wf = build_detail_workflow(
            video_path=str(in_path), prompt=settings.detail_pass_prompt, seed=seed,
            prefix=prefix, width=width, height=height, length=length, fps=fps)
        pid = comfy.submit(wf)
        comfy.wait(pid, on_tick=on_tick)
        out = comfy.newest_output(prefix)
        if out is None:
            print("[pipeline] 细节修复没有输出,退回原片", flush=True)
            return clip_path
        print(f"[pipeline] 镜头 {shot.key} 细节修复完成:{out.name}", flush=True)
        return out
    except Exception as exc:  # noqa: BLE001
        print(f"[pipeline] 细节修复失败,退回原片:{str(exc)[:160]}", flush=True)
        return clip_path


def _maybe_upscale_clip(clip_path: Path, project: Project) -> Path:
    """把生成片段用 ESRGAN 超分到交付分辨率;失败就退回原片,不让整条镜头挂掉。"""
    if not settings.upscale_video:
        return clip_path
    dst = clip_path.with_name(clip_path.stem + "_up.mp4")
    try:
        ok = upscale.upscale_clip(clip_path, dst,
                                  project.out_width, project.out_height, project.fps)
    except Exception as exc:  # noqa: BLE001
        print(f"[pipeline] 超分失败,退回原片 {clip_path.name}: {str(exc)[:160]}", flush=True)
        return clip_path
    if ok:
        print(f"[pipeline] 超分完成 {dst.name}", flush=True)
        return dst
    print(f"[pipeline] 超分没有输出,退回原片 {clip_path.name}", flush=True)
    return clip_path


# ---------------------------------------------------------------- 成片增强(后期)
# 只在拼接成片时处理,不重新生成镜头 —— 所以旧项目重跑一遍后期就能变好看。
# 注意:不要把这些效果提前烘焙进 clips,否则复用片段时会叠加两次。
_GAMMA_LUT = [min(255, int(255 * ((i / 255) ** 1.08))) for i in range(256)]
_GAMMA_LUT_RGB = _GAMMA_LUT * 3      # RGB 图要求每个通道一份,共 768 项


def _vignette_mask(size: tuple[int, int], strength: float = 0.30) -> Image.Image:
    """暗角遮罩:在 64x64 上画径向渐变再放大,避免逐像素算距离。"""
    m = Image.new("L", (64, 64))
    px = m.load()
    for y in range(64):
        for x in range(64):
            dx = (x - 31.5) / 31.5
            dy = (y - 31.5) / 31.5
            r = min(1.0, (dx * dx + dy * dy) ** 0.5)
            px[x, y] = int(255 * (1 - strength * r * r))
    return m.resize(size, Image.BILINEAR).convert("RGB")


def _grade(im: Image.Image, vignette: Image.Image | None) -> Image.Image:
    """调色:提对比、加饱和、压暗部,再叠一层暗角 —— 治"发灰、没质感"。"""
    im = ImageEnhance.Contrast(im).enhance(1.16)
    im = ImageEnhance.Color(im).enhance(1.12)
    im = im.point(_GAMMA_LUT_RGB)
    if vignette is not None:
        im = ImageChops.multiply(im, vignette)
    return im


def _push(im: Image.Image, progress: float) -> Image.Image:
    """按镜头内的进度轻微推近(居中裁切再放大),让静态画面"活"起来。"""
    amount = settings.film_push_amount
    if amount <= 0 or progress <= 0:
        return im
    w, h = im.size
    z = 1.0 + amount * progress
    cw, ch = int(w / z), int(h / z)
    left, top = (w - cw) // 2, (h - ch) // 2
    return im.crop((left, top, left + cw, top + ch)).resize((w, h), Image.LANCZOS)


def _resample(frames: list[Image.Image], speed: float) -> list[Image.Image]:
    """变速:抽掉一部分帧(播放更快)。>1 变快,镜头与成片时长会相应变短。"""
    if speed <= 1.0 or len(frames) < 3:
        return frames
    n_out = max(2, int(len(frames) / speed))
    idx = [round(i * (len(frames) - 1) / (n_out - 1)) for i in range(n_out)]
    return [frames[i] for i in idx]


# ---------------------------------------------------------------- 状态镜之间的「刷过去」
# 悬浮变装的「状态镜链」(只穿打底 → +背心 → +长裤 → …)每两镜之间刷一次:上一层软边蒙版
# 从上往下推,推过去的那些像素换成下一镜的画面 —— 看起来就是"衣服从画面上方落下来、
# 刷过之后她已经穿好了"。两镜的人物大小/位置已经被 _align_person 对齐,所以刷得很干净。
def _wipe_mask(size: tuple[int, int], pos: float, soft: int) -> Image.Image:
    col = Image.new("L", (1, size[1]))
    px = col.load()
    for y in range(size[1]):
        a = (pos - y) / soft + 0.5
        px[0, y] = 255 if a >= 1 else (0 if a <= 0 else int(a * 255))
    return col.resize(size)


def _apply_wipe(frames: list[Image.Image], under: Image.Image,
                n: int, soft_frac: float) -> list[Image.Image]:
    """把这一镜的前 n 帧,用「从上往下刷」的软边蒙版叠在上一镜的末帧(under)上。"""
    if not frames or under is None or n <= 0:
        return frames
    W, H = frames[0].size
    soft = max(2, int(H * soft_frac))
    n = min(n, len(frames))
    base = under.convert("RGB")
    out = list(frames)
    for k in range(n):
        t = (k + 1) / n                                   # 最后一帧刷到底
        out[k] = Image.composite(frames[k].convert("RGB"), base,
                                 _wipe_mask((W, H), -soft + t * (H + 2 * soft), soft))
    return out


def _is_float_full(shot) -> bool:
    """这一镜是不是「悬浮变装的全身镜」(换装镜 / 展示镜)—— 只有这种镜头之间才刷过渡。"""
    return bool((shot.options or {}).get("float")) and is_full_body_shot(shot.prompt)


def _wants_wipe(shot) -> bool:
    """这一镜的开头要不要做「从上一镜刷过去」的过渡。

    「按件排镜」这条链**必须关掉** —— 它的每一剪都刚好差一件衣服(这正是它的设计),
    而且展示镜没有首帧锚、取景也会差一点;把上一镜的末帧整张叠到这一镜头上,出来的
    是**两个人叠在一起的重影**(实测项目 177 的 s02→s03 接缝:两个头、两身衣服)。
    这条链的"接不上"是靠状态图锚 + 提示词解决的,不靠后期刷。
    建项目时给按件镜打了 `no_wipe` 标记(见 projects.create_project_from_script)。
    """
    return _is_float_full(shot) and not (shot.options or {}).get("no_wipe")


# 「先穿好 → 让衣服自己飞走」拍法:模型在**第一帧**就照商品图把衣服画准了(这一步以前
# 总是不准,因为"衣服自己长到她身上"它画不出来);我们再**倒放**,成片里就变成
# "衣服从画外飞来、一落到底就是商品图那一件"。倒放后动作方向也正好对(往上飞 → 往下落)。
_REVERSE_MARKERS = ("自己从她身上脱开", "飘出画外", "飞出画外")


def _is_reverse_take(prompt: str | None) -> bool:
    return any(m in (prompt or "") for m in _REVERSE_MARKERS)


def write_clip(path, frames: list[Image.Image], size: tuple[int, int], fps: int, crf: str = "20") -> None:
    out = av.open(str(path), mode="w")
    st = out.add_stream("libx264", rate=fps)
    st.width, st.height = size
    st.pix_fmt = "yuv420p"
    st.options = {"crf": crf, "preset": "medium"}
    for im in frames:
        for pkt in st.encode(av.VideoFrame.from_image(im)):
            out.mux(pkt)
    for pkt in st.encode():
        out.mux(pkt)
    out.close()
    # 顺手留一张首帧图:画布节点默认只加载它,别让浏览器同时拉几十个视频
    if frames:
        write_thumb(path, frames[0])


def _close_partial(container, path) -> None:
    """收掉一个没写完的成片:关闭容器 + 把半个 mp4 删掉。

    成片是一边生成一边 mux 进文件的,所以任务失败/被终止时磁盘上会留下一个能播的
    「半成品」——它没有写进 project.final_rel_path,但会被当成成片误导人,必须清掉。
    """
    try:
        container.close()
    except Exception:  # noqa: BLE001
        pass
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


# ---------------------------------------------------------------- 接上一镜(换装镜 / 展示镜)
# 「悬浮变装」是一镜一镜往下演的:开衫镜开始时,她应该**已经穿着上一镜换好的长裤**。
# 但每一镜都是从同一套参考图从零生成的,模型手里只有:
#   ①起点 —— 打底(黑色细吊带背心 + 黑色短裤),写在提示词里;
#   ②终点 —— 人物参考图(那张图里她穿着一整套灰衣服)。
# **"演到一半"的中间态它没有图**,只靠文字压不住:实测"她开始时已经穿着长裤"这句会被
# "打底是黑色短裤"顶掉,开衫镜第一帧腿上是打底短裤、长裤整段丢失(绑不绑商品图都一样)。
# 所以干脆把**上一镜最后那一眼**当一张参考图直接喂进去 —— 她开始时要长什么样,给图看。
_PREV_DIR = STORAGE_DIR / "refs_prev"


def _prev_last_frame(project: Project, shot) -> str | None:
    """上一镜(同项目、idx-1)成片片段里的最后一帧,落成 png 返回路径。

    上一镜没出片(片段不存在)就返回 None —— 这时退回纯文字,不硬凑。
    """
    prev = next((s for s in project.shots if s.idx == shot.idx - 1), None)
    if prev is None or not prev.clip_rel_path:
        return None
    clip = STORAGE_DIR / prev.clip_rel_path
    if not clip.exists():
        return None
    try:
        frames = load_frames(clip)
    except Exception:  # noqa: BLE001 - 片段坏了就当没有
        return None
    if not frames:
        return None
    _PREV_DIR.mkdir(parents=True, exist_ok=True)
    dst = _PREV_DIR / f"p{project.id}_s{shot.idx:02d}.png"
    frames[-1].save(dst)
    return str(dst)


# 它总是**最后一张**参考图,所以编号就是 len(refs)。
# 光把上一镜的图塞进去还不够 —— 提示词里「人物参考图只用来锁定五官、发型和身材」
# 「不要照搬参考图里她的其他衣服」这些句子会让模型**把所有参考图里的"穿什么"都忽略掉**,
# 连这张新加的也一起忽略(实测:只加图、结果和没加一样)。所以必须同时给一条**优先级**:
# "开始时穿什么一律以 <Picture N> 为准,和文字对不上也以它为准"。
_PREV_DEF = (
    "\n<Picture {n}> 是**上一个镜头最后拍到的那个画面(实拍截帧,不是商品图)**。"
    "**这一镜开始时她身上穿的,一律以 <Picture {n}> 为准**:<Picture {n}> 里她穿着几件、什么样子,"
    "她开始时就是几件、什么样子 —— **哪怕文字说明里写了「打底」「黑色短裤」「赤脚」之类,"
    "只要和 <Picture {n}> 对不上,都以 <Picture {n}> 为准(下半身尤其如此)**;"
    "<Picture {n}> 里没有的(只有本镜头刚要穿上的那一件),才按本镜头的动作描述加上。"
    "(人物参考图 —— 前面那几张 —— 只用来锁定她的五官 / 发型 / 身材和面料细节,"
    "**不用来管她此刻已经穿到哪一步**) 。"
    "**她的大小和站位也照 <Picture {n}> 来**:<Picture {n}> 里她占画面多高、头顶离上沿多远、"
    "脚离下沿多远、人在不在画面正中 —— 本镜头就照这个样子,**不许放大、不许缩小、也不许上下挪**;"
    "**这两个镜头是硬切紧接着的,她必须一样大 —— 不然一眼就看出是两段拼的。**"
    "这一镜第一帧必须和她 <Picture {n}> 的样子对得上:不许少穿、不许换回打底、也不许改样子;"
    "这张图用来对齐她**开始时穿什么、长什么样、以及她有多大 / 站在哪儿**;"
    "**只有它的背景纹理和光线不要照搬**(背景仍按本镜头自己的说明生成)。"
)


def _add_prev_frame_def(prompt: str, n: int) -> str:
    """把「上一镜末帧」的图片说明补进 subject_definitions 末尾。"""
    line = _PREV_DEF.format(n=n)
    marker = "\n\nsummary:"
    if marker in prompt:
        return prompt.replace(marker, line + marker, 1)
    return prompt + line


# ---------------------------------------------------------------- 换装镜的「飞入动作」兜底
# 建项目时提示词会被 rule_condense 精简:动作只留前 3 句 + 结尾一句,
# 而「被扔进来的那一段」排在 detailed_description 第 4 段之后 → 整段被裁掉。
# 结果是 268/269 这些项目里,换装镜的提示词只剩"开始时穿什么 / 结束时穿什么",
# 模型只能硬切(用户报"衣服扔的效果都没有了")。这里按提示词里已有的件名补回一段。
_CHANGE_HINTS = ("扔", "弧线", "飞向她", "飞向", "飞过来", "飘进来", "平移过来", "递过去")
_CHANGE_PART = {"top": "上半身", "outer": "上半身", "bottom": "下半身", "whole": "身上"}


def _ensure_change_action(prompt: str) -> str:
    """换装镜提示词里没有「衣服怎么飞进来」时,补一段标准动作(幂等)。"""
    if any(k in prompt for k in _CHANGE_HINTS):
        return prompt
    m = re.search(r"在「([^」]+)」碰到", prompt)
    if not m:
        return prompt
    name = m.group(1)
    from .piece_prompt import kind_of

    kind = kind_of(name)
    part = _CHANGE_PART.get(kind)
    if not part:
        return prompt
    q = "条" if kind == "bottom" else "件"
    corner = "右下角" if kind == "bottom" else "右上角"
    # 旧件在"贴上之前"必须一直完整穿着 —— 模型很容易演成先脱旧的、再等新的飞过来。
    # 用「之前 / 之后」两个状态 + 「中间没有第三个状态」来说(实测比"不许脱"管用)。
    before_m = re.search(r"开始时[^。]*?穿的是完整的「([^」]+)」", prompt)
    olds = re.findall(r"「([^」]+)」那一[件条双]", prompt)
    state_line = ""
    if before_m:
        before = before_m.group(1)
        before_items = [x.strip() for x in re.split(r"[、,，]", before) if x.strip()]
        after_items = [x for x in before_items if x not in olds]
        if name not in after_items:
            after_items.append(name)
        if olds:
            old_txt = "」「".join(olds)
            state_line = (
                f"\n这一段只有两个状态:贴上去之前,她身上是完整的「{before}」,"
                f"「{name}」还在画面外 —— 「{old_txt}」一直穿着、完整、实心;"
                f"贴上去之后,「{old_txt}」立刻从画面里消失,"
                f"她身上是「{'、'.join(after_items)}」。"
                "两个状态之间不许出现脱衣、半脱、变透明或身上空出一块的中间状态。\n"
            )
        else:
            # 「加穿」型(外套叠穿):没有任何旧件会消失,别把"消失"写进去
            state_line = (
                f"\n这一段只有两个状态:贴上去之前,她身上是完整的「{before}」,"
                f"「{name}」还在画面外;贴上去之后,她身上是「{'、'.join(after_items)}」"
                f"—— 原来那几件一件都没少、也没变样,只是外面多穿了这件「{name}」。"
                "两个状态之间不许出现脱衣、半脱、变透明或身上空出一块的中间状态。\n"
            )
    line = (
        f"\n这一镜要做的事:像有人从画面外把这{q}「{name}」朝她扔过去那样 —— "
        f"它从画面的{corner}外侧被扔出来,飞快地划一道弧线飞向她的{part},"
        f"碰到她身体的那一瞬间就已经整{q}穿好了(完全服帖、一次到位)。"
        "速度要快,像被扔出去的东西 —— 不许慢慢飘、不许在空中悬停、不许停在半路。 "
        f"贴上去的那一下,这{q}「{name}」就已经整{q}穿在她身上了(像贴纸一下贴好、直接成型)。\n"
        + state_line
    )
    # 插在「结束时…」/「机位固定…」之前,保持原来的收尾顺序
    cut = len(prompt)
    for marker in ("结束时", "机位固定"):
        i = prompt.find(marker)
        if 0 <= i < cut:
            cut = i
    return prompt[:cut].rstrip() + "\n" + line + prompt[cut:]


# ---------------------------------------------------------------- 通用 / 自定义分镜的「穿着样式参照」
# 通用分镜(半身补妆、化妆师、自拍…)是各自独立生成的,手里只有商品图和人物图 ——
# 衣服到底怎么穿(内搭扎不扎进裙腰、开衫敞不敞开)每镜都在重新猜,
# 成片里就会出现"一会儿扎进去、一会儿放出来"。
# 这里把**换装链最后一镜的尾帧**当一张参考图塞进去,并明确它只管"怎么穿"、不管取景。
_STYLE_DIR = STORAGE_DIR / "refs_style"

_STYLE_DEF = (
    "- <Picture {n}> 是她整套换装**全部完成之后**的样子,用来对齐**穿着方式**:"
    "内搭下摆是扎进裙腰还是放出来、开衫是扣着还是敞着、袖口和裤脚到哪儿,都以它为准。"
    "**但每件衣服的穿法 / 开合,以本镜【道具定义】里的那一条为准**"
    "(那里写扣上就全程扣着,写敞开才敞开);不要按「开衫通常敞开」的印象自行改,"
    "也不要给她另加一件内搭 / 吊带 / 打底衫 —— 她身上只有【道具定义】列出的衣服和配饰。"
    "**这张图只用来对齐穿着方式**:景别、机位、她在画面里的大小和站位、背景,"
    "一律按本镜头自己的说明生成,不要照搬 <Picture {n}> 的构图。"
)


def _insert_into_person(prompt: str, line: str) -> str:
    """把一条人物相关的定义并进【人物】段末尾(没有【人物】段才退回放到最前面)。"""
    return insert_into_person(prompt, line)


def _add_style_ref_def(prompt: str, n: int) -> str:
    """把「穿着方式」的图片说明并进【人物】段,并把里面对这张图的编号纠正成实际编号。

    用户 2026-10-22:提示词里不能出现「那张参考图」这种没有 <Picture N> 的悬空指代 ——
    编号由这张图在 refs 里的实际位置决定,所以建项目时先按预算写一个,这里再校一遍。
    静态风格的【人物】段已经把「认人 + 服装 + 穿搭」写全了,这时只校编号、不重复追加。
    """
    prompt = re.sub(
        r"<Picture \d+>(?=\(整套换装完成后的样子\))", f"<Picture {n}>", prompt,
    )
    if "整套换装完成后的样子" in prompt:
        return prompt
    if "是她整套换装" in prompt:
        return re.sub(r"<Picture \d+> 是她整套换装", f"<Picture {n}> 是她整套换装", prompt)
    return _insert_into_person(prompt, _STYLE_DEF.format(n=n).strip())


def _drop_style_pic_ref(prompt: str) -> str:
    """这张图这次没挂上(静态换装还没出图):把编号指代退回文字描述,免得参考图对不上号。"""
    return re.sub(
        r"<Picture \d+>(?=\(整套换装完成后的样子\))",
        "「整套换装完成后的样子」那张图",
        prompt,
    )


# 人物头像:整片每一镜都挂,只锁头部(脸型 / 五官 / 发型 / 发色 / 肤色),不参与构图和姿势。
_HEAD_DEF = (
    "\n<Picture {n}> 是人物头部的标准参考图。**本镜她的头 / 脸一律以 <Picture {n}> 为准**:"
    "脸型、五官、发型、发长、发色、肤色都和 <Picture {n}> 完全一致,不要换成别人。"
    "这张图只取**脸和发型**;如果它是正面 / 侧面 / 背面拼在一起的多格图,"
    "不要照搬拼图排布。图里裸露的肩颈只是头像素材的拍法,不代表本镜也裸露:"
    "不要照搬裸露的皮肤,也不要因此给她加吊带 / 背心 / 打底衫 —— 上身穿什么只看本镜【道具定义】。"
    "这张图**只用来对齐头部**:身体姿势、服装、场景、景别、机位和她在画面里的大小,"
    "一律按本镜头自己的说明生成,不要照搬 <Picture {n}> 的构图和姿势。"
)


def _add_head_def(prompt: str, n: int) -> str:
    return _insert_into_person(prompt, "- " + _HEAD_DEF.format(n=n).strip())


def _style_ref_for(project: Project, shot) -> str | None:
    """这一镜该喂的「穿着方式参照」图。

    用户 2026-10-22:不做任何处理 —— 不缩放、不裁切、不放大,直接把原图给模型。
    """
    return _chain_tail_frame(project, shot)


def _chain_tail_frame(project: Project, shot) -> str | None:
    """换装链最后一镜的「结束样子」,给通用 / 自定义分镜当穿着样式参照。

    优先取那一镜真实成片的最后一帧(用户要的"换装最后一个视频的尾帧");
    还没出片就用它该有的结束状态图(state_out / anchor_last)。
    """
    chain = [
        s for s in project.shots
        if s.idx < shot.idx
        and not (s.options or {}).get("universal")
        and ((s.options or {}).get("state_out") or (s.options or {}).get("anchor_last")
             # 一镜整套换装(options.whole_look):它的最后一帧就是「整套穿好」的样子
             or (s.options or {}).get("whole_look"))
    ]
    if not chain:
        return None
    # 从链尾往前找:最后一镜还没出片 / 状态图也还没落盘时,退到前一个"看得见她穿到哪一步"的画面
    for last in sorted(chain, key=lambda s: s.idx, reverse=True):
        # 静态图风格:换装成片被取景压过,高清原图存在 style_source 里,优先用它
        _hires = str((last.options or {}).get("style_source") or "")
        if _hires and os.path.exists(_hires):
            return _hires
        if last.clip_rel_path:
            clip = STORAGE_DIR / last.clip_rel_path
            if clip.exists():
                # 静态图分镜:clip 本身就是一张图,直接用它当「穿着样式参照」,不用抽帧
                if clip.suffix.lower() in (".png", ".jpg", ".jpeg", ".webp"):
                    return str(clip)
                _STYLE_DIR.mkdir(parents=True, exist_ok=True)
                dst = _STYLE_DIR / f"p{project.id}_s{last.idx:02d}_style.png"
                try:
                    # 片段重出过就重新抽,别拿旧帧
                    if not dst.exists() or dst.stat().st_mtime < clip.stat().st_mtime:
                        frames = load_frames(clip)
                        if frames:
                            frames[-1].save(dst)
                except Exception:  # noqa: BLE001 - 抽帧失败就走下面的状态图
                    pass
                if dst.exists():
                    return str(dst)

        for key in ("state_out", "anchor_last"):
            path = (last.options or {}).get(key)
            if path and os.path.exists(path):
                return str(path)
    return None


def compose_prompt(db: Session, project: Project, shot):
    """拼出真正送进模型前的提示词。

    生成和「查看最终文案」共用同一段逻辑,保证看到的和实际发出去的一字不差:
    机位子句注入 + 全身镜自动附上一镜末帧的图片说明。
    返回 (提示词, 参考图列表, 自动附上的上一镜末帧路径)。
    """
    options = shot.options or {}
    refs = resolve_refs(db, shot)
    base_prompt = shot.prompt or ""
    # 换装镜(首尾锚都有)如果被精简得连"飞入动作"都没了,这里补回去
    if options.get("anchor0") and options.get("anchor_last") and not options.get("universal"):
        base_prompt = _ensure_change_action(base_prompt)
    prompt = apply_camera(base_prompt, options.get("camera"))
    prev_frame = None
    if (settings.chain_anchor and not options.get("anchor0") and shot.idx > 1
            and is_full_body_shot(prompt) and not options.get("universal")):
        prev_shot = next((s for s in project.shots if s.idx == shot.idx - 1), None)
        if prev_shot is not None and is_full_body_shot(prev_shot.prompt):
            prev_frame = _prev_last_frame(project, shot)
            if prev_frame:
                refs = refs + [prev_frame]
                prompt = _add_prev_frame_def(prompt, len(refs))
    # 人物头像:整片每一镜都挂,只锁头部;建项目时已挂进 refs 的话这里只补定义,不重复追加。
    # (放在「穿着方式」前面:两者都属于【人物】定义,先认人、再对齐穿法。)
    head = _ref_path(db, getattr(project, "head_ref", "") or "")
    if head:
        _norm = os.path.normpath(head)
        _hit = [
            i + 1 for i, r in enumerate(refs)
            if os.path.normpath(str(r)) == _norm
        ]
        if _hit:
            if "人物头部的标准参考图" not in prompt:
                prompt = _add_head_def(prompt, _hit[0])
        else:
            refs = refs + [head]
            prompt = _add_head_def(prompt, len(refs))
    # 通用 / 自定义分镜:各自独立,不拿上一镜当首帧;但**穿着方式**要跟换装链最后一镜对齐
    # (内搭扎不扎进裙腰这种事,只靠文字每镜都会重新猜)。
    # 静态图风格也走这里(用户 2026-10-22):静态成片只当**穿着方式参考图**,
    # 不再硬锚成首帧 —— 锚成首帧会让镜头内突然推近、按文字重画一套衣服。
    if (options.get("universal") and not options.get("anchor0")):
        style_ref = _style_ref_for(project, shot)
        if not style_ref:
            # 项目里没有换装链(比如剧情片项目):整套换装成片是建项目时直接挂进 refs 的,
            # 按 options.style_ref 取那一张,别把提示词里的 <Picture N> 退回成文字。
            _sn = int(options.get("style_ref") or 0)
            if 0 < _sn <= len(refs):
                style_ref = str(refs[_sn - 1])
        if style_ref:
            # 建项目时这张图已经挂进 refs 了(前端要靠它渲染缩略图),这里别再追加一次
            _hit = [
                i + 1 for i, r in enumerate(refs)
                if os.path.normpath(str(r)) == os.path.normpath(style_ref)
            ]
            if _hit:
                prompt = _add_style_ref_def(prompt, _hit[0])
            else:
                refs = refs + [style_ref]
                prompt = _add_style_ref_def(prompt, len(refs))
        else:
            # 图还没出(静态链没跑完):提示词里的编号指代先退回文字,别指向不存在的图
            prompt = _drop_style_pic_ref(prompt)
    return prompt, refs, prev_frame


def _generate_shot(db: Session, project: Project, job: RenderJob, shot, variant: int = 0,
                   seed: int | None = None) -> list[Image.Image]:
    # 抽卡:同一个任务里连出多张时,每张用各自的输出前缀,免得 comfy.newest_output 认错文件
    prefix = f"w{job.id}_{shot.key}" + (f"_v{variant}" if variant else "")
    options = shot.options or {}
    # 多角度机位 + 自动附加的上一镜末帧都在这段里拼好(和「查看最终文案」同一份逻辑)
    prompt, refs, prev_frame = compose_prompt(db, project, shot)
    # 换装镜 / 全身展示镜:额外喂一张「上一镜最后一帧」当参考图(见上面说明)。
    anchor_frame0 = None
    anchor_frame_last = None
    mid_anchor = None
    # 换装链镜头可以在 options 里直接指定首尾硬锚(状态图),优先级高于自动取「上一镜末帧」
    if settings.chain_anchor and options.get("anchor0"):
        anchor_frame0 = options["anchor0"] or None
        anchor_frame_last = options.get("anchor_last") or None
        # 锚图还没落盘(比如前面的展示镜没跑成)就**别硬塞** —— 塞了 ComfyUI 读不到图会直接报错,
        # 整个任务挂掉;宁可退化成"没有锚"。
        if anchor_frame0 and not os.path.exists(anchor_frame0):
            print(f"[pipeline] 镜头 {shot.key} 首帧锚不存在,忽略:{anchor_frame0}", flush=True)
            anchor_frame0 = None
        if anchor_frame_last and not os.path.exists(anchor_frame_last):
            print(f"[pipeline] 镜头 {shot.key} 尾帧锚不存在,忽略:{anchor_frame_last}", flush=True)
            anchor_frame_last = None
        print(f"[pipeline] 镜头 {shot.key} 使用换装链首尾硬锚"
              f"(首 {'有' if anchor_frame0 else '无'} / 尾 {'有' if anchor_frame_last else '无'})",
              flush=True)
        # 可选的「中间帧路标」:[[图片路径, 第几帧], ...]。用来钉死飞入衣服的尺寸/位置。
        # 支持两种写法:单条 [path, idx] 或多条 [[path, idx], ...]。
        ma = options.get("mid_anchor")
        if isinstance(ma, (list, tuple)) and ma:
            if len(ma) == 2 and not isinstance(ma[0], (list, tuple)):
                mid_anchor = [(str(ma[0]), int(ma[1]))]
            else:
                mid_anchor = [(str(p), int(i)) for p, i in ma if p]
            if mid_anchor:
                print(f"[pipeline] 镜头 {shot.key} 中间帧路标 {len(mid_anchor)} 条:"
                      f"{[i for _, i in mid_anchor]}", flush=True)
    elif settings.chain_anchor and prev_frame:
        # 只有换装链上的全身镜才拿上一镜末帧当锚(判断在 compose_prompt 里);
        # 同时把它硬锚在第 0 帧,人物从上一镜一路延续,不会被重编。
        anchor_frame0 = prev_frame
        print(f"[pipeline] 镜头 {shot.key} 附加「上一镜末帧」参考图:<Picture {len(refs)}>"
              f"(并硬锚定第 0 帧)", flush=True)
    # 静态图风格:换装链后面接的通用 / 自定义分镜不再把最后那张静态成片**硬锚到第 0 帧**
    # (用户 2026-10-22)—— 那张图只作为穿着方式参考图,在 compose_prompt 里按 <Picture N> 塞进 refs。
    # 硬锚会让镜头一开始就是静态构图,模型随后按文字推近 / 重画衣服。
    mode = shot.mode
    if mode in (MODE_FIRST, MODE_FIRST_LAST) and refs:
        first, last = refs[0], (refs[1] if len(refs) > 1 else None)
    else:
        first = last = None
    if mode == MODE_REF and not refs and not anchor_frame0:
        # nothing to reference -> fall back to plain image-to-video
        mode = "text"

    gw, gh = _gen_size(project)
    # 参考图档位:物体镜(衣服要抛起 / 飞入 / 悬浮)走保真档 —— 衣服腾空时糊了最看得出来;
    # 其余普通镜走快档(实测快一倍,成品肉眼基本没差)。两个档位都能在 .env 里改。
    object_shot = is_object_shot(shot.prompt, bool((shot.options or {}).get("float")), shot.idx)
    ref_size = settings.ref_image_size if object_shot else settings.ref_image_size_fast
    print(f"[pipeline] 镜头 {shot.key} 参考图档位={ref_size}"
          f"({'物体镜' if object_shot else '普通镜'})", flush=True)
    # 通用分镜即使不做飞入动作,鞋类商品图(俯视一双鞋占满整幅)也得缩小留白,
    # 否则模型会把它整幅抄进画面 —— 实测 teset999 #15 脚下凭空多出两只巨大拖鞋。
    shoe_scale_only = (not object_shot) and bool((shot.options or {}).get("universal"))
    if object_shot or shoe_scale_only:
        # 悬浮变装:参考图统一缩小留白,否则模型会照抄"人物/衣服占满画幅"的构图
        # —— 但首尾帧状态图(锚图)本身就是带留白的完整构图,再缩小留白的话,
        #    模型会把「米色画布 + 内嵌小图」的构图抄进视频(画面出现两层背景),
        #    所以锚图保持原样、不参与缩放
        skip_scale = {os.path.normpath(p) for p in (anchor_frame0, anchor_frame_last) if p}
        # 「换装完成后的穿搭参考图」(链后通用分镜用)= 整幅人物构图,不能缩小留白 ——
        # 缩了模型会抄成「米色画布 + 内嵌小图」的两层背景。
        _look_path = str((shot.options or {}).get("look_path") or "")
        if _look_path:
            skip_scale.add(os.path.normpath(_look_path))
        # 「穿着样式参照」(换装链最后一镜的尾帧)本身就是整幅人物构图,也不能再缩小留白。
        if (shot.options or {}).get("universal") and not (shot.options or {}).get("anchor0"):
            _style_path = _style_ref_for(project, shot)
            if _style_path:
                skip_scale.add(os.path.normpath(_style_path))
        # 「人物背景图」是整幅实景,不能缩小留白 —— 否则模型会照抄成「画布 + 内嵌小图」两层背景。
        _bg_ref = str((shot.options or {}).get("bg_ref") or "")
        _bg_path = _ref_path(db, _bg_ref) if _bg_ref else None
        if _bg_path:
            skip_scale.add(os.path.normpath(_bg_path))
        # 「人物头像」要保持原始画幅(缩了脸就糊),也不参与缩小留白。
        _head_path = _ref_path(db, getattr(project, "head_ref", "") or "")
        if _head_path:
            skip_scale.add(os.path.normpath(_head_path))
        scale_of = _ref_scales(db, shot)
        scales = []
        for r in refs:
            sc = scale_of.get(os.path.normpath(r), settings.float_ref_scale)
            # 普通通用分镜只缩鞋 / 小件配饰,别的参考图维持原样(避免把人物/衣服也改小)
            if shoe_scale_only and sc not in (settings.float_ref_scale_shoe,
                                               settings.float_ref_scale_small):
                sc = 1.0
            scales.append(sc)
        scaled = _scaled_refs(refs, scales)
        refs = ([r if os.path.normpath(r) in skip_scale else s for r, s in zip(refs, scaled)]
                if skip_scale else scaled)
    _seed = int(seed if seed is not None
                else (options.get("seed") or DEFAULT_SEED))
    _gen_len = int(options.get("gen_length") or settings.gen_length)
    wf = build_workflow(
        mode=mode,
        prompt=prompt,
        # 抽卡时由调用方逐张传进随机 seed;没传才退回 options 里存的 / 全局定稿种子。
        # 以前这里是「9000 + 项目号×100 + 镜号」→ 整片生成和单镜重出用的不是同一个种子
        # (项目 313:s01 整片 83.6%、单镜重出 69.1%,用户 2026-10-20 要求统一)。
        seed=_seed,
        prefix=prefix,
        length=_gen_len,
        width=gw,
        height=gh,
        fps=project.fps,
        ref_images=refs if mode == MODE_REF else None,
        first_frame=first,
        last_frame=last,
        ref_image_size=ref_size,
        anchor_frame0=anchor_frame0 if mode == MODE_REF else None,
        anchor_frame_last=anchor_frame_last if mode == MODE_REF else None,
        mid_anchor=mid_anchor if mode == MODE_REF else None,
        # 链后通用分镜:最终穿搭参考图用单独的加载节点(id 9,标题「最终穿搭参考(尾帧)」)
        look_image=str(options.get("look_path") or "") or None,
    )

    def _submit_and_wait() -> None:
        pid = comfy.submit(wf)
        comfy.wait(pid, on_tick=lambda _sec: _beat(job.id))

    try:
        _submit_and_wait()
    except comfy.ComfyError as exc:
        # 16GB 卡 + 本机 ComfyUI 上偶发跑挂:读模型分片失败(hostbuf_file_reader_read /
        # HostBuffer.read_file_slice)、采样/解码时 CUDA 抖动(Fault failed / out of memory),
        # 或者 ComfyUI 本身正在重启(连接被拒)。等它就绪后原样重试几次 —— 实测基本都能过。
        markers = ("hostbuf", "read_file_slice", "fault failed", "out of memory", "cuda error",
                   "无法连接", "urlopen", "已丢失")
        if not any(m in str(exc).lower() for m in markers):
            raise
        last: Exception = exc
        for _ in range(3):
            print(f"[pipeline] 镜头 {shot.key} 生成中断({str(last)[:70]}),"
                  f"等 ComfyUI 就绪后重试", flush=True)
            comfy.free_memory()
            for _ in range(20):                      # ComfyUI 可能在重启,等它回来
                if comfy.is_up():
                    break
                time.sleep(3)
            try:
                _submit_and_wait()
                last = None                          # type: ignore[assignment]
                break
            except comfy.ComfyError as exc2:
                last = exc2
        if last is not None:
            raise last
    clip = comfy.newest_output(prefix)
    if clip is None:
        raise comfy.ComfyError(f"镜头 {shot.key} 生成后未找到输出文件")
    # 第二遍 v2v 细节修复(锐化 LoRA 的官方用法):默认跟随 .env,
    # 单镜可以用 options.detail_pass=0 关掉;失败会自动退回原片。
    _want_detail = (settings.detail_pass if options.get("detail_pass") is None
                    else bool(options.get("detail_pass")))
    if _want_detail:
        clip = _detail_pass_clip(
            clip, shot, width=gw, height=gh, length=_gen_len, seed=_seed,
            fps=project.fps, on_tick=lambda _sec: _beat(job.id))
    frames = load_frames(clip)
    if (settings.float_reverse_take and _is_reverse_take(shot.prompt)
            and not (shot.options or {}).get("no_reverse")):
        # 「先穿好、再让衣服自己飞走」拍法:模型只在第一帧照商品图把衣服画准,
        # 我们倒放 → 成片里就是"衣服从画外飞来、一落到底就是商品图那一件"。
        # **「扯上身」风格必须关掉**(建项目时打了 no_reverse):它本来就要"先穿着 → 甩掉",
        # 倒放之后变成"一开场就只剩内搭"(实测 p306/p307 一直不对的真正原因)。
        frames = frames[::-1]
        print(f"[pipeline] 镜头 {shot.key} 按「倒放」处理(衣服自己飞走的拍法)", flush=True)
    return frames


def _generate_shot_for_stance(db: Session, project: Project, job: RenderJob, shot) -> list[Image.Image]:
    """要产状态图的展示镜:自动多抽几张,挑「脚最正」的那张。

    对**所有** state_only 展示镜生效 —— 链头(打底)、替换型(半裙/背心)、加穿型(开衫)。
    替换型虽然有首帧锚,但模型在中途/末帧还是会把脚画歪(实测 S1 左脚 16.9°、S2 右脚 17.9°),
    所以每一张状态图都要抽检。评分见 _stance_score:0° = 两只脚都正对镜头。
    """
    opts = shot.options or {}
    if not settings.stance_auto or not opts.get("state_only"):
        return _generate_shot(db, project, job, shot)

    first = bool(opts.get("state_first"))
    want = max(1, int(settings.stance_candidates))
    best_frames: list[Image.Image] | None = None
    best_score: float | None = None
    for i in range(want):
        seed = random.randint(1, 2 ** 31 - 1)
        frames = _generate_shot(db, project, job, shot, variant=i, seed=seed)
        score = _stance_score(frames[0] if first else frames[-1])
        print(f"[pipeline] 站姿评分 {shot.key} 第 {i + 1}/{want} 张:"
              f"{'量不到' if score is None else f'{score:.1f}°'}", flush=True)
        if score is not None and (best_score is None or score < best_score):
            if best_frames is not None:
                del best_frames
            best_frames, best_score = frames, score
        else:
            del frames
        if best_score is not None and best_score <= settings.stance_max_deg:
            break

    if best_frames is None:
        # 量不到脚(比如画面里没有鞋),退回普通生成,别把这一镜跑挂
        return _generate_shot(db, project, job, shot)
    print(f"[pipeline] 站姿自动选择 {shot.key}:{best_score:.1f}°"
          f"(阈值 {settings.stance_max_deg:.0f}°)", flush=True)
    return best_frames


def _fail(db: Session, job_id: int, exc: Exception) -> None:
    """Finalise a job. If the user already pressed 终止生成 we keep 已终止 instead of 失败."""
    db.rollback()
    job = db.get(RenderJob, job_id)
    if job is None:
        return
    canceled = job.status == ST_CANCELED
    if not canceled:
        job.status = ST_FAILED
        job.error = str(exc)[:2000]
        job.message = "生成失败"
    else:
        job.message = "已终止"
    job.finished_at = utcnow()
    if job.shot_id:
        shot = db.get(ProjectShot, job.shot_id)
        if shot is not None:
            shot.status = job.status
    elif job.current_shot:
        # 整片任务:当时正在生成的那个分镜要收尾,否则会一直停在「生成中」
        (
            db.query(ProjectShot)
            .filter(
                ProjectShot.project_id == job.project_id,
                ProjectShot.key == job.current_shot,
            )
            .update({ProjectShot.status: ST_QUEUED if canceled else ST_FAILED})
        )
    if not canceled:
        project = db.get(Project, job.project_id)
        if project is not None:
            project.status = ST_FAILED
    else:
        # 断点续传:用户主动终止后,项目不能一直停在「生成中」(否则界面再也没法点继续生成)。
        # 已生成的镜头片段都留着,项目落到「已终止」,再点一次渲染就从没做完的那镜接着跑。
        project = db.get(Project, job.project_id)
        if project is not None and project.status == ST_RUNNING:
            project.status = ST_CANCELED
    db.commit()


def _run_shot_job(db: Session, project: Project, job: RenderJob) -> None:
    """Preview-render a single shot.

    默认只出 1 段。「抽卡」时 job.variants > 1:一次连出 N 段(每段换一个随机种子),
    全部存进 shot.candidates,并把刚抽的第一张设为当前片段,用户再从中挑一张。
    """
    shot = db.get(ProjectShot, job.shot_id) if job.shot_id else None
    if shot is None:
        _fail(db, job.id, comfy.ComfyError("镜头不存在"))
        return

    variants = max(1, int(job.variants or 1))
    job.status = ST_RUNNING
    job.started_at = utcnow()
    job.heartbeat_at = utcnow()
    job.progress = 5
    job.current_shot = shot.key
    job.message = (f"正在生成镜头 {shot.idx}({shot.key})" if variants == 1
                   else f"抽卡 镜头 {shot.idx}({shot.key})·共 {variants} 张")
    shot.status = ST_RUNNING
    db.commit()

    new_rel: list[str] = []
    # 抽卡的种子策略:默认「固定种子模式」—— 第 1 张就是界面上的默认种子,后面依次 +1,
    # 既复现得了、又不会每张一模一样;只有用户勾了「随机种子」才每张取随机数。
    _seed_mode = str((shot.options or {}).get("seed_mode") or "random")
    _base_seed = int((shot.options or {}).get("seed") or DEFAULT_SEED)
    try:
        for i in range(variants):
            if _is_canceled(db, job.id):
                raise Canceled()
            if variants > 1:
                if _seed_mode == "fixed":
                    vseed = _base_seed + i
                else:
                    # 每张都换一个**随机**种子 —— 同一种子出来的是同一段影像
                    vseed = random.randint(1, 2 ** 31 - 1)
                shot.options = {**(shot.options or {}), "seed": vseed}
                db.commit()
            else:
                # 单张:用 options 里的种子(用户在界面上指定的固定种子,或 render_shot 给的随机值),
                # 没有再兜底一个随机 —— 这样界面上填了种子就能精确复现同一条片子
                vseed = int((shot.options or {}).get("seed") or random.randint(1, 2 ** 31 - 1))
            variant = i + 1 if variants > 1 else 0
            if variants == 1:
                # 单镜重出也走「自动站姿挑选」:展示镜(链头/加穿型)自动挑脚最正的;
                # 抽卡(count>1)保持人工挑选,不多抽。
                frames = _generate_shot_for_stance(db, project, job, shot)
            else:
                frames = _generate_shot(db, project, job, shot, variant=variant, seed=vseed)
            seg_raw = segment(frames, shot.duration_frames, shot.options)
            # 先统一取景(70% 高 / 头顶 15% / 背景刷白),状态图也用这一版 ——
            # 锚图跟着 70% 走,后面换装镜就不用"先画大再缩小"。
            seg = _align_shot(seg_raw, shot)
            seg = _match_chain_frame(seg, shot)
            seg = _lock_lateral(seg, shot)
            seg = _lock_clip_tone(seg, shot.project_id)
            seg = _maybe_lock_feet(seg, shot)
            _save_state_image(shot, seg)
            clip_rel = (f"clips/p{project.id}_j{job.id}_{shot.key}.mp4" if variants == 1
                        else f"clips/p{project.id}_j{job.id}_{shot.key}_v{variant}.mp4")
            clip_file = STORAGE_DIR / clip_rel
            write_clip(clip_file, seg, _gen_size(project), project.fps)
            up_file = _maybe_upscale_clip(clip_file, project)
            if up_file != clip_file:
                clip_rel = up_file.relative_to(STORAGE_DIR).as_posix()
            del frames, seg
            new_rel.append(clip_rel)
            # 每抽出一张就立刻记进候选并提交 —— 用户不用等整轮抽完,
            # 页面上就能一张张看到结果,边出边挑
            kept = [c for c in (shot.candidates or []) if c]
            merged = kept + [c for c in new_rel if c not in kept]
            if len(merged) > _MAX_CANDIDATES:   # 超上限的最老的删掉,免得磁盘越堆越多
                for old in merged[:-_MAX_CANDIDATES]:
                    try:
                        (STORAGE_DIR / old).unlink(missing_ok=True)
                    except OSError:
                        pass
                merged = merged[-_MAX_CANDIDATES:]
            shot.candidates = merged
            shot.clip_rel_path = new_rel[0]      # 默认先看刚抽的第一张,用户可再挑别的
            job.progress = min(95, int((i + 1) / variants * 95))
            job.message = ("镜头生成完成" if variants == 1 else f"抽卡 {i + 1}/{variants} 完成")
            db.commit()

        shot.status = ST_DONE
        job.out_rel_path = shot.clip_rel_path
        job.progress = 100
        job.status = ST_DONE
        job.message = "镜头生成完成"
        job.finished_at = utcnow()
        # 单镜任务不碰项目状态:项目「完成」只能由整片任务在真的合成出成片时才给。
        # (以前这里会把 failed 翻成 done —— 整片失败后随便补一个镜头,项目就假「完成」了。)
        db.commit()
    except Canceled:
        if new_rel:      # 抽到一半被终止:已经抽出来的几张留着当候选,别白丢
            kept = [c for c in (shot.candidates or []) if c]
            shot.candidates = (kept + [c for c in new_rel if c not in kept])[-_MAX_CANDIDATES:]
            shot.clip_rel_path = shot.clip_rel_path or new_rel[0]
            db.commit()
        _fail(db, job.id, Canceled("用户终止"))
    except Exception as exc:  # noqa: BLE001
        _fail(db, job.id, exc)


def _run_film_job(db: Session, project: Project, job: RenderJob) -> None:
    """Render every enabled shot and assemble the film at the project's out size/fps."""
    shots = [s for s in project.shots if s.enabled]
    # 进度分母只算**真的会进成片**的镜头 —— 「只产状态图」的展示镜不进成片(见下面 state_only),
    # 算进去的话进度条会卡在六成然后直接跳 100%。
    total = sum(s.duration_frames for s in shots
                if not (s.options or {}).get("state_only")) or 1
    out_size = (project.out_width, project.out_height)
    # 悬浮变装这个风格要求「零运镜 + 低饱和」,正好和成片增强的推镜/加饱和相反,
    # 所以整片都是 float 镜头时,后期增强全部跳过。
    float_project = bool(shots) and all((s.options or {}).get("float") for s in shots)
    push_on = settings.film_push and not float_project
    # 暗角遮罩按"生成分辨率"做(调色在放大之前做,省 3 倍算力,效果一样)
    vignette = (
        _vignette_mask(_gen_size(project))
        if (settings.film_grade and not float_project)
        else None
    )

    job.status = ST_RUNNING
    job.started_at = utcnow()
    job.heartbeat_at = utcnow()
    job.progress = 1
    job.message = f"共 {len(shots)} 个镜头,开始生成"
    project.status = ST_RUNNING
    db.commit()

    final_path = RENDER_DIR / f"project_{project.id}_job_{job.id}.mp4"
    container = av.open(str(final_path), mode="w")
    stream = container.add_stream("libx264", rate=project.fps)
    stream.width, stream.height = project.out_width, project.out_height
    stream.pix_fmt = "yuv420p"
    stream.options = {"crf": "19", "preset": "medium"}

    done = 0
    prev_last: Image.Image | None = None       # 上一镜的末帧(用来给下一镜做"刷过去")
    prev_full = False                          # 上一镜是不是悬浮变装的全身镜
    try:
        for shot in shots:
            if _is_canceled(db, job.id):
                raise Canceled()

            job.current_shot = shot.key
            shot.status = ST_RUNNING

            # 展示镜(只产状态图、不进成片)唯一的用途就是给换装镜当首/尾帧锚。
            # 首尾帧硬锚关掉之后它什么也不产出,白跑一镜 —— 直接跳过(2026-10-21 用户要求)。
            if (shot.options or {}).get("state_only") and not settings.chain_anchor:
                shot.status = ST_DONE
                job.message = f"镜头 {shot.key} 展示镜跳过(不需要状态图锚)"
                db.commit()
                continue

            # 已经有片段的镜头直接复用:提示词/分辨率/帧率被改动时片段会被清掉,
            # 所以"还有片段"就等于"这段还能用"。这样渲染被打断后重跑,只补没做完的镜头。
            seg = None
            reused = False
            if shot.clip_rel_path:
                clip_path = STORAGE_DIR / shot.clip_rel_path
                if clip_path.exists():
                    try:
                        seg = segment(load_frames(clip_path), shot.duration_frames, None)
                        reused = True
                    except Exception:  # noqa: BLE001 - 片段坏了就重新生成
                        seg = None

            if seg is None:
                job.message = f"正在生成镜头 {shot.idx}/{len(shots)}({shot.key})"
                db.commit()
                frames = _generate_shot_for_stance(db, project, job, shot)
                seg = segment(frames, shot.duration_frames, shot.options)
                del frames

            # 统一取景后再落盘/落状态图 —— 缓存片段也过一遍(已在目标附近的只会刷白背景)
            seg = _align_shot(seg, shot)
            seg = _match_chain_frame(seg, shot)
            seg = _lock_lateral(seg, shot)
            seg = _lock_clip_tone(seg, shot.project_id)
            seg = _maybe_lock_feet(seg, shot)

            if not reused:
                clip_rel = f"clips/p{project.id}_j{job.id}_{shot.key}.mp4"
                clip_file = STORAGE_DIR / clip_rel
                write_clip(clip_file, seg, _gen_size(project), project.fps)
            else:
                # 复用已有片段时 clip_rel / clip_file 也必须补上 ——
                # 否则下面 _maybe_upscale_clip(clip_file) 会 UnboundLocalError
                clip_rel = shot.clip_rel_path
                clip_file = STORAGE_DIR / clip_rel
            up_file = _maybe_upscale_clip(clip_file, project)
            if up_file != clip_file:
                clip_rel = up_file.relative_to(STORAGE_DIR).as_posix()
            shot.clip_rel_path = clip_rel

            # 状态图:这一镜(已归一)的末帧落盘,给后面的换装镜当尾帧硬锚。
            # 缓存片段也补一次 —— 状态图丢了能自己长回来(换装镜的锚不会悬空)。
            _save_state_image(shot, seg)

            # 「只产状态图」的镜头(按件排镜里的展示镜):它存在的唯一意义就是**给下一镜当锚**,
            # 末帧已经落成状态图了 —— 这一镜本身**不进成片**(成片只由那几段换装镜组成)。
            if (shot.options or {}).get("state_only"):
                shot.status = ST_DONE
                job.heartbeat_at = utcnow()
                job.message = f"镜头 {shot.key} 状态图已落盘(不进成片)"
                db.commit()
                del seg
                continue

            shot.status = ST_DONE
            db.commit()

            cur_full = _wants_wipe(shot)
            if settings.film_wipe and cur_full and prev_full and prev_last is not None:
                seg = _apply_wipe(seg, prev_last, settings.film_wipe_frames,
                                  settings.film_wipe_soft)
                print(f"[pipeline] 镜头 {shot.key} 从上一镜「刷过去」"
                      f"({settings.film_wipe_frames} 帧)", flush=True)
            prev_full = cur_full

            seg = _resample(seg, settings.film_speed)      # 整片变速(可选,>1 变快)
            last = len(seg) - 1
            for i, im in enumerate(seg):
                if push_on:
                    im = _push(im, i / last if last else 0.0)
                if vignette is not None:
                    im = _grade(im, vignette)
                for pkt in stream.encode(av.VideoFrame.from_image(_upscale(im, out_size))):
                    container.mux(pkt)
            prev_last = seg[-1].copy() if seg else None      # 给下一镜的"刷过去"用
            del seg

            if settings.film_flash:                        # 镜头之间的闪白(可选)
                white = Image.new("RGB", out_size, (255, 255, 255))
                for _ in range(max(1, settings.film_flash_frames)):
                    for pkt in stream.encode(av.VideoFrame.from_image(white)):
                        container.mux(pkt)

            done += shot.duration_frames
            job.progress = min(95, int(done / total * 95))
            job.heartbeat_at = utcnow()
            job.message = f"镜头 {shot.key} 完成({len(shots)} 个中第 {shot.idx} 个)"
            db.commit()

        for pkt in stream.encode():
            container.mux(pkt)
        container.close()

        job.out_rel_path = f"renders/{final_path.name}"
        project.final_rel_path = job.out_rel_path
        project.status = ST_DONE
        job.status = ST_DONE
        job.progress = 100
        job.message = "成片完成"
        job.finished_at = utcnow()
        db.commit()
    except Canceled:
        _close_partial(container, final_path)
        _fail(db, job.id, Canceled("用户终止"))
        return
    except Exception as exc:  # noqa: BLE001
        _close_partial(container, final_path)
        _fail(db, job.id, exc)


def _resolve_asset_path(db: Session, ref: str) -> Path | None:
    """把 "asset:N" / "file:<path>" 解析成本地绝对路径;解析不到返回 None。"""
    ref = (ref or "").strip()
    if not ref:
        return None
    if ref.startswith("asset:"):
        try:
            a = db.get(Asset, int(ref.split(":", 1)[1]))
        except (ValueError, TypeError):
            return None
        if a is None:
            return None
        p = STORAGE_DIR / a.rel_path
        return p if p.exists() else None
    if ref.startswith("file:"):
        p = Path(ref[5:])
        return p if p.exists() else None
    return None


def _fit_full_body_frame(
    path: Path,
    size: tuple[int, int] | None = None,
    ratio: float = 0.70,
    margin: float = 0.15,
    force: bool = False,
) -> None:
    """把静态图重新取景成「人物占画面高度约 70%,头顶 / 脚底各留约 15% 白边」,
    画布尺寸取**视频生成分辨率**(``size``,即 derive_gen_size 的结果),
    这样静态成片和视频分镜是同一个长宽比,拼接 / 预览 / 历史记录里都一致(用户 2026-10-22)。

    imgedit 出图的构图通常是人物几乎顶满画框(头顶贴上沿、脚底贴下沿),
    跟视频分镜「全景 70% + 上下留白」的取景对不上。
    只做**抠出人物 + 白底重排**,不重画、不拉伸(等比缩放,水平居中)。
    """
    try:
        im = Image.open(path).convert("RGB")
    except Exception as exc:  # noqa: BLE001
        print(f"[static] 取景处理读图失败 {path}: {exc}", flush=True)
        return
    # 找人物外接框:先取四边中位数当背景色(生成出来的白底常带一点点暖灰/冷灰),
    # 再把「和背景色差得明显」的像素当主体 —— 直接拿纯白做差会把整张微偏色的背景都算进去。
    arr = np.asarray(im)
    h, w = arr.shape[:2]
    band = np.concatenate([
        arr[:8].reshape(-1, 3), arr[max(0, h - 8):].reshape(-1, 3),
        arr[:, :8].reshape(-1, 3), arr[:, max(0, w - 8):].reshape(-1, 3),
    ])
    bg = np.median(band, axis=0)
    dist = np.abs(arr.astype(np.int16) - bg.astype(np.int16)).max(axis=2)
    mask = dist > 26
    rows = np.where(mask.any(axis=1))[0]
    if rows.size == 0:
        print(f"[static] 取景处理:找不到人物轮廓,跳过 {path.name}", flush=True)
        return
    top, bottom = int(rows[0]), int(rows[-1]) + 1
    cols = np.where(mask[top:bottom].any(axis=0))[0]
    if cols.size == 0:
        return
    left, right = int(cols[0]), int(cols[-1]) + 1
    subj_h = bottom - top
    subj_w = right - left
    if subj_h <= 0 or subj_w <= 0:
        return
    W, H = size or (w, h)
    # 等比缩放:高度占 ratio;同时左右各留 6% 白边,太宽的姿势也不会顶到画框。
    scale = min(ratio * H / subj_h, (W * 0.88) / subj_w)
    # 已经是目标画布、人物比例也对了 → 不再折腾(反复重渲时越留越空)
    if not force and (w, h) == (W, H) and abs(scale - 1.0) <= 0.03:
        return
    pad = 4  # 留一点抗锯齿边,免得抠出来的人物边缘出现硬切口
    crop = im.crop((max(0, left - pad), max(0, top - pad),
                    min(w, right + pad), min(h, bottom + pad)))
    # imgedit 的底常常是暖灰 / 冷灰(240 上下),直接贴到白画布上会显出一块灰色矩形。
    # 从人物左边(或右边)那圈底色往里漫填:先填洋红再统一换成纯白 ——
    # PIL 的 floodfill 用白色去填纯白种子会直接返回,所以不能直接填白。
    _seed = None
    _mid = crop.height // 2
    for _x in list(range(0, max(1, crop.width // 2))) + list(range(crop.width - 1, crop.width // 2, -1)):
        try:
            if min(crop.getpixel((_x, _mid))) < 250:
                _seed = (_x, _mid)
                break
        except Exception:  # noqa: BLE001
            break
    if _seed is not None:
        try:
            ImageDraw.floodfill(crop, _seed, (255, 0, 255), thresh=60)
        except Exception:  # noqa: BLE001
            pass
    _carr = np.array(crop)
    _carr[(_carr[:, :, 0] == 255) & (_carr[:, :, 1] == 0) & (_carr[:, :, 2] == 255)] = 255
    crop = Image.fromarray(_carr)
    new_w = max(1, round(crop.width * scale))
    new_h = max(1, round(crop.height * scale))
    if (new_w, new_h) != crop.size:
        crop = crop.resize((new_w, new_h), Image.LANCZOS)
    canvas = Image.new("RGB", (W, H), (255, 255, 255))
    canvas.paste(crop, ((W - new_w) // 2, int(round(margin * H))))
    canvas.save(path)
    print(f"[static] 重新取景 {path.name}:{im.size} → {canvas.size}"
          f"(人物占高 {subj_h / im.height:.0%} → {ratio:.0%})", flush=True)


def _run_static_shot_job(db: Session, project: Project, job: RenderJob, shot) -> None:
    """静态图分镜的**单镜**生成 / 抽卡:出 1 张或 count 张 PNG,候选存进 shot.candidates。

    底图 = 上一件静态换装镜的 PNG(第 1 件用人物参考图);服装图 = options.garment_ref。
    之前单镜任务一律走视频流程,所以在静态镜上点「抽卡」会生成一段视频(用户 2026-10-22)。
    """
    from .routers.imgedit import run_imgedit  # 避免循环 import

    opts = shot.options or {}
    dest_dir = STORAGE_DIR / "static_shots" / f"p{project.id}"
    dest_dir.mkdir(parents=True, exist_ok=True)

    prev_key = (opts.get("prev_static") or "").strip()
    base_path: Path | None = None
    if prev_key:
        cand = dest_dir / f"{prev_key}.png"
        if cand.exists():
            base_path = cand
    if base_path is None:
        base_path = _resolve_asset_path(db, opts.get("character_ref") or "")
    if base_path is None:
        job.status = ST_FAILED
        job.error = f"镜头 {shot.key}:底图取不到"
        db.commit()
        return
    garment_ref = (opts.get("garment_ref") or "").strip()
    # 「整套换装」一镜多件:options.garment_refs = [ref1, ref2, ...]
    _garment_refs = [
        str(x).strip() for x in (opts.get("garment_refs") or []) if str(x).strip()
    ] or ([garment_ref] if garment_ref else [])
    garment_paths: list[Path] = []
    for _gr in _garment_refs:
        _gp = _resolve_asset_path(db, _gr)
        if _gp is None:
            job.status = ST_FAILED
            job.error = f"镜头 {shot.key}:服装参考图取不到({_gr})"
            db.commit()
            return
        garment_paths.append(_gp)
    if not garment_paths:
        job.status = ST_FAILED
        job.error = f"镜头 {shot.key}:没有服装参考图"
        db.commit()
        return

    n = max(1, int(job.variants or 1))
    job.status = ST_RUNNING
    job.started_at = utcnow()
    job.heartbeat_at = utcnow()
    job.progress = 1
    job.current_shot = shot.key
    shot.status = ST_RUNNING
    db.commit()

    rels: list[str] = []
    try:
        for i in range(n):
            if _is_canceled(db, job.id):
                raise Canceled()
            job.message = (f"正在生成 {shot.key}" if n == 1
                           else f"正在抽卡 {i + 1}/{n} ({shot.key})")
            job.progress = min(95, int((i + 0.5) / n * 95))
            job.heartbeat_at = utcnow()
            db.commit()

            out = run_imgedit(
                [base_path, *garment_paths],
                shot.prompt or "",
                mask_bytes=b"",
                dest_dir=dest_dir,
                prefix=f"p{project.id}_s{shot.idx:02d}" + (f"_v{i + 1}" if i else ""),
            )
            target = dest_dir / (
                f"s{shot.idx:02d}.png" if i == 0 else f"s{shot.idx:02d}_v{i + 1}.png"
            )
            try:
                if target.exists():
                    target.unlink()
                out.replace(target)
            except Exception:  # noqa: BLE001
                import shutil as _sh
                _sh.copyfile(out, target)
            rels.append(target.relative_to(STORAGE_DIR).as_posix())

        kept = [c for c in (shot.candidates or []) if c]
        merged = kept + [r for r in rels if r not in kept]
        if len(merged) > 8:
            for old in merged[:-8]:
                try:
                    (STORAGE_DIR / old).unlink(missing_ok=True)
                except OSError:
                    pass
            merged = merged[-8:]
        shot.candidates = merged
        shot.clip_rel_path = rels[0]
        o = dict(shot.options or {})
        o["state_out"] = str(dest_dir / f"s{shot.idx:02d}.png")
        shot.options = o
        shot.status = ST_DONE

        # 这一镜要是链上最后一镜,它本身就是成片:按 70% 取景,并留一张高清版给后续分镜
        chain = [s for s in project.shots if s.enabled and (s.options or {}).get("static_image")]
        if chain and max(s.idx for s in chain) == shot.idx:
            canonical = dest_dir / f"s{shot.idx:02d}.png"
            hires = canonical.with_name(f"{canonical.stem}_hires.png")
            try:
                import shutil as _sh
                if not hires.exists():
                    _sh.copyfile(canonical, hires)
                o["style_source"] = str(hires)
                shot.options = o
                db.commit()
                _fit_full_body_frame(
                    canonical, derive_gen_size(project.out_width, project.out_height)
                )
            except Exception as exc:  # noqa: BLE001
                print(f"[pipeline] 静态成片取景失败:{exc}", flush=True)
            project.final_rel_path = canonical.relative_to(STORAGE_DIR).as_posix()
            job.out_rel_path = project.final_rel_path

        job.progress = 100
        job.status = ST_DONE
        job.message = "图片生成完成" if n == 1 else f"抽卡完成({len(rels)} 张)"
        job.finished_at = utcnow()
        db.commit()
    except Canceled:
        if rels:
            shot.candidates = ([c for c in (shot.candidates or []) if c] + rels)[-8:]
            shot.clip_rel_path = shot.clip_rel_path or rels[0]
        shot.status = ST_DRAFT
        job.status = ST_CANCELED
        job.message = "已终止"
        job.finished_at = utcnow()
        db.commit()
    except Exception as exc:  # noqa: BLE001
        shot.status = ST_FAILED
        db.commit()
        print(f"[pipeline] 静态单镜生成失败 {shot.key}:{str(exc)[:160]}", flush=True)
        job.status = ST_FAILED
        job.error = str(exc)[:500]
        job.finished_at = utcnow()
        db.commit()
    finally:
        comfy_client.free_memory()


def _run_static_job(db: Session, project: Project, job: RenderJob) -> None:
    """静态换装:逐镜跑 FLUX3 Klein imgedit,每件输出一张 PNG;最后一镜的 PNG 设为 project.final_rel_path。

    跟 _run_film_job 的最大区别:不生成 mp4、不走状态图锚 / 首尾帧锚 / 成片拼接 ——
    每镜就是「上一镜的 PNG(第 1 镜是人物参考图)+ 当前件参考图 → 一张新 PNG」。
    但每镜仍会把 PNG 路径写到 shot.options.state_out,这样:
      · _chain_tail_frame / _style_ref_for 无缝接入(后续追加的通用/自定义视频分镜
        会拿到最后那张静态图当「穿着样式参照」)
      · 编辑器里每个静态图分镜节点的 clip_url 指向 PNG(按扩展名渲染 <img>)
    """
    from .routers.imgedit import run_imgedit  # 避免循环 import(routers 也 import pipeline)

    static_shots = [
        s for s in project.shots
        if s.enabled and (s.options or {}).get("static_image")
    ]
    static_shots.sort(key=lambda s: s.idx)
    if not static_shots:
        job.status = ST_FAILED
        job.error = "静态换装:没有找到主链静态分镜(options.static_image=1)"
        db.commit()
        return

    job.status = ST_RUNNING
    job.started_at = utcnow()
    job.heartbeat_at = utcnow()
    job.progress = 1
    job.message = f"静态换装共 {len(static_shots)} 件,开始生成"
    project.status = ST_RUNNING
    db.commit()

    dest_dir = STORAGE_DIR / "static_shots" / f"p{project.id}"
    dest_dir.mkdir(parents=True, exist_ok=True)

    # 断点续传(用户 2026-10-22):已经出好的静态换装镜直接复用,「继续生成」不能再从
    # 第 1 镜重新画 —— 这会让前面已经调好的成片全部变样。只跳过从第 1 镜起**连续完成**
    # 的那一段:链式换装的底图必须是上一镜真实的 PNG,中间缺一张就从缺的那张接着画。
    prev_png: Path | None = None
    pending: list = []
    skipped = 0
    for shot in static_shots:
        canonical = dest_dir / f"s{shot.idx:02d}.png"
        if (not pending
                and (shot.clip_rel_path or "").strip()
                and canonical.exists()):
            prev_png = canonical
            shot.status = ST_DONE
            skipped += 1
            continue
        pending.append(shot)

    job.message = (
        f"静态换装共 {len(static_shots)} 件,已完成 {skipped} 件,"
        f"继续剩余 {len(pending)} 件"
        if skipped else f"静态换装共 {len(static_shots)} 件,开始生成"
    )

    try:
        for i, shot in enumerate(pending):
            if _is_canceled(db, job.id):
                raise Canceled()
            job.current_shot = shot.key
            shot.status = ST_RUNNING
            job.message = f"正在生成 {i + 1}/{len(pending)} ({shot.key} {shot.title})"
            job.heartbeat_at = utcnow()
            db.commit()

            opts = shot.options or {}
            char_ref = (opts.get("character_ref") or "").strip()
            garment_ref = (opts.get("garment_ref") or "").strip()
            # 「整套换装」一镜穿上多件:options.garment_refs = [ref1, ref2, ...]
            garment_refs = [
                str(x).strip() for x in (opts.get("garment_refs") or []) if str(x).strip()
            ] or ([garment_ref] if garment_ref else [])
            garment_name = (opts.get("garment_name") or "").strip() or f"第 {i + 1} 件"

            # 底图:第 1 镜 = 人物参考图;第 i 镜(i>0)= 上一镜的 PNG
            if prev_png is None:
                base_path = _resolve_asset_path(db, char_ref)
                if base_path is None:
                    raise RuntimeError(f"镜头 {shot.key}:人物参考图取不到({char_ref})")
            else:
                base_path = prev_png

            garment_paths: list[Path] = []
            for _gr in garment_refs:
                _gp = _resolve_asset_path(db, _gr)
                if _gp is None:
                    raise RuntimeError(f"镜头 {shot.key}:服装参考图取不到({_gr})")
                garment_paths.append(_gp)
            if not garment_paths:
                raise RuntimeError(f"镜头 {shot.key}:没有服装参考图")

            instruction = (shot.prompt or "").strip()
            if not instruction:
                if len(garment_paths) == 1:
                    instruction = (
                        f"以图1为底,让她穿上图2的「{garment_name}」。"
                        "脸 / 发型 / 身材 / 姿势 / 背景完全不变;图1里她身上已经穿着的其他衣物必须保持原样,"
                        "颜色 / 图案 / 版型 / 长度都不要变;颜色深浅、版型和细节和图2一致,"
                        "不要变浅,图2上没有的图案 / 花纹不要添加;"
                        "图2上有的部件(腰带 / 抽绳 / 纽扣 / 拉链头)全部保留,一件都不能少。"
                    )
                else:
                    _parts = "、".join(
                        f"图{k + 2}的「{str(nm).strip() or '这一件'}」"
                        for k, nm in enumerate(
                            (opts.get("garment_names") or [])
                            or [garment_name] * len(garment_paths)
                        )
                    )
                    instruction = (
                        f"以图1为底,让她依次穿上{_parts}。"
                        "脸 / 发型 / 身材 / 姿势 / 背景完全不变,只把这几件穿到她身上;"
                        "颜色深浅 / 饱和度、版型和细节各自和对应的参考图一致,不要变浅;"
                        "面料干净平滑、颜色均匀一致,参考图上没有的图案 / 花纹不要添加;"
                        "每件上的部件(腰带 / 纽扣 / 拉链头)全部保留,一件都不能少。"
                    )
            # 静态换装图链**完全不带常驻配饰**(用户 2026-10-22):换装图只负责衣服,
            # 配饰在后面通用 / 自定义分镜里用到哪件,就由那镜自己的 refs 引入。
            # 人物头像**不进换装图链**(用户 2026-10-22):换装镜只负责衣服,
            # 底图 + 衣服几张就够,头像只给后面的普通 / 自定义分镜锁头部。
            imgs = [base_path] + garment_paths
            out_path = run_imgedit(
                imgs,
                instruction,
                mask_bytes=b"",
                dest_dir=dest_dir,
                prefix=f"p{project.id}_s{shot.idx:02d}",
            )

            # 把 PNG 拷到稳定命名:static_shots/p{pid}/s{idx:02d}.png
            # (run_imgedit 内部用的是 uuid 名,这里改成可预测的,编辑器「重渲」覆盖同名)
            canonical = dest_dir / f"s{shot.idx:02d}.png"
            try:
                if canonical.exists():
                    canonical.unlink()
                out_path.replace(canonical)
            except Exception:  # noqa: BLE001
                import shutil as _sh
                _sh.copyfile(out_path, canonical)

            rel = canonical.relative_to(STORAGE_DIR).as_posix()
            shot.clip_rel_path = rel
            new_opts = dict(shot.options or {})
            new_opts["state_out"] = str(canonical)  # 给 _chain_tail_frame / _style_ref_for 用
            shot.options = new_opts
            shot.status = ST_DONE
            prev_png = canonical

            job.progress = min(95, int((i + 1) / len(pending) * 95))
            job.heartbeat_at = utcnow()
            job.message = f"{shot.key} 完成({i + 1}/{len(pending)})"
            db.commit()

        # 最后一镜的 PNG 就是项目的成片(单张静态图)。
        # 先把构图调成「人物占画面高度约 70%、上下各 15% 白边」的统一取景 ——
        # 中间几张还要当下一镜的底图,不能缩放,只有成片这一张重新取景。
        if prev_png is not None:
            # 只有这次真的重画了成片那张才重新取景;全部跳过时不要二次压缩,
            # 否则「继续生成」会把已经取好的 70% 构图再缩一遍。
            if pending:
                # 取景会把 848×2448 压成视频分辨率(可能只有 448×800)。压之前先留一份原尺寸,
                # 给后面所有通用 / 自定义分镜当「高清穿着参考图」——糊图喂进去脸和布料都会糊。
                hires = prev_png.with_name(f"{prev_png.stem}_hires.png")
                try:
                    if not hires.exists():
                        import shutil as _sh
                        _sh.copyfile(prev_png, hires)
                    last_static = max(static_shots, key=lambda s: s.idx)
                    _o = dict(last_static.options or {})
                    _o["style_source"] = str(hires)
                    last_static.options = _o
                    db.commit()
                except Exception as exc:  # noqa: BLE001 - 留不到就用取景后的
                    print(f"[pipeline] 高清参考图保存失败:{exc}", flush=True)
                _fit_full_body_frame(
                    prev_png, derive_gen_size(project.out_width, project.out_height)
                )
            final_rel = prev_png.relative_to(STORAGE_DIR).as_posix()
            project.final_rel_path = final_rel
            job.out_rel_path = final_rel
        project.status = ST_DONE
        job.status = ST_DONE
        job.progress = 100
        job.message = "静态换装完成"
        job.finished_at = utcnow()
        db.commit()
    except Canceled:
        _fail(db, job.id, Canceled("用户终止"))
        return
    except Exception as exc:  # noqa: BLE001
        _fail(db, job.id, exc)
    finally:
        # 静态图跑完立刻回收显存:FLUX3 Klein(DiT + 文本编码器)会一直缓存在显存里,
        # 不释放的话接着生成视频只剩 2GB 可用,MiniMaxH3 每步都要从内存搬权重(实测 40s/it)。
        try:
            comfy.free_memory()
            print("[pipeline] 静态换装结束,已回收 ComfyUI 显存", flush=True)
        except Exception as exc:  # noqa: BLE001 - 回收失败不影响已经出好的图
            print(f"[pipeline] 回收显存失败:{exc}", flush=True)


def _run_static_detail_job(db: Session, project: Project, job: RenderJob) -> None:
    """静态风格的整片任务第二阶段:把换装图后面还没做的普通 / 自定义分镜(视频)补齐。

    「继续生成」在静态项目上只跑换装图会让用户觉得什么都没发生(已完成的静态镜全被跳过,
    任务秒结)。这里接着把缺片段的细节镜按顺序做完;成片仍然是最后那张静态 PNG,
    不重新合成整片。
    """
    pending = [
        s for s in project.shots
        if s.enabled
        and not (s.options or {}).get("static_image")
        and not (s.options or {}).get("state_only")
        and not (s.clip_rel_path and (STORAGE_DIR / s.clip_rel_path).exists())
    ]
    shots_all = [s for s in project.shots if s.enabled]
    if not pending:
        job.progress = 100
        job.status = ST_DONE
        job.message = f"全部 {len(shots_all)} 个镜头已完成"
        job.finished_at = utcnow()
        project.status = ST_DONE
        db.commit()
        return

    job.message = f"换装图已完成,继续生成剩余 {len(pending)} 个分镜"
    db.commit()
    try:
        for i, shot in enumerate(pending):
            if _is_canceled(db, job.id):
                raise Canceled()
            job.current_shot = shot.key
            job.message = f"正在生成分镜 {i + 1}/{len(pending)}({shot.key} {shot.title})"
            shot.status = ST_RUNNING
            db.commit()

            frames = _generate_shot_for_stance(db, project, job, shot)
            seg = segment(frames, shot.duration_frames, shot.options)
            del frames
            seg = _align_shot(seg, shot)
            seg = _match_chain_frame(seg, shot)
            seg = _lock_lateral(seg, shot)
            seg = _lock_clip_tone(seg, shot.project_id)
            seg = _maybe_lock_feet(seg, shot)

            clip_rel = f"clips/p{project.id}_j{job.id}_{shot.key}.mp4"
            clip_file = STORAGE_DIR / clip_rel
            write_clip(clip_file, seg, _gen_size(project), project.fps)
            up_file = _maybe_upscale_clip(clip_file, project)
            if up_file != clip_file:
                clip_rel = up_file.relative_to(STORAGE_DIR).as_posix()
            shot.clip_rel_path = clip_rel
            _save_state_image(shot, seg)
            shot.status = ST_DONE

            job.progress = min(95, int((i + 1) / len(pending) * 95))
            job.heartbeat_at = utcnow()
            job.message = f"{shot.key} 完成({i + 1}/{len(pending)})"
            db.commit()

        job.progress = 100
        job.status = ST_DONE
        job.message = f"全部 {len(shots_all)} 个镜头已完成"
        job.finished_at = utcnow()
        project.status = ST_DONE
        db.commit()
    except Canceled:
        _fail(db, job.id, Canceled("用户终止"))
    except Exception as exc:  # noqa: BLE001
        _fail(db, job.id, exc)


def run_job(db: Session, job_id: int) -> None:
    job = db.get(RenderJob, job_id)
    if job is None or job.status == ST_CANCELED:
        return
    project = db.get(Project, job.project_id)
    if project is None:
        job.status, job.message = ST_FAILED, "项目不存在"
        db.commit()
        return

    # 静态换装风格:整片任务(shot_id=None)先跑换装图链,再把后面缺片段的普通 / 自定义分镜
    # 补齐(它们是视频)。单镜任务仍走 _run_shot_job。
    if (project.piece_style or "").strip().lower() == "static" and not job.shot_id:
        _run_static_job(db, project, job)
        db.refresh(job)
        if job.status == ST_DONE:
            _run_static_detail_job(db, project, job)
        return

    if job.shot_id:
        shot = db.get(ProjectShot, job.shot_id)
        if shot is not None and (shot.options or {}).get("static_image"):
            # 静态图分镜的单镜生成 / 抽卡:出 PNG,不是视频(用户 2026-10-22 报「图片抽卡变视频」)
            _run_static_shot_job(db, project, job, shot)
        else:
            _run_shot_job(db, project, job)
    else:
        _run_film_job(db, project, job)
