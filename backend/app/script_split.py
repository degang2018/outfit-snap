"""把用户给的剧本切成镜头,并识别剧本里的人物 / 道具 / 场景。

两步走:
1. `analyze_script()` —— 让 LLM 输出「实体清单(人物/道具/场景)+ 镜头列表」,
   每个镜头标注它出现了哪些实体。这一步只做分析,不建项目,前端拿它让用户逐步指定素材。
2. `build_prompt()` —— 建项目时,把「本镜头出现的实体 + 用户给实体指定的参考图」套进
   H3 参考生成的固定结构里,输出该镜头的最终提示词。

没有 LLM 时自动退回规则切分(只切镜头,不识别实体)。
"""
from __future__ import annotations

import re

from . import llm
from .config import settings
from .garment_kind import NO_UNDERWEAR_TXT, fasten_clause, fasten_line, fasten_short, parse_slot

MARKER = re.compile(
    r"(?m)^\s*(?:(?:镜头|分镜|画面|场景|场次)\s*[0-9一二三四五六七八九十]*"
    r"|第\s*[0-9一二三四五六七八九十]+\s*[场幕镜]"
    r"|scene\s*\d+"
    r"|\d+\s*[\.、\)）])\s*"
)
SENT_END = re.compile(r"(?<=[。!?;…!?])\s*")
PARA_END = re.compile(r"\n\s*\n|\n")
CUT_CHARS = "。,;、,)）"

MAX_CHARS = 120
MIN_CHARS = 12
MAX_BEATS = 60

KINDS = ("characters", "props", "scenes")
KIND_LABEL = {"characters": "Subject", "props": "Prop", "scenes": "Scene"}
KIND_CN = {"characters": "人物", "props": "道具", "scenes": "场景"}

# 动作里出现这些词,说明这一镜要把某件东西当「物体」处理(抛起 / 甩到身上 / 飞入 / 落下…)。
# 两个地方用它:①只有这种镜头才绑道具参考图(见 routers/projects.py);
# ②这种镜头的参考图走"高保真"档 —— 衣服要腾空飞,参考图糊了最看得出来。
OBJECT_ACTIONS = ("抛起", "抛出", "抛向", "托起", "捧起", "悬浮", "悬停",
                  "飞进", "飞来", "飞过来", "落下", "落回",
                  "甩到", "甩向", "扔到", "扔向", "平移进来", "吸到她身上", "横向进来")


def is_object_shot(text: str | None, float_mode: bool = False, idx: int = 0) -> bool:
    """这一镜是不是"把衣服当物体抛/甩/飞"的镜头。

    文案里出现上面那些词就算;另外**悬浮变装的第 1 镜(招牌镜)天生就是** ——
    它一定在抛/甩衣服,但措辞每版都会变(抛起 → 抛向 → 甩到…),所以按位置兜底,
    不指望文案里永远出现同一个词。
    """
    t = text or ""
    return any(k in t for k in OBJECT_ACTIONS) or (float_mode and idx == 1)


# 「墨镜 / 项链 / 帽子 / 腰带」这类小件佩戴配饰:体积小、戴在固定部位(眼睛 / 脖子 / 头 / 腰…),
# 和「衣服」是两回事。建项目时它们和「包」一样,**每一镜都要绑上自己的商品参考图** ——
# 衣服的参考图是"只有衣服、没有人",绑上去会把镜头劫持成"从衣服起手";而这些小件绑上只会更稳。
# 不绑的后果实测过:整条片子里墨镜直接不出现、包被抄成人物参考图里那只黑包。
WORN_ACCESSORY_HINTS = (
    "墨镜", "太阳镜", "眼镜", "镜框",
    "项链", "耳环", "耳钉", "耳饰", "戒指", "手链", "手镯", "手表", "腕表",
    "帽", "围巾", "丝巾", "腰带", "皮带", "发夹", "发卡", "发箍", "头饰",
    "胸针", "领带", "领结", "手套",
    # 鞋 / 拖鞋:和墨镜同理 —— 一直穿在脚上,不单独排镜、每一镜都要绑自己的商品参考图。
    # (实测:不绑的话模型会照抄人物参考图里的脚,鞋子根本不出现)
    "鞋", "靴", "拖鞋",
)


def is_worn_accessory(name: str | None) -> bool:
    """这是不是「戴在身上的小配件」(墨镜 / 项链 / 帽子…)—— 既不是衣服,也不是包。"""
    return any(k in (name or "") for k in WORN_ACCESSORY_HINTS)


# 「一直戴着的小配件」各自该待在哪儿 —— **必须一个个点名,不能笼统写"戴在她身上"**:
# 实测把「拖鞋」和「墨镜」并列写成"这两件小配件一直戴在她身上",模型就把墨镜也摆到了脚上、
# 和拖鞋叠在一起(用户报"眼镜怎么跑到脚上了")。所以按件名给出"地方 + 完整说法"。
_WEAR_PLACE = (
    (("墨镜", "太阳镜", "眼镜"), "眼睛上", "戴在眼睛上、镜腿架在耳朵上"),
    (("帽", "发夹", "发卡", "发箍", "头饰"), "头上", "戴在头上"),
    (("项链", "锁骨链", "吊坠"), "脖子上", "戴在脖子上、贴着锁骨"),
    (("耳环", "耳钉"), "耳朵上", "戴在两只耳朵上"),
    (("围巾", "丝巾"), "脖子上", "围在脖子上"),
    (("腰带", "皮带"), "腰上", "系在腰上"),
    (("手套",), "双手上", "戴在两只手上"),
    (("手表", "手链", "手镯"), "手腕上", "戴在手腕上"),
    (("鞋", "靴", "拖鞋", "袜"), "双脚上", "穿在双脚上"),
)


def wear_place(name: str | None) -> tuple[str, str]:
    """这件小配件该待在哪儿:返回 (地方, 完整说法)。认不出来的按「身上」处理。"""
    n = name or ""
    for keys, where, how in _WEAR_PLACE:
        if any(k in n for k in keys):
            return where, how
    return "身上", "好好戴在她身上"


# 人物身材:上围丰满(约 C 罩杯)。用户明确要求「穿上衣服的时候前面要是鼓的」。
# 为什么要用文字硬补:人物参考图是穿抹胸的平胸模特,模型会照抄参考图里的身形
# (同类问题:开衫参考图画的是七分袖,提示词写"长袖"也压不住 —— 图比文字权重高)。
# 所以这里**先把优先级写死**:「参考图里偏平的身形不适用,以这句为准」。
# 同时把「衣服被撑起来的立体感」一起写进去 —— 用户要的是穿上衣服之后的效果。
# 躯干比例(身高 / 腰围 / 腿型 / 脸的年龄感)明确写「不变」,只改胸,免得人物整体走形。
BODY_BUST_HINT = (
    "**她的身材:上围丰满(约 C 罩杯),胸部饱满、胸型轮廓明确 —— "
    "参考图里偏平的身形不适用,以这句为准。**"
    "**穿上衣时前胸要被自然撑起来:衣服正面(胸前)有明显的外凸立体弧度、不是平的;"
    "前襟和领口被胸围撑得饱满、绷紧,两胸之间与胸下的面料有自然牵拉和浅阴影;"
    "上衣下摆因为胸部而略微离开腹部(前短后长的悬空感);侧缝顺着胸型向前走;"
    "软面料(针织 / 莫代尔 / 蕾丝 / 棉)贴住胸部时,能看出自然、连续的胸廓起伏;"
    "有肩带的款式肩带被微微拉紧、领口因此更饱满;"
    "有前襟的款式前襟被撑得**平整饱满、扣好后有自然的牵拉** —— "
    "**绝不许因为胸大就把前襟撑得敞开、两襟外张(该扣的必须一直扣着)。**"
    "**这只是正常穿衣的立体感:不要夸张变形、不要透视、不要露点、不要刻画身体细节;"
    "身高、腰围、腿型和脸的年龄感一律不变。**"
)

# 肤色一致性(实测问题:参考图是"多角度模特卡",几张脸的光线 / 妆面本来就不一样;
# 视频模型逐帧重画时会把它们混在一起,脸上出现白一块黄一块的色块,尤其是近景)。
# 只做**正向**描述(不要写"不要白斑 / 不要色块" —— 负面词反而容易把色块画出来)。
SKIN_HINT = (
    "**她的肤色与光线:脸、脖子、锁骨、肩膀和手的肤色是同一个自然色调,过渡均匀一致;"
    "脸上的底妆和脖子、身体完全同色;柔和均匀的正面漫射光铺满整张脸,"
    "明暗过渡细腻自然,皮肤质感真实、细腻。**"
)


# ------------------------------------------------------------------ 规则切分
def _hard_split(part: str) -> list[str]:
    out: list[str] = []
    while len(part) > MAX_CHARS:
        cut = max(part.rfind(ch, 0, MAX_CHARS) for ch in CUT_CHARS)
        if cut < MIN_CHARS:
            cut = MAX_CHARS - 1
        out.append(part[: cut + 1].strip())
        part = part[cut + 1 :].strip()
    if part:
        out.append(part)
    return out


def split_beats(script: str) -> list[str]:
    text = (script or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if not text:
        return []

    if MARKER.search(text):
        parts = [p.strip() for p in MARKER.split(text) if p.strip()]
        beats: list[str] = []
        for part in parts:
            beats.extend(_hard_split(part))
        return beats[:MAX_BEATS]

    parts: list[str] = []
    for para in PARA_END.split(text):
        para = para.strip()
        if not para:
            continue
        parts.extend([s.strip() for s in SENT_END.split(para) if s.strip()])

    beats = []
    buf = ""
    for part in parts:
        for piece in _hard_split(part):
            if not buf:
                buf = piece
            elif len(buf) + len(piece) <= MAX_CHARS:
                buf += piece
            else:
                beats.append(buf)
                buf = piece
    if buf:
        beats.append(buf)
    return beats[:MAX_BEATS]


def _split_at_middle(text: str) -> list[str]:
    mid = len(text) // 2
    for start, stop, step in ((mid, min(len(text) - 1, mid + 30), 1), (mid, max(1, mid - 30), -1)):
        for i in range(start, stop, step):
            if text[i] in CUT_CHARS:
                return [text[: i + 1].strip(), text[i + 1 :].strip()]
    return [text[:mid].strip(), text[mid:].strip()]


def _fit_to_count(beats: list[str], count: int) -> list[str]:
    if count <= 0 or not beats:
        return beats
    if len(beats) > count:
        buckets: list[list[str]] = [[] for _ in range(count)]
        for i, beat in enumerate(beats):
            buckets[min(count - 1, i * count // len(beats))].append(beat)
        return ["".join(b) for b in buckets if b]
    out = list(beats)
    while len(out) < count:
        i = max(range(len(out)), key=lambda k: len(out[k]))
        piece = out[i]
        if len(piece) < 10:
            break
        out[i : i + 1] = [p for p in _split_at_middle(piece) if p]
    return out


def beat_title(beat: str, limit: int = 28) -> str:
    title = beat.strip().split("\n")[0]
    return title[:limit] + ("…" if len(title) > limit else "")


# 内容清单里行首自带的【分镜名】(quick_start 拼清单时写死的机器名字)。
# 有它就用它 —— 名字不再依赖模型(模型偶尔抽风退到规则兜底时,名字会变成"正文前 12 个字"
# 这种碎句,用户报过「镜头名字都有问题了」)。这一段同时要**从正文里剥掉**,
# 不然会进提示词。
_LINE_TAG = re.compile(r"^\s*【([^】\n]{1,40})】[ \t]*")


def _split_line_tag(line: str) -> tuple[str, str]:
    """(行首的【名字】, 剥掉名字之后的正文);没有名字就返回 ("", 原文)。"""
    m = _LINE_TAG.match(str(line or ""))
    if not m:
        return "", str(line or "").strip()
    return m.group(1).strip(), str(line or "")[m.end():].strip()


# ------------------------------------------------------------ 提示词拼接
TAIL_ZH = (
    " 镜头:机位完全固定、三脚架锁死,全程零推拉摇移、零变焦,不要任何运镜,不要晃镜。"
    "景别:主体离镜头近、在画面里占得足够大,不要远景、不要大远景;"
    "**取景守则:头绝对不能被切掉 —— 头部和脸要完整入画、清晰可见(看得清表情),头顶接近画面上沿;"
    "同时最少带到腰、能看到肩颈和上身的衣服。既不能只拍一张脸或一个头,也不能把镜头拉得太远让人看不清脸,"
    "也不要大特写把脸放大到占满画面**;"
    "即使拍全身,也要头顶接近画面上沿、脚接近下沿,四周不留大片空白。"
    "场景:纯白背景,无其他环境元素、无渐变、无阴影、无其他物体,全程保持同一纯白背景。"
    "光线:柔和均匀的棚拍光线,人物和背景之间保持自然明暗层次,不要强烈侧光或逆光,"
    "避免在纯白背景上产生明显阴影。速度:所有动作按正常实时速度发生,不要慢动作、不要慢放。"
    "画面:写实、主体清晰、有质感;不要偏色。不要出现文字、字幕、水印、logo、品牌名。"
)

TAIL_EN = (
    " CAMERA: locked off on a tripod, absolutely no push-in, pull-out, pan, tilt or zoom - "
    "no camera movement of any kind, no shake. "
    "FRAMING: keep the subject close to the camera and filling a large part of the frame - "
    "no wide shot and no extreme wide shot; never crop off the top of the head - the whole head and "
    "face must stay in frame and read clearly (the expression must be legible), with the top of the "
    "head near the top edge; at the same time keep at least down to the waist so the shoulders, the "
    "upper body and the clothing stay visible. Never frame only the head or face alone - but never "
    "pull so far back that the face becomes too small to read either, and never a tight close-up that "
    "enlarges the face to fill the frame; "
    "even in a full-body shot the head should be near the top "
    "edge and the feet near the bottom edge, with no large empty margins. "
    "SCENE: a pure-white backdrop with nothing else in it - no gradient, no shadows, no objects, "
    "and it stays the same pure white for the whole shot. "
    "LIGHT: soft even studio lighting with natural separation between the subject and the backdrop - "
    "no strong side light or rim light, no visible shadows cast on the pure-white backdrop. "
    "MOVEMENT SPEED: everything happens at "
    "normal real-time speed, never slow motion and never a slow-motion replay. Look: photorealistic, "
    "crisp, with texture; no colour cast. No text, no subtitles, no watermark, no logo, no brand name."
)


# 「悬浮变装」专用尾部:这个风格要求机位锁死、色彩低饱和(推镜和加饱和是反的)
TAIL_ZH_FIXED = (
    " 镜头:机位完全固定,全程零推拉摇移,不要任何运镜,不要晃镜。"
    "**注意:人物必须像木偶一样全身静止 —— 除了手臂按首尾帧的角度过渡,"
    "身体的任何部位(呼吸起伏、头、眼、手、重心、发丝)都不许有任何变化;"
    "就是要一张静止照片 / 定帧的效果,绝对不要为了「避免定帧」而让她微微动。**"
    "景别:不要把镜头拉得太远(脸要看得清,不要远景 / 大远景);"
    "**取景守则(硬性):头绝对不能被切掉 —— 头部和脸要完整入画、清晰可见(看得清表情);"
    "同时最少带到腰、能看到肩颈和上身的衣服。既不能只拍一张脸或一个头,"
    "也不要大特写把脸放大到占满画面**;"
    "**拍全身时(衣服飞入 / 换装那几镜):固定的全身镜头 —— 机位高度约 1.1 米(与人物腰部齐平)、"
    "镜头轴水平、等效焦距 50mm、镜头距人物约 5 米、三脚架锁死;她从头到脚完整入画(头、腿、脚都"
    "不能出画、不能被裁掉);构图按参考片标定:头顶在画面高度的 20% 处、脚底在 78% 处,"
    "全身占画面高度的 58%(上方留白 20%、下方留白 22%);"
    "人物形象全程锁定(同一个人、7.5 头身比例与参考图一致,任何镜头都不许变);"
    "人水平居中、脚不挪位置**;"
    "**注意:服装参考图是「只有衣服」的产品图,衣服在里面又大又近 —— 那不是本镜头的机位;"
    "人物的大小、离镜头的远近一律以本说明为准,绝对不要因为参考图里衣服很大就把镜头推近、把人放大**。"
    "**画面里不出现衣架 / 挂钩 / 晾衣杆 —— 衣服是凭空进到画面上、罩上她身体的,没有挂具。**"
    "光线:柔和的定向光从画面的右上方来(右侧背景比左侧略亮一点);背景接近纯白但不是死白、"
    "有轻微的明暗渐变;人物和背景之间要有明暗层次,但不要强烈的戏剧性高光;"
    "**她脚下来一层淡淡的接地投影 —— 别让她看着像贴上去的**。"
    "速度:所有动作按正常实时速度发生,不要慢动作、不要慢放。"
    "色彩:低饱和、柔和的真实质感,不要浓郁艳丽的调色,不要偏色。"
    "画面:写实、主体清晰。不要出现文字、字幕、水印、logo、品牌名。"
)

TAIL_ZH_FIXED_BODY = (
    " 镜头:机位完全固定、三脚架锁死,全程零推拉摇移、零变焦,不要任何运镜,不要晃镜。"
    "**人物的大小必须全程不变:从第 1 帧到最后一帧,她的景别、大小和位置完全相同 —— "
    "绝对不许推近 / 拉近 / 放大她,绝对不许在结尾给她一个近景或特写。**"
    "景别:固定的全身镜头,她从头到脚完整入画(头、腿、脚都不能出画),"
    "头顶接近画面上沿、脚接近下沿;"
    "**取景守则(硬性):头绝对不能被切掉 —— 头部和脸要完整入画、清晰可见;"
    "既不能只拍一张脸或一个头,也不要大特写把脸放大到占满画面。**"
    "光线:柔和均匀的商业棚拍光。速度:按正常实时速度,不要慢动作、不要慢放。"
    "画面:写实、主体清晰、有质感;不要偏色。不要出现文字、水印、logo、品牌名。"
)

TAIL_EN_FIXED = (
    " CAMERA: locked off, absolutely no push-in, pan, tilt or shake. "
    "NOTE: the subject must be frozen like a puppet from head to toe - apart from the arm-angle "
    "transition between the first and the last frame, nothing on her body may change (no breathing, no "
    "head / eye / finger / weight-shift movement, no drifting hair); a still-photo look is exactly what "
    "we want here, never nudge her just to avoid looking like a freeze-frame. "
    " FRAMING (hard rules): never crop off the top of the "
    "head - the whole head and face must stay in frame and read clearly (the expression must be legible); "
    "at the same time keep at least down to the waist so the "
    "shoulders, the upper body and the clothing stay visible. Never frame only the head or face alone - "
    "but never pull so far back that the face becomes too small to read either, and never a tight "
    "close-up that enlarges the face to fill the frame. "
    "For full-body shots (the flying-garment / changing shots) the setup is a fixed full-body camera: "
    "tripod locked, camera height about 1.1 m (waist level), level axis, 50mm equivalent, about 5 m away "
    "from her; she stays fully in frame from the top of her head to the soles of her feet - legs and feet "
    "are never cropped (never a knee-up or thigh-up framing); framing is calibrated to the reference: the "
    "top of her head sits at 20% of the frame height and her feet at 78%, so her body fills 58% of the "
    "frame height (20% empty above her head, 22% below her feet); her identity is locked (same face, "
    "hair, skin, height, build and 7.5-head proportions as the reference, identical in every shot), "
    "centred horizontally, feet planted. "
    "NOTE: the garment references are product shots of the clothing ALONE, where the item looks big and "
    "close - that is NOT this shot's camera. Her size and her distance from the lens follow this brief "
    "only: never push the camera in or enlarge her just because the garment looks big in a reference. "
    "No clothes hanger, hook or rail may appear in frame - the garment comes in from off-frame and lands on her. "
    "LIGHT: soft directional light from the upper right (the right side of the backdrop reads a touch "
    "brighter than the left); the backdrop is near-white but not flat dead white - it keeps a faint "
    "gradient; keep separation between her and the background with a soft contact shadow under her feet "
    "(never let her look pasted on), but no harsh dramatic highlights. "
    "MOVEMENT SPEED: normal real-time speed, never slow motion. "
    "COLOUR: low saturation, soft realistic tones - no heavy grading, no colour cast. "
    "Look: photorealistic and crisp. No text, no subtitles, no watermark, no logo, no brand name."
)


# 「我的镜头」(自定义分镜)专用尾部:机位/景别/场景/光线由动作文本自己写死
# (俯拍头顶出画这类写法和取景守则冲突),只留「速度 + 画面」两句通用约束。
TAIL_ZH_CUSTOM = (
    " 速度:所有动作按正常实时速度发生,不要慢动作、不要慢放。"
    "画面:写实、主体清晰、有质感;不要偏色。不要出现文字、字幕、水印、logo、品牌名。"
)
TAIL_EN_CUSTOM = (
    " MOVEMENT SPEED: normal real-time speed, never slow motion. "
    "Look: photorealistic, crisp, with texture; no colour cast. "
    "No text, no subtitles, no watermark, no logo, no brand name."
)


def _lang(lang: str | None = None) -> str:
    return (lang or settings.llm_prompt_lang or "zh").lower()


# 「单镜整套 · 衣服飘入替换」镜的标记 —— 必须和 quick_start.whole_look_action 里那句
# 完全一致:这种镜头要换用 566 版的尾部(固定全身 + 锁死人物大小)。
WHOLE_LOOK_ACTION_MARK = "固定的全景全身构图"


def _tails(float_mode: bool, action: str = "") -> tuple[str, str]:
    """尾部「镜头」段。

    悬浮变装的**整套飘入替换镜**(见 quick_start.whole_look_action)走 566 那一版:
    固定全身 + 锁死人物大小。它不能用后来为「状态图」改的「木偶静止 / 定帧」版 ——
    那一版要求"静止照片 + 别拉太远(脸要看得清)",跟"衣服飞进来、从头到脚完整入画"打架。
    """
    if not float_mode:
        return TAIL_ZH, TAIL_EN
    if WHOLE_LOOK_ACTION_MARK in (action or ""):
        return TAIL_ZH_FIXED_BODY, TAIL_EN_FIXED
    return TAIL_ZH_FIXED, TAIL_EN_FIXED


def _float_clothing(action: str, index: int, total: int, variant: str, lang: str,
                    base_outfit: str = "") -> str:
    """悬浮变装:人物参考图里她穿着那套要展示的衣服,所以必须把「本镜头穿什么」写死。

    规则(用户定的):**只有招牌镜(第 1 镜,以及描述"抛起/悬浮套装"的镜)是打底,
    其余镜头一律已穿套装**。base_outfit 是玩法里配置的打底穿搭(留空则用通用说法)。
    只写「以本镜头描述为准」是不够的 —— 描述里没提衣服时,模型就退回参考图,
    直接把套装穿在她身上,悬浮永远做不出来(项目 53 就是这么失败的)。
    """
    a = action or ""
    base = (base_outfit or "").strip()
    base_zh = base or "基础打底(不是要展示的那套服装)"
    base_en = base or "a plain base outfit, NOT the outfit being shown"
    # 兜底:如果以后又把「落地变装」镜加回来,它的描述里会出现"从头顶落下/穿好"
    landing = any(k in a for k in ("从头顶落下", "落下后穿好", "自动穿到她身上", "自动穿到身上"))
    # 招牌镜:第 1 镜,以及任何描述"把套装抛起 / 套装悬浮"的镜头 —— 这些镜里她都是打底。
    # 用内容判断而不是只用序号,是因为分镜有可能把招牌动作拆成两镜(捧着 + 抛起)。
    toss = ("抛起" in a) or ("悬浮" in a and "全景" in a)
    # 「遮挡换装镜」:这一件直接吸到她身上、只挡住她身体的一个部位 → 贴合的瞬间她已经穿好这件。
    # 要点:**不是**糊满整个镜头 —— 人物全程从头到脚看得见,衣服只遮住一个部位。
    wipe = any(k in a for k in ("滑过去之后消失", "正好盖住她的", "横扫而过",
                                "糊满整个镜头", "把画面完全遮住"))
    # 「配饰飞到手里 / 肩上」:包这类不走"盖住身体",飞过来是让她拿住 / 挎上。
    carry = any(k in a for k in ("落进她伸出的手里", "落到她伸出的手里", "挎到她的肩上"))

    if lang.startswith("zh"):
        if landing:
            return (
                f"本镜头开始时她**只穿{base_zh}**,服装悬在空中、"
                "镜头中段自动落到她身上穿好,镜头结束时她已经穿着那套服装;"
                "**不要照搬参考图里她穿的那套衣服。**"
            )
        if carry:
            return (
                "**本镜头要展示的这件配件此刻还不在她手里 / 身上** —— 她身上是前几件"
                "(以下面的动作描述为准);**这一镜只演「这件配件从画外飞过来、落到她手里或肩上、她接住」,"
                "它是配件、不是衣服 —— 不要穿到她身上、也不要盖住她的身体**;"
                "**不要照搬参考图里她的其他衣服或配饰。**"
            )
        if index <= 1 or toss:
            if variant == "side" and index <= 1:
                return (
                    f"本镜头开始时**只穿{base_zh}**,身上没有别的衣服;"
                    "镜头中段服装**从画面上方落下来罩到她身上**、自动穿好,镜头结束时已经穿着那套服装;"
                    "**不要照搬参考图里她穿的那套衣服。**"
                )
            return (
                f"在本镜头里**只穿{base_zh}**,身上没有别的衣服;"
                "**绝对不要照搬参考图里她穿的那套衣服。**"
            )
        if wipe:
            # 进场方向按动作里的写法区分:上装"从画面上方落下"、下装"横向进来"
            how = ("**这一件从画面一侧横着扫进来、罩到她腿上**"
                   if "横向进来" in a else
                   "**这一件从画面上方落下来、罩到她身上**")
            return (
                f"**本镜头要展示的这一件此刻还不在她身上** —— 她身上**还穿着打底({base_zh})**,"
                "以及前面几镜已经穿好的那几件(以下面的动作描述为准);"
                f"{how}、**只挡住她身体的一个部位**(是挡住她,"
                "**不是把整个画面糊住 —— 她的人和脸全程都看得见**),到位之后她已经穿好这一件;"
                "**她全程都必须穿着衣服 —— 绝对不许把打底或前面已经穿好的那些脱掉,"
                "绝对不许出现光着上半身 / 光着后背 / 没穿衣服的状态(实测踩过:这里只写"
                "「她身上是上一件」时,模型直接把她画成光着上身、等着衣服落下来)**;"
                "**不要照搬参考图里她的其他衣服。**"
            )
        return (
            "在本镜头里她已经**穿好了那套要展示的服装**,全身完整上身;"
            "不要照搬参考图里她的其他衣服。"
        )

    if landing:
        return (f"At the start of this shot she wears ONLY {base_en}; the outfit floats in and settles "
                "onto her, so by the end she is wearing it. "
                "Do NOT copy the outfit shown in the reference.")
    if index <= 1 or toss:
        if variant == "side" and index <= 1:
            return (f"At the start of this shot she wears ONLY {base_en}; during the shot the outfit drops in "
                    "from above and dresses her, so by the end she is wearing it. "
                    "Do NOT copy the outfit shown in the reference.")
        return (f"In this shot she wears ONLY {base_en}, nothing else; "
                "do NOT copy the outfit shown in the reference.")
    if wipe:
        how = ("it sweeps in from the side of the frame and wraps her legs (waistband first)"
               if "横向进来" in a else
               "it drops in from above (as it enters only its lower half is visible - its top is "
               "cropped by the frame edge) and lands over her head / shoulders")
        return ("The piece shown in this shot is NOT on her yet - she is still wearing the previous piece "
                f"(follow the action below); mid-shot {how} and covers only one part of her body "
                "(she herself and her face stay visible the whole time - it never fills the frame), "
                "and by the end she is wearing it. Do not copy other clothing from the reference.")
    return "In this shot she is ALREADY wearing the outfit being shown; do not copy other clothing."


def _generic_prompt(action: str, index: int, total: int, lang: str, float_mode: bool = False) -> str:
    """剧本没识别出实体时的通用骨架。"""
    tail_zh, tail_en = _tails(float_mode, action)
    if lang.startswith("zh"):
        return (
            "subject_definitions:\n"
            "<Subject 1> 是本镜头的主体(下面描述的人或物);提供了参考图时,外貌必须与参考图完全一致。\n"
            "<Scene 1> 是镜头描述里的地点、时间与光线环境。\n\n"
            "summary:\n"
            f"[第 {index} 个镜头,共 {total} 个] {action}\n\n"
            "retention_analysis:\n"
            "<Subject 1>: fully_preserved - 全片外貌保持一致。\n"
            "<Scene 1>: fully_preserved - 场景、光线与白平衡保持一致。\n\n"
            "detailed_description:\n"
            f"{action}\n\n"
            "overall_soundscape:\n安静的环境底噪。\n\nnon_diegetic_music:\nNone." + tail_zh
        )
    return (
        "subject_definitions:\n"
        "<Subject 1> is the main subject of this shot, matching the reference image exactly when one is "
        "provided.\n<Scene 1> is the place, time of day and lighting implied by the shot description.\n\n"
        "summary:\n"
        f"[shot {index} of {total}] {action}\n\n"
        "retention_analysis:\n"
        "<Subject 1>: fully_preserved - identical appearance throughout the shot.\n"
        "<Scene 1>: fully_preserved - one consistent location, lighting and white balance.\n\n"
        "detailed_description:\n"
        f"{action}\n\n"
        "overall_soundscape:\nQuiet ambient room tone.\n\nnon_diegetic_music:\nNone." + tail_en
    )


def _adapt_action_to_garments(action: str, present: dict[str, list[dict]]) -> tuple[str, list[str]]:
    """通用分镜(镜头库细节镜)的 action 是通用模板,写的是「开衫 / 裤子 / 前襟 / 包」这类
    占位词。按本镜头**实际穿着**的件名(_worn 实体,带 _slot)动态替换,避免模型画出
    项目里不存在的衣服:

    - 上装不是开衫时:「开衫」→ 实际件名;「撩前襟」动作改写成「捏下摆展示版型」
    - 下装是裙子时:「裤子 / 裤脚 / 裤腿 / 裤长 / 裤型」→ 裙版措辞;「插兜」改「垂手」
    - 包 / 耳饰 没有对应实体(= 没传参考图)时,从动作里删掉相关句子,并返回删除的道具名
    - 墨镜「推至头顶」违反全程佩戴约束,改成「轻扶边框、全程在眼睛上」
    - 180° / 90° 转身在 2 秒镜头里做不完,统一收成 30° 侧转
    - 统一加「场景为纯白背景,无其他环境元素,」前缀

    返回 (新 action, 需要从 present["props"] 删除的道具名列表)。

    对换装镜 / 展示镜也安全:它们的 action 是程序按实际件名生成的,不含这些占位句式;
    件名本身含「开衫 / 裤」时对应分支直接跳过。
    """
    if not action:
        return action, []
    props = present.get("props") or []
    worn = [e for e in props if e.get("_worn")]
    pool = worn or props  # 通用分镜的 worn 件就是她身上穿的;没有 worn 时退而用全部道具
    top = next((e.get("name") or "" for e in pool if (e.get("_slot") or "") == "上身"), "")
    bottom = next((e.get("name") or "" for e in pool if (e.get("_slot") or "") == "下身"), "")
    names = {e.get("name") or "" for e in props}
    to_remove: list[str] = []

    # --- 上装:前襟动作只对真开衫成立;不是开衫时改写动作,再把「开衫」换成实际件名 ---
    if top and "开衫" not in top:
        if "前襟" in action:
            action = action.replace(
                "将双侧前襟向外展开约10至15厘米", "将下摆两侧向外轻拉约10至15厘米")
            action = action.replace(
                "短暂停留展示内搭、腰部比例和服装层次", "短暂停留展示面料弹性和垂坠感")
            action = action.replace("不改变内搭、扣子、蕾丝和服装颜色", "不改变蕾丝花纹、领口形状和服装颜色")
            action = action.replace("前襟", "下摆")
        action = action.replace("开衫", top)

    # --- 下装:是裙子时,裤子类措辞全部换成裙版;插兜动作改成垂手 ---
    if bottom and "裙" in bottom:
        if "插入裤子口袋" in action:
            action = action.replace(
                "双手分别插入裤子口袋,手部与口袋开口的位置准确,肩膀放松。",
                "双手自然垂在身侧,手指微微张开,肩膀放松。")
            action = action.replace("双手保持插兜状态", "双手保持自然垂放状态")
            action = action.replace("口袋位置变化、衣服穿模及裤型扭曲", "衣服穿模及裙型扭曲")
        action = action.replace("裤子", bottom)
        action = (action.replace("裤脚", "裙摆").replace("裤腿", "裙摆")
                        .replace("裤长", "裙长").replace("裤型", "裙型"))

    # --- 蕾丝:镜头库的「指尖划领口蕾丝」默认这件衣服有蕾丝领口;实际这件没有蕾丝时,
    # 动作里反复说「领口蕾丝 / 蕾丝花纹」会让模型在领口凭空画出**白蕾丝边**、黑针织上
    # 还冒出一片白色絮状纹理(用户报:"为什么衣服好多白色的东西")。---
    has_lace = any("蕾丝" in (e.get("name") or "") or "蕾丝" in (e.get("desc") or "")
                   for e in pool)
    if not has_lace and "蕾丝" in action:
        action = (action.replace("清晰呈现蕾丝花纹", "清晰呈现领口纹理")
                        .replace("领口蕾丝", "领口")
                        .replace("蕾丝花纹", "领口纹理")
                        .replace("蕾丝纹路", "领口纹路")
                        .replace("压坏蕾丝", "压坏领口")
                        .replace("蕾丝不融化、不变形", "领口形状稳定、不变形")
                        .replace("蕾丝", "领口细节"))

    # --- 包 / 耳饰:没有对应实体(没传参考图)就从动作里删掉,并标记删除 ---
    if "包" in action and not any("包" in n for n in names):
        action = action.replace("一手拿着原有道具,肩上自然挎着包,", "一手自然垂在身侧,")
        action = action.replace("包带和道具始终保持一致", "服装版型始终保持一致")
        to_remove.append("包")
    if "耳饰" in action and not any("耳" in n for n in names):
        action = action.replace("完整露出耳朵和原有耳饰", "完整露出耳朵")
        action = action.replace("突出侧脸轮廓、耳饰、颈肩线条", "突出侧脸轮廓、颈肩线条")
        to_remove.append("耳饰")

    # --- 墨镜:全程戴在眼睛上,不许推到头顶 ---
    action = action.replace("将原有墨镜轻轻推至头顶", "轻轻扶一下墨镜边框、调整位置,墨镜全程保持在眼睛上")
    action = action.replace("墨镜自然停留在头顶", "墨镜全程保持在眼睛上")

    # --- 大转身:2 秒做不完,收成 30° 侧转(在 top/bottom 替换之后做,两种形式都试) ---
    t, b = top or "上衣", bottom or "下装"
    new180 = f"缓慢向一侧转动约30度,展示{t}侧面线条、肩线和整体版型,短暂停留,再自然转回正面"
    old180_after = f"缓慢转身180度,完整展示{t}背部、肩线和整体后背版型,短暂停留,再自然转回正面"
    old180_raw = "缓慢转身180度,完整展示开衫背部、肩线和整体后背版型,短暂停留,再自然转回正面"
    if old180_after in action:
        action = action.replace(old180_after, new180)
    elif old180_raw in action:
        action = action.replace(old180_raw, new180)
    # 配套的「正面、背面与侧面剪裁」只出现在 180° 转身那镜 —— 转身收成 30° 后,
    # 背面展示已经不成立,这句必须一起改。
    action = action.replace("突出整套服装的正面、背面与侧面剪裁", "突出整套服装的正面与侧面剪裁")

    new90 = f"缓慢将身体向一侧转动约30度,展示{t}侧面版型、肩线和{b}的侧面垂坠感,随后自然转回正面。"
    old90_after = (f"缓慢将身体和双脚向一侧转动约90度,再继续转至接近背面,"
                   f"短暂停留展示{t}背部版型、肩线和{b}的侧面垂坠感,随后自然转回正面。")
    old90_raw = ("缓慢将身体和双脚向一侧转动约90度,再继续转至接近背面,"
                 "短暂停留展示开衫背部版型、肩线和裤子的侧面垂坠感,随后自然转回正面。")
    if old90_after in action:
        action = action.replace(old90_after, new90)
    elif old90_raw in action:
        action = action.replace(old90_raw, new90)

    # --- 纯白背景定死:所有镜头库 action 都以「已经穿好完整套装」开头 ---
    if action.startswith("已经穿好完整套装"):
        action = "场景为纯白背景,无其他环境元素," + action

    return action, to_remove


# 名字里带这些字的道具当「服装」处理:要强调它由哪几件组成、不能合并成一件。
# 不限件数,这里只是「哪些字算衣服」的词表 —— 用户加了新品类(棉袄、马甲、大衣…)也能覆盖。
_GARMENT_HINTS = (
    "套", "服", "装", "衣", "裤", "裙", "衫", "褛", "袍", "袄", "褂",
    "夹", "靴", "鞋", "袜", "帽", "巾", "领", "带", "背", "马甲",
)


# 部位是「穿在身上的衣服」的那几档(读图给的 place,见 garment_kind)——
# 上身 / 下身 / 全身 算衣服;脚 / 头 / 手 / 配饰 是「一直戴着的小配件」,走 is_worn_accessory。
# 「T恤」「内搭」「POLO」这类件名里一个衣服字都没有,靠 _GARMENT_HINTS 猜必然漏:
# 实测把「短袖衫」换成「T恤」之后,这一件就被当成"非服装",提示词里「实心不透明」「整件平移」
# 两句直接没了(件名是开放词表,以后加件名也不用回来补字表)。
_GARMENT_PLACES = ("上身", "下身", "全身")


def is_garment_name(name: str | None, slot: str = "") -> bool:
    """这个名字看起来是不是「一件衣服」。

    整套换装的「件名」必须能通过它 —— build_prompt 就是靠这套字判断一个名字要不要进
    「逐件换装说明」。实测:读图把一件上衣认成「其他」,名字里一个衣服字都没有 →
    整段换装说明直接丢了,提示词等于什么都没写。所以自动认出来的件名要用它兜一道底。

    slot 给了就先信 slot(读图时模型已经判过"穿在哪儿、是不是穿在外面"),件名认不出来也不打紧。
    """
    if is_worn_accessory(name):
        return False
    place, _ = parse_slot(slot)
    if place in _GARMENT_PLACES:
        return True
    if place:
        return False                  # 脚 / 头 / 手 / 配饰 = 小配件,不是衣服
    return any(h in (name or "") for h in _GARMENT_HINTS)


# 上半身「衣服」词表:用来判断一张状态图里是不是有**两件上衣要叠着穿**。
# 背景(用户报的坑):链上"第一次换上衣"那种状态图(如 睡衣上衣+睡裤+吊带背心),
# 模型会把里层的睡衣上衣吞掉、只画外层背心 —— 于是那张尾帧锚图本身就"少了一件",
# 换装镜跟着把旧上衣脱掉(实测:胸口皮肤占比从 12% 跳到 30%)。所以只要同框两件上衣,
# 就必须写死「两层叠穿」:里层的领口 / 袖子 / 下摆要露出来,外层只盖躯干中间。
_TOP_HINTS = (
    "上衣", "衬衫", "衬衣", "T恤", "t恤", "卫衣", "毛衣", "针织", "吊带", "背心",
    "开衫", "外套", "大衣", "夹克", "马甲", "西装", "西服", "睡袍", "浴袍",
)
_BOTTOM_HINTS = ("裤", "裙")


def _is_top_garment(name: str | None) -> bool:
    """这件是不是「上半身衣物」(用来判断两件上衣要不要强调叠穿)。"""
    n = name or ""
    if any(h in n for h in _BOTTOM_HINTS):
        return False
    return any(h in n for h in _TOP_HINTS)


def _pic_ref(nums: list[int], lang: str) -> str:
    pics = "、".join(f"<Picture {n}>" for n in nums)
    return (f"参考图 {pics}" if lang.startswith("zh") else f"reference image{'s' if len(nums) > 1 else ''} {pics}")


def _wear_verb(name: str, slot: str = "") -> str:
    """「穿 / 挎 / 戴」——包 / 帽子 / 眼镜 / 鞋**不是穿在身上的衣服**。

    写成"她身上已经穿着的那一件"(包)时,模型会当没这回事(实测单肩包在通用分镜里也不出现)。
    """
    if is_garment_name(name, slot):
        return "穿"
    n = str(name or "")
    return "挎" if ("包" in n or "袋" in n) else "戴"


def _bg_tail(tail: str, pic: int) -> str:
    """把尾部的「纯白背景」守则换成「以人物背景参考图为准」。

    用户上传了背景图时(见 projects.create_project_from_script),整片都站在这个背景里:
    背景的空间 / 陈设 / 色调 / 光线必须照参考图,不能再写"纯白背景、无其他物体"。
    """
    if not tail:
        return tail
    t = tail.replace(
        "场景:纯白背景,无其他环境元素、无渐变、无阴影、无其他物体,全程保持同一纯白背景。",
        f"场景:背景以参考图 <Picture {pic}> 为准 —— 空间、墙面和地面、陈设、色调和光线"
        "都跟这张图完全一致;全程保持同一个背景,不许切换、不许增减陈设、"
        "不许出现参考图里没有的东西。**背景里每件东西的大小 / 高度 / 透视也照参考图,"
        "不许为了配合人物而缩放背景;人物要按这个空间的真实尺度站进去 —— 背景里比她高的"
        "板子 / 门框 / 柜子,顶部必须高过她的头顶,绝不许把她画得比背景里的板子 / 家具还高。**")
    t = t.replace(
        "背景接近纯白但不是死白、有轻微的明暗渐变;",
        f"背景以参考图 <Picture {pic}> 为准(空间 / 陈设 / 色调 / 光线都照它,全程不变);")
    t = t.replace(
        "光线:柔和的定向光从画面的右上方来(右侧背景比左侧略亮一点);",
        "光线:以参考图里的自然光线为准;")
    t = t.replace("避免在纯白背景上产生明显阴影。", "阴影方向要和参考图里的光线一致。")
    t = t.replace(
        "SCENE: a pure-white backdrop with nothing else in it - no gradient, no shadows, no objects, "
        "and it stays the same pure white for the whole shot.",
        f"SCENE: the place in <Picture {pic}> (its space, walls, floor, furniture, colours and lighting) "
        "is the backdrop and stays exactly the same for the whole shot - no cuts, "
        "no added or removed objects.")
    t = t.replace("no visible shadows cast on the pure-white backdrop", "no harsh shadows")
    return t


def build_prompt(
    shot: dict,
    present: dict[str, list[dict]],
    picture_of: dict[str, list[int]],
    index: int,
    total: int,
    lang: str | None = None,
    float_mode: bool = False,
    float_variant: str = "overhead",
    float_base_outfit: str = "",
    garment_desc: str = "",
    float_timeline: str = "",
    wear_ref_pics: list[int] | None = None,
    no_ndm: bool = False,
) -> str:
    """拼出该镜头的最终提示词。

    present:    本镜头出现的实体 {"characters":[{"name","desc"}...], "props":[...], "scenes":[...]}
    picture_of: 实体键 "kind:name" -> 参考图序号列表(1 起,可多张)。
                没在里面的就是"不指定",交给模型自己生成。
    float_mode: 悬浮变装。这个玩法里人物参考图**只定脸**,并且「本镜头她穿什么」由阶段决定,
                必须写死进提示词 —— 否则模型会照搬参考图里她已经穿着的那套衣服,衣服永远离不开身体。
    float_timeline: 「一镜穿完整套」的时间轴(由 quick_start.float_timeline_text 按这一镜
                实际有几件生成)。接在动作说明后面 —— 实测这是压住"几件一起糊上来"最有效的一处。
    wear_ref_pics: 「实拍穿着参考」的参考图序号(不绑定实体,单独一张图挂在最后)。
    """
    lg = _lang(lang)
    action = shot.get("action") or shot.get("title") or ""
    # 通用分镜(镜头库)的 action 是「开衫/裤子/前襟」这类占位词 —— 按本镜头实际穿着
    # 的件名动态替换;换装镜/展示镜的 action 是程序按实际件名生成的,函数内部会安全跳过。
    action, to_remove = _adapt_action_to_garments(action, present)
    if to_remove:
        # 无图配饰(包 / 耳饰)被动作文本带进了 present,但用户没传参考图 —— 不能留在
        # 提示词里,否则模型会凭空生成。从 present 里删掉,Subject/Prop/retention 里就不会出现。
        present = dict(present)
        present["props"] = [e for e in (present.get("props") or []) if e.get("name") not in to_remove]
    # 镜头库 action 里可能带进「包」「耳饰」等无图配饰 —— 它们在 _adapt 里没被删掉
    # (因为 present 里有实体),这里再兜底删一次。只删无参考图的,有参考图的正常保留。
    _NO_REF_ACCESSORIES = {"包", "耳饰"}
    present = dict(present)
    present["props"] = [e for e in (present.get("props") or [])
                        if not (e.get("name") in _NO_REF_ACCESSORIES and not e.get("_ref"))]
    if float_timeline:
        action = f"{action}\n{float_timeline}"
    # 「人物背景图」:场景实体带参考图时,整片都用这张图当背景 —— 动作文本里那句写死的
    # 「场景为纯白背景」要去掉,尾部那段纯白背景守则也换成"以背景图为准"(见 _bg_tail)。
    bg_scene_pic: list[int] = []
    for _se in (present.get("scenes") or []):
        _p = picture_of.get(f"scenes:{_se.get('name') or ''}") or []
        if _p:
            bg_scene_pic = _p
            break
    if bg_scene_pic:
        action = action.replace("场景为纯白背景,无其他环境元素,", "")
        # 镜头库 / 自定义分镜的动作文本末尾常写着「纯白背景，柔和影棚布光」——
        # 有背景图时这些句子会和白背景打架,统一改成"以背景参考图为准"。
        for _old, _new in (
            ("纯白背景，柔和影棚布光。", "背景以背景参考图为准，光线与它一致。"),
            ("纯白背景，柔和均匀的影棚布光", "背景以背景参考图为准，柔和均匀的影棚布光"),
            ("白色背景，柔和影棚布光。", "背景以背景参考图为准，光线与它一致。"),
            ("背景纯白,柔和均匀的商业棚拍光线", "背景以背景参考图为准,光线与它一致"),
        ):
            action = action.replace(_old, _new)
    has_entity = any(present.get(k) for k in KINDS)
    if not has_entity:
        return _generic_prompt(action, index, total, lg, float_mode)
    tail_zh, tail_en = _tails(float_mode, action)
    if bg_scene_pic:
        tail_zh = _bg_tail(tail_zh, bg_scene_pic[0])
        tail_en = _bg_tail(tail_en, bg_scene_pic[0])

    defs: list[str] = []
    retention: list[str] = []
    # 本镜头的服装道具:{label, name, pics}。判断它到底是「一整套(多件)」还是「单独一件」,
    # 提示词末尾的写法完全不同 —— 说错了模型就会把一件衣服画成好几片(实测踩过)。
    garment_pieces: list[dict] = []
    for kind in KINDS:
        for i, ent in enumerate(present.get(kind) or [], start=1):
            label = f"<{KIND_LABEL[kind]} {i}>"
            name = ent.get("name") or f"{KIND_CN[kind]}{i}"
            desc = _clean_desc(ent.get("desc"))
            nums = picture_of.get(f"{kind}:{name}") or []
            if lg.startswith("zh"):
                if nums:
                    if kind == "props":
                        # 服装要把「这一套包含哪几件」写出来,否则模型容易把几件揉成一件。
                        # 实体自己没有 desc 时,用读图得到的整体服装描述兜底 ——
                        # 但**拆件**(名字像「套装-吊带背心」)绝不能用整套描述兜底:
                        # 那会把整套(连配饰)都写进"这一件"的描述里,模型就把整套一起生成出来。
                        # 小件佩戴配饰(墨镜 / 项链 / 帽子 / 腰带…)不算「衣服」:当成衣服会给它套上
                        # "领口 / 袖子 / 衣身"那套话,模型就画不对了 —— 它该被"戴"上去,不是"穿"。
                        is_garment = is_garment_name(name, ent.get("_slot") or "")
                        if desc:
                            tip = desc
                        elif is_garment:
                            tip = (_piece_desc(name, garment_desc) if _is_piece_name(name)
                                   else garment_desc.strip())
                        else:
                            tip = ""
                        # 顺序别动:描述写在前面、再跟一句"以参考图为准"。
                        # 曾经为了治"衣服跟商品图不像",把"以参考图为准"提到最前面、还加了一句
                        # "和参考图对不上的地方一律以参考图为准、不要按这句话改图" ——
                        # 结果模型变得严格照抄商品图(那是一张"衣服占满整幅"的特写),
                        # **连比例和景别一起抄了**,飞进来的衣服被吹成巨幅、罩住整个人(实测两镜都翻)。
                        # 所以退回这一版:描述先给个"它是件什么衣服"的定调,再说以图为准(只锁外观细节)。
                        # 「她这时已经穿在身上的那一件」(建项目时按之前的换装镜补进来的):
                        # 它**不是**本镜头要换的件 —— 只当"她现在穿着的样子"的参考。
                        # 不区分的话,末尾那段"本镜头这几件必须全都穿上、先穿内搭再套外套"会把
                        # 它也算进去,模型就会把已经穿好的东西重画一遍、还把内搭也画上
                        # (实测:开衫镜里凭空多出一件灰背心,用户报"把整个衣服都改变了")。
                        already_on = bool(ent.get("_worn"))
                        # _base:她「一开始就穿着、本镜头会被换掉」的那几件(起始套装)。
                        # 它和 _worn 不一样:_worn 是本镜头不动的, _base 是这一镜要换掉的旧件。
                        # 两种情况都不能算进「本镜头要换上的件」(否则通用换装说明会多出
                        # "把睡衣穿上"这种错拍)。
                        start_on = bool(ent.get("_base"))
                        # 形状:飞过来时**整件一起飘**——布料有轻微起伏 / 褶皱飘动,但不要翻滚 /
                        # 不要在空中转圈(方向稳才认得出是哪件衣服)。
                        # 注意:这句以前写的是"整件**平移**过来 —— 不要波浪状飘动",和动作说明里的
                        # "布料要有轻微的起伏 / 褶皱飘动"**打架**,而定义这一段权重更高、说的又是
                        # "这件衣服本身长什么样" —— 模型按它演成一块硬板平移过去(用户报"衣服飘的时候
                        # 都没有效果")。总说明那边早就改成允许起伏了,这里当时漏了。
                        # **但旧件 / 已穿件根本不飞** —— 以前这里照样写着"飞过来的时候…",等于在叫
                        # 模型让它飞一次:实测旧睡衣上衣被复制了一份飘出来(用户报"为什么睡衣也飘了")。
                        if not is_garment:
                            shape_note = "全程形状不变。"
                        elif start_on or already_on:
                            shape_note = ("花纹 / 颜色 / 款式 / 长短全程不变(**它不参与飞行** —— "
                                          "它一直好好穿在她身上、不会出现在空中,"
                                          "也绝对不会被复制出第二份飘过来)。")
                        else:
                            # 朝向 + 尺寸都必须在**定义**里锁死(实测 defs 权重高于动作说明)。
                            # · 朝向用**正面锚点**(正立 + 正面朝镜头 + 只平移不旋转),不用否定式;
                            #   而且**不写"和参考图一样"** —— 一强调"以参考图为准",模型就会连
                            #   商品图的**比例和景别**一起抄走(项目踩过:飞进来的衣服被吹成巨幅)。
                            # · 尺寸同理:参考图是"衣服占满整幅"的竖长商品图,只靠总说明那句
                            #   "不要按参考图占满画面的比例放大"对长裤压不住,所以这里给**可量化的锚**
                            #   (飞在空中的大小 = 她穿上后的大小,整件不许比她还高、不许占满画面)。
                            shape_note = ("花纹 / 颜色 / 款式 / 长短全程不变(飞过来的时候是整件一起飘过来,"
                                          "**在空中始终保持正立:该朝上的部位一直朝上、该朝下的部位一直朝下"
                                          "(上衣是领口 / 肩线朝上、下摆朝下;下装是腰头朝上、裤脚 / 裙摆朝下),"
                                          "正面始终对着镜头、左右也不翻转 —— 整件只平移、不旋转**;"
                                          "**它飞在空中的大小 = 她穿上之后的大小:上衣约从她的肩到胯、"
                                          "下装约从她的腰到脚踝,整件绝不许比她还高、绝不许占满画面、"
                                          "也绝不许朝镜头方向放大;飘进来多大,落到她身上就多大**;"
                                          "面料有自然的垂坠和轻微起伏)。")
                        # 不透明约束:商品图(尤其平铺的蕾丝 / 雪纺)自带"薄、透"的拍摄感,模型会照抄
                        # 成半透明 —— 实测平铺的蕾丝上衣被画成一层透明的纱,隔着还能看见里面的衣服
                        # 和身体(用户报"上衣透明、原地飘")。所以每一件衣服都写死"实心不透明"。
                        opaque_note = ("穿在她身上时是**实心、不透明的** —— 绝不许画成半透明 / 透视 / "
                                       "飘着的一层纱:后面绝不透出皮肤 / 内衣 / 背景;蕾丝、薄纱也只当"
                                       "**实体面料**画(花纹照参考图,面料本身不透光)。"
                                       if is_garment else "")
                        base_def = (f"{label} 是「{name}」" + (f":{tip}" if tip else "") + "。"
                                    f"外观、颜色、材质、版型和细节都以{_pic_ref(nums, lg)} 为准,"
                                    f"必须完全一致,而且" + shape_note + opaque_note)
                        if is_worn_accessory(name) and lg.startswith("zh"):
                            # 小配件必须**写到具体位置**:以前只笼统说"戴在她身上",实测模型会把
                            # 墨镜也摆到脚上、和拖鞋叠在一起(用户报"眼镜怎么跑到脚上了")。
                            where, how = wear_place(name)
                            base_def += (f"**它是「一直戴在她身上」的小配件:必须全程{how} —— "
                                         f"绝对不许摘下来,也绝对不许出现在{where}以外的任何位置"
                                         f"(不许拿在手里、不许放在脚边 / 地上、不许当摆件摆在她旁边)。**")
                        if already_on:
                            _wv = _wear_verb(name, ent.get("_slot") or "")
                            _q = "件" if _wv == "穿" else ("只" if _wv == "挎" else "件")
                            _off = "脱" if _wv == "穿" else "摘"
                            base_def += (f"**这一件是她现在身上已经{_wv}着的那一{_q} —— 本镜头不换它、"
                                         f"不{_off}它、也不再{_wv}它一次,"
                                         "只要让它一直是参考图里这个样子就行**。")
                        elif start_on:
                            _wv = _wear_verb(name, ent.get("_slot") or "")
                            base_def += (f"**这一件是她一开始就{_wv}在身上的、从头到尾都在她身上 —— "
                                         "它全程贴着她身上,绝不许出现在空中、绝不许朝她飞过来、"
                                         "也绝不许被复制成第二件;本镜头结束前它会被新件替掉、"
                                         "从她身上消失,但它**绝对不是**这一镜要飞进来的那件"
                                         "(要飞进来的只有下面列出的「新件」)。**")
                        # 「开合」:这一件是扣上 / 拉上还是敞开(件级 close > 全局默认)。
                        # **写在定义里** —— defs 权重高于动作说明,只写在动作里模型会照参考图
                        # 自己敞开(用户报"黑色开衫被穿成敞开、还露出蕾丝内衣")。
                        base_def += fasten_clause(name, ent.get("_slot") or "",
                                                  ent.get("_close") or "")
                        defs.append(base_def)
                        if is_garment and not already_on and not start_on:
                            # slot = 读图给的「部位」,换装说明拿它判部位(比按件名里的字猜准),
                            # 见 quick_start._part_group;没给就退回按件名猜。
                            garment_pieces.append({"label": label, "name": name, "pics": len(nums),
                                                   "slot": ent.get("_slot") or ""})
                    elif kind == "scenes":
                        defs.append(
                            f"{label} 是「{name}」" + (f":{desc}" if desc else "") + "。"
                            f"空间的样子、陈设、色调和光线都以{_pic_ref(nums, lg)} 为准,必须完全一致。"
                        )
                    else:
                        # 绑了参考图就以图为准:再写一遍文字描述,模型会照文字走、偏离照片。
                        # 但悬浮变装的人物参考图里常带着「多余的」配饰(包 / 鞋 / 帽子)——
                        # 说「配饰也以参考图为准」会把这件多余的配饰一起抄进来(实测:参考图里她背着黑包,
                        # 生成出来就真的多在身上挂一个黑包),所以这个玩法只锁五官 / 发型,配饰一律不照搬。
                        if float_mode:
                            defs.append(
                                f"{label} 是「{name}」。五官、脸型、发型、妆容以{_pic_ref(nums, lg)} 为准,"
                                f"必须完全一致;但**参考图里她身上的包 / 鞋 / 帽子 / 首饰 / 腰带等配饰,"
                                f"以及内衣 / 抹胸 / 打底小衣 / 露出来的肩膀 / 腰 / 腿,一律不要照搬** —— "
                                f"她身上穿什么、露出多少,一律以本镜头列出的 <Prop> 和动作说明为准"
                                f"(参考图里那些不属于本镜头的东西,比如脚上的球鞋、身上那件内衣,一律不要出现)。"
                            )
                        else:
                            defs.append(
                                f"{label} 是「{name}」。外观、发型、妆容和身上的配饰都以{_pic_ref(nums, lg)} 为准,"
                                f"必须完全一致;不要添加参考图里没有的配饰或装饰"
                                f"(本镜头动作里明确要用到的道具除外)。"
                                # 人物参考图常常是"内衣模特卡"(抹胸 + 内裤、露腰):不写这句,
                                # 模型会把那件内衣和露出的肩膀 / 腰 / 腿一起照抄进来 ——
                                # 实测自拍镜里黑开衫被穿成敞开、里面是一件蕾丝胸衣。
                                f"**但参考图里她身上的内衣 / 抹胸 / 打底小衣,以及露出来的肩膀 / 腰 / 腿,"
                                f"一律不要照搬** —— 她身上穿什么、露出多少,一律以本镜头列出的 <Prop> "
                                f"和动作说明为准(她全程衣着完整,绝不许出现内衣 / 胸衣 / 裸露的胸口)。"
                            )
                        # 身材(上围丰满)单独一条:参考图里她是平胸模特,不写这条衣服正面就是平的
                        # 但只对「她」加 —— 化妆师/摄影师的「手臂」不是人物主体,不需要女性身材描述
                        if "手臂" not in name and "化妆师" not in name and "摄影师" not in name:
                            defs.append(BODY_BUST_HINT)
                            defs.append(SKIN_HINT)
                    if float_mode and kind == "characters":
                        # 悬浮变装的关键:必须把「本镜头她穿什么」写死,否则模型照搬参考图的衣服
                        base_names = [e.get("name") or "" for e in (present.get("props") or [])
                                      if e.get("_base")]
                        if base_names and lg.startswith("zh"):
                            if float_variant == "piece":
                                # 按件排镜:同一套打底会被**强制**带进每一镜,但「这一镜开始时
                                # 她到底穿哪几件」是逐镜变化的(第 2 镜起上一镜已经换掉一件了)。
                                # 这里写死全局打底会和动作说明打架(实测:s03 写"穿着睡裤"、
                                # 同一段的动作却说"她开始时穿着睡衣上衣、短裙" → 模型把睡裤又穿回来)。
                                defs.append(
                                    f"{label} 的穿着**以动作说明为准**(人物参考图只用来锁定五官和发型):"
                                    "**动作说明里写的「她开始时穿着…」就是她此刻身上的穿着,"
                                    "严格照那句来 —— 那一句里没有的衣服,她身上就不许出现。**"
                                )
                            else:
                                # 这一镜有明确的「起始套装」(_base 那几件)-> 直接点名,别再用通用打底话术
                                defs.append(
                                    f"{label} 的穿着以下面的说明为准(人物参考图只用来锁定五官和发型):"
                                    "**本镜头开始时她身上是「" + " + ".join(base_names) + "」"
                                    "(见下面的动作说明);绝对不要照搬人物参考图里她穿的那套衣服。**"
                                )
                        else:
                            if float_variant == "piece":
                                # 按件排镜:一镜只换一件,动作说明里已经写清这一件怎么进来、
                                # 别的部位保持原样 —— 不再把「整套换装说明」(_float_clothing)搬进来。
                                # 那段是给「一镜换整套」写的,一趟 4~5 千字;defs 权重又高,
                                # 塞进单件镜里只会把这一镜真正的指令淹没。
                                # **展示镜什么都不换**(动作说明写的是"这一镜只做展示 —— 不换装")——
                                # 再写"只换动作说明里的那一件"就跟正文打架,模型会去脱/换一件
                                # (实测:状态展示镜里衣服被换掉/脱掉)。
                                if "这一镜只做展示" in (shot.get("action") or ""):
                                    defs.append(
                                        f"{label} 的穿着以动作说明为准:**这一镜只做展示、什么都不换:"
                                        f"动作说明里写的「她身上穿的那套」就是她此刻的穿着 —— "
                                        f"不许换掉、不许脱掉、也不许再给她穿一件上去**"
                                        f"(人物参考图只用来锁定五官和发型,不要照搬她身上那套衣服)。"
                                    )
                                else:
                                    defs.append(
                                        f"{label} 的穿着以动作说明为准:**这一镜只换动作说明里的那一件,"
                                        f"别的部位 / 别的件保持原样,不许换回打底**"
                                        f"(人物参考图只用来锁定五官和发型,不要照搬她身上那套衣服)。"
                                    )
                            else:
                                defs.append(
                                    f"{label} 的穿着以下面的说明为准"
                                    # 身材不再交给参考图锁定(参考图是平胸模特),改由上面的 BODY_BUST_HINT 用文字定
                                    f"(人物参考图只用来锁定五官和发型):"
                                    + _float_clothing(action, index, total, float_variant, lg,
                                                      float_base_outfit)
                                )
                        # **没绑到参考图的配饰不写进提示词**(用户 2026-10-15 要求):没有样本时
                        # 模型只能照件名凭空画(实测凭空多出一副眼镜)。判据与上面绑图同一口径。
                        worn_here = [e.get("name") or "" for e in (present.get("props") or [])
                                     if is_worn_accessory(e.get("name") or "")
                                     and picture_of.get(f"props:{e.get('name') or ''}")]
                        if worn_here:
                            # 小件配饰(墨镜 / 项链 / 帽…)不单独排镜、全程戴着。光绑商品图不够:
                            # 上面的「穿什么只看本镜头动作」会让没提到它的镜头把它省掉(实测:举手机自拍里没了墨镜)。
                            defs.append(
                                "**她身上一直戴着这几件小配件,从第 1 镜到最后一镜都在:"
                                + "、".join(f"「{n}」({wear_place(n)[1]})" for n in worn_here) + ";"
                                "它们是**戴在她身上**的 —— 不许摘下来、不许只在某几镜出现、"
                                "也不许当成一件商品摆在旁边**。"
                                "**每一件都固定待在自己的位置:墨镜只在眼睛上、鞋 / 拖鞋只在脚上 —— "
                                "绝对不许把它们挪到别处(比如把墨镜放到脚上)。**"
                                "**她身上 / 脚上 / 头上只许出现这几件配件 —— "
                                "绝对不许出现别的鞋、别的帽子、别的包(比如参考图里那双球鞋)。**"
                            )
                    if ent.get("_base"):
                        # 「旧件 / 打底」:它开场在她身上,但**会被新件换掉** —— 绝不能写
                        # fully_preserved(全片与参考图一致),那等于叫模型让它一直在(实测睡裤不消失)。
                        retention.append(
                            f"{label}: replaced - 她开场就穿着这一件({_pic_ref(nums, lg)}),"
                            "**被新件换掉之后就不许再出现在画面里**。")
                    else:
                        retention.append(f"{label}: fully_preserved - 全片与{_pic_ref(nums, lg)} 保持一致。")
                else:
                    body = f"{label} 是「{name}」" + (f":{desc}" if desc else "")
                    defs.append(f"{body}。没有参考图,请严格按这段文字描述生成。")
                    if ent.get("_base"):
                        # 同上:换装镜里的旧件(不绑图)走的就是这一支 —— 也不能写 fully_preserved
                        retention.append(
                            f"{label}: replaced - 她开场就穿着这一件(按文字描述),"
                            "**被新件换掉之后就不许再出现在画面里**。")
                    else:
                        retention.append(f"{label}: fully_preserved - 全片按文字描述保持一致。")
            else:
                if nums:
                    if kind == "props":
                        # 小件佩戴配饰(墨镜 / 项链 / 帽子 / 腰带…)不算「衣服」:当成衣服会给它套上
                        # "领口 / 袖子 / 衣身"那套话,模型就画不对了 —— 它该被"戴"上去,不是"穿"。
                        is_garment = is_garment_name(name, ent.get("_slot") or "")
                        tip = desc or (garment_desc.strip() if is_garment else "")
                        defs.append(
                            f"{label} is {name}" + (f": {tip}" if tip else "") + ". "
                            f"Its look, colour, material, cut and details must match the "
                            f"{_pic_ref(nums, lg)} exactly, and its shape must stay identical throughout."
                        )
                        if is_garment:
                            garment_pieces.append({"label": label, "name": name, "pics": len(nums),
                                                   "slot": ent.get("_slot") or ""})
                    elif kind == "scenes":
                        defs.append(
                            f"{label} is {name}" + (f": {desc}" if desc else "") + ". "
                            f"The space, its contents, colour palette and lighting must match the "
                            f"{_pic_ref(nums, lg)} exactly."
                        )
                    else:
                        if float_mode:
                            # 同中文分支:悬浮变装只锁五官 / 发型,不照搬参考图里她身上的包 / 鞋等配饰。
                            defs.append(
                                f"{label} is {name}. The face, hair and makeup must match the "
                                f"{_pic_ref(nums, lg)} exactly, but do NOT copy the accessories she wears in the "
                                f"reference (bag / shoes / hat / jewellery / belt) - what she wears or carries is "
                                f"decided solely by this shot's action description."
                            )
                        else:
                            defs.append(
                                f"{label} is {name}. Appearance, hair, makeup and any accessories must match the "
                                f"{_pic_ref(nums, lg)} exactly - do not add accessories or ornaments that are not in "
                                f"the reference (except props this shot explicitly uses)."
                            )
                    if float_mode and kind == "characters":
                        if float_variant == "piece":
                            # 按件排镜:一镜一件,不再搬整套换装说明(同中文分支)。
                            defs.append(
                                f"What {label} is wearing follows the action description: "
                                f"**swap ONLY the one piece named there; everything else stays as it is**. "
                                f"(The reference is used solely to lock the face, hair and body.)"
                            )
                        else:
                            defs.append(
                                f"What {label} is wearing is stated below "
                                f"(the reference is used solely to lock the face, hair and body): "
                                + _float_clothing(action, index, total, float_variant, lg, float_base_outfit)
                            )
                        # **没绑到参考图的配饰不写进提示词**(用户 2026-10-15 要求):没有样本时
                        # 模型只能照件名凭空画(实测凭空多出一副眼镜)。判据与上面绑图同一口径。
                        worn_here = [e.get("name") or "" for e in (present.get("props") or [])
                                     if is_worn_accessory(e.get("name") or "")
                                     and picture_of.get(f"props:{e.get('name') or ''}")]
                        if worn_here:
                            defs.append(
                                "**She wears these small accessories the WHOLE time - in every single shot: "
                                + ", ".join(f'"{n}"' for n in worn_here) + ". They are WORN ON HER - never take "
                                "them off, never show them in only some shots, and never place them next to her "
                                "as a product.**"
                            )
                    retention.append(f"{label}: fully_preserved - matches {_pic_ref(nums, lg)} throughout.")
                else:
                    body = f"{label} is {name}" + (f" ({desc})" if desc else "")
                    defs.append(f"{body}. No reference image - generate it strictly from this description.")
                    retention.append(f"{label}: fully_preserved - consistent with the description.")

    # 「实拍穿着参考」(用户上传的实拍 lookbook / 风格参考图):不属于任何实体,单独挂在参考图最后。
    # **必须配一句"只参考衣服"** —— 实测真人实拍图参与生成时,会把那个模特露出来的部分(光腿 / 肩)
    # 一起抄进画面(以前"开场没穿睡裤、光腿"就是这么来的)。措辞照抄项目里已经验证过的那版。
    if wear_ref_pics and lg.startswith("zh"):
        pics = "、".join(f"<Picture {n}>" for n in wear_ref_pics)
        defs.append(
            f"{pics} 是这套衣服的**实拍穿着参考**:**只参考它身上这套衣服的款式 / 颜色 / 材质 / "
            "垂坠感和穿法**;**绝对不要参考画面里的脸 / 头发 / 身材 / 皮肤 / 姿势 / 背景** —— "
            "她的脸、发型、身材、还有身上露出来的部分(肩 / 腰 / 腿)一律以 <Picture 1> 为准,"
            "绝对不许把参考图里露出的皮肤 / 光腿照搬进来,背景一律纯白。"
        )

    # 服装要说清「这一镜到底是一件还是一整套」,否则模型会把一件衣服画成好几片(实测踩过)
    if float_mode and garment_pieces:
        pieces = "、".join(g["label"] for g in garment_pieces)
        one = garment_pieces[0]
        is_set = (
            not _is_piece_name(one["name"])              # 拆件(「套装-吊带背心」)本身就是单件
            and ("套" in one["name"] or one["pics"] > 1)  # 名字带"套"或挂了多张图 = 一整套
        )
        if len(garment_pieces) > 1:
            # 分析已经把套装拆成了好几件道具:每件都有名字,逐件点名
            zh = (
                f"本镜头的 {pieces} 是同一套里的几个**单件**:"
                "**穿的时候必须一件一件按上面换装说明的三步顺序来 —— 贴身的先穿、外面的后穿;"
                "绝对不能先把最外层穿上、再往里面塞别的**;"
                "而且这几件必须**全都穿上**,不能把它们合并成一件,也不能漏掉其中任何一件。"
            )
            en = (
                f"{pieces} in this shot are the separate pieces of ONE outfit: they must be put on ONE BY ONE "
                "in the correct order - the innermost layer (camisole / base layer) first, then the middle "
                "layer, and only then the outer cardigan / jacket on top; never put the outer layer on first "
                "and tuck anything inside it. ALL of them must be worn - never merge them into one garment and "
                "never drop any of them."
            )
            # 同框有两件及以上「上半身衣物」-> 必须写死两层叠穿。
            # 背景:链上"第一次换上衣"的状态图(睡衣上衣+睡裤+吊带背心)里,模型会把里层的睡衣上衣
            # 吞掉、只画外层背心;那张图是要当尾帧锚用的,于是换装镜就名正言顺地把旧上衣脱了。
            tops = [g for g in garment_pieces if _is_top_garment(g.get("name"))]
            if len(tops) >= 2:
                names = "、".join(g["label"] for g in tops)
                zh += (
                    f"**这一镜上半身同时有 {len(tops)} 件上衣({names}),必须一眼看出是"
                    "「一层套一层」叠着穿的:**"
                    "**里层那件的领口、袖子、下摆都要露在最外面看得见;外层只盖住躯干中间那一块,"
                    "不许盖住里层露出来的领口、袖子、下摆;从正面要能同时看到里层和外层;**"
                    "**绝对不许把这两层合成一件、绝对不许把里层省掉、绝对不许让里层消失"
                    "(少了里层,后面那镜就会变成把人脱光的镜头)。**"
                )
                en += (
                    f" In this shot her upper body wears {len(tops)} layers at once ({names}) and it must be "
                    "visibly layered: the INNER layer's collar, sleeves and hem stay visible on the outside, "
                    "the outer layer covers only the middle of her torso and must not hide the inner layer's "
                    "sleeves, collar or hem. Never merge the two into one garment and never drop the inner "
                    "layer."
                )
        elif is_set:
            # 只有一套(没拆件):它是一整套,必须一件一件来、整身穿好 ——
            # 尤其不能一次糊上身、不能只换上半身、也不能把她那件打底复制成"飞过来的衣服"
            base = (float_base_outfit or "").strip() or "她身上那件打底"
            zh = (
                f"本镜头的 {pieces} 是**一整套**服装,里面有好几件(内搭 / 下装 / 外套):"
                "几张参考图是这套衣服的**不同件**,合起来才算完整。"
                "**要一件一件依次来、按正确顺序穿到她身上 —— 先穿贴身的内搭 / 吊带打底,再穿下装(裤子 / 裙子),"
                "最后才套外面的开衫 / 外套;绝对不能一次把整套一起套上身,也不能让同一件出现两次、"
                "或者漏掉其中任何一件**;"
                "**整套都要穿到她身上:上半身的内搭和外套、下半身对应的裤子 / 裙子 —— "
                "绝对不能只换掉上半身、下身还原样穿着打底的那件**。"
                f"**这套衣服的颜色、材质和版型全部以参考图为准;她身上那件打底({base})只是打底,"
                "不属于这套服装 —— 它不能被复制、不能多出一件飘在空中、也不能被当成要穿的那套。**"
            )
            en = (
                f"{pieces} in this shot is ONE complete outfit made of several pieces (base layer / bottoms / "
                "outer layer): the reference images are DIFFERENT PIECES of it and only together define the "
                "whole look. Bring them on ONE PIECE AT A TIME, in the correct order - innermost base layer "
                "(camisole) first, then the bottoms (trousers / skirt), and only then the outer cardigan / "
                "jacket; never put the whole outfit on in one go, never show the same piece twice and never "
                "drop any of them. The WHOLE outfit must end up on her: the top layers AND the matching "
                "bottoms - never change only the top half and leave the base bottoms on. "
                f"Colour, material and cut all come from the reference images; the base outfit she starts in "
                f"({base}) is just underwear-style base clothing, NOT part of this outfit - it must not be "
                "duplicated, must not float in as an extra garment, and must not be treated as the outfit."
            )
        else:
            # 这一镜只有单独一件衣服(拆镜后的常态):就说它是「一件」——
            # 说成"一整套 / 里面有好几件"会把模型直接带成"画好几片"
            zh = (
                f"本镜头的 {pieces} 是**单独的一件衣服**(不是一整套、也不是几块布):"
                "领口(或肩带)、袖子(如果有)和衣身**连在一起**,整件一起移动、形状完整;"
                "**不要出现没有形状的布条或布片,也不要把它变成两件或几件**。"
            )
            en = (
                f"{pieces} in this shot is a SINGLE garment (not an outfit and not a bundle of cloth): the "
                "neckline (or straps), sleeves (if any) and body stay JOINED as one piece that moves as a "
                "whole with its shape intact; never break it into shapeless strips or fragments and never "
                "turn it into two or more pieces."
            )
        defs.append(zh if lg.startswith("zh") else en)
        if lg.startswith("zh"):
            # 衣服参考图(尤其是"实拍拼图")里通常也带着那个模特的脸和碎发 ——
            # 不说清楚,模型会把人家的脸 / 发丝一起抄过来(实测:#92 换拼图后主角脸上多出一道
            # 像疤一样的线,就是拼图里的碎发被抄进来了)。脸永远只认 <Picture 1>。
            action = (
                f"{action}\n**她的脸和五官只以 <Picture 1> 为准:脸上绝对不许出现任何多余的"
                "线 / 疤 / 碎发 / 痕迹;而每件衣服的参考图只用来定义「那件衣服本身」—— "
                "参考图里那个人的脸、头发、皮肤、姿势、背景一律不要参考、不要出现在画面里。**"
            )
            # 起始套装要**在动作说明里点名**(带描述)——只写在 defs 里权重太低,
            # 实测模型会把睡衣上衣自己编成"斗篷"、把长睡裤编成"短裤"。
            # slot:读图给的「部位」(这里把键名统一成 slot)—— 换装说明拿它按部位给新旧件配对。
            base_items = [{"name": e.get("name"), "desc": e.get("desc"),
                           "slot": e.get("_slot") or ""}
                          for e in (present.get("props") or []) if e.get("_base")]
            if base_items:
                bits = []
                for e in base_items:
                    d = _clean_desc(e.get("desc"))
                    bits.append(f"「{e.get('name')}」" + (f"({d})" if d else ""))
                action = (
                    f"{action}\n**开始时她穿着"
                    + " + ".join(bits)
                    + ",都是实心、不透明的;它们必须一直和各自的参考图完全一致(版型、长短都要对),"
                      "绝对不许被换成别的样子(比如长上衣变成斗篷、长裤变成短裤)。**"
                )
            # 换装顺序的通用说明:件数 / 种类都由这一镜的「件名」推出来(见 quick_start)。
            # **必须拼进 action**,不能只放进 subject_definitions —— 动作说明会同时出现在
            # summary 和 detailed_description 里,那是模型最听的两段;实测只放 defs 时
            # 模型基本不照做(同一套规则,放 defs 出废片、放 action 就正常)。
            # 函数内延迟 import:quick_start 反向依赖本模块,放文件顶部会循环 import。
            from .quick_start import float_change_sequence_text
            # 「一直戴着的小配件」(墨镜 / 拖鞋 / 项链…)也交给这段说明去钉死:实测同一份提示词,
            # 有的种子它们全程都在、换一个种子整条片子一次都不出现 —— 必须在说明里逐件点名。
            acc_names = [e.get("name") for e in (present.get("props") or [])
                         if is_worn_accessory(e.get("name"))]
            seq = float_change_sequence_text(garment_pieces, base_items, lg,
                                             accessories=acc_names)
            # 「按件排镜」(一镜只换一件)不拼这段 —— 它是给「一镜穿完整套」写的:
            # 里面全是"接下来按顺序一件一件飞入""每换完一件、下一件才允许出现""整段里手臂
            # 只抬一次"这种多件节奏的话,单件镜里塞进去等于在跟模型说"后面还有衣服",
            # 反而把它带偏(实测对比:项目 155 的按件镜就没有这段,效果是好的)。
            # 单件镜该说的都在动作文案里(只有这一件会飞、旧件不许飞、贴纸贴好、无中间态)。
            if seq and not (float_mode and float_variant == "piece"):
                action = f"{action}\n{seq}"
        # 尺寸:服装参考图经常是"特写裁片"(衣服占满整张图),模型会照参考图的占比把衣服画成巨型 ——
        # 实测过一次:一件吊带被画成从胸口垂到脚踝的一大块布。所以这里写死"和真人身上那件一样大"。
        defs.append(
            "**服装参考图常是一张特写裁片(衣服占满整张图),不要照它在参考图里占满画面的比例放大** ——"
            "**每件的长短 / 宽窄都以它自己的参考图为准,必须看着就是「她穿着正好合身」的那一件,"
            "不要拉长成床单 / 长裙 / 披风,也不要缩得不像同一件**;"
            "**衣服全程待在她所在的景深处 —— 绝对不许朝镜头飞过来、不许凑到镜头跟前,"
            "不许因为离得近就显得很大,也绝不许糊住整个画面;"
            "它在她身上时多大,落到她身上时就多大**;"
            "**这一镜是「实拍一个空间」,不是拼贴:不要出现隐形人台 / 衣架 / 挂钩,"
            "不要出现分格、方块或第二层背景,也不要把衣服做成一张带边框的方形图片 —— "
            "衣服进来的时候就是「这件衣服本身干净完整的形状」;"
            "她的人和这件衣服在同一个空间里、同一个光线下。**"
            if lg.startswith("zh") else
            "**Every piece must be life-size as worn on a real person** - a garment reference may be a "
            "close-up crop, so do NOT scale it up to fill the frame: its length and width come from its own "
            "reference. It stays at the same depth as her - never flies towards the lens, never looks huge "
            "because it got too close, and never covers the entire frame. This is a real space, not a collage: "
            "no mannequin, hanger or hook, no panels or second background layer, and never a framed square "
            "image of the garment."
        )

    # no_ndm: 镜头库勾进来的通用分镜(系统内置 + 自定义) —— non_diegetic_music 段保留;
    # 机位/景别/背景/光线由动作文本自己写死(俯拍头顶出画这类写法和取景守则冲突),
    # 尾部只留「速度 + 画面」两句通用约束(TAIL_ZH_CUSTOM / TAIL_EN_CUSTOM)。
    sound_zh = "overall_soundscape:\n安静的环境底噪。\n\nnon_diegetic_music:\nNone."
    sound_en = "overall_soundscape:\nQuiet ambient room tone.\n\nnon_diegetic_music:\nNone."
    if no_ndm:
        tail_zh, tail_en = TAIL_ZH_CUSTOM, TAIL_EN_CUSTOM
    # 「一直戴着的小配件」(墨镜 / 帽子 / 项链 / 鞋…)**无条件**写进动作说明 ——
    # 动作说明会同时进 summary 和 detailed_description,那是模型最听的两段。
    # 实测只写在 <Prop> 定义里时,换个种子就整镜消失(用户报"化妆师补右脸,眼镜怎么没有了":
    # 手在脸上补妆,模型会为了"把脸露出来"自动把墨镜摘掉)。
    if lg.startswith("zh"):
        _accs, _seen_where = [], set()
        for e in (present.get("props") or []):
            n = e.get("name") or ""
            if not is_worn_accessory(n):
                continue
            where = wear_place(n)[0]
            # 同一部位只留第一件:实测计划里同时带「乐福鞋」和「拖鞋」(打底带的),
            # 两句都会写进提示词 -> 模型左右脚穿不同的鞋 / 一会儿拖鞋一会儿乐福鞋。
            if where in _seen_where:
                continue
            _seen_where.add(where)
            _accs.append(n)
        if _accs:
            _acc = "、".join(f"「{n}」({wear_place(n)[1]})" for n in _accs)
            action = (f"{action}\n**她身上一直戴着{_acc} —— 从头到尾都戴着:绝对不许摘下来、"
                      "绝对不许挪到别处;别人的手(化妆师 / 摄影师)碰到她的脸时也不许取下来 —— "
                      "镜头里始终能看到它戴在原位。**")
    if lg.startswith("zh"):
        # 「开合」短句:**summary 和 detailed_description 是模型最听的两段**(见本文件那条老注释),
        # 只写在 <Prop> 定义里它照样把开衫穿成敞开(实测项目 test09000 的自拍镜)。
        _fs = fasten_line([(e.get("name") or "", e.get("_slot") or "", e.get("_close") or "")
                           for e in (present.get("props") or [])])
        return (
            "subject_definitions:\n" + "\n".join(defs) + "\n\n"
            "summary:\n"
            f"[第 {index} 个镜头,共 {total} 个] {action}"
            + (f"\n{_fs}" if _fs else "") + "\n\n"
            "retention_analysis:\n" + "\n".join(retention) + "\n\n"
            "detailed_description:\n"
            f"{action}\n\n"
            + (f"{_fs}\n\n" if _fs else "")
            # 常开:只许穿清单里那几件,绝不许冒出清单外的内衣 / 胸衣(与开合无关,每镜都写)
            + NO_UNDERWEAR_TXT + "\n\n"
            + sound_zh + tail_zh
        )
    return (
        "subject_definitions:\n" + "\n".join(defs) + "\n\n"
        "summary:\n"
        f"[shot {index} of {total}] {action}\n\n"
        "retention_analysis:\n" + "\n".join(retention) + "\n\n"
        "detailed_description:\n"
        f"{action}\n\n"
        + sound_en + tail_en
    )





# ---------------- 静态图风格:普通 / 自定义分镜统一取景(用户 2026-10-22) ----------------
# 这两行直接写进每个分镜的提示词里,动作原文原样保留,后面各镜要改就单独改。
_STATIC_FRAME_LINES = (
    # 用「远景」这个镜头语言把人画小 —— 比写百分比更容易被模型理解(用户 2026-10-22)。
    "- 镜头:远景(long shot),摄影机离她好几米远,整个空间都收进画面,"
    "她只是画面里的一个人物,四周留出大片空白。",
    "- 取景:全景全身,她占画面高度约 70%(头顶离上沿约 15%、脚底离下沿约 15%);"
    "双脚和鞋完整在画面里。",
    "- Camera: static tripod, wide long shot from a distance; the camera is pulled far back, "
    "with a large empty margin above her head and below her feet. She occupies only about 70% "
    "of the frame height and stays centred in frame from the first frame to the last.",
)
# 动作原文里常带自己的景别(近景 / 半身 / 俯拍),会盖掉上面的取景 —— 在动作后面再说一遍,
# 并用「留白比例」这种可核对的说法压住(用户 2026-10-22:取景两行没有生效)。
_STATIC_FRAME_TAIL = (
    "- 以上动作全部在【画面】的取景范围内完成:头顶到画面上沿、鞋底到画面下沿各留约 15% 空白;"
    "不要裁头、不要裁脚、也不要把人物放大到填满画面;机位全程固定,不推近、不变焦。"
)

# 个别镜头自己的机位/景别和「全景全身 70%」冲突(俯拍只拍到嘴、七分身、近景…)——
# 在该镜的动作里带上这句标记,构建时就不写标准【画面】段和收口句,一切按动作自己写的来。
# (用户 2026-10-23:第 7 镜俯拍挥手打招呼要去掉【画面】)
_NO_FRAME_MARK = "本镜不用标准取景框"



# 人物头像:整片每一镜都挂,只锁头部(脸型 / 五官 / 发型 / 发色 / 肤色),不参与构图和姿势。
HEAD_REF_DEF = (
    "<Picture {n}> 是人物头部的标准参考图。**本镜她的头 / 脸一律以 <Picture {n}> 为准**:"
    "脸型、五官、发型、发长、发色、肤色都和 <Picture {n}> 完全一致,不要换成别人。"
    "这张图**只用来对齐头部**:身体姿势、服装、场景、景别、机位和她在画面里的大小,"
    "一律按本镜头自己的说明生成,不要照搬 <Picture {n}> 的构图和姿势。"
)


def head_def_text(n: int) -> str:
    return HEAD_REF_DEF.format(n=n)


def insert_into_person(prompt: str, line: str) -> str:
    """把一条人物相关的定义并进【人物】段末尾(没有【人物】段才退回放到最前面)。

    用户 2026-10-22:头像 / 穿着方式这类「人物定义」不要堆在提示词开头,放进【人物】里。
    """
    marker = "【人物】"
    i = prompt.find(marker)
    if i < 0:
        return line + "\n\n" + prompt
    j = prompt.find("\n\n【", i + len(marker))
    if j < 0:
        return prompt.rstrip() + "\n" + line + "\n"
    return prompt[:j].rstrip() + "\n" + line + prompt[j:]


# 动作里出现这些词 = 这是有真实环境的剧情镜(不是纯白棚拍),场景交给模型按描述画出来。
_SCENE_WORDS = (
    "卧室", "房间", "客厅", "厨房", "浴室", "阳台", "玄关", "鞋柜", "床", "窗",
    "小区", "大门", "街道", "人行道", "马路", "路口", "公园", "商场", "咖啡",
    "地铁", "站台", "闸机", "列车", "车厢", "公交", "公司", "写字楼", "办公楼",
    "办公室", "室外", "室内", "街景", "店铺", "便利店", "餐馆", "餐厅", "饭馆", "食堂",
    "路灯", "玄关", "工位", "电脑", "电梯", "楼梯",
)


def wants_real_scene(text: str) -> bool:
    t = str(text or "")
    # 动作里已经写明是纯白 / 无环境元素时,一律按白棚处理 —— 别被「咖啡杯」这类词骗到
    # (用户 2026-10-23 p349 第 4 镜:端咖啡杯是纯白背景,却被判成咖啡厅,背景发灰)。
    if "纯白" in t or "无其他环境元素" in t or "白背景" in t:
        return False
    t = t.replace("纯白背景", "").replace("白背景", "")
    return any(k in t for k in _SCENE_WORDS)


# 动作里写了墙 / 地毯 / 雪地 / 陈设的镜(靠着白墙、脚下是雪、旁边有圣诞树)——
# 背景不能再一刀写成「整块纯白影棚纸」(用户 2026-10-23「不能都写成纯白」)。
# 注意别把泛泛的「地面 / 影子」算进来:那是每镜都有的,一算就全都走不到纯白分支。
_ENV_HINTS = (
    "墙", "地毯", "雪地", "雪堆", "泡沫雪", "圣诞树", "礼盒", "黑板", "白板", "背景板",
    "床", "沙发", "柜", "窗帘", "窗", "台阶", "舞台", "墙角", "转角",
)


def _has_env_detail(text: str | None) -> bool:
    return any(k in str(text or "") for k in _ENV_HINTS)


# 「画面」那一行通常写死「不要出现文字」;但动作里明确要求黑板上写字时,
# 这条会跟黑板打架(字要么写不出来、要么糊掉),所以按动作切两种说法。
_PICTURE_RULE = "- 画面:写实、主体清晰、有质感;不要偏色;不要出现文字、字幕、水印、logo、品牌名。"
_PICTURE_RULE_TEXT = (
    "- 画面:写实、主体清晰、有质感;不要偏色;"
    "**画面里唯一的文字就是动作描述里写的那块背景板上的字** —— 白粉笔、笔画粗、字号大、"
    "正面朝向镜头、清楚可读;除此之外不要出现别的文字、字幕、水印、logo、品牌名。"
)
_TEXT_BOARD_HINTS = ("黑板", "白板", "背景板", "写着", "写字")


# 包 / 手袋这类「拿在身上」的配件(不是穿在身上的衣服,也不是墨镜那种小件)。
# 用户 2026-10-23 报 p349 第 12 镜「鞋旁边多了一团黑色东西」:那一镜她双手捧着平板、
# 人还在走,提示词却只写「随身带着」—— 包没地方放,模型就把它揉成一坨黑块丢在脚边。
# 所以包必须写死「一直挂在她身上、离地面有距离」,而且**动作里没提到时根本不加**(见 projects)。
_CARRY_HINTS = ("包", "袋", "手拿", "手提", "挎", "拎")
# 「包臀裙 / 包边」这类只是名字里带「包」,不是包
_CARRY_NOT = ("包臀", "包边", "包跟", "包装")

# 动作文本里出现这些词 = 这一镜真的要她带着包
_CARRY_WORDS = ("包", "挎", "手袋", "拎")

_CARRY_TAIL = (
    "一直带在她身上(挎在肩上 / 提在手里,包身贴着她身侧、离地面还有一段距离),"
    "跟着她一起移动;整镜保持这个样子。"
)


def is_carried_prop(name: str | None) -> bool:
    """这件是不是「包 / 手袋」这类**拿在身上**的配件。"""
    n = str(name or "")
    if any(k in n for k in _CARRY_NOT):
        return False
    return any(k in n for k in _CARRY_HINTS)


def mentions_carry(text: str | None) -> bool:
    """动作里写了包没有 —— 只在写了的时候才把包挂进这一镜(用户 2026-10-23)。"""
    t = str(text or "")
    return any(k in t for k in _CARRY_WORDS)


# 深色单品(黑裙 / 黑裤 / 黑上衣)在纯白棚拍里最容易被压成一片死黑,
# 面料褶皱和纹理全糊掉(项目 testsdfsdfse 的黑长裙)。只加光影约束,不改颜色本身。
DARK_FABRIC_TIP = (
    "深色面料不要压成一片死黑:褶皱和垂坠处要有清晰的明暗层次、柔和反光"
    "和布料本身的纹理,暗部保留细节"
)

# 外穿件(开衫 / 外套 / 大衣)在「飞过来穿上」这类镜头里最容易被画成「披在肩上」——
# 用户 2026-10-23 第 1 镜:开衫整个滑到肩头,两条手臂根本没穿进袖子。
WORN_PROPERLY_TIP = (
    "必须真正穿在身上:两个肩头都稳稳搭在她自己的两个肩膀上;两只手都从袖口伸出来、"
    "露在袖管外面;从肩膀到手腕的整条手臂都包在袖管里,袖管贴着胳膊 —— "
    "不许露肩、不许掉肩、不许只穿一只袖子,不许滑到手臂或肘部,不许出现空荡下垂的袖管,"
    "不许像披肩 / 披巾那样搭在肩上;整段里保持这个穿法不变;"
    "从侧后方看过去时,开衫的肩线一样要盖住两侧肩头 —— "
    "画面里露在外面的皮肤只有脸、脖子和两只手,肩膀 / 上臂 / 后背都不露肤"
)
# 无袖的件不能要求「手臂穿进袖子」
_SLEEVELESS_HINTS = ("马甲", "背心", "吊带", "抹胸", "胸衣")
# 带袖的外穿件
_OUTER_SLEEVED_HINTS = ("开衫", "外套", "大衣", "风衣", "西装", "夹克", "衬衫", "衬衣", "卫衣", "毛衣", "罩衫")


def is_outer_sleeved(name: str, slot: str = "") -> bool:
    """这件是不是「带袖子的外穿件」—— 只有它们才要强调手臂必须穿进袖子。"""
    n = str(name or "")
    if any(k in n for k in _SLEEVELESS_HINTS):
        return False
    _place, outside = parse_slot(slot)
    return outside or any(k in n for k in _OUTER_SLEEVED_HINTS)


def _prop_def_line(i: int, name: str, e: dict, ref: str, role: str) -> str:
    """静态风格【道具定义】里的一行 —— **只负责定义这件东西本身**。

    用户 2026-10-22:每件衣服 / 配饰的长短、开合、「以哪张参考图为准」都写进它自己那条定义里。
    用户 2026-10-23:**不要再补「她身上穿着这一件,整镜保持原样」「戴在眼睛上…整镜保持这个样子」
    这类话** —— 「穿在身上、整镜保持」是【身上穿着】那一段的事,道具定义里重复只会让模型
    把同一件东西再画一遍,产生奇怪画面。
    """
    d = str(e.get("desc") or "").strip().rstrip("。;；")
    # 长短必须锚在参考图上。**不要写「长款」这种词** —— 用户 2026-10-23 第 14 镜:
    # 驼色中长开衫因为写了「长款」+ 大半身裁切,被模型拉成了拖地长风衣。
    _len = str(e.get("_len") or "").strip()
    if role == "worn" and _len in ("短", "长"):
        d += (";" if d else "") + "长度与下摆位置以参考图为准(不要自行加长或缩短)"
    # ref 已经是「参考图 <Picture 3>」这种成品写法
    src = ref or "上面的文字描述"
    bits = [d] if d else []
    bits.append(f"外观 / 颜色 / 材质 / 版型以{src} 为准")
    if role == "worn":
        _fs = fasten_short(name, str(e.get("_slot") or ""),
                           (e.get("_close") or "").strip().lower() or "closed")
        if _fs:
            bits.append(f"穿法:{_fs}")
        if is_outer_sleeved(name, str(e.get("_slot") or "")):
            bits.append(WORN_PROPERLY_TIP)
        if ("黑" in str(name or "") or "黑" in d) and not any(
                k in str(name or "") for k in ("鞋", "靴", "拖鞋")):
            bits.append(DARK_FABRIC_TIP)
    # 鞋:参考图是「一双两只看正面」的商品图,写成"脚上就这两只"(只定义数量,不再写动作)
    if any(k in str(name or "") for k in ("鞋", "靴", "拖鞋")):
        bits.append("一双两只:左脚一只、右脚一只")
    elif is_carried_prop(name):
        bits.append("挎在肩上 / 提在手里,包身垂在身侧")
    return f"- <Prop {i}>「{name}」:" + ";".join(bits) + "。"


def build_universal_prompt(
    shot: dict,
    present: dict[str, list[dict]],
    picture_of: dict[str, list[int]],
    index: int,
    total: int,
    wear_ref_pics: list[int] | None = None,
    include_unmapped: bool = False,
    full_body_frame: bool = False,
    scene_from_action: bool = False,
) -> str:
    """「通用分镜 / 自定义分镜」的紧凑提示词(按件排镜里的细节镜用)。

    这类镜头的**动作文本本身就已经写好了景别 / 机位 / 动作**(俯拍、半身、低头阅读、
    手挡脸自拍…),所以只补三块上下文 —— 场景、人物、身上穿着 / 道具 —— 再接动作原文,
    最后给两句统一约束。

    **刻意不写**:固定全身机位 / 取景守则 / 场景光线那套模板(会和俯拍、半身景别打架),
    以及 retention_analysis 和一堆"绝不许…"的黑名单(用户 2026-10-20:提示词要专业、没用的删掉)。
    """
    title = shot.get("title") or f"镜头 {index}"
    action = (shot.get("action") or title).strip()
    action, to_remove = _adapt_action_to_garments(action, present)
    # 动作里要求黑板上写字时,「不要出现文字」那条要放宽(见 _PICTURE_RULE_TEXT)
    _has_onboard_text = any(k in action for k in _TEXT_BOARD_HINTS)
    props = [e for e in (present.get("props") or []) if e.get("name") not in to_remove]
    chars = present.get("characters") or []
    scenes = present.get("scenes") or []

    # 场景(有背景图就以它为准;否则纯白棚拍)
    scene_pics: list[int] = []
    for sc in scenes:
        got = picture_of.get(f"scenes:{sc.get('name') or ''}") or []
        if got:
            scene_pics = got
            break

    L = [f"【镜头】第 {index} / {total} 镜 · {title}", ""]

    L.append("【场景】")
    if scene_pics:
        L.append(f"- 背景以{_pic_ref(scene_pics, 'zh')}为准:墙面、地面、陈设、空间大小"
                 "和光线都照它,全程不变。")
        if "纯白背景" in action:
            action = action.replace("场景为纯白背景,无其他环境元素,", "")
            action = action.replace("纯白背景", "背景参考图里的场景")
    elif scene_from_action:
        # 剧情镜(卧室 / 小区 / 地铁 / 写字楼…):没有背景图,场景由动作描述自己带出来
        L.append("- 环境按【动作】里描述的场景生成:真实空间,墙面、地面、陈设、光源都照描述来,"
                 "光线自然;同一镜内场景不切换、不增减陈设,不要变成纯白影棚。")
    else:
        # 用户 2026-10-23:只写「纯白背景」模型会画成灰底 + 渐变(泡泡那镜尤其明显)。
        # 但动作自己写了墙 / 地面 / 雪地 / 陈设的镜(靠在白墙边、脚下是雪)不能一刀切成白纸。
        if _has_env_detail(action):
            L.append("- 场景按【动作】里的描述搭建:墙、地面、陈设和光线都照动作写的那样,"
                     "同一镜内的场景不切换;背景保持干净、颜色均匀,动作里没写的东西不要加。")
        else:
            L.append("- **背景是一整块纯白无缝影棚纸:颜色从上到下、从左到右完全一致,"
                     "是干净的纯白(接近 RGB 255)**;柔和均匀的棚拍光,没有其他陈设。")
        # 动作原文里自带的"场景为纯白背景…"和上面这句重复,去掉。
        action = action.replace("场景为纯白背景,无其他环境元素,", "")

    # 人物
    person_pics: list[int] = []
    for ch in chars:
        got = picture_of.get(f"characters:{ch.get('name') or ''}") or []
        if got:
            person_pics = got
            break
    pdesc = " ".join(str(ch.get("desc") or "").strip() for ch in chars if ch.get("desc")).strip()
    L.append("")
    L.append("【人物】")
    if person_pics:
        L.append(f"- 长相 / 发型 / 妆容:只以{_pic_ref(person_pics, 'zh')} 为准"
                 + (f"({pdesc})" if pdesc else "")
                 + ";参考图里的内衣和露出的皮肤不照搬。")
    else:
        # desc 允许写成多行(静态风格要按参考图分工逐条写),每行各成一条 bullet
        for _ln in (pdesc or "画面里的人物").split("\n"):
            _ln = _ln.strip()
            if _ln:
                L.append(_ln if _ln.startswith("- ") else "- " + _ln)

    # 身上穿着 / 配饰 / 道具(只写有参考图的 —— 没图写进去等于让模型凭空画)
    worn: list[str] = []
    worn_names: list[str] = []
    accs: list[str] = []
    tools: list[str] = []
    # 静态图风格:每件写成一条 <Prop N> 定义(用户 2026-10-22「加一个【道具定义】」),
    # 长短 / 开合 / 「以哪张参考图为准」都挂在各自那条上。
    prop_defs: list[str] = []
    for e in props:
        nm = (e.get("name") or "").strip()
        pics = picture_of.get(f"props:{nm}") or []
        # 默认只写「有参考图」的件(没图写进去等于让模型凭空画);
        # include_unmapped=True 是静态图风格的用法:整身已经穿在首帧里,只保留文字、不带图号。
        if not nm or (not pics and not include_unmapped):
            continue
        ref = _pic_ref(pics, "zh") if pics else ""
        _p = f"({ref})" if ref else ""
        if is_garment_name(nm, e.get("_slot") or "") or e.get("_worn"):
            role = "worn"
            worn.append(f"「{nm}」{_p}")
            worn_names.append(f"「{nm}」")
        elif is_worn_accessory(nm):
            role = "accessory"
            accs.append(f"「{nm}」({ref + ',' if ref else ''}{wear_place(nm)[1]})")
        else:
            role = "tool"
            tools.append(f"「{nm}」{_p}")
        if full_body_frame:
            prop_defs.append(
                _prop_def_line(len(prop_defs) + 1, nm, e, ref, role)
            )
    # 头像参考图通常是「裸脸」素材,模型会优先照它画,导致本镜明明有墨镜却没戴。
    # 动作里没提到墨镜时,补一条强约束;提到「推到头顶」这类动作时不补,免得打架。
    _sunglasses_lock = (
        "- **墨镜是固定配饰:她脸上全程戴着墨镜,镜片遮住眼睛;头像参考图里的裸眼只是素材,"
        "不要照搬;不许摘下来、不许画成透明镜片。**"
    )
    _need_sunglasses_lock = any("墨镜" in a for a in accs) and "墨镜" not in action
    # 镜头库的动作里常写着「画面里只有她身上这件「X」」—— 那一句是为"只看到一件"写的,
    # 现在她一身穿着好几件,照抄会逼模型把别的衣服丢掉。改成把本镜这几件都列上。
    if len(worn_names) >= 2 and "画面里只有她身上这件" in action:
        action = re.sub(r"画面里只有她身上这件「[^」]+」",
                        "画面里只有她身上这几件:" + "、".join(worn_names), action)
    # 外穿件(开衫 / 外套)在全身分镜里也常被画成「披在肩上」——
    # 用户 2026-10-23 第 4 镜:开衫挂在肩头、两臂没进袖子。这里补一条穿法定义。
    _outer_names = [
        str(e.get("name") or "") for e in props
        if e.get("_worn") and is_outer_sleeved(
            str(e.get("name") or ""), str(e.get("_slot") or ""))
    ]
    _outer_line = (
        "- " + "、".join(f"「{n}」" for n in _outer_names) + " " + WORN_PROPERLY_TIP + "。"
    ) if _outer_names else ""
    if full_body_frame and prop_defs:
        # 用户 2026-10-23:静态风格的分镜也要有【身上穿着】和【配饰】这两段(和「扔过来飞入」一样),
        # 只在【道具定义】里逐件写,模型容易把整身当成"待穿的一堆件"。
        if worn:
            L.append("")
            L.append("【身上穿着】")
            L.append("- " + "、".join(worn) + " —— 每件分开穿在身上,实心、不透明;"
                     "**每件的长短和下摆位置都以各自参考图为准,画面的裁切不会改变衣服长度**;"
                     "**门襟开合状态、扣子的数量 / 位置 / 大小 / 排列都以参考图为准**"
                     "(各件的开合按下面【道具定义】里那件的「穿法」来),"
                     "不要自行敞开、不要增减或重排扣子;整镜保持这个样子。")
            if _outer_line:
                L.append(_outer_line)
        if accs:
            L.append("- 配饰:" + "、".join(accs) + " —— 全程戴在原位。")
            if _need_sunglasses_lock:
                L.append(_sunglasses_lock)
        L.append("")
        L.append("【道具定义】")
        L.extend(prop_defs)
    elif worn:
        L.append("")
        L.append("【身上穿着】")
        L.append("- " + "、".join(worn) + " —— 每件分开穿在身上,实心、不透明;"
                 "扣子的数量 / 位置 / 大小 / 排列和参考图完全一致,不要增减或重排扣子;"
                 "她身上只有这里列出的这几件,不要再加背心、吊带或打底衫。")
        if _outer_line:
            L.append(_outer_line)
        # 「开合」按页面里这件衣服的设置走(closed / half / open);没配置的按下扣上处理,
        # 不再让模型自己猜(用户 2026-10-22:auto 页面对每件衣服都有开合配置)。
        _fs = [
            t for t in (
                fasten_short(e.get("name") or "", e.get("_slot") or "",
                             (e.get("_close") or "").strip().lower() or "closed")
                for e in props if e.get("_worn")
            ) if t
        ]
        if _fs:
            L.append("- **穿法:" + ";".join(_fs) + "。**")
    if not full_body_frame:
        if accs:
            L.append("- 配饰:" + "、".join(accs) + " —— 全程戴在原位。")
            if _need_sunglasses_lock:
                L.append(_sunglasses_lock)
        if tools:
            L.append("")
            L.append("【道具】")
            L.append("- " + "、".join(tools) + "。")
    if wear_ref_pics:
        L.append(f"- {_pic_ref(wear_ref_pics[:1], 'zh')} 是这套衣服的实拍穿着参考:"
                 "只参考衣服的款式 / 颜色 / 材质 / 穿法,不参考画面里的人、脸、身材和背景。")

    # 动作(原样保留:景别 / 机位 / 动作都在里面)
    L.append("")
    if full_body_frame:
        # 动作里带了「本镜不用标准取景框」标记的镜(俯拍只拍到嘴这类),不写标准【画面】段
        # (用户 2026-10-23:第 7 镜俯拍挥手,标准全身 70% 取景和它自己的景别打架)。
        _no_std_frame = _NO_FRAME_MARK in action
        if not _no_std_frame:
            # 静态图风格:取景单独成【画面】,动作原文原样放进【动作】(用户 2026-10-22 要求分开)
            L.append("【画面】")
            L.extend(_STATIC_FRAME_LINES)
        L.append("")
        L.append("【动作】")
        L.append(action)
        # 动作自己的景别会盖掉取景:动作之后再收一次口。
        if not _no_std_frame:
            L.append(_STATIC_FRAME_TAIL)
    else:
        L.append("【画面 / 动作】")
        # 动作文本里完全没写景别 / 机位时(自定义输入往往就一句动作)才补一句最通用的,
        # 免得模型把近景动作拍成全身、或者自己乱运镜。写了就**一律不动**。
        _cues = ("特写", "近景", "中景", "半身", "全身", "全景", "远景", "俯拍", "仰拍",
                 "跟拍", "机位", "镜头")
        if not any(c in action for c in _cues):
            L.append("- 景别:半身(头部和上半身完整入画),机位固定、不推拉摇移。")
        L.append(action)

    # 统一约束(动作文本里已经写过就不再重复)
    L.append("")
    L.append("【统一约束】")
    # 参考图里有她本人,模型很容易顺手多画一个「第二个人」(实测 p339 s01 床上出现两个人)
    L.append("- 人物:画面里**只有一个人** —— 就是本镜文案里写的她;不要出现第二个女性、"
             "她的克隆 / 镜像 / 倒影,也不要把参考图里的人当成另一个人再画一遍。"
             "（文案里明确写到的人除外）")
    if prop_defs:
        # 只做正向描述(本项目的经验:列一长串"不要出现 XX"反而会把 XX 画出来)。
        # 别写「地面上只有她的影子」—— 有狗 / 圣诞树 / 气球的那几镜会打架(用户 2026-10-23)。
        L.append("- 画面干净:背景和地面保持空净清爽;"
                 "画面里出现的每一样东西都是上面写明的那几样,数量、位置按说明来。")
    if "速度:" in action:
        L.append(_PICTURE_RULE if not _has_onboard_text else _PICTURE_RULE_TEXT)
    else:
        L.append("- 速度:所有动作按正常实时速度发生,不要慢动作、不要慢放。")
        L.append(_PICTURE_RULE if not _has_onboard_text else _PICTURE_RULE_TEXT)
    return "\n".join(L)


# ------------------------------------------------------------------ LLM 分析
SYSTEM_ZH = (
    "你是短视频分镜师。用户给你一段剧本和镜头数量,请你:\n"
    "1. 列出剧本里出现的实体:characters(人物)、props(道具)、scenes(场景)。"
    "每个实体给出 name(简短中文名)和 desc(不超过 20 字的中文外观/环境描述)。没有就留空数组。"
    "注意:\n"
    "  (1) 如果剧本里有一件被**单独拿在手里、抛起、悬浮、从画外飞进来或单独展示**的服装或道具"
    "(比如整套服装、包),必须把它**单独列为 props 里的一个道具**,不要并进人物里;\n"
    "  (2) 如果这是**一整套多件衣服**(比如「吊带背心 + 开衫 + 阔腿裤」),必须**按件拆开、"
    "每一件单独列一个道具**,名字用「同一前缀 + 破折号 + 单件名」(例如「套装-吊带背心」「套装-开衫」"
    "「套装-阔腿裤」)。**件数不限,描述里有几件就拆几件** —— 四件、五件都一样,"
    "多加了一件棉袄就多一个「套装-棉袄」。这样才能给**每一件各绑一张参考图**,模型才不会把几件揉成一件。"
    "如果剧本里没写清是哪几件,就照原文列一个道具(例如「套装」) —— **服装绝对不能漏**。"
    "另外,**她身上那件基础打底(例如「黑色细吊带」「黑色短裤」)不算道具,不要列**;\n"
    "2. 把剧本拆成指定数量的镜头,顺序与剧情一致,不新增也不遗漏剧情。"
    "**如果剧本已经按段落切好了镜头(一段一个镜头),而且段落数正好等于要求的镜头数,"
    "就一段严格对应一个镜头,不要拆分、也不要合并段落。**\n"
    "3. 每个镜头给出:title(不超过 12 个汉字)、action(中文一句话描述画面,含主体动作、"
    "镜头景别(远景/中景/近景/特写)、环境与光线;"
    "**原剧本里写了景别的(全景/中景/近景/特写),action 必须沿用那个景别,不要自己改写**;"
    "**如果原剧本写明人物此刻的穿着状态(例如「只穿黑色细吊带和黑色短裤、赤脚」或「已经穿上套装」),"
    "action 里必须原样保留这句,不能省略**;"
    "只描述画面,不要写台词、字幕、配乐、水印、品牌名)、"
    "seconds(建议时长,单位秒,由你按内容决定,不要所有镜头都一样长,总和接近用户给的总时长)、"
    "以及该镜头出现的 characters / props / scenes(填实体的 name,必须是上面清单里的名字,没有就留空数组)。\n"
    '4. 只输出 JSON,格式严格为:{"characters":[{"name":"","desc":""}],"props":[],"scenes":[],'
    '"shots":[{"title":"","action":"","seconds":4.5,"characters":[],"props":[],"scenes":[]}]}'
)

SYSTEM_EN = (
    "你是短视频分镜师。用户给你一段剧本和镜头数量,请你:\n"
    "1. 列出剧本里出现的实体:characters(人物)、props(道具)、scenes(场景)。"
    "每个实体给出 name(简短中文名)和 desc(不超过 20 字的中文外观/环境描述)。没有就留空数组。\n"
    "2. 把剧本拆成指定数量的镜头,顺序与剧情一致。\n"
    "3. 每个镜头给出:title(不超过 12 个汉字)、action(one English sentence describing the picture: "
    "subject action, shot size, environment, lighting; no dialogue/subtitles/music/watermark/brand)、"
    "seconds(建议时长,单位秒,按内容决定,总和接近用户给的总时长)、"
    "以及该镜头出现的 characters / props / scenes(填实体 name,没有就留空数组)。\n"
    '4. 只输出 JSON,格式严格为:{"characters":[{"name":"","desc":""}],"props":[],"scenes":[],'
    '"shots":[{"title":"","action":"","seconds":4.5,"characters":[],"props":[],"scenes":[]}]}'
)


def _system_prompt() -> str:
    return SYSTEM_ZH if _lang().startswith("zh") else SYSTEM_EN


# 「内容清单」模式:用户一行写一条要拍的东西,LLM 只把它排成分镜,不自己编内容。
SYSTEM_CONTENT_ZH = (
    "你是短视频分镜师。用户给你一份「内容清单」——一行一条,写的是他要在片子里拍到的东西。\n"
    "你的任务**只是把这份清单排成一张分镜表**,**不要自己编内容**。\n"
    "\n"
    "1. **一条内容 = 一个镜头**;镜头数量必须等于清单的条数,顺序就按他写的顺序,"
    "**不许合并、不许拆分、不许调整顺序**;\n"
    "2. **不要输出正文(action)**:正文由我们照抄清单那一行,你只用 i 标出「这是第几条」。"
    "你只负责:title(这个镜头的短名字)、seconds(这条大概几秒)、以及这条内容里出现的实体。"
    "**绝对不要替他加戏、加道具、加剧情**;\n"
    "3. 清单里每条写的细节(包括人物此刻穿什么、镜头要求)都会原样使用,你不用改写、也不用复述;\n"
    "4. 每条内容里出现的实体,照实列进 characters / props / scenes,别漏也别编;"
    "**scenes 的 name/desc 只许写清单里明确写到的环境元素**(比如清单写了「白色墙」就写「白色墙」),"
    "**绝对不许自己往里加清单里没有的东西** —— 不许加地毯、家具、植物、摆件、圣诞树、"
    "门窗、窗帘这类陈设,也不许加清单里没写的光线 / 颜色;"
    "**服装绝对不能漏**:清单里出现了一整套衣服(比如「套装」)时 ——"
    "上面**给了服装描述**就按件拆开、每件单独列一个道具,名字用「同一前缀 + 破折号 + 单件名」"
    "(例如「套装-吊带背心」「套装-开衫」「套装-阔腿裤」;**描述里有几件就拆几件,四件、五件都一样,"
    "多加一件棉袄就多一个「套装-棉袄」**);"
    "上面**没给服装描述**就照原文列一个道具(例如「套装」),不要凭空猜件数,但**绝对不能不写**。"
    "另外,**她身上那件基础打底(例如「黑色细吊带」「黑色短裤」)不算道具,不要列** —— "
    "要展示的那套衣服才算;\n"
    "5. 每个镜头给出:i(第几条,从 1 开始)、title(不超过 12 个汉字)、"
    "seconds(这条内容大概需要几秒:只有一个动作的短镜 1~2.5 秒,带完整过程的最多 5 秒,"
    "总和接近用户给的总时长)、以及该镜头出现的 characters / props / scenes;\n"
    "6. 某条内容写得简单或者只有半句,就照实标出那条的 title 和实体即可,不要替他补全剧情;\n"
    '7. 只输出 JSON,格式严格为:{"characters":[{"name":"","desc":""}],"props":[],"scenes":[],'
    '"shots":[{"i":1,"title":"","seconds":4.5,"characters":[],"props":[],"scenes":[]}]}'
)


# 模型常把"没有外观描述"写成"无"这类占位。直接存下来会出问题:
# build_prompt 里 `tip = desc or garment_desc` —— "无"算真值,会把真正的服装描述顶掉,
# 于是提示词里连这套衣服是什么样都没交代。
_PLACEHOLDER_DESC = {"无", "无。", "没有", "暂无", "未指定", "none", "null", "n/a", "na", "-", "—"}


def _is_piece_name(name: str) -> bool:
    """件名像「套装-吊带背心」这种「同一前缀 + 破折号 + 单件名」—— 说明它是拆出来的单件。"""
    return "-" in (name or "") or "—" in (name or "")


def _piece_desc(name: str, garment_desc: str) -> str:
    """从「整套服装描述」里挑出这一件自己那句话。

    拆件兜底时直接拿整套描述会出事:一套四件的描述里把吊带 / 裤子 / 开衫 / 小方包全列上了,
    而这一镜只该出现吊带 —— 结果模型把整套连包一起画了出来(实测踩过)。
    这里按件名的尾段(「套装-蕾丝吊带背心」-> 「蕾丝吊带背心」)在整套描述里找那一小句;
    找不到就干脆不写,靠参考图说话。
    """
    gd = (garment_desc or "").strip()
    tail = re.split(r"[-—·]", name or "")[-1].strip()
    if not gd or not tail:
        return ""
    for part in re.split(r"[①②③④⑤⑥⑦⑧⑨⑩;；,、\n]", gd):
        if tail in part:
            return part.strip()
    return ""


def _clean_desc(text: str | None) -> str:
    t = (text or "").strip()
    return "" if t.lower() in _PLACEHOLDER_DESC else t


def _clean_entities(raw) -> list[dict]:
    out: list[dict] = []
    if not isinstance(raw, list):
        return out
    for item in raw:
        if isinstance(item, str):
            name, desc = item.strip(), ""
        elif isinstance(item, dict):
            name = str(item.get("name") or "").strip()
            desc = _clean_desc(item.get("desc"))
        else:
            continue
        if name and all(e["name"] != name for e in out):
            out.append({"name": name[:40], "desc": desc[:80]})
    return out


def _shot_from_item(item: dict, action: str, plan: dict, title: str = "") -> dict:
    """把模型给的一条分镜(不含正文)拼成内部镜头:正文用 action(调用方决定从哪来)。

    title 是调用方给的**确定名字**(内容清单行首的【…】),有它就压过模型给的名字。
    """
    try:
        seconds = max(0.0, float(item.get("seconds")))
    except (TypeError, ValueError):
        seconds = 0.0
    shot = {
        "title": (str(title or "").strip()
                  or str(item.get("title") or "").strip()
                  or beat_title(action, 12))[:40],
        "action": action,
        "seconds": seconds,
    }
    for kind in KINDS:
        names = item.get(kind)
        known = {e["name"] for e in plan[kind]}
        shot[kind] = [n for n in (names or []) if isinstance(n, str) and n in known]
    return shot


def _normalize(data: dict | None, count: int, content_lines: list[str] | None = None) -> dict | None:
    if not isinstance(data, dict):
        return None
    plan = {k: _clean_entities(data.get(k)) for k in KINDS}

    raw_shots = data.get("shots")
    if not isinstance(raw_shots, list):
        return None

    if content_lines:
        # 内容清单模式:正文**由我们照抄清单那一行**(模型只回了 i / title / seconds / 实体)。
        # 以前让模型把正文也复述一遍,22 条长正文直接撑爆输出的 token 上限 →
        # 返回被截断、整份 JSON 解析失败 → 悄悄退化成规则切分,实体全空(实测踩过)。
        by_i: dict[int, dict] = {}
        for pos, item in enumerate(raw_shots, start=1):
            if not isinstance(item, dict):
                continue
            try:
                i = int(item.get("i") or item.get("index") or pos)
            except (TypeError, ValueError):
                i = pos
            by_i.setdefault(i, item)
        shots = [_shot_from_item(by_i.get(i, {}), body, plan, title=tag)
                 for i, line in enumerate(content_lines, start=1)
                 for tag, body in (_split_line_tag(line),)]
        plan["shots"] = shots[:count] if count else shots
        return plan

    shots: list[dict] = []
    for item in raw_shots:
        if not isinstance(item, dict):
            continue
        action = str(item.get("action") or item.get("prompt") or "").strip()
        if not action:
            continue
        shots.append(_shot_from_item(item, action, plan))
    if not shots:
        return None
    plan["shots"] = shots[:count]
    return plan


def _rule_plan(script: str, count: int) -> dict:
    """规则兜底(模型用不了时)。**内容清单形态(非空行数 = 镜头数)直接一行一镜** ——
    以前一律丢给 split_beats 按句切,切点常常落在句子中间:正文被拦腰截断、行首的
    【分镜名】也丢了,名字就变成碎句(实测项目 195/197)。一行一镜时名字和正文都是完整的。
    """
    lines = [s.strip() for s in (script or "").replace("\r\n", "\n").split("\n") if s.strip()]
    beats = lines if (count and len(lines) == count) else _fit_to_count(split_beats(script), count)
    shots = []
    for b in beats:
        tag, body = _split_line_tag(b)
        shots.append({"title": tag or beat_title(body, 12), "action": body, "seconds": 0.0,
                      "characters": [], "props": [], "scenes": []})
    return {"characters": [], "props": [], "scenes": [], "shots": shots}


# 「排分镜」这一步的硬超时(秒)。模型过载时**连接会建起来但迟迟不返回** —— 用 settings 里那个
# 180 秒的大超时、失败再重试一次,就是 6 分钟;UI 上一直停在
# 「生成提示词 —— 编排换装顺序与动作」不动(用户报"怎么一直在这个状态不动啊")。
# 排分镜是很小的任务(几行清单),正常几秒就回;60 秒还没回说明后端已经用不了,早点退到规则兜底。
_ANALYZE_TIMEOUT = 60


def analyze_script(
    script: str,
    shot_count: int,
    total_seconds: float,
    content_mode: bool = False,
    garment_desc: str = "",
) -> tuple[dict, str, str]:
    """返回 (plan, engine, detail)。plan 含 characters/props/scenes/shots。

    content_mode=True 时按「内容清单」处理:一行一条,LLM 只排分镜,不自己编内容。
    garment_desc 是这件衣服的客观描述 —— 给了它,模型才能把一整套按件拆成
    「套装-吊带背心」「套装-开衫」这种独立道具,让用户逐件绑参考图。
    """
    # 内容清单模式:**镜头数 = 清单行数**,一律不看客户端传进来的 shot_count。
    # 客户端那个数是前端按"第 3 栏件数 × 2 + 1"估的,把「包 / 围裙」这类**携带件**、
    # 或者误放进「常驻配饰」栏的衣服算漏时它就偏小 —— 以前这里按它截断 shots,清单尾巴
    # (最后那件衣服的「飞入换装」镜 + 后面的细节镜)整段被切掉:那一件永远进不了状态链,
    # 商品图也绑不上,最后一镜只剩个标题(实测项目 224 / 234 的"分镜全乱了")。
    content_lines: list[str] = (
        [s.strip() for s in (script or "").split("\n") if s.strip()] if content_mode else []
    )
    count = len(content_lines) or shot_count
    if llm.available():
        gd = garment_desc.strip()
        garment_line = f"\n这套服装的客观描述(拆单件时以它为准):{gd}\n" if gd else ""
        lines: list[str] = []
        if content_mode:
            lines = content_lines
            system = SYSTEM_CONTENT_ZH
            ask = (f"内容清单(共 {len(lines)} 条,一行一条):\n{script}\n{garment_line}\n"
                   f"请按一行一条排成 {len(lines) or shot_count} 个镜头"
                   f"(一条一个镜头,不要合并或拆分),总时长约 {total_seconds} 秒。"
                   f"shots 里**不要写 action**,只给 i(第几条)/ title / seconds / 实体。"
                   f"seconds 参考:整件飞进来 / 平移进来这类换装镜给 2.5 秒左右,"
                   f"单一小动作 1.5~2 秒,带完整过程的最多 4 秒,总和接近 {total_seconds:g} 秒。")
        else:
            system = _system_prompt()
            ask = (f"剧本:\n{script}\n{garment_line}\n"
                   f"请拆分出 {shot_count} 个镜头,总时长约 {total_seconds} 秒。")
        detail = "模型返回的内容无法解析为分镜 JSON"
        # 模型偶尔抽风(超时 / JSON 被截断)→ 以前一次失败就直接退到规则兜底:实体清单全空
        # (于是参考图绑不上、人物图"消失")、名字退成正文碎句。重试一次能救回绝大多数偶发失败。
        for attempt in (1, 2):
            try:
                data = llm.chat_json(system, ask, timeout=_ANALYZE_TIMEOUT)
                plan = _normalize(data, count, content_lines=lines or None)
                if plan:
                    return plan, "llm", settings.llm_model
                detail = "模型返回的内容无法解析为分镜 JSON"
            except Exception as exc:  # noqa: BLE001
                detail = f"调用模型失败:{str(exc)[:120]}"
            if attempt == 1:
                print(f"[script_split] 排分镜失败({detail}),重试一次", flush=True)
    else:
        detail = "大模型不可用(检查 LLM 配置)"

    print(f"[script_split] 退到规则兜底(engine=rule):{detail}", flush=True)
    return _rule_plan(script, count), "rule", detail


# ------------------------------------------------------------ 提示词优化
OPTIMIZE_SYSTEM_ZH = (
    "你是短视频视频生成提示词优化师。用户会给你一段已经写好的提示词,"
    "以及一条可选的修改要求,请在不破坏结构的前提下让它更精准、更可控。硬性要求:\n"
    "0. 如果原文是自然短句、没有小节结构,就保持短句风格,不要新增任何小节名或分析段落。\n"
    "1. 如果原文有结构化小节,必须保留全部小节名与顺序:subject_definitions / summary / "
    "retention_analysis / detailed_description / overall_soundscape / non_diegetic_music,不增不删。\n"
    "2. 必须原样保留所有 <Subject i> / <Prop i> / <Scene i> 定义的编号与名称。\n"
    "3. 必须原样保留所有 <Picture N> 引用,编号一个都不能改,不能新增或删除参考图。\n"
    "4. 只优化正文表达:动作更具体、景别与光线更明确、避免慢动作/文字/水印这类冲突描述;"
    "不要加入原文未出现的新人物、新道具、新剧情。\n"
    "5. 篇幅与原文相当,不要写成教程或解释,直接给可用的提示词。\n"
    '6. 只输出 JSON:{"prompt":"优化后的完整提示词"}'
)

OPTIMIZE_SYSTEM_EN = (
    "You polish prompts for a video generation model. The user gives you an existing prompt and an "
    "optional change request. Hard rules:\n"
    "0. If the original is already a short natural-language paragraph with no sections, keep that "
    "style - do not add section names or analysis blocks.\n"
    "1. If the original uses sections, keep every section name and order: subject_definitions / "
    "summary / retention_analysis / detailed_description / overall_soundscape / non_diegetic_music.\n"
    "2. Keep every <Subject i> / <Prop i> / <Scene i> definition and its numbering and name unchanged.\n"
    "3. Keep every <Picture N> reference exactly as-is - never renumber, add or remove them.\n"
    "4. Only improve the prose: more concrete action, clearer shot size and lighting; no slow-motion, "
    "no text/watermark. Do not invent new characters, props or plot.\n"
    "5. Output only JSON: {\"prompt\":\"the full improved prompt\"}"
)

_SECTIONS = ("subject_definitions:", "summary:", "retention_analysis:", "detailed_description:")


def validate_optimized(original: str, new: str) -> str | None:
    """优化结果必须仍然可出片:结构不能缺、参考图编号不能变。通过返回 None。"""
    for section in _SECTIONS:
        if section in original and section not in new:
            return f"缺少小节 {section}"
    old_pics = sorted(set(re.findall(r"<Picture (\d+)>", original)))
    new_pics = sorted(set(re.findall(r"<Picture (\d+)>", new)))
    if old_pics != new_pics:
        return f"参考图引用被改动(原来 {old_pics or ['无']} → 现在 {new_pics or ['无']})"
    return None


def optimize_prompt(prompt: str, instruction: str = "") -> tuple[str | None, str, str]:
    """让模型优化一段提示词,返回 (新提示词 或 None, engine, detail)。"""
    if not llm.available():
        return None, "rule", "大模型不可用(检查 LLM 配置)"

    system = OPTIMIZE_SYSTEM_ZH if _lang().startswith("zh") else OPTIMIZE_SYSTEM_EN
    ask = instruction.strip()
    user = f"现有提示词:\n{prompt}\n\n"
    user += f"修改要求:{ask}" if ask else "修改要求:没有特别要求,请整体润色,让画面更可控。"

    try:
        data = llm.chat_json(system, user, temperature=0.4)
    except Exception as exc:  # noqa: BLE001
        return None, "llm", f"调用模型失败:{str(exc)[:160]}"

    text = str((data or {}).get("prompt") or "").strip()
    if not text:
        return None, "llm", "模型没有返回可用的提示词"
    return text, "llm", settings.llm_model
