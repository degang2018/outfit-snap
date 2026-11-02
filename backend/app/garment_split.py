"""衣服分割:把一张平铺商品图(白底/纯色底)按背景切成若干件独立图片。

设计原则 —— **不破坏原图像素**:
  * 每件都是从原图 **1:1 裁出**的原始像素,不缩放、不重采样、不做任何颜色处理;
  * 输出 PNG(无损压缩),不会像 JPEG 那样产生压缩损失;
  * 只有「不属于这一件」的背景像素被填成白底(或透明),衣服本身的像素一个都不动;
  * 原图另存一份**字节完全相同**的副本(见 router)。

只依赖 numpy + Pillow(后端 .pylibs 里没有 scipy,所以连通域是自己实现的)。
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
from PIL import Image

# 相对整张图的面积占比:低于它的连通域当噪点丢掉
NOISE_RATIO = 0.0008
# 相对**最大那件**的面积占比:低于它的连通域当碎屑丢掉(线头、反光点之类)
FRAGMENT_RATIO = 0.02


def _dilate(m: np.ndarray, r: int) -> np.ndarray:
    for _ in range(r):
        p = np.pad(m, 1, constant_values=False)
        m = p[1:-1, 1:-1] | p[:-2, 1:-1] | p[2:, 1:-1] | p[1:-1, :-2] | p[1:-1, 2:]
    return m


def _erode(m: np.ndarray, r: int) -> np.ndarray:
    # 边界按"前景"处理:贴着画面边缘的衣服不会被啃掉一圈
    for _ in range(r):
        p = np.pad(m, 1, constant_values=True)
        m = p[1:-1, 1:-1] & p[:-2, 1:-1] & p[2:, 1:-1] & p[1:-1, :-2] & p[1:-1, 2:]
    return m


def _bg_color(a: np.ndarray) -> np.ndarray:
    """背景色 = 四条边像素的中位数(衣服通常不会占满整条边)。"""
    border = np.concatenate([a[0], a[-1], a[:, 0], a[:, -1]], axis=0)
    return np.median(border, axis=0)


def _label(fg: np.ndarray) -> np.ndarray:
    """4-邻域连通域标注,只用 numpy(后端没有 scipy)。

    做法:把每一行的前景压成若干「横条(run)」,相邻两行的横条只要在 x 上重叠就并到同一件,
    用并查集串起来。横条数量远小于像素数,所以比逐像素 BFS 快得多。
    返回 int32 标签图,0 = 背景,其余为 1..K-1 连续编号。
    """
    h, w = fg.shape
    labels = np.zeros((h, w), dtype=np.int32)
    parent: list[int] = [0]          # 并查集,下标 0 占位(背景)

    def find(x: int) -> int:
        root = x
        while parent[root] != root:
            root = parent[root]
        while parent[x] != root:      # 路径压缩
            parent[x], x = root, parent[x]
        return root

    prev: list[tuple[int, int, int]] = []      # 上一行的横条 [(x0, x1, label)]
    for y in range(h):
        row = fg[y]
        if not row.any():
            prev = []
            continue
        padded = np.concatenate(([False], row, [False]))
        t = np.flatnonzero(padded[1:] != padded[:-1])
        runs: list[tuple[int, int, int]] = []
        j = 0
        for k in range(0, len(t), 2):
            x0, x1 = int(t[k]), int(t[k + 1])       # 左闭右开
            lbl = 0
            while j < len(prev) and prev[j][1] <= x0:
                j += 1
            m = j
            while m < len(prev) and prev[m][0] < x1:
                other = prev[m][2]
                if lbl == 0:
                    lbl = other
                else:
                    ra, rb = find(lbl), find(other)
                    if ra != rb:
                        parent[max(ra, rb)] = min(ra, rb)
                m += 1
            if lbl == 0:
                parent.append(len(parent))
                lbl = len(parent) - 1
            labels[y, x0:x1] = lbl
            runs.append((x0, x1, lbl))
        prev = runs

    if len(parent) > 1:
        roots = np.array([find(i) for i in range(len(parent))], dtype=np.int32)
        labels = roots[labels]
    uniq = np.unique(labels)
    remap = np.zeros(int(labels.max()) + 1, dtype=np.int32)
    remap[uniq] = np.arange(len(uniq), dtype=np.int32)
    return remap[labels]


def split_image(
    src: Path,
    out_dir: Path,
    stem: str,
    parts: int = 0,
    tolerance: int = 20,
    transparent: bool = False,
) -> list[dict]:
    """把 src 按背景拆成若干件,写到 out_dir,返回每件的元信息(不含 url)。

    parts=0 表示自动(有几个连通域就切几件);>=2 时按「质心从上到下 + 最大的几个间隙」
    合并成 parts 组 —— 例如套装图上下叠了两件、你想强行只要 2 张时用。
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    with Image.open(src) as im:
        rgb = im.convert("RGB")
    a = np.asarray(rgb)
    h, w = a.shape[:2]

    bg = _bg_color(a)
    fg = np.abs(a.astype(np.int16) - bg.astype(np.int16)).max(axis=2) > int(tolerance)
    fg = _erode(_dilate(fg, 2), 2)        # 闭运算:补掉针织纹理里的细小孔洞

    labels = _label(fg)
    k = int(labels.max()) + 1
    if k <= 1:
        raise ValueError("没有找到衣服:这张图看起来是纯色底、没有可分割的主体")

    ys, xs = np.nonzero(labels)
    lab = labels[ys, xs]
    areas = np.bincount(lab, minlength=k)
    centroid_y = np.bincount(lab, weights=ys.astype(np.float64), minlength=k) / np.maximum(areas, 1)
    gy0 = np.full(k, h, dtype=np.int64); np.minimum.at(gy0, lab, ys)
    gy1 = np.zeros(k, dtype=np.int64);    np.maximum.at(gy1, lab, ys)
    gx0 = np.full(k, w, dtype=np.int64); np.minimum.at(gx0, lab, xs)
    gx1 = np.zeros(k, dtype=np.int64);    np.maximum.at(gx1, lab, xs)

    biggest = int(areas[1:].max())
    keep = [i for i in range(1, k)
            if areas[i] >= max(NOISE_RATIO * h * w, FRAGMENT_RATIO * biggest)]
    if not keep:
        raise ValueError("没有找到衣服")
    keep.sort(key=lambda i: float(centroid_y[i]))      # 从上到下

    if 2 <= parts <= len(keep):
        gaps = sorted(((float(centroid_y[keep[i + 1]] - centroid_y[keep[i]]), i)
                       for i in range(len(keep) - 1)), reverse=True)[:parts - 1]
        groups: list[list[int]] = []
        start = 0
        for _, cut in sorted(gaps, key=lambda g: g[1]):
            groups.append(keep[start:cut + 1])
            start = cut + 1
        groups.append(keep[start:])
    else:
        groups = [[i] for i in keep]

    pad = max(2, round(max(h, w) * 0.01))
    total = int(areas[keep].sum())
    out: list[dict] = []
    for n, group in enumerate(groups, start=1):
        mask = np.isin(labels, group)
        cy0 = max(int(min(gy0[g] for g in group)) - pad, 0)
        cx0 = max(int(min(gx0[g] for g in group)) - pad, 0)
        cy1 = min(int(max(gy1[g] for g in group)) + pad + 1, h)
        cx1 = min(int(max(gx1[g] for g in group)) + pad + 1, w)

        crop = a[cy0:cy1, cx0:cx1].copy()
        cm = mask[cy0:cy1, cx0:cx1]
        if transparent:
            alpha = np.where(cm, 255, 0).astype(np.uint8)
            img = Image.fromarray(np.dstack([crop, alpha]), "RGBA")
        else:
            crop[~cm] = 255                  # 只动"不属于这件"的背景像素
            img = Image.fromarray(crop, "RGB")

        name = f"{stem}_{n}.png"
        img.save(out_dir / name)             # PNG = 无损
        out.append({
            "index": n,
            "filename": name,
            "url": "",
            "width": cx1 - cx0,
            "height": cy1 - cy0,
            "share": round(float(areas[np.array(group, dtype=np.int64)].sum()) / total, 4),
        })
    return out
