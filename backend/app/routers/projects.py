"""Projects = a user's editable copy of a template, plus its shots and render trigger."""

from __future__ import annotations

import json
import os
import random
import re
import shutil
import uuid
import zipfile
from datetime import datetime
from io import BytesIO
from pathlib import Path
from urllib.parse import quote

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile, status
from fastapi.responses import StreamingResponse
from sqlalchemy import func
from sqlalchemy.orm import Session

from .. import comfy_client, comfy_workflow, concise, llm, preview, quick_start
from ..config import DEFAULT_SEED, STORAGE_DIR, settings
from ..db import get_db
from ..deps import get_current_user
from ..garment_kind import CLOSE_STATES, default_close, fasten_short, front_kind, parse_slot
from ..models import (
    CAT_PROP,
    MODE_FIRST,
    MODE_FIRST_LAST,
    MODE_REF,
    MODE_TEXT,
    ST_DRAFT,
    ST_QUEUED,
    ST_RUNNING,
    Asset,
    Playbook,
    Project,
    ProjectShot,
    RenderJob,
    Template,
    User,
)
from ..schemas import (
    CanvasImageIn,
    CondensePromptIn,
    CondensePromptOut,
    DraftScriptIn,
    DraftScriptOut,
    GarmentIn,
    GarmentOut,
    GarmentPieceOut,
    GeneralShotIn,
    PreviewJobOut,
    ProjectBrief,
    ProjectCreateIn,
    ProjectOut,
    ProjectPageOut,
    ProjectPatchIn,
    QuickOptionsOut,
    RenderJobOut,
    ScriptAnalyzeIn,
    ScriptBuildIn,
    ScriptEntity,
    ScriptPlanOut,
    ScriptProjectOut,
    ScriptShotPlan,
    ShotOptimizeIn,
    ShotOptimizeOut,
    ShotOut,
    ShotPatchIn,
    ShotPickIn,
    ShotReorderIn,
    WholeLookIn,
    WholeLookItem,
    WholeLookOut,
)
from ..script_split import (
    DARK_FABRIC_TIP,
    KINDS,
    TAIL_ZH_CUSTOM,
    WORN_PROPERLY_TIP,
    analyze_script,
    build_prompt,
    build_universal_prompt,
    head_def_text,
    insert_into_person,
    is_garment_name,
    is_carried_prop,
    is_object_shot,
    is_outer_sleeved,
    is_worn_accessory,
    mentions_carry,
    wants_real_scene,
    optimize_prompt,
    validate_optimized,
)
from ..serializers import job_out, media_url, project_brief, project_out, shot_out
from .assets import recognize_assets
from .shots import user_shot_library

router = APIRouter(prefix="/projects", tags=["projects"])

# 单镜头最短 1 秒(24 帧),否则没有可看内容
MIN_SHOT_FRAMES = 24

# 「通用分镜」(勾选的细节镜)默认时长:3 秒 —— 库里只存了文案(lable / action),没存时长。
# 建完在详情页可以逐镜改。
UNIVERSAL_SHOT_SECONDS = 3.0

# 通用分镜 / 自定义分镜的取景锚:**已回滚**(2026-10-19 用户要求先撤掉,统一加对半身镜不合适)。

# 「换装镜」的判定词在 quick_start 里(和「统一取景」共用一套标记,见 CHANGE_MARKERS)——
# 这几镜要**一整段原始视频**才演得完(衣服从画外飞进来 → 贴上 → 穿好,实测到 3.3 秒才真正
# 穿好),按建议时长裁会正好切在"还没穿好"的位置,所以时长直接用满 settings.gen_length。


def _own(project_id: int, user: User, db: Session) -> Project:
    project = db.get(Project, project_id)
    if project is None or project.user_id != user.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "项目不存在")
    return project


def _own_shot(project: Project, shot_id: int) -> ProjectShot:
    for s in project.shots:
        if s.id == shot_id:
            return s
    raise HTTPException(status.HTTP_404_NOT_FOUND, "镜头不存在")


def _ensure_idle(db: Session, project: Project) -> None:
    active = (
        db.query(RenderJob)
        .filter(
            RenderJob.project_id == project.id,
            RenderJob.status.in_((ST_QUEUED, ST_RUNNING)),
        )
        .first()
    )
    if active is not None:
        raise HTTPException(
            status.HTTP_409_CONFLICT, "该项目已有渲染任务在进行中,请先等待或取消"
        )


def _ensure_no_preview() -> None:
    """预览图用的是 SDXL,它和视频用的 MiniMax H3 抢显存 —— 同时在跑必炸。"""
    if preview.any_running() is not None:
        raise HTTPException(
            status.HTTP_409_CONFLICT, "有预览图任务正在跑,等它结束再开始生成"
        )


def _unlink(rel_path: str | None) -> None:
    """删除 storage 下的产物文件(尽力而为,失败不影响接口)。"""
    if not rel_path:
        return
    try:
        (STORAGE_DIR / rel_path).unlink(missing_ok=True)
    except OSError:
        pass


@router.post("", response_model=ProjectOut, status_code=status.HTTP_201_CREATED)
def create_project(
    payload: ProjectCreateIn,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> ProjectOut:
    tpl = db.get(Template, payload.template_id)
    if tpl is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "模板不存在")

    project = Project(
        user_id=user.id,
        template_id=tpl.id,
        title=payload.title or tpl.title,
        out_width=tpl.out_width,
        out_height=tpl.out_height,
        fps=tpl.fps,
        status=ST_QUEUED,
    )
    db.add(project)
    db.flush()
    for ts in tpl.shots:
        db.add(
            ProjectShot(
                project_id=project.id,
                idx=ts.idx,
                key=ts.key,
                title=ts.title,
                mode=ts.mode,
                duration_frames=ts.duration_frames,
                prompt=concise.rule_condense(ts.prompt),
                refs=list(ts.refs or []),
                options=dict(ts.options or {}),
                enabled=True,
                status=ST_QUEUED,
            )
        )
    db.commit()
    db.refresh(project)
    return project_out(project)


@router.post("/analyze-script", response_model=ScriptPlanOut)
def analyze_script_endpoint(
    payload: ScriptAnalyzeIn,
    user: User = Depends(get_current_user),
) -> ScriptPlanOut:
    """只做分析:让模型列出剧本里的人物 / 道具 / 场景,以及每个镜头出现了哪些实体。
    不建项目,前端拿这个结果让用户逐个指定素材。"""
    plan, engine, detail = analyze_script(
        payload.script,
        payload.shot_count,
        payload.total_seconds,
        content_mode=payload.content_mode,
        garment_desc=payload.garment_desc,
    )
    if not plan["shots"]:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "剧本内容为空,无法切分镜头")
    # 换装镜实际是按「这一镜要换几件」算出来的帧数拍的(见 quick_start.float_shot_frames),
    # 分镜列表上把秒数显示成真实的那个 —— 不然列表写着 2.0s、其实拍了 5.9s,看的人一头雾水。
    prop_names = [str(e.get("name") or "") for e in (plan.get("props") or [])]
    for shot in plan["shots"]:
        if quick_start.is_change_shot(shot.get("action")):
            want = set(shot.get("props") or [])
            ev = quick_start.change_events([n for n in prop_names if n in want])
            fr = (
                quick_start.float_shot_frames(len(ev), settings.fps)
                if len(ev) >= 2
                else 3 * settings.fps
            )  # 一镜一件 → 3 秒
            shot["seconds"] = round(fr / settings.fps, 1)
    return ScriptPlanOut(**plan, engine=engine, engine_detail=detail)


# ---------------- 快速创建:上传服装图 -> 读图 -> 生成剧本 ----------------
def _resolve_image(ref: str, db: Session) -> str:
    """把 "asset:1" / "file:H:/x.png" 解析成本地绝对路径。"""
    ref = (ref or "").strip()
    if ref.startswith("asset:"):
        try:
            asset = db.get(Asset, int(ref.split(":", 1)[1]))
        except ValueError:
            asset = None
        if asset is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "素材不存在")
        path = STORAGE_DIR / asset.rel_path
    elif ref.startswith("file:"):
        path = Path(ref[5:])
    else:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "图片引用格式不对")
    if not path.exists():
        raise HTTPException(status.HTTP_404_NOT_FOUND, "图片文件不存在")
    return str(path)


def _has_image(ref: str, db: Session) -> bool:
    """这个引用能不能真的取到图(不抛异常版,给「常驻配饰」的过滤用)。

    取不到图的常驻配饰不能写进提示词:提示词里等于告诉模型"她全程戴着这件",
    但模型手里没有样本,只能照件名凭空画(见下面 accessories 那段注释)。
    """
    ref = (ref or "").strip()
    if ref.startswith("asset:"):
        try:
            asset = db.get(Asset, int(ref.split(":", 1)[1]))
        except ValueError:
            return False
        return asset is not None and (STORAGE_DIR / asset.rel_path).exists()
    if ref.startswith("file:"):
        return Path(ref[5:]).exists()
    return False


@router.get("/quick-options", response_model=QuickOptionsOut)
def quick_options(
    user: User = Depends(get_current_user), db: Session = Depends(get_db)
) -> QuickOptionsOut:
    """快速创建页要用的场景 / 调性 / 道具清单(每个都带一句"适合什么"的建议)+ 细节镜头库。"""
    lib = user_shot_library(user, db)["library"]
    return QuickOptionsOut(**quick_start.options(), shot_library=lib)


@router.post("/describe-garment", response_model=GarmentOut)
def describe_garment_endpoint(
    payload: GarmentIn,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> GarmentOut:
    """读服装图(一张或多张):返回客观描述 + 根据衣服风格推荐的场景/调性/道具。

    多张 = 同一套服装的不同件。视觉模型会逐张认出「这张是哪一件」,并在 pieces 里回填到对应的 ref
    —— 分析阶段靠它把「套装」拆成一件件独立道具,建项目时再按件绑图,模型就不会把几件揉成一件。
    """
    refs = [r.strip() for r in (payload.refs or []) if r and r.strip()]
    if not refs and payload.ref.strip():
        refs = [payload.ref.strip()]  # 兼容只传单张的老调用
    if not refs:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "先选一张服装图")
    if len(refs) > 8:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "一套服装最多 8 张图")

    # 图片顺序 = refs 顺序 = 模型看到的「图 1、图 2…」,认件结果要靠这个顺序回填
    pairs = [(ref, _resolve_image(ref, db)) for ref in refs]
    data, engine, detail = quick_start.describe_garment([p for _, p in pairs])
    if data is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail)

    pieces: list[GarmentPieceOut] = []
    for piece in data.pop("pieces", []) or []:
        try:
            idx = int(piece.get("index"))
        except (TypeError, ValueError):
            continue
        if 1 <= idx <= len(pairs):
            pieces.append(
                GarmentPieceOut(
                    ref=pairs[idx - 1][0],
                    part=str(piece.get("part") or ""),
                    desc=str(piece.get("desc") or ""),
                )
            )
    return GarmentOut(**data, pieces=pieces, engine=engine, engine_detail=detail)


@router.post("/draft-script", response_model=DraftScriptOut)
def draft_script_endpoint(
    payload: DraftScriptIn,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> DraftScriptOut:
    """按(服装 + 空间 + 调性 + 道具 + 时长/镜头数)写一份可直接出片的剧本。

    传了 playbook_id 就以那个玩法为准:结构、打底穿搭、自定义动作、场景都从玩法取;
    镜头数 / 总时长仍以请求里的为准(前端选中玩法时会用玩法默认值预填,用户可改)。
    """
    float_mode = payload.float_mode
    float_variant = payload.float_variant
    base_outfit = payload.base_outfit
    custom_actions = [a for a in (payload.custom_actions or []) if a.strip()]
    scene = payload.scene

    if payload.playbook_id:
        pb = db.get(Playbook, payload.playbook_id)
        if pb is None or (not pb.is_system and pb.owner_id != user.id):
            raise HTTPException(status.HTTP_404_NOT_FOUND, "玩法不存在")
        params = pb.params or {}
        float_mode = True
        float_variant = "side" if pb.structure == "float_dress_side" else "overhead"
        base_outfit = base_outfit or str(params.get("base_outfit") or "")
        if not custom_actions:
            custom_actions = [
                str(a) for a in (params.get("custom_actions") or []) if str(a).strip()
            ]
        scene = scene or str(params.get("scene") or "")

    scene_desc = payload.scene_desc.strip()
    if not scene_desc:
        scene_desc = next(
            (s["desc"] for s in quick_start.SCENES if s["v"] == scene and s["desc"]),
            "一间干净的白色影棚:纯白背景墙、浅灰地面,没有任何家具或陈设,冷调柔光",
        )

    lib = user_shot_library(user, db)
    data, engine, detail = quick_start.draft_script(
        garment=payload.garment,
        scene_desc=scene_desc,
        style=payload.style,
        props=payload.props,
        shot_count=payload.shot_count,
        total_seconds=payload.total_seconds,
        extra=payload.extra,
        float_mode=float_mode,
        float_variant=float_variant,
        base_outfit=base_outfit,
        custom_actions=custom_actions,
        picked_shots=payload.picked_shots,
        shot_map=lib["shot_map"],
        label_map={it["key"]: (it.get("label") or "") for it in lib["library"]},
        parts=payload.parts,
        part_kinds=payload.part_kinds,
        piece_style=payload.piece_style,
    )
    if data is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail)
    return DraftScriptOut(
        title=data["title"],
        script=data["script"],
        engine=engine,
        engine_detail=detail,
        # 按件排镜:清单有N行就该有N镜(可能比前端按件数估的多)—— 前端拿它去排分镜
        shot_count=data.get("shot_count"),
    )


def _round_to_half_second(frames: int) -> int:
    """把帧数对齐到半秒的整数倍。

    不对齐会出 1.96 秒这种"刚好差一点点"的镜头 —— 播放器的时间轴按整秒向下取整,
    会把它显示成 0:01,看起来就像"这个视频少 1 秒"(实测用户报过)。
    """
    step = max(2, round(settings.fps / 2))
    return max(step, round(frames / step) * step)


def _distribute_frames(
    seconds: list[float], total_seconds: float
) -> tuple[list[int], bool]:
    """把总时长按每个镜头的权重分配成帧数,并做单镜头上下限约束。"""
    count = max(1, len(seconds))
    want = max(count * MIN_SHOT_FRAMES, round(total_seconds * settings.fps))
    cap = max(MIN_SHOT_FRAMES, min(settings.gen_length, want))

    weights = [s if s and s > 0 else 0.0 for s in seconds]
    if sum(weights) <= 0:
        weights = [1.0] * count

    total_weight = sum(weights)
    raw = [round(w / total_weight * want) for w in weights]
    frames = [_round_to_half_second(max(MIN_SHOT_FRAMES, min(cap, r))) for r in raw]
    return frames, any(r > cap for r in raw)


def _gone_groups(pool: list[dict], name: str) -> set[str]:
    """按件名在 pool 里找实体,算出它穿上身后**打底里哪几组会被顶掉**。

    外套默认只是加穿;但**扣上的外套**(开合 closed)已经是这一镜的新上衣,
    里面那件旧上衣就该退场 —— 否则状态链和换装说明会互相打架
    (项目 tezsd99 第 4 镜说"睡衣上衣不在她身上",第 5 镜又把它写成"整镜保持原样")。
    """
    e = next((x for x in pool if x.get("name") == name), None)
    if e is None and name:
        e = next(
            (
                x
                for x in pool
                if x.get("name") and (name in x["name"] or x["name"] in name)
            ),
            None,
        )
    e = e or {}
    slot = e.get("_slot") or ""
    # 页面上的「自动」是空值;万一传了别的字(例如 auto),也按"跟素材库 / 全局默认"解析,
    # 不能把它当成一个显式开合 —— 否则提示词说扣齐、状态链却按加穿算。
    close = str(e.get("_close") or "").strip().lower()
    if close not in CLOSE_STATES:
        close = default_close(name, slot)
    return quick_start._displaced_groups(name, slot, close)


@router.post(
    "/from-script", response_model=ScriptProjectOut, status_code=status.HTTP_201_CREATED
)
def create_project_from_script(
    payload: ScriptBuildIn,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> ScriptProjectOut:
    """按剧本分析结果建项目。

    用户为某个人物/道具/场景指定了素材,该素材就会成为「出现它的那些镜头」的参考图,
    提示词里用 <Picture N> 引用;没指定的实体只写文字描述,交给模型自己生成。
    """
    shots = [s.model_dump() for s in payload.plan.shots]
    if not shots:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "分镜为空,无法创建项目")

    # 第 4 栏「风格参考图」(实拍 lookbook):挂在每一镜参考图列表最后,当「实拍穿着参考」用 ——
    # 它不占件、不进「要换上的这几件」,build_prompt 只补一句"只参考它身上这套衣服的款式 /
    # 颜色 / 材质 / 垂坠感和穿法,不参考那个人"。
    # 「按件排镜」以前**根本没把这个栏位传下来**,所以用户在页面上传了也等于没传(用户报"是不是要加个风格参考图")。
    style_refs = [
        (it.ref or "").strip() for it in payload.style_items if (it.ref or "").strip()
    ]
    if payload.float_mode and style_refs:
        for i0, s in enumerate(shots):
            # **开场那一镜(她原本那套打底的展示)不挂风格参考图** ——
            # 风格图是"要换上的这套衣服的实拍穿着照",和第 1 镜那身打底根本不是同一套;
            # 挂上去模型会直接照抄风格图里那身(用户报"短袖衫阔腿裤展示怎么不是睡衣")。
            # 项目 155 能跑对,就是因为它的第 1 镜没挂这张图。
            if (
                i0 == 0
                and payload.base_items
                and quick_start.is_display_shot(s.get("action"))
            ):
                continue
            s["wear_refs"] = list(style_refs)

    entities = {k: [e.model_dump() for e in getattr(payload.plan, k)] for k in KINDS}

    # 「人物背景图」:整片所有分镜统一使用的场景 —— 人物就站在这张背景里摆动作。
    # 做法:把它当成**唯一的场景实体**传给每一镜(参考图 + <Scene> 定义),
    # 原本的「纯白背景 / 白棚」场景全部换掉,提示词里的纯白背景约束也会跟着换成"以这张图为准"。
    bg_ref = (payload.background_ref or "").strip()
    bg_scene: dict | None = None
    bg_desc = ""
    if bg_ref:
        bg_desc = (payload.background_desc or "").strip()
        if not bg_desc and bg_ref.startswith("asset:"):
            try:
                _bg_asset = db.get(Asset, int(bg_ref.split(":", 1)[1]))
            except (ValueError, TypeError):
                _bg_asset = None
            if _bg_asset is not None:
                bg_desc = (_bg_asset.ai_desc or "").strip()
        entities["scenes"] = [{"name": "人物背景", "desc": bg_desc}]
        bg_scene = entities["scenes"][0]
        payload.assignments["scenes:人物背景"] = [bg_ref]

    # 按件排镜一定只有一个模特 —— 分析剧本那一步偶尔会**漏列 characters**(内容清单模式下它只负责
    # 排镜头、不编内容),一旦漏了:① 人物参考图绑不进 refs → 提示词里给模特的那句
    # 「见 <Picture 1>」会指到**第一件衣服**上(用户报"人物图怎么没有了");② person_desc 拿不到,
    # 全篇代词退化成"模特"。这里兜底补一个人物实体,图由下面绑图那段补上。
    if payload.float_mode and not entities["characters"]:
        entities["characters"] = [{"name": "她", "desc": ""}]

    # 件名 -> 「读图认件」的结果(部位 / 长短)。图片在入库或第一次用到时就认过一遍,结果缓存在
    # asset.ai_kind / ai_len 上;这里按 assignments 的"这件用了哪张图"把结果**补回实体**。
    # 少了这一步图片等于白认:分析剧本那步只给出「件名」(开放词表),后端就只能拿件名里的字
    # 去猜"这是外套还是上衣、是一整件还是下装" —— 雪服 / 汉服 / 内搭 / T恤 一个都猜不出来
    # (用户报"图片不是调用了 LLM 识别吗")。
    _ai_of_ref: dict[str, dict] = {}

    def _remember_ai(_ref: str) -> None:
        """把一张素材图缓存里的读图结果(部位 / 长短 / 开合 / 描述)记下来。"""
        _ref = (_ref or "").strip()
        if not _ref.startswith("asset:") or _ref in _ai_of_ref:
            return
        try:
            _a = db.get(Asset, int(_ref.split(":", 1)[1]))
        except (ValueError, TypeError):
            return
        if _a is not None:
            _ai_of_ref[_ref] = {
                "slot": (_a.ai_kind or "").strip(),
                "len": (_a.ai_len or "").strip(),
                "close": (_a.ai_close or "").strip(),
                # 素材的读图描述也带上：小配件（鞋 / 眼镜…）如果只靠模型给的
                # 「拖鞋，穿在脚上」这种泛化描述，模型会自己画成另一双鞋
                # （用户报「鞋子跟道具里不一样」）。有 ai_desc 就用它覆盖。
                "desc": (_a.ai_desc or "").strip(),
            }

    for _pairs in (payload.assignments or {}).values():
        for _ref in _pairs or []:
            _remember_ai(_ref)
    # **打底 / 常驻配饰也要记**:它们的图是页面单独传的(base_items / accessories),不在
    # assignments 里。不记的话这些件就没有「部位」,只能按件名 / 描述里的字猜 ——
    # 实测「拖鞋」(描述是「米色针织毛毡穆勒拖鞋」)被「针织」骗成上身,拖鞋定义 / 鞋型全丢。
    for _it in list(payload.base_items or []) + list(payload.accessories or []):
        _remember_ai(getattr(_it, "ref", "") or "")

    # 项目级「开合」覆盖(一键换装件列表里改的):**按参考图**覆盖素材库里的设置 ——
    # 用 ref 而不是件名,免得"分析剧本"给这件改个名就对不上了。
    for _ref, _st in (payload.closes or {}).items():
        _ref = str(_ref or "").strip()
        _st = str(_st or "").strip().lower()
        if not _ref or _st not in CLOSE_STATES:
            continue
        _ai_of_ref.setdefault(_ref, {})
        _ai_of_ref[_ref]["close"] = _st

    def _recognition(ref: str) -> dict:
        """这件用的那张图上缓存的读图结果(_slot / _len),外加它自带的那张图(_ref)。

        `_ref` 是给绑参考图用的:打底 / 常驻配饰的图是页面直接传的,存下来就不必再靠
        assignments 的名字匹配(plan 改名之后就永远对不上)。
        """
        ref = (ref or "").strip()
        out: dict = {}
        got = _ai_of_ref.get(ref)
        if got:
            if got["slot"]:
                out["_slot"] = got["slot"]
            if got["len"]:
                out["_len"] = got["len"]
            if got.get("close"):
                out["_close"] = got["close"]
        if ref.startswith("asset:"):
            out["_ref"] = ref
        return out

    def _refs_of(nm: str) -> list[str]:
        """plan 里这个件名绑了哪几张图(页面第 3 栏那件就是靠它认出来的)。"""
        return [
            str(r).strip()
            for r in (payload.assignments or {}).get(f"props:{nm}") or []
            if str(r).strip()
        ]

    def _name_hit(e: dict, nm: str) -> bool:
        n = e.get("name") or ""
        return n == nm or (nm and (nm in n or n in nm))

    def _fresh_base_name(nm: str, pool: list[dict]) -> str:
        """给打底件取个不撞车的名字:「阔腿裤」→「阔腿裤(旧)」(只有真撞名才加后缀)。"""
        taken = {e.get("name") or "" for e in pool}
        if nm not in taken:
            return nm
        for suf in ("(旧)", "(旧)2", "(旧)3"):
            if nm + suf not in taken:
                return nm + suf
        return nm + "(旧)"

    for _e in entities["props"]:
        for _ref in (payload.assignments or {}).get(
            f"props:{_e.get('name') or ''}", []
        ) or []:
            got = _ai_of_ref.get((_ref or "").strip())
            if not got:
                continue
            if got["slot"] and not _e.get("_slot"):
                _e["_slot"] = got["slot"]
            if got["len"] and not _e.get("_len"):
                _e["_len"] = got["len"]
            if got.get("close") and not _e.get("_close"):
                _e["_close"] = got["close"]
            # 描述太泛（空 / 只有件名 / 「XX，穿在脚上」这种）→ 用素材读图描述补全。
            # 参考图只是软条件，鞋子的「包头 / 不露脚趾」这种版型细节必须靠文字钉住。
            _d = (got.get("desc") or "").strip()
            _cur = (_e.get("desc") or "").strip()
            if _d and (
                not _cur
                or len(_cur) <= 12
                or "穿在脚上" in _cur
                or "戴在眼睛上" in _cur
                or "拿在手里" in _cur
            ):
                _e["desc"] = _d
            break

    # **把「客户端明确绑了图、但模型漏列的件」补进 props**。件名 + 图是页面上的确定值
    # (第 3 栏每件都会写一条 assignments["props:<件名>"]),而 plan.props 的件名是**模型给的** ——
    # 「分析剧本」那步模型过载时(实测 DeepSeek "Service is too busy")会整条漏列件名
    # (实测项目 197:要换的那两件完全没进 props)。漏了之后下面的状态链 (`_resolve` / `nth_piece` /
    # `piece_state_names`) 全解析不出来 —— 换装镜既没有人物图、也没有要换的那件
    # (用户报"人物图怎么没有了"、"要换的衣服没绑上")。这里按件名去重后补进去,模型退化成什么样都不影响。
    for _akey, _avals in (payload.assignments or {}).items():
        if not str(_akey).startswith("props:"):
            continue
        _anm = str(_akey).split(":", 1)[1].strip()
        _aref = next((str(r).strip() for r in (_avals or []) if str(r).strip()), "")
        if not _anm or not _aref:
            continue
        if any(_name_hit(e, _anm) for e in entities["props"]):
            continue
        entities["props"] = entities["props"] + [
            {"name": _anm, "desc": "", **_recognition(_aref)}
        ]

    # 「扯上身」风格(2026-10-20):开场把初始衣服一把扯掉,只剩固定内搭(短上衣 + 短裤),
    # 整套要换的衣服无人穿着、等距悬空排成一列在她身侧,她一件件扯下来穿上。
    from ..piece_prompt import (
        PULL_INNER_BOT,
        PULL_INNER_BOT_DESC,
        PULL_INNER_TOP,
        PULL_INNER_TOP_DESC,
    )

    _pull = bool(
        payload.float_mode
        and payload.float_variant == "piece"
        and (payload.piece_style or "").strip().lower() == "pull"
    )
    # 「扯上身」是**一镜穿整套** —— 这一镜要穿上的全部件名(页面按顺序传上来)。
    _pull_names = [
        str(n).strip() for n in (payload.piece_names or []) if str(n).strip()
    ]

    def _shot_title(t: str) -> str:
        """「扯上身」风格把镜名里的「飞入换装」换成「扯上身」——
        不改的话分镜清单里还写着"飞入换装",看着像没切风格(检测走的是动作文本,不受影响)。"""
        return (t or "").replace("飞入换装", "扯上身") if _pull else (t or "")

    # 「打底 / 旧件」(用户指定、**带图**):必须进每一镜,否则模型手里没有那件旧衣的图 ——
    # 会自己编一件(用户报"我的睡衣呢"),旧件也不会"消失"。
    # 标 _base:build_prompt 会写成「她一开始就穿着、本镜头会被换掉」,并注明它不参与飞行。
    base_ents: list[dict] = []
    if payload.float_mode and payload.base_items:
        for it in payload.base_items:
            nm = (it.name or "").strip()
            if not nm:
                continue
            want_ref = (it.ref or "").strip()
            ent = next((e for e in entities["props"] if _name_hit(e, nm)), None)
            # **同名不等于同一件**:页面第 2 栏和第 3 栏放两件同名的衣服(典型:"换一条阔腿裤")
            # 时,分析剧本只会给出一条件 —— 这时比**参考图**:图不是同一张,就说明是两件不同的
            # 衣服,打底那条要**另立一条实体**(名字加后缀区分)。否则两条会并成一条,
            # 要换上的那件永远绑着打底那张图、而且没有旧件可替换
            # (实测 168:阔腿裤换阔腿裤 → 变成"加穿一条阔腿裤")。
            if ent is not None and want_ref:
                own = _refs_of(ent.get("name") or "")
                if own and want_ref not in own:
                    ent = None
            if ent is not None:
                # 分析那步可能已经把它列进 props 了 —— 那就地补 _base 标记
                # (标了才会被 build_prompt 当「本镜头会被换掉的旧件」,也才会排到新件后面)。
                ent["_base"] = True
                base_ents.append(ent)
            else:
                ent = {
                    "name": _fresh_base_name(nm, entities["props"]),
                    "desc": (it.desc or "").strip(),
                    "_base": True,
                    **_recognition(it.ref),
                }
                base_ents.append(ent)
                entities["props"] = entities["props"] + [ent]
    # 固定内搭(扯掉初始衣服之后剩下的那两件):**没有参考图**,只是文字里的穿着层。
    # 但必须进 props —— 状态链 _state() 会引用它们,不然每镜「她身上穿着…」会变成空的。
    if _pull:
        for _inm, _ids, _islot in (
            (PULL_INNER_TOP, PULL_INNER_TOP_DESC, "上身"),
            (PULL_INNER_BOT, PULL_INNER_BOT_DESC, "下身"),
        ):
            if not any(e.get("name") == _inm for e in entities["props"]):
                entities["props"] = entities["props"] + [
                    {"name": _inm, "desc": _ids, "_slot": _islot, "_inner": True}
                ]
        # 固定内搭**不能挂任何商品图 / 别人的描述**:分析剧本会在清单里读到「短上衣」「短裤」,
        # 前端按名字分组绑图时会把它俩配到「睡衣上衣 / 碎花睡裤」上(同组兜底)——
        # 结果"剩下的短裤"就是那条睡裤的图,模型照着把长裤剪成短裤(实测 p305)。
        for _k in ("props:" + PULL_INNER_TOP, "props:" + PULL_INNER_BOT):
            (payload.assignments or {}).pop(_k, None)
        for _e in entities["props"]:
            if (_e.get("name") or "") in (PULL_INNER_TOP, PULL_INNER_BOT):
                _e["_inner"] = True
                _e["_slot"] = "上身" if _e["name"] == PULL_INNER_TOP else "下身"
                _e["desc"] = (
                    PULL_INNER_TOP_DESC
                    if _e["name"] == PULL_INNER_TOP
                    else PULL_INNER_BOT_DESC
                )
                _e.pop("_ref", None)
        # 「这一镜要一次穿上的全部件名」以**页面绑定的图**为准(assignments 的 props:*)——
    # 分析剧本那步可能把几件揉进一句、甚至改名,靠 plan.props 认不全(实测 p304 只认出高跟鞋,
    # 吊带 / 半裙 / 开衫全丢,那一镜被当成"陈列镜"、新衣服整场没进画面)。
    # 排除:初始服装(它们会被扯掉)、常驻配饰(一直戴着)、以及固定内搭。
    if _pull and not _pull_names:
        _base_nm = {str(it.name or "").strip() for it in (payload.base_items or [])}
        _acc_nm = {str(it.name or "").strip() for it in (payload.accessories or [])}
        _asg = [
            str(k).split(":", 1)[1].strip()
            for k in (payload.assignments or {})
            if str(k).startswith("props:")
        ]
        _pull_names = [
            n
            for n in dict.fromkeys(_asg)
            if n
            and n not in _base_nm
            and n not in _acc_nm
            and n not in (PULL_INNER_TOP, PULL_INNER_BOT)
        ]
    if _pull and _pull_names:
        # 件名统一成页面上的名字。分析剧本那步会把件改名(「吊带背心(燕麦色)」「套装-开衫」…)——
        # 状态链 / present 是按件名精确过滤的,名字对不上这件就被丢掉,那一镜只剩内搭
        # (实测 p304/p305:新衣服全部没进画面)。找不到实体的就用 assignments 那张图补一条。
        for _want in list(_pull_names):
            if any(e.get("name") == _want for e in entities["props"]):
                continue  # 已经有同名实体(assignments 兜底补过),不重复加
            _ent = next(
                (
                    e
                    for e in entities["props"]
                    if not e.get("_base")
                    and not e.get("_inner")
                    and _name_hit(e, _want)
                ),
                None,
            )
            if _ent is not None:
                _ent["name"] = _want
                continue
            _ref = next(
                (
                    str(r).strip()
                    for r in ((payload.assignments or {}).get("props:" + _want) or [])
                    if str(r).strip()
                ),
                "",
            )
            if _ref:
                entities["props"] = entities["props"] + [
                    {"name": _want, "desc": "", **_recognition(_ref)}
                ]
    # 「常驻配饰」(墨镜 / 拖鞋…):全程戴着、不换的小件。分析那步通常认不出来(它们是"配备"
    # 不是"衣服"),所以这里和旧件一样按用户传的件名**补进 props** —— 补进去之后下面那段
    # always_worn 才会把它们绑到每一镜(否则开场状态镜里她既没墨镜也没鞋)。
    if payload.float_mode and payload.accessories:
        for it in payload.accessories:
            nm = (it.name or "").strip()
            if not nm:
                continue
            # **没有参考图的常驻配饰一律不写进提示词**(用户 2026-10-15 要求):
            # 写进去 = 告诉模型"她全程戴着这件",可模型手里没样本,只能照件名凭空画 ——
            # 实测项目 191(测试499)凭空多出一副眼镜架在头顶,用户报"我没让她戴着眼镜啊"。
            # 顺带把每件的 ref 打出来:这个 bug 已经复现两次,日志能一眼看出它从哪来。
            ok = _has_image(it.ref, db)
            print(
                f"[projects] 常驻配饰「{nm}」ref={it.ref!r} "
                + ("有图,绑上" if ok else "取不到图 → 跳过、不写进提示词"),
                flush=True,
            )
            if not ok:
                continue
            got = _recognition(it.ref)
            ent = next(
                (
                    e
                    for e in entities["props"]
                    if e.get("name") == nm
                    or nm in (e.get("name") or "")
                    or (e.get("name") or "") in nm
                ),
                None,
            )
            if ent is None:
                entities["props"] = entities["props"] + [
                    {"name": nm, "desc": (it.desc or "").strip(), **got}
                ]
            else:
                # 分析那步已经把它列进 props 了 —— 也要把**用户传的这张图**挂上去。
                # 不挂的话它会变成"只有名字的小配件":图片绑定走 assignments 的名字匹配,
                # plan 稍一改名就永远对不上,模型照样只能凭文字编。
                ent.update(got)
    count = len(shots)
    # 所有从镜头库勾进来的通用分镜(系统内置 + 自定义「我的镜头」):尾部统一精简为
    # 「速度 + 画面」两句(见 script_split.TAIL_ZH_CUSTOM),non_diegetic_music 段保留。
    # plan 走完 LLM 后 key 已经丢了,这里按动作文本前 40 字跟库里的 action 比对来认。
    _lib = user_shot_library(user, db)
    _lib_acts = [a.strip() for a in _lib["shot_map"].values() if a]
    # 自定义分镜在镜头库里设了默认时长(seconds)的,建项目时按它落地,
    # 不要被总时长按权重平均掉(否则设了 5 秒还是会被分回 3 秒)。
    _sec_by_action = {
        (it.get("action") or "").strip(): float(it.get("seconds") or 0)
        for it in _lib["library"]
        if it.get("group") == "custom" and (it.get("action") or "").strip()
    }
    frames, clamped = _distribute_frames(
        [float(s.get("seconds") or 0) for s in shots], payload.total_seconds
    )
    for _i, _shot in enumerate(shots):
        _secs = _sec_by_action.get((_shot.get("action") or "").strip()) or 0
        if _secs:
            frames[_i] = max(
                MIN_SHOT_FRAMES,
                _round_to_half_second(round(_secs * settings.fps)),
            )
    # 「换装镜」(衣服直接吸上身)的时长按**这一镜实际要换几件**来算
    # (见 quick_start.float_shot_frames):一件 = 进场 + 停顿,件数越多、这一镜越长 ——
    # 时间给够,模型才排得开「一件一件」。以前写死 56 帧,一镜三件时它会在 0.75 秒内
    # 把三件一起糊上来(实测)。
    float_events: dict[int, list[str]] = {}
    if payload.float_mode:
        # 「旧件」不算「这一镜要换的件」——它只是开场穿在身上、会被替换掉。
        # 算进去的话,单件镜会被当成多件镜,时长按"一拍一件"放大(实测 243 帧 / 10 秒)。
        base_name_set = {str(e.get("name") or "") for e in base_ents}
        prop_names = [str(e.get("name") or "") for e in entities["props"]]
        prop_slots = {
            str(e.get("name") or ""): str(e.get("_slot") or "")
            for e in entities["props"]
        }
        for i, shot in enumerate(shots, start=1):
            want = set(shot.get("props") or [])
            float_events[i] = quick_start.change_events(
                [n for n in prop_names if n in want and n not in base_name_set],
                slots=prop_slots,
                # 按件排镜:鞋是**独立的一件**(旧拖鞋 → 新鞋),不能当常驻配饰滤掉。
                keep_shoes=(payload.float_variant == "piece"),
            )
        for i, shot in enumerate(shots):
            if quick_start.is_change_shot(shot.get("action")):
                ev = float_events[i + 1]
                # 一镜只换一件:「按件排镜」和单件换装镜都拍 3 秒。
                # 「一镜穿完整套」(≥2 件)才按「一拍一件」的时长公式放大。
                if payload.float_variant == "piece":
                    # 按件模式:每镜只管一件 —— 直接用 3 秒(用户 2026-10-19 实测 3 秒版
                    # 动作排得开、换装也完整;48 帧偏赶)。
                    frames[i] = 3 * settings.fps
                else:
                    frames[i] = (
                        quick_start.float_shot_frames(len(ev), settings.fps)
                        if len(ev) >= 2
                        else 3 * settings.fps
                    )
            elif payload.float_variant == "piece" and not quick_start.is_display_shot(
                shot.get("action")
            ):
                # 按件模式的「通用分镜」(镜头库勾进来的细节镜):一律 3 秒 —— 这些都是
                # 单个小动作(补妆/自拍/划领口…),1 秒还没站稳就切走了;权重分出来的
                # 时长也压不到 1.5 秒。3 秒动作看得完,模型也不会把动作摊稀。
                frames[i] = 3 * settings.fps
    detail = payload.plan.engine_detail
    if clamped:
        longest = settings.gen_length
        if float_events:
            longest = max(
                longest,
                max(
                    quick_start.float_shot_frames(len(v), settings.fps)
                    for v in float_events.values()
                ),
            )
        detail = (
            f"{detail};单镜头一次生成最长约 {longest / settings.fps:.1f} 秒,"
            f"更长的镜头已封顶"
        )

    project = Project(
        user_id=user.id,
        template_id=None,
        title=payload.title,
        out_width=settings.out_width,
        out_height=settings.out_height,
        fps=settings.fps,
        status=ST_QUEUED,
        bg_ref=bg_ref or None,
        bg_desc=bg_desc or None,
        head_ref=(payload.head_ref or "").strip()[:512] or None,
        piece_style=(payload.piece_style or "throw").strip().lower() or "throw",
    )
    db.add(project)
    db.flush()

    # 悬浮变装里的小件佩戴配饰(墨镜 / 项链 / 帽…)不单独排镜、从第一镜起就一直戴着 ——
    # 所以每一镜都算它出现,把它自己的商品图绑上,免得模型凭空画、或者干脆不画。
    # 同一条规矩:**没绑到参考图的常驻配饰不写进提示词**(test3001 / 201 的眼镜墨镜项链
    # 耳饰,都是这么混进来的)。图有两个来源:页面上传的 `_ref` 或 assignments 里的绑定。
    # 鞋要单独排一镜换装(用户 2026-10-19:初始服装里加一双鞋、新衣服里加一双新鞋,
    # 换装时把旧的换掉)。所以它**不能**再按「一直戴着的小配件」绑到每一镜 ——
    # 绑了的话开场就把新鞋画上脚、换完又把旧鞋挂回去,和换鞋分镜直接打架。
    # 判据:带 `_base` 的旧鞋 + 剧本里被当成「要换上的那一件」的鞋。
    _swap_shoes = {quick_start.piece_name_of(s.get("action")) or "" for s in shots}
    if _pull:
        # 「扯上身」是整套一次穿 —— 这一镜要穿的鞋也算"被换的",不能再按常驻配饰绑到每一镜
        # (绑了开场就把新鞋画上脚了)。
        _swap_shoes |= set(_pull_names)
    _swap_shoes = {n for n in _swap_shoes if n and quick_start._is_shoe(n)}

    def _is_swapped_shoe(e: dict) -> bool:
        nm = e.get("name") or ""
        if not quick_start._is_shoe(nm):
            return False
        return bool(e.get("_base")) or any(
            nm == s or nm in s or s in nm for s in _swap_shoes
        )

    always_worn = (
        [
            e
            for e in entities["props"]
            if is_worn_accessory(e["name"])
            and not _is_swapped_shoe(e)
            and (e.get("_ref") or payload.assignments.get(f"props:{e['name']}"))
        ]
        if payload.float_mode
        else []
    )

    # 换装镜要「接着上一镜的衣服」:一件一件换的时候,后面那一镜得把**她已经穿在身上的那几件**
    # 的商品图也一起绑上 —— 不然模型手上只有文字,会把那件衣服重新"想象"一遍,出来就跟上一镜
    # 不是同一件(用户报过"第一张图不对、没有继续上张图的衣服")。只在这些换装镜里补,
    # 细节镜不动(往细节镜塞衣服图会把它劫持成"从衣服起手")。
    worn_ahead: dict[int, list[str]] = {}
    if payload.float_mode:
        done: list[str] = []
        for i, shot in enumerate(shots, start=1):
            worn_ahead[i] = list(done)
            nm = quick_start.piece_name_of(shot.get("action"))
            if nm:
                done.append(nm)

    # 「按件排镜」的状态图链:每件前面那镜「展示镜」拍完之后,把它的末帧落成一张**状态图**;
    # 换装镜的首帧锚 = 上一张状态图、尾帧锚 = 这一张状态图 —— 模型照着图把衣服穿好,
    # 「衣服穿不上 / 停在身前 / 旧件不消失」这类问题就通了。
    # 状态图路径按项目号固定生成,渲染时由 pipeline 落盘(见 _save_state_image)。
    change_ordinal: dict[int, int] = {}  # 镜号 -> 这是第几件(1 起)
    state_ordinal: dict[int, int] = {}  # 镜号 -> 这一镜展示的是第几个状态(0 = 打底)
    if payload.float_mode:
        seen = 0
        for i2, sh2 in enumerate(shots, start=1):
            _is_chg = quick_start.is_change_shot(sh2.get("action"))
            _is_disp = quick_start.is_display_shot(sh2.get("action"))
            if _pull:
                # 「扯上身」只有两镜,按**标题**兜底认 —— 分析剧本那步可能把动作文本改写,
                # 把"吸到她身上 / 这一镜只做展示"这些标记词弄丢(实测 p306:换装镜被当成通用分镜)。
                _t = str(sh2.get("title") or "")
                if "扯上身" in _t:
                    _is_chg, _is_disp = True, False
                elif "打底" in _t or "全套展示" in _t:
                    _is_disp, _is_chg = True, False
            if _is_chg:
                seen += 1
                change_ordinal[i2] = seen
            elif _is_disp:
                # 第 1 镜(有打底时)就是「打底整套」那个状态 = S0;其余展示镜都是 S(seen+1)
                # 「扯上身」的开场镜即使没填初始服装也是链头 S0(它就是"扯掉之后"的状态)。
                if i2 == 1 and (base_ents or _pull):
                    state_ordinal[i2] = 0
                else:
                    state_ordinal[i2] = seen + 1

    def _state_path(j: int) -> str:
        return str(STORAGE_DIR / "states" / f"p{project.id}_s{j}.png")

    # ---- 按件排镜:每一镜她身上穿什么,由后端按「状态链」算,不采纳 plan 给的 props ----
    # 剧本清单本来就是后端写的(展示(打底) → [展示(第 k 件换好) → 换装(第 k 件)] × N),
    # 「分析剧本」那步只是让 LLM 把清单拆成镜 + 提实体 —— 而它提实体时会提错(实测:第 1 镜
    # 把「短裙 + 长袖上衣」当成打底、换装镜里那一件还留着「套装-XXX」这种拆件名),
    # 状态图链(每镜的尾帧锚 = 这一镜穿好的样子)就会跟着全错。所以这一镜穿什么**后端自己算**:
    #   状态 S(n) = 打底里「部位还没被换掉」的那些 + 已经换上的前 n 件。
    piece_state_names: dict[int, list[str]] = {}
    piece_swap: dict[
        int, tuple[list[str], list[str]]
    ] = {}  # 展示镜:这一镜(原来哪几件 → 换成哪几件)
    nth_piece: dict[int, str] = {}  # 第 k 件换上的是哪个实体(状态链用)
    if (
        payload.float_mode
        and payload.float_variant == "piece"
        and (change_ordinal or state_ordinal)
    ):
        pool = [e for e in entities["props"]]

        def _resolve(want: str, taken: set[str], prefer_new: bool = False) -> str:
            """在 props 实体里找出「want 这一件」:完全同名 > 互相包含 > 同部位。

            件名对不上是常态 —— 剧本用的是**页面上那件素材的名字**(「长袖衫」),plan 里却
            可能已经把它拆成了「套装-蕾丝一字肩长袖上衣」。按部位(读图给的 slot 优先)
            一定对得上,所以留这条兜底。

            ① 必须**先全池找完全同名的**:打底那条同名件会被改名成「阔腿裤(旧)」,
            「阔腿裤」不能因为「包含」关系被它抢走。
            """
            if not want:
                return ""
            cand = sorted(
                pool, key=lambda e: bool(e.get("_base")) if prefer_new else False
            )

            def _ok(e: dict) -> bool:
                nm = e.get("name") or ""
                return bool(nm) and nm not in taken

            for e in cand:
                if _ok(e) and e["name"] == want:
                    return e["name"]
            for e in cand:
                if _ok(e) and (want in e["name"] or e["name"] in want):
                    return e["name"]
            grp = quick_start._part_group(want, "")
            if grp and grp != "other":
                for e in cand:
                    if (
                        _ok(e)
                        and quick_start._part_group(e["name"], e.get("_slot") or "")
                        == grp
                    ):
                        return e["name"]
            return ""

        for i2, k2 in change_ordinal.items():
            got = _resolve(
                quick_start.piece_name_of(shots[i2 - 1].get("action")) or "",
                set(nth_piece.values()),
                prefer_new=True,
            )
            if got:
                nth_piece[k2] = got
        base_entity_names = [e["name"] for e in pool if e.get("_base")]
        if _pull:
            # 「扯上身」风格:开场镜就把初始那套扯掉了 —— 状态链的底 = 固定内搭(短上衣 + 短裤),
            # 后面每一镜都是在它外面往上穿。
            base_entity_names = [PULL_INNER_TOP, PULL_INNER_BOT]

        def _ent_of(nm: str) -> dict:
            """按件名找回实体(打底件优先)—— 取它的部位 / 开合。"""
            return (
                next((e for e in base_ents if e.get("name") == nm), None)
                or next((e for e in entities["props"] if e.get("name") == nm), None)
                or {}
            )

        def _state(n: int) -> list[str]:
            """第 n 个状态(0 = 打底)她身上有哪几件。"""
            if _pull:
                # 「扯上身」只有**一镜**:整套新衣服一次吸上身。
                # 她开始只穿内搭(短上衣 + 短裤),穿完就是内搭 + 全部新件。
                return [PULL_INNER_TOP, PULL_INNER_BOT] + (
                    list(_pull_names) if n >= 1 else []
                )
            done = [nth_piece[k] for k in range(1, n + 1) if k in nth_piece]
            changed: set[str] = set()
            for nm in done:
                changed |= _gone_groups(entities["props"], nm)
            return [
                nm
                for nm in base_entity_names
                if quick_start._replace_group(nm, _ent_of(nm).get("_slot") or "")
                not in changed
            ] + done

        if _pull and not _pull_names:
            # 页面没传件名时兜底:除打底 / 内搭 / 常驻配饰以外的都是这一镜要穿上的。
            _pull_names = [
                e["name"]
                for e in entities["props"]
                if not e.get("_base")
                and not e.get("_inner")
                and not is_worn_accessory(e["name"])
            ]

        for i2 in range(1, len(shots) + 1):
            if i2 in change_ordinal:
                k = change_ordinal[i2]
                # 换装镜 = 「换之前那身」+ 「这一镜要换上的那件」—— 两者都在,才能一头定义新件、
                # 一头把旧件标成 replaced(见下面 present["props"] 的重排)。
                if _pull:
                    # 只有一镜:内搭 + 全部新件
                    piece_state_names[i2] = _state(k)
                else:
                    names = _state(k - 1)
                    if nth_piece.get(k):
                        names = names + [nth_piece[k]]
                    piece_state_names[i2] = names
            elif i2 in state_ordinal:
                n = state_ordinal[i2]
                piece_state_names[i2] = _state(n)
                # 展示镜要**自己完成这一件替换**(它的末帧就是状态图)。光写"她穿着 A+B"是
                # **状态描述**,模型会保持不动;得把"原来哪件 → 换成哪件"这个**动作**告诉它
                # (实测:只写状态时,该换阔腿裤那一镜的末帧里她下半身还是睡裤)。
                if n >= 1:
                    prev_names, cur_names = _state(n - 1), _state(n)
                    piece_swap[i2] = (
                        [x for x in prev_names if x not in set(cur_names)],
                        [x for x in cur_names if x not in set(prev_names)],
                    )

    prev_pic_map: dict[
        int, int
    ] = {}  # 镜号 -> 「上一镜尾帧」是第几号参考图(加穿型展示镜用)
    # 「最后换装」这一镜 + 它产出的状态图:链之后的**通用分镜**都要把这张图当穿搭参考。
    # 用它(而不是 max(state_ordinal))是因为链后面还可能再插「整套展示」这类展示镜。
    last_change_idx = max(change_ordinal) if change_ordinal else 0
    final_state_idx = max(change_ordinal.values()) if change_ordinal else 0
    for i, shot in enumerate(shots, start=1):
        # 件链之外的镜 = 从镜头库勾进来的**通用分镜**(半身补妆 / 化妆师补右脸…)。
        # 它既不是展示镜也不是换装镜,排在换装链后面 —— 到这一镜她**早就换好装了**,
        # 但件链"每镜穿什么由状态链算"那套对它不生效(见下面 present 的补件)。
        general_piece_shot = (
            payload.float_mode
            and payload.float_variant == "piece"
            and i not in state_ordinal
            and i not in change_ordinal
        )
        _act0 = (shot.get("action") or "").strip()
        _no_ndm = bool(_act0) and any(_act0[:40] == ca[:40] for ca in _lib_acts)
        if i in piece_state_names:
            # 按件排镜:穿着由状态链给出(上面那段),plan 给的 props 不采纳。
            # 人物 / 场景照单全收 —— 按件镜里一定只有一个人、一面背景,plan 漏写就会丢掉代词
            # (`person_desc` 拿不到 → 提示词里全变成"模特")。
            want = set(piece_state_names[i])
            present = {
                "props": [e for e in entities["props"] if e["name"] in want],
                "characters": list(entities["characters"]),
                "scenes": list(entities["scenes"]),
            }
            # 「扯上身」风格的开场镜(S0):初始那套要真的出现在画面里(她一把扯掉它们),
            # 所以把它们的参考图也带上 —— 不然模型手里没有那几件旧衣的图,会自己编。
            if _pull and state_ordinal.get(i) == 0 and base_ents:
                _have = {e["name"] for e in present["props"]}
                present["props"] = list(present["props"]) + [
                    e for e in base_ents if e["name"] not in _have
                ]
        else:
            present = {
                k: [e for e in entities[k] if e["name"] in (shot.get(k) or [])]
                for k in KINDS
            }
            # **人物是每一镜都必须有的**:排镜大模型偶尔会在某一镜的 characters 里漏写人物,
            # 那一镜就会整镜丢掉人物参考图(实测 test99878 的「弯腰摸狗牵气球」:refs 里没有人,
            # <Subject 1> 也没有 → 脸只能靠文字猜)。漏了就用项目里的人物兜底补回来。
            if not present.get("characters"):
                present["characters"] = list(entities["characters"])
            # 通用分镜:无参考图的道具,只留「本镜动作文本里明确点到名字」的(手机自拍→手机)。
            # LLM 排镜会把所有镜的动作道具(墨镜/项链/耳饰/报纸…)全塞进每个细节镜的
            # shot.props —— 没图又和本镜动作无关的写进提示词,模型只会凭空乱画
            # (test3001 的眼镜、201 的墨镜项链耳饰,都是这么混进来的)。
            if general_piece_shot:
                act = shot.get("action") or ""
                present["props"] = [
                    e
                    for e in present["props"]
                    if e.get("_ref")
                    or payload.assignments.get(f"props:{e['name']}")
                    or e["name"] in act
                ]
            # 通用分镜(按件模式、不在件链上):到这一镜她**已经换好装了** —— 把「这会儿她
            # 身上穿的那几件」一起带过去(新换上的 + 还没被顶掉的打底)。补件的时机 = 前面已经
            # 走完的换装镜数量(`worn_ahead[i]` 就是它们),取状态 S(那个数)。
            #
            # 不补的话模型手上只有人物图 + 文字:整套衣服它自己**重新想象一遍**,出来和前面几镜
            # 对不上(和换装镜当初的问题一模一样,见上面 worn_ahead 那段注释)。补进来的件照
            # `_worn` 处理 —— 提示词里写成"她现在身上已经穿着的那一件",它的商品图也一并绑上
            # (见下面 refs 那段),两处一起钉住"这一镜她穿的是哪一套"。
            # 只认**绑了图的**件:没图的写进提示词等于叫模型凭空画一件不存在的衣服
            # (和「没绑到参考图的常驻配饰不写进提示词」同一条规矩)。图有两个来源:
            # 打底 / 常驻配饰是页面上直接给的(`_ref`),换上的那几件是按实体名从 assignments 取的。
            if general_piece_shot and change_ordinal:
                worn_names = _state(len(worn_ahead.get(i) or []))
                # **打底(初始服装)里已经被前面换装镜顶掉的那几件,在这一镜不能再给** ——
                # 排镜大模型写 shot.props 时会把打底旧件也列上(内容清单里提过它们),留着就会
                # 把它绑回参考图、还写成 <Prop>:到这一镜她早就换掉了,模型却照着旧件画
                # (用户报"除了换装那几个分镜,其它的分镜不应该有初始服装")。
                # 判据 = 状态链 S(换装完成数):不在这一套里的打底件一律剔除。
                present["props"] = [
                    e
                    for e in present["props"]
                    if not e.get("_base") or e["name"] in worn_names
                ]
                # 从 shot.props 匹配进来的 worn 件没有 _worn 标记 —— 先标上,
                # 不然 refs 闸门(下面)只认 _worn 不放行,它们的商品图就绑不上。
                for e in present["props"]:
                    if e["name"] in worn_names:
                        e["_worn"] = True
                have = {e["name"] for e in present["props"]}
                present["props"] = present["props"] + [
                    {**e, "_worn": True}
                    for e in entities["props"]
                    if e["name"] in worn_names
                    and e["name"] not in have
                    and (e.get("_ref") or payload.assignments.get(f"props:{e['name']}"))
                ]
        # 「人物背景图」是整片统一的场景:**每一镜都强制带上它**,不采纳 plan 里给的场景
        # (排镜模型会写「纯白影棚」之类的东西,和用户上传的背景图打架)。
        if bg_scene is not None:
            present["scenes"] = [dict(bg_scene)]
        if always_worn:
            have = {e["name"] for e in present["props"]}
            present["props"] = present["props"] + [
                e for e in always_worn if e["name"] not in have
            ]
        # 按件排镜:打底 / 旧件只补**这一镜她身上真的还穿着**的那几件 —— 带图 + _base 定义,
        # 并点名这一镜被换掉的那件。
        # 前面几镜已经换掉的部位(比如第 3 镜用短裙换掉了阔腿裤),它的旧件**不能再给** ——
        # 否则第 5 镜(短袖衫换长袖衫)会把早已不在她身上的「阔腿裤」也绑上,模型会以为她还穿着
        # 那条裤子、甚至把它画出来(用户报"短袖衫换长袖衫这个多挂了")。
        keep_bases: list[dict] = []  # 这一镜她身上还穿着的旧件(下面按件模式要用)
        # 「扯上身」风格里初始那套在开场镜就全部扯掉了 —— 后面的换装镜**不能再把旧件补进来**
        # (补了模型会以为她还穿着,提示词里出现"要换掉 X"、状态图也跟着错)。
        if base_ents and not _pull and quick_start.is_change_shot(shot.get("action")):
            # 判据用**替换组**而不是部位:外套默认不顶打底的上装(开衫是加穿),
            # 但**扣上的外套**已经是这一镜的新上衣,里面那件要退场;整身一件顶掉两组。
            done_prev = worn_ahead.get(i) or []
            gone: set[str] = set()
            for n in done_prev:
                gone |= _gone_groups(entities["props"], n)
            keep_bases = [
                e
                for e in base_ents
                if quick_start._replace_group(e["name"], e.get("_slot") or "")
                not in gone
            ]
            have = {e["name"] for e in present["props"]}
            present["props"] = present["props"] + [
                e for e in keep_bases if e["name"] not in have
            ]
            # **这一镜要换的那件排到最前**(紧跟人物图),旧件排到最后:参考图里「人物图之后
            # 的第一张服装图」会被模型当成"这一镜要飞进来的那件" —— 排错了它就会把旧件 / 别的件
            # 也复制一份飞进来(实测过的坑)。
            tgt = quick_start.piece_name_of(shot.get("action"))
            props = present["props"]
            if tgt:
                grp = quick_start._part_group(tgt, "")
                first = [
                    e
                    for e in props
                    if not e.get("_base")
                    and quick_start._part_group(e["name"], "") == grp
                ]
                rest = [e for e in props if e not in first and not e.get("_base")]
                # 旧件内部**下装先**(裙子 / 裤子排在上衣前面)—— 这一条是实测过的治法:
                # 旧件栏里"下装在上装前面"时,模型不会把旧裤子复制一份当"要飞进来的那条"飞出来
                # (反过来的顺序实测会把旧睡裤飘一份进来,用户报"飞入又有问题了")。
                bases = sorted(
                    [e for e in props if e.get("_base")],
                    key=lambda e: (
                        0 if quick_start._part_group(e["name"], "") == "bottom" else 1
                    ),
                )
                present["props"] = first + rest + bases
            else:
                present["props"] = [e for e in props if not e.get("_base")] + [
                    e for e in props if e.get("_base")
                ]
            if tgt:
                tgt_gone = _gone_groups(entities["props"], tgt)
                olds = [
                    e["name"]
                    for e in base_ents
                    if quick_start._replace_group(e["name"], e.get("_slot") or "")
                    in tgt_gone
                ]
                act = shot.get("action") or ""
                missing = [n for n in olds if f"「{n}」" not in act]
                if missing:
                    shot["action"] = (
                        act
                        + "**被换掉的是"
                        + "、".join(f"「{n}」" for n in missing)
                        + "(她原来穿着的那件)**。"
                    )
        # 「只有衣服、没有人」的参考图会把镜头劫持成"从衣服起手"(实测:第一帧只有衣服、没有皮肤,
        # 补妆的手永远出不来),所以**衣服**的道具参考图只在「这一镜要把这件衣服当物体处理」时才绑;
        # 其余镜头只绑人物 / 场景参考图 —— 衣服靠人物参考图(她已经穿着)+ 文字描述还原。
        object_shot = is_object_shot(shot.get("action"), payload.float_mode, i)
        # 「已经穿在她身上的那几件」只在**全身展示镜**里绑商品图(它整段就是站着展示,
        # 绑上才能跟前面几镜是同一件)。
        # **换装镜绝对不能绑**(实测对比 p66 与 p81):
        # 只要把已经穿好的衣服的商品图也给它,模型就会把它当成"这一镜要飞进来的衣服",
        # 从第 1 帧开始演"还没穿"—— 长裤开头就没了(而且片段越长越演得完整,7.3 秒那版
        # 前 1 秒还是打底短裤)。p66 只绑本镜头要换的那一件时,开场就已经穿着长裤。
        display_shot = payload.float_mode and quick_start.is_display_shot(
            shot.get("action")
        )
        worn_here: list[str] = []
        if display_shot:
            for nm in worn_ahead.get(i, []):
                ent = next(
                    (
                        e
                        for e in entities["props"]
                        if e["name"] == nm or e["name"].endswith(nm) or nm in e["name"]
                    ),
                    None,
                )
                if ent is not None and all(
                    e["name"] != ent["name"] for e in present["props"]
                ):
                    # _worn 标记:build_prompt 靠它区分"她身上已经穿着的"和"本镜头要换的"
                    present["props"].append({**ent, "_worn": True})
                    worn_here.append(ent["name"])

        # 「打底 / 旧件」的**保留语义**必须分清 —— 全打上 _base 会让 build_prompt 把它们写成
        # `replaced`(「被新件换掉之后就不许再出现在画面里」),而:
        #   · 本镜真要顶掉的那件/那几件(替换组相同 → 见 _displaced_groups)→ 该是 _base(replaced)✓
        #   · 本镜不动的其他打底件(换裤子时那件上衣;加穿外套时里面的上衣)→ 该是 _worn,
        #     写成 replaced 模型会把上衣一起脱掉;
        #   · **展示镜**全场不换(它就是"她这会儿穿着什么"的状态图来源)→ 全部 _worn。
        tgt_name = quick_start.piece_name_of(shot.get("action"))
        tgt_gone = _gone_groups(entities["props"], tgt_name) if tgt_name else set()
        normalized: list[dict] = []
        # 「扯上身」只有一镜、没有"甩旧衣"那段了 —— 初始那套不再需要保留 _base。
        _strip_shot = False
        for ent in present["props"]:
            keep_base = (_strip_shot and bool(ent.get("_base"))) or (
                (not display_shot)
                and bool(tgt_gone)
                and quick_start._replace_group(
                    ent.get("name") or "", ent.get("_slot") or ""
                )
                in tgt_gone
            )
            if ent.get("_base") and not keep_base:
                ent = {k: v for k, v in ent.items() if k != "_base"}
                ent = {**ent, "_worn": True}
            normalized.append(ent)
        present["props"] = normalized

        # 猫 / 狗这类动物**不是人物主体**：从 characters 挪到 props，按文字生成。
        # 留在 characters 里会被套上「人物」那套定义（发型 / 内衣 / 胸围），
        # 实测「侧身高举猫咪」里猫被当成第二个人，白毛还糊到了开衫前襟上。
        _animal_chars = [
            e
            for e in present["characters"]
            if any(k in (e.get("name") or "") for k in ("猫", "狗", "宠物", "兔"))
        ]
        if _animal_chars:
            present["characters"] = [
                e for e in present["characters"] if e not in _animal_chars
            ]
            _have = {e.get("name") for e in present["props"]}
            present["props"] += [
                e for e in _animal_chars if (e.get("name") or "") not in _have
            ]

        # 猫 / 狗这类动物**只在提到它的那一镜出现**：动作里没点名就不带进这一镜。
        # 它们是项目级道具（「侧身高举猫咪」里的猫），不筛的话会混进打底展示 / 换装镜，
        # 提示词里被写成"戴在她身上"，模型就在她怀里画一只猫（用户报「打底全套展示出现一只猫」）。
        _act_animal = shot.get("action") or ""
        present["props"] = [
            e
            for e in present["props"]
            if not (
                any(k in (e.get("name") or "") for k in ("猫", "狗", "宠物", "兔"))
                and not any(k in _act_animal for k in ("猫", "狗", "宠物", "兔"))
            )
        ]

        # 化妆师 / 摄影师只是入画的一只手 / 手臂，**不是镜头主体**：
        # 不要给它们完整人物定义（外观、内衣、衣着那一整套），否则模型会照着
        # 画第二个人的衣服 —— 实测化妆师的灰袖子 / 围巾被画到了模特脖子上。
        present["characters"] = [
            e
            for e in present["characters"]
            if not any(k in (e.get("name") or "") for k in ("化妆师", "摄影师"))
        ]

        # **包 / 手袋这类「拿在手上的件」：动作里没点名就不带进这一镜**。
        # 放在这里（补件 / 展示镜 worn_here 都做完之后）才能拦住按件链补进来的包。
        # 用户要求：动作里没写到的包不要自动挂上 —— 自定义分镜（如「整套展示」）也算。
        if payload.float_mode:
            _act_bag = shot.get("action") or ""
            # 只认「包 / 手袋 / 手提袋」这类**包本身**的字样。
            # 不能用「手拿 / 手提 / 挎 / 拎 / 袋」这种泛词 —— 「一只手拿着咖啡杯」「插进裤子口袋」
            # 都会命中，结果包又被带进来了（用户报「端咖啡杯不应该有包」）。
            _bag_mentioned = (
                "包" in _act_bag or "手袋" in _act_bag or "手提袋" in _act_bag
            )
            if not _bag_mentioned:
                present["props"] = [
                    e
                    for e in present["props"]
                    if not quick_start.is_carried_piece(e.get("name") or "")
                ]

        refs: list[str] = []
        picture_of: dict[str, list[int]] = {}
        for kind in KINDS:
            for ent in present[kind]:
                # 衣服的商品图在**物体镜(有衣服飞入的换装镜)和展示镜**里都绑 ——
                # 旧件 / 打底也一样要绑:
                #   · 不绑的话,模型手上根本没有那件旧衣服,开场直接画成内衣、或者自己编一件
                #     (实测量过:换装镜开场她只剩内衣);
                #   · 会不会被当成"这一镜要飞进来的那件"?—— 不会:present 里已经把它们排到
                #     **最后**(要换的那件紧跟人物图),而且 build_prompt 会给旧件写
                #     "它不参与飞行 —— 一直好好穿在她身上",两处一起把它压住。
                # 其余镜头(细节镜等)只绑**小件配饰**:不绑模型手上没样本,会干脆不画。
                if kind == "props" and not (object_shot or display_shot):
                    nm = ent.get("name") or ""
                    # 通用分镜里「她这会儿已经穿好的那几件」(上面按 _worn 补进来的)也要绑图 ——
                    # 手上没样本,模型会把那套衣服重新想象一遍,出来和前面几镜不是同一套。
                    if not (
                        quick_start.is_carried_piece(nm)
                        or is_worn_accessory(nm)
                        or (general_piece_shot and ent.get("_worn"))
                    ):
                        continue
                pics: list[int] = []
                # 实体自己带的图优先:打底 / 常驻配饰是**页面上直接传给我们的**(base_items /
                # accessories 里就有 ref),不该指望 assignments —— assignments 是前端按
                # plan 的实体名建的表,plan 一旦把那件改个名字(「长裤」→「套装-阔腿裤」)就永远
                # 对不上,打底那两件于是一张图都没有(实测 167:s01 的短袖衫 / 长裤全是"按文字描述")。
                own = [ent.get("_ref")] if ent.get("_ref") else []
                cand = list(payload.assignments.get(f"{kind}:{ent['name']}") or [])
                if kind == "characters" and not cand:
                    # **人物名对不上就按「有没有绑图」兜底取**:页面绑图时写死的是
                    # `characters:她`,而分析剧本给的人物名可能是「模特」「女主」——
                    # 名字一错,人物图就绑不进来,refs 里一张人物图都没有(用户报"人物图怎么没有了"),
                    # 提示词里给模特的那句「见 <Picture 1>」还会指到第一件衣服上。
                    # 人物是第一个进 refs 的(KINDS 首项),所以它在 <Picture 1> 这个位置上。
                    cand = next(
                        (
                            list(v)
                            for k2, v in (payload.assignments or {}).items()
                            if k2.startswith("characters:") and v
                        ),
                        [],
                    )
                for ref in own + cand:
                    ref = (ref or "").strip()
                    if not ref:
                        continue
                    if ref not in refs:
                        refs.append(ref)  # 同一张图被多个实体共用时只占一个槽位
                    n = refs.index(ref) + 1
                    if n not in pics:
                        pics.append(n)
                if pics:
                    picture_of[f"{kind}:{ent['name']}"] = pics
                # 没指定 -> 不放进 picture_of,提示词里只写文字描述,交给模型自己生成

        # 本镜的「实拍穿着参考」(风格参考图):不绑定任何实体 —— 直接挂在参考图列表**最后**,
        # 再把它的图号交给 build_prompt,补一段"只参考它身上这套衣服的款式 / 颜色 / 穿法"。
        wear_ref_pics: list[int] = []
        for ref in shot.get("wear_refs") or []:
            ref = (ref or "").strip()
            if not ref:
                continue
            if ref not in refs:
                refs.append(ref)
            n = refs.index(ref) + 1
            if n not in wear_ref_pics:
                wear_ref_pics.append(n)

        opts: dict = {"float": 1} if payload.float_mode else {}
        # 长于默认单次生成帧数(124)的通用分镜(比如库里设了 10 秒),
        # 生成帧数要抬到 H3 的 17k+5,否则拍到 5 秒就停、后面冻结。
        if frames[i - 1] > settings.gen_length:
            opts["gen_length"] = min(
                362, 5 + 17 * max(2, -(-(frames[i - 1] - 5) // 17))
            )
        if bg_scene is not None:
            # 有背景图:①pipeline 见到 bg 就跳过「近白背景量人物框」那套归一(背景不是白的,
            # 量出来会是整幅画);②背景参考图不做「缩小留白」,否则模型会抄成两层背景。
            opts["bg"] = 1
            opts["bg_ref"] = bg_ref
        timeline = ""
        if payload.float_mode and quick_start.is_change_shot(shot.get("action")):
            # 生成帧数必须和这一镜的时长一致 —— 否则拍到一半就停了,时间轴写得再长也没用
            opts["gen_length"] = frames[i - 1]
            # 只有「一镜穿完整套」(≥2 件)才拼时间轴;一镜一件的「按件排镜」保持原样,
            # 它自己的动作文案里已经写好了节奏。
            if len(float_events.get(i, [])) >= 2:
                timeline = quick_start.float_timeline_text(
                    float_events[i], frames[i - 1], settings.fps
                )
        if payload.float_mode and i in state_ordinal:
            # 状态展示镜:拍完把末帧落成状态图 S(j)。
            #
            # **首帧锚 = 上一张状态图**(链头 state 0 没有上一张,不给)。状态图之间是同一台"机位"
            # 下来的,锚住它就能把取景 / 人物大小 / 背景钉住 —— 实测锚上之后四张状态图的占高
            # 从 0.902~0.935 收到 0.897~0.905。
            #
            # 以前这里刻意**不给锚**:当时担心"给锚模型就直接延续上一个状态、链断在这"。现在这套
            # 强措辞(点名新旧件 + 各自的外观描述 + "别配成一套花色")能压住锚了 —— **但只对"换掉"型**:
            # 它点名了旧件"必须彻底没有",模型知道要从锚图那身变过去,末帧照样是换好之后的样子。
            #
            # **"加穿"型(外套叠穿:锚图那身就是"原来那几件")不能给锚** —— 模型会把锚图直接照抄,
            # 新加的那一层整场不出现(实测项目 177 的 s06:两版措辞都被无视,末帧和上一张状态图
            # 一模一样、开衫没了)。它宁可不加,也不肯偏离锚图。
            j = state_ordinal[i]
            # 生成帧数:H3 习惯用 17k+5(39/56/73…),取不小于这一镜时长的最小值,
            # 免得给它一个奇怪的帧数(实测 48 也能跑,但按它的习惯来更稳)。
            want = max(int(frames[i - 1]), 39)
            opts["gen_length"] = 5 + 17 * max(2, -(-(want - 5) // 17))
            opts["state_out"] = _state_path(j)
            if j == 0:
                # 链头(打底那套展示,它没有首帧锚):状态图取**第 1 帧** —— 展示镜都会在 1 秒里
                # 自己漂一点,而链头没有锚,第 1 帧才是"模型照着人物图 + 文字画的第一版"。
                # 其余展示镜不能这么干:它们的第 1 帧被 anchor0 钉着 = 上一张状态图,存下来链会卡住。
                # 「扯上身」的链头就是"完整穿着初始那套"的静态镜,取第 1 帧正合适。
                opts["state_first"] = 1
            # 「替换型」展示镜给首帧锚(锚图里已经穿着上一件,姿态能接上)。
            # 「加穿型」(开衫 / 外套 / 配饰)**不能给锚**:锚图里根本没有这件新东西,
            # 模型会整张照抄、新件从头到尾不出现(2026-10-19 实测:加锚后开衫整场没了)。
            # 加穿型的做法改成「把上一镜最后一帧当参考图引进来 + 提示词点名姿势一致」。
            if j >= 1 and (piece_swap.get(i) or ([], []))[0]:
                opts["anchor0"] = _state_path(j - 1)
            # 加穿型:把上一镜尾帧挂进 refs,记录图号给提示词用。
            # **不要判断文件是否存在**:状态图是渲染时由前一镜落盘的,建项目时它必然还不存在 ——
            # 以前这里用 os.path.exists 兜底,结果新项目里加穿型(开衫 / 包)的展示镜
            # 永远拿不到上一镜尾帧、提示词里也没有「姿势和它一致」那句(用户 2026-10-20 报)。
            # 它挂在 refs 最后一位,真缺文件时 resolve_refs 会丢掉,不影响前面的编号。
            if j >= 1 and not (piece_swap.get(i) or ([], []))[0]:
                _prev_ref = f"file:{_state_path(j - 1)}"
                if _prev_ref not in refs:
                    refs.append(_prev_ref)
                prev_pic_map[i] = refs.index(_prev_ref) + 1
        # 换装镜**首帧锚 = 「换之前那身」的状态图 S(k-1)、尾帧锚 = 「这一件换好之后」的状态图 S(k)**
        # —— 与已验证的项目 176 / 155 配方一致(见下面按件排镜那一支)。
        #   · 不给首帧锚:模型从零起手,会把「待替换的旧件」演成"还没穿" —— 实测项目 184 的 s03
        #     开局她上半身直接是裸的(睡裤是 worn 所以还留着,睡衣上衣是 replaced 就没了);
        #   · 不给尾帧锚:新件没有"该落到哪儿 / 多大"的落点约束,会朝镜头扑过来、先变大再缩小
        #     (正是提示词里明令禁止的那两条)。项目 176 钉上之后衣服就是躯干大小、干净贴上去。
        # 状态图链由**展示镜**产出(见上面的 state_out / 替换型展示镜的 anchor0)。
        # 「按件排镜」走**专用生成器**(逐字对齐项目 155 的措辞,见 piece_prompt.py);
        # 其余玩法照旧用通用 build_prompt。
        # 件链上的镜 = 展示镜(state_ordinal)+ 换装镜(change_ordinal)。不在链上的就是
        # 从镜头库勾进来的**通用分镜** —— 它们不走按件生成器(理由见下面那段)。
        is_piece_chain = (i in state_ordinal) or (i in change_ordinal)
        if payload.float_mode and payload.float_variant == "piece":
            # 通用分镜（从镜头库勾进来的细节镜）**不是换装链的一部分** —— 它们各自独立，
            # 不能拿上一镜的末帧当首帧锚：上一镜的景别 / 背景 / 道具会被带进来，
            # 实测「整套展示」接在近景后面就变成"从脸慢慢拉远到全身"的变焦。
            # 打个标记，pipeline 见到就不做「上一镜末帧」兜底。
            if not is_piece_chain:
                opts["universal"] = 1
            # 「按件排镜」这条链**关掉成片后期那层「从上一镜刷过去」**(见 pipeline._wants_wipe):
            # 它假设前后两镜的末帧/首帧几乎一样,而按件链每一剪都刚好差一件衣服 ——
            # 叠出来的就是两个人重合的重影(实测项目 177 的 s02→s03 接缝)。
            opts["no_wipe"] = 1
            if _pull:
                # 「扯上身」必须正向播放(先穿着 → 甩掉 → 再穿新):pipeline 里有个
                # 「先穿好再让衣服飞走 → 倒放」的拍法,它会把这一镜倒过来 —— 结果就是
                # 一开场只剩内搭。显式关掉。
                opts["no_reverse"] = 1
            # 换装镜:补回首尾状态图硬锚(首 = S(k-1)「换之前那身」,尾 = S(k)「这件换好之后」)。
            # 这两张图都是紧挨在它前面的展示镜产出的(见上面的 state_out),此时一定已经落盘;
            # 万一没落盘,pipeline 会自己忽略掉不存在的锚图(见 pipeline._generate_shot)。
            chg_k = change_ordinal.get(i)
            if chg_k:
                opts["anchor0"] = _state_path(chg_k - 1)
                opts["anchor_last"] = _state_path(chg_k)
                if _pull:
                    # 「扯上身」没有"换好之后的展示镜"了 —— 链尾状态图(S1)改由这一镜自己的
                    # 末帧产出,后面的通用分镜才有"最后穿好的样子"可以当穿搭参考。
                    opts["state_out"] = _state_path(chg_k)
                # 不再钉第 8 帧中间锚(2026-10-19):首尾两张状态图本来就几乎同一姿势,
                # 中间再钉一张「换之前那身」= 整段被三张同姿势的图夹死,手臂 / 手指一动不动
                # (用户反复报「手还是没有动、人很僵硬」)。去掉后中段才有让模型做小动作的空间。
            # 通用分镜(件链之外,半身补妆 / 化妆师补右脸…):**不钉换装链的状态图** ——
            # 状态图是展示镜视频抽出来的帧(448×800),拿它当首帧既糊、又让每个细节镜
            # 都从同一张定格正面站姿开始(用户实测「效果不好」)。衣服一致性交给 refs
            # 里的商品图 + 「已穿着」话术(见上面 general_piece_shot 的补件),人物图
            # 用户自己维护。
            # 展示镜**只用来产出状态图**(它的末帧就是下一个换装镜的头/尾帧锚),不进成片 ——
            # 成片只由那几段"换一件衣服"(约 5 秒)的换装镜组成(见 pipeline.run_job 的 state_only)。
            if i in state_ordinal:
                opts["state_only"] = 1

        # 「按件排镜」走**专用生成器**(逐字对齐项目 155 的措辞,见 piece_prompt.py);
        # 其余玩法照旧用通用 build_prompt。
        # **但只有件链上的镜才交给它** —— 它认的角色只有「新件 / 旧件 / 不变件 / 配饰」四种,
        # 通篇按「全身站定、从头到脚完整入画」写。勾进来的**通用分镜**(半身补妆 / 化妆师补右脸…)
        # 不在件链上:它没有新件,交进去会被当成"展示镜" —— 道具被写成"她穿着这件粉扑",
        # **景别还被按成全身**(用户实测:「半身补妆」那一镜里冒出"固定全身机位…从头到脚完整入画")。
        # 这类镜一律回通用 build_prompt,景别严格按分镜自己的动作文本走 —— 动作文本里本来
        # 就写着"半身景别(头部和脸必须完整入画…)",不该被按件模板的全身取景覆盖掉。
        if payload.float_mode and payload.float_variant == "piece" and is_piece_chain:
            from ..piece_prompt import build_piece_prompt

            def _pics_of(nm: str, kind: str = "props") -> list[int]:
                return list(picture_of.get(f"{kind}:{nm}") or [])

            def _pp(e: dict, role: str) -> dict:
                # 把**识别结果**一起带下去:槽位(读图给的部位)+ 长度 + 描述 ——
                # 提示词生成器靠它们判"这件是什么、多长"(雪服 / 汉服 / 内搭 这种没见过的名字才不会判错)
                return {
                    "name": e["name"],
                    "pics": _pics_of(e["name"]),
                    "role": role,
                    "slot": e.get("_slot") or "",
                    "len": e.get("_len") or "",
                    # 开合(扣子 / 拉链合不合上):件级覆盖 > 全局默认,见 garment_kind.fasten_clause
                    "close": e.get("_close") or "",
                    "desc": e.get("desc") or "",
                }

            # 这一镜要换的那一件:优先用**状态链已经认出来的**那件(剧本里的件名和 plan 里的
            # 实体名经常对不上 —— 剧本用素材名「长袖衫」、plan 用拆件名「套装-蕾丝一字肩长袖上衣」),
            # 再退回按名字 / 按部位匹配。匹配不上就会退化成「展示镜」,
            # 这一镜不换任何东西、状态图链断在这里(实测 s05)。
            tgt_e = None
            if tgt_name:
                want_nm = nth_piece.get(change_ordinal.get(i, 0), "")
                if want_nm:
                    tgt_e = next(
                        (e for e in present["props"] if e["name"] == want_nm), None
                    )
                if tgt_e is None:
                    tgt_e = next(
                        (
                            e
                            for e in present["props"]
                            if (
                                e["name"] == tgt_name
                                or tgt_name in e["name"]
                                or e["name"] in tgt_name
                            )
                        ),
                        None,
                    )
                if tgt_e is None:
                    grp = quick_start._part_group(tgt_name, "")
                    if grp and grp != "other":
                        tgt_e = next(
                            (
                                e
                                for e in present["props"]
                                if not e.get("_base")
                                and quick_start._part_group(
                                    e["name"], e.get("_slot") or ""
                                )
                                == grp
                            ),
                            None,
                        )
            piece_props: list[dict] = []
            # 「扯上身」那一镜是**一次穿整套** → 这一镜要穿上的每一件都标成 new
            # (不是 new 的话会被当成"已经穿着、整镜保持原样",衣服就永远穿不上去)。
            # **一直戴着 / 穿着的配件不参与「落下来」**:
            #   · 墨镜 / 眼镜 / 帽子 —— 用户 2026-10-20「墨镜默认戴着」;
            #   · 鞋 —— 用户 2026-10-20「鞋子也默认穿在脚上」。
            # 它们留在 present 里、走下面的 accessory 分支,写成「第一帧就已经…」。
            _pull_new = (
                set(_pull_names) - {nm for nm in _pull_names if is_worn_accessory(nm)}
                if (_pull and change_ordinal.get(i, 0) >= 1)
                else set()
            )
            if _pull_new:
                for e in present["props"]:
                    if e["name"] in _pull_new:
                        piece_props.append(_pp(e, "new"))
            elif tgt_e is not None:
                piece_props.append(_pp(tgt_e, "new"))
            for e in present["props"]:
                if (_pull_new and e["name"] in _pull_new) or (
                    not _pull_new and tgt_e is not None and e["name"] == tgt_e["name"]
                ):
                    continue
                # **旧件先判**:到这一步还带 `_base` 的,只剩「本镜确实要被顶掉」的那几件
                # (上面 normalized 已经把不换的打底转成 _worn)。鞋也在 is_worn_accessory 里,
                # 以前这一支会把打底的旧鞋判成「一直戴着的小配件」,于是 build_piece_prompt
                # 认为没有旧件可配 → 换鞋被写成"再加一双"(用户 2026-10-19)。
                if e.get("_base"):
                    role = "old"
                elif is_worn_accessory(e["name"]):
                    role = "accessory"
                else:
                    role = "worn"  # _worn:本镜不动的、以及前面几镜已经换上的
                piece_props.append(_pp(e, role))
            for e in present["scenes"]:
                piece_props.append(
                    {
                        "name": e["name"],
                        "pics": _pics_of(e["name"], "scenes"),
                        "desc": e.get("desc") or "",
                        "role": "scene",
                    }
                )
            person_desc = ""
            for e in present.get("characters") or []:
                person_desc = (e.get("desc") or "") + " " + person_desc
            # 这一镜是第几件换上的(展示镜没有这个号 → 0,不影响,它不飞衣服)
            _k_chg = change_ordinal.get(i) or 0
            prompt_text = build_piece_prompt(
                title=_shot_title(shot.get("title") or f"镜头 {i}"),
                index=i,
                total=count,
                props=piece_props,
                wear_ref_pics=wear_ref_pics,
                person_desc=person_desc,
                swap=piece_swap.get(i),
                # 第一件换上时:允许双臂从身侧向外张开一次(展示镜要把这个姿势留在末帧,
                # 换装镜再从头演一遍张开);后面每一镜只保持这个姿势。
                first_change=(
                    (state_ordinal.get(i) == 1) or (change_ordinal.get(i) == 1)
                ),
                # 只有「最后一件」的换装镜和第一件一样保留「风把头发吹起来」那段;
                # 中段写了风,模型会连头一起动(低头看衣服 / 仰头)—— 用户 2026-10-20 实测。
                # **最后一镜的展示镜也算 last_change**:它产出的状态图 S(N) 是最后一张锚图,
                # 末帧手臂要垂下,换装镜的尾帧锚才和它一致(否则锚图张手、提示词让她放下,互相打架)。
                last_change=bool(
                    (_k_chg and _k_chg == final_state_idx)
                    or (final_state_idx and state_ordinal.get(i) == final_state_idx)
                ),
                prev_pic=prev_pic_map.get(i, 0),
                # 新件左右交替:第 1 件右上、第 2 件左上、第 3 件右上…(下装同理:右下 / 左下)。
                entry_side=("left" if _k_chg and _k_chg % 2 == 0 else "right"),
                # 换装风格:throw = 画外扔过来飞入(默认);pull = 整套悬空、她扯上身。
                style=("pull" if _pull else "throw"),
                # 「扯上身」只有一镜(整套一次吸上身),没有"甩旧衣"那一段了。
                strip_base=False,
            )
        elif payload.float_mode and payload.float_variant == "piece":
            # 件链之外的**通用分镜 / 自定义分镜**:用紧凑版生成器 —— 只补场景 / 人物 /
            # 身上穿着 / 道具,再接动作**原文**(景别、机位、俯拍这些都写在原文里),
            # 不再套"固定全身机位 + 取景守则 + 黑名单"那套模板。
            prompt_text = build_universal_prompt(
                shot, present, picture_of, i, count, wear_ref_pics=wear_ref_pics
            )
        else:
            prompt_text = build_prompt(
                shot,
                present,
                picture_of,
                i,
                count,
                float_mode=payload.float_mode,
                float_variant=payload.float_variant,
                float_base_outfit=payload.float_base_outfit
                or quick_start.DEFAULT_BASE_OUTFIT,
                garment_desc=payload.garment_desc,
                float_timeline=timeline,
                wear_ref_pics=wear_ref_pics,
                no_ndm=_no_ndm,
            )
        # 按件排镜的提示词在 build_piece_prompt / build_universal_prompt 里**已经写成
        # 最终形态**(线性叙事、每个约束只出现一次),不再过精简器 —— 精简器会把姿态句提到
        # 最前面、还会按"前 3 句 + 关键词"丢句子(用户 2026-10-20:「提示写得非常乱」)。
        # 其余玩法(单镜整套)照旧精简。
        if payload.float_mode and payload.float_variant == "piece":
            prompt_final = prompt_text
        else:
            prompt_final = concise.rule_condense(prompt_text)
        # 换装链之后(通用分镜):把「最后换装完成后的样子」当穿搭参考图引进来 ——
        # 用户 2026-10-19 要求:链后每个视频都要带这张图,只参考衣服穿戴,其他不参考。
        if (
            payload.float_mode
            and payload.float_variant == "piece"
            and last_change_idx
            and i > last_change_idx
            and i not in state_ordinal
            and i not in change_ordinal
        ):
            # 注意:**不检查文件是否存在** —— 状态图要等链渲染完才落盘,建项目时还没有;
            # 渲染时链在前、通用分镜在后,轮到这些镜时文件已经生成。
            _look = _state_path(final_state_idx)
            _look_ref = f"file:{_look}"
            if _look_ref not in refs:
                refs.append(_look_ref)
            _look_n = refs.index(_look_ref) + 1
            opts["look_ref"] = _look_n
            opts["look_path"] = _look
            prompt_final += (
                f"\n\n<Picture {_look_n}> 是她全部换装完成后的样子 —— **只参考她身上衣服和鞋子的"
                f"款式、颜色和穿法**:这一镜她的穿搭必须和 <Picture {_look_n}> 一模一样"
                "(包括脚上穿的那双鞋);"
                "人物的长相 / 发型 / 姿势 / 背景都不参考(长相仍以人物参考图为准)。"
            )
        # 人物头像:整片每一镜都挂(用户 2026-10-22) —— 只锁头 / 脸,构图按本镜说明。
        _head = (payload.head_ref or "").strip()
        if _head and _head not in refs:
            refs.append(_head)
            _head_n = refs.index(_head) + 1
            opts["head_ref"] = _head_n
            prompt_final = insert_into_person(prompt_final, head_def_text(_head_n))
        db.add(
            ProjectShot(
                project_id=project.id,
                idx=i,
                key=f"s{i:02d}",
                title=_shot_title(shot.get("title") or f"镜头 {i}"),
                mode=payload.mode,
                duration_frames=frames[i - 1],
                prompt=prompt_final,
                refs=refs,
                # float 标记成片后期:悬浮变装要关掉推镜和加饱和(和这个风格正好相反)
                options=opts,
                enabled=True,
                status=ST_QUEUED,
            )
        )

    # ---- 静态风格(2026-10-22):覆写主链分镜 —— 不走视频,改成 FLUX3 imgedit 一件一镜 ----
    # 上面那套是视频流程(build_piece_prompt / anchor / 状态图链),对静态风格都不适用。
    # 静态风格:每镜就是「人物图 + 当前件图 → 一张 PNG」,所以:
    #   · prompt 精简成 imgedit 指令(「以图1为底,让她穿上图2的 X」)
    #   · refs 留空(pipeline 用 options 里的 character_ref / garment_ref 自己解析绝对路径)
    #   · options 只留 static_image / character_ref / garment_ref / garment_name / prev_static
    #   · 件链外的通用分镜(非换装镜)不动 —— 它们后面在编辑器里追加,仍走视频流程
    _is_static_style = (
        payload.float_mode
        and payload.float_variant == "piece"
        and (payload.piece_style or "").strip().lower() == "static"
    )
    if _is_static_style:
        # 人物参考图:静态换装必须有(它就是第 1 镜的底图)
        char_ref = ""
        for _k, _v in (payload.assignments or {}).items():
            if str(_k).startswith("characters:"):
                char_ref = next(
                    (str(r).strip() for r in (_v or []) if str(r).strip()), ""
                )
                if char_ref:
                    break
        if not char_ref:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                "静态换装需要给人物绑定一张参考图(作为第 1 镜的底图)",
            )
        # 「件名 → 服装参考图」表:解析 assignments 的 props:* 绑定
        prop_ref: dict[str, str] = {}
        for _k, _v in (payload.assignments or {}).items():
            if not str(_k).startswith("props:"):
                continue
            _nm = str(_k).split(":", 1)[1].strip()
            _r = next((str(x).strip() for x in (_v or []) if str(x).strip()), "")
            # file: 前缀的是状态图 / 上一镜尾帧,不是素材图
            if _nm and _r and not _r.startswith("file:"):
                prop_ref[_nm] = _r

        # 件序:页面传的件名(贴身先穿、外套最后);没传就按「第几件 → 实体名」的顺序
        _names = [str(n).strip() for n in (payload.piece_names or []) if str(n).strip()]
        if not _names:
            _names = [nm for _, nm in sorted(nth_piece.items())]
        _names.sort(key=quick_start._wear_layer)
        # 常驻配饰(墨镜 / 拖鞋 / 项链…)不单独排镜 —— 它们从第 1 镜起就一直戴在身上,
        # 后面几镜靠上一镜 PNG 一路延续;排成换装镜会变成「这一镜才戴上」,不是用户要的。
        # 用页面「常驻配饰」栏单独传的 accessories 判定,不能靠名字猜:
        # 「乐福鞋」这类是正式件,名字里带「鞋」也会被 is_worn_accessory 误伤。
        _acc_names = [
            str(getattr(a, "name", "") or "").strip() for a in (payload.accessories or [])
        ]
        _acc_names = [n for n in _acc_names if n]
        _names = [n for n in _names if n not in _acc_names]
        if not _names:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                "静态换装至少要有一件可换的衣服(常驻配饰不排镜)",
            )

        # 静态风格「一件一镜」:件链上的视频镜(展示镜 + 飞入/扯上身换装镜)整批不要,重建 N 个静态图镜。
        # 但从镜头库勾进来的**通用分镜 / 自定义分镜**必须留着 —— 它们排在 N 张静态图后面,
        # 各自独立、不拿上一镜当首帧,靠 options.universal 走 pipeline._style_ref_for
        # 拿「最后一张静态图」当穿着样式参照(用户要的就是这个)。
        from sqlalchemy import select as _sa_select

        # 先把上面视频流程 db.add() 的那些镜真正写进库里 —— SessionLocal 是 autoflush=False,
        # 不 flush 的话下面的 SELECT 查不到 pending 对象,删不掉,提交后两套镜并存(同 idx 两条)。
        db.flush()
        _old_shots = (
            db.execute(
                _sa_select(ProjectShot)
                .where(ProjectShot.project_id == project.id)
            )
            .scalars()
            .all()
        )
        _keep_detail: list[ProjectShot] = []
        for _s in _old_shots:
            # 静态剧本文本(`【件名·静态换装】…`)也会被解析成通用镜,跟下面重建的静态图镜重名重复;
            # 标题以「·静态换装」结尾的一律丢掉,真正的静态图镜由下面重新创建
            if (_s.options or {}).get("universal") and not str(_s.title or "").endswith("·静态换装"):
                _keep_detail.append(_s)
            else:
                db.delete(_s)
        db.flush()

        # 件名 / 描述 / 开合 / 长短(整套镜和后面的分镜共用)
        def _ent_of(nm: str) -> dict:
            """按件名找回实体(打底件优先)—— 取它的部位 / 开合 / 描述。"""
            return (
                next((e for e in base_ents if e.get("name") == nm), None)
                or next((e for e in entities["props"] if e.get("name") == nm), None)
                or {}
            )

        _names_refs = [
            (
                nm,
                prop_ref.get(nm, ""),
                str(_ent_of(nm).get("_slot") or ""),
                # 「开合」:页面件列表里选的 > 素材库读图结果 > 全局默认(见 garment_kind)
                str(_ent_of(nm).get("_close") or ""),
                # 描述 / 长短:写进【道具定义】那一条(用户 2026-10-22)
                str(_ent_of(nm).get("desc") or ""),
                str(_ent_of(nm).get("_len") or ""),
            )
            for nm in _names
        ]

        # —— 整套一镜(视频) ——
        # 用户 2026-10-23:auto 选「普通(静态图)」也只要**一镜把整套依次穿上**,
        # 不再按件生成 N 条静态换装镜(静态链在这套素材上容易跑偏)。
        _garment_refs: list[str] = []
        for _i, (_nm, _r, _slot, _close, _desc, _len) in enumerate(_names_refs, start=1):
            _garment_ref = _r
            if not _garment_ref:
                _ent = next((e for e in entities["props"] if e.get("name") == _nm), None)
                _garment_ref = (_ent.get("_ref") or "") if _ent else ""
            if not _garment_ref:
                raise HTTPException(
                    status.HTTP_400_BAD_REQUEST,
                    f"整套换装:第 {_i} 件「{_nm}」找不到参考图",
                )
            _garment_refs.append(_garment_ref)

        # 常驻配饰:整套镜第一帧就戴着(换装图链以前不带配饰,这里一镜到底所以要带)
        _acc_desc = {
            str(getattr(a, "name", "") or "").strip():
                str(getattr(a, "desc", "") or "").strip()
            for a in (payload.accessories or [])
        }
        _acc_items: list[tuple[str, str, str]] = []
        for _an in _acc_names:
            # 包只在动作里提到时才进镜头(用户 2026-10-23)——
            # 整套换装镜没有动作文案,把包挂在第一帧会占住她的手,开衫就更穿不进去
            if is_carried_prop(_an):
                continue
            _r = prop_ref.get(_an) or str(_ent_of(_an).get("_ref") or "")
            if _r:
                _acc_items.append(
                    (_an, _r, _acc_desc.get(_an) or str(_ent_of(_an).get("desc") or ""))
                )

        _detail_count = len(_keep_detail)
        _total = 1 + _detail_count
        _head_ref = (payload.head_ref or "").strip()
        _shot_refs = [char_ref, *_garment_refs, *[r for _n, r, _d in _acc_items]]
        if _head_ref:
            # 头像**直接挂进参考图**(用户 2026-10-23):编辑器底部参考图面板要能看见它,
            # 也方便用 @ 指代 —— 以前只靠 compose_prompt 运行期偷偷追加,面板上是空的。
            _shot_refs.append(_head_ref)
        _head_pic = len(_shot_refs)  # 头像在 refs 里就是最后一张
        # 第 4 栏「风格参考图」(style_items):整套镜也要带上,只借它的穿法
        _wear_pics: list[int] = []
        for _w in [
            str(getattr(it, "ref", "") or "").strip()
            for it in (payload.style_items or [])
        ]:
            if not _w:
                continue
            _shot_refs.append(_w)
            _wear_pics.append(len(_shot_refs))
        db.add(
            ProjectShot(
                project_id=project.id,
                idx=1,
                key="s01",
                title="整套换装·直接穿上",
                mode=MODE_REF,
                duration_frames=3 * settings.fps,
                prompt=_whole_look_shot_prompt(
                    names=_names_refs,
                    accs=_acc_items,
                    head_pic=_head_pic,
                    wear_pics=_wear_pics,
                    total=_total,
                ),
                refs=_shot_refs,
                options={
                    "float": 1,
                    "gen_length": 3 * settings.fps,
                    "no_wipe": 1,
                    # 让 _chain_tail_frame 认这一镜是「换装链」——后面的普通 / 自定义分镜
                    # 会把它的**最后一帧**当穿着方式参照图。
                    "whole_look": 1,
                },
                enabled=True,
                status=ST_QUEUED,
            )
        )

        # 细节镜(通用 / 自定义分镜)顺延到整套镜后面
        _detail_shots = sorted(_keep_detail, key=lambda s: s.idx)
        for _j, _detail in enumerate(_detail_shots, start=2):
            _detail.idx = _j
            _detail.key = f"s{_j:02d}"
            _detail.status = ST_DRAFT
        count = _total

        # 项目改成「视频」:整套镜是视频,后面分镜也是视频(不再走静态图链)
        project.piece_style = "throw"

        # 这些细节镜的提示词要跟「扔过来飞入」风格同构(用户 2026-10-22):
        # 【场景】【人物】【身上穿着】【配饰】【道具】【画面 / 动作】【统一约束】,
        # 末尾再附一张「整套换好之后」的图当穿搭参照 —— 视频流程那套按状态链算
        # 「这一镜穿着什么」的逻辑在静态风格里算不出来(没有件链状态镜)。
        _cname, _cdesc = "", ""
        for _k, _v in (payload.assignments or {}).items():
            if str(_k).startswith("characters:") and (_v or []):
                _cname = str(_k).split(":", 1)[1].strip()
                break
        if _cname:
            _cdesc = _clean_static_char_desc(str(_ent_of(_cname).get("desc") or ""))
        _rebuild_static_detail_shots(
            _detail_shots,
            names=_names_refs,
            char_name=_cname,
            char_desc=_cdesc,
            accs=_acc_items,
            total=count,
            head_ref=(payload.head_ref or "").strip(),
            # 人物全身图:和整套镜用的是同一张(用户 2026-10-22)—— 这些分镜也要认身材
            char_ref=char_ref,
            # 整套镜(视频)的**最后一帧**:由 pipeline 的 _chain_tail_frame 自动抽到 refs_style/,
            # 这里先按同一个路径挂进去,生成完就能对上号。
            style_ref="file:" + (
                STORAGE_DIR / "refs_style" / f"p{project.id}_s01_style.png"
            ).as_posix(),
            has_bg=bool(project.bg_ref),
        )

    db.commit()
    db.refresh(project)
    return ScriptProjectOut(
        project=project_out(project),
        engine=payload.plan.engine,
        engine_detail=detail,
        shot_count=count,
    )


# 静态风格剧本行会把「脸 / 发型 / …全程不变」「穿着整套服装」写进人物 desc,
# 这些是镜头级的动作约束,不该留在【人物】里(用户 2026-10-22)。
_STATIC_DESC_NOISE = (
    "脸 / 发型 / 身材 / 姿势 / 背景全程不变，",
    "脸/发型/身材/姿势/背景全程不变，",
    "脸 / 发型 / 身材 / 姿势 / 背景全程不变,",
    "脸/发型/身材/姿势/背景全程不变,",
    "脸 / 发型 / 身材 / 姿势 / 背景全程不变",
    "脸/发型/身材/姿势/背景全程不变",
    "穿着整套服装",
    "站在纯白背景前、全身正对镜头",
    "全身正对镜头",
)


def _clean_static_char_desc(desc: str) -> str:
    out = str(desc or "")
    for _p in _STATIC_DESC_NOISE:
        out = out.replace(_p, "")
    # 静态剧本常把「她穿着 A、B、C」写进人物描述里 —— 衣服由【身上穿着】负责,这里清掉
    out = re.sub(r"[，,]\s*穿着[^;；]*$", "", out)
    return out.strip(" ,，;；、")


def _whole_look_shot_prompt(
    *,
    names: list[tuple[str, str, str, str, str, str]],
    accs: list[tuple[str, str, str]],
    head_pic: int,
    wear_pics: list[int] | None = None,
    total: int,
) -> str:
    """「整套换装·直接穿上」一镜的提示词(视频)。

    auto 页选「普通(静态图)」以前会按件生成 N 条静态换装镜;用户 2026-10-23 要求
    跟项目 test2323333 一样:**一镜把整套依次穿上**,静态图链全部去掉。
    参考图顺序 = 图1 人物底图 → 图2..N 各件衣服 → 配饰;
    头像由 compose_prompt 追加在最后(head_pic = 参考图总数 + 1)。
    """
    clothes = [n for n in names if n[0] and n[1]]
    props: list[str] = []
    for i, (nm, _r, _slot, close, desc, _len) in enumerate(clothes, start=2):
        d = str(desc or "").strip().rstrip("。")
        st = str(close or "").strip()
        if not st and ("无扣" in d or "没有扣" in d):
            st = "open"
        # 没有前襟的件(背心 / 半裙 / 裤子 / 鞋…)绝不能提「扣子 / 门襟」——
        # 硬写「扣子全部扣齐」会让模型凭空长出一排扣子(项目 testsdfsdfse 的针织背心)。
        if front_kind(nm, _slot) == "none":
            wear = "整件实心、与参考图完全一致,不要自行添加扣子 / 门襟 / 拉链或开口"
        else:
            wear = fasten_short(nm, _slot, st or "closed") or "前襟按参考图合上"
        if ("黑" in str(nm or "") or "黑" in d) and not any(
                k in str(nm or "") for k in ("鞋", "靴", "拖鞋")):
            wear += ";" + DARK_FABRIC_TIP
        # 开衫 / 外套在「飞过来穿上」的镜头里最容易变成披在肩上 —— 明确要求手臂穿进袖子
        if is_outer_sleeved(nm, _slot):
            wear += ";" + WORN_PROPERLY_TIP
        props.append(
            f"- <Prop {i - 1}>「{nm}」{d + ';' if d else ''}"
            f"外观 / 颜色 / 材质 / 版型以参考图 <Picture {i}> 为准;{wear};这一镜要穿上的就是它。"
        )
    _n = len(clothes) + 2
    for an, _r, adesc in accs:
        d = str(adesc or "").strip().rstrip("。")
        props.append(
            f"- <Prop {_n - 1}>「{an}」{d + ';' if d else ''}"
            f"外观 / 颜色 / 材质 / 版型以参考图 <Picture {_n}> 为准;"
            "**第一帧就已经戴在身上**,整段保持原样。"
        )
        _n += 1

    order = " → ".join(f"「{nm}」" for nm, _r, _s, _c, _d, _l in clothes)
    names_txt = "、".join(f"「{nm}」" for nm, *_ in clothes)
    acc_txt = "、".join(f"「{an}」" for an, _r, _d in accs)
    wear_txt = "\n".join(
        f"- <Picture {n}> 是这套衣服的实拍穿着参考:里面的人不是她,"
        "只用来对齐这一身衣服怎么穿(层次 / 垂坠 / 开合 / 袖口和裤脚落点),"
        "**尤其外套 / 开衫的袖子:照它那样把手臂穿进袖管、手从袖口伸出,不要当成披肩**;"
        "不要照搬那个人的脸、发型、身材、背景和构图。"
        for n in (wear_pics or [])
    )
    return f"""【镜头】第 1 / {total} 镜 · 整套换装·直接穿上

【画面】
- 机位:固定三脚架,全程零推拉摇移、零变焦。
- 场景:纯白背景,柔和均匀的棚拍光,没有其他陈设。
- 取景:全景全身,她占画面高度约 70%(头顶离上沿约 15%、脚底离下沿约 15%);双脚和鞋完整在画面里。
- Camera: static tripod, wide full-body shot; the camera is pulled far back, with a clear band of empty white space above her head and below her feet. She occupies only about 70% of the frame height and stays centred in frame from the first frame to the last.

【人物】
- 长相 / 发型:以 <Picture {head_pic}>(人物头部的标准参考图)为准;身材、身高、头身比、四肢比例以 <Picture 1> 为准。
- 站姿:直立站定,双脚平行朝前、钉在同一个点;肩膀水平,脸正对镜头。
- 表情:全程平静克制 —— 不笑、双唇自然闭合、不露齿。
- 生命感:呼吸自然轻微、偶尔眨眼;手是放松的真实的手。
{wear_txt + chr(10) if wear_txt else ''}- 穿着边界:她身上只出现本镜列出的这几件;衣服盖住的地方必须是盖住的,绝不露出内衣 / 胸口 / 腰腹。

【道具定义】
{chr(10).join(props)}
- 她身上只有这里列出的这几件,不要再加别的背心、吊带、打底衫或鞋。

【换装】
- 开始(第 1 帧):她身上只有白色无肩带打底上衣和白色短裤,**不穿本镜要换上的任何一件**(配饰除外);这一镜要把{names_txt}按顺序穿上。
- 新件:这些衣服在这一镜开始之后才从画外依次飞来 —— 第 1 帧时它们都还在画外。
- 动作:有人在画外把衣服朝她身上扔过来,一件接一件:先是第一件从画面一侧飞出来,划一道上扬的弧线落到她身上的对应部位,**碰到身体的一瞬间已经穿好、完全服帖(硬切,没有中间态)**;上一件完全服帖之后,下一件才飞入,彼此间隔约 0.5 秒。飞行中布料是软的,衣角被风带起,整件始终是这件衣服原本的完整形状。**画面里只有她一个人和正在飞的衣服 —— 扔衣服的人始终在画外。**
- 「穿好」的标准:**每件衣服都是真的穿在身上 —— 有袖子的件,两只手都从袖口伸出来、从肩到手腕的整条手臂都包在袖管里;绝不许出现空荡下垂的袖管,也不许像披肩 / 披巾那样搭在肩上。**
- 顺序:{order}。
- 收尾:全部穿好之后,**白色打底上衣和白色短裤整条消失(硬切)**,她身上只剩{names_txt}{('、' + acc_txt) if acc_txt else ''};每件实心、不透明、形状和各自参考图一致,胸口 / 腰 / 下摆处都不许再露出白色打底。**开衫 / 外套的两只袖子都穿在手臂上、两只手从袖口露出来 —— 是穿着的,不是搭在肩上的披肩。**

【声音】
overall_soundscape:安静的环境底噪。
non_diegetic_music:无。"""


def _rebuild_static_detail_shots(
    details: list,
    *,
    names: list[tuple[str, str, str, str, str, str]],
    char_name: str,
    char_desc: str,
    accs: list[tuple[str, str, str]],
    total: int,
    head_ref: str = "",
    style_ref: str = "",
    char_ref: str = "",
    has_bg: bool = False,
) -> None:
    """静态风格项目里的通用 / 自定义分镜:重排成跟「扔过来飞入」同构的提示词。

    **衣服也要挂参考图**(用户 2026-10-22「这些分镜衣服都要加上的」):只写文字描述时,
    模型会按自己的理解重画版型 / 领口 / 颜色;每件衣服的平铺图挂进 refs 并在
    【道具定义】带上对应的 <Picture N>,衣服细节才和换装链一致。

    常驻配饰:**每个通用 / 自定义分镜都把它挂进参考图**(用户 2026-10-22)——
    常驻 = 全程戴着,只写文字不给样本的话,模型画不出墨镜 / 鞋,或者干脆不画。
    换装图链仍然不带配饰,配饰全部由这些分镜自己的 refs 引入。

    参考图顺序 = 衣服 → 配饰;运行时 compose_prompt 再把静态成片作为
    「穿着方式参考图」追加到最后一张。
    """
    def _present_for(names_here: list[tuple[str, str, str, str, str, str]]):
        """按**这一镜真正用到**的件生成 present + 参考图顺序。

        包只在动作里写了的时候才进来(用户 2026-10-23),所以每镜的件数不一样,
        `<Picture N>` 的编号必须跟着这一镜重算。
        """
        props: list[dict] = []
        _seen_ref: set[str] = set()
        for nm, _r, slot, _close, desc, _len in names_here:
            if _r and _r in _seen_ref:
                # 同一张参考图挂了两遍(auto 页面重复上传 / 复制了一份改名)→ 只算一件,
                # 否则提示词里会写成两件衣服(实测 test9999123:「毛线厚底鞋」+「毛线厚底鞋（左脚）」)
                continue
            if _r:
                _seen_ref.add(_r)
            _place, _ = parse_slot(slot)
            # 描述里写明「无扣 / 没有扣子」的(开衫常见),不能再按全局默认写成"扣子全部扣齐" ——
            # 无扣的件写"扣齐"会让模型干脆把它脱掉(用户报「衣服自己消失了」)。
            if not _close and desc and ("无扣" in desc or "没有扣" in desc):
                _close = "open"
            # 穿在身上的(上身 / 下身 / 全身 / 脚 / 头)进「身上穿着」;包这类仍算道具,
            # 和飞入风格一致(鞋虽然也算「常驻配饰」,但它是穿在脚上的,不能丢进配饰)
            # 头这一档是墨镜 / 帽子这类小件 → 归「配饰」;脚上的鞋算穿着的一件
            # (用户 2026-10-23 给的样式:高跟鞋在【身上穿着】里,墨镜在【配饰】里)
            _is_worn = _place in ("上身", "下身", "全身", "脚")
            if not _place and not is_worn_accessory(nm):
                _is_worn = True
            props.append({"name": nm, "desc": desc, "_slot": slot, "_len": _len,
                          "_close": _close, "_worn": _is_worn})
        for an, _r, desc in accs:
            props.append({"name": an, "desc": desc})
        # 【人物】里有**三张图**,职责必须分开写清楚(用户 2026-10-22):
        #   人物全身图 → 定身材(身高 / 头身比 / 肩宽 / 腰胯 / 腿长);
        #   头像图     → 只管头;整套换装完成后的图 → 管这一身衣服和穿法。构图三张都不管。
        # 编号 = 衣服 + 配饰 (+ 全身) (+ 头像) + 1,这里直接写死,提示词里不会出现没有对应 <Picture N> 的指代。
        _n_used = sum(1 for t in names_here if t[0] and t[1]) + sum(1 for t in accs if t[0] and t[1])
        _char_pic = _n_used + 1 if char_ref else 0
        _head_pic = _n_used + (1 if char_ref else 0) + 1 if head_ref else 0
        _style_pic = _n_used + (1 if char_ref else 0) + (1 if head_ref else 0) + 1
        _n_person_pics = 1 + (1 if char_ref else 0) + (1 if head_ref else 0)
        _cnt_txt = {2: "两", 3: "三"}.get(_n_person_pics, str(_n_person_pics))
        _desc_lines = [
            f"本镜是同一个人,由下面{_cnt_txt}张参考图分工确定。",
        ]
        if _char_pic:
            _desc_lines.append(
                f"<Picture {_char_pic}>(人物全身参考图)定**她的身材**:身高、头身比、肩宽、腰胯、"
                "腿长和四肢粗细照它;这张图只是素材拍法,图里的打底衣物不要照搬,"
                "上身穿什么以【道具定义】为准。"
            )
        if _head_pic:
            _desc_lines.append(
                f"<Picture {_head_pic}>(人物头部的标准参考图)**只定头**:脸型、五官、发型、发长、"
                "发色、肤色照它;身材、衣服、穿搭、姿势、景别、机位、背景都不看这张图。"
                "如果它是正面 / 侧面 / 背面拼在一起的多格图,不要照搬拼图排布;"
                "图里裸露的肩颈只是头像素材的拍法,不要照搬,也不要因此给她加吊带 / 背心 / 打底衫。"
            )
        _style_src = (
            f"她的长相 / 发型看 <Picture {_head_pic}>、身材看 <Picture {_char_pic}>,"
            if (_head_pic and _char_pic)
            else ("她的长相 / 发型 / 身材都不看这张图," if _n_person_pics > 1 else "")
        )
        _desc_lines.append(
            f"<Picture {_style_pic}>(整套换装完成后的样子)定**这一身衣服 + 穿搭**:{_style_src}"
            "衣服的层次(哪件在里面、哪件在外面)、垂坠和褶皱、长度落点,"
            "以及内搭下摆扎不扎进裙腰、开衫扣着还是敞着、袖口和裤脚到哪儿,也都照它。"
            "单件的款式 / 颜色 / 材质 / 版型以【道具定义】里各自那张参考图为准,"
            "每件的开合以【道具定义】的「穿法」为准。"
        )
        _desc_lines.append(
            f"构图(景别 / 机位 / 她在画面里的大小和站位 / 背景)不看这{_cnt_txt}张图,"
            "一律按本镜说明生成,不要照搬任何一张参考图的构图。"
        )
        _desc = "\n".join(_desc_lines)
        if char_desc:
            _desc += f"({char_desc})"
        present = {
            "characters": [{"name": char_name or "人物", "desc": _desc}],
            "props": props,
            "scenes": [],
        }
        used = [(t[0], t[1]) for t in names_here if t[0] and t[1]]
        used += [(t[0], t[1]) for t in accs if t[0] and t[1]]
        return present, used

    for detail in details:
        txt = detail.prompt or ""
        # 新格式【画面】+【动作】分开;老格式是【画面 / 动作】,重建时都要能取出动作原文。
        m = re.search(r"【动作】\n(.*?)(?:\n\n【统一约束】|\Z)", txt, re.S)
        if not m:
            m = re.search(r"【画面 / 动作】\n(.*?)(?:\n\n【统一约束】|\Z)", txt, re.S)
        action = (m.group(1) if m else txt).strip()
        # 上一版统一追加的取景收口句不留在动作里,免得重建后越积越多
        action = re.sub(
            r"\n*- (?:取景以上面两行为准|以上动作全部在【画面】的取景范围内完成).*$",
            "", action, flags=re.S,
        ).strip()
        # 上一次追加的穿搭参照句不留在动作里,免得和下面新生成的那句重复
        action = re.sub(
            r"\n*<Picture \d+> 是她全部换装完成后的样子.*$", "", action, flags=re.S
        ).strip()
        # 景别按分镜自己的文案走,不再强制全身(用户 2026-10-22 要求去掉近景限制)。
        # 每镜的参考图顺序:衣服(与【身上穿着】同序)→ 常驻配饰。
        # 衣服必须挂图:只给文字,模型会自己改版型 / 颜色(用户 2026-10-22)。
        # 包只在动作里提到时才挂(用户 2026-10-23):她双手捧着别的东西时包里没地方放,
        # 模型会把它揉成一团黑色物体丢在脚边。鞋 / 墨镜这种一直穿戴的小件不受影响。
        _names_here = [
            t for t in names
            if not (t[0] and is_carried_prop(t[0])) or mentions_carry(action)
        ]
        present, _used = _present_for(_names_here)
        picture_of = {f"props:{n}": [i + 1] for i, (n, _r) in enumerate(_used)}
        detail.prompt = build_universal_prompt(
            {"title": detail.title, "action": action},
            present,
            picture_of,
            detail.idx,
            total,
            include_unmapped=True,
            # 静态风格统一取景(用户 2026-10-22):全景全身,她占画面高度约 70%。
            full_body_frame=True,
            # 剧情镜(卧室 / 小区 / 地铁 / 写字楼…)没有背景图时,场景按动作描述让模型自己画
            scene_from_action=wants_real_scene(action) and not has_bg,
        )
        detail.refs = [r for _n, r in _used]
        _opts = dict(detail.options or {})
        _opts.pop("look_ref", None)
        _opts.pop("look_path", None)
        # 人物全身:挂在头像前面(用户 2026-10-22「人物也需要引入进来」)——
        # 只靠头像锁不住身高 / 头身比 / 腿长,身材得单独给一张全身图。
        if char_ref:
            detail.refs = detail.refs + [char_ref]
        # 人物头像:整片每一镜都挂(用户 2026-10-22) —— 只锁头 / 脸,构图按本镜说明。
        if head_ref:
            detail.refs = detail.refs + [head_ref]
            _opts["head_ref"] = len(detail.refs)
            # 【人物】里的分工描述已经写了头像那条,别再插一遍
            if "人物头部的标准参考图" not in detail.prompt:
                detail.prompt = insert_into_person(
                    detail.prompt, "- " + head_def_text(len(detail.refs))
                )
        # 「整套换装完成后的样子」这张图也直接挂进 refs(用户 2026-10-22:别人的 <Picture N> 都有
        # 小图,只有 7 没有)—— 存在 refs 里前端才渲染得出缩略图,生成时也不会再重复追加。
        if style_ref:
            detail.refs = detail.refs + [style_ref]
            _opts["style_ref"] = len(detail.refs)
        # 取景靠提示词里的「远景镜头」描述,不做后期缩放(用户 2026-10-22「不要做归一」)
        _opts.pop("frame_pp", None)
        detail.options = _opts


# 读图没认出具体件名时,用「部位」拼一个兜底件名用到的词。
# 名字里必须带衣 / 裤 / 下装这类字,否则换装说明整段会被丢掉(见 script_split._GARMENT_HINTS)。
_SLOT_NAME = {
    "上身": "上衣",
    "下身": "下装",
    "全身": "连衣裙",
    "脚": "鞋",
    "头": "帽",
    "手": "包",
    "配饰": "配饰",
    # 老素材缓存里的品类词(重新识别之前还会读到)
    "上装": "上衣",
    "下装": "下装",
    "鞋": "鞋",
    "帽": "帽",
    "包": "包",
    "裤": "长裤",
    "裙": "半裙",
    "整身": "连衣裙",
}


@router.post(
    "/whole-look", response_model=WholeLookOut, status_code=status.HTTP_201_CREATED
)
def build_whole_look(
    payload: WholeLookIn,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> WholeLookOut:
    """「单镜整套 · 衣服飘入替换」:一镜里换整套,建项目 + 直接开渲。

    她开始时穿着 base_items(旧件);new_items 一件件从画外飘进来、罩到她身上、把旧件顶掉;
    accessories(墨镜 / 鞋…)全程戴着不动。件名和描述由前端读图认件得到后传进来。

    这里**只做编排**:把 plan 拼出来,交给和「剧本建项目」完全相同的那条路 ——
    提示词 / 绑图 / 换装顺序 / 旧件消失,全部复用以前调好的那套逻辑。
    """
    if not payload.new_items:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "至少要有一件要换上的新衣服")

    used: set[str] = set()

    # 件名 / 描述:调用方传了就用它;没传的先用素材上的读图缓存(ai_name / ai_desc);
    # 缓存还没有 → 现场读图认一遍并写回缓存,下次直接用、不再调视觉模型。
    all_items = (
        list(payload.base_items) + list(payload.new_items) + list(payload.accessories)
    )
    assets: list[Asset] = []
    for it in all_items:
        ref = (it.ref or "").strip()
        if not ref.startswith("asset:"):
            continue
        try:
            found = db.get(Asset, int(ref.split(":", 1)[1]))
        except ValueError:
            found = None
        if found is not None and all(found.id != a.id for a in assets):
            assets.append(found)
    if assets:
        recognize_assets(db, assets)
    # 一张图可能认出好几件(实拍拼图):排成「第 1 件 / 第 2 件 / …」的队列,
    # 同一张图被用几次就按顺序取几次 —— 这样"两件新衣共用一张拼图"也能各自拿到正确的件名。
    cached: dict[str, list[dict]] = {}
    for a in assets:
        seq = [
            {
                "name": (a.ai_name or "").strip(),
                "desc": (a.ai_desc or "").strip(),
                "kind": (a.ai_kind or "").strip(),
            }
        ]
        try:
            extra = json.loads(a.ai_more) if a.ai_more else []
        except ValueError:
            extra = []
        for m in extra if isinstance(extra, list) else []:
            if isinstance(m, dict):
                seq.append(
                    {
                        "name": str(m.get("name") or "").strip(),
                        "desc": str(m.get("desc") or "").strip(),
                        "kind": str(m.get("kind") or "").strip(),
                    }
                )
        cached[f"asset:{a.id}"] = seq
    taken: dict[str, int] = {}

    def _take(ref: str) -> dict:
        seq = cached.get(ref) or []
        n = taken.get(ref, 0)
        taken[ref] = n + 1
        return seq[n] if n < len(seq) else {}

    def _norm(it: WholeLookItem, i: int, prefix: str, garment_group: bool) -> dict:
        nm = (it.name or "").strip()
        # 描述里"微透 / 薄纱"这类拍摄属性词一律清掉 —— 留着会被模型照着画成半透明
        ds = quick_start.sanitize_desc(it.desc)
        kind = ""
        from_user = bool(nm)  # 用户手填的名字一律尊重,不做兜底替换
        if not nm or not ds or not kind:
            c = _take((it.ref or "").strip())
            nm = nm or c.get("name", "")
            ds = ds or quick_start.sanitize_desc(c.get("desc", ""))
            kind = c.get("kind", "")
        # 自动认出来的件名必须「看起来像衣服」:认成「其他」这类不含衣服字的名字会让
        # 整套换装说明(逐件 beats)整段丢掉 —— 这时用「部位」拼个兜底名(新上衣1 / 新下装1),
        # 名字里带着衣 / 裤 / 下装这些字,换装顺序、旧件配对、进场位置就都还算得对。
        # 但部位是上装 / 下装时(读图已经明说这是衣服,只是件名像「T恤」这种没有衣服字的词)
        # is_garment_name 也认它 —— 这时别改名,照着原件名走。
        if not from_user and garment_group and not is_garment_name(nm, kind):
            nm = ""
        if not nm:
            # 部位可能带「·外」后缀(上身·外)—— 取「穿在哪儿」那一半来拼兜底名
            place = parse_slot(kind)[0]
            nm = (
                f"{prefix}{_SLOT_NAME.get(place) or _SLOT_NAME.get(kind) or '衣服'}{i}"
                if garment_group
                else f"配件{i}"
            )
        if nm in used:  # 读图偶尔会把两件认成同名 —— 撞名会串掉参考图绑定
            nm = f"{nm}{i}"
        used.add(nm)
        return {"ref": it.ref, "name": nm, "desc": ds[:120], "kind": kind}

    base = [_norm(it, i, "旧", True) for i, it in enumerate(payload.base_items, 1)]
    new = [_norm(it, i, "新", True) for i, it in enumerate(payload.new_items, 1)]
    acc = [_norm(it, i, "", False) for i, it in enumerate(payload.accessories, 1)]

    # _base 标记 = 「一开始穿着、本镜头会被换掉」的旧件 —— build_prompt 靠它写"起始穿着",
    # 也靠它把这几件排除出"要飘进来的新件"(见 script_split)。
    # _slot = 读图给的「部位」(上装 / 下装 / 鞋 / 帽 / 包…)—— 让提示词按部位给新旧件配对、
    # 按部位定进场高度,比事后拿件名里的字去猜准(件名是开放词表,猜必漏)。
    #
    # **顺序要紧:要换上的(会飞的)排在前面、旧件往后放。** 参考图的顺序 = 提示词里的
    # <Picture 2、3…>,而模型是按**图片顺序**理解"第几件要飞的衣服"的。
    #
    # 实测(同一套"睡衣上衣 / 阔腿裤"素材、同种子):
    #   · 旧件排在人物图后面最前面 → 模型把旧件复制一份飘进来(用户报"为什么睡衣也飘了");
    #   · 试过"旧件在前 + 把下装错开到第一位"(#149)—— 那一版没飘,但同样的顺序只因为
    #     defs 里多了一句尺寸锚(#150),睡衣又飘了:说明"旧件在前"对措辞极其敏感、属模型层面的
    #     翻转,不是措辞能锁住的;而"新件在前"(#145 / #147)两次都没飘过旧件。
    #   所以这里保持**新件在前**;新件入场姿态(以前会横躺 / 翻滚)改由 script_split 里
    #   每件衣服的 <Prop> 定义(正立朝向 + 尺寸锚)去锁 —— 两件事分开治,互不打架。
    # 旧件 / 配件 / 风格图的**内部顺序仍按用户给的**,不重排。
    props = (
        [
            ScriptEntity(name=p["name"], desc=p["desc"], _slot=p.get("kind", ""))
            for p in new
        ]
        + [
            ScriptEntity(
                name=p["name"], desc=p["desc"], _base=True, _slot=p.get("kind", "")
            )
            for p in base
        ]
        + [
            ScriptEntity(name=p["name"], desc=p["desc"], _slot=p.get("kind", ""))
            for p in acc
        ]
    )
    names = [p.name for p in props]

    fps = settings.fps
    # 时长:默认整段 **15 秒**(= 360 帧,刚好是 H3 单次生成的上限 362 以内)。
    # 实测 10 秒那版模型会把两件的换装节奏挤在一起;放到 15 秒它有地方把先后排开。
    secs = payload.seconds or 15.0
    frames = min(
        quick_start.FLOAT_MAX_FRAMES, _round_to_half_second(int(round(secs * fps)))
    )
    scene_desc = quick_start.WHOLE_LOOK_SCENE_DESC
    # 第 4 栏「风格参考图」(实拍 lookbook):作为这一镜的「实拍穿着参考」挂在参考图最后。
    # 它**不占件、不进「要换上的这几件」**,只在提示词里补一句"只参考衣服、不参考那个人"。
    style_refs = [
        (it.ref or "").strip() for it in payload.style_items if (it.ref or "").strip()
    ]
    plan = ScriptPlanOut(
        characters=[ScriptEntity(name="她", desc="")],
        props=props,
        scenes=[ScriptEntity(name="纯白背景", desc=scene_desc)],
        shots=[
            ScriptShotPlan(
                title="单镜整套 · 衣服飘入替换",
                action=quick_start.whole_look_action(
                    [p["name"] for p in base], [p["name"] for p in new]
                ),
                seconds=frames / fps,
                characters=["她"],
                props=names,
                scenes=["纯白背景"],
                wear_refs=style_refs,
            )
        ],
    )
    assignments: dict[str, list[str]] = {"characters:她": [payload.person]}
    # 顺序必须和上面 props 一致(新件在前、旧件在后),否则 ref↔件名 会串位
    for it, p in zip(
        list(payload.new_items) + list(payload.base_items) + list(payload.accessories),
        new + base + acc,
    ):
        assignments[f"props:{p['name']}"] = [it.ref]

    title = (
        payload.title.strip()
        or f"单镜整套换装 {datetime.now().strftime('%m-%d %H:%M')}"
    )
    res = create_project_from_script(
        ScriptBuildIn(
            title=title,
            total_seconds=frames / fps,
            shot_count=1,
            mode="ref",
            plan=plan,
            assignments=assignments,
            float_mode=True,
            float_variant="side",
            float_base_outfit="",
            garment_desc="",
            head_ref=(payload.head_ref or "").strip(),
        ),
        user,
        db,
    )
    # 这一镜要按定好的时长**整段**生成 —— 不补 gen_length 的话只会拍默认那 124 帧就停了。
    # (这一镜刻意不是「按件数放大帧数」的换装镜,所以不会自动带上 gen_length。)
    shot = db.get(ProjectShot, res.project.shots[0].id)
    if shot is not None:
        shot.duration_frames = frames
        # seed 固定成标准那一版(#92)的,不写的话会按项目号推、每个项目都不一样
        shot.options = {
            **(shot.options or {}),
            "gen_length": frames,
            "seed": quick_start.WHOLE_LOOK_SEED,
        }
        db.commit()

    # ---- 勾选的「通用分镜」:接在换装镜后面,一起建进同一个工程 ----
    # 参考图直接用本工程那套:人物 + 要换上的(新件) + 配件(+ 风格参考),**不含旧件** ——
    # 到这些镜头时她早就换好了。
    # 关键:新件在这里按 `_worn`(她已经穿着的那一件)传进去 —— build_prompt 会写成
    # "这一件是她现在身上已经穿着的那一件",既不会再多出一段"要飞进来的",也不会把它当成
    # 本镜要换的件。手工搬进 #143 的那批细节镜就是这个结构。
    if payload.shot_keys:
        lib = user_shot_library(user, db)
        actions = lib["shot_map"]
        labels = {it["key"]: it["label"] for it in lib["library"]}
        # 每个自定义分镜可以有自己的默认时长(库里没设的仍按 3 秒)
        seconds_map = {
            it["key"]: float(it.get("seconds") or UNIVERSAL_SHOT_SECONDS)
            for it in lib["library"]
        }
        order = list(payload.new_items) + list(payload.accessories)
        # 同一张参考图被重复加进列表(上传两次 / 复制改名)→ 只算一件:
        # 否则换装顺序里会出现一件重复的衣服(实测「毛线厚底鞋」和「毛线厚底鞋（左脚）」)
        _seen_ref: set[str] = set()
        _dedup_order = []
        for _it in order:
            _r = (_it.ref or "").strip()
            if _r and _r in _seen_ref:
                continue
            if _r:
                _seen_ref.add(_r)
            _dedup_order.append(_it)
        order = _dedup_order
        refs: list[str] = [payload.person]
        for it in order:
            r = (it.ref or "").strip()
            if r and r not in refs:
                refs.append(r)
        wear_pics: list[int] = []
        for r in style_refs:  # 第 4 栏风格参考图:挂在最后
            if r and r not in refs:
                refs.append(r)
            n = refs.index(r) + 1
            if n not in wear_pics:
                wear_pics.append(n)
        present = {
            "characters": [{"name": "她", "desc": ""}],
            "props": (
                [{"name": p["name"], "desc": p["desc"], "_worn": True} for p in new]
                + [{"name": p["name"], "desc": p["desc"]} for p in acc]
            ),
            "scenes": [{"name": "纯白背景", "desc": scene_desc}],
        }
        picture_of: dict[str, list[int]] = {"characters:她": [1]}
        for it, p in zip(order, new + acc):
            r = (it.ref or "").strip()
            if r in refs:
                picture_of[f"props:{p['name']}"] = [refs.index(r) + 1]
        # 通用分镜默认时长:库里有 seconds 就用它,没有仍按 3 秒
        total = 1 + len(payload.shot_keys)
        # 「道具」栏(素材库 category=prop)里被本镜动作点名的 → 自动挂参考图 + <Prop N>。
        # 以前整套换装只认「要换上的件 / 常驻配饰」,道具库里的包、手机、书这些永远进不来,
        # 提示词里写了「挎着单肩包」模型却手上没图,只能自己瞎画(用户 2026-10-24 报)。
        _prop_assets = [
            a for a in db.query(Asset)
            .filter(Asset.user_id == project.user_id, Asset.category == "prop")
            .all()
            if (a.ai_name or "").strip()
            # 只认真正的「道具」:衣服 / 鞋 / 帽子这些穿在身上的不进这一支
            # (它们该走 new_items / accessories,否则同一件会既算穿着又算道具)
            and (a.ai_kind or "").strip() not in ("上身", "下身", "全身", "脚", "头")
        ]
        for i, key in enumerate(payload.shot_keys, start=2):
            action = (actions.get(key) or "").strip()
            if not action:
                continue
            _secs = seconds_map.get(key) or UNIVERSAL_SHOT_SECONDS
            _frames = max(MIN_SHOT_FRAMES, _round_to_half_second(round(_secs * fps)))
            _opts = {"float": 1}
            if _frames > settings.gen_length:
                _opts["gen_length"] = min(
                    362, 5 + 17 * max(2, -(-(_frames - 5) // 17))
                )
            _shot_refs = list(refs)
            _shot_present = {
                "characters": present["characters"],
                "props": list(present["props"]),
                "scenes": present["scenes"],
            }
            _shot_pics = dict(picture_of)
            for _a in _prop_assets:
                _nm = _a.ai_name.strip()
                if _nm not in action:                      # 本镜没点到这件道具
                    continue
                if any(p["name"] == _nm for p in _shot_present["props"]):
                    continue                               # 已经在件/配饰里了
                _ref = f"asset:{_a.id}"
                if _ref not in _shot_refs:
                    _shot_refs.append(_ref)                # 追加在最后,前面图号不动
                _shot_present["props"].append(
                    {"name": _nm, "desc": (_a.ai_desc or "").strip()})
                _shot_pics[f"props:{_nm}"] = [_shot_refs.index(_ref) + 1]
            db.add(
                ProjectShot(
                    project_id=res.project.id,
                    idx=i,
                    key=f"s{i:02d}",
                    title=labels.get(key) or f"镜头 {i}",
                    mode="ref",
                    duration_frames=_frames,
                    prompt=concise.rule_condense(
                        build_prompt(
                            {"title": labels.get(key, ""), "action": action},
                            _shot_present,
                            _shot_pics,
                            i,
                            total,
                            float_mode=True,
                            float_variant="side",
                            wear_ref_pics=wear_pics,
                            no_ndm=True,
                        )
                    ),
                    refs=_shot_refs,
                    options=_opts,
                    enabled=True,
                    status=ST_QUEUED,
                )
            )
        db.commit()

    job = start_render(res.project.id, user, db)
    return WholeLookOut(project=project_out(db.get(Project, res.project.id)), job=job)


@router.get("", response_model=ProjectPageOut)
def list_projects(
    page: int = Query(1, ge=1),
    page_size: int = Query(12, ge=1, le=100),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> ProjectPageOut:
    """「我的项目」列表:按 id 倒序分页。

    项目只增不减(实测 190+),列表页一律全量返回会让页面上铺一长条、每次轮询也白传一遍。
    这里只取**当前这一页** + 总数,total 让前端知道该画几页。
    """
    q = db.query(Project).filter(Project.user_id == user.id)
    total = q.count()
    rows = (
        q.order_by(Project.id.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
        .all()
    )
    return ProjectPageOut(
        items=[project_brief(p) for p in rows],
        total=total,
        page=page,
        page_size=page_size,
    )


@router.get("/{project_id}", response_model=ProjectOut)
def get_project(
    project_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> ProjectOut:
    return project_out(_own(project_id, user, db))


@router.patch("/{project_id}", response_model=ProjectOut)
def update_project(
    project_id: int,
    payload: ProjectPatchIn,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> ProjectOut:
    """改标题 / 成片分辨率 / 帧率。

    分辨率或帧率一变,已生成的成片与镜头片段就跟设置不匹配了,所以会一并失效,
    等用户重新点「开始生成」。改 fps 时按「秒」不变重算每个镜头的帧数,
    单镜头仍受一次生成上限约束。
    """
    project = _own(project_id, user, db)
    data = payload.model_dump(exclude_unset=True)

    render_keys = {"out_width", "out_height", "fps"}
    if render_keys & data.keys():
        _ensure_idle(db, project)

    if data.get("title") is not None:
        project.title = data["title"]

    old_fps = project.fps or settings.fps
    new_fps = data.get("fps") or old_fps
    if new_fps != old_fps:
        for s in project.shots:
            secs = s.duration_frames / old_fps
            s.duration_frames = max(
                MIN_SHOT_FRAMES, min(settings.gen_length, round(secs * new_fps))
            )

    for field in ("out_width", "out_height", "fps"):
        if data.get(field) is not None:
            setattr(project, field, data[field])

    # 画布布局:纯 UI 状态,不影响成片,直接原样保存
    if "canvas_state" in data:
        project.canvas_state = data.get("canvas_state") or {}

    if render_keys & data.keys():
        project.final_rel_path = None
        project.status = ST_QUEUED
        for s in project.shots:
            s.clip_rel_path = None
            s.status = ST_QUEUED

    db.commit()
    db.refresh(project)
    return project_out(project)


@router.delete("/{project_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_project(
    project_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> None:
    project = _own(project_id, user, db)
    db.delete(project)
    db.commit()


@router.patch("/{project_id}/shots/{shot_id}", response_model=ShotOut)
def update_shot(
    project_id: int,
    shot_id: int,
    payload: ShotPatchIn,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> ShotOut:
    project = _own(project_id, user, db)
    shot = _own_shot(project, shot_id)

    data = payload.model_dump(exclude_unset=True)
    for field, value in data.items():
        setattr(shot, field, value)
    # 只改名字不影响出片内容,不用作废已生成的片段;其余字段(提示词/参考图/模式/时长/参数)一改就作废
    if set(data) - {"title"}:
        shot.status = ST_QUEUED
        shot.clip_rel_path = None
        for c in (
            shot.candidates or []
        ):  # 抽卡候选也一并作废(提示词/参数都改了,旧片子不能再用)
            _unlink(c)
        shot.candidates = []
    db.commit()
    db.refresh(shot)
    return shot_out(shot)


@router.post("/{project_id}/shots/{shot_id}/optimize", response_model=ShotOptimizeOut)
def optimize_shot_prompt(
    project_id: int,
    shot_id: int,
    payload: ShotOptimizeIn,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> ShotOptimizeOut:
    """让模型优化这段提示词并返回结果。

    只返回文本、不落库 —— 前端把它写进编辑框,用户确认后自己点「保存」。
    优化结果会做校验:小节不能缺、<Picture N> 编号不能变,否则直接拒绝。
    """
    project = _own(project_id, user, db)
    _own_shot(project, shot_id)  # 只做归属校验

    text, engine, detail = optimize_prompt(payload.prompt, payload.instruction)
    if text is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail)

    problem = validate_optimized(payload.prompt, text)
    if problem:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            f"这次优化结果不可用({problem}),已放弃。可以换个说法再试。",
        )
    return ShotOptimizeOut(prompt=text, engine=engine, engine_detail=detail)


@router.post("/{project_id}/condense-prompt", response_model=CondensePromptOut)
def condense_prompt_text(
    project_id: int,
    payload: CondensePromptIn,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> CondensePromptOut:
    """单镜「精简」:只返回精简后的文本,不落库 —— 用户确认后自己点「保存」。

    先试模型改写(像导演讲戏的 2-5 句),模型不可用/漏引用标记时自动退回规则版。
    """
    _own(project_id, user, db)
    text, engine = concise.condense(payload.prompt, use_llm=True)
    detail = "AI 精简" if engine == "llm" else "规则精简(模型不可用或结果不合法)"
    return CondensePromptOut(prompt=text, engine=engine, engine_detail=detail)


@router.post("/{project_id}/canvas/upload")
async def canvas_upload(
    project_id: int,
    file: UploadFile = File(...),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """画布自定义节点的图片 / 视频上传。

    图片会同时写进素材库(返回 asset:<id> 作为 ref,之后可以连到分镜上);
    视频只存文件,返回 /media/canvas/... 的公开地址。
    """
    _own(project_id, user, db)
    mime = (file.content_type or "").lower()
    filename = file.filename or "media"
    is_image = mime.startswith("image/")
    is_video = mime.startswith("video/")
    if not (is_image or is_video):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "只支持图片或视频文件")

    ext = Path(filename).suffix.lower()
    if not ext:
        ext = ".png" if is_image else ".mp4"
    rel = f"canvas/{uuid.uuid4().hex}{ext}"
    dest = STORAGE_DIR / rel
    dest.parent.mkdir(parents=True, exist_ok=True)

    size = 0
    with dest.open("wb") as out:
        while True:
            chunk = await file.read(1024 * 1024)
            if not chunk:
                break
            size += len(chunk)
            if size > 300 * 1024 * 1024:
                out.close()
                dest.unlink(missing_ok=True)
                raise HTTPException(
                    status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, "文件太大(上限 300MB)"
                )
            out.write(chunk)

    ref = ""
    if is_image:
        width = height = 0
        try:
            from PIL import Image

            with Image.open(dest) as im:
                width, height = im.size
        except Exception:  # noqa: BLE001
            pass
        asset = Asset(
            user_id=user.id,
            filename=filename,
            rel_path=rel,
            category=CAT_PROP,
            mime=mime,
            width=width,
            height=height,
        )
        db.add(asset)
        db.commit()
        db.refresh(asset)
        ref = f"asset:{asset.id}"

    return {
        "url": f"/media/{rel}",
        "filename": filename,
        "kind": "image" if is_image else "video",
        "ref": ref,
    }


@router.post("/{project_id}/canvas/image")
def canvas_generate_image(
    project_id: int,
    payload: CanvasImageIn,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """画布图片节点:用 SDXL 预览模型按一句描述出 1-4 张图。

    这是「快速看画面」的模型,和成片 H3 不是同一个;人物长相/衣服纹理不会完全一致。
    """
    project = _own(project_id, user, db)
    _ensure_idle(db, project)
    if not comfy_client.is_up():
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, "ComfyUI 未启动,无法生成图片"
        )

    base = {
        "16:9": (1024, 576),
        "1:1": (1024, 1024),
        "9:16": (576, 1024),
        "3:4": (896, 1152),
    }
    base_w, base_h = base.get(str(payload.aspect or "").strip(), (1024, 576))
    scale = 1.5 if str(payload.resolution).upper() == "2K" else 1.0
    width = max(512, int(round(base_w * scale / 64)) * 64)
    height = max(512, int(round(base_h * scale / 64)) * 64)

    prompt = payload.prompt.strip()
    if re.search(r"[\u4e00-\u9fff]", prompt):
        try:
            translated = llm.chat_text(
                "Translate this image description into ONE short English prompt for SDXL. "
                "Keep the subject, clothing, pose, background and lighting. "
                "Output only the prompt, no quotes or labels.",
                prompt,
                temperature=0.2,
            )
            if translated:
                prompt = translated
        except Exception:  # noqa: BLE001
            pass
    prompt = " ".join(prompt.split())[:600]
    if "photorealistic" not in prompt.lower():
        prompt += ", photorealistic, high detail, sharp focus"

    comfy_client.free_memory()
    images: list[str] = []
    try:
        for i in range(int(payload.count or 1)):
            prefix = f"canvas_p{project_id}_{uuid.uuid4().hex[:8]}_{i}"
            wf = comfy_workflow.build_preview_workflow(
                prompt=prompt,
                seed=random.randint(1, 2**31 - 1),
                prefix=prefix,
                width=width,
                height=height,
            )
            prompt_id = comfy_client.submit(wf)
            comfy_client.wait(prompt_id, timeout=settings.preview_timeout)
            src = comfy_client.newest_image(prefix)
            if src is None:
                continue
            rel = f"canvas/{uuid.uuid4().hex}.png"
            dest = STORAGE_DIR / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src, dest)
            images.append(f"/media/{rel}")
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(
            status.HTTP_500_INTERNAL_SERVER_ERROR, f"图片生成失败: {str(exc)[:200]}"
        ) from exc
    finally:
        comfy_client.free_memory()

    if not images:
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, "图片生成失败")
    return {"images": images, "width": width, "height": height}


def _canvas_state_of(project: Project) -> dict:
    state = project.canvas_state if isinstance(project.canvas_state, dict) else {}
    return dict(state)


def _custom_nodes_of(state: dict) -> list[dict]:
    nodes = state.get("custom_nodes")
    return nodes if isinstance(nodes, list) else []


def _media_local(url: str | None) -> Path | None:
    if isinstance(url, str) and url.startswith("/media/"):
        path = STORAGE_DIR / url[len("/media/") :]
        return path if path.exists() else None
    return None


def _last_frame_png(clip_path: Path) -> Path | None:
    """抽一段视频的最后一帧,存成 png,返回本地路径。"""
    try:
        import av

        with av.open(str(clip_path)) as container:
            stream = container.streams.video[0]
            last = None
            for frame in container.decode(stream):
                last = frame
            if last is None:
                return None
            image = last.to_image()
        rel = f"canvas/links/{uuid.uuid4().hex}.png"
        dest = STORAGE_DIR / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        image.save(dest, "PNG")
        return dest
    except Exception as exc:  # noqa: BLE001
        print(f"[canvas-link] 取尾帧失败 {clip_path}: {exc}", flush=True)
        return None


def _link_source(db: Session, project: Project, from_key: str):
    """返回 (本地图片路径, 可展示 url, 来源类型)。没有画面时路径/url 为空。"""
    if from_key.startswith("shot:"):
        try:
            sid = int(from_key.split(":", 1)[1])
        except ValueError:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "无效的源节点")
        shot = next((s for s in project.shots if s.id == sid), None)
        if shot is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "源镜头不存在")
        if not shot.clip_rel_path:
            return None, None, "shot"
        clip = STORAGE_DIR / shot.clip_rel_path
        if not clip.exists():
            return None, None, "shot"
        path = _last_frame_png(clip)
        if path is None:
            return None, None, "shot"
        rel = path.relative_to(STORAGE_DIR).as_posix()
        return path, f"/media/{rel}", "video_tail"

    if from_key.startswith("custom:"):
        nid = from_key.split(":", 1)[1]
        state = _canvas_state_of(project)
        node = next((n for n in _custom_nodes_of(state) if n.get("id") == nid), None)
        if node is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "源节点不存在")
        typ = node.get("type")
        if typ in ("threeview", "personview"):
            # 三视图 / 人物三视图节点输出的是多视图拼图,只能当参考图,绝不能当首帧硬锚
            url = (node.get("outputs") or [None])[0] or node.get("url")
            return _media_local(url), url, "threeview"
        url = node.get("url") or ((node.get("images") or [None])[0])
        local = _media_local(url)
        ref = str(node.get("ref") or "")
        if local is None and ref.startswith("asset:"):
            try:
                asset = db.get(Asset, int(ref.split(":", 1)[1]))
            except ValueError:
                asset = None
            if asset is not None:
                local = STORAGE_DIR / asset.rel_path
                if not local.exists():
                    local = None
                if local is not None and not url:
                    url = f"/media/{asset.rel_path}"
        if typ == "video" and local is not None:
            tail = _last_frame_png(local)
            if tail is not None:
                rel = tail.relative_to(STORAGE_DIR).as_posix()
                return tail, f"/media/{rel}", "video_tail"
        if typ == "image":
            return local, url, "image"
        return None, None, typ or "custom"

    return None, None, "other"


def _apply_link_to_shot(
    db: Session,
    project: Project,
    shot: ProjectShot,
    source_path: Path,
    source_kind: str,
) -> str:
    rel = source_path.relative_to(STORAGE_DIR).as_posix()
    asset = (
        db.query(Asset)
        .filter(Asset.user_id == project.user_id, Asset.rel_path == rel)
        .first()
    )
    if asset is None:
        width = height = 0
        try:
            from PIL import Image

            with Image.open(source_path) as im:
                width, height = im.size
        except Exception:  # noqa: BLE001
            pass
        asset = Asset(
            user_id=project.user_id,
            filename="上一镜尾帧.png"
            if source_kind == "video_tail"
            else "节点图片.png",
            rel_path=rel,
            category=CAT_PROP,
            mime="image/png",
            width=width,
            height=height,
        )
        db.add(asset)
        db.flush()

    ref = f"asset:{asset.id}"
    refs = list(shot.refs or [])
    if source_kind == "threeview":
        # 三视图是「正面/侧面/背面」拼在一张的版式图,拿来当第 0 帧会把拼图直接
        # 演成视频(人没头没腿、多张脸叠在一起)。这里只当参考图,不设首帧锚。
        if ref not in refs:
            refs.append(ref)
        opts = {**(shot.options or {}), "link_kind": "threeview"}
        if (shot.options or {}).get("link_kind") == "threeview":
            opts.pop("anchor0", None)
        shot.options = opts
        shot.refs = refs
        if shot.mode == MODE_TEXT:
            shot.mode = MODE_REF
        shot.status = ST_QUEUED
        if shot.clip_rel_path:
            _unlink(shot.clip_rel_path)
        shot.clip_rel_path = None
        for c in shot.candidates or []:
            _unlink(c)
        shot.candidates = []
        return ref
    if shot.mode in (MODE_FIRST, MODE_FIRST_LAST):
        # 首帧 / 首尾帧模式:把接过来的画面放在第一张,就是新的首帧
        refs = [ref] + [r for r in refs if r != ref]
    elif ref not in refs:
        refs.append(ref)
    shot.refs = refs
    if shot.mode == MODE_TEXT:
        shot.mode = MODE_REF
    shot.options = {
        **(shot.options or {}),
        "anchor0": str(source_path),
        "link_kind": source_kind,
    }
    shot.status = ST_QUEUED
    if shot.clip_rel_path:
        _unlink(shot.clip_rel_path)
    shot.clip_rel_path = None
    for c in shot.candidates or []:
        _unlink(c)
    shot.candidates = []
    return ref


@router.post("/{project_id}/canvas/link")
def canvas_link(
    project_id: int,
    payload: dict,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """手动连线:把源节点的「尾帧 / 图片」传给目标节点。

    * 源是视频镜头 / 视频节点 -> 抽最后一帧;
    * 源是图片节点 -> 直接用那张图;
    * 目标是视频镜头 -> 加进参考图,并作为首帧锚(anchor0),镜头需要重出;
    * 目标是图片节点 -> 直接把图显示到节点里。
    """
    project = _own(project_id, user, db)
    from_key = str(payload.get("from") or "")
    to_key = str(payload.get("to") or "")
    if not from_key or not to_key or from_key == to_key:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "无效的连线")

    source_path, source_url, source_kind = _link_source(db, project, from_key)

    if to_key.startswith("custom:"):
        if not source_url:
            return {
                "ok": True,
                "applied": False,
                "reload": False,
                "message": "源节点还没有可传递的画面，已只建立连线",
            }
        nid = to_key.split(":", 1)[1]
        state = _canvas_state_of(project)
        # 复制一份再改:直接在原 list/dict 上改的话,SQLAlchemy 比较 JSON 时会认为没变化
        nodes = [dict(n) for n in _custom_nodes_of(state)]
        node = next((n for n in nodes if n.get("id") == nid), None)
        if node is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "目标节点不存在")
        if node.get("type") in ("threeview", "personview"):
            # 人物三视图:第一张接进来的图先当「人物照片」,之后接进来的才算服装
            if node.get("type") == "personview":
                opts = dict(node.get("opts") or {})
                if not opts.get("person"):
                    opts["person"] = source_url
                    opts.setdefault("mode", "person")
                    node["opts"] = opts
                    project.canvas_state = {**state, "custom_nodes": nodes}
                    db.commit()
                    return {
                        "ok": True,
                        "applied": True,
                        "reload": True,
                        "message": "已把画面接成人物照片，再传几张服装就能生成",
                    }
            # 三视图 / 人物三视图节点:把接过来的画面追加成输入图
            # (三视图最多 3 张;人物三视图里人物占 1 张,服装最多 2 张)
            imgs = [u for u in (node.get("images") or []) if u]
            if source_url not in imgs:
                imgs.append(source_url)
            limit = 2 if node.get("type") == "personview" else 3
            node["images"] = imgs[-limit:]
            project.canvas_state = {**state, "custom_nodes": nodes}
            db.commit()
            return {
                "ok": True,
                "applied": True,
                "reload": True,
                "message": f"已把画面接进节点（{len(node['images'])}/{limit} 张输入）",
            }
        if node.get("type") != "image":
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "目标节点不是图片节点")
        node["url"] = source_url
        node["images"] = [source_url]
        node["imageIndex"] = 0
        node["title"] = node.get("title") or "图片"
        project.canvas_state = {**state, "custom_nodes": nodes}
        db.commit()
        return {
            "ok": True,
            "applied": True,
            "reload": True,
            "message": "已把上一节点的尾帧/图片传给图片节点",
        }

    if to_key.startswith("shot:"):
        if source_path is None:
            return {
                "ok": True,
                "applied": False,
                "reload": False,
                "message": "源节点还没有可传递的画面，已只建立连线",
            }
        try:
            sid = int(to_key.split(":", 1)[1])
        except ValueError:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "无效的目标节点")
        shot = next((s for s in project.shots if s.id == sid), None)
        if shot is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "目标镜头不存在")
        _apply_link_to_shot(db, project, shot, source_path, source_kind)
        db.commit()
        return {
            "ok": True,
            "applied": True,
            "reload": True,
            "message": "已把上一节点的尾帧/图片传给这个镜头，目标镜头需要重新生成",
        }

    raise HTTPException(status.HTTP_400_BAD_REQUEST, "目标节点不支持接收画面")


@router.post("/{project_id}/canvas/inpaint")
def canvas_inpaint(
    project_id: int,
    payload: dict,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """图片节点局部修理:把涂抹区域重绘,区域外保持不动。"""
    import base64

    from .. import inpaint

    project = _own(project_id, user, db)
    node_id = str(payload.get("node_id") or "")
    state = _canvas_state_of(project)
    nodes = [dict(n) for n in _custom_nodes_of(state)]
    node = next((n for n in nodes if n.get("id") == node_id), None)
    if node is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "节点不存在")
    url = node.get("url") or ((node.get("images") or [None])[0])
    if not url:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "这个节点还没有图片")
    mask = payload.get("mask")
    if not isinstance(mask, str) or "," not in mask:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "没有收到涂抹区域")
    try:
        raw = base64.b64decode(mask.split(",", 1)[1])
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "涂抹区域解析失败") from exc
    try:
        new_url = inpaint.run(
            url, raw, str(payload.get("prompt") or ""), int(payload.get("steps") or 24)
        )
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, str(exc)) from exc

    imgs = [u for u in (node.get("images") or []) if u]
    if new_url not in imgs:
        imgs.append(new_url)
    node["images"] = imgs
    node["url"] = new_url
    node["imageIndex"] = imgs.index(new_url)
    project.canvas_state = {**state, "custom_nodes": nodes}
    db.commit()
    return {"ok": True, "url": new_url, "images": imgs}


@router.post("/{project_id}/canvas/threeview/start")
def canvas_threeview_start(
    project_id: int,
    payload: dict,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """启动画布上的「三视图 / 人物三视图」节点。"""
    from .. import threeview

    project = _own(project_id, user, db)
    node_id = str(payload.get("node_id") or "")
    state = _canvas_state_of(project)
    node = next((n for n in _custom_nodes_of(state) if n.get("id") == node_id), None)
    if node is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "节点不存在")
    if node.get("type") not in ("threeview", "personview"):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "这不是三视图节点")
    try:
        job = threeview.start(project.id, node)
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    return {"ok": True, **job}


@router.get("/{project_id}/canvas/threeview/status")
def canvas_threeview_status(
    project_id: int,
    node_id: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    from .. import threeview

    project = _own(project_id, user, db)
    state = _canvas_state_of(project)
    node = next((n for n in _custom_nodes_of(state) if n.get("id") == node_id), None)
    if node is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "节点不存在")
    job = threeview.status(project.id, node_id)
    if job.get("status"):
        return {"ok": True, **job}
    # 后端重启后内存里的任务没了:节点还停在 running/queued 就明确告诉前端已中断
    if node.get("status") in ("queued", "running"):
        return {
            "ok": True,
            "status": "failed",
            "message": "",
            "outputs": node.get("outputs") or [],
            "error": "后端重启，任务已中断，请重新生成",
        }
    return {
        "ok": True,
        "status": node.get("status") or "",
        "message": node.get("message") or "",
        "outputs": node.get("outputs") or [],
        "error": node.get("error") or "",
    }


@router.post("/{project_id}/shots/reorder", response_model=ProjectOut)
def reorder_shots(
    project_id: int,
    payload: ShotReorderIn,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> ProjectOut:
    project = _own(project_id, user, db)
    _ensure_idle(db, project)
    by_id = {s.id: s for s in project.shots}
    if set(payload.shot_ids) != set(by_id):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "镜头列表不完整")
    for i, sid in enumerate(payload.shot_ids, start=1):
        by_id[sid].idx = i
    db.commit()
    db.expire(project, ["shots"])
    # 排序后提示词里的「第 N 个镜头,共 M 个」编号也要同步刷新
    _renumber_prompt_brackets(project, db)
    db.refresh(project)
    return project_out(project)


@router.delete("/{project_id}/shots/{shot_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_shot(
    project_id: int,
    shot_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> None:
    project = _own(project_id, user, db)
    shot = _own_shot(project, shot_id)
    _ensure_idle(db, project)

    # keep old jobs from pointing at a shot that no longer exists
    db.query(RenderJob).filter(RenderJob.shot_id == shot.id).update(
        {RenderJob.shot_id: None}
    )
    db.delete(shot)
    db.flush()
    db.expire(project, ["shots"])
    for i, s in enumerate(sorted(project.shots, key=lambda x: x.idx), start=1):
        s.idx = i
    db.commit()
    _renumber_prompt_brackets(project, db)


def _zip_shots_response(project: Project, shots: list[ProjectShot]) -> StreamingResponse:
    """把给定镜头的已生成片段打包成 zip 返回。扩展名按实际文件取(视频 .mp4 / 静态图 .png)。"""
    buf = BytesIO()
    packed = 0
    seen = {}
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for s in shots:
            if not s.clip_rel_path:
                continue
            clip_path = STORAGE_DIR / s.clip_rel_path
            if not clip_path.exists():
                continue
            # 文件名: idx_标题.扩展名,避免重名
            safe_title = re.sub(r'[\\/:*?"<>|]', "_", s.title or s.key)
            arcname = f"{s.idx:02d}_{safe_title}{clip_path.suffix or '.mp4'}"
            if arcname in seen:
                seen[arcname] += 1
                arcname = f"{s.idx:02d}_{safe_title}_{seen[arcname]}{clip_path.suffix or '.mp4'}"
            else:
                seen[arcname] = 1
            zf.write(clip_path, arcname)
            packed += 1
    if not packed:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "这个项目还没有已生成的镜头片段")
    buf.seek(0)
    safe_title = re.sub(r'[\\/:*?"<>|\r\n]', "_", project.title or "project")
    # 头里**不能直接放中文**:starlette 用 latin-1 编码 header,中文会抛
    # UnicodeEncodeError → 500。按 RFC 5987 给 ASCII 兜底名 + UTF-8 原名。
    ascii_name = re.sub(r"[^A-Za-z0-9._-]+", "_", safe_title).strip("_") or "project"
    disp = (
        f'attachment; filename="{ascii_name}_shots.zip"; '
        f"filename*=UTF-8''{quote(safe_title + '_shots.zip')}"
    )
    return StreamingResponse(
        buf,
        media_type="application/zip",
        headers={"Content-Disposition": disp},
    )


@router.post("/{project_id}/shots/download-zip")
def download_shots_zip(
    project_id: int,
    payload: ShotReorderIn,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """把选中的镜头片段打包成 zip 下载。payload.shot_ids = 要打包的镜头 id 列表。"""
    project = _own(project_id, user, db)
    by_id = {s.id: s for s in project.shots}
    missing = [sid for sid in payload.shot_ids if sid not in by_id]
    if missing:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"镜头不存在: {missing}")
    return _zip_shots_response(project, [by_id[sid] for sid in payload.shot_ids])


@router.get("/{project_id}/shots/download-zip")
def download_all_shots_zip(
    project_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """把项目里所有已生成的分镜片段打包成 zip 下载(按分镜顺序)。"""
    project = _own(project_id, user, db)
    return _zip_shots_response(project, sorted(project.shots, key=lambda s: s.idx))


def _replace_general_prompt(
    prompt: str, title: str, action: str, idx: int, total: int
) -> str:
    """替换通用分镜的提示词:换镜头编号 + 动作描述(summary 和 detailed_description 两处)。

    新格式的通用分镜提示词就是纯动作文本(没有 summary/detailed_description 结构),
    这种直接整段换成新动作 —— 否则正则不命中会静默复制模板内容(实测踩过:
    加出来的分镜内容永远是模板镜的补妆文本)。
    """
    # 七段式【镜头】【场景】【人物】【道具定义】【画面】【动作】【统一约束】:
    # 只换【镜头】标题和【动作】正文,其余段(场景 / 人物 / 道具定义 / 画面 / 统一约束)**原样保留**,
    # 这样新加的自定义分镜和普通分镜结构一致,<Picture N> 引用也一起继承下来。
    if "【动作】" in prompt and "【镜头】" in prompt:
        prompt = re.sub(
            r"^【镜头】第 \d+ / \d+ 镜 · .*$",
            f"【镜头】第 {idx} / {total} 镜 · {title}",
            prompt,
            count=1,
            flags=re.M,
        )
        m = re.search(r"【动作】\n(.*?)(?=\n【|\Z)", prompt, flags=re.DOTALL)
        if m:
            body = m.group(1)
            # 段末那几行「以上动作全部在【画面】的取景范围内完成…」是收口约束,保留
            k = body.find("- 以上动作")
            tail = ("\n" + body[k:].rstrip()) if k >= 0 else ""
            prompt = prompt[: m.start(1)] + action.strip() + tail + "\n" + prompt[m.end(1):]
        return prompt
    if "detailed_description:" not in prompt:
        return action
    # summary: [第 N 个镜头,共 M 个] {title}
    # 这里必须用**标题**、不是整段动作 —— summary 权重比 detailed_description 高,
    # 塞一整段动作进去会把它当"镜头标题"念(实测加出来的镜头 summary 变成一长串动作)。
    prompt = re.sub(
        r"\[第 \d+ 个镜头,共 \d+ 个\] .*?(?=\n\n|\Z)",
        f"[第 {idx} 个镜头,共 {total} 个] {title}",
        prompt,
        count=1,
        flags=re.DOTALL,
    )
    # detailed_description: {action}
    prompt = re.sub(
        r"detailed_description:\n.*?(?=\n\n|\Z)",
        f"detailed_description:\n{action}",
        prompt,
        count=1,
        flags=re.DOTALL,
    )
    return prompt


def _renumber_prompt_brackets(project: Project, db: Session) -> None:
    """增删镜头后,把每个镜头提示词里的「第 N 个镜头,共 M 个」编号刷成当前值。

    只动方括号里的数字,不碰动作文本(件链镜 summary 和 detailed_description
    的动作文本本来就不一样,整体替换会把件链镜的 summary 写成 detailed 的内容)。
    """
    shots = sorted(project.shots, key=lambda s: s.idx)
    total = len(shots)
    for s in shots:
        if not s.prompt:
            continue
        new_prompt = re.sub(
            r"\[第 \d+ 个镜头,共 \d+ 个\]",
            f"[第 {s.idx} 个镜头,共 {total} 个]",
            s.prompt,
            count=1,
        )
        # 七段式【镜头】第 N / M 镜 · 标题:增删镜头后同样要刷编号
        new_prompt = re.sub(
            r"^【镜头】第 \d+ / \d+ 镜",
            f"【镜头】第 {s.idx} / {total} 镜",
            new_prompt,
            count=1,
            flags=re.M,
        )
        if new_prompt != s.prompt:
            s.prompt = new_prompt
    db.commit()


@router.post(
    "/{project_id}/shots/add-general",
    response_model=ShotOut,
    status_code=status.HTTP_201_CREATED,
)
def add_general_shot(
    project_id: int,
    shot_in: GeneralShotIn,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> ShotOut:
    """添加通用分镜:复制现有通用分镜的提示词结构,替换标题/动作/镜头编号。

    通用分镜的提示词结构固定(subject_definitions + summary + retention_analysis
    + detailed_description + overall_soundscape + non_diegetic_music),entities/refs
    都一样,只是动作文本不同 —— 复制模板替换关键字段即可,不必重走 from-script 全流程。
    """
    project = _own(project_id, user, db)
    # 不检查渲染状态:添加通用分镜不应被正在进行的渲染任务阻塞

    # 找一个现有通用分镜作为模板:不带件链锚标记(anchor0/anchor_last/state_out/state_first)的镜。
    # 不能按 key 猜(s01-s05)—— 件数多时件链更长,s06/s07 还是链上镜。
    #
    # **选「道具最少」的那一镜**：模板会把 <Prop> 定义整段复制给新镜头。
    # 如果拿「化妆师补右脸」当模板，新镜头会凭空多出「粉扑」这种本镜根本没用到的道具
    # （用户报「我没有粉扑啊」）。道具最少的通用镜通常只有身上那套衣服 + 常驻配饰，
    # 复制过来最干净；新动作里提到的手机 / 平板由动作文本自己描述。
    chain_marks = ("anchor0", "anchor_last", "state_out", "state_first")
    # **必须挑一条「普通 / 自定义分镜」当模板**(options.universal=1):
    # 换装镜(整套换装 / 链上镜)的提示词是【画面】【换装】结构,没有【道具定义】这一套,
    # 拿它当模板,加出来的自定义分镜会变成一条换装镜(用户报「加的自定义分镜没有【场景】【人物】」)。
    universal = [
        s
        for s in sorted(project.shots, key=lambda x: x.idx)
        if (s.options or {}).get("universal")
        and not (s.options or {}).get("state_only")
        and not any(m in (s.options or {}) for m in chain_marks)
    ]
    candidates = universal or [
        s
        for s in sorted(project.shots, key=lambda x: x.idx)
        if not any(m in (s.options or {}) for m in chain_marks)
    ]
    if not candidates:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "没有可复制的通用分镜模板")
    template = min(
        candidates,
        key=lambda s: (len(re.findall(r"<Prop \d+>", s.prompt or "")), s.idx),
    )

    # 计算新镜的 idx/key/总数
    max_idx = (
        db.query(func.max(ProjectShot.idx)).filter_by(project_id=project_id).scalar()
        or 0
    )
    new_idx = max_idx + 1
    new_key = f"s{new_idx:02d}"
    total = max_idx + 1  # 新总数

    # 替换 prompt:换镜头编号 + 动作描述
    new_prompt = _replace_general_prompt(
        template.prompt, shot_in.title, shot_in.action, new_idx, total
    )

    # 自定义分镜的动作自带户外场景 / 自定义取景时,模板里的「纯白棚」场景段、标准【画面】段
    # 和「上下各留 15% 空白」的收口句会原样留下 —— 模型会把那些留白直接画成白框
    # (用户实测:秋日林间生成出白色边框)。这里按动作意图把这几段一起换掉。
    if wants_real_scene(shot_in.action) or "本镜不用标准取景框" in shot_in.action:
        new_prompt = re.sub(
            r"【场景】\n.*?(?=\n\n【人物】)",
            "【场景】\n- 环境按【动作】里描述的场景生成:真实空间,墙面、地面、陈设、光源都照描述来,"
            "光线自然;同一镜内场景不切换、不增减陈设,不要变成纯白影棚。",
            new_prompt,
            count=1,
            flags=re.S,
        )
        new_prompt = re.sub(
            r"\n【画面】\n.*?(?=\n\n【动作】)", "", new_prompt, count=1, flags=re.S
        )
        new_prompt = re.sub(
            r"\n- 以上动作全部在【画面】的取景范围内完成:.*?(?=\n\n【统一约束】)",
            "",
            new_prompt,
            count=1,
            flags=re.S,
        )

    # 七段式【镜头】【场景】【人物】【道具定义】【画面】【动作】【统一约束】的模板:
    # 结构本身就是要给模型看的,不能再走老的那套「精简 / 补声音段 / 补尾注」——
    # 精简会把换行和【人物】里的 <Picture 定义> 全压掉(用户报「加完只剩【镜头】」)。
    seven = "【动作】" in new_prompt

    # 通用分镜尾部统一精简:系统内置和自定义都只留「速度 + 画面」两句。
    # 机位/景别/场景/光线由动作文本自己写死,不需要系统追加整段镜头守则。
    if not seven:
        for _tm in (" 镜头:机位完全固定", " CAMERA: locked off"):
            _ti = new_prompt.find(_tm)
            if _ti >= 0:
                new_prompt = new_prompt[:_ti].rstrip()
                break
        # 模板是纯动作文本(PURE)时连声音段都没有,一并补上
        if "overall_soundscape:" not in new_prompt:
            new_prompt += "\n\noverall_soundscape:\n安静的环境底噪。"
        if "non_diegetic_music:" not in new_prompt:
            new_prompt += "\n\nnon_diegetic_music:\nNone."
        # 模板本身已经带过这段尾注(结构化的通用分镜都带)—— 再拼一次会变成两句重复的
        # 「速度 / 画面」,模型会把同一件事念两遍。只有还没有时才补。
        if not new_prompt.rstrip().endswith(TAIL_ZH_CUSTOM.strip()):
            new_prompt += TAIL_ZH_CUSTOM

    # 复制 refs 和 options(清掉 seed/anchor/state 标记 —— 通用分镜不该有)
    new_refs = list(template.refs or [])
    new_options = {
        k: v
        for k, v in (template.options or {}).items()
        if k
        not in (
            "seed",
            "anchor0",
            "anchor0_crop",
            "anchor_last",
            "state_out",
            "state_first",
            "state_only",
        )
    }
    new_options["float"] = 1
    new_options["no_wipe"] = 1

    # 长镜头(例如 10 秒 = 240 帧)如果仍按默认的 124 帧生成,后 4.8 秒只能冻结末帧。
    # 超过默认生成长度时,把生成帧数抬到 H3 的 17k+5(上限 362),再按 duration_frames 裁到目标时长。
    frames = int(shot_in.seconds * project.fps)
    if frames > settings.gen_length:
        new_options["gen_length"] = min(
            362, 5 + 17 * max(2, -(-(frames - 5) // 17))
        )

    _prompt = new_prompt if seven else concise.rule_condense(new_prompt)
    new_shot = ProjectShot(
        project_id=project_id,
        idx=new_idx,
        key=new_key,
        title=shot_in.title,
        mode=template.mode,
        duration_frames=frames,
        prompt=_prompt,
        refs=new_refs,
        options=new_options,
        enabled=True,
        status=ST_DRAFT,
    )
    db.add(new_shot)
    db.commit()
    # 总数变了(21 -> 22),把全部镜头提示词里的「共 M 个」刷成新值
    db.expire(project, ["shots"])
    _renumber_prompt_brackets(project, db)
    db.refresh(new_shot)
    return ShotOut.model_validate(new_shot)


@router.get("/{project_id}/shots/{shot_id}/final-prompt")
def shot_final_prompt(
    project_id: int,
    shot_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """查看这一镜真正发出去给模型的文案(机位 / 上一镜末帧 / 首尾帧都已算进去)。"""
    from ..pipeline import compose_prompt

    project = _own(project_id, user, db)
    shot = _own_shot(project, shot_id)
    prompt, refs, prev_frame = compose_prompt(db, project, shot)
    options = shot.options or {}
    notes = []
    warnings = []
    if options.get("camera"):
        notes.append("已注入机位")
        if options.get("anchor0"):
            warnings.append(
                "这一镜用了换装链锚图，机位角度会被首帧锚限制，换角度不一定明显"
            )
        if shot.mode in (MODE_FIRST, MODE_FIRST_LAST):
            warnings.append("首帧 / 尾帧图会锁住构图，机位角度可能只部分生效")
    if options.get("anchor0"):
        notes.append("已使用换装链锚图")
    if prev_frame:
        notes.append("已自动附上上一镜末帧")
    if shot.mode == MODE_FIRST:
        notes.append("首帧图")
    elif shot.mode == MODE_FIRST_LAST:
        notes.append("首帧 + 尾帧图")
    return {
        "prompt": prompt or "",
        "refs": len(refs),
        "mode": shot.mode,
        "notes": notes,
        "warnings": warnings,
    }


@router.get("/{project_id}/shots/{shot_id}/style-ref")
def shot_style_ref(
    project_id: int,
    shot_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """通用 / 自定义分镜自动附加的「穿着样式参照」(换装链最后一镜尾帧)。

    只在生成时临时追加,不写进 shot.refs —— 这里给前端一个只读缩略图看它到底取了哪张。
    """
    from ..pipeline import _style_ref_for

    project = _own(project_id, user, db)
    shot = _own_shot(project, shot_id)
    if not (shot.options or {}).get("universal"):
        return {"url": None, "picture": 0}
    path = _style_ref_for(project, shot)
    if not path:
        return {"url": None, "picture": 0}
    try:
        rel = Path(path).resolve().relative_to(STORAGE_DIR.resolve()).as_posix()
    except ValueError:
        return {"url": None, "picture": 0}
    return {"url": media_url(rel), "picture": len(shot.refs or []) + 1}


@router.post(
    "/{project_id}/shots/{shot_id}/render",
    response_model=RenderJobOut,
    status_code=status.HTTP_202_ACCEPTED,
)
def render_shot(
    project_id: int,
    shot_id: int,
    count: int = 1,
    seed: int | None = None,
    random_seed: bool = False,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> RenderJobOut:
    """单镜手动生成:只跑这一个镜头,产出可预览的片段,不满意可以反复重跑。

    count > 1 就是「抽卡」:一次连出 count 段,全部留在候选清单里,
    用户挑一张作为这一镜最终用的片段(见 pick_shot_clip)。

    seed:指定固定种子(结果可复现)。
    random_seed:抽卡时是否每张换随机种子。**默认 False** —— 抽卡走「默认种子 + 序号」
    (第 1 张 = 默认种子,后面依次 +1),既可复现、也不会每张一模一样;
    勾上才每张取随机种子。
    """
    project = _own(project_id, user, db)
    shot = _own_shot(project, shot_id)
    _ensure_idle(db, project)
    _ensure_no_preview()

    if not shot.prompt.strip():
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "该镜头还没有填写提示词")

    count = max(1, min(int(count or 1), 8))
    opts = {**(shot.options or {})}
    if seed is not None and int(seed) > 0:
        opts["seed"] = max(1, int(seed))
    elif not (opts.get("seed") or 0):
        opts["seed"] = DEFAULT_SEED
    if count > 1:
        # 抽卡:默认「固定种子模式」(见 pipeline._run_shot_job);勾了随机才每张随机
        opts["seed_mode"] = "random" if random_seed else "fixed"
    else:
        # 单张没给种子:换一个随机种子 —— 不然反复点出来的永远是同一段影像
        opts.pop("seed_mode", None)
        if seed is None:
            opts["seed"] = random.randint(1, 2**31 - 1)
    shot.options = opts
    shot.status = ST_QUEUED
    # 静态图镜保留当前 PNG 当底图预览,别一点生成节点就变空
    if not opts.get("static_image"):
        shot.clip_rel_path = None
    job = RenderJob(
        project_id=project.id,
        user_id=user.id,
        shot_id=shot.id,
        variants=count,
        status=ST_QUEUED,
        message=(
            f"镜头 {shot.key} 排队中"
            if count == 1
            else f"镜头 {shot.key} 抽卡 {count} 张·排队中"
        ),
    )
    db.add(job)
    db.commit()
    db.refresh(job)
    return job_out(job)


@router.post("/{project_id}/shots/{shot_id}/rerender-static", response_model=ShotOut)
async def rerender_static_shot(
    project_id: int,
    shot_id: int,
    mask: UploadFile | None = File(None),
    instruction: str = Form(""),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> ShotOut:
    """静态图分镜的「涂抹重渲」(2026-10-22):涂 mask + 可选新指令 → 覆盖该镜的 PNG。

    流程:
      · 底图 = 上一镜静态图(prev_static 那镜的 state_out);第 1 镜用人物参考图
      · 服装图 = options.garment_ref
      · instruction 留空用 shot.prompt
      · 输出覆盖 shot.clip_rel_path 和 shot.options.state_out
      · 如果这是最后一镜静态图,同步更新 project.final_rel_path
    """
    import asyncio as _asyncio

    from .imgedit import run_imgedit
    from ..pipeline import _resolve_asset_path

    project = _own(project_id, user, db)
    shot = _own_shot(project, shot_id)
    _ensure_idle(db, project)

    opts = shot.options or {}
    if not opts.get("static_image"):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, "这一镜不是静态图分镜(整链只有主链换装镜是静态)"
        )

    # 底图:**优先用这一镜自己的 PNG** —— 局部重渲是在「现在这张图」上改,
    # 涂不到的像素必须保持现在这样(拿上一镜当底图的话,涂改区外的当前这件衣服会凭空消失)。
    # 只有这一镜还没出过图时才退到上一镜 / 人物参考图。
    base_path: Path | None = None
    if shot.clip_rel_path:
        cand = STORAGE_DIR / shot.clip_rel_path
        if cand.exists():
            base_path = cand
    prev_key = (opts.get("prev_static") or "").strip()
    if base_path is None and prev_key:
        prev_shot = next(
            (s for s in project.shots if s.key == prev_key and (s.options or {}).get("static_image")),
            None,
        )
        if prev_shot is not None:
            prev_out = (prev_shot.options or {}).get("state_out") or ""
            if prev_out and Path(prev_out).exists():
                base_path = Path(prev_out)
        if base_path is None and prev_shot is not None and prev_shot.clip_rel_path:
            cand = STORAGE_DIR / prev_shot.clip_rel_path
            if cand.exists():
                base_path = cand
    if base_path is None:
        base_path = _resolve_asset_path(db, opts.get("character_ref") or "")
    if base_path is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "取不到这一镜的底图(这一镜 PNG / 上一镜 PNG / 人物参考图)")

    garment_path = _resolve_asset_path(db, opts.get("garment_ref") or "")
    if garment_path is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "取不到这一镜的服装参考图")

    mask_bytes = await mask.read() if mask is not None and mask.filename else b""
    instr = (instruction or "").strip() or (shot.prompt or "").strip()
    if not instr:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "提示词为空")

    dest_dir = STORAGE_DIR / "static_shots" / f"p{project.id}"
    dest_dir.mkdir(parents=True, exist_ok=True)

    if not comfy_client.is_up():
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "ComfyUI 没启动(8188)")

    try:
        out_path = await _asyncio.to_thread(
            run_imgedit,
            [base_path, garment_path],
            instr,
            mask_bytes,
            dest_dir,
            f"p{project.id}_s{shot.idx:02d}_re",
        )
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, f"重渲失败:{str(exc)[:300]}") from exc

    # 覆盖到 canonical 命名
    canonical = dest_dir / f"s{shot.idx:02d}.png"
    try:
        if canonical.exists():
            canonical.unlink()
        out_path.replace(canonical)
    except Exception:  # noqa: BLE001
        shutil.copyfile(out_path, canonical)

    rel = canonical.relative_to(STORAGE_DIR).as_posix()
    shot.clip_rel_path = rel
    new_opts = dict(shot.options or {})
    new_opts["state_out"] = str(canonical)
    shot.options = new_opts

    # 如果这是最后一镜静态图 → 按视频分镜的统一取景重新扩边(人物占画面高度约 70%),
    # 再更新 project.final_rel_path
    static_shots = sorted(
        [s for s in project.shots if (s.options or {}).get("static_image")],
        key=lambda s: s.idx,
    )
    if static_shots and static_shots[-1].id == shot.id:
        from ..config import derive_gen_size
        from ..pipeline import _fit_full_body_frame
        _fit_full_body_frame(
            canonical, derive_gen_size(project.out_width, project.out_height)
        )
        project.final_rel_path = rel

    db.commit()
    db.refresh(shot)
    return shot_out(shot)


@router.post("/{project_id}/shots/{shot_id}/pick", response_model=ShotOut)
def pick_shot_clip(
    project_id: int,
    shot_id: int,
    payload: ShotPickIn,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> ShotOut:
    """从抽卡候选里挑一张,设为这一镜最终用的片段(整片合成/预览都用它)。"""
    project = _own(project_id, user, db)
    shot = _own_shot(project, shot_id)
    _ensure_idle(db, project)

    rel = payload.clip
    if rel.startswith("/media/"):
        rel = rel[len("/media/") :]
    if rel not in [c for c in (shot.candidates or []) if c]:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, "这张候选不在本镜头的候选清单里"
        )

    shot.clip_rel_path = rel
    shot.status = ST_DONE
    db.commit()
    db.refresh(shot)
    return shot_out(shot)


@router.post("/{project_id}/shots/{shot_id}/delete-candidate")
def delete_candidate(
    project_id: int,
    shot_id: int,
    payload: ShotPickIn,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> ShotOut:
    """删除抽卡候选里的某一张:从候选清单移除,并删掉对应文件。"""
    project = _own(project_id, user, db)
    shot = _own_shot(project, shot_id)
    _ensure_idle(db, project)

    rel = payload.clip
    if rel.startswith("/media/"):
        rel = rel[len("/media/") :]
    cands = [c for c in (shot.candidates or []) if c]
    if rel not in cands:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, "这张候选不在本镜头的候选清单里"
        )
    # 不允许删除当前正选用的那张
    if rel == shot.clip_rel_path:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, "不能删除当前正选用的片段,请先切换到别的候选"
        )

    shot.candidates = [c for c in cands if c != rel]
    _unlink(rel)
    db.commit()
    db.refresh(shot)
    return shot_out(shot)


@router.post(
    "/{project_id}/render",
    response_model=RenderJobOut,
    status_code=status.HTTP_202_ACCEPTED,
)
def start_render(
    project_id: int,
    redo: bool = False,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> RenderJobOut:
    project = _own(project_id, user, db)
    _ensure_idle(db, project)
    _ensure_no_preview()

    enabled = [s for s in project.shots if s.enabled and s.prompt.strip()]
    if not enabled:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, "至少需要一个启用且填写了提示词的镜头"
        )

    # 不动 s.clip_rel_path:已经有片段的镜头,渲染时直接复用,不重新生成。
    # (提示词/分辨率/帧率被改动时片段会被清掉,所以留着就说明还能用)
    # 断点续传:只把「还没有片段」的镜头标成排队,已生成的保持「已完成」——
    # 这样中断后再点一次,就只补没做的镜头,界面上也能一眼看出还剩哪几个。
    for s in enabled:
        if redo:
            # 「全部重跑」:不管有没有片段都重做。旧片段文件先留着(万一中途失败还能找回来),
            # 只是不再被引用;新任务会写新的文件名。
            s.clip_rel_path = None
            s.candidates = []
            s.status = ST_QUEUED
        elif not s.clip_rel_path:
            s.status = ST_QUEUED
    if redo:
        project.final_rel_path = None
    job = RenderJob(
        project_id=project.id,
        user_id=user.id,
        status=ST_QUEUED,
        message="全部重跑:排队中" if redo else "排队中",
    )
    db.add(job)
    project.status = ST_QUEUED
    db.commit()
    db.refresh(job)
    return job_out(job)


@router.post(
    "/{project_id}/preview-images",
    response_model=PreviewJobOut,
    status_code=status.HTTP_202_ACCEPTED,
)
def start_preview_images(
    project_id: int,
    shot_id: int | None = None,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> PreviewJobOut:
    """每个镜头先出一张静帧预览图(SDXL 纯文生图),快速看构图/景别,不用等整片跑完。

    传 shot_id 就只出这一镜的(镜头详情里的「出预览图」按钮);
    注意:这不是成片用的 MiniMax H3,只是构图参考 —— 人物长相和衣服纹理都不会跟素材一致。
    """
    project = _own(project_id, user, db)
    _ensure_idle(db, project)

    # 全站有渲染在跑就先不抢显卡(16G 卡同时跑会炸)
    busy = (
        db.query(RenderJob)
        .filter(RenderJob.status.in_((ST_QUEUED, ST_RUNNING)))
        .first()
    )
    if busy is not None:
        raise HTTPException(
            status.HTTP_409_CONFLICT, "有渲染任务正在跑,等它结束再出预览图"
        )

    shots = [s for s in project.shots if s.enabled and (s.prompt or "").strip()]
    if shot_id is not None:
        shots = [s for s in shots if s.id == shot_id]
        if not shots:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST, "该镜头不存在、没启用或还没写提示词"
            )
    if not shots:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "没有启用、且写了提示词的镜头")
    return PreviewJobOut(**preview.start(project.id, [s.id for s in shots]))


@router.get("/{project_id}/preview-images", response_model=PreviewJobOut)
def preview_images_status(
    project_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> PreviewJobOut:
    _own(project_id, user, db)
    return PreviewJobOut(**preview.status(project_id))


@router.delete("/{project_id}/film", response_model=ProjectOut)
def delete_film(
    project_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> ProjectOut:
    """删掉合成好的整片(不动各分镜自己的片段)。"""
    project = _own(project_id, user, db)
    _ensure_idle(db, project)

    _unlink(project.final_rel_path)
    project.final_rel_path = None
    project.status = ST_QUEUED
    db.commit()
    db.refresh(project)
    return project_out(project)


@router.delete("/{project_id}/shots/{shot_id}/clip", response_model=ShotOut)
def delete_shot_clip(
    project_id: int,
    shot_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> ShotOut:
    """删掉单个分镜已生成的视频(含全部抽卡候选),镜头回到「未生成」。"""
    project = _own(project_id, user, db)
    shot = _own_shot(project, shot_id)
    _ensure_idle(db, project)

    for rel in set([shot.clip_rel_path, *(shot.candidates or [])]):
        _unlink(rel)
    shot.clip_rel_path = None
    shot.candidates = []
    shot.status = ST_QUEUED
    db.commit()
    db.refresh(shot)
    return shot_out(shot)


@router.get("/{project_id}/jobs", response_model=list[RenderJobOut])
def list_jobs(
    project_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[RenderJobOut]:
    project = _own(project_id, user, db)
    return [job_out(j) for j in sorted(project.jobs, key=lambda j: j.id, reverse=True)]
