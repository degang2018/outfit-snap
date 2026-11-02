"""Pydantic request/response schemas."""
from __future__ import annotations

import re
from datetime import datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .models import CATEGORIES, MODE_FIRST, MODE_FIRST_LAST, MODE_REF, MODE_TEXT

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[A-Za-z]{2,}$")
ShotMode = str


def _check_mode(v: str) -> str:
    if v not in (MODE_REF, MODE_FIRST, MODE_FIRST_LAST, MODE_TEXT):
        raise ValueError(f"mode must be one of {MODE_REF}/{MODE_FIRST}/{MODE_FIRST_LAST}/{MODE_TEXT}")
    return v


# ---------------- auth ----------------
class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    email: Optional[str] = None
    display_name: str
    is_guest: bool


class TokenOut(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user: UserOut


class BindEmailIn(BaseModel):
    email: str
    password: str = Field(min_length=6, max_length=128)
    display_name: Optional[str] = Field(default=None, max_length=80)

    @field_validator("email")
    @classmethod
    def _email(cls, v: str) -> str:
        v = v.strip().lower()
        if not EMAIL_RE.match(v):
            raise ValueError("邮箱格式不正确")
        return v


class LoginIn(BaseModel):
    email: str
    password: str


# ---------------- assets ----------------
class AssetOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    filename: str
    url: str
    category: str
    width: int
    height: int
    is_system: bool = False
    # 读图认件的缓存(件名 + 一句外观描述 + 部位 + 长短):前端直接显示,不用再调模型
    ai_name: str = ""
    ai_desc: str = ""
    ai_kind: str = ""
    ai_len: str = ""
    # 上衣的扣子 / 外套的拉链要不要合上:"" = 自动(按全局默认) / closed / half / open
    ai_close: str = ""
    # 这张图里除第一件以外还有哪几件:[{name, desc, kind, len}] —— 页面按它自动拆成多条
    ai_more: list[dict] = []


class AssetPatchIn(BaseModel):
    """素材管理里的「保存」:改分类 / 改名 / 改件名与描述。"""

    filename: Optional[str] = Field(default=None, max_length=255)
    category: Optional[str] = None
    # 读图认件认错了可以直接改;改完就固定下来(生成时优先用这里的值)
    ai_name: Optional[str] = Field(default=None, max_length=80)
    ai_desc: Optional[str] = Field(default=None, max_length=500)
    # 开合:"" = 自动(按全局默认) / closed 扣上·拉满 / half 半开 / open 敞开
    ai_close: Optional[str] = Field(default=None, max_length=10)

    @field_validator("ai_close")
    @classmethod
    def _close(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        v = str(v).strip().lower()
        if v not in ("", "auto", "closed", "half", "open"):
            raise ValueError("ai_close must be '' / auto / closed / half / open")
        return "" if v == "auto" else v

    @field_validator("category")
    @classmethod
    def _cat(cls, v: Optional[str]) -> Optional[str]:
        if v is not None and v not in CATEGORIES:
            raise ValueError(f"category must be one of {'/'.join(CATEGORIES)}")
        return v


class AssetRecognizeIn(BaseModel):
    """把选中的素材读图认件一遍(结果缓存到素材上)。force=True 时重认已缓存的。"""

    ids: list[int] = []
    force: bool = False


# ---------------- templates ----------------
class TemplateShotOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    idx: int
    key: str
    title: str
    mode: str
    duration_frames: int
    prompt: str
    refs: list[str] = []
    options: dict = {}


class TemplateOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    key: str
    title: str
    description: str
    cover_url: str
    out_width: int
    out_height: int
    fps: int
    is_system: bool = False
    owner_id: Optional[int] = None
    shots: list[TemplateShotOut] = []


class TemplateFromProjectIn(BaseModel):
    """把一个项目另存为自己的模板。"""

    title: str = Field(min_length=1, max_length=160)
    description: str = Field(default="", max_length=2000)


class ScriptEntity(BaseModel):
    """剧本里识别出的一个人物 / 道具 / 场景。

    extra="allow":道具实体可以带内部标记 —— `_base`(她一开始穿着、本镜头会被换掉的旧件)、
    `_worn`(本镜头不动的已穿件)、`_slot`(读图给的「部位」)。建项目/拼提示词时靠它们
    区分「旧件 / 新件 / 不动件」,并按部位配对、定进场位置。
    """

    model_config = ConfigDict(extra="allow")

    name: str = Field(min_length=1, max_length=40)
    desc: str = Field(default="", max_length=120)


class ScriptShotPlan(BaseModel):
    title: str = Field(default="", max_length=60)
    # 「按件排镜」这类换装镜的动作文本很长(机位/站姿/影子/多件说明全在一句里,实测 2500+ 字),
    # 限到 2000 会把 analyze-script 的返回卡在校验里直接 500(返回给前端时 ScriptPlanOut 校验不过)。
    # 和 ScriptAnalyzeIn.script 一样放宽到 20000。
    action: str = Field(min_length=1, max_length=20000)
    seconds: float = Field(default=0.0, ge=0.0, le=600.0)
    characters: list[str] = []
    props: list[str] = []
    scenes: list[str] = []
    # 本镜的「实拍穿着参考」(风格参考图):**不绑定任何实体**,只是额外挂在参考图列表最后的一张图,
    # 提示词里补一句"只参考它身上这套衣服的款式 / 颜色 / 穿法,不要参考那个人"。
    # 用在「单镜整套换装」:用户上传的实拍 lookbook 既想让模型看着穿法,又怕它把模特的身体抄进来。
    wear_refs: list[str] = []


class ScriptPlanOut(BaseModel):
    """剧本分析结果:实体清单 + 镜头列表(每个镜头标注出现了哪些实体)。"""

    characters: list[ScriptEntity] = []
    props: list[ScriptEntity] = []
    scenes: list[ScriptEntity] = []
    shots: list[ScriptShotPlan] = []
    engine: str = "rule"
    engine_detail: str = ""


class ScriptAnalyzeIn(BaseModel):
    script: str = Field(min_length=1, max_length=20000)
    shot_count: int = Field(default=5, ge=1, le=60)
    total_seconds: float = Field(default=20.0, ge=2.0, le=600.0)
    # True = script 是一份「内容清单」(一行一条),LLM 只排分镜、不自己编内容
    content_mode: bool = False
    # 这件衣服的客观描述:给了它,模型才能把一整套按件拆成「套装-吊带背心」这种独立道具
    garment_desc: str = Field(default="", max_length=3000)


class ScriptBuildIn(BaseModel):
    """按分析结果建项目。

    assignments 是「实体 → 素材引用列表」,一个实体可以指定多张(比如正面 + 背面)。
    留空/不填表示不指定,交给模型自己生成。
    """

    title: str = Field(min_length=1, max_length=160)
    total_seconds: float = Field(default=20.0, ge=2.0, le=600.0)
    shot_count: int = Field(default=5, ge=1, le=60)
    mode: str = MODE_REF
    plan: ScriptPlanOut
    assignments: dict[str, list[str]] = {}
    # 项目级「开合」覆盖:{参考图 ref -> closed / half / open} —— 「一键换装」件列表上改的。
    # 按 **ref** 传、不按件名:分析剧本那步可能给这件改个名,件名一对不上就白设了。
    closes: dict[str, str] = {}
    # 「打底 / 旧件」:她一开始就穿着、会在换装镜里被换掉的那几件(带图)。
    # 按件排镜时必须传 —— 建项目时会把它们**强制**加进每一镜(参考图 + <Prop> 定义,
    # 并标成「她现在就穿着、本镜头会被换掉」),否则模型手里没有你指定的那件旧衣,
    # 会自己编一件、旧件也不会"消失"。
    base_items: list[WholeLookItem] = []
    # 「常驻配饰」(墨镜 / 拖鞋…):全程戴在人物身上、不换的小件(带图)。按件排镜时必须传 ——
    # 建项目时会把它们**强制**加进每一镜(参考图 + <Prop> 定义),否则模型手上没有样本,
    # 实测会直接不画(墨镜整条片子都不出现)或者照抄人物参考图里的那双鞋。
    accessories: list[WholeLookItem] = []
    # 第 4 栏「风格参考图」(实拍 lookbook):挂在每一镜参考图最后,当「实拍穿着参考」用 ——
    # 只参考它身上这套衣服的款式 / 颜色 / 材质 / 垂坠感和穿法,不参考那个人。
    # 「按件排镜」原来**根本没传这个栏位**,所以用户在页面上传了也等于没传。
    style_items: list[WholeLookItem] = []
    # 悬浮变装:人物参考图只定脸,衣服由文字描述决定;成片不做推镜/加饱和
    float_mode: bool = False
    # overhead = 手托举到头顶悬浮;side = 衣服从画面左右两侧飞入自动穿上
    float_variant: str = Field(default="overhead", max_length=20)
    # 「按件排镜」的换装风格(2026-10-20):
    #   throw  = 画外有人把衣服朝她扔过来、碰到身体就穿好(默认);
    #   pull   = 开场一把扯掉初始衣服、只剩短上衣 + 短裤;整套新衣服无人穿着、等距悬空
    #            排成一列在她身侧,她一件件从那一列里扯下来、拉到自己身上。
    #   static = 普通·静态图(2026-10-22):主链不再生成视频,改成每件调 FLUX3 Klein
    #            imgedit 出一张 PNG,一件一镜;最后一镜的 PNG 作为项目 final_rel_path。
    #            后续追加的通用/自定义分镜仍是视频,通过 _style_ref_for 引用最后那张静态图。
    piece_style: str = Field(default="throw", max_length=20)
    # 「扯上身」风格要穿上的那几件(按页面上的顺序)。它不走"一件一镜",而是开场扯掉 + **一镜穿整套**,
    # 所以后端需要这份清单来算这一镜的穿着状态。
    piece_names: list[str] = []
    # 招牌镜里她穿的打底(留空则用内置默认:黑色细吊带 + 黑色短裤、赤脚)
    float_base_outfit: str = Field(default="", max_length=200)
    # 这件衣服的客观描述(读图得到):写进服装道具的定义里,免得模型把一套多件揉成一件
    garment_desc: str = Field(default="", max_length=3000)
    # 「人物背景图」:整片所有分镜统一使用的场景参考(人物在这个背景里摆动作)。
    # 传素材引用 "asset:123";给了它就替换掉默认的「纯白背景」。
    background_ref: str = Field(default="", max_length=512)
    background_desc: str = Field(default="", max_length=1000)
    # 「人物头像」:整片所有分镜统一使用的头部参考图 —— 脸型 / 五官 / 发型 / 发色以它为准。
    # 传素材引用 "asset:123";不给就沿用人物图里的脸。
    head_ref: str = Field(default="", max_length=512)

    @field_validator("mode")
    @classmethod
    def _mode(cls, v: str) -> str:
        return _check_mode(v)


# ---------------- project shots ----------------
class ShotOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    idx: int
    key: str
    title: str
    mode: str
    duration_frames: int
    prompt: str
    refs: list[str] = []
    options: dict = {}
    enabled: bool
    status: str
    clip_url: Optional[str] = None
    # 片段首帧缩略图(节点默认只加载它,点播放才加载视频)
    thumb_url: Optional[str] = None
    # 抽卡候选片段(每个是一段可预览的视频 url);选中的那张同时就是 clip_url。
    candidates: list[str] = []
    # 视频候选没有 thumbs/*.jpg 缩略图,对应位置是 null(前端按真假判断,不会渲染 <img>)
    candidate_thumbs: list[Optional[str]] = []
    # 预览图(SDXL 按本镜提示词先出的一张静帧,只用来快速看构图/景别)
    preview_url: Optional[str] = None


class ShotPickIn(BaseModel):
    """从抽卡候选里挑一张,设为这一镜最终用的片段(整片合成会用它)。"""

    clip: str = Field(min_length=1, max_length=512)


class ShotPatchIn(BaseModel):
    title: Optional[str] = None
    mode: Optional[str] = None
    duration_frames: Optional[int] = Field(default=None, ge=4, le=600)
    prompt: Optional[str] = None
    refs: Optional[list[str]] = None
    options: Optional[dict] = None
    enabled: Optional[bool] = None

    @field_validator("mode")
    @classmethod
    def _mode(cls, v: Optional[str]) -> Optional[str]:
        return None if v is None else _check_mode(v)


class GeneralShotIn(BaseModel):
    """添加通用分镜:镜头库勾选或自定义输入,后端复制现有通用分镜的提示词结构并替换标题/动作。"""

    title: str = Field(min_length=1, max_length=120)
    action: str = Field(min_length=1, max_length=2000)
    seconds: float = Field(default=2.0, ge=0.5, le=10.0)


class ShotOptimizeIn(BaseModel):
    """让模型优化这段提示词。instruction 可选,形如「改成近景」「光线再暖一点」。"""

    prompt: str = Field(min_length=1, max_length=20000)
    instruction: str = Field(default="", max_length=500)


class ShotOptimizeOut(BaseModel):
    prompt: str
    engine: str
    engine_detail: str


class CondensePromptIn(BaseModel):
    """把一段冗长提示词压成自然短句(只返回文本,不落库)。"""

    prompt: str = Field(min_length=1, max_length=30000)


class CondensePromptOut(BaseModel):
    prompt: str
    engine: str
    engine_detail: str


class CanvasImageIn(BaseModel):
    """画布图片节点:按一句描述生成图片(SDXL 预览模型)。"""

    prompt: str = Field(min_length=1, max_length=2000)
    aspect: str = "16:9"
    resolution: str = "2K"
    count: int = Field(default=1, ge=1, le=4)


class ShotReorderIn(BaseModel):
    """Ordered list of project-shot ids."""

    shot_ids: list[int]


# ---------------- projects ----------------
class ProjectCreateIn(BaseModel):
    template_id: int
    title: Optional[str] = Field(default=None, max_length=160)


class ProjectPatchIn(BaseModel):
    """项目级设置。只传要改的字段:标题 / 成片分辨率 / 帧率。

    改分辨率或帧率会让已生成的成片和镜头片段失效(需要重新生成)。
    """

    title: Optional[str] = Field(default=None, max_length=160)
    out_width: Optional[int] = Field(default=None, ge=128, le=4096)
    out_height: Optional[int] = Field(default=None, ge=128, le=4096)
    fps: Optional[int] = Field(default=None, ge=8, le=60)
    # 分镜画布布局(节点坐标 / 缩放 / 平移 / 显示开关),前端原样存取
    canvas_state: Optional[dict] = None

    @field_validator("out_width", "out_height")
    @classmethod
    def _even(cls, v: Optional[int]) -> Optional[int]:
        if v is not None and v % 2:
            raise ValueError("分辨率必须是偶数(yuv420p 编码要求)")
        return v


class SpeedSettingIn(BaseModel):
    """出片速度的两把旋钮(全局):生成短边 + 参考图档位。只传要改的那个。"""

    gen_short: Optional[int] = Field(default=None, ge=256, le=2048)
    ref_size: Optional[str] = Field(default=None, max_length=8)


class RenderJobOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    status: str
    progress: int
    current_shot: str
    message: str
    error: str
    shot_id: Optional[int] = None
    out_url: Optional[str] = None
    created_at: datetime
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None


class ProjectOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    title: str
    status: str
    template_id: Optional[int]
    out_width: int
    out_height: int
    fps: int
    total_frames: int
    duration_seconds: float
    final_url: Optional[str] = None
    # 人物背景图(整片统一):前端拿它显示「这个项目用的背景」
    bg_ref: Optional[str] = None
    bg_desc: Optional[str] = None
    canvas_state: dict = {}
    created_at: datetime
    updated_at: datetime
    shots: list[ShotOut] = []
    latest_job: Optional[RenderJobOut] = None


class ProjectBrief(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    title: str
    status: str
    out_width: int
    out_height: int
    fps: int
    total_frames: int
    duration_seconds: float
    final_url: Optional[str] = None
    created_at: datetime
    updated_at: datetime


class ProjectPageOut(BaseModel):
    """「我的项目」列表的一页(分页):项目按 id 倒序,一页 page_size 条 + 总数。

    项目会一直涨(实测已经 190+),一次性把全部返回给前端既浪费流量、又把列表页
    铺得老长,所以列表改成按页取。「我的项目」页据此画「上一页 / 下一页」。
    """

    items: list[ProjectBrief] = []
    total: int = 0
    page: int = 1
    page_size: int = 12


class ScriptProjectOut(BaseModel):
    """剧本建项目的返回:项目本体 + 本次切分用的引擎(便于前端提示)。"""

    project: ProjectOut
    engine: str
    engine_detail: str
    shot_count: int


# ---------------- 玩法(可复用的拍摄结构 + 参数) ----------------
class PlaybookOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    key: str
    title: str
    description: str = ""
    structure: str
    params: dict = {}
    is_system: bool = False
    owner_id: Optional[int] = None


class PlaybookIn(BaseModel):
    """新建 / 保存玩法。params 里放 base_outfit / custom_actions / scene / shot_count / total_seconds。"""

    title: str = Field(min_length=1, max_length=160)
    description: str = Field(default="", max_length=2000)
    structure: str = Field(default="float_dress", max_length=40)
    params: dict = {}


class PlaybookPatchIn(BaseModel):
    title: Optional[str] = Field(default=None, max_length=160)
    description: Optional[str] = Field(default=None, max_length=2000)
    params: Optional[dict] = None


# ---------------- 细节镜头库:内置镜头 + 用户自己的改动 ----------------
class ShotIdeaIn(BaseModel):
    """新增一个「我的」镜头。"""

    label: str = Field(min_length=1, max_length=60)
    action: str = Field(min_length=1, max_length=2000)
    # 建进项目时的默认时长(秒);auto 页和「添加通用分镜」都会读它
    seconds: float = Field(default=3.0, ge=0.5, le=10.0)


class ShotIdeaPatchIn(BaseModel):
    label: Optional[str] = Field(default=None, max_length=60)
    action: Optional[str] = Field(default=None, max_length=2000)
    seconds: Optional[float] = Field(default=None, ge=0.5, le=10.0)
    hidden: Optional[bool] = None


class ShotFromShotIn(BaseModel):
    """把某个镜头存成「通用分镜」:只传镜头 id,标题和正文由后端从那一镜里取。"""

    shot_id: int


class ShotBuiltinPatchIn(BaseModel):
    """改 / 隐藏一个内置镜头(只影响当前用户)。"""

    label: Optional[str] = Field(default=None, max_length=60)
    action: Optional[str] = Field(default=None, max_length=2000)
    hidden: Optional[bool] = None


class ShotLibraryOut(BaseModel):
    """library 用于勾选区(已剔除被删掉的),items 用于「管理细节镜头」面板(含被删掉的)。"""

    library: list[dict] = []
    items: list[dict] = []


class PreviewJobOut(BaseModel):
    """预览图任务(内存里跑,不落库)。"""

    status: str = "idle"          # idle | running | done | failed
    total: int = 0
    done: int = 0
    current: str = ""
    error: str = ""


# ---------------- 快速创建:上传服装图 -> 读图 -> 生成剧本 ----------------
class QuickOptionsOut(BaseModel):
    """给用户挑的场景 / 调性 / 道具清单(含一句建议)。"""

    scenes: list[dict]
    styles: list[dict]
    props: list[dict]
    # 细节镜头库(悬浮变装用):一键出稿里点选,只有勾中的才会进片子
    shot_library: list[dict] = []


class GarmentIn(BaseModel):
    # 单图(老调用)或多图:同一套服装的不同件,一件一张
    ref: str = Field(default="", max_length=512)        # "asset:1" 或 "file:H:/x.png"
    refs: list[str] = []                                # 多图时填这里,顺序就是"图 1、图 2…"的顺序


class GarmentPieceOut(BaseModel):
    """一套服装里的一张图 = 一件。ref 说明这张图是哪张素材,part 说明它是哪一件。"""

    ref: str
    part: str            # 件名:吊带背心 / 开衫 / 长裤 …
    desc: str = ""       # 这一件的一句话外观


class GarmentOut(BaseModel):
    """读图结果:客观描述 + 根据衣服风格给出的拍摄建议。"""

    description: str
    # 传了多张图时,这里逐张说明"这张是哪一件"(分析阶段靠它把套装拆成一件件道具)
    pieces: list[GarmentPieceOut] = []
    style: str = ""          # 推荐的调性(取自可选调性)
    scene: str = ""          # 推荐的场景 key(取自可选场景)
    props: list[str] = []    # 从道具清单里推荐的
    extra_props: list[str] = []   # 清单里没有、但更适合这件衣服的(建议写进补充要求)
    why: str = ""            # 一句话推荐理由
    engine: str
    engine_detail: str


class DraftScriptIn(BaseModel):
    garment: str = Field(min_length=1, max_length=3000)
    scene: str = Field(default="", max_length=40)        # 预设场景 key
    scene_desc: str = Field(default="", max_length=600)  # 选了「自定义」时自己写
    style: str = Field(default="", max_length=200)
    props: list[str] = []
    shot_count: int = Field(default=8, ge=1, le=60)
    total_seconds: float = Field(default=30.0, ge=2.0, le=600.0)
    extra: str = Field(default="", max_length=1000)
    # 悬浮变装:强制纯白影棚,剧本按「托举 → 悬浮 → 落下变装」的结构写
    float_mode: bool = False
    # overhead = 手托举到头顶悬浮;side = 衣服从画面左右两侧飞入自动穿上
    float_variant: str = Field(default="overhead", max_length=20)
    # 选了「玩法」时传它的 id:结构、打底穿搭、自定义动作、场景、镜头数/时长都以玩法为准
    playbook_id: Optional[int] = None
    # 打底穿搭 / 自定义动作(一行一个),可选;留空用玩法的默认
    base_outfit: str = Field(default="", max_length=200)
    custom_actions: list[str] = []
    # 从镜头库里勾选的细节镜头 key;传了它(哪怕是空列表)就不再让模型编内容,
    # 直接按「招牌镜 + 勾选的镜头 + custom_actions」拼出内容清单
    picked_shots: Optional[list[str]] = None
    # 「按件排镜」:一件件衣服名(来自读图认件),件数不限;float_variant="piece" 时用它自动排 N+1 镜
    parts: list[str] = []
    # 「按件排镜」的换装风格:throw = 每件一镜飞入(默认);pull = 开场扯掉初始衣服 + 一镜穿整套
    piece_style: str = Field(default="throw", max_length=20)
    # 和 parts 一一对应的「部位」(读图给的 slot:内搭 / 上装 / 外套 / 裤 / 裙 / 整身…)。
    # 有它就不必再拿件名里的字猜"这件穿在第几层、它会顶掉哪件旧件"。
    part_kinds: list[str] = []


class DraftScriptOut(BaseModel):
    title: str
    script: str
    engine: str
    engine_detail: str
    # 「按件排镜」时后端算出来的镜头数(= 内容清单行数)。前端别自己按件数估 ——
    # 清单里还会有从配饰栏进来的"携带件"(包 / 围裙…),估少了排分镜就会截断尾巴。
    shot_count: Optional[int] = None


# ---------------- 「单镜整套 · 衣服飘入替换」 ----------------
class WholeLookItem(BaseModel):
    """一件衣服:用哪张图 + 它叫什么(件名,通常由读图认件填)。"""

    ref: str = Field(min_length=1, max_length=512)
    name: str = Field(default="", max_length=40)
    desc: str = Field(default="", max_length=120)
    # 这一件在本项目里的「开合」覆盖:"" = 跟着素材库的设置(再不行按全局默认);
    # closed 扣上·拉满 / half 半开 / open 敞开 —— 页面上那件衣服的扣子/拉链要不要合上。
    close: str = Field(default="", max_length=10)


class WholeLookIn(BaseModel):
    """一镜里换整套:新衣服一件件从画外飘进来罩到身上,开始穿的旧件被替换掉。

    person 是人物参考图(只锁脸 / 发型);base_items 是她开始时穿的旧件(可空);
    new_items 是要飘进来的新衣服(至少一件);accessories 是全程佩戴的小件(墨镜 / 鞋…,可空)。
    """

    title: str = Field(default="", max_length=160)
    person: str = Field(min_length=1, max_length=512)
    base_items: list[WholeLookItem] = []
    new_items: list[WholeLookItem] = []
    accessories: list[WholeLookItem] = []
    # 第 4 栏「风格参考图」(素材库里分类是 wear_ref):**参与生成** —— 会作为最后一张参考图
    # 挂进这一镜,并自动配一句"只参考它身上这套衣服的款式 / 颜色 / 穿法,不要参考那个人"。
    # 空列表就是不加(以前它是完全没用上的,只有前端在存图)。
    style_items: list[WholeLookItem] = []
    # 勾选的「通用分镜」(镜头库里的 key,见 /shots):接在换装镜后面一起建进同一个工程。
    # 空列表就是只要那个换装镜。
    shot_keys: list[str] = []
    scene: str = Field(default="studio_white", max_length=40)
    seconds: float = Field(default=0.0, ge=0.0, le=60.0)
    # 「人物头像」:整片所有分镜统一使用的头部参考图(见 ScriptBuildIn.head_ref)。
    head_ref: str = Field(default="", max_length=512)


class WholeLookOut(BaseModel):
    project: ProjectOut
    job: RenderJobOut
