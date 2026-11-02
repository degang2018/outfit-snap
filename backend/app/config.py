"""Application settings, all env-driven so the same code runs on SQLite (dev) or MySQL."""
from __future__ import annotations

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent          # web/backend


def _load_dotenv(path: Path) -> None:
    """Minimal .env loader (KEY=VALUE), no extra dependency."""
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip())


_load_dotenv(BASE_DIR / ".env")

STORAGE_DIR = Path(os.getenv("STORAGE_DIR", str(BASE_DIR / "storage")))
ASSET_DIR = STORAGE_DIR / "assets"
RENDER_DIR = STORAGE_DIR / "renders"
CLIP_DIR = STORAGE_DIR / "clips"
PREVIEW_DIR = STORAGE_DIR / "previews"


def _default_db_url() -> str:
    """SQLite by default. Point DATABASE_URL at MySQL for production, e.g.
    mysql+pymysql://user:pass@127.0.0.1:3306/outfit_video?charset=utf8mb4
    """
    return os.getenv("DATABASE_URL", f"sqlite:///{(BASE_DIR / 'app.db').as_posix()}")


def _list(env: str, default: str) -> list[str]:
    return [x.strip() for x in os.getenv(env, default).split(",") if x.strip()]


# 「默认种子」—— 和前端 index.html 的 DEFAULT_SEED 保持一致。
# 抽卡默认走这个种子(第 1 张 = 它,后面依次 +1),勾了「随机种子」才每张随机。
DEFAULT_SEED = int(os.getenv("DEFAULT_SEED", "639795015"))


class Settings:
    app_name = "AI Outfit Video Studio"
    api_prefix = "/api"

    database_url = _default_db_url()

    jwt_secret = os.getenv("JWT_SECRET", "dev-secret-change-me")
    jwt_algorithm = "HS256"
    access_token_ttl_hours = int(os.getenv("ACCESS_TOKEN_TTL_HOURS", "168"))
    cors_origins = _list("CORS_ORIGINS", "http://localhost:5173,http://127.0.0.1:5173")

    # --- ComfyUI ---
    comfy_api = os.getenv("COMFY_API", "http://127.0.0.1:8188")
    # ComfyUI output 目录。必须在 .env 里显式设置,否则找不到生成的图片/视频。
    # 示例: COMFY_OUTPUT_DIR=C:/ComfyUI/ComfyUI/output
    comfy_output_dir = Path(os.getenv("COMFY_OUTPUT_DIR", str(STORAGE_DIR / "comfy_output")))

    # generation vs delivery resolution (16GB card cannot render 1080x1920 directly)
    gen_width = int(os.getenv("GEN_WIDTH", "576"))
    gen_height = int(os.getenv("GEN_HEIGHT", "1024"))
    out_width = int(os.getenv("OUT_WIDTH", "1080"))
    out_height = int(os.getenv("OUT_HEIGHT", "1920"))
    fps = int(os.getenv("FPS", "24"))
    gen_length = int(os.getenv("GEN_LENGTH", "124"))     # H3 renders ~124 frames per take
    # 生成完的片段用 ESRGAN 超分到交付分辨率(1080x1920),再拼成片。
    # 比直接 Lanczos 放大清晰很多;代价是每个镜头多 ~45 秒。关掉写 UPSCALE_VIDEO=0。
    upscale_video = os.getenv("UPSCALE_VIDEO", "1") == "1"
    # ComfyUI/models/upscale_models 里已有的模型:
    #   realesr-general-x4v3.pth   小、快(推荐日常用)
    #   4x_NMKD-Siax_200k.pth      更锐、更慢
    upscale_model = os.getenv("UPSCALE_MODEL", "realesr-general-x4v3.pth")
    # 超分结果混回原图的比例(0~1):ESRGAN 会明显加锐、出白边/塑料脸,
    # 混一部分「普通 Lanczos 放大」回去能压掉锐化感。0=纯超分,1=完全不超分。
    upscale_blend = float(os.getenv("UPSCALE_BLEND", "0.5"))
    # 超分一次送进模型多少帧。0 = 按帧尺寸自动算(峰值卡在 ~2.5GB 内存);
    # 4 倍放大会把整批帧一次性变成一个大张量,144 帧 576x1024 要 16GB → OOM(实测炸过)。
    upscale_chunk = int(os.getenv("UPSCALE_CHUNK", "0"))
    # 「换装镜」的整段帧数。90 帧(3.75s)实测模型会把"落下"摊满整段、变成 2 秒慢速下降;
    # 56 帧(2.3s) 能把动作压紧。必须是 17k+5 的值(H3 要求),上限约 362。
    float_shot_length = int(os.getenv("FLOAT_SHOT_LENGTH", "56"))
    shot_timeout = int(os.getenv("SHOT_TIMEOUT", "1800"))

    # ComfyUI model file names
    clip_name = os.getenv("CLIP_NAME", "qwen3vl_32b_heretic_minimax_h3_nvfp4.safetensors")
    unet_name = os.getenv("UNET_NAME", "minimax_h3_fl2va_pruned_int8_convrot.safetensors")
    lora_name = os.getenv("LORA_NAME", "minimax_h3_fl2v_turbo_8step_v1.0_comfyui_bf16.safetensors")
    # 可选第二 LoRA:叠在 turbo 主 LoRA 后面(用来加"锐化/细节修复"这类适配器)。
    # 空 = 不加载。强度默认 0.8(Alissonerdx 的 lms 锐化 LoRA 社区实测 0.8 皮肤最自然)。
    lora_name_2 = os.getenv("LORA_NAME_2", "").strip()
    lora_strength_2 = float(os.getenv("LORA_STRENGTH_2", "0.8"))
    # 第二遍 v2v 细节修复(锐化 LoRA 的官方用法):主片段生成完,把它当 guide 再采一遍。
    # 每镜多一次生成时间;单镜可以用 options.detail_pass=0 关掉。
    detail_pass = os.getenv("DETAIL_PASS", "0") == "1"
    detail_pass_steps = int(os.getenv("DETAIL_PASS_STEPS", "8"))
    detail_pass_prompt = os.getenv(
        "DETAIL_PASS_PROMPT",
        "Enhance this video with sharp, crisp details while preserving a natural photorealistic appearance.")
    vae_video = os.getenv("VAE_VIDEO", "minimax_h3_video_vae_fp16.safetensors")
    vae_audio = os.getenv("VAE_AUDIO", "minimax_h3_audio_vae_fp32.safetensors")
    sigmas = os.getenv("SIGMAS", "1.0000, 0.9730, 0.9231, 0.7500, 0.5500, 0.3014, 0.0000")

    # --- 速度/显存旋钮(改 .env 就生效,不用动代码) ---
    # 参考图尺寸:max = 按 2048px 短边编码(身份最保真,但参考图 token 每步都参与采样 → 慢好几倍);
    # match = 缩到"生成分辨率的像素量级"(快很多,身份保真略降)。实测:多一张 max 参考图 ≈ 多 1 分钟。
    # 两个档位是给「物体镜 / 普通镜」分开用的(见 pipeline._generate_shot):
    #   物体镜(衣服要抛起/飞入/悬浮)用 ref_image_size —— 衣服在空中,糊了最明显,走保真档;
    #   其余普通镜用 ref_image_size_fast —— 实测快 2 倍,成品肉眼基本没差。
    # 想全局统一,把两个设成一样即可(都写 max = 全保真,都写 match = 全提速)。
    ref_image_size = os.getenv("REF_IMAGE_SIZE", "max").strip().lower()
    ref_image_size_fast = os.getenv("REF_IMAGE_SIZE_FAST", "match").strip().lower()
    # 悬浮变装:人物参考图先缩小留白再喂给模型。人物参考图是"紧裁全身照"(人几乎占满整张图),
    # 模型会把构图一起抄走 → 生成出来的人顶天立地把画面塞满(实测 93% 画面高,参考片只有 67%),
    # 而且这句写在提示词里也压不住。1.0 = 关闭。
    float_ref_scale = float(os.getenv("FLOAT_REF_SCALE", "0.67"))
    # 悬浮变装:**道具 / 服装**类参考图单独一档,比人物图缩得更狠。
    # 商品图是"衣服摊开占满整幅"的特写裁片,模型会把它的占屏比例一起抄走 ——
    # 实测(人物/道具都用 0.5 时)飞进来的衣服量出来是 45% 画面宽 × 73% 画面高(正确是 26%×23%),
    # 而人物图缩了会影响脸和身材,所以只缩道具那几张(见 pipeline._ref_scales)。
    float_ref_scale_prop = float(os.getenv("FLOAT_REF_SCALE_PROP", "0.33"))
    # 鞋子商品图单独再小一档:鞋图是「俯视一双鞋占满整幅」,0.33 时仍会被整幅抄进画面,
    # 脚下凭空多出两只巨大的拖鞋(实测 teset999 的 #15 端咖啡杯)。
    float_ref_scale_shoe = float(os.getenv("FLOAT_REF_SCALE_SHOE", "0.15"))
    # 小件配饰(墨镜 / 帽子 / 项链…)也压到这一档:整幅商品图交上去,模型会把它当成
    # "场景里的物件"再画一遍(用户 2026-10-23 p349 第 12 镜:脚边凭空多出一副墨镜)。
    float_ref_scale_small = float(os.getenv("FLOAT_REF_SCALE_SMALL", "0.15"))
    # 悬浮变装:跨镜人物大小归一(成片后期,不动生成)。
    # 每个换装镜/展示镜都按「人物高度占画面 X + 头顶离上沿 Y」重新取景 —— 实测同一套素材
    # 生成出来背心镜 0.812、长裤镜 0.701(差 15%),并排看就是"人忽大忽小"。
    # 目标值对齐抖音参考片:人物约占画面高度 70%、头顶到画面上沿约 15%,四周留白更多。
    float_align = os.getenv("FLOAT_ALIGN", "1") == "1"
    float_align_height = float(os.getenv("FLOAT_ALIGN_HEIGHT", "0.70"))
    float_align_top = float(os.getenv("FLOAT_ALIGN_TOP", "0.15"))
    # 状态图重新取景(只动「落盘的锚图」这一张静帧,不逐帧搬视频):
    # 换装链的每一镜都拿上一镜的状态图当首/尾帧硬锚,所以只要链头那张是「人 70% 高、头顶 15%」,
    # 后面的镜头会照着抄,整条片子天然就是小人物 —— 也不用后期逐帧缩放(会左右晃)。
    # 实测:人物参考图四周加白边并不能让模型照抄取景(打底展示镜仍是 85% 高、头顶 9%)。
    state_align = os.getenv("STATE_ALIGN", "1") == "1"
    # 状态图落盘前压掉皮肤的色块 / 彩噪(只动这张静态锚图,不动视频)。
    # 状态图会被下一镜当锚图 / 参考图,皮肤瑕疵不清理就会沿链一路传播。
    state_skin_clean = os.getenv("STATE_SKIN_CLEAN", "1") == "1"
    # 状态图的肤色锁回链头 S0(只做逐通道肤色增益,不动大小 / 取景)。
    # 每换一次衣服模型都会把肤色画深一点,不锁会沿链一路加深。
    state_tone_lock = os.getenv("STATE_TONE_LOCK", "1") == "1"
    # 暗部轻提亮:纯白棚拍里黑色面料(黑裙 / 黑裤)常被压成一片死黑、纹理全丢。
    # 只在低亮度区往上抬这个量(0=关闭),白背景和肤色基本不动。
    shadow_lift = float(os.getenv("SHADOW_LIFT", "18"))
    # 站姿自动挑选:只对「要产状态图、又没有首帧锚」的展示镜生效(链头 / 加穿型)。
    # 每次最多抽 N 张,量脚踝以下「脚跟→脚尖」的水平偏角(0=正前),取分最低的;
    # 分 <= stance_max_deg 就直接收,不再多跑。
    stance_auto = os.getenv("STANCE_AUTO", "1") == "1"
    stance_candidates = int(os.getenv("STANCE_CANDIDATES", "3"))
    stance_max_deg = float(os.getenv("STANCE_MAX_DEG", "8"))
    # 换装镜头(换的是上装/外套)把脚踝以下锁死成末帧:模型在衣服飞进来的那 1 秒里
    # 会自己把脚转成外八(实测 s05 第 4~20 帧 9~14.7°),冻住脚就没了。
    # 换下装的镜头不启用(裤脚/裙摆会经过脚踝,冻住会卡住旧衣服)。
    feet_lock = os.getenv("FEET_LOCK", "1") == "1"
    # 换装镜「水平锁位」:只做逐帧左右平移,把人物中心钉回第一帧(不动大小 / 取景)。
    # 模型在衣服飞进来时会把整个人横着滑一段(实测 316 s03 头中心漂 43px)。
    lateral_lock = os.getenv("LATERAL_LOCK", "1") == "1"
    # 状态图的「脸」锁死成链头那张(S0):每换一次衣服模型都会把脸重画一遍,几跳之后
    # 嘴就开始糊/变形(实测 S2 的嘴)。状态图里头部位置/大小是固定的,直接把 S0 的头
    # 按头部对齐贴回来,嘴就永远是第一张那个嘴。
    face_lock = os.getenv("FACE_LOCK", "1") == "1"
    # 状态图的「脚」对齐到链头 S0:同一双鞋落在同一像素。
    # 换装镜只有首/尾两个硬锚、中间没有锚 —— 两张锚图的脚**横向**差几像素,模型就会演一次
    # 「换重心 / 挪半步」把脚从 A 挪到 B(用户 2026-10-20 报「人会挪步」)。
    # 只做水平平移;纵向由状态图 70% 高 / 头顶 15% 锁死(展示图顶部留白必须一致)。
    state_feet_align = os.getenv("STATE_FEET_ALIGN", "1") == "1"
    # 换装链「首尾帧硬锚」总开关:关掉后换装链上的镜头不再拿上一镜状态图当首/尾帧,
    # 也不再把上一镜末帧挂成第 0 帧锚 —— 每镜都从人物图 + 服装参考图重新起手。
    # 用来验证「肤色越换越深」是不是沿锚图链传下去的。
    chain_anchor = os.getenv("CHAIN_ANCHOR", "1") == "1"
    # 换装链「取景对齐」:每镜落盘前整段用一个缩放值,把人物缩到「70% 高 / 头顶 15% / 脚底 15%」,
    # 这样硬切到下一镜时头和脚的位置一致(背景纯白,补边看不出来)。0=关。
    frame_match = os.getenv("FRAME_MATCH", "1") == "1"
    # 分块只是削显存峰值、不改结果;显存够就调小换速度
    ff_chunks = int(os.getenv("FF_CHUNKS", "4"))                    # MiniMaxChunkFeedForward 切几块
    ff_seq_threshold = int(os.getenv("FF_SEQ_THRESHOLD", "4096"))   # token 超过它才切
    attn_head_chunks = int(os.getenv("ATTN_HEAD_CHUNKS", "4"))      # 注意力按 head 分几组算

    # background render worker
    worker_enabled = os.getenv("WORKER_ENABLED", "1") == "1"
    worker_poll_seconds = float(os.getenv("WORKER_POLL_SECONDS", "3"))
    # 看门狗:running 的任务超过这么久没有心跳,就当成僵尸任务自动清掉并释放项目
    job_idle_timeout = int(os.getenv("JOB_IDLE_TIMEOUT", "1200"))    # 20 分钟
    watchdog_interval = float(os.getenv("WATCHDOG_INTERVAL", "30"))

    # --- 成片增强(只在拼接成片时做,不重新生成镜头) ---
    film_grade = os.getenv("FILM_GRADE", "1") == "1"                 # 调色:提对比/加饱和/压暗部/暗角
    film_push = os.getenv("FILM_PUSH", "1") == "1"                   # 每个镜头轻微推近,画面"活"起来
    film_push_amount = float(os.getenv("FILM_PUSH_AMOUNT", "0.05"))  # 推镜幅度(0.05 = 5%)
    film_flash = os.getenv("FILM_FLASH", "0") == "1"                 # 镜头之间插几帧闪白(风格化)
    film_flash_frames = int(os.getenv("FILM_FLASH_FRAMES", "2"))
    film_speed = float(os.getenv("FILM_SPEED", "1.0"))               # 整片变速;>1 变快(成片会相应变短)
    # 悬浮变装:相邻两个「状态镜」之间加一次过渡 —— 从上往下刷一层软边蒙版,刷过之后
    # 就是下一套衣服。只在**前后两镜都是悬浮变装的全身镜**时生效(状态镜链),别的镜头之间照旧硬切。
    # 关掉写 FILM_WIPE=0。
    film_wipe = os.getenv("FILM_WIPE", "1") == "1"
    film_wipe_frames = int(os.getenv("FILM_WIPE_FRAMES", "8"))       # 过渡占几帧(24fps 下 8 帧 ≈ 0.33 秒)
    film_wipe_soft = float(os.getenv("FILM_WIPE_SOFT", "0.25"))      # 软边宽度(占画面高的比例)
    # 「先穿好 → 让衣服自己飞走」拍法:这一镜生成出来之后自动倒放。
    # 好处:衣服的外观由**第一帧**决定 —— 那一帧模型是照着商品图画的,所以准;
    # 倒放之后就是"衣服从画外飞来、落定即商品图那一件"。关掉写 FLOAT_REVERSE_TAKE=0。
    float_reverse_take = os.getenv("FLOAT_REVERSE_TAKE", "1") == "1"

    # --- 预览图:用 SDXL 按每个镜头的提示词先出一张图,提前看构图/景别,不用等整片跑完。
    # 纯文生图(不锁脸),只是构图参考,人物长相和衣服纹理都不会跟素材一致。
    preview_ckpt = os.getenv("PREVIEW_CKPT", "SDXL 1.0\\chinese\\wdr_v10.safetensors")
    preview_width = int(os.getenv("PREVIEW_WIDTH", "768"))
    preview_height = int(os.getenv("PREVIEW_HEIGHT", "1344"))
    preview_steps = int(os.getenv("PREVIEW_STEPS", "26"))
    preview_cfg = float(os.getenv("PREVIEW_CFG", "6.5"))
    preview_sampler = os.getenv("PREVIEW_SAMPLER", "dpmpp_2m")
    preview_scheduler = os.getenv("PREVIEW_SCHEDULER", "karras")
    preview_timeout = int(os.getenv("PREVIEW_TIMEOUT", "600"))

    # --- 生成偏好:衣服的「开合」(扣子 / 拉链)---
    # 用户要求"全局控制上衣把扣子扣上"(实测:黑色开衫被模型穿成敞开、还露出蕾丝内衣)。
    # 这一档是**全局默认**,只在「这件没单独指定」时生效:
    #   件级覆盖(素材库 ai_close)> 项目级覆盖(一键换装件列表 close)> 这一档
    # **默认 auto = 什么都不写**,让模型照参考图自己决定(参考图上扣着就扣着、敞开就敞开)——
    # 想强制扣上/敞开再显式设:
    #   closed(扣上 / 拉链拉满) / half(半开) / open(敞开) / auto(不写,默认)
    # 只对**有前襟的件**生效:吊带 / 背心 / 裤 / 鞋 / 包 / 套头款(T恤 / 毛衣)自动跳过。
    garment_close_default = os.getenv("GARMENT_CLOSE_DEFAULT", "auto").strip().lower()

    # --- LLM:把剧本切成镜头(默认走本机 Ollama,且强制 CPU 推理) ---
    llm_enabled = os.getenv("LLM_ENABLED", "1") == "1"
    llm_api_style = os.getenv("LLM_API_STYLE", "ollama")        # ollama | openai
    llm_base_url = os.getenv("LLM_BASE_URL", "http://127.0.0.1:11434")
    llm_api_key = os.getenv("LLM_API_KEY", "")
    llm_model = os.getenv("LLM_MODEL", "qwen3:4b")
    # 读图(上传服装图 -> 自动出剧本)用的视觉模型。DeepSeek 的视觉模型是 deepseek-flash,
    # 旧的 deepseek-chat 只吃文本;留空则退回 llm_model。
    llm_vision_model = os.getenv("LLM_VISION_MODEL", "deepseek-flash")
    llm_timeout = int(os.getenv("LLM_TIMEOUT", "300"))
    # 0 = 纯 CPU。显卡被 ComfyUI 占满时,走 GPU 会 cudaMalloc 失败
    llm_num_gpu = int(os.getenv("LLM_NUM_GPU", "0"))
    # 镜头描述(action)与提示词正文用哪种语言:zh | en
    # 注意只影响正文;subject_definitions / retention_analysis 等小节名保留英文,
    # 那是 H3 参考生成的固定结构字段,不属于自然语言内容。
    llm_prompt_lang = os.getenv("LLM_PROMPT_LANG", "zh").strip().lower()


settings = Settings()


# 生成分辨率档位:键 = 短边,值 = 写进 .env 的 (GEN_WIDTH, GEN_HEIGHT)。
# 真正起作用的是**短边**(见 derive_gen_size),这里给 9:16 的直观值;
# 换别的比例(方形/横屏)也照样按成片比例自动换算。
#
# **长宽必须能被 32 整除** —— MiniMax H3 会把 latent 按 32 一格切块,除不尽就会
# 在 SamplerCustomAdvanced 里报形状错(实测:432÷32=13.5 直接崩,报
# "shape '[1, 24, 1, 1, 24, 2, 13, 2]' is invalid for input ...";带首尾锚图的镜头必现,
# 不带锚图的状态图恰好侥幸能跑,所以这个坑很隐蔽)。原来的 432×768 / 720×1280 都不合法。
GEN_PRESETS: dict[int, tuple[int, int]] = {
    448: (448, 800),
    576: (576, 1024),
    640: (640, 1152),
    704: (704, 1248),
}

REF_SIZE_TIERS = ("match", "max")


def apply_speed(gen_short: int | None = None, ref_size: str | None = None) -> dict[str, str]:
    """调出片速度的两把旋钮(生成短边 / 参考图档位):立刻生效 + 写回 .env 下次启动还在。

    返回真正需要写进 .env 的键值。速度相关的设置是**全局**的(所有项目共用)。
    """
    updates: dict[str, str] = {}
    if gen_short:
        pair = GEN_PRESETS.get(int(gen_short))
        if pair:
            w, h = pair
            settings.gen_width, settings.gen_height = w, h
            updates["GEN_WIDTH"], updates["GEN_HEIGHT"] = str(w), str(h)
    if ref_size in REF_SIZE_TIERS:
        settings.ref_image_size = ref_size
        settings.ref_image_size_fast = ref_size
        updates["REF_IMAGE_SIZE"] = ref_size
        updates["REF_IMAGE_SIZE_FAST"] = ref_size
    if updates:
        _write_env(updates)
    return updates


def _write_env(updates: dict[str, str]) -> None:
    """把这几个键写回 .env:已有行就地替换,没有的追加;其它行(含密钥/注释)原样保留。"""
    path = BASE_DIR / ".env"
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    seen: set[str] = set()
    out: list[str] = []
    for line in lines:
        s = line.strip()
        key = s.split("=", 1)[0].strip() if (s and not s.startswith("#") and "=" in s) else ""
        if key in updates:
            out.append(f"{key}={updates[key]}")
            seen.add(key)
        else:
            out.append(line)
    for key, value in updates.items():
        if key not in seen:
            out.append(f"{key}={value}")
    path.write_text("\n".join(out) + "\n", encoding="utf-8")


def derive_gen_size(out_width: int, out_height: int) -> tuple[int, int]:
    """按成片比例算出一个 H3 能跑的生成分辨率。

    16GB 显卡直接跑 1080x1920 会爆显存,所以按「短边 = min(gen_width, gen_height)」等比缩放,
    再对齐到 32 的倍数。默认 1080x1920 -> 576x1024,与既有出片参数一致;换成别的比例也不会被拉伸。
    """
    base = min(settings.gen_width, settings.gen_height) or 576
    short = min(out_width, out_height) or base
    scale = base / short

    def _snap(v: int) -> int:
        # 必须对齐到 **32**:H3 的 latent 按 32 切块(见 GEN_PRESETS 上面的注释),
        # 只对齐 16 会得到 640×1136 这种高度(1136÷32=35.5),带锚图的镜头直接报形状错。
        return max(256, int(round(v * scale / 32)) * 32)

    return _snap(out_width), _snap(out_height)


for _d in (STORAGE_DIR, ASSET_DIR, RENDER_DIR, CLIP_DIR, PREVIEW_DIR):
    _d.mkdir(parents=True, exist_ok=True)
