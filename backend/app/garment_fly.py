"""程序化「新衣服进场」:抠商品图 → 量落点 → 造**中间帧锚图**(喂 shot.options["mid_anchor"])。

为什么要它(实测结论):
* 提示词/参考图管不了几何 —— 写"占画面 72%"出来 0.88,写"右上角进"它把衣服放在画面正中、
  还比躯干大两倍。文字是语义通道,传不了数值。
* 但 **AddGuide 能钉死几何**:把"第 f 帧衣服应该在哪儿、多大"做成一张图喂进去,模型只能照办
  (项目 185 s03 实测:锚点帧上衣服的框和锚图差 ≤2px)。
* 所以:几何由程序定(这套),像素还是模型画(有光影/布料感,不是贴纸)。

落点怎么量:**按人高归一化**再投影 —— 不同状态片段的取景/站位会差几十像素,直接拿绝对坐标
会贴歪。测量帧里的人框当基准,换算成相对比例,再投到锚图背景那一帧的人框上。
"""
from __future__ import annotations

import math
import os
from collections import Counter

from PIL import Image, ImageChops

# 进场:第 ENTER 帧只在画面边缘露 <ENTRY_VISIBLE> 的宽度,第 LAND 帧到位。
# TOTAL_FRAMES 只是算"第几帧"时的参考总长(实际用镜头自己的 gen_length)。
TOTAL_FRAMES = 124
ENTER_FRAME = 4
LAND_FRAME = 70
ENTRY_VISIBLE = 0.25
# 横向最多压缩到落点宽度的几倍(平铺图 vs 穿着时的宽高比差异,别硬缩成一条窄带)。
MAX_SQUEEZE = 1.35
# 各部位的进场角度(倾斜,度)与布料波浪振幅(px):到位时都归零。
_FLY = {
    "top": (0.06, -8.0, 9),      # (起点纵坐标占画面比, 起始倾斜, 起始波浪)
    "outer": (0.06, -10.0, 11),
    "bottom": (0.80, 6.0, 7),
}


def person_box(im: Image.Image, th: int = 200):
    """近白背景里量人物外框。"""
    return im.convert("L").point(lambda v: 255 if v < th else 0).getbbox()


def _light(p) -> bool:
    """中性浅灰(浅灰衣服):排掉背景(235+)和皮肤(偏暖)。"""
    rr, gg, bb = p
    m = (rr + gg + bb) / 3
    return max(rr, gg, bb) - min(rr, gg, bb) < 18 and 150 < m < 218


def _dense_box(im, y0, y1, x0, x1, pred):
    col, row = Counter(), Counter()
    for y in range(y0, y1):
        for x in range(x0, x1):
            if pred(im.getpixel((x, y))):
                col[x] += 1
                row[y] += 1
    if not col or not row:
        return None
    cmin, rmin = 0.15 * (y1 - y0), 0.15 * max(col.values())
    xs = sorted(x for x, c in col.items() if c >= cmin)
    ys = sorted(y for y, c in row.items() if c >= rmin)
    return None if not xs or not ys else (xs[0], ys[0], xs[-1], ys[-1])


def measure_relative(im: Image.Image, kind: str):
    """在「换装后」那一帧里量出这件衣服的框,换算成**相对人框**的比例。
    x 按**人框宽**归一、y 按**人框高**归一(这样换一段取景不一样的片段时,投过去也不会歪)。"""
    pb = person_box(im)
    if not pb:
        return None
    l, t, r, b = pb
    h = b - t
    if kind == "bottom":                     # 下装:在下半身找浅灰(浅灰阔腿裤)/深色(深色裤)
        y0, y1 = t + int(0.42 * h), t + int(0.99 * h)
        box = _dense_box(im, y0, y1, l + int(0.06 * (r - l)), r - int(0.06 * (r - l)), _light)
        if box is None:
            box = _dense_box(im, y0, y1, l + int(0.06 * (r - l)), r - int(0.06 * (r - l)),
                             lambda p: max(p) < 90)
    else:                                    # 上装 / 外搭:躯干带找浅灰
        y0, y1 = t + int(0.10 * h), t + int(0.70 * h)
        box = _dense_box(im, y0, y1, l + int(0.02 * (r - l)), r - int(0.02 * (r - l)), _light)
    if box is None:
        return None
    pw = max(1, r - l)
    return ((box[0] - l) / pw, (box[1] - t) / h, (box[2] - l) / pw, (box[3] - t) / h)


# 量不到落点时的兜底(x 按人宽、y 按人高)。数值是实测标定出来的:
#   上装 吊带背心 宽 0.62 / 顶 0.254 / 高 0.234 ・ 下装 阔腿裤 宽 0.79 / 顶 0.419 / 高 0.535
#   外搭 长开衫   宽 0.56 / 顶 0.199 / 高 0.499
_FALLBACK = {
    "top": (0.19, 0.25, 0.81, 0.49),
    "bottom": (0.10, 0.42, 0.90, 0.95),
    "outer": (0.11, 0.20, 0.89, 0.70),
    "whole": (0.11, 0.18, 0.89, 0.62),
}


def project_onto(bg: Image.Image, rel):
    """把相对比例投到锚图背景这一帧的人物框上,得到绝对落点框。"""
    pb = person_box(bg)
    l, t, r, b = pb
    h, pw = b - t, max(1, r - l)
    return (int(round(l + rel[0] * pw)), int(round(t + rel[1] * h)),
            int(round(l + rel[2] * pw)), int(round(t + rel[3] * h)))


def cutout(path: str, white: int = 244, solid: int = 224) -> Image.Image:
    """白底商品图 -> RGBA(按通道最小值算 alpha,边缘留渐隐)。"""
    g = Image.open(path).convert("RGB")
    mn = ImageChops.darker(ImageChops.darker(*g.split()[:2]), g.split()[2])
    alpha = mn.point(lambda v: 0 if v >= white else (255 if v <= solid else int((white - v) / (white - solid) * 255)))
    im = g.convert("RGBA")
    im.putalpha(alpha)
    return im.crop(alpha.getbbox())


def tilt_wave(img: Image.Image, tilt: float, amp: float) -> Image.Image:
    """先按行做正弦横移(布料波浪),再整体倾斜一点点 —— 锚图之间要有形变差,否则模型被夹成硬板。"""
    pad = int(abs(amp)) + 4
    canvas = Image.new("RGBA", (img.width + 2 * pad, img.height), (0, 0, 0, 0))
    for y in range(img.height):
        phase = 2 * math.pi * (y / max(1, img.height - 1)) * 0.9
        canvas.paste(img.crop((0, y, img.width, y + 1)), (pad + int(round(amp * math.sin(phase))), y))
    canvas = canvas.crop((pad, 0, pad + img.width, img.height))
    return canvas.rotate(tilt, resample=Image.BICUBIC, expand=True) if abs(tilt) > 0.05 else canvas


def build_anchors(*, bg: Image.Image, cut: Image.Image, target, kind: str, frames, out_dir: str,
                  prefix: str, total: int = 124, enter: int = ENTER_FRAME, land: int = LAND_FRAME):
    """造中间帧锚图,返回 [(路径, 帧号), ...]。"""
    W, H = bg.size
    bx0, by0, bx1, by1 = target
    tw_t, th_t = bx1 - bx0, by1 - by0
    fly_y, tilt0, amp0 = _FLY.get(kind, _FLY["top"])

    # 定标:**高度用实测的(衣服"多长"最直观)**,宽度按平铺图自己的比例 —— 但最多允许压缩到
    # 落点宽度的 MAX_SQUEEZE 倍。商品平铺图是"摊开"的,和穿着时的宽高比能差很多(实测阔腿裤:
    # 平铺 0.57 / 穿着 0.36),硬按落点框缩会把它挤成一条窄带。
    natural_w = th_t * (cut.width / cut.height)
    tw = int(round(min(max(natural_w, tw_t), tw_t * MAX_SQUEEZE)))
    th = th_t

    # 起点:画面右侧画外,只露一点点;终点:落点框中心
    sx = W - ENTRY_VISIBLE * tw
    c0 = (sx + tw / 2, fly_y * H + th / 2)
    c1 = ((bx0 + bx1) / 2, (by0 + by1) / 2)

    ref = cut.resize((max(1, tw), max(1, th)), Image.LANCZOS)
    os.makedirs(out_dir, exist_ok=True)
    out = []
    for f in frames:
        k = (f - enter) / (land - enter)
        cx = c0[0] + (c1[0] - c0[0]) * k
        cy = c0[1] + (c1[1] - c0[1]) * k
        sh = tilt_wave(ref, tilt0 * (1 - k), amp0 * (1 - k))
        fr = bg.copy()
        fr.paste(sh, (int(round(cx - sh.width / 2)), int(round(cy - sh.height / 2))), sh)
        p = os.path.join(out_dir, f"{prefix}_mid{f:03d}.png")
        fr.save(p)
        out.append((p, f))
    return out


def plan_frames(count: int, total: int = TOTAL_FRAMES, enter: int = ENTER_FRAME,
                land: int = LAND_FRAME) -> list[int]:
    """一条飞行轨迹上挂哪几帧当路标:均匀铺在 enter→land 之间(两头各留一点余量)。

    一条不够(模型过了路标又开始画巨幅),所以要一串;太多又会把人物也钉僵,默认 4 条。
    """
    n = max(1, int(count))
    if n == 1:
        return [int(round(enter + (land - enter) * 0.6))]
    return [int(round(enter + (land - enter) * (0.2 + 0.7 * i / (n - 1)))) for i in range(n)]


def make_anchors(*, prev_img: Image.Image, next_img: Image.Image, garment_path: str, kind: str,
                 out_dir: str, prefix: str, count: int = 4, total: int = TOTAL_FRAMES,
                 enter: int = ENTER_FRAME, land: int = LAND_FRAME):
    """一步到位:量落点(量不到用兜底) → 抠商品图 → 造 count 张中间帧锚图。

    prev_img = 「换之前」那身的状态图(当背景,和首帧锚是同一张);next_img = 「换好之后」那身
    (从它身上量这件衣服的位置/大小)。返回 [(路径, 帧号), ...];素材有问题就返回 []。
    """
    rel = measure_relative(next_img, kind) or _FALLBACK.get(kind) or _FALLBACK["top"]
    target = project_onto(prev_img, rel)
    if target[2] - target[0] < 8 or target[3] - target[1] < 8:
        return []
    try:
        cut = cutout(garment_path)
    except Exception:            # noqa: BLE001 - 抠图失败就当没锚图,别连累生成
        return []
    if cut.width < 8 or cut.height < 8:
        return []
    return build_anchors(bg=prev_img, cut=cut, target=target, kind=kind,
                         frames=plan_frames(count, total, enter, land),
                         out_dir=out_dir, prefix=prefix, total=total, enter=enter, land=land)
