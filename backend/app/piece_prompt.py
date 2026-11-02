"""「按件排镜」专用提示词生成器 —— 骨架照项目 155(单镜整套换装 · 按件拆),但**一切按数据推**,通用。

通用点(用户强调"要想到各种可能性"):
* 件数不限 —— N 件 → 2N+1 镜(每件前先来一镜"换好之后的样子");
* 部位/类型**优先用读图认件的结果**(槽位 slot + 外观描述 desc),件名里的关键词只作兜底 ——
  上装 / 外套 / 裤 / 裙 / 整身 连衣裙 / 鞋 / 包 / 帽 / 眼镜 / 其它配饰,
  每种有自己的"从哪飞、落到哪、多大、量词";
* 长短同样**优先用读图给的那一档**(短 / 常规 / 长),拿不到才按件名里的字猜;
* 代词:男模→他、女模→她、认不出→"模特"(从人物描述里读,见 _pronoun_of);
* 缺什么就不写那句:没旧件 / 没配饰 / 没风格图 / 不是展示镜;
* 一镜要飞多件(≥2)也能写("第 1 件…第 2 件…")。
"""

from __future__ import annotations

from .garment_kind import (
    CLOSE_STATES,
    NO_UNDERWEAR_TXT,
    default_close,
    fasten_clause,
    parse_slot,
)

# 肤色一致性(和 script_split.SKIN_HINT 保持同一段措辞)。
SKIN_HINT = (
    "**她的肤色与光线:脸、脖子、锁骨、肩膀和手的肤色是同一个自然色调,过渡均匀一致;"
    "脸上的底妆和脖子、身体完全同色;柔和均匀的正面漫射光铺满整张脸,"
    "明暗过渡细腻自然,皮肤质感真实、细腻。**"
)


def _fasten_of(e: dict) -> str:
    """这一件「开合」的约束句(件级 close > 全局默认 settings.garment_close_default)。

    没有前襟的件(吊带 / 背心 / 裤 / 鞋 / 包…)返回 ""。
    **同一句要写进 <Prop> 定义和动作说明两处** —— defs 权重更高,只改一处模型会打自己脸。
    """
    return fasten_clause(
        e.get("name") or "", e.get("slot") or e.get("_slot") or "", e.get("close") or ""
    )


# ---- 类型关键词(按"名字里有没有这些字"判;顺序有讲究:整身 > 外套 > 下装 > 上装 > 鞋/包/帽)
_WHOLE = ("连衣裙", "连体", "连衣裤", "睡裙", "套装", "旗袍", "吊带裙")
_OUTER = (
    "外套",
    "大衣",
    "风衣",
    "羽绒",
    "棉袄",
    "棉服",
    "夹克",
    "西装",
    "马甲",
    "开衫",
    "披风",
)
_BOTTOM = ("裤", "裙")
_TOP = (
    "衫",
    "衣",
    "上衣",
    "T恤",
    "t恤",
    "卫衣",
    "背心",
    "吊带",
    "打底",
    "衬衫",
    "毛衣",
    "针织",
)
_SHOE = ("鞋", "靴", "拖鞋", "凉拖", "棉拖", "高跟")
_BAG = ("包", "手袋", "托特", "背包", "挎包", "方包", "腰包")
_HAT = ("帽", "贝雷", "渔夫", "头盔")
_GLASSES = ("镜", "眼镜", "墨镜", "太阳镜")
_LONG = (
    "长",
    "拖地",
    "及踝",
    "过膝",
    "大衣",
    "风衣",
    "羽绒",
    "棉袄",
    "棉服",
    "阔腿",
    "maxi",
)


_OUTER_EXTRA = (
    "雪服",
    "滑雪服",
    "滑雪外套",
    "防寒服",
    "冲锋",
    "唐装",
    "和服",
    "汉服",
    "大氅",
)

# 读图只回答两件事(见 garment_kind):**部位** + **是不是穿在别的衣服外面**。
# 这一件"飞进来是什么姿态、落到哪儿、多大",就由这两条推出来 —— 不再需要品类词表:
#   · 部位=全身 / 上身+外穿 / 上身 / 下身 / 脚 / 头 / 手 / 配饰;
#   · 眼镜和帽子都戴在「头」上,靠件名分一下(它们飞进来的姿态不一样)。
# 读图没给部位时(老素材缓存 / plan 里没绑图的件)才退回按件名里的字猜。

# 拆件名的分隔符:「同一前缀 + 破折号 + 单件名」(见 script_split 的拆件约定)。
_PIECE_SEP = ("-", "－", "—", "–", "·")


def _strip_piece_prefix(name: str) -> str:
    """「套装-高腰百褶短裙」-> 「高腰百褶短裙」。

    整套拆出来的每一件都带着同一个前缀,前缀说的是"这是哪一套",不是这一件的样子 ——
    拿整名去扫关键词必然被「套装 / 套装裙」这类词带偏,所以判类型前先把前缀剥掉。
    """
    n = str(name or "")
    for sep in _PIECE_SEP:
        head, s, tail = n.rpartition(sep)
        if s and head and tail.strip():
            return tail.strip()
    return n


def _kind_from_hint(name: str, slot: str = "", desc: str = "") -> str:
    """**先用读图给的答案判**(部位 + 是不是穿在外面),再用名字/描述里的关键词兜底。

    这样没见过的名字(雪服 / 汉服 / 睡裤 / JK 制服…)天生就能判对 —— 读图会答
    「上身 + 是(穿在外面)」,不需要我们知道"雪服"这个词。
    返回 "" 表示判不出来(交给调用方兜底)。
    """
    place, outside = parse_slot(slot)
    if place == "全身":
        return "whole"
    if place == "上身":
        return "outer" if outside else "top"
    if place == "下身":
        return "bottom"
    if place == "脚":
        return "shoe"
    if place == "头":
        return "glasses" if any(k in str(name or "") for k in _GLASSES) else "hat"
    if place == "手":
        return "bag"
    if place:
        return "acc"  # 配饰:小件
    # 没给部位 -> 退回按件名 / 描述里的字猜。顺序:整身 > 眼镜 > 鞋 > 穿在外面的 > 下装 > 上装
    # (鞋必须排在下装 / 上装前面:「针织拖鞋」「棉拖鞋」的描述里有"针织",会被上装词抢走)
    blob = _strip_piece_prefix(name) + str(desc or "")
    if any(k in blob for k in _WHOLE):
        return "whole"
    if any(k in blob for k in _GLASSES):
        return "glasses"
    if any(k in blob for k in _SHOE):
        return "shoe"
    if any(k in blob for k in _OUTER) or any(k in blob for k in _OUTER_EXTRA):
        return "outer"
    if any(k in blob for k in _BOTTOM):
        return "bottom"
    if any(k in blob for k in _TOP):
        return "top"
    if any(k in blob for k in _BAG):
        return "bag"
    if any(k in blob for k in _HAT):
        return "hat"
    return ""


# 一次 build 里"件名 -> 类型"的缓存:build_piece_prompt 开头会按识别结果填一遍,
# 里面那些 helper 只拿到件名字符串,靠它才能用上识别结果(单线程构建,构建前会清空)。
_KIND_HINTS: dict[str, str] = {}

# 同理:件名 -> 读图给的长短(短 / 常规 / 长)。
_LEN_HINTS: dict[str, str] = {}

# 新件从哪一侧进场:"right" / "left"。建项目时按「第几件换上的」左右交替,
# 由 build_piece_prompt(entry_side=...) 写进来(和上面两个缓存一样,单线程构建)。
_ENTRY_SIDE = "right"


def kind_of(name: str, slot: str = "", desc: str = "") -> str:
    """件名 -> 类型:whole / outer / bottom / top / shoe / bag / hat / glasses / acc。"""
    if name in _KIND_HINTS:
        return _KIND_HINTS[name]
    k = _kind_from_hint(name, slot, desc)
    if k:
        return k
    n = _strip_piece_prefix(name)  # 拆件名的「套装-」前缀不算这一件的样子
    if any(x in n for x in _WHOLE):
        return "whole"
    if any(x in n for x in _GLASSES):
        return "glasses"
    if any(x in n for x in _SHOE):
        return "shoe"
    if any(x in n for x in _BAG):
        return "bag"
    if any(x in n for x in _HAT):
        return "hat"
    if any(x in n for x in _OUTER) or any(x in n for x in _OUTER_EXTRA):
        return "outer"
    if any(x in n for x in _BOTTOM):
        return "bottom"
    if any(x in n for x in _TOP):
        return "top"
    return "acc"


def _len_hint(name: str) -> str:
    """读图给的长短(短 / 常规 / 长);没认出来就给 ""。"""
    return _LEN_HINTS.get(str(name or ""), "")


def _slipper_def(i: int, e: dict, pics: list[int], role: str = "accessory") -> str:
    """「拖鞋」的定稿定义 —— **所有分镜统一用这一条**(2026-10-19 用户逐字给的原话)。

    重点在鞋头:圆润闭合、像小帽子一样盖住脚的前半部分(之前的"拖鞋"老被画成露趾凉拖)。

    **末尾那句按角色分**(2026-10-19 之后鞋也进换装链了):
      · accessory / worn —— 原话照写:一直戴在脚上、绝不摘下来;
      · old —— 此刻正穿着,直到新鞋贴上才换掉(写"绝不摘下来"会跟换鞋镜自己打架);
      · new —— 这一镜要落到脚上的那一双(不能写"一开场就戴着",模型会直接跳过换鞋)。
    """
    pic = _pic(pics) or "文字描述"
    txt = (
        f"<Prop {i}> 是「{e['name']}」:米色亚麻布料，**鞋头呈圆润的闭合状，"
        "鞋面一直延伸到脚背上方，像小帽子一样完全覆盖住脚的前半部分**。"
        "后跟完全敞开。内里浅驼色。"
        f" 外观、颜色、材质、版型和细节完全参考原图{pic}，必须完全一致。"
        "**它的位置只有双脚:两只脚各穿一只、整双都在、鞋头朝前、左右成对 —— "
        "不许拿在手里、不许放在脚边 / 地上、不许当摆件摆在她旁边。**"
    )
    if role in ("accessory", "worn"):
        txt += (
            " **它是「一直戴在她身上」的小配件:必须全程穿在双脚上 —— "
            "绝对不许摘下来,也绝对不许出现在双脚上以外的任何位置"
            "(不许拿在手里、不许放在脚边 / 地上、不许当摆件摆在她旁边)。**"
        )
    elif role == "old":
        txt += (
            " **她此刻双脚上正穿着这一双 —— 这一镜开始时它完整穿在双脚上,"
            "直到新鞋贴上、整双穿好之后才被换掉。**"
        )
    else:
        txt += " **这一双是她这一镜要落到双脚上的那一双:两只脚各穿一只、整双都在、鞋头朝前。**"
    return txt


def _shoe_shape(desc: str) -> str:
    """鞋型描述 —— **返回空,一律只写"和参考图一模一样"**(2026-10-19 用户定):

    「最开始的鞋子没有问题」时用的就是那句最短的「鞋型、鞋头、颜色、材质都和参考图
    里那双鞋一模一样」(项目 281~283 实测)。后来按用户口述补了「包头无后跟拖鞋 /
    鞋面是一整块翻毛皮」这种细描述,材质跟参考图对不上,鞋反而跟道具不一样了
    (用户:「现在鞋子跟道具不一样」)。所以这里不再生成的任何鞋型细节。
    """
    return ""


def _close_state(e: dict) -> str:
    """这一件的开合状态(件级 close > 全局默认);没前襟 / 没指定 → ""。"""
    st = str(e.get("close") or e.get("_close") or "").strip().lower()
    if st not in CLOSE_STATES:
        st = default_close(e.get("name") or "", e.get("slot") or e.get("_slot") or "")
    return st if st in CLOSE_STATES else ""


def _closed_outer(e: dict) -> bool:
    """这一件是不是**扣上的外套** —— 它已经是这一镜的新上衣,会顶掉里层那件。"""
    return kind_of(e.get("name") or "") == "outer" and _close_state(e) == "closed"


# 这一件穿上之后会**顶掉哪一层** —— 用来判这一镜是「换掉旧件」还是「加穿」。
#   · 外套默认只顶外套:外套是穿在别的衣服外面的 —— 打底长袖衫 + 敞开的外套是「加穿」;
#   · **扣上的外套**已经是这一镜的新上衣,连里层上衣一起顶掉 —— 不然提示词一边要求
#     "扣齐、不许露出里面的衣服",一边把旧上衣列成"整镜保持原样",模型只能演成敞怀露睡衣
#     (项目 tezsd99 第 5 镜);
#   · 上装 / 内搭 互相顶替(吊带背心换掉短袖衫是常见需求);
#   · 整身(连衣裙 / 连体衣)一件盖住上下半身,所以它同时顶「上装 + 下装」两层。
_REPLACE_OF = {
    "outer": ("outer",),
    "top": ("body",),
    "bottom": ("bottom",),
    "whole": ("body", "bottom", "whole"),
    # 鞋和鞋互相替换(旧拖鞋 → 新鞋)。以前鞋是「一直戴着的小配件」,没有这一层;
    # 现在新鞋会单独排一镜换装(用户 2026-10-19 要求),得能顶掉打底里那双旧鞋。
    "shoe": ("feet",),
}


def _replace_set(name: str, close: str = "") -> frozenset:
    """这一件能顶掉哪几层;配件 / 眼镜这类返回空集(它们谁都不顶)。

    外套默认只顶外套;``close == "closed"`` 时它已经是这一镜的新上衣,连 ``body`` 一起顶掉。
    """
    if kind_of(name) == "outer" and str(close or "").strip().lower() == "closed":
        return frozenset(("outer", "body"))
    return frozenset(_REPLACE_OF.get(kind_of(name), ()))


def _is_long(name: str) -> bool:
    """这件是不是「长款」—— 决定它飞进来时的大小锚。

    **先信读图给的长短**(短 / 常规 / 长),再退回按件名里的字猜。猜的时候要先把「长袖 / 短袖」
    抠掉:「长袖衫」的"长"是袖子长,不是衣长 —— 不抠就会把一件短上衣当成长大衣,尺寸锚整整大一圈。
    """
    lv = _len_hint(name)
    if lv:
        return lv == "长"
    n = str(name or "").replace("长袖", "").replace("短袖", "")
    return any(k in n for k in _LONG)


def _quant(name: str) -> str:
    # 包用「只」(整只包 / 一只包 都比"个"更像商品);"一{_quant}"就是「一只 / 一件 / 一双…」
    return {
        "whole": "件",
        "outer": "件",
        "top": "件",
        "bottom": "条",
        "shoe": "双",
        "bag": "只",
        "hat": "顶",
        "glasses": "副",
    }.get(kind_of(name), "件")


# ---- 「穿」还是「戴 / 挎」:包 / 帽子 / 眼镜 / 鞋**不是"穿在身上的衣服"** ----
# 写成「她穿着 … + 这个单肩包」「它必须是一整件穿在她身上的衣服(有衣身、有袖子)」时,
# 模型会当没这回事 —— 实测两个包镜(展示镜 + 换装镜)里包整条不见。
_CLOTH_KINDS = ("top", "outer", "bottom", "whole")


def _is_cloth(name: str) -> bool:
    """这件是**穿在身上的衣服**(上衣 / 外套 / 裤 / 裙 / 整身)吗?包 / 帽 / 眼镜 / 鞋 / 小件不算。"""
    return kind_of(name) in _CLOTH_KINDS


def _wear_verb(name: str) -> str:
    k = kind_of(name)
    if k == "bag":
        return "挎"
    if k in ("hat", "glasses", "acc"):
        return "戴"
    return "穿"


def _whole_of(name: str) -> str:
    """整**只** / 整**顶** / 整**副** / 整**双** / 整**件**。"""
    return {"bag": "整只", "hat": "整顶", "glasses": "整副", "shoe": "整双"}.get(
        kind_of(name), "整件"
    )


def _on_body_where(name: str) -> str:
    """它在她身上是「挎 / 戴 / 穿」在哪儿。

    **包不能说"挎在肩上 / 提在手里"这种二选一**:模型会挑中间那个 —— 包悬在她肩旁飘着、
    既没挎上也没提在手里(用户报"根本没有挎上去啊")。落点只能是一个确定的位置。
    """
    return {
        "bag": "挎在她的肩上 —— 肩带搭在她的肩上、包身贴着她的体侧、垂在她的上臂外侧",
        "hat": "戴在头上",
        "glasses": "戴在眼睛上(镜腿架在耳朵上)",
        "shoe": "穿在双脚上",
        "acc": "戴在身上",
    }.get(kind_of(name), "带在身上")


def _on_where(name: str) -> str:
    return {
        "whole": "身上",
        "outer": "上半身",
        "top": "上半身",
        "bottom": "下半身",
        "shoe": "脚上",
        "bag": "肩上",
        "hat": "头上",
        "glasses": "脸上",
    }.get(kind_of(name), "身上")


def _place_txt(name: str) -> str:
    return {
        "glasses": "(戴在眼睛上、镜腿架在耳朵上)",
        "shoe": "(穿在双脚上)",
        "hat": "(戴在头上)",
        "bag": "(挎在右肩上 —— 肩带平搭在肩头、包身垂在右侧胯部)",
        "acc": "(戴在她身上)",
    }.get(kind_of(name), "(戴在她身上)")


def _wear_where(name: str, close: str = "") -> str:
    """一句话说清这件穿在哪儿 —— 「清点」那一句用,免得模型把某件漏掉。"""
    if kind_of(name) == "outer":
        return {
            "closed": "穿在最外面一层、前襟合上",
            "half": "穿在最外面一层、前襟半开",
            "open": "穿在最外面一层、前襟敞开",
        }.get(str(close or "").strip().lower(), "穿在最外面一层、前襟敞开")
    return {
        "glasses": "戴在脸上",
        "hat": "戴在头上",
        "shoe": "穿在双脚上",
        "bag": "挎在右肩上",
        "bottom": "穿在腰胯",
        "top": "贴身穿着",
        "whole": "整身穿好",
        "acc": "戴在身上",
    }.get(kind_of(name), "穿在身上")


# 单肩包的「穿好状态」——只写"挎在她身上",模型会把它拿在手里 / 挂在手肘上 / 干脆不画
# (用户 2026-10-20 报「单肩包·穿好展示 这个包怎么没有挎上」)。肩带跨哪只肩、包身垂在哪、
# 手不许碰,三条都要写明。展示镜和换装镜共用这一段,保证两张状态图挎法一致。
_BAG_WORN = (
    "肩带跨过她的右肩(观众视角画面左侧的那只手那边)、平搭在肩头;"
    "包身垂在她身体右侧的胯部高度、贴着体侧,整只包完整可见;"
    "双手自然垂着、不去扶包"
)


# ---------------- 换装风格(2026-10-20 新增第二种) ----------------
# throw = 画外有人把衣服朝她扔过来,碰到身体就穿好(原有玩法,默认);
# pull  = 开场一把扯掉初始衣服、只剩固定内搭(短上衣 + 短裤);整套要换的衣服**无人穿着、
#         等距悬空排成一列**在她身侧,她一件件从那一列里扯下来、拉到自己身上。
PIECE_STYLE_THROW = "throw"
PIECE_STYLE_PULL = "pull"
PULL_INNER_TOP = "短上衣"  # 扯掉初始衣服后剩下的固定内搭(不带参考图,只写进提示词)
PULL_INNER_BOT = "短裤"
# 固定内搭的**外观**也是写死的(用户 2026-10-20:两件都要黑色;短上衣不要吊带那种,
# 要无肩带 / 抹胸式)。这里改一处,建项目时注入的 desc 和重置时用的 desc 都跟着变。
PULL_INNER_TOP_DESC = "黑色的贴身短上衣(无肩带、抹胸式,没有肩带和吊带,下摆到腰)"
PULL_INNER_BOT_DESC = "黑色的贴身短裤(裤脚到大腿中部)"


# 新衣服怎么进场 —— **定稿是「被人从画外扔过来」**(2026-10-15 用户定,项目 185 验证)。
# 走过的三条弯路,都不要再写回去:
#   · "飞进画面 / 甩进画面" → 模型演成"朝镜头扑过来、先变大再缩小"(衣服糊满整屏、比人还大);
#   · "整件平行地平移 / 像贴纸滑过来" → 位置对了,但像一块硬纸板平移,用户报"没有衣服的效果、不起浮";
#   · "整件一起飘" → 布料软了,但飘得没方向感、也不像有人扔。
# 现在这套 = **速度感(被扔) + 布料软(甩开 / 起浮) + 尺寸锚(始终等于她身上这一件) + 不朝镜头飞**。
#
# 进场位置:上装 / 外搭从**右上角、往下约 30% 处的外侧**,下装从**右下角、往上约 30% 处的外侧**
# (2026-10-19 用户定:角落要按画面高度挪 30%,不能正好贴着角)。
# 左侧同理:左上角往下 30%、左下角往上 30%(留给以后要换侧的情况)。
#
# 这条同时写进 defs(_fly_shape)和动作说明(detailed_description)——**两处必须用同一份文案**,
# 实测 defs 权重更高,只改一处的会被另一处带跑。
def _move_in(name: str) -> str:
    """新衣服的进场方式(defs 用):从角落外侧被扔过来 + 布料软 / 会甩 + 尺寸锚 + 不朝镜头飞。"""
    bottom = kind_of(name) == "bottom"
    # 起点不在正角落:下装从「下角往上约 30%」、上装 / 外套从「上角往下约 30%」的画外起飞。
    # 左右按件交替(见 _ENTRY_SIDE):第 1 件右上 / 第 2 件左上 / 第 3 件右上…
    _side = "左" if _ENTRY_SIDE == "left" else "右"
    # 除了"角落挪 30%"，再给一个具体高度锚 —— 只写百分比模型基本无视
    # (2026-10-19 用户报「右下角往上约 30% 外侧怎么不生效」)。
    corner = (
        f"{_side}下角**往上约 30%** 处(大约在她大腿的高度)"
        if bottom
        else f"{_side}上角**往下约 30%** 处(大约在她肩膀的高度)"
    )
    if bottom and _is_long(name):
        size = (
            "它的大小始终 = 她身上这一条的大小(腰头到脚踝、和她的腿一样长;"
            "在画面里大约占画面宽度的 1/4、高度的 1/3)"
        )
    elif bottom:
        # **短款不能再写"腰头到脚踝"** —— 短裙 / 短裤会被按长款的尺寸画成一块大布
        # (2026-10-19 用户报「这个衣服飘的时候怎么这么大」:格纹短裙飞过来有半个画面大)。
        _hem = "腰头到裙摆" if "裙" in name else "腰头到裤脚"
        size = (
            f"它的大小始终 = 她身上这一条的大小({_hem},只到大腿中部,"
            "宽度大约等于她的胯宽;在画面里大约占画面宽度的 1/4、高度的 1/4)"
        )
    else:
        size = (
            "它的大小始终 = 她身上这一件的大小(宽度大约等于她的肩宽;"
            "在画面里大约占画面宽度的 1/4、高度的 1/4)"
        )
    return (
        "它是**被人从画面" + corner + "外侧扔向她**的:飞行中布料本身是软的、"
        # 不要写"必须有起伏和甩动 / 甩出波浪状" —— 模型会把裙子当成一块**摊开的布**
        # 画成半幅宽(用户报「怎么这么大」)。只留轻微摆动,形状必须保持。
        "衣身有轻微自然的摆动、下摆和衣角轻轻晃动、被风带出几道自然褶皱;"
        # 正向写「保持完整形状」就够;不要写"不是一块摊开的布" —— 否定词反而会把那个概念喂给模型
        "**它从头到尾都是她身上这一件衣服原本的完整形状**;"
        "**绝不许像一块硬纸板 / 一张平铺的产品图那样僵直**;"
        + size
        + ",绝不许因为它朝她飞过来就画得比她还大;"
        "**绝对不许朝镜头方向飞、不许凑近镜头、不许先变大再缩小、不许糊住画面**"
    )


def _fly_shape(name: str) -> str:
    """扔进来的那件:进场方式 + 尺寸锚(按类型 / 长短分)。

    注意:**不要再写"到达时是一整件摊开的衣服(大约从她的肩到胯那么大)"这种尺寸描述** ——
    模型会照字面把衣服**摊平画大**(实测用户报"衣服怎么这大 / 这么长")。尺寸只留 `_move_in()` 里
    那一句"始终 = 她身上这一件的大小"。
    """
    k, long_ = kind_of(name), _is_long(name)
    if k == "shoe":
        return (
            "到达之前一整双一起平移过来,**始终正面朝上、鞋头朝前、左右成对不分开**;"
            "**它的大小 = 她脚上那双的大小**"
        )
    if k == "bag":
        return (
            "整只包一起平移过来,**不翻转、不翻滚**;"
            "**它的大小 = 真人手里 / 肩上挎着的那个大小,绝不许占满画面**"
        )
    if k == "hat":
        return "整顶帽子一起平移过来,**帽顶朝上、不翻转、不翻滚**"
    if k == "glasses":
        return (
            "整副一起平移过来,**镜腿朝后、上下不翻转、不翻滚**;"
            "**它的大小 = 她脸上戴着的那副大小,绝不许占满画面**"
        )
    if k == "acc":  # 围巾 / 项链 / 腰带 这类小件
        return (
            "整件一起平移过来,**不翻转、不翻滚、也不许缠成一团**;"
            "**它的大小 = 真人戴着的那个大小,绝不许占满画面**"
        )
    # whole / bottom / top / outer:都是**被人从画外扔过来**(见 _move_in)
    return _move_in(name)


def _from_where(name: str) -> tuple[str, str, str]:
    """(部位说法, 从哪飞来 / 停在哪, 落上去之后长什么样)"""
    k = kind_of(name)
    if k == "shoe":
        return (
            "脚",
            "先在画面下方、大约和她膝盖齐高的位置",
            "整双套在她的两只脚上、鞋跟对脚跟",
        )
    if k == "bag":
        # 包**不能停在"手上 / 半空"**:实测结尾那帧包悬在她右肩旁边飘着、根本没挎上(用户报
        # "根本没有挎上去啊")。落点必须直接写"落到她身上 + 贴住体侧"。
        return (
            "肩上",
            "先在画面侧后方",
            "整只挎在她的肩上 —— 肩带搭在她的肩上、包身贴着她的体侧、垂在她的上臂外侧",
        )
    if k == "hat":
        return "头上", "先在画面上方", "整顶扣在她的头上、帽檐朝前"
    if k == "glasses":
        return (
            "脸上",
            "先在画面上方、大约和她额头齐高的位置",
            "整副戴在她的眼睛上、镜腿架在耳朵上",
        )
    if k == "acc":
        return "身上", "先在画面上方", "整件戴在她身上、贴着她的身体"
    if k == "whole":
        return (
            "身上",
            "先在画面右侧、大约和她肩膀齐高的位置",
            "领口套在她脖子上、整件罩住她的身体、下摆到膝盖",
        )
    if k == "bottom":
        return (
            "下半身",
            "先在画面右侧、大约和她腰胯齐高的位置",
            (
                "腰头环绕她的腰、裙身包住她的臀和大腿上部、她的腿从裙摆下面伸出来"
                if "裙" in name
                else "腰头环绕她的腰、裤腿包住她的两条腿"
            ),
        )
    return "上半身", "先在画面右上方", "领口环绕她的脖子、衣身包住她的躯干"


def _part_where(name: str) -> str:
    """它落到她身上哪一块(动作段用):下半身 / 上半身 / 身上。"""
    k = kind_of(name)
    return "下半身" if k == "bottom" else "上半身" if k in ("top", "outer") else "身上"


def _size_anchor(name: str) -> str:
    """「飞在空中的大小 = 她身上这一件的大小」—— **必须写在动作段**(defs 里没有飞行说明)。

    只写"照参考图的版型"不够:参考图是商品图(衣服摊开、占满整幅),模型会照抄它的占屏比例,
    下装就被画成一块到膝盖以下的硬板、把腿整个盖住(2026-10-20 用户报「腿都没有了」)。
    """
    k = kind_of(name)
    if k == "bottom":
        if _is_long(name):
            return (
                "它的大小始终 = 她身上这一条的大小(腰头到脚踝、和她的腿一样长;"
                "在画面里大约占画面宽度的 1/4、高度的 1/3);"
            )
        _hem = "腰头到裙摆" if "裙" in name else "腰头到裤脚"
        return (
            f"它的大小始终 = 她身上这一条的大小({_hem},**只到大腿中部**,"
            "宽度大约等于她的胯宽;在画面里大约占画面宽度的 1/4、高度的 1/4),"
            "**它的下沿停在她大腿中部,两条腿从下面伸出来、看得见**;"
        )
    if k == "whole":
        return (
            "它的大小始终 = 她身上这一件的大小(从肩到膝盖;"
            "在画面里大约占画面宽度的 1/4、高度的 1/2);"
        )
    return (
        "它的大小始终 = 她身上这一件的大小(宽度大约等于她的肩宽;"
        "在画面里大约占画面宽度的 1/4、高度的 1/4);"
    )


def _cloth_flight_txt(name: str, pro: str, part: str) -> str:
    """衣服的飞入段(动作段)。

    ① 形状:软布料 + 完整轮廓(**不写"一整件摊开的衣服 / 整件展开"** —— 会被画成半幅宽巨布);
    ② 尺寸锚:必须写,而且只写到该到的部位(下装到大腿中部,腿要露出来);
    ③ 落地:贴到该穿的部位就整件穿好(硬切,没有中间态)。
    """
    return (
        f"它是**一整件软布料做的「{name}」**(有完整的轮廓、一眼看得出就是这一件,"
        "照参考图那件的版型;腰头 / 衣身 / 裙摆或裤腿都保持它原本的形状),"
        "飞行时布料是软的、有自然的垂坠和轻微摆动:衣角和下摆轻轻晃;"
        + _size_anchor(name)
        + f"它飞到{pro}身前之后**直接贴到{pro}的{part}上** —— "
        "**贴上身体的那一瞬间它就已经整件穿好了,这是硬切、不是渐变:"
        "动作要快、一路不停,不许停在半路、不许出现中间态。**\n"
    )


def _style_block(pic: int, bg_pic_txt: str = "") -> str:
    _bg = f"背景以背景参考图 {bg_pic_txt} 为准。" if bg_pic_txt else "背景一律纯白。"
    return (
        f"<Picture {pic}> 是这套衣服的**实拍穿着参考**:"
        "**只参考它身上这套衣服的款式 / 颜色 / 材质 / 垂坠感和穿法**;"
        "**绝对不要参考画面里的脸 / 头发 / 身材 / 皮肤 / 姿势 / 背景** —— "
        "她的脸、发型、身材、还有身上露出来的部分(肩 / 腰 / 腿)一律以 <Picture 1> 为准,"
        "绝对不许把参考图里露出的皮肤 / 光腿照搬进来," + _bg + "\n"
        "**服装参考图是一张特写裁片(衣服占满整张图),不要照它在参考图里占满画面的比例放大**;"
        "**这是「实拍一个空间」,不是拼贴:不要出现隐形人台 / 衣架 / 挂钩,"
        "不要出现分格、方块或第二层背景。**"
    )


def _pronoun_of(person_desc: str) -> str:
    """人物描述里读性别 —— 读不到就用中性的「模特」。

    以前只认「男性 / 女性 / 男生 / 女生 / 男士 / 女士 / 男孩 / 女孩」这几个完整词,
    描述写成「一名年轻男子」「少女感」就认不出来,全篇变成"模特"。
    现在按「男 / 女」这两个字判 —— 人物描述里出现它们的场合基本只有性别。
    """
    d = str(person_desc or "")
    if "男" in d:
        return "他"
    if "女" in d:
        return "她"
    return "模特"


def _pic(nums) -> str:
    return " / ".join(f"<Picture {n}>" for n in (nums or []))


def _names_txt(names) -> str:
    """「A」「B」—— 把件名列表拼成提示词里的说法(照样剥掉「套装-」前缀)。"""
    return "、".join(f"「{_strip_piece_prefix(n)}」" for n in (names or []))


def _bottom_txt(name: str) -> str:
    """下装的形状 / 长度约束。

    展示镜也要写这条:实测参考图是一条**到脚踝的阔腿长睡裤**,生成出来的却是同花色的
    **荷叶边短裤** —— 模型会把下装"压缩"。长裤必须点明到脚踝、绝不许变短裤。
    """
    if "裙" in str(name or ""):
        return (
            f"**「{name}」必须一眼看出是「半裙」**:腰头清楚、裙摆和两条腿分开、下摆有开口,"
            "长度和参考图里那条一样;**绝不许连成一整片裹住双腿的长布、不许变成连衣裙**。"
        )
    return (
        f"**「{name}」必须一眼看出是「两条分开的裤腿」**:腰头清楚、左右两条裤腿各自垂下来、"
        "膝盖以下明显分开;**长度必须和参考图里那条一模一样 —— 参考图是长裤就画长裤,"
        "裤脚到脚踝,绝不许画成短裤 / 五分裤**;也不许连成一整片没有分叉的长布、"
        "不许变成连衣裙或长裙。"
    )


# ---------------- 取景标定(人物在画面里多大) ----------------
# 只写"头顶上方留一点空"这种模糊说法时,模型会把画面塞满 —— 实测人高 0.89、头顶只留 0.07,
# 跟项目里那套"链头 70% 取景"的既定标定(见 .env 的 FLOAT_ALIGN_HEIGHT)差了一大截。
# 模型认数字不认形容词,所以这里把「占画面多高 / 上下各留多少」写死。要调大小改这三个值。
_FRAME_H = 0.70  # 人物全身占画面高的比例(镜头拉远后的目标;和状态图 70% 对齐)
_FRAME_TOP = 0.15  # 头顶离画面上沿的比例
_FRAME_BOT = 0.15  # 脚底离画面下沿的比例


def build_piece_prompt(
    *,
    title: str,
    index: int,
    total: int,
    props: list[dict],
    wear_ref_pics: list[int] | None = None,
    person_desc: str = "",
    swap: tuple[list[str], list[str]] | None = None,
    first_change: bool = False,
    last_change: bool = False,
    prev_pic: int = 0,
    entry_side: str = "right",
    style: str = PIECE_STYLE_THROW,
    strip_base: bool = False,
) -> str:
    """生成一个「按件排镜」镜头的完整提示词(展示镜 / 换装镜共用)。

    **结构固定成一条线性叙事,每个约束只出现一次**(2026-10-20 重写):
      镜头 → 画面(机位 / 取景 / 场景) → 人物(长相 / 站姿 / 手臂 / 穿着边界)
      → 道具定义 → 本镜穿着 / 换装顺序 → 动作 → 收尾 → 声音

    重写原因:以前是一段段往上加补丁,再由 `concise.rule_condense` 后处理 ——
    精简器会把姿态句抽到最前面、还会按"前 3 句 + 关键词"丢句子,结果提示词又长又乱,
    同一句约束出现 2~3 次(用户 2026-10-20:「我总感觉你的提示写得非常乱」)。
    现在这份**直接就是最终提示词**,不再过精简器。

    props 每项:{name, pics(list[int]), role: new|old|worn|accessory|scene,
                slot(读图给的部位), len(读图给的长短), desc(外观描述), close(开合)}
    swap:展示镜 —— (这一镜换掉的, 这一镜换上的)。
    first_change / last_change:第一件 / 最后一件的换装镜(决定手臂与头发的写法)。
    """
    global _ENTRY_SIDE
    _ENTRY_SIDE = "left" if str(entry_side).strip().lower().startswith("l") else "right"
    _pull = str(style or "").strip().lower() == PIECE_STYLE_PULL
    wear_ref_pics = wear_ref_pics or []
    swap = swap or ([], [])
    # 拆件名「套装-阔腿裤」的前缀进提示词是无意义的噪音(模型还可能读成"一整套飞进来")——
    # 统一在入口剥掉,后面的判定 / 文案 / 参考图对应都用干净的名字。
    for _e in props:
        if _e.get("name"):
            _e["name"] = _strip_piece_prefix(_e["name"])
    # 先用**识别结果**(读图给的槽位 / 长短 + 描述)把"件名 -> 类型"定下来;
    # 这样雪服 / 汉服 / JK 制服这种没见过的名字也能判对。
    _KIND_HINTS.clear()
    _LEN_HINTS.clear()
    for _e in props:
        _nm = _e.get("name") or ""
        if not _nm:
            continue
        _k = _kind_from_hint(_nm, _e.get("slot") or "", _e.get("desc") or "")
        if _k:
            _KIND_HINTS[_nm] = _k
        _lv = str(_e.get("len") or "").strip()
        if _lv:
            _LEN_HINTS[_nm] = _lv

    pro = _pronoun_of(person_desc)
    new = [e for e in props if e.get("role") == "new"]
    old = [e for e in props if e.get("role") == "old"]
    worn = [e for e in props if e.get("role") == "worn"]
    # **没绑到参考图的配饰一律不写进提示词**(模型只能照件名凭空画)。
    accs = [e for e in props if e.get("role") == "accessory" and (e.get("pics") or [])]
    scenes = [e for e in props if e.get("role") == "scene"]
    bg_scene = next((e for e in scenes if (e.get("pics") or [])), None)
    bg_pic_txt = _pic(bg_scene.get("pics") or []) if bg_scene else ""
    is_display = not new
    ordered = (worn + old + accs) if is_display else (new + accs + worn + old)
    tgt = new[0] if new else None
    _nm = tgt["name"] if tgt else ""
    _carried = bool(_nm) and not _is_cloth(_nm)
    _is_bag = _carried and kind_of(_nm) == "bag"
    _shoe = bool(_nm) and kind_of(_nm) == "shoe"
    # 展示镜里**这一镜新挎上的包**(swap 的"换上新件"那一半)—— 给它单独写「拿起来挎上肩」的动作。
    # 纯静态写"已经挎好"会被"照抄上一帧"压掉:实测包整场不出现(用户 2026-10-20)。
    _disp_bag = ""
    if is_display and swap[1]:
        _disp_bag = next((n for n in swap[1] if kind_of(n) == "bag"), "")

    L: list[str] = []
    L.append(f"【镜头】第 {index} / {total} 镜 · {title}")

    # ---------------- 画面 ----------------
    L.append("\n【画面】")
    L.append("- 机位:固定三脚架,全程零推拉摇移、零变焦。")
    if bg_scene:
        L.append(
            f"- 场景:以 {bg_pic_txt} 为准 —— 墙面、地面、陈设、大小和透视都照它,"
            "人物合成进这个真实空间里,不缩放背景。"
        )
        L.append("- 取景:全景全身;脚和鞋完整在画面里,头顶上方留出一段明显空白。")
    else:
        L.append("- 场景:纯白背景,柔和均匀的棚拍光,没有其他陈设。")
        L.append(
            f"- 取景:全景全身,{pro}占画面高度约 {_FRAME_H:.0%}"
            f"(头顶离上沿约 {_FRAME_TOP:.0%}、脚底离下沿约 {_FRAME_BOT:.0%});"
            "双脚和鞋完整在画面里。"
        )
        # 同一句意思再写一遍**正向英文**画面描述:链头那一镜没有首帧锚,模型会自己取景,
        # 中文的百分比常被无视(实测 p312 s01 画到 88% 高、脚还横移 30px,p299 同一句是 80%)。
        # 「wide full-body shot / camera pulled back / empty space」都在训练分布里,
        # 比"不要拉近 / 别撑满画幅"这种否定句稳(否定句会反噬)。
        L.append(
            "- Camera: static tripod, wide full-body shot; the camera is pulled far back, "
            "with a clear band of empty white space above her head and below her feet. "
            f"She occupies only about {_FRAME_H:.0%} of the frame height and stays centred in frame "
            "from the first frame to the last."
        )

    # ---------------- 人物 ----------------
    L.append("\n【人物】")
    L.append(
        "- 长相 / 发型 / 妆容:只以 <Picture 1> 为准;参考图里的内衣、打底小衣和露出的肩腰腿不照搬。"
    )
    # 皮肤质感只写"干净、均匀、细腻" —— 之前写过「真实毛孔 / 自然光泽」,
    # 在 640×1152 下模型会把毛孔画成红紫色块(2026-10-20 实测膝盖/胸口出现瘀斑感),
    # 纹理细节交给超分和细节 LoRA,提示词不主动要求。
    L.append("- 皮肤:细腻、肤色均匀,脸 / 脖子 / 手臂 / 腿的肤色一致。")
    if _pull and not is_display:
        # 固定内搭是黑色的(用户 2026-10-20)—— 不写这句,模型会照抄人物参考图里那身浅色内搭。
        L.append(
            f"- 打底:她贴身穿的是**黑色的「{PULL_INNER_TOP}」和黑色的「{PULL_INNER_BOT}」**"
            "(无肩带、抹胸式;人物参考图里那身浅色内搭不照搬)。"
        )
    L.append(
        "- 站姿:直立站定,双脚平行朝前、钉在同一个点;肩膀水平,下巴与地面平行;"
        "脸整个正对镜头、两只眼睛都在画面里,视线始终在镜头方向。"
    )
    L.append(
        "- 生命感:呼吸自然轻微、偶尔眨眼;手是真实放松的手 —— 掌心朝内、手指并拢、指节自然微屈。"
    )
    if _pull and not is_display:
        # 「扯上身」风格(2026-10-20 定稿):她站在正中不动,整套衣服分成两簇悬在她
        # **右上方 / 左上方**,像下雨一样从上往下落到她身上 —— 不写"伸手去扯 / 勾手指"。
        _start = (
            "开始时两条手臂自然垂在身体两侧" if first_change else "开始时双臂保持微张"
        )
        if len(new) >= 2:
            _end = "整身一次穿好之后,两条手臂自然垂下(手臂贴着身侧、手指放松),保持到最后一帧。"
        elif last_change:
            _end = "这一件穿好之后,两条手臂自然垂下(手臂贴着身侧、手指放松),保持到最后一帧。"
        elif first_change:
            _end = "这一件穿好之后,双臂向外张开到微张位置(手掌离胯约 10cm、掌心朝内、手指并拢、指节微屈)并保持。"
        else:
            _end = "这一件穿好之后,双臂回到微张的姿势并保持。"
        _bag_multi = any(kind_of(e["name"]) == "bag" for e in new)
        # 有包时**手臂最后还要抬一次去接**:不能写成"穿好就垂到底、保持到最后一帧",
        # 否则和「最后抬手接包」打架(实测会演成手一直垂着、包凭空挂上)。
        _end_use = (
            _end
            if not (_bag_multi and len(new) >= 2)
            else "挎好之后她松开手,两条手臂再自然垂在身体两侧"
            "(手臂贴着身侧、手指放松),保持到最后一帧。"
        )
        L.append(
            f"- 手臂:{_start};"
            + (
                "她站在画面正中、身体保持不动(只有呼吸和很轻微的重心变化),"
                "两条手臂自然垂在身体两侧、手指放松,"
                "让衣服自己从上方飘落到她身上;"
                + (
                    "最后包被画外的人扔过来时,她**抬起右手把它接住、把肩带越过右肩**;"
                    if _bag_multi
                    else ""
                )
                if len(new) >= 2
                else "她抬起右臂,把它从那一列里扯下来、带到自己身前;"
            )
            + _end_use
        )
    elif strip_base:
        # 开场镜:她要用双手把初始那套扯掉,不能写成"一直垂着不动"。
        L.append(
            "- 手臂:开始时两条手臂自然垂在身体两侧、手能碰到衣襟和下摆;"
            "扯掉之后两条手臂自然垂回身体两侧,保持到最后一帧。"
        )
    elif _is_bag:
        L.append(
            "- 手臂:全程站定;只允许右臂做「接包 → 挎上右肩」这一下,左臂自然垂在身侧;"
            "挎好之后右臂也自然垂回身侧 —— 两条手臂垂在身体两侧,直到最后一帧。"
        )
    elif is_display:
        # 展示镜的手臂姿势要和**上一张状态图**一致,否则链上的姿势会跳:
        #   · 链头 S0(没有 swap):手臂自然垂着;
        #   · 第一件换好那镜(first_change):从垂着张开一次,之后保持;
        #   · 后面每一镜(包括换鞋 / 加挎):**保持微张**。
        # 以前这里写了 all(_is_cloth(...)) —— 换鞋 / 加挎的展示镜就被排除成"自然垂在身侧",
        # 于是 s08 / s10 的手臂从张开变回下垂,状态图链上的姿势断了(用户 2026-10-20 发现)。
        if _disp_bag:
            # 展示镜就是**静态展示**:不加"拿包 / 挎上肩"的动作(用户 2026-10-20 明确),
            # 只把"已经挎在右肩上"写细(_BAG_WORN:肩带在肩头、包身在右侧胯部、手不扶包)。
            L.append(
                "- 手臂:两条手臂自然垂在身体两侧(手臂贴着身侧、手指放松),"
                "从第一帧到最后一帧都是这个姿势。"
            )
        elif swap[1] and last_change:
            # 最后一个状态:这一镜的末帧就是最后一张状态图 —— 手臂要**自然垂下**,
            # 和最后那个换装镜的收尾一致(用户 2026-10-20:换完了手要垂直下来)。
            if first_change:
                # 只有一件(既是第一件也是最后一件):链头 S0 本来就是垂着的,全程保持垂下。
                L.append(
                    "- 手臂:全程自然垂在身体两侧(手臂贴着身侧、手指放松),"
                    "从第一帧到最后一帧都是这个姿势。"
                )
            else:
                L.append(
                    "- 手臂:开始时双臂微张;随后两条手臂自然放下、垂在身体两侧"
                    "(手臂贴着身侧、手指放松),并保持到最后一帧。"
                )
        elif swap[1] and first_change:
            L.append(
                "- 手臂:自然垂在身侧、略微离开身体;这一件穿好之后,双臂向外张开到微张位置"
                "(手掌离胯约 10cm、掌心朝内、手指并拢、指节微屈,臂与躯干之间留出缝隙)并保持。"
            )
        elif swap[1]:
            L.append(
                "- 手臂:双臂保持微张的姿势 —— 手掌离胯约 10cm、掌心朝内、手指并拢、"
                "指节微屈,手臂与躯干之间留出缝隙;从第一帧到最后一帧都是这个姿势。"
            )
        else:
            L.append("- 手臂:自然垂在身侧、略微离开身体。")
    elif first_change and not last_change:
        L.append(
            "- 手臂:开始时自然垂在身侧;衣服整件穿好之后向外张开一次到微张位置"
            "(手掌离胯约 10cm、掌心朝内、手指并拢、指节微屈,臂与躯干之间留出缝隙),之后保持。"
        )
    else:
        # 最后一件换装:穿好之后手臂**自然垂下**(用户 2026-10-20 要求:换完了手要下来、垂直下来)。
        # 前面的换装镜一直保持微张;最后一镜收尾时放下,整条片子以自然站姿结束。
        if last_change and first_change:
            # 只有一件(既是第一件也是最后一件):全程自然垂下,不用先张开再放下。
            L.append(
                "- 手臂:全程自然垂在身体两侧、略微离开身体(手臂贴着身侧、手指放松)。"
            )
        elif last_change:
            L.append(
                "- 手臂:开始时双臂微张(手掌离胯约 10cm、掌心朝内、手指并拢、指节微屈);"
                "这一件整件穿好、完全服帖之后,两条手臂自然放下、垂在身体两侧"
                "(手臂贴着身侧、手指放松),并保持到最后一帧。"
            )
        else:
            L.append(
                "- 手臂:保持微张(手掌离胯约 10cm、掌心朝内、手指并拢、指节微屈);"
                "衣服贴好的一瞬间手有一次很轻的反应(抬起约 3cm、掌心略朝前、手指轻开),随后回到原位。"
            )
    L.append(
        "- 穿着边界:她身上只出现本镜列出的这几件;不能出现清单之外的内衣 / 胸衣 / "
        "裸露的胸口 / 腰腹;衣服盖住的地方必须是盖住的。"
    )
    # **换鞋时身高不能跳**(用户 2026-10-20:换上高跟鞋人会突然变高,很怪)。
    # 模型会把"鞋跟高了"演成"腿变长 / 踮脚 / 整个人拔高" —— 这里钉死头顶高度和腿长不变,
    # 鞋跟的高度只体现在鞋本身,不体现在身体上。
    _all_names = [e.get("name") or "" for e in ordered]
    if not strip_base and any(kind_of(n) == "shoe" for n in _all_names if n):
        L.append(
            "- 身高:整段她的头顶高度和腿长都和上一镜一致 —— 换鞋只是脚上那双鞋换了,"
            "身体不拉伸、腿不变长、不踮脚;画面上的人物高度从头到尾一样。"
        )

    # ---------------- 道具定义 ----------------
    L.append("\n【道具定义】")
    for i, e in enumerate(ordered, start=1):
        pics = e.get("pics") or []
        role = e.get("role")
        ref = f"参考图 {_pic(pics)}" if pics else "上面的文字描述"
        d = str(e.get("desc") or "").strip().rstrip("。")
        k = kind_of(e["name"])
        q = _quant(e["name"])
        # 鞋:不写长描述,只写"和参考图一模一样"(用户 2026-10-19:提示要短)。
        if k == "shoe" and pics:
            d = "鞋型、鞋头、颜色、材质都和参考图里那双鞋一模一样"
        # 下装:长短必须写死(模型会把长裤压成短裤 / 把短裙拉成长裙)。
        if k == "bottom":
            _lv = _len_hint(e["name"])
            if _lv in ("长", "短"):
                d += (";" if d else "") + f"{_lv}款(长短与参考图完全一致)"
        d_txt = f":{d};" if d else ":"
        close_txt = _fasten_of(e).rstrip("。")
        # 拖鞋:所有分镜统一用用户给的定稿定义(开场"扯掉旧衣"那镜除外 —— 那段写着
        # "直到新鞋贴上才被换掉",和"一把扯掉"打架)。
        if k == "shoe" and "拖鞋" in e["name"] and pics and not strip_base:
            L.append("- " + _slipper_def(i, e, pics, role=role or "accessory"))
            continue
        # 外套的穿法跟着开合走:**扣上的外套就是这一镜的新上衣** —— 这里再写死"前襟敞开"
        # 会和上面的扣齐约束打架,模型会演成敞怀露睡衣(项目 tezsd99 第 5 镜)。
        if k == "outer":
            _st = _close_state(e)
            if _closed_outer(e):
                _new_txt = (
                    "这一镜要换上的就是它。这是她这一镜的**上衣**:里面不再有别的上衣,"
                    "前襟按上面的扣法合上、两只袖子都穿在她的手臂上。"
                )
            elif _st == "half":
                _new_txt = (
                    "这一镜要换上的就是它。这是独立的一层外套:"
                    "穿在里层衣服外面(第二层)、前襟半开、两只袖子都在她的手臂上。"
                )
            elif _st == "open":
                _new_txt = (
                    "这一镜要换上的就是它。这是独立的一层外套:"
                    "穿在里层衣服外面(第二层)、前襟敞开、两只袖子都在她的手臂上。"
                )
            else:
                _new_txt = (
                    "这一镜要换上的就是它。这是独立的一层外套:"
                    "穿在里层衣服外面(第二层)、两只袖子都在她的手臂上。"
                )
        else:
            _new_txt = "这一镜要换上的就是它。"
        tail = {
            "new": _new_txt,
            # **不要在这里写"会被扯掉 / 会消失"**:defs 的权重比动作段高,模型读到这句
            # 会直接从第 1 帧就不画它(实测 p306:开场镜第 1 帧就只剩内搭,旧衣一帧都没出现)。
            # "什么时候离开"只写在下面的动作段里。
            "old": (
                f"{pro}此刻{_on_where(e['name'])}正穿着这一{q},是她开场这一身的一部分(完整、不透明)。"
                if strip_base
                else f"{pro}此刻{_on_where(e['name'])}正穿着这一{q},直到新件穿好才换下。"
            ),
            "worn": "她身上穿着这一件,整镜保持原样。",
            # 一直戴着的小配件(墨镜…) + 默认穿着的鞋:defs 权重最高,这里点名"第一帧就有",
            # 压住"从空中落下来 / 干脆不画"(用户 2026-10-20 实测墨镜整场不见、鞋没穿上)。
            "accessory": f"**第一帧就已经{_place_txt(e['name']).strip('()')}**,整段保持原样。",
        }.get(role or "", f"全程{_place_txt(e['name']).strip('()')}。")
        L.append(
            f"- <Prop {i}>「{e['name']}」{d_txt}外观 / 颜色 / 材质 / 版型以{ref} 为准;"
            + (f"{close_txt};" if close_txt else "")
            + tail
        )
    if bg_scene:
        _sd = str(bg_scene.get("desc") or "").strip().rstrip("。")
        L.append(
            f"- <Scene 1>「{bg_scene.get('name') or '人物背景'}」"
            + (f":{_sd};" if _sd else ":")
            + f"空间、陈设、墙面地面、色调光线都以 {_pic(bg_scene.get('pics') or [])} 为准,全程不变。"
        )
    if wear_ref_pics:
        _w = _pic(wear_ref_pics[:1])
        L.append(
            f"- {_w} 是这套衣服的实拍穿着参考:只参考衣服的款式 / 颜色 / 材质 / 穿法,"
            "不参考画面里的人、脸、身材和背景。"
        )

    # ---------------- 本镜穿着 / 换装 ----------------
    from_n, to_n = swap
    L.append("\n【本镜穿着】" if is_display else "\n【换装】")
    if is_display:
        cloth_items = [e for e in (worn + old) if _is_cloth(e["name"])]
        other_items = [e for e in (worn + old) if not _is_cloth(e["name"])] + accs
        if strip_base and old:
            # 「扯上身」风格的开场镜(用户 2026-10-20):她一把把开场那套扯掉,只剩固定内搭。
            # 它的末帧就是链头状态图 S0,后面所有镜都从这个状态往上穿。
            # **只列 old(初始那套)**:worn 里是扯掉之后剩下的内搭,不能一起写进"要扯掉"。
            _off = list(old)
            _off_names = "、".join(f"「{e['name']}」" for e in _off)
            _has_shoe = any(kind_of(e["name"]) == "shoe" for e in _off)
            L.append(f"- 开场(第 1 帧):她**完整穿着**{_off_names}站在画面正中。")
            L.append(
                "- 特效:这是**换装特效镜头** —— 她一只手抓住上衣、一只手抓住裤腰,"
                "像甩外套一样**同时朝画面左侧用力一甩**,整套外层衣服在空中甩开、整件飞出画外"
                "(不是慢慢脱、不是变透明)。"
            )
            L.append(
                "- 动作:**上衣、下装"
                + ("、鞋" if _has_shoe else "")
                + "被同一把甩脱,带着速度飞出画面左侧**;甩掉的那一瞬间,她身上已经是贴身的"
                f"「{PULL_INNER_TOP}」和「{PULL_INNER_BOT}」"
                "(这两件是她一开始就穿在里面的,不是由旧衣变出来的)。"
            )
            L.append(
                "- 收尾:从这一下之后到最后一帧,她身上只有贴身的"
                f"「{PULL_INNER_TOP}」和「{PULL_INNER_BOT}」"
                "(这两件是她一开始就穿在里面的);上一秒还在的旧衣彻底没有了 ——"
                "画面里没有半脱的布片、也没有被剪短的裤子。"
            )
            for e in accs:
                L.append(
                    f"- 配饰:「{e['name']}」"
                    + (f"({_pic(e.get('pics') or [])})" if e.get("pics") else "")
                    + f"全程{_place_txt(e['name']).strip('()')}。"
                )
        else:
            if cloth_items:
                L.append(
                    "- 衣服:"
                    + "、".join(
                        f"「{e['name']}」"
                        + (f"({_pic(e.get('pics') or [])})" if e.get("pics") else "")
                        for e in cloth_items
                    )
                    + " —— 每件分开穿在身上,实心、不透明。"
                )
            for e in other_items:
                if kind_of(e["name"]) == "shoe":
                    L.append(
                        f"- 鞋:脚上穿着「{e['name']}」"
                        + (f"({_pic(e.get('pics') or [])})" if e.get("pics") else "")
                        + " —— 两只脚各穿一只、整双都在、鞋头朝前。"
                    )
                elif kind_of(e["name"]) == "bag":
                    # 包不能只写"挎在她身上"(模型会拿在手里 / 挂肘上 / 不画),见 _BAG_WORN。
                    L.append(
                        f"- 配饰:「{e['name']}」"
                        + (f"({_pic(e.get('pics') or [])})" if e.get("pics") else "")
                        + f" —— 挎在右肩上:{_BAG_WORN}。"
                    )
                else:
                    L.append(
                        f"- 配饰:「{e['name']}」"
                        + (f"({_pic(e.get('pics') or [])})" if e.get("pics") else "")
                        + f"全程{_place_txt(e['name']).strip('()')}。"
                    )
        # 这一镜的状态(这一件已经换好)
        if strip_base:
            pass
        elif from_n and to_n:
            L.append(
                f"- 状态:从第一帧起她穿的就已经是{_names_txt(to_n)};"
                f"{_names_txt(from_n)}不在她身上。"
            )
        elif to_n:
            if _disp_bag:
                L.append(
                    f"- 状态:在她原来那一身外面再{_wear_verb(_disp_bag)}上{_names_txt(to_n)},"
                    "里面那几件不变。"
                )
            else:
                L.append(
                    f"- 状态:在她原有那一身外面多穿{_names_txt(to_n)},里面那几件不变。"
                )
        for e in worn + old:
            if not strip_base and kind_of(e["name"]) in ("bottom", "whole"):
                L.append(f"- 长短:{_short_len(e['name'])}")
        # 包:**静态展示**,不写"拿包 / 挎上肩"的动作(用户 2026-10-20 明确)。
        # 只把"已经挎着"写死到肩带 / 包身的位置上(_BAG_WORN)—— 只写"挎在她身上"模型会忽略。
        if prev_pic:
            _nn = to_n[0] if to_n else (new[0]["name"] if new else "")
            if _disp_bag:
                L.append(
                    f"- 姿势:<Picture {prev_pic}> 是上一镜的最后一帧 —— 站位、身体姿势、手型都和它一致;"
                    f"这一镜只是外面多{_wear_verb(_disp_bag)}一只「{_disp_bag}」。"
                )
            elif last_change:
                # 最后一个展示镜:开头接上一镜的姿势,**穿好之后手臂自然垂下**(用户 2026-10-20)。
                L.append(
                    f"- 姿势:<Picture {prev_pic}> 是上一镜的最后一帧 —— 站位、身体姿势、手型都和它一致"
                    f"(开始时手臂位置也和它一致);这一镜只是外面多穿「{_nn}」,"
                    "穿好之后两条手臂自然垂下。"
                )
            else:
                L.append(
                    f"- 姿势:<Picture {prev_pic}> 是上一镜的最后一帧 —— 站位、身体姿势、手臂位置、"
                    f"手型都和它一致;这一镜只是外面多穿「{_nn}」。"
                )
            # 肤色**不能**锚到上一镜:那张图是视频抽帧,皮肤上的色块 / 红印会被原样带进这一镜
            # (用户 2026-10-21:「我感觉就是首尾帧用的是视频的图片导致」)。
            # 肤色只认人物参考图;上一镜那张只用来对齐姿势和穿搭。
            L.append(
                f"- 肤色:肤色 / 肤质以 <Picture 1> 为准;"
                f"<Picture {prev_pic}> 只用来对齐姿势和穿搭。"
            )
    else:
        # 开场 + 要对掉的那件
        before = "、".join([e["name"] for e in worn] + [e["name"] for e in old])
        mine = _replace_set(tgt["name"], _close_state(tgt or {}))
        olds = [e for e in old if mine & _replace_set(e["name"], _close_state(e))]
        _kt = kind_of(_nm)
        _q = _quant(_nm)
        _part = _part_where(_nm)
        _gone = "、".join(f"「{e['name']}」" for e in olds)
        after = "、".join(
            dict.fromkeys(
                [e["name"] for e in worn]
                + [e["name"] for e in new]
                + [e["name"] for e in accs]
            )
        )
        if before:
            # 开始状态写**全套**:保留的 + 这镜要换掉的 + 墨镜 / 拖鞋这类常驻配饰。
            # 首尾帧硬锚关掉之后模型只能靠文字判断"她这会儿穿着什么",
            # 不写全它就会把"已经穿好的"和"还没飞过来的"搞反(用户 2026-10-21)。
            _start_names = _names_txt(
                [e["name"] for e in worn]
                + [e["name"] for e in olds]
                + [e["name"] for e in accs]
            )
            L.append(
                f"- 开始(第 1 帧):她身上完整穿着{_start_names}"
                "(实心、不透明,已经穿戴整齐)"
                + (f";这一镜要换掉的是{_gone}。" if olds else "。")
            )
            if _nm:
                L.append(
                    f"- 新件:「{_nm}」在这一镜开始之后才从画外飞来 —— "
                    "第 1 帧时它还在画外,她身上是上面那一身。"
                )
            # 旧件是下装时,第 1 帧必须把"两条裤腿"写死:不写模型会只画上衣、
            # 下半身空着等新裙子(= 用户报的「裤子没有穿」)。
            for e in olds:
                if kind_of(e["name"]) in ("bottom", "whole"):
                    L.append(f"- 第 1 帧下半身:{_bottom_txt(e['name'])}")
        # 动作
        if _pull and len(new) >= 2:
            # 「扯上身」一镜穿整套(2026-10-20 用户定稿:衣服悬在**左右两个上角**,
            # 她站在正中不动,整套像**下雨一样从上往下**落到她身上)。
            # 分簇规则:上衣 / 外套 / 小配件归右上;下装 / 鞋归左上。
            # **包不跟着雨落**:用户 2026-10-20 要求「那个包最后拿」——
            # 等衣服都穿好了,包才最后落下来挎到右肩。
            _up_kinds = ("top", "outer", "whole", "acc", "glasses", "hat")
            _bag_e = next((e for e in new if kind_of(e["name"]) == "bag"), None)
            _rain = [e for e in new if e is not _bag_e]
            _right = [e for e in _rain if kind_of(e["name"]) in _up_kinds]
            _left = [e for e in _rain if e not in _right]
            if _right and _left:
                _float_txt = (
                    "画面**右上方的空中**悬着"
                    + _names_txt([e["name"] for e in _right])
                    + ",**左上方的空中**悬着"
                    + _names_txt([e["name"] for e in _left])
                    + ";两簇都离她大约一臂远,"
                )
            else:
                _all = _right or _left
                _float_txt = (
                    "画面**右上方的空中**悬着"
                    + _names_txt([e["name"] for e in _all])
                    + ",离她大约一臂远,"
                )
            L.append(
                "- 悬衣:"
                + _float_txt
                + "每一件都**正面朝向镜头、平铺展开、单独挂着** —— "
                "各件之间留出明显空隙,画面里任何两件都不重叠、不互相遮挡、不叠成一堆;"
                "保持原本的完整形状 / 颜色 / 材质;"
                "没有人穿着它们,也没有衣架 / 挂钩 / 绳子;"
                "悬着的时候布面有很轻微的上下浮动(像被一团看不见的气托着)。"
            )
            # 陈列太大 / 挤在一起会糊成一团(用户 2026-10-20「不能重叠,衣服可以小一点」)。
            L.append(
                "- 大小与排布:悬着的每一件都比真人穿着时略小一点(约为真实大小的四分之三)、"
                "比例不变;同一簇里的几件**一上一下竖着错开排开**,"
                "排开后彼此之间有清楚的空隙、边缘不碰在一起。"
            )
            # 落点必须在**身前那一层**:不写这句,模型会把衣服落到她背后 / 侧面
            # (用户 2026-10-20 实测「有的衣服跑到人后面了」)。
            # 只列**真的会落下来的**部位(鞋默认就穿着,不能写"鞋落在脚背上")。
            _lands = []
            if any(
                kind_of(e["name"]) in ("top", "outer", "whole", "acc", "hat")
                for e in _rain
            ):
                _lands.append("上衣落在肩胸前面")
            if any(kind_of(e["name"]) == "bottom" for e in _rain):
                _lands.append("下装落在腰胯前面")
            if any(kind_of(e["name"]) == "shoe" for e in _rain):
                _lands.append("鞋落在脚背上")
            L.append(
                "- 落点:每一件都从她**身体正前方的那一层**往下落,落点就在她身前 —— "
                + ("、".join(_lands) + ";" if _lands else "")
                + "整个下落过程都在她身前,落定之后也贴在她身前那一层。"
            )
            # 太僵硬:站位钉死,但人要有生命感(用户 2026-10-20「人不要太僵硬」)。
            L.append(
                "- 状态:她是一个放松站着的人 —— 肩膀随呼吸轻微起伏、偶尔自然眨眼、"
                "手指放松有很细小的动作、重心有极轻微的变化;站位始终钉在同一个点。"
            )
            # 一件一件落(用户 2026-10-20):不要再写成"同时松开"——那样几件会糊成一团、
            # 也看不清每件是怎么穿上的。按真实穿衣顺序排:上衣 → 下装 → 外套 → 配饰。
            _ord_kind = {
                "top": 0,
                "whole": 1,
                "bottom": 2,
                "outer": 3,
                "acc": 4,
                "hat": 5,
            }
            _seq = sorted(_rain, key=lambda e: _ord_kind.get(kind_of(e["name"]), 9))
            _rel = [f"「{e['name']}」落下来、落到对应部位穿好" for e in _seq]
            if len(_rel) == 1:
                _seq_txt = _rel[0]
            elif len(_rel) == 2:
                _seq_txt = f"先{_rel[0]},再{_rel[1]}"
            else:
                _seq_txt = (
                    "先"
                    + _rel[0]
                    + "".join(f",再{r}" for r in _rel[1:-1])
                    + f",最后{_rel[-1]}"
                )
            L.append(
                "- 动作:她**站在画面正中**(双脚钉在同一个点、身体不转身、不弯腰);"
                "两簇衣服**一件一件轮流松开、从上往下飘落**,不是一起落 —— "
                + _seq_txt
                + ";"
                "每一件往下落的时候,布料是软的、表面轻轻起伏,衣角 / 下摆 / 袖口被风带着慢慢摆动,"
                "像一片很轻的布在空气里飘下来(不是直直掉下来、不是硬邦邦的一整块平移);"
                "其余几件仍然安静地悬在原位、保持不动;"
                "每件碰到她身体的那一瞬间,那一件已经穿好、完全服帖(硬切,没有中间态);"
                "全部落完之后,上方两簇的位置都变空。"
                "**画面里只有她一个人和飘下来的这套衣服 —— 没有第二个人替她拿。**"
            )
            _shoe_nm = next(
                (e["name"] for e in _rain if kind_of(e["name"]) == "shoe"), ""
            )
            if _shoe_nm:
                # 鞋不写死"落到脚上"就会停在脚边 / 半空(用户 2026-10-20 实测「鞋子也没有穿上」)。
                L.append(
                    f"- 鞋:那一双「{_shoe_nm}」从上方**一直落到她的脚上** —— "
                    "落到脚背的那一瞬间,两只脚各穿一只、整双已经穿好、鞋头朝前,"
                    "两只鞋都在脚上(画面里看得见)。"
                )
            if _bag_e:
                # 包**最后**才拿,而且不是"从天上掉下来"(用户 2026-10-20):
                # 画外有人把包扔给她 → 她**用手接住** → 把肩带越过右肩挎好。
                L.append(
                    f"- 包(最后):**衣服全部穿好之后包才出现** —— 前一段画面里还没有包;"
                    "等她整身穿好了,**画外有人把「"
                    + _bag_e["name"]
                    + "」朝她扔过来** —— "
                    "她**抬起右手(观众视角画面左侧的那只手)、手心朝上把它接住**,"
                    "再把肩带越过右肩、挎到右肩上;肩带落上右肩的那一瞬间,整只包已经挎好:"
                    f"{_BAG_WORN}。挎好之后她松开手,右臂自然垂回身侧。"
                    "**扔包的人始终在画外 —— 画面里只有她和这只正在飞过来的包,没有第二个人。**"
                )
                L.append(
                    "- 时间线:第 1 帧 → 她站着,空中悬着要落的衣服;"
                    "中段 → 衣服一件一件飘下来、每落一件就穿好;"
                    "最后 → 画外有人把包扔过来,她抬手接住、挎上右肩。"
                )
            if accs:
                # 墨镜 / 鞋这类**默认就戴着 / 穿着的**不参与"下雨":第一帧就在位上。
                # 不点这句,模型会把它们也画进空中那一堆,或者干脆漏掉。
                L.append(
                    "- 默认已经上身:"
                    + "、".join(f"「{e['name']}」{_place_txt(e['name'])}" for e in accs)
                    + " —— **第一帧就已经在这个位置**,之后每一帧都在原位,一直到最后一帧;"
                    "落下来的衣服叠在它们旁边 / 下面,不把它们挡住。"
                )
            # 清点:少件是这一镜最常见的翻车(实测开衫 / 墨镜整场不见),最后再点名一遍。
            L.append(
                "- 清点:最后一帧画面里必须同时看到这 "
                + str(len(new) + len(accs))
                + " 件 —— "
                + "、".join(
                    f"「{e['name']}」({_wear_where(e['name'], _close_state(e))})"
                    for e in new + accs
                )
                + ";每一件都完整可见。"
            )
            L.append(
                "- 最后一帧:整套衣服都穿在她身上,每一件完整、服帖,都在她身前;上方是空的"
                + (f";脚上穿着整双「{_shoe_nm}」" if _shoe_nm else "")
                + "。"
            )
        elif _pull:
            # 「扯上身」风格(用户 2026-10-20):整套还没穿的衣服**无人穿着、等距悬空排成一列**在
            # 她身体一侧,她把这一件从那一列里扯下来、直接拉到自己身上;穿好的从那一列里消失。
            L.append(
                "- 悬衣:画面左侧的空中、离她大约一臂远,还没穿上的那几件正**等距排成一列悬着** —— "
                "正面朝向镜头、保持原本的完整形状 / 颜色 / 材质;没有人穿着它们,也没有衣架 / 挂钩 / 绳子;"
                "已经穿到她身上的,从这一列里消失。"
            )
            if _is_bag:
                L.append(
                    f"- 动作:她从那一列里把这只「{_nm}」扯下来、接到身侧,把肩带越过右肩、挎在右肩上;"
                    "肩带落上右肩的那一瞬间,这只包已经整只挎好 —— "
                    "包身垂在她右侧胯部、贴着体侧,整只包完整可见。"
                )
            elif _shoe:
                L.append(
                    f"- 动作:那一双「{_nm}」悬在这一列的最下面;她把它们从空中取下来,"
                    f"在碰到{pro}双脚的那一瞬间,两只脚各穿一只、整双已经穿好。"
                )
            elif _carried:
                L.append(
                    f"- 动作:她从那一列里把「{_nm}」取下来、带到身前;"
                    f"在它碰到{pro}身体的那一瞬间,它已经整{_q}{_wear_verb(_nm)}好、"
                    f"贴在{pro}{_on_where(_nm)}上。"
                )
            else:
                L.append(
                    f"- 动作:她抬起右手,把这一{_q}「{_nm}」从那一列里**扯下来**、"
                    f"直接拉到自己{_part}上;被扯走的位置在那一列里留空,其他衣服保持原位、不被碰乱;"
                    f"在「{_nm}」碰到{pro}{_part}的那一瞬间,整{_q}已经穿好、完全服帖(硬切,没有中间态)。"
                    "**画面里只有她一个人和那一排悬空的衣服 —— 没有第二个人、也没有别人替她拿。**"
                )
                L.append(f"- 大小:{_size_anchor(_nm)}".rstrip(";") + "。")
        elif _is_bag:
            # 包:**先接住 → 再像正常人一样把肩带挎上右肩**(2026-10-21 用户定稿)。
            # 三个动作要按顺序写死,模型才会演成"抓包 → 提起来 → 肩带过肩 → 手放下";
            # 只写"挎在肩上"它会一开场就拎着包、或者把包悬在肩前不入位。
            L.append(
                f"- 动作:第 1 帧她两只手自然垂在身侧、手里是空的,包还在画外;"
                f"随后画外有人把「{_nm}」朝她扔过来 —— 它从**画面左上角(大约她肩膀高度)的画外**"
                "飞出来,带着速度划一道平缓的弧线,落向她身前腰胯的高度;"
                "她抬起右手(观众视角画面左侧的那只手)、手心朝内,一把抓住包的提手和肩带,把它稳稳接住;"
                "接着右手把包提到右肩前,手腕顺势一翻,让肩带滑过右肩肩头落在肩上"
                "(肩膀被压得轻轻一沉);整只包顺着肩带自然垂下 —— "
                "肩带跨过右肩肩头、贴肩平搭,包身垂在她右侧胯部、贴着手臂外侧;"
                "挎好之后右手自然垂回身侧、不再扶着包,整只包完整可见。"
                "**画面里只有她一个人和这只正在飞过来的包 —— 扔它的人始终在画外。**"
            )
            L.append(
                f"- 大小:整只包和参考图里的比例一致,宽度大约等于她的腰宽;"
                "挎好之后包身垂在她右侧胯部,不挡脸、不挡衣服。"
            )
        elif _shoe:
            # 鞋:**从右下角贴地滑进来 → 到她脚前 → 脚尖进鞋、穿好**(2026-10-21 用户定)。
            # 原来写"从正下方、膝盖高度平移进来",实测鞋停在**小腿前面**、一直不入脚
            # (项目 319 s09:鞋悬在膝盖高度、脚上还是旧拖鞋)。
            L.append(
                f"- 动作:一整双「{_nm}」从**画面右下角的外侧**(大约脚踝高度)贴着地面水平滑进来 —— "
                "正面朝上、鞋头朝前、左右两只成对不分开、不翻转;"
                "它一路平移到她双脚正前方、贴住脚尖的位置;"
                "接着两只鞋同时向前一送,她的脚尖先滑进鞋口,鞋子顺着脚背滑到脚踝、后空的带子扣上 —— "
                "鞋跟落在地面上的那一瞬间,整双已经穿好、完全服帖(硬切,没有中间态);"
                "整双鞋全程在脚踝高度、贴着地面滑行。"
                "**画面里只有她一个人和这双正在滑进来的鞋 —— 扔它的人始终在画外。**"
            )
        elif _carried:
            L.append(
                f"- 动作:有人从画外把「{_nm}」递过来;在「{_nm}」碰到{pro}身体的那一瞬间,"
                f"它已经整{q}{_wear_verb(_nm)}好、贴在{pro}{_on_where(_nm)}上。"
            )
        else:
            # 进场角落按**类型**固定(2026-10-20 用户定,不再按件左右交替):
            #   · 上衣 / 外套 / 连体衣:从**画面右下角往上**扔过来(大约腰胯高度起飞);
            #   · 下装(裤 / 裙):从**画面左下角往上**扔过来(大约膝盖到大腿的高度起飞);
            # 要的是「画外有人朝她身上扔」的效果 —— 不要"自己平飘进来"。
            if _kt == "bottom":
                _corner = "左下角、往上约 30% 处(大约在她膝盖到大腿的高度)"
            else:
                _corner = "右下角、往上约 30% 处(大约在她腰胯的高度)"
            L.append(
                f"- 动作:有人在画外把一{q}「{_nm}」朝她身上扔过来 —— 它从画面{_corner}的外侧飞出来,"
                f"带着速度划一道上扬的弧线飞向她的{_part};"
                "飞行中布料是软的,衣角和下摆被风带起来轻轻摆动,"
                "**整件始终是这件衣服原本的完整形状**;"
                f"在「{_nm}」碰到{pro}{_part}的那一瞬间,整{q}已经穿好、完全服帖"
                "(硬切,没有中间态)。"
                # 扔它的人留在画外:画面里只有她和这件衣服(不能露出一只手 / 一条胳膊)
                "**画面里只有她一个人和这件正在飞的衣服 —— 扔它的人始终在画外。**"
            )
            L.append(f"- 大小:{_size_anchor(_nm)}".rstrip(";") + "。")
        # 顺序:新穿好 → 旧的才消失
        if olds:
            L.append(
                f"- 顺序:{_gone}在新件整{q}穿好、完全服帖之后才从画面里消失;"
                f"她身上只剩「{after}」。中间没有淡出 / 半透明的旧件,也没有'什么都没穿'的空档。"
            )
        else:
            L.append(
                f"- 顺序:这是在她原来那一身外面加{_wear_verb(_nm)},"
                f"{_names_txt([e['name'] for e in worn])}一件都不消失、不变样。"
            )
        if first_change or last_change:
            _hair_how = (
                "从上方落下来的那一下,"
                if (_pull and len(new) >= 2)
                else ("被扯过来的那一下," if _pull else "过来的那一下,")
            )
            L.append(
                f"- 头发:{'东西' if _carried else '衣服'}"
                + _hair_how
                + "只有头发被风轻轻带起(发丝和鬓角往后飘),身体保持同一个站姿。"
            )
        L.append(
            f"- 收尾:结束时她身上是「{after}」;每件实心、不透明、形状和参考图一致"
            + (f";「{_nm}」挎在右肩上 —— {_BAG_WORN}" if _is_bag else "")
            + "。"
        )

    L.append("\n【声音】")
    L.append("overall_soundscape:安静的环境底噪。")
    L.append("non_diegetic_music:无。")

    text = "\n".join(L)
    # 代词兜底:文案是按「她」写的,男模 / 认不出时统一替换。
    if pro != "她":
        text = text.replace("她", pro)
    return text


def _short_len(name: str) -> str:
    """展示镜用的一句长短说明(防止长裤被画短、短裙被拉长)。"""
    lv = _len_hint(name)
    if lv == "短":
        return "与参考图完全一致 —— 到大腿中部,双腿在下面看得见。"
    if lv == "长" or _is_long(name):
        return "与参考图完全一致 —— 长款,裙摆 / 裤脚到脚踝。"
    return "与参考图完全一致。"
