"""摄像机机位(多角度管理)-> 提示词子句。

前端把机位存在 shot.options["camera"]:
    {"azimuth": 0..360, "elevation": -30..60, "distance": 1..10,
     "subject_mode": "fixed" | "follow", "scale": 0..100}

  azimuth      0=正面, 90=右侧, 180=背面, 270=左侧
  elevation    正=机位高于人物(俯拍), 负=低于人物(仰拍), 0=平视
  distance     摄像机到人物的距离(米), 1=特写, 4=中景, 8=全身
  subject_mode fixed  = 人朝场景正前方不动, 镜头绕人转(会看到侧面/背面)
               follow = 人始终跟着镜头转身, 画面始终是正面
  scale        旧字段(0=近, 100=远), 没存 distance 时用来反推距离

机位是"生成时注入"的,不写死进用户保存的提示词正文 —— 这样用户来回调角度不会
在提示词里堆一长串互相矛盾的机位描述。
"""
from __future__ import annotations

_SIDES = ((0, "正面"), (45, "右前方"), (90, "右侧"), (135, "右后方"),
          (180, "背面"), (225, "左后方"), (270, "左侧"), (315, "左前方"))

_SIDES_EN = {0: "front", 45: "front-right", 90: "right side", 135: "back-right",
             180: "back", 225: "back-left", 270: "left side", 315: "front-left"}


def normalize(cam) -> dict | None:
    """把前端传来的机位收拢到合法范围;坏数据直接当没设置。"""
    if not isinstance(cam, dict):
        return None
    try:
        az = float(cam.get("azimuth", 0)) % 360
        el = max(-30.0, min(60.0, float(cam.get("elevation", 0))))
        sc = max(0.0, min(100.0, float(cam.get("scale", 50))))
    except (TypeError, ValueError):
        return None
    # 距离优先;老数据只有 scale 时按 0..100 -> 1..10 米反推
    try:
        dist = float(cam.get("distance"))
    except (TypeError, ValueError):
        dist = 1.0 + sc / 100.0 * 9.0
    dist = max(1.0, min(10.0, dist))
    mode = cam.get("subject_mode")
    if mode not in ("fixed", "follow"):
        mode = "fixed"
    return {
        "azimuth": round(az),
        "elevation": round(el),
        "scale": round(sc),
        "distance": round(dist, 1),
        "subject_mode": mode,
    }


def _side(az: int) -> str:
    return min(_SIDES, key=lambda p: min(abs(az - p[0]), 360 - abs(az - p[0])))[1]


def _side_key(az: int) -> int:
    return min((p[0] for p in _SIDES), key=lambda v: min(abs(az - v), 360 - abs(az - v)))


def _tilt(el: int) -> str:
    if abs(el) < 8:
        return "平视"
    return ("俯拍" if el > 0 else "仰拍") + f" {abs(el)}°"


def _tilt_en(el: int) -> str:
    if abs(el) < 8:
        return "eye-level"
    return ("high-angle" if el > 0 else "low-angle") + f" {abs(el)} degrees"


# 距离(米) -> 景别。普通 35-50mm 镜头、成人身高约 1.7m 的经验值
_FRAMING = ((1.8, "特写"), (2.8, "近景"), (4.6, "中景"), (6.6, "中近景"), (8.6, "全身"))


def _framing(dist: float) -> str:
    for limit, label in _FRAMING:
        if dist <= limit:
            return label
    return "远景"


def _framing_en(dist: float) -> str:
    if dist <= 1.8:
        return "close-up"
    if dist <= 2.8:
        return "close shot"
    if dist <= 4.6:
        return "medium shot"
    if dist <= 6.6:
        return "medium-full shot"
    if dist <= 8.6:
        return "full shot"
    return "wide shot"


# 每个景别在画面里到底拍到哪里 —— 只写「全身」两个字模型经常还是裁到膝盖。
_FRAMING_TIP = {
    "特写": "画面只拍到头部和颈肩（大约到锁骨），不要出现胸部以下",
    "近景": "画面拍到胸部以上（头部到胸口），不要拍到腰以下",
    "中景": "画面从头顶拍到胯部，不要出现膝盖以下和脚",
    "中近景": "画面从头顶拍到大腿中部，不要出现脚",
    "全身": ("画面必须从头顶到脚底完整入画：头顶留少量空间，"
             "双腿、双脚和鞋子都要完整出现在画面里，绝对不要裁掉腿或脚"),
    "远景": "画面从头顶到脚底完整入画，人物大约占画面高度的二分之一到三分之二，四周留白",
}

_FRAMING_TIP_EN = {
    "close-up": "only her head and neck/shoulders are in frame, nothing below the chest",
    "close shot": "head to chest only, nothing below the waist",
    "medium shot": "head to hips, no knees or feet in frame",
    "medium-full shot": "head to mid-thigh, feet not in frame",
    "full shot": ("her whole body from the top of the head to the soles of both feet, "
                  "with shoes fully visible and a little headroom; never crop the legs or feet"),
    "wide shot": ("full body head-to-toe, occupying about half to two thirds of the frame height, "
                  "with space around her"),
}


# 每个方位「画面里应该看到什么」——比只写角度更能让模型真的转过去
_VISIBLE = {
    0: "画面是正面视角:能看到她的正脸、双肩和身体正面",
    45: "画面是右前方视角:能看到她的脸和身体右侧的立体轮廓",
    90: "画面是右侧视角:能看到她的右侧脸、右耳和右侧肩膀,身体呈侧面",
    135: "画面是右后方视角:主要看到她的后背、右侧肩膀和身体轮廓",
    180: "画面是背面视角:主要看到她的后背、后脑勺和肩背线条",
    225: "画面是左后方视角:主要看到她的后背、左侧肩膀和身体轮廓",
    270: "画面是左侧视角:能看到她的左侧脸、左耳和左侧肩膀,身体呈侧面",
    315: "画面是左前方视角:能看到她的脸和身体左侧的立体轮廓",
}


def clause_zh(cam) -> str:
    c = normalize(cam)
    if c is None:
        return ""
    dist = c["distance"]
    label = _framing(dist)
    head = (
        "【机位·最高优先级】"
        f"摄像机在人物{_side(c['azimuth'])}、{_tilt(c['elevation'])}，"
        f"距离约 {dist:.1f} 米，景别：{label}"
        f"（水平 {c['azimuth']}°，垂直 {c['elevation']}°）。"
        f"{_FRAMING_TIP[label]}。"
    )
    if c["subject_mode"] == "follow":
        # 场景一:人一直对着镜头。机位固定时只需要「正面朝向镜头」——
        # 不能写「跟着摄像机转身 / 摄像机绕着她移动」:和同一句里的「机位固定」
        # 自相矛盾,模型会理解成人物自己在原地慢慢转身(实测踩过)。
        body = (
            "她始终正面朝向镜头,脸和身体正面都对着镜头,不背对、不侧转、不自己转身;"
        )
    else:
        # 场景二:人朝场景正前方不动 —— 镜头绕人转,会看到侧面 / 背面
        body = (
            f"{_VISIBLE[_side_key(c['azimuth'])]};"
            "她保持面朝场景正前方,身体和脸都不跟着镜头转身。"
        )
    return (
        head + body
        + "整段都按这个机位拍摄:即使参考图是正面,也不要照抄参考图的正面构图。"
        "机位固定，不推拉摇移、不变焦。"
    )


_VISIBLE_EN = {
    0: "front view showing her face and both shoulders",
    45: "front-right three-quarter view showing the right side of her body",
    90: "right profile view showing her right cheek, right ear and right shoulder",
    135: "back-right view showing her back and right shoulder",
    180: "back view showing her back and the back of her head",
    225: "back-left view showing her back and left shoulder",
    270: "left profile view showing her left cheek, left ear and left shoulder",
    315: "front-left three-quarter view showing the left side of her body",
}


def clause_en(cam) -> str:
    c = normalize(cam)
    if c is None:
        return ""
    dist = c["distance"]
    label = _framing_en(dist)
    head = (
        "CAMERA ANGLE (highest priority): "
        f"camera at {_SIDES_EN[_side_key(c['azimuth'])]}, {_tilt_en(c['elevation'])}, "
        f"about {dist:.1f} meters from her, {label}. "
        f"{_FRAMING_TIP_EN[label]}. "
    )
    if c["subject_mode"] == "follow":
        body = ("She always faces the lens with her face and body front-on, never turning away or "
                "turning herself. ")
    else:
        body = (f"{_VISIBLE_EN[_side_key(c['azimuth'])]}. "
                "She keeps facing the fixed front direction of the scene and does not turn with the camera. ")
    return (
        head + body
        + "Shoot the whole shot from this angle even if the reference image is a front view; "
        "do not copy the reference framing. Fixed camera, no zoom or camera movement."
    )


def apply_zh(prompt: str, cam) -> str:
    """机位子句放在最前面 —— 放在长提示词末尾时模型基本会忽略。"""
    line = clause_zh(cam)
    if not line:
        return prompt
    body = (prompt or "").strip()
    return (line + "\n" + body) if body else line


def apply_en(prompt: str, cam) -> str:
    line = clause_en(cam)
    if not line:
        return prompt
    body = (prompt or "").strip()
    return (line + " " + body) if body else line


def apply(prompt: str, cam) -> str:
    """按提示词语言挑中/英子句 —— 英文提示词里塞中文机位,模型经常直接忽略。"""
    body = prompt or ""
    latin = sum(1 for ch in body if ch.isascii() and ch.isalpha())
    cjk = sum(1 for ch in body if "\u4e00" <= ch <= "\u9fff")
    return apply_en(body, cam) if latin > cjk * 2 else apply_zh(body, cam)
