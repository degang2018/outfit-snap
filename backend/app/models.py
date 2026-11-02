"""Domain models: users, templates, projects, shots, assets, render jobs."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base

# ---------- shot modes (per-shot generation strategy) ----------
MODE_REF = "ref"                 # 参考图生视频 (MiniMaxH3ReferenceToVideo, 1..N reference images)
MODE_FIRST = "first"             # 首帧生视频   (MiniMaxH3ImageToVideo + first_frame)
MODE_FIRST_LAST = "first_last"   # 首尾帧生视频 (MiniMaxH3ImageToVideo + first_frame + last_frame)
MODE_TEXT = "text"               # 纯文本生视频
MODES = (MODE_REF, MODE_FIRST, MODE_FIRST_LAST, MODE_TEXT)

# ---------- asset categories (素材库分组) ----------
CAT_CHARACTER = "character"      # 人物
CAT_PROP = "prop"                # 道具
CAT_SCENE = "scene"              # 场景
CAT_WEAR_REF = "wear_ref"        # 实拍参考图(这套衣服穿在真人身上的实拍图,只借版型/垂坠/腰头)
CATEGORIES = (CAT_CHARACTER, CAT_PROP, CAT_SCENE, CAT_WEAR_REF)

# ---------- statuses ----------
ST_QUEUED, ST_RUNNING, ST_DONE, ST_FAILED, ST_CANCELED = "queued", "running", "done", "failed", "canceled"
ST_ACTIVE = (ST_QUEUED, ST_RUNNING)
# 项目还没生成过成片(不是「排队中」——那是真的提交了渲染任务)
ST_DRAFT = "draft"


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[Optional[str]] = mapped_column(String(255), unique=True, index=True, nullable=True)
    password_hash: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    display_name: Mapped[str] = mapped_column(String(80), default="游客")
    is_guest: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    projects: Mapped[list["Project"]] = relationship(back_populates="user", cascade="all, delete-orphan")


class Asset(Base):
    """An uploaded image the user can reference from a shot prompt."""

    __tablename__ = "assets"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    filename: Mapped[str] = mapped_column(String(255))
    rel_path: Mapped[str] = mapped_column(String(512))       # relative to STORAGE_DIR
    category: Mapped[str] = mapped_column(String(20), default=CAT_PROP, index=True)
    is_system: Mapped[bool] = mapped_column(Boolean, default=False)   # built-in 素材,所有人可见只读
    mime: Mapped[str] = mapped_column(String(80), default="image/png")
    width: Mapped[int] = mapped_column(Integer, default=0)
    height: Mapped[int] = mapped_column(Integer, default=0)
    # 读图认件的缓存:这张图「是哪一件衣服」叫什么、长什么样。
    # 第一次用到时由视觉模型认出来写在这里,以后每次生成直接用 —— 不再重复调模型。
    # 用户可以在素材库里改;改过就是用户的,不会被自动覆盖(见 assets.recognize_assets)。
    ai_name: Mapped[str] = mapped_column(String(80), default="")
    ai_desc: Mapped[str] = mapped_column(String(500), default="")
    # 部位(内搭 / 上装 / 外套 / 裤 / 裙 / 整身 / 鞋 / 帽 / …):件名万一挑不出来,靠它兜底命名,
    # 也靠它判"这是外套还是上衣、是一整件还是下装"(见 piece_prompt._kind_from_hint)。
    ai_kind: Mapped[str] = mapped_column(String(20), default="")
    # 长度(短 / 常规 / 长):吊牌上不会写,但提示词要用 —— 决定这件飞进来时的大小锚。
    # 以前是拿件名里的「长」字猜,「长袖衫」的"长"是袖子长 → 把短上衣当成长大衣。
    ai_len: Mapped[str] = mapped_column(String(10), default="")
    # 「开合」:上衣的扣子 / 外套的拉链要不要合上。空 = 按全局默认(见 garment_kind.default_close),
    # 值域 closed(扣上·拉满) / half(半开) / open(敞开)。用户可以在资源管理里改;
    # 项目里还能在「一键换装」件列表上临时覆盖(见 WholeLookItem.close)。
    ai_close: Mapped[str] = mapped_column(String(10), default="")
    # 这张图里**除第一件以外**还有哪几件(JSON 列表:[{name, desc, kind, len}])。
    # 实拍拼图那种"一张图里好几件"就靠它:一张图能认出一串件名,页面按它自动拆成多条。
    ai_more: Mapped[str] = mapped_column(String(2000), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Template(Base):
    __tablename__ = "templates"

    id: Mapped[int] = mapped_column(primary_key=True)
    # NULL / is_system=True -> built-in 系统模板;user_id set -> 用户自己保存的模板
    user_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=True, index=True
    )
    is_system: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    key: Mapped[str] = mapped_column(String(80), unique=True, index=True)
    title: Mapped[str] = mapped_column(String(160))
    description: Mapped[str] = mapped_column(Text, default="")
    cover_url: Mapped[str] = mapped_column(String(512), default="")
    out_width: Mapped[int] = mapped_column(Integer, default=1080)
    out_height: Mapped[int] = mapped_column(Integer, default=1920)
    fps: Mapped[int] = mapped_column(Integer, default=24)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    shots: Mapped[list["TemplateShot"]] = relationship(
        back_populates="template", cascade="all, delete-orphan", order_by="TemplateShot.idx"
    )


class TemplateShot(Base):
    __tablename__ = "template_shots"

    id: Mapped[int] = mapped_column(primary_key=True)
    template_id: Mapped[int] = mapped_column(ForeignKey("templates.id", ondelete="CASCADE"), index=True)
    idx: Mapped[int] = mapped_column(Integer)
    key: Mapped[str] = mapped_column(String(20))
    title: Mapped[str] = mapped_column(String(160), default="")
    mode: Mapped[str] = mapped_column(String(20), default=MODE_REF)
    duration_frames: Mapped[int] = mapped_column(Integer, default=48)
    prompt: Mapped[str] = mapped_column(Text, default="")
    refs: Mapped[list] = mapped_column(JSON, default=list)      # ["asset:12", "file:H:/.../a.png"]
    # free-form per-shot extras, e.g. {"source_frames": 120} to retime a shot faster than real time
    options: Mapped[dict] = mapped_column(JSON, default=dict)

    template: Mapped["Template"] = relationship(back_populates="shots")


class Playbook(Base):
    """一套可复用的「玩法」:拍摄结构 + 参数(打底穿搭 / 自定义动作 / 场景 / 镜头数与时长)。

    和 Template 的区别:模板存的是写死的镜头和提示词;玩法只存「规则 + 参数」,
    每次用的时候再按这件衣服现场让 LLM 写出剧本 —— 所以换任何衣服都能套。
    """

    __tablename__ = "playbooks"

    id: Mapped[int] = mapped_column(primary_key=True)
    # NULL / is_system=True -> 内置玩法;owner_id 有值 -> 用户自己保存的玩法
    owner_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=True, index=True
    )
    is_system: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    key: Mapped[str] = mapped_column(String(80), unique=True, index=True)
    title: Mapped[str] = mapped_column(String(160))
    description: Mapped[str] = mapped_column(Text, default="")
    # 拍摄结构:float_dress(托举式悬浮变装) / float_dress_side(侧面飞入式)
    structure: Mapped[str] = mapped_column(String(40), default="float_dress")
    params: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class ShotIdea(Base):
    """用户对内置「细节镜头库」的私有改动,以及自己新增的镜头。

    source_key 为空 -> 用户自己加的镜头(「我的」那一组);
    source_key 有值 -> 针对某个内置镜头的改动:改文案,或 hidden=True 表示把它删掉。

    内置原版永远不动,所以每个用户都能随时「恢复默认」,也不会影响别人。
    """

    __tablename__ = "shot_ideas"
    __table_args__ = (UniqueConstraint("owner_id", "source_key", name="uq_shot_idea_owner_source"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    owner_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    source_key: Mapped[Optional[str]] = mapped_column(String(64), nullable=True, index=True)
    label: Mapped[str] = mapped_column(String(60), default="")
    action: Mapped[str] = mapped_column(Text, default="")
    # 这个镜头建进项目时的默认时长(秒)。auto 页 / 添加通用分镜都读它;
    # 空值(老数据)按 3 秒处理。
    seconds: Mapped[float] = mapped_column(Float, default=3.0)
    hidden: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Project(Base):
    __tablename__ = "projects"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    template_id: Mapped[Optional[int]] = mapped_column(ForeignKey("templates.id", ondelete="SET NULL"), nullable=True)
    title: Mapped[str] = mapped_column(String(160), default="未命名项目")
    status: Mapped[str] = mapped_column(String(20), default=ST_QUEUED)
    out_width: Mapped[int] = mapped_column(Integer, default=1080)
    out_height: Mapped[int] = mapped_column(Integer, default=1920)
    fps: Mapped[int] = mapped_column(Integer, default=24)
    final_rel_path: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    # 人物背景图(整片统一使用):所有分镜都拿它当场景参考 —— 人物站在这个背景里摆动作。
    # bg_ref 存素材引用("asset:123"),bg_desc 是读图得到的场景描述。
    bg_ref: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    bg_desc: Mapped[Optional[str]] = mapped_column(String(1000), nullable=True)
    # 人物头像(整片统一):所有分镜都挂这张图当「头部参考」—— 脸型 / 五官 / 发型 / 发色以它为准。
    head_ref: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    # 分镜画布(节点视图)的界面状态:各节点坐标、缩放、平移、连线开关等。
    # 纯前端 UI 状态,后端只负责原样存取,不参与渲染。
    canvas_state: Mapped[dict] = mapped_column(JSON, default=dict)
    # 换装风格:throw(扔过来飞入·视频) / pull(从身上拉出来·视频) / static(普通·静态图,
    # 每件调 FLUX3 imgedit 出一张 PNG,最后一镜作为 project.final_rel_path)
    piece_style: Mapped[str] = mapped_column(String(20), default="throw")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    user: Mapped["User"] = relationship(back_populates="projects")
    shots: Mapped[list["ProjectShot"]] = relationship(
        back_populates="project", cascade="all, delete-orphan", order_by="ProjectShot.idx"
    )
    jobs: Mapped[list["RenderJob"]] = relationship(
        back_populates="project", cascade="all, delete-orphan", order_by="RenderJob.id.desc()"
    )


class ProjectShot(Base):
    __tablename__ = "project_shots"

    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    idx: Mapped[int] = mapped_column(Integer)
    key: Mapped[str] = mapped_column(String(20))
    title: Mapped[str] = mapped_column(String(160), default="")
    mode: Mapped[str] = mapped_column(String(20), default=MODE_REF)
    duration_frames: Mapped[int] = mapped_column(Integer, default=48)
    prompt: Mapped[str] = mapped_column(Text, default="")
    refs: Mapped[list] = mapped_column(JSON, default=list)
    options: Mapped[dict] = mapped_column(JSON, default=dict)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    status: Mapped[str] = mapped_column(String(20), default=ST_QUEUED)
    clip_rel_path: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    # 抽卡候选片段(storage 下的相对路径列表)。每次「抽卡」抽出的都留在这里,
    # 用户挑中哪张就把 clip_rel_path 指过去(选中的那张也一定在列表里)。
    candidates: Mapped[list] = mapped_column(JSON, default=list)

    project: Mapped["Project"] = relationship(back_populates="shots")


class RenderJob(Base):
    __tablename__ = "render_jobs"

    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    # None -> render the whole film; set -> preview-render just that one shot
    shot_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("project_shots.id", ondelete="SET NULL"), nullable=True, index=True
    )
    status: Mapped[str] = mapped_column(String(20), default=ST_QUEUED, index=True)
    progress: Mapped[int] = mapped_column(Integer, default=0)
    current_shot: Mapped[str] = mapped_column(String(20), default="")
    # 单镜任务「抽卡」一次要出几张候选(整片任务/普通单镜任务都是 1)
    variants: Mapped[int] = mapped_column(Integer, default=1)
    message: Mapped[str] = mapped_column(String(512), default="")
    error: Mapped[str] = mapped_column(Text, default="")
    out_rel_path: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    started_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    # worker 一边生成一边刷新这个时间;看门狗靠它判断任务是不是卡死了(见 worker.py)
    heartbeat_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    project: Mapped["Project"] = relationship(back_populates="jobs")
