"""读图给的「这件是哪儿、是不是穿在外面」——词汇 + 解析,全项目只有这一个来源。

读图那步**只回答两件事**:

1. ``place``   这件穿在身体的哪儿 —— 上身 / 下身 / 全身 / 脚 / 头 / 手 / 配饰;
2. ``outside`` 它是不是**穿在别的衣服外面**的(开衫 / 大衣 / 风衣 / 羽绒服 / 罩衫…)。

下游要的东西全部由这两条推出来:

* 从画面哪里进场 → ``place_group()``(top / bottom / feet / head / hand);
* 这一镜是「替掉身上那件」还是「加穿一件」→ ``outside``;
* 一件是上下连成一件的整身(``place == "全身"``)→ 它穿上之后上装 + 下装**都要消失**;
* 会顶掉衣服还是"谁都不顶"(脚 / 头 / 手 / 配饰只是配件)。

**这里故意没有品类词表**:睡裤 / 雪服 / 汉服 / 晨袍 / JK 制服 这些名字压根不需要我们认识 ——
名字只用来显示和写进提示词,「长什么样、怎么穿」由参考图说了算。
"""

from __future__ import annotations

# 部位 —— 只有这几档,认件时四选一,不存在"词表里没有这个词"的问题。
# 「配饰」是给项链 / 围巾 / 腰带 / 耳环这类小件的:它们**谁都不顶**,不是「一件衣服」。
PLACE_PARTS = ("上身", "下身", "全身", "脚", "头", "手", "配饰")

PLACE_GUIDE = (
    "上身=穿在上半身的(衬衫 / 毛衣 / 吊带 / 内衣 / 开衫 / 大衣 都写「上身」);"
    "下身=裤子 / 裙子;全身=**上下连成一件**的(连衣裙 / 连体衣 / 睡裙 / 旗袍);"
    "脚=鞋 / 靴 / 拖鞋;头=帽子 / 眼镜 / 头饰;手=包;"
    "配饰=项链 / 围巾 / 腰带 / 耳环 / 手表这类小件"
)

OUTSIDE_GUIDE = (
    "**这一件是不是穿在别的衣服外面的**(开衫 / 大衣 / 风衣 / 羽绒服 / 罩衫 / 披风…)——"
    "贴身穿的、或者就是一件普通上衣 / 裤子 / 裙子,都写「否」"
)

OUTSIDE_TOKEN = "·外"  # 「上身·外」= 上半身、而且穿在别的衣服外面

_PLACE_GROUP = {
    "上身": "top",
    "下身": "bottom",
    "全身": "bottom",
    "脚": "feet",
    "头": "head",
    "手": "hand",
    "配饰": "other",
}

# 老素材缓存里存的还是那套品类词(内搭 / 上装 / 外套 / 裤 / 裙 …)——
# 读回来得能对上,不然"重新识别"跑完之前那一批素材全废。
_LEGACY: dict[str, tuple[str, bool]] = {
    "内搭": ("上身", False),
    "上装": ("上身", False),
    "外套": ("上身", True),
    "下装": ("下身", False),
    "裤": ("下身", False),
    "裙": ("下身", False),
    "整身": ("全身", False),
    "鞋": ("脚", False),
    "帽": ("头", False),
    "眼镜": ("头", False),
    "包": ("手", False),
    "围巾": ("配饰", False),
    "腰带": ("配饰", False),
    "首饰": ("配饰", False),
    "配饰": ("配饰", False),
    "其他": ("", False),
}


def parse_slot(slot: str) -> tuple[str, bool]:
    """「上身·外」-> ``("上身", True)``;认不出来就给 ``("", False)``。"""
    s = str(slot or "").strip()
    if not s:
        return "", False
    outside = s.endswith(OUTSIDE_TOKEN)
    if outside:
        s = s[: -len(OUTSIDE_TOKEN)].strip()
    if s in PLACE_PARTS:
        return s, outside
    return _LEGACY.get(s, ("", False))


def parse_outside(value) -> bool:
    """读图回的「外穿」那一栏 —— 是 / 否、true / false 都认。"""
    v = value
    if isinstance(v, bool):
        return v
    s = str(v or "").strip().lower()
    return s in ("是", "true", "1", "yes", "y", "外", "外套")


def join_slot(place: str, outside: bool) -> str:
    """``("上身", True)`` -> ``"上身·外"`` —— 存进素材缓存(asset.ai_kind)的那串。"""
    p = str(place or "").strip()
    if p not in PLACE_PARTS:
        return ""
    return p + (OUTSIDE_TOKEN if outside else "")


def place_group(place: str) -> str:
    """place -> 进场用的几何部位(top / bottom / feet / head / hand / other)。"""
    return _PLACE_GROUP.get(str(place or "").strip(), "other")


# ---------------------------------------------------------------- 「开合」:扣子 / 拉链合不合上
# 用户要求"全局控制上衣把扣子扣上"(实测:黑色针织开衫被模型穿成敞开、还露出蕾丝内衣)。
# 这里只回答一件事:**这件的前襟是拉链还是扣子**(没有前襟的件 = 这一档不适用)。
# 具体要不要合上由调用方给(state),以及全局默认 settings.garment_close_default 兜底。
CLOSE_STATES = ("closed", "half", "open")

# 拆件名的分隔符:「套装-高腰百褶短裙」-> 「高腰百褶短裙」——
# 前缀说的是"这是哪一套",拿整名去扫关键词会被「套装」这类词带偏
# (和 quick_start / piece_prompt 里那两份是同一个规矩;这里自带一份,避免循环 import)。
_PIECE_SEP = ("-", "－", "—", "–", "·")


def _bare_name(name: str) -> str:
    n = str(name or "")
    for sep in _PIECE_SEP:
        head, s, tail = n.rpartition(sep)
        if s and head and tail.strip():
            return tail.strip()
    return n


# 硬性没有前襟的件:「开合」对它们无意义,一律跳过(先判这一组,它们不可能是开衫 / 外套)
_NEVER_FRONT_HINTS = (
    "吊带",
    "背心",
    "抹胸",
    "内衣",
    "胸衣",
    "bra",
    "半裙",
    "短裙",
    "百褶",
    "睡裤",
    "裤",
    "鞋",
    "拖鞋",
    "靴",
    "袜",
    "帽",
    "包",
    "袋",
    "围巾",
    "丝巾",
    "腰带",
    "皮带",
    "项链",
    "耳环",
    "耳钉",
    "手链",
    "手表",
    "手套",
    "墨镜",
    "眼镜",
)
# 拉链类
_ZIPPER_HINTS = (
    "拉链",
    "羽绒",
    "棉服",
    "棉袄",
    "冲锋",
    "滑雪",
    "雪服",
    "夹克",
    "皮衣",
    "运动服",
    "连体衣",
    "连体裤",
)
# 扣子类(必须排在"套头款"之前 —— 否则「开衫」会被「衫」吃成"没前襟")
_BUTTON_HINTS = (
    "开衫",
    "衬衫",
    "衬衣",
    "西服",
    "西装",
    "马甲",
    "POLO",
    "polo",
    "睡袍",
    "浴袍",
    "袍",
    "JK",
    "制服",
)
# 套头款:**没有前襟**。硬写"把扣子扣上",模型会凭空长出扣子
_PULLOVER_HINTS = (
    "T恤",
    "t恤",
    "上衣",
    "衫",
    "套头",
    "卫衣",
    "毛衣",
    "针织衫",
    "内搭",
    "打底",
)
# 有前襟、但看不出是扣还是拉链 → 用"通用前襟"的措辞(两种都提一句)
_FRONT_HINTS = ("外套", "风衣", "大衣", "连衣裙", "睡裙")


def front_kind(name: str, slot: str = "") -> str:
    """这件的前襟是「zipper」拉链 / 「button」扣子 / 「front」有前襟但不详 / 「none」没有前襟。

    判据顺序:部位(脚 / 头 / 手 / 配饰 / 下身 → 没前襟)> 硬性无前襟词 > 拉链词 > 扣子词
    > 套头款(没前襟)> 通用前襟词 > 兜底:读图说"穿在衣服外面"就当拉链外套,其余算没前襟。
    """
    n = _bare_name(name)
    place, outside = parse_slot(slot)
    if place in ("脚", "头", "手", "配饰", "下身"):
        return "none"
    if any(k in n for k in _NEVER_FRONT_HINTS):
        return "none"
    if any(k in n for k in _ZIPPER_HINTS):
        return "zipper"
    if any(k in n for k in _BUTTON_HINTS):
        return "button"
    if any(k in n for k in _PULLOVER_HINTS):
        return "none"
    if any(k in n for k in _FRONT_HINTS):
        return "front"
    if place == "全身":
        return "front"
    if outside:
        return "zipper"
    return "none"


def default_close(name: str, slot: str = "") -> str:
    """没单独指定时这一件的开合 —— 取全局默认(settings.garment_close_default)。"""
    from .config import settings  # 延迟 import:避免模块级循环依赖

    if front_kind(name, slot) == "none":
        return ""
    v = (settings.garment_close_default or "").strip().lower()
    return v if v in CLOSE_STATES else ""


def _close_how(kind: str, state: str) -> str:
    """按前襟型式给出「怎么合 / 怎么开」的具体说法。"""
    if kind == "zipper":
        return {
            "closed": "拉链**拉到最高、完全拉上**",
            "half": "拉链**只拉到胸口**",
            "open": "拉链**全程拉开**",
        }[state]
    if kind == "button":
        # 「扣齐(一颗不落)」就够了:再补下摆 / 参考图开口那些话,反而会提醒模型去画那道缝
        # (项目 sdffwefw:加上"参考图下摆有一点开口"之后,模型反而照抄成了敞开的 V 口)。
        return {
            "closed": "前襟的**扣子全部扣上、扣齐(一颗不落)**",
            "half": "**只扣中间一两颗扣子**",
            "open": "**扣子全部解开**",
        }[state]
    return {
        "closed": "前襟**合上**(有扣子就把扣子全扣齐、有拉链就拉到最高)",
        "half": "前襟**只合上一半**(扣到胸口 / 拉链拉到胸口)",
        "open": "前襟**完全敞开**",
    }[state]


def fasten_clause(name: str, slot: str = "", state: str = "") -> str:
    """这一件「开合」的硬约束句(给提示词用);没前襟 / 状态为空 → 返回 ""。

    state 空时按 default_close() 取全局默认,所以调用方只要传件名 + 部位就行。
    只写进 <Prop> 定义就够了 —— 【换装】/动作段不再重复(用户 2026-10-21 实测:
    重复反而把动作段的注意力从换装动作上带走)。
    """
    kind = front_kind(name, slot)
    if kind == "none":
        return ""
    st = (state or "").strip().lower() or default_close(name, slot)
    if st not in CLOSE_STATES:
        return ""
    q = f"「{name}」"
    if st == "closed":
        return f"{q}必须**合上穿**:{_close_how(kind, st)}。"
    if st == "half":
        return (
            f"{q}{_close_how(kind, st)},上半段留一道开口 —— 开口处**只允许露出本镜头清单里的内搭**;"
            "**绝不许出现清单外的内衣 / 蕾丝胸衣 / 裸露的胸口**。"
        )
    return (
        f"{q}**敞开穿**({_close_how(kind, st)})"
        " —— 敞开处**只允许露出本镜头清单里的内搭**;"
        "**绝不许出现清单外的内衣 / 蕾丝胸衣 / 裸露的胸口**。"
    )


def fasten_short(name: str, slot: str = "", state: str = "") -> str:
    """summary / detailed_description 用的**短句版**(和 fasten_clause 同一档状态)。

    实测:只在 <Prop> 定义里写是不够的 —— **summary 和 detailed_description 才是模型最听的两段**
    (见 script_split 里那条老注释)。项目 test09000 的自拍镜就是这么翻车的:
    定义里明明写着"扣子全部扣上",那两段里没有,模型照样把黑开衫穿成敞开、还露出蕾丝内衣。
    """
    kind = front_kind(name, slot)
    if kind == "none":
        return ""
    st = (state or "").strip().lower() or default_close(name, slot)
    if st not in CLOSE_STATES:
        return ""
    if st == "closed":
        how = {
            "zipper": "拉链拉满(拉到顶)",
            "button": "扣子全部扣齐(从领口扣到下摆、一颗不落)",
            "front": "前襟合上(扣齐 / 拉满)",
        }[kind]
        return (
            f"「{name}」{how}、前襟闭合不许敞开 —— 下摆也要合拢,"
            "绝不露出内衣 / 胸口 / 腰 / 肚子"
        )
    if st == "half":
        return f"「{name}」只合一半(扣到胸口 / 拉链拉到胸口)"
    return f"「{name}」敞开穿,敞开处只许露出本清单里的内搭"


def fasten_line(items: list[tuple[str, str, str]]) -> str:
    """把几件的开合短句拼成一行,给 summary / detailed_description 用;都没有就返回 ""。

    items 是 (件名, 部位, 状态) 三元组 —— 各个生成器按自己的字段取。
    """
    parts = [t for t in (fasten_short(n, s, c) for n, s, c in items) if t]
    return ("**穿法:" + ";".join(parts) + "。**") if parts else ""


# 和开合无关、但每次都要说的那句(截图里那件黑蕾丝胸衣就是这么冒出来的)
NO_UNDERWEAR_TXT = (
    "**她身上只穿本镜头列出的那几件衣服 —— 绝不许出现清单之外的内衣 / 文胸 / 蕾丝胸衣 / "
    "裸露的胸口 / 裸露的腰 / 肚子 / 肚脐**;内衣只可能在参考图里出现,不许照抄进来;"
    "**衣服盖住的地方就必须是盖住的 —— 绝不许把上衣下摆撩开 / 卷起 / 松开。**"
)
