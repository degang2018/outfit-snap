"""把冗长的结构化分镜提示词压成自然短句(参考即梦那种写法)。

背景:旧提示词是 subject_definitions / summary / retention_analysis /
detailed_description / overall_soundscape / non_diegetic_music 一大套,模型能看懂,
但用户读起来全是重复的约束和黑名单。这里提供两种压缩:

* rule_condense(): 纯规则,快、可离线,用于"新建项目时默认精简";
* clean_prompt(): 更激进的一次性数据清洗(历史数据迁移用);
* condense_with_llm(): 调模型改写成 2-5 句自然语言,用于单镜「精简」按钮。

两条路径都**保留所有尖括号引用**(<Picture N> / <Subject N> / <Garment X> /
<Prop N> / <Scene N>)—— 参考图绑定靠它们,丢了图就绑错了。
"""
from __future__ import annotations

import re

from . import llm

_SECTIONS = (
    "subject_definitions:",
    "summary:",
    "retention_analysis:",
    "detailed_description:",
    "overall_soundscape:",
    "non_diegetic_music:",
)

_TOKEN_RE = re.compile(r"<(?:Picture|Subject|Garment|Prop|Scene)\s+[^>\n]+>")
_CN_RE = re.compile(r"[\u4e00-\u9fff]")

_ZH_TAIL = "机位固定，不推拉摇移、不变焦。全程无音乐，动作自然流畅。"
_EN_TAIL = ("Camera fixed, no movement or zoom. No music; natural movement.")

_LLM_SYSTEM = (
    "你是分镜提示词编辑。把用户给的冗长分镜提示词改写成 2-5 句自然口语化的分镜描述，"
    "像导演在现场讲戏，不要小节名、不要分点、不要解释、不要重复约束。\n"
    "必须保留：\n"
    "1) 所有尖括号标记（<Picture N>、<Subject N>、<Garment X>、<Prop N>、<Scene N>）原样保留，"
    "一个都不能少；\n"
    "2) 人物、服装、场景和这一镜的关键动作；\n"
    "3) 一句「机位固定，不推拉摇移、不变焦」；\n"
    "4) 结尾一句「全程无音乐，动作自然流畅」。\n"
    "删除 subject_definitions / summary / retention_analysis / detailed_description / "
    "overall_soundscape / non_diegetic_music 等小节名，以及重复的负面清单、分析、镜号和元指令。\n"
    "原文是中文就输出中文，原文是英文就输出英文。只输出改写后的正文。"
)


def _lang(text: str) -> str:
    return "zh" if len(_CN_RE.findall(text or "")) >= 12 else "en"


def _section_body(text: str, name: str) -> str:
    """取某个小节到下一个已知小节之间的正文。"""
    i = text.find(name)
    if i < 0:
        return ""
    start = i + len(name)
    end = len(text)
    for other in _SECTIONS:
        j = text.find(other, start)
        if 0 <= j < end:
            end = j
    return text[start:end].strip()


def _first_sentence(text: str, limit: int = 170) -> str:
    text = re.sub(r"\s+", " ", text or "").strip()
    if not text:
        return ""
    m = re.search(r"[。！？!?]|\.\s", text)
    if m:
        text = text[:m.end()]
    if len(text) > limit:
        text = text[:limit].rstrip(" ,，;；。.!?") + "…"
    return text


def _condense_defs(lines: list[str], lang: str) -> list[str]:
    out: list[str] = []
    for ln in lines:
        m = re.match(r"<([^>]+)>\s*(.*)", ln.strip())
        if not m:
            continue
        tag = f"<{m.group(1)}>"
        body = m.group(2)
        if tag.startswith("<Subject"):
            # 人物长相交给参考图,不再重复几十字的外貌描述
            first = ("是参考图中的同一位人物，外貌、发型、身材与参考图完全一致。"
                     if lang == "zh" else
                     "is the same person as the reference; face, hair and build match exactly.")
        elif tag.startswith("<Garment"):
            first = re.sub(r"\s+", " ", body or "").strip()
            suffix = ("颜色、材质和版型以参考图为准。"
                      if lang == "zh" else " Colour, fabric and fit follow the reference.")
            if suffix and suffix.rstrip("。. ") not in first:
                first = first + suffix
        else:
            # **逐件定义不截断**(2026-10-19 用户:「为什么不全」):以前 Prop 只留 80 字,
            # 包头拖鞋那条正好切在「参考图 <Pict.」——图号都被切成半个,模型也绑不上图。
            first = re.sub(r"\s+", " ", body or "").strip()
        if first:
            out.append(f"{tag} {first}")
    return out


def _split_sentences(text: str) -> list[str]:
    parts = re.split(r"(?<=[。！？!?])\s+|(?<=\.)\s+", text or "")
    return [p.strip() for p in parts if p.strip()]


# 换装镜里「衣服怎么飞进来」的句子必须留下(否则成片变成硬切,没有扔/飞入的动作)。
_CHANGE_KEEP = ("扔", "弧线", "飞向", "飞过来", "飘", "碰到", "贴上", "穿好",
                "挎到肩上", "落到手里", "递过去")

# 站姿 / 手臂姿势的句子必须留下,而且**提到最前面** —— 它们原本排在动作描述末尾,
# 会被「只留前 3 句」和 900 字上限裁掉(实测状态图里站姿句整段丢失)。
_POSE_KEEP = ("Static shot", "feet are perfectly parallel", "motionless",
              "relaxed and alive", "natural breathing", "natural blink",
              "palms facing inward", "fingers close together", "lifts both hands",
              "ease a little further open", "ease slightly outward", "上一镜的最后一帧",
              "stay open like that", "open a little", "open slightly outward",
              "held slightly away", "10 cm away", "11 cm away",
              "两条分开的裤腿", "一眼看出是", "长度必须和参考图",
              "visible gap", "直立站姿", "重心平均落在两脚之间", "下巴与地面平行",
              "头部保持端正", "两只眼睛都在画面里", "视线方向",
              "保持微张", "离胯约10厘米", "明显缝隙", "向外张开", "掌心朝向身体")


def _condense_action(text: str) -> str:
    text = re.sub(r"\*\*", "", text or "")
    text = re.sub(r"\s+", " ", text).strip()
    if not text:
        return ""
    parts = _split_sentences(text)
    if len(parts) <= 3:
        out = " ".join(parts)
    else:
        keep = parts[:3]
        # 换装动作排在后面,不能被"只留前 3 句"裁掉 —— 把带这些关键词的句子一并留下
        for p in parts[3:]:
            if p not in keep and any(k in p for k in _CHANGE_KEEP):
                keep.append(p)
        tail = next((p for p in reversed(parts[-3:])
                     if any(k in p for k in ("穿好", "dressed", "final", "最后", "结束"))), "")
        if tail and tail not in keep:
            keep.append(tail)
        # 站姿 / 手臂姿势提到最前面:这些句子在原文末尾,不提前就会被长度上限裁掉
        pose = [p for p in parts if any(k in p for k in _POSE_KEEP)]
        keep = list(dict.fromkeys(pose + keep))
        out = " ".join(keep)
    # **不设长度上限**(2026-10-19 用户要求):上限会把排在后面的站姿 / 手臂句**截成半句**
    # (实测「吊带背心·穿好展示」只剩 "...both arms ease slightly o。",模型看不懂、手就收回去)。
    # 精简只负责去小节名 / 去重复约束,正文该多长就多长。
    return out


def _missing_tokens(original: str, new: str) -> list[str]:
    """原文里有、但改写后丢了的图片引用。

    只检查 <Picture N> —— <Subject>/<Garment>/<Prop>/<Scene> 是实体定义,
    自然短句里本来就会被去掉,不算"丢"。
    """
    pic_re = r"<Picture\s+\d+>"
    have = set(re.findall(pic_re, new or ""))
    return [t for t in dict.fromkeys(re.findall(pic_re, original or "")) if t not in have]


def _dedup_pictures(text: str) -> str:
    seen: set[str] = set()

    def repl(m):
        t = m.group(0)
        if t in seen:
            return ""
        seen.add(t)
        return t

    return re.sub(r"<Picture\s+\d+>", repl, text or "")


_DEF_RE = re.compile(r"^<(Subject|Prop|Garment|Scene)\s+([^>]+)>\s*(.*)")


def strip_defs_to_natural(prompt: str) -> str:
    """把 <Subject>/<Garment>/<Prop>/<Scene> 定义行去掉,正文里换成自然称呼。

    即梦那种写法是「参考图 + 一句人话」,不是一长串实体定义。参考图绑定靠
    <Picture N>,所以这里只删定义、保留图片引用。
    """
    prompt = (prompt or "").strip()
    if not prompt:
        return prompt
    zh = _lang(prompt) == "zh"
    mapping: dict[str, str] = {}
    keep: list[str] = []
    defs_keep: list[str] = []
    for ln in prompt.splitlines():
        s = ln.strip()
        m = _DEF_RE.match(s)
        if not m:
            keep.append(ln)
            continue
        kind, _num, body = m.group(1), m.group(2), m.group(3)
        tag = f"<{kind} {_num}>"
        # 逐件定义块保留下来,放在正文最前面(用户要能一眼看到每件道具是什么)
        defs_keep.append(s)
        if kind == "Subject":
            mapping[tag] = "她" if zh else "she"
        elif kind == "Garment":
            mapping[tag] = "这套服装" if zh else "this outfit"
        elif kind == "Prop":
            name_m = re.search(r"「([^」]+)」", body)
            mapping[tag] = (name_m.group(1) if name_m
                            else ("这件道具" if zh else "this prop"))
        else:
            mapping[tag] = "场景" if zh else "the scene"
    text = "\n".join(keep)
    for tag, word in mapping.items():
        text = text.replace(tag, word)
    if defs_keep:
        header = "道具定义：" if zh else "Prop definitions:"
        text = header + "\n" + "\n".join(defs_keep) + "\n\n" + text
    # 定义行里的 <Picture N> 若正文没出现,补到末尾,保证参考图不丢
    pics = list(dict.fromkeys(_TOKEN_RE.findall(prompt)))
    for p in pics:
        if p.startswith("<Picture") and p not in text:
            text += " " + p
    text = re.sub(r"[ \t]+", " ", text).strip()
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = text.replace("…", "。").replace("。。", "。")
    return _dedup_pictures(text)


def rule_condense(prompt: str) -> str:
    """规则版精简:去掉小节骨架/分析/黑名单,只留定义 + 动作 + 一句尾部约束。"""
    prompt = (prompt or "").strip()
    if not prompt:
        return prompt
    # 已经是自然短句(没有小节名)就只做「去实体定义」,别把用户手写的提示词改坏
    if not any(s in prompt for s in _SECTIONS):
        return strip_defs_to_natural(prompt)

    lang = _lang(prompt)
    defs_body = _section_body(prompt, "subject_definitions:")
    defs_lines = [ln for ln in defs_body.splitlines() if ln.strip().startswith("<")]
    defs = _condense_defs(defs_lines, lang)

    # 第一个小节之前的正文(纯动作文本 + 声音段这种写法,动作就在开头)
    first_sec = min((prompt.find(s) for s in _SECTIONS if prompt.find(s) >= 0), default=-1)
    head = prompt[:first_sec].strip() if first_sec > 0 else ""

    action = _section_body(prompt, "detailed_description:") or _section_body(prompt, "summary:") or head
    action = re.sub(r"^\[(?:第\s*\d+\s*个镜头|shot\s*\d+\s*of\s*\d+)[^\]]*\]\s*",
                    "", action, flags=re.I)
    # 动作正文里可能还粘着后续小节的标题;在第一个已知小节处截断
    for name in _SECTIONS:
        i = action.find(name)
        if i >= 0:
            action = action[:i]
    action = _condense_action(action)

    parts = list(defs)
    if action:
        parts.append(action)
    parts.append(_ZH_TAIL if lang == "zh" else _EN_TAIL)
    out = "\n".join(p for p in parts if p)

    # 原提示词里被删掉的小节若带着图片引用,补在末尾,保证参考图绑定不丢
    missing = _missing_tokens(prompt, out)
    if missing:
        out += " " + " ".join(missing)
    return strip_defs_to_natural(out.strip())


def _clean_llm_output(text: str) -> str:
    out = (text or "").strip()
    out = re.sub(r"^```(?:\w+)?\s*", "", out)
    out = re.sub(r"\s*```$", "", out)
    for prefix in ("改写：", "精简：", "改写:", "精简:", "Rewrite:", "Condensed:"):
        if out.startswith(prefix):
            out = out[len(prefix):].strip()
    return out


def _valid(original: str, new: str) -> bool:
    if not new:
        return False
    if any(s in new for s in _SECTIONS):
        return False
    if len(new) > 1800:
        return False
    if _missing_tokens(original, new):
        return False
    return True


# ---------------------------------------------------------------- 数据清洗(更激进)
# 只用于一次性清洗历史数据:把动作里反复的机位/构图/负面黑名单也删掉,
# 只留「谁、穿什么、这一镜发生什么」,再补一句极短的机位/声音约束。
_ZH_ACTION_WORDS = (
    "飞", "抛", "落", "穿", "换", "递", "拿", "举", "走", "转", "坐", "站",
    "抬手", "伸手", "补妆", "展示", "微笑", "自拍", "整理", "打开", "合上",
    "拍", "抱", "放", "接", "说", "看", "蹲", "起", "停", "摸", "撩", "拉",
)
_ZH_FRAME_WORDS = (
    "机位", "镜头轴", "三脚架", "从头到脚", "背景里", "头顶", "脚和鞋子",
    "两只脚", "从第 1 帧", "目光直视", "站姿", "原样不动", "画面上沿",
    "画面里", "构图", "景别", "绝对不许", "绝不能", "必须", "不要", "不许",
    "参考图", "外观", "材质", "版型",
)
_EN_ACTION_WORDS = (
    "fly", "fall", "land", "wear", "change", "throw", "catch", "hand", "hold",
    "lift", "put", "walk", "turn", "sit", "stand", "raise", "reach", "smile",
    "look", "say", "apply", "show", "smooth", "touch", "open", "close", "pull",
    "push", "take", "drink", "pick", "kneel", "pose",
)
_EN_FRAME_WORDS = (
    "camera", "tripod", "locked off", "framing", "top of the head", "feet",
    "never", "must not", "no camera", "from the first frame", "perfectly still",
    "background", "composition", "shot size", "reference",
)
_ZH_STRONG_ACTION = ("飞", "抛", "落", "换", "递", "补妆", "展示", "穿上", "脱下", "自拍", "微笑")
_EN_STRONG_ACTION = ("fly", "fall", "land", "change", "throw", "catch", "hand", "show",
                     "pose", "smile", "apply", "walk", "turn", "pick")


def _short_def_line(line: str, lang: str) -> str:
    m = re.match(r"<([^>]+)>\s*(.*)", line.strip())
    if not m:
        return ""
    tag = f"<{m.group(1)}>"
    body = m.group(2).strip()
    pics = " ".join(dict.fromkeys(_TOKEN_RE.findall(body)))
    if tag.startswith("<Subject"):
        return (f"{tag} 是参考图中的同一位人物，外貌、发型和身材与参考图一致。"
                if lang == "zh" else
                f"{tag} is the same person as the reference; face, hair and build match.")
    if tag.startswith("<Scene"):
        text = _first_sentence(body, 50)
        return f"{tag} {text}" if text else ""
    # 服装 / 道具:只留第一小句(到分号/逗号前),去掉「参考图 xx,外观/颜色/版型…」这类尾注
    body = re.split(r"[;；]|参考图|，外观|, appearance", body, maxsplit=1)[0].strip()
    if len(body) > 70:
        body = body[:70].rstrip(" ,，;；。") + "…"
    if not body:
        body = "以参考图为准"
    return f"{tag} {body}" + (f" {pics}" if pics else "")


def _aggressive_action(text: str, lang: str) -> str:
    text = re.sub(r"\*\*", "", text or "")
    text = re.sub(r"\s+", " ", text).strip()
    if not text:
        return ""
    parts = re.split(r"[。！？!?；;]|(?<=\.)\s+", text)
    parts = [p.strip(" ;；") for p in parts if p.strip(" ;；")]
    if not parts:
        return ""
    action_words = _ZH_ACTION_WORDS if lang == "zh" else _EN_ACTION_WORDS
    frame_words = _ZH_FRAME_WORDS if lang == "zh" else _EN_FRAME_WORDS
    strong_words = _ZH_STRONG_ACTION if lang == "zh" else _EN_STRONG_ACTION

    def boilerplate(clause: str) -> bool:
        low = clause.lower()
        has_action = any(w in low for w in action_words)
        has_frame = any(w in low for w in frame_words)
        if len(clause) < 36 and not has_frame:
            return False
        if has_frame and not any(w in low for w in strong_words):
            return True
        return False

    keep = [parts[0]]
    for clause in parts[1:]:
        if len(keep) >= 4:
            break
        if boilerplate(clause):
            continue
        keep.append(clause)
    sep = "。" if lang == "zh" else ". "
    out = sep.join(p.rstrip("。. ") for p in keep) + ("" if lang == "en" else "。")
    if lang == "en" and not out.endswith(". "):
        out += ". "
    if len(out) > 400:
        if lang == "en":
            cut = out[:400]
            cut = cut[:cut.rfind(" ")] if " " in cut else cut
            out = cut.rstrip(" ,;.") + ". "
        else:
            out = out[:400].rstrip(" ,，;；。") + "。"
    return out.strip()


def clean_prompt(prompt: str) -> str:
    """一次性数据清洗:在 rule_condense 基础上再删掉动作里的机位/构图/黑名单长句。

    只处理带实体定义的结构化提示词;用户手写的纯动作短句原样保留。
    """
    prompt = (prompt or "").strip()
    if not prompt:
        return prompt
    has_struct = any(t in prompt for t in (
        "<Subject", "<Prop", "<Garment", "<Scene",
        "subject_definitions:", "detailed_description:",
    ))
    if not has_struct:
        return prompt
    lang = _lang(prompt)
    base = rule_condense(prompt)
    lines = base.splitlines()
    defs: list[str] = []
    action_parts: list[str] = []
    for ln in lines:
        s = ln.strip()
        if not s:
            continue
        if s.startswith("<") and ">" in s:
            short = _short_def_line(s, lang)
            if short:
                defs.append(short)
        elif s.startswith("参考：") or s.startswith("References:"):
            continue
        else:
            action_parts.append(s)
    action = _aggressive_action(" ".join(action_parts), lang)
    need_cam = ("机位" not in action) and ("Camera fixed" not in action)
    need_music = ("全程无音乐" not in action) and ("No music" not in action)
    tail_parts = []
    if need_cam:
        tail_parts.append("机位固定，不推拉摇移、不变焦。" if lang == "zh"
                          else "Camera fixed, no movement or zoom.")
    if need_music:
        tail_parts.append("全程无音乐，动作自然流畅。" if lang == "zh"
                          else "No music; natural movement.")
    tail = " ".join(tail_parts)
    parts = list(defs)
    if action:
        parts.append(action)
    if tail:
        parts.append(tail)
    out = "\n".join(p for p in parts if p).strip()
    missing = _missing_tokens(prompt, out)
    if missing:
        out += " " + " ".join(missing)
    out = out.replace("…", "。").replace("。。", "。")
    return _dedup_pictures(out)


def condense_with_llm(prompt: str) -> str | None:
    """让模型改写成自然短句;失败/漏引用标记时返回 None。"""
    try:
        out = llm.chat_text(_LLM_SYSTEM, prompt, temperature=0.2)
    except Exception:  # noqa: BLE001
        return None
    out = _clean_llm_output(out or "")
    return out if _valid(prompt, out) else None


def condense(prompt: str, use_llm: bool = True) -> tuple[str, str]:
    """返回 (精简后的提示词, engine)。

    use_llm=True 时先试模型,失败自动退回规则版;批量精简用 use_llm=False 更快。
    """
    if use_llm:
        out = condense_with_llm(prompt)
        if out:
            return out, "llm"
    return rule_condense(prompt), "rule"
