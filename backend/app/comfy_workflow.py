"""ComfyUI workflow builders.

Three shot modes, matching the local MiniMax H3 install:

* ref         -> MiniMaxH3ReferenceToVideo   (1..N reference images, Autogrow input)
* first       -> MiniMaxH3ImageToVideo       (first_frame)
* first_last  -> MiniMaxH3ImageToVideo       (first_frame + last_frame)
* text        -> MiniMaxH3ImageToVideo       (no frame images)

The reference-input trick from the CLI pipeline is preserved: Autogrow inputs MUST be
submitted as flat dotted keys ("ref_images.ref_image_0"), a nested dict is silently dropped.

Plus one unrelated builder: build_preview_workflow() —— 用 SDXL(+可选 FaceID 锁脸)
按镜头提示词先出一张图,用来提前看构图/景别,不用等整片跑完。
"""

from __future__ import annotations

import os
from pathlib import Path

from .comfy_client import upload_image
from .config import settings
from .models import MODE_FIRST, MODE_FIRST_LAST, MODE_REF

KITCHEN_ATTENTION = "comfy kitchen attention"

# 出预览图用的负面词(照搬 sdxl_outfits_workflow.json 里那套)
PREVIEW_NEGATIVE = (
    "ng_deepnegative_v1_75t, (worst quality:2), (low quality:2), lowres, bad anatomy, bad hands, "
    "deformed, disfigured, poorly drawn, mutated, extra limbs, missing limbs, cartoon, anime, "
    "3d render, cgi, octane render, unreal engine, video game, plastic, digital art, illustration, "
    "painting, oversaturated, text, watermark, signature, logo, jpeg artifacts, distorted face, "
    "cloned face, extra fingers, missing fingers, deformed hands, fused fingers, blurry, soft, unclear"
)


def _loader_nodes() -> dict:
    """Shared model graph: the four accelerator nodes get the 16GB card to ~100% utilisation."""
    nodes = {
        "1": {
            "class_type": "CLIPLoader",
            "inputs": {
                "clip_name": settings.clip_name,
                "type": "minimax",
                "device": "default",
            },
        },
        "2": {
            "class_type": "UNETLoader",
            "inputs": {"unet_name": settings.unet_name, "weight_dtype": "default"},
        },
        "3": {
            "class_type": "ModelAttentionBackend",
            "inputs": {"model": ["2", 0], "attention": KITCHEN_ATTENTION},
        },
        "4": {
            "class_type": "MiniMaxChunkFeedForward",
            "inputs": {
                "model": ["3", 0],
                "chunks": settings.ff_chunks,
                "seq_threshold": settings.ff_seq_threshold,
            },
        },
        "5": {
            "class_type": "MiniMaxLowVRAMAttention",
            "inputs": {"model": ["4", 0], "head_chunks": settings.attn_head_chunks},
        },
        "6": {
            "class_type": "LoraLoaderModelOnly",
            "inputs": {
                "model": ["5", 0],
                "lora_name": settings.lora_name,
                "strength_model": 1.0,
            },
        },
        "7": {"class_type": "VAELoader", "inputs": {"vae_name": settings.vae_video}},
        "8": {"class_type": "VAELoader", "inputs": {"vae_name": settings.vae_audio}},
    }
    # 可选的第二 LoRA(锐化 / 细节修复):串在主 LoRA 后面。采样器改用 61 这个输出。
    if settings.lora_name_2:
        nodes["61"] = {
            "class_type": "LoraLoaderModelOnly",
            "inputs": {
                "model": ["6", 0],
                "lora_name": settings.lora_name_2,
                "strength_model": settings.lora_strength_2,
            },
        }
    return nodes


def _image_node(wf: dict, node_id: str, path: str) -> None:
    """图片节点:先把本地文件传到 ComfyUI 的 input 目录,再用文件名引用。

    以前这里写的是 VHS_LoadImagePath + 本机绝对路径,只在「后端和 ComfyUI 同机」
    时成立;换成远端 ComfyUI 后会直接报「Invalid file path」拒绝整条工作流。
    """
    wf[node_id] = {
        "class_type": "LoadImage",
        "inputs": {"image": upload_image(Path(path))},
    }


def _sampler_nodes(
    cond_node: str, seed: int, prefix: str, fps: int, latent_node: str | None = None
) -> dict:
    # cond_node 给 guider(latent 经 AddGuide 后仍是原节点);latent_node 单独给 latent_image。
    latent_node = latent_node or cond_node
    model_node = "61" if settings.lora_name_2 else "6"
    return {
        "30": {
            "class_type": "RandomNoise",
            "inputs": {"noise_seed": seed, "control_after_generate": "fixed"},
        },
        "40": {
            "class_type": "BasicGuider",
            "inputs": {"model": [model_node, 0], "conditioning": [cond_node, 0]},
        },
        "50": {"class_type": "KSamplerSelect", "inputs": {"sampler_name": "euler"}},
        "60": {"class_type": "ManualSigmas", "inputs": {"sigmas": settings.sigmas}},
        "70": {
            "class_type": "SamplerCustomAdvanced",
            "inputs": {
                "noise": ["30", 0],
                "guider": ["40", 0],
                "sampler": ["50", 0],
                "sigmas": ["60", 0],
                "latent_image": [latent_node, 1],
            },
        },
        "80": {
            "class_type": "VAEDecode",
            "inputs": {"samples": ["70", 0], "vae": ["7", 0]},
        },
        "90": {
            "class_type": "VHS_VideoCombine",
            "inputs": {
                "images": ["80", 0],
                "frame_rate": fps,
                "loop_count": 0,
                "filename_prefix": prefix,
                "format": "video/h264-mp4",
                "pingpong": False,
                "save_output": True,
            },
        },
    }


def build_workflow(
    *,
    mode: str,
    prompt: str,
    seed: int,
    prefix: str,
    length: int | None = None,
    width: int | None = None,
    height: int | None = None,
    fps: int | None = None,
    ref_images: list[str] | None = None,
    first_frame: str | None = None,
    last_frame: str | None = None,
    ref_image_size: str | None = None,
    anchor_frame0: str | None = None,
    anchor_frame_last: str | None = None,
    mid_anchor: list[tuple[str, int]] | tuple[str, int] | None = None,
    look_image: str | None = None,
) -> dict:
    wf = _loader_nodes()
    length = length or settings.gen_length
    common = {
        "clip": ["1", 0],
        "vae": ["7", 0],
        "prompt": prompt,
        "width": width or settings.gen_width,
        "height": height or settings.gen_height,
        "length": length,
    }

    if mode == MODE_REF:
        ref_inputs: dict = {}
        _look = os.path.normpath(look_image) if look_image else ""
        for i, path in enumerate(ref_images or []):
            # 「最终穿搭参考(尾帧)」用**单独的加载节点**(id 9 + 中文标题)——
            # 它在图里一眼能找到,不会和商品图 / 人物图混在一起;仍按原顺序占 ref_image_N 槽位,
            # 所以提示词里的 <Picture N> 编号不变。
            if _look and os.path.normpath(path) == _look:
                node_id = "9"
                _image_node(wf, node_id, path)
                wf[node_id]["_meta"] = {"title": "最终穿搭参考(尾帧)"}
            else:
                node_id = str(10 + i)
                _image_node(wf, node_id, path)
            ref_inputs[f"ref_images.ref_image_{i}"] = [node_id, 0]
        wf["20"] = {
            "class_type": "MiniMaxH3ReferenceToVideo",
            "inputs": {
                **common,
                "audio_vae": ["8", 0],
                "ref_image_size": ref_image_size or settings.ref_image_size,
                **ref_inputs,
            },
        }
        cond_node = "20"
        if anchor_frame0:
            # 把锚点图硬钉在第 0 帧(实拍截帧),让本镜从上一镜结束的画面接着演 ——
            # 参考图模式锁人太软(参考图一多人物就被重编),这里用 keyframe 把它钉死。
            _image_node(wf, "18", anchor_frame0)
            wf["25"] = {
                "class_type": "MiniMaxH3AddGuide",
                "inputs": {
                    "positive": ["20", 0],
                    "vae": ["7", 0],
                    "latent": ["20", 1],
                    "image": ["18", 0],
                    "frame_idx": 0,
                },
            }
            cond_node = "25"
        if anchor_frame_last:
            # 再钉最后一帧(首尾双硬锚) —— fl1h 已验证的换装链配方:
            # ref 模式负责认人/认道具,双端 AddGuide 负责钉死开始和结束的样子。
            _image_node(wf, "19", anchor_frame_last)
            wf["26"] = {
                "class_type": "MiniMaxH3AddGuide",
                "inputs": {
                    "positive": [cond_node, 0],
                    "vae": ["7", 0],
                    "latent": ["20", 1],
                    "image": ["19", 0],
                    "frame_idx": -1,
                },
            }
            cond_node = "26"
        mids = list(mid_anchor or [])
        if mids and isinstance(mids[0], str):  # 允许只传一条 (path, idx)
            mids = [mids]
        for j, m in enumerate(mids):
            mp_, fidx = m[0], int(m[1])
            if not mp_:
                continue
            # 中间帧路标(可挂多条):飞入的**尺寸/位置**由这些帧钉死 ——
            # 模型对"飞进来的衣服"有自己的先验(永远画成一大块),光靠文字和参考图压不住;
            # 但**一条不够**(它会在路标之后又开始画巨幅),所以要挂一串、每张形变都不一样,
            # 模型在它们之间插值,衣服才会一路变换形状而不是一坨静止的板。
            _image_node(wf, str(71 + j), mp_)
            wf[str(81 + j)] = {
                "class_type": "MiniMaxH3AddGuide",
                "inputs": {
                    "positive": [cond_node, 0],
                    "vae": ["7", 0],
                    "latent": ["20", 1],
                    "image": [str(71 + j), 0],
                    "frame_idx": fidx,
                },
            }
            cond_node = str(81 + j)
    else:
        ins = dict(common)
        if first_frame:
            _image_node(wf, "11", first_frame)
            ins["first_frame"] = ["11", 0]
        if mode == MODE_FIRST_LAST and last_frame:
            _image_node(wf, "12", last_frame)
            ins["last_frame"] = ["12", 0]
        wf["20"] = {"class_type": "MiniMaxH3ImageToVideo", "inputs": ins}
        cond_node = "20"

    wf.update(
        _sampler_nodes(cond_node, seed, prefix, fps or settings.fps, latent_node="20")
    )
    return wf


def build_detail_workflow(
    *,
    video_path: str,
    prompt: str,
    seed: int,
    prefix: str,
    width: int,
    height: int,
    length: int,
    fps: int,
) -> dict:
    """第二遍 v2v 细节修复(锐化 LoRA 的官方用法)。

    把**已经生成的片段**当 guide 喂回模型(同分辨率、不缩放),再用第二 LoRA 采一遍:
      VHS_LoadVideoPath -> ImageScale -> MiniMaxH3AddGuide(frame 0) -> 8 步采样 -> 解码。
    和主生成的区别只有输入条件:主生成从参考图 + 文字起手,这里从"自己刚生成的画面"起手,
    所以 LoRA 的细节修复能力才真正吃得到。
    """
    wf = _loader_nodes()
    model_node = "61" if settings.lora_name_2 else "6"
    wf.update(
        {
            "200": {
                "class_type": "VHS_LoadVideoPath",
                "inputs": {
                    "video": video_path,
                    "force_rate": 0,
                    "custom_width": 0,
                    "custom_height": 0,
                    "frame_load_cap": int(length),
                    "skip_first_frames": 0,
                    "select_every_nth": 1,
                },
            },
            "201": {
                "class_type": "ImageScale",
                "inputs": {
                    "image": ["200", 0],
                    "upscale_method": "lanczos",
                    "width": int(width),
                    "height": int(height),
                    "crop": "disabled",
                },
            },
            "202": {
                "class_type": "EmptyMiniMaxH3LatentAV",
                "inputs": {
                    "width": int(width),
                    "height": int(height),
                    "length": int(length),
                },
            },
            "203": {
                "class_type": "CLIPTextEncode",
                "inputs": {"clip": ["1", 0], "text": prompt},
            },
            "204": {
                "class_type": "MiniMaxH3AddGuide",
                "inputs": {
                    "positive": ["203", 0],
                    "latent": ["202", 0],
                    "vae": ["7", 0],
                    "image": ["201", 0],
                    "frame_idx": 0,
                },
            },
            "205": {
                "class_type": "BasicGuider",
                "inputs": {"model": [model_node, 0], "conditioning": ["204", 0]},
            },
            "206": {
                "class_type": "KSamplerSelect",
                "inputs": {"sampler_name": "euler"},
            },
            "207": {
                "class_type": "BasicScheduler",
                "inputs": {
                    "model": [model_node, 0],
                    "scheduler": "simple",
                    "steps": int(settings.detail_pass_steps),
                    "denoise": 1.0,
                },
            },
            "208": {
                "class_type": "RandomNoise",
                "inputs": {"noise_seed": int(seed), "control_after_generate": "fixed"},
            },
            "209": {
                "class_type": "SamplerCustomAdvanced",
                "inputs": {
                    "noise": ["208", 0],
                    "guider": ["205", 0],
                    "sampler": ["206", 0],
                    "sigmas": ["207", 0],
                    "latent_image": ["202", 0],
                },
            },
            "210": {
                "class_type": "VAEDecode",
                "inputs": {"samples": ["209", 0], "vae": ["7", 0]},
            },
            "211": {
                "class_type": "VHS_VideoCombine",
                "inputs": {
                    "images": ["210", 0],
                    "frame_rate": int(fps),
                    "loop_count": 0,
                    "filename_prefix": prefix,
                    "format": "video/h264-mp4",
                    "pingpong": False,
                    "save_output": True,
                },
            },
        }
    )
    return wf


def build_preview_workflow(
    *,
    prompt: str,
    seed: int,
    prefix: str,
    width: int | None = None,
    height: int | None = None,
) -> dict:
    """SDXL 纯文生图出一张预览图。

    只是「提前看构图/景别」用的,和成片(MiniMax H3)不是同一个模型 ——
    人物长相和衣服纹理都不会跟素材一致,看的是取景范围、有没有切头、构图对不对。
    """
    wf: dict = {
        "100": {
            "class_type": "CheckpointLoaderSimple",
            "inputs": {"ckpt_name": settings.preview_ckpt},
        },
        "110": {
            "class_type": "CLIPTextEncode",
            "inputs": {"clip": ["100", 1], "text": prompt},
        },
        "111": {
            "class_type": "CLIPTextEncode",
            "inputs": {"clip": ["100", 1], "text": PREVIEW_NEGATIVE},
        },
        "112": {
            "class_type": "EmptyLatentImage",
            "inputs": {
                "width": width or settings.preview_width,
                "height": height or settings.preview_height,
                "batch_size": 1,
            },
        },
        "130": {
            "class_type": "KSampler",
            "inputs": {
                "seed": seed,
                "steps": settings.preview_steps,
                "cfg": settings.preview_cfg,
                "sampler_name": settings.preview_sampler,
                "scheduler": settings.preview_scheduler,
                "denoise": 1.0,
                "model": ["100", 0],
                "positive": ["110", 0],
                "negative": ["111", 0],
                "latent_image": ["112", 0],
            },
        },
        "140": {
            "class_type": "VAEDecode",
            "inputs": {"samples": ["130", 0], "vae": ["100", 2]},
        },
        "150": {
            "class_type": "SaveImage",
            "inputs": {"images": ["140", 0], "filename_prefix": prefix},
        },
    }
    return wf
