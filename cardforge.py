#!/usr/bin/env python3
"""cardforge —— 本地卡牌制作工具

核心流程：输入一张图 → rembg+BiRefNet 抠图 → 产出卡牌资产（front/foreground）
         → 生成卡牌定义 card.json → 可选 2D 合成预览卡图

两种调用方式：
  1. 命令行（供脚本/其他工具调用）：
       cardforge 封面.png --name "莲之空双人组"
       cardforge 封面.png --name "xxx" --style full-bleed
       cardforge 封面.png --name xxx --json
  2. Web 界面（python webapp.py）：可视化操作

退出码：0=成功，2=参数错误，1=处理失败（供外部工具判断）
"""
from __future__ import annotations

import argparse
import base64
import json
import re
import shutil
import sys
from pathlib import Path

from PIL import Image

from carddef import build_card_def, save_card_def
from compositor import CARD_H, CARD_W, compose_card, frame_glow_boost
from engine import (
    DEFAULT_MODEL,
    SUPPORTED_MODELS,
    MattingEngine,
    create_engine,
    get_api_config,
)

PROJECT_ROOT = Path(__file__).resolve().parent
EFFECTS_DIR = PROJECT_ROOT / "assets" / "effects"        # 自定义特效 JSON（名称.json → {"def": {...}}）
EFFECT_IMG_DIR = PROJECT_ROOT / "assets" / "effects-images"  # 特效图像/精灵帧图

# 内置特效预设（与 assets/effects/*.json 的自定义特效同构；可在任何位置选择触发）
BUILTIN_EFFECTS = {
    "love": {
        "name": "love",
        "count": 4,
        "image": "text:💗",
        "lifetime": 2.4,
        "lifetimeRandom": 1.2,
        "delay": 4.5,
        "delayRandom": 3.5,
        "relOffsetY": 1.1,
        "speedX": 0.3,
        "speedY": 0.7,
        "spin": 1.5,
        "startScale": 0.55,
        "endScale": 1.1,
        "fadeIn": 0.25,
        "fadeOut": 0.6,
        "alpha": 1,
    },
    "sparkle": {
        "name": "sparkle",
        "count": 1,
        "image": "text:✨",
        "lifetime": 0.9,
        "lifetimeRandom": 0.5,
        "delay": 1.8,
        "delayRandom": 2.5,
        "startScale": 0.3,
        "endScale": 1.1,
        "fadeIn": 0.12,
        "fadeOut": 0.45,
        "alpha": 1,
    },
    "petal": {
        "name": "petal",
        "count": 3,
        "image": "text:🌸",
        "lifetime": 3.2,
        "lifetimeRandom": 1.5,
        "delay": 2.5,
        "delayRandom": 2.2,
        "relOffsetY": -1.4,
        "speedX": 0.3,
        "speedY": 0.55,
        "spin": 2,
        "startScale": 0.5,
        "endScale": 0.95,
        "fadeIn": 0.3,
        "fadeOut": 0.7,
        "alpha": 1,
    },
    "star": {
        "name": "star",
        "count": 2,
        "image": "text:⭐",
        "lifetime": 1.6,
        "lifetimeRandom": 1.2,
        "delay": 1.2,
        "delayRandom": 2,
        "startScale": 0.15,
        "endScale": 0.5,
        "spin": 0.8,
        "fadeIn": 0.15,
        "fadeOut": 0.6,
        "alpha": 1,
    },
    "note": {
        "name": "note",
        "count": 2,
        "image": "text:🎵",
        "lifetime": 2.2,
        "lifetimeRandom": 1,
        "delay": 3,
        "delayRandom": 2.5,
        "relOffsetY": 1.2,
        "speedX": 0.25,
        "speedY": 0.5,
        "spin": -1,
        "startScale": 0.6,
        "endScale": 1,
        "fadeIn": 0.2,
        "fadeOut": 0.6,
        "alpha": 1,
    },
    "bubble": {
        "name": "bubble",
        "count": 3,
        "image": "text:🫧",
        "lifetime": 2.6,
        "lifetimeRandom": 1.3,
        "delay": 1.5,
        "delayRandom": 2,
        "relOffsetY": 1.3,
        "speedX": 0.2,
        "speedY": 0.35,
        "startScale": 0.3,
        "endScale": 1.1,
        "fadeIn": 0.2,
        "fadeOut": 0.5,
        "alpha": 0.85,
    },
    "snow": {
        "name": "snow",
        "count": 5,
        "image": "text:❄️",
        "lifetime": 3.5,
        "lifetimeRandom": 1.8,
        "delay": 1.2,
        "delayRandom": 1.5,
        "relOffsetY": -1.5,
        "speedX": 0.3,
        "speedY": 0.5,
        "spin": 1.2,
        "startScale": 0.35,
        "endScale": 0.75,
        "fadeIn": 0.4,
        "fadeOut": 0.5,
        "alpha": 1,
    },
    "coin": {
        "name": "coin",
        "count": 2,
        "image": "text:🪙",
        "lifetime": 2.4,
        "lifetimeRandom": 1,
        "delay": 2.8,
        "delayRandom": 2.4,
        "relOffsetY": -1.2,
        "speedX": 0.2,
        "speedY": 0.7,
        "spin": 6,
        "startScale": 0.55,
        "endScale": 0.8,
        "fadeIn": 0.15,
        "fadeOut": 0.45,
        "alpha": 1,
    },
}

_POS_ALIAS = {
    "随机": "random", "上": "top", "下": "bottom", "左": "left", "右": "right",
    "中央": "center", "中": "center", "左上": "topLeft", "右上": "topRight",
    "左下": "bottomLeft", "右下": "bottomRight",
}


def parse_effect_spec(text: str) -> dict:
    """解析 '名称@位置' → {name, posType, posX, posY}。位置：random/top/bottom/.../x:y 或中文别名。"""
    name, pos = text, "random"
    if "@" in text:
        name, pos = text.rsplit("@", 1)
    pos = _POS_ALIAS.get(pos, pos)
    if ":" in pos:
        try:
            x, y = pos.split(":")
            return {"name": name, "posType": "custom", "posX": float(x), "posY": float(y)}
        except ValueError:
            pass
    return {"name": name, "posType": pos}


def list_custom_effects() -> list[str]:
    """返回已保存的自定义特效名称（assets/effects/*.json）。"""
    if not EFFECTS_DIR.exists():
        return []
    return sorted(p.stem for p in EFFECTS_DIR.glob("*.json"))


def resolve_effect_def(name: str) -> dict:
    """取特效定义：自定义优先，其次内置预设。找不到抛 ValueError。"""
    f = EFFECTS_DIR / f"{name}.json"
    if f.exists():
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return dict(data.get("def") or data)
        except (OSError, ValueError):
            pass
    if name in BUILTIN_EFFECTS:
        return dict(BUILTIN_EFFECTS[name])
    raise ValueError(f"未知特效: {name}（可新建或使用内置: {', '.join(BUILTIN_EFFECTS)}）")


def _resolve_effects(effects) -> list[dict]:
    """把实例列表 [{"name", "posType", "posX", "posY"} | "name@pos"] 解析为引擎实例列表。

    返回 [{"name":..., "def": {...}, "pos": {...}}]。
    """
    out = []
    for item in effects or []:
        if isinstance(item, str):
            item = parse_effect_spec(item)
        elif isinstance(item, dict) and isinstance(item.get("name"), str) and "@" in item["name"]:
            parsed = parse_effect_spec(item["name"])
            item = {**item, "name": parsed["name"], "posType": item.get("posType") or parsed["posType"]}
        name = item.get("name") or ""
        if not name:
            continue
        pos = {"type": item.get("posType") or "random"}
        if pos["type"] == "custom":
            try:
                pos["x"] = min(1.0, max(0.0, float(item.get("posX", 0.5))))
                pos["y"] = min(1.0, max(0.0, float(item.get("posY", 0.5))))
            except (TypeError, ValueError):
                pos = {"type": "random"}
        defn = resolve_effect_def(name)
        out.append({"name": name, "def": defn, "pos": pos})
    return out


def _materialize_effect_images(effects: list[dict], out_dir: Path) -> list[dict]:
    """把特效引用的图片复制到 out_dir/effects/，def.image 改写为相对路径，保证单 HTML 自包含。组合特效递归处理。"""
    effect_img_dir = out_dir / "effects"
    effect_img_dir.mkdir(parents=True, exist_ok=True)

    def _walk(defn: dict):
        if not isinstance(defn, dict):
            return
        src = defn.get("image") or ""
        if src.startswith("text:"):
            pass
        elif src:
            rel = src[6:] if src.startswith("image:") else src
            src_path = Path(rel)
            if not src_path.is_absolute():
                candidates = [EFFECT_IMG_DIR / src_path, PROJECT_ROOT / "assets" / src_path]
                src_path = next((c for c in candidates if c.exists()), src_path)
            if src_path.exists():
                target = effect_img_dir / src_path.name
                target.write_bytes(src_path.read_bytes())
                defn["image"] = "image:effects/" + src_path.name
        for sub in defn.get("subs") or []:
            if isinstance(sub, dict):
                _walk(sub.get("def"))

    for item in effects or []:
        _walk(item.get("def") if isinstance(item, dict) else None)
    return effects


def slugify(text: str) -> str:
    s = re.sub(r"[^\w\u4e00-\u9fff-]+", "-", text.strip().lower())
    s = re.sub(r"-+", "-", s).strip("-")
    return s or "card"


def parse_size(text: str) -> tuple[int, int]:
    w, h = text.lower().split("x")
    return int(w), int(h)


_LAYER_DIRS = {
    "background": "backgrounds",
    "face": "faces",
    "frame": ("frames", "frames-text"),  # 边框可能放在普通边框或文本框边框目录
    "seal": "seals",
    "back": "backs",
}


def _load_layer(path: str | None, kind: str | None = None) -> str | None:
    """把素材路径解析为项目内绝对路径（支持相对/绝对，自动补全 assets/<类型>/ 前缀）。"""
    if not path or path.lower() in ("无", "none", "default"):
        return None
    p = Path(path)
    if not p.is_absolute() and not p.exists():
        candidates = [PROJECT_ROOT / "assets" / p]
        subs = _LAYER_DIRS.get(kind or "")
        if isinstance(subs, str):
            subs = [subs]
        for sub in subs or []:
            candidates.append(PROJECT_ROOT / "assets" / sub / p)
        for candidate in candidates:
            if candidate.exists():
                return str(candidate)
    return str(p) if p.exists() else None


def export_card(card_id: str, etype: str = "image", out_path: str | Path | None = None) -> Path:
    """导出已有卡牌：image → 2D 合成图 card.png；html → 自包含单文件（同目录图片 base64 内嵌）。

    卡 id 即 assets/output/ 下的目录名；out_path 缺省输出到当前目录 <卡id>.png/.html。
    返回导出文件路径；卡牌缺失/类型非法抛 ValueError。
    """
    cid = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "", (card_id or "").strip())
    d = PROJECT_ROOT / "assets" / "output" / cid
    if not cid or not d.is_dir():
        raise ValueError(f"找不到卡牌「{card_id}」（assets/output/ 下无此目录）")
    if etype == "image":
        p = d / "card.png"
        if not p.exists():
            raise ValueError(f"卡牌「{cid}」缺少 2D 合成图 card.png")
        out = Path(out_path) if out_path else (Path.cwd() / f"{cid}.png")
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(p.read_bytes())
        return out
    if etype == "html":
        p = d / "card.html"
        if not p.exists():
            raise ValueError(f"卡牌「{cid}」缺少 3D 网页卡 card.html")
        html = p.read_text(encoding="utf-8")
        _mime = {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg", "webp": "image/webp"}

        def _embed(m: re.Match) -> str:
            name = m.group(2)
            fp = d / name
            if not fp.is_file():
                return m.group(0)
            q = m.group(1)
            b64 = base64.b64encode(fp.read_bytes()).decode("ascii")
            mime = _mime.get(Path(name).suffix.lower().lstrip("."), "application/octet-stream")
            return f"{q}data:{mime};base64,{b64}{q}"

        html = re.sub(r"""(['"])([A-Za-z0-9_.-]+\.(?:png|jpg|jpeg|webp))\1""", _embed, html)
        out = Path(out_path) if out_path else (Path.cwd() / f"{cid}.html")
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(html, encoding="utf-8")
        return out
    raise ValueError(f"不支持的导出类型：{etype}")


def make_card(
    image_path,
    *,
    name: str | None = None,
    slug: str | None = None,
    style: str = "transparent",
    engine_name: str = "local",
    model: str = DEFAULT_MODEL,
    engine: MattingEngine | None = None,
    title: str | None = None,
    desc: str | None = None,
    text_type: str = "none",
    text_pos: dict | None = None,
    background: str | None = None,
    face: str | None = None,
    frame: str | None = None,
    seal: str | None = None,
    seal_frame: str | None = None,
    animation: str = "none",
    effects: list | None = None,
    back: str | None = None,
    card_size: str = f"{CARD_W}x{CARD_H}",
    compose: bool = True,
    subject_over_frame: bool = False,
    subject_outline: bool = False,
    mask_clip: bool = True,
    scale: float = 1.0,
    glow_name: str | None = None,
    glow_strength: float = 1.0,
    adaptive: bool = True,
    adaptive_mode: int = 1,
    progress=None,
) -> dict:
    """核心制作流程（CLI 与 Web 界面共用）。

    progress: 可选回调 progress(step, total, msg)，step 从 0 到 total，用于界面显示进度。
    返回结果 dict；出错抛 ValueError/RuntimeError。
    """
    image_path = Path(image_path)
    if not image_path.exists():
        raise ValueError(f"找不到输入图像 {image_path}")

    if style not in ("transparent", "full-bleed"):
        raise ValueError(f"未知风格: {style}")
    if desc and text_type == "none":
        text_type = "transparent"  # 有描述但未指定类型时按特殊文本型处理

    # 特效实例：优先取 effects 列表；兼容旧 animation 单特效参数
    if effects:
        effect_instances = _resolve_effects(effects)
    elif animation and animation != "none":
        effect_instances = _resolve_effects([parse_effect_spec(animation)])
    else:
        effect_instances = []

    name = name or image_path.stem
    card_id = slug or slugify(name)
    out_dir = PROJECT_ROOT / "assets" / "output" / card_id
    out_dir.mkdir(parents=True, exist_ok=True)

    total_steps = 5 if compose else 4
    step = 0

    def log(msg):
        if progress:
            progress(step, total_steps, msg)

    try:
        source = Image.open(image_path)
        source.load()
    except Exception as e:
        raise ValueError(f"无法读取图像 {image_path}: {e}") from e

    # 1. 抠图（模型为 none 时跳过，整图直接作为卡面素材）
    #    在原始图像上进行，避免裁剪先砍掉主体边缘；抠图结果随后与卡面同步适配。
    if model == "none":
        log("不使用抠图，整图直接作为卡面素材…")
        foreground = source.convert("RGBA")
        engine_desc = "不使用抠图"
    else:
        log(f"抠图中（模型: {model}）…")
        if engine is None:
            engine = create_engine(engine_name, model)
        foreground = engine.remove_background(source)
        engine_desc = engine.describe()
    step += 1

    # 上传图按所选自适应方式等比缩放+居中裁剪/透明画布贴图（原图与抠图同步处理）。
    # 文本型卡的实际显示范围缩小为卡图区（卡顶到文本区顶部 y1），主体按剩余范围判定；
    # 底图（front）恒整幅铺满。
    # 仅文本型（boxed）主体按卡图区（剩余显示范围）判定（无论是否已输入描述）；无文本型/特殊文本型整幅显示
    text_adapt = text_type == "boxed"
    adapt_limit = 0.70
    if text_pos and all(k in text_pos for k in ("x1", "x2", "y1", "y2")):
        try:
            adapt_limit = min(1.0, max(0.05, float(text_pos["y1"])))
        except (TypeError, ValueError):
            pass
    adapt_h = max(1, round(CARD_H * adapt_limit))

    source_orig = source.convert("RGBA")
    source = _fit_card(source, CARD_W, CARD_H, adaptive=True, mode=1)
    # 整幅卡面：主体适配必须与底图（恒 cover 整幅）完全一致，才能保证抠出主体
    # 恰好遮盖在底图原主体位置上（视差效果）；方式2（contain）或关闭自适应会
    # 让主体变换与底图不一致，导致错位
    if style == "full-bleed":
        adaptive, adaptive_mode = True, 1
    foreground = _fit_card(
        foreground, CARD_W, adapt_h if text_adapt else CARD_H,
        adaptive=adaptive, mode=adaptive_mode,
    )
    # 整幅卡面+未使用抠图：裁剪后的整图即底图，无独立主体层。
    # 前景保存为透明空图，3D 主体层（含影子）不显示，避免两层相同图像叠加错位
    if style == "full-bleed" and model == "none":
        foreground = Image.new("RGBA", foreground.size, (0, 0, 0, 0))

    # 2. 保存资产
    log("保存卡牌资产…")
    src_copy = out_dir / "source.png"
    src_copy.write_bytes(image_path.read_bytes())
    # 整幅+抠图：忽略背景素材（原图做底），主体悬浮/描边仍生效
    full_bleed_subject = style == "full-bleed" and model != "none"
    # 整幅+抠图+文本型：背景跟随主体（front 垂直居中裁切区与主体同变换，主体精确遮盖背景
    # 原主体），文本区留白垫虚化背景底；2D 合成与 3D 卡网页同步启用
    follow_bg = style == "full-bleed" and full_bleed_subject and text_adapt
    # 整幅+抠图+文本型：生成与主体同几何的背景跟随图（从原始源图按主体相同的 cover 适配，
    # 任意源图比例下主体都能像素级遮盖背景原主体；文本框让位后背景随主体同步缩放/位移）
    bg_follow_rel = None
    if follow_bg:
        _fit_card(source_orig, CARD_W, foreground.height, adaptive=True, mode=1).save(
            out_dir / "bg_follow.png")
        bg_follow_rel = "bg_follow.png"
    bg_path = None if full_bleed_subject else _load_layer(background, "background")
    front_img = build_front(source, foreground, style, bg_path)
    front_img.save(out_dir / "front.png")
    foreground.save(out_dir / "foreground.png")
    # 主体白色描边（贴纸边）：透明主体卡与整幅+抠图卡启用时生成
    outline_rel = None
    if subject_outline and (style == "transparent" or full_bleed_subject):
        from compositor import make_outline

        ol = make_outline(foreground)
        if ol is not None:
            ol.save(out_dir / "outline.png")
            outline_rel = "outline.png"
    step += 1

    # 3. 卡牌定义
    log("生成卡牌定义…")
    frame_path = _load_layer(frame, "frame")
    seal_path = _load_layer(seal, "seal")
    # 边框卡封"与卡封一致"：与卡牌卡封使用同一素材（只记录一次）
    seal_frame_same = seal_frame == "same"
    if seal_frame_same:
        seal_frame = seal
    seal_frame_path = _load_layer(seal_frame, "seal")
    layers = {
        "background": _load_layer(background, "background"),
        "face": _load_layer(face, "face"),
        "frame": frame_path,
        "seal": seal_path,
        "seal_frame": seal_frame_path,
    }
    text = {"title": title, "description": desc, "type": text_type, "pos": text_pos}
    back = _load_layer(back, "back") or "assets/backs/sample-1.png"
    card = build_card_def(
        card_id=card_id,
        name=name,
        style=style,
        source=str(src_copy),
        front=str(out_dir / "front.png"),
        foreground=str(out_dir / "foreground.png"),
        engine=engine_desc,
        back=back,
        layers=layers,
        text=text,
        extra={"card_size": card_size, "composed": compose, "animation": animation,
               "subject_over_frame": subject_over_frame, "subject_outline": subject_outline,
               "mask_clip": mask_clip,
               "seal_name": Path(seal_path).name if seal_path else None,
               "seal_strength_front": 0.945, "seal_strength_back": 0.5775,
               "seal_frame_name": Path(seal_frame_path).name if seal_frame_path else None,
               "seal_frame_strength_front": 0.8, "seal_frame_strength_back": 0.5,
               "seal_frame_same": seal_frame_same,
               "card_scale": scale,
               "glow_name": glow_name,
               "glow_strength": glow_strength},
    )
    save_card_def(card, PROJECT_ROOT / "cards" / f"{card_id}.json")
    step += 1

    # 4. 2D 合成预览
    composed_path = None
    if compose:
        log("合成预览卡图…")
        size = parse_size(card_size)
        composed = compose_card(
            foreground if (style == "transparent" or text_adapt or full_bleed_subject) else None,
            style=style,
            background=_load_layer(background, "background"),
            face=_load_layer(face, "face") or (str(image_path) if style == "full-bleed" else None),
            frame=_load_layer(frame, "frame"),
            seal=_load_layer(seal, "seal"),
            seal_frame=_load_layer(seal_frame, "seal"),
            title=title,
            description=desc,
            text_type=text_type,
            text_area=text_pos,
            subject_over_frame=subject_over_frame,
            subject_outline=subject_outline,
            mask_clip=mask_clip,
            size=size,
            front=str(out_dir / "front.png"),
            content_scale=scale,
            face_scales=style == "full-bleed",
            subject_full_bleed=full_bleed_subject,
            follow_bg=follow_bg,
            follow_bg_src=str(out_dir / "bg_follow.png") if bg_follow_rel else None,
        )
        composed_path = out_dir / "card.png"
        composed.save(composed_path)
        step += 1

    # 5. 3D 卡网页（自包含单 HTML，双击即开）
    log("生成 3D 卡网页…")
    back_src = _load_layer(back, "back") or (PROJECT_ROOT / "assets" / "backs" / "sample-1.png")
    back_copy = out_dir / "back.png"
    Image.open(back_src).convert("RGB").save(back_copy)
    from webcard import build_card_html

    _materialize_effect_images(effect_instances, out_dir)
    web_path = out_dir / "card.html"

    # 边框/卡封复制到输出目录，保证 card.html 自包含（本地双击与 HTTP 访问均可用）
    frame_path = _load_layer(frame, "frame")
    seal_path = _load_layer(seal, "seal")
    seal_frame_path = _load_layer(seal_frame, "seal")
    frame_rel = seal_rel = seal_frame_rel = None
    if frame_path:
        shutil.copy(frame_path, out_dir / "frame.png")
        frame_rel = "frame.png"
    if seal_path:
        shutil.copy(seal_path, out_dir / "seal.png")
        seal_rel = "seal.png"
    if seal_frame_path:
        shutil.copy(seal_frame_path, out_dir / "seal_frame.png")
        seal_frame_rel = "seal_frame.png"
    # 边框内区域蒙版：整卡内容仅绘制在边框外围以内（3D shader 裁剪，与 2D 一致）
    interior_rel = None
    if frame_path:
        from compositor import save_frame_interior_mask

        try:
            save_frame_interior_mask(Image.open(frame_path), out_dir / "interior.png")
            interior_rel = "interior.png"
        except Exception:
            interior_rel = None
    # 未开启浮于边框时：边框按主体包围盒缩放（3D 卡，浏览器端计算包围盒）
    web_path.write_text(
        build_card_html(name, "front.png", "foreground.png", "back.png", effects=effect_instances,
                        frame_rel=frame_rel,
                        seal_rel=seal_rel,
                        seal_name=Path(seal_path).name if seal_path else None,
                        seal_frame_rel=seal_frame_rel,
                        seal_frame_name=Path(seal_frame_path).name if seal_frame_path else None,
                        description=desc, text_type=text_type, text_pos=text_pos,
                        subject_over_frame=subject_over_frame,
                        round_foreground=True,
                        frame_fit_subject=(style != "full-bleed"),
                        outline_rel=outline_rel,
                        content_scale=scale,
                        face_scales=style == "full-bleed",
                        interior_rel=interior_rel,
                        mask_clip=mask_clip,
                        glow_name=glow_name,
                        glow_strength=glow_strength,
                        glow_boost=frame_glow_boost(frame_path) if frame_path else 1.0,
                        follow_bg=follow_bg,
                        bg_follow_rel=bg_follow_rel),
        encoding="utf-8",
    )

    step += 1
    log("完成")
    return {
        "ok": True,
        "id": card_id,
        "name": name,
        "style": style,
        "engine": engine_desc,
        "dir": str(out_dir),
        "source": str(src_copy),
        "front": str(out_dir / "front.png"),
        "foreground": str(out_dir / "foreground.png"),
        "card_json": str(PROJECT_ROOT / "cards" / f"{card_id}.json"),
        "composed": str(composed_path) if composed_path else None,
        "webcard": str(web_path),
        "effects": [{"name": e["name"], "pos": e["pos"]} for e in effect_instances],
    }


def _center_crop_ratio(im: Image.Image, ratio: float) -> Image.Image:
    """按目标宽高比居中裁剪，保持原图不变形（不拉伸）。"""
    w, h = im.size
    cur = w / h
    if abs(cur - ratio) < 1e-3:
        return im
    if cur > ratio:
        new_w = round(h * ratio)
        x = (w - new_w) // 2
        return im.crop((x, 0, x + new_w, h))
    new_h = round(w / ratio)
    y = (h - new_h) // 2
    return im.crop((0, y, w, y + new_h))


def _fit_card(im: Image.Image, w: int, h: int, adaptive: bool = True, mode: int = 1) -> Image.Image:
    """上传图按所选自适应方式处理到目标尺寸 (w, h)，不变形。

    adaptive=True 时按上传图与目标尺寸的大小/比例判定：
      mode=1（整幅判定）：图像宽于目标时按高度缩放（裁左右），窄于目标时按宽度缩放
          （裁上下），恒铺满（cover），与整幅卡面一致；
      mode=2（透明主体判定）：横图按宽度缩放、纵图/方图按高度缩放；另一方向不足目标时
          居中贴到透明画布（contain，主体完整不裁切），超出时居中裁剪铺满。
    adaptive=False（最原始）：不做任何判定，直接拉伸填满目标尺寸（可能变形）。
    """
    if not adaptive:
        return im.convert("RGBA").resize((w, h), Image.LANCZOS)
    ratio = w / h
    cur = im.width / im.height
    if mode == 1:
        if cur > ratio:
            scale = h / im.height
        else:
            scale = w / im.width
        nw = max(1, round(im.width * scale))
        nh = max(1, round(im.height * scale))
        scaled = im.convert("RGBA").resize((nw, nh), Image.LANCZOS)
        x, y = (nw - w) // 2, (nh - h) // 2
        return scaled.crop((x, y, x + w, y + h))
    if cur > 1.0:  # 横向长方形 → 宽与目标一致
        scale = w / im.width
    else:  # 纵向长方形或正方形 → 高与目标一致
        scale = h / im.height
    nw = max(1, round(im.width * scale))
    nh = max(1, round(im.height * scale))
    scaled = im.convert("RGBA").resize((nw, nh), Image.LANCZOS)
    if nw >= w and nh >= h:
        x, y = (nw - w) // 2, (nh - h) // 2
        return scaled.crop((x, y, x + w, y + h))
    canvas = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    canvas.alpha_composite(scaled, ((w - nw) // 2, (h - nh) // 2))
    return canvas


def build_front(source: Image.Image, foreground: Image.Image, style: str, background_path: str | None) -> Image.Image:
    """生成 3D 应用底图层 front（恒整卡尺寸）。

    full-bleed（整幅卡面）：用户选择背景层时背景作底（文本型卡的主体区外露背景），
        未选背景时整幅原图即底图；
    transparent：选了背景用背景（等比裁剪铺满），未选背景用深色底。
    """
    w, h = CARD_W, CARD_H
    if background_path is not None:
        bg = Image.open(background_path).convert("RGBA")
        scale = max(w / bg.width, h / bg.height)
        bg = bg.resize((max(1, round(bg.width * scale)), max(1, round(bg.height * scale))), Image.LANCZOS)
        x, y = (bg.width - w) // 2, (bg.height - h) // 2
        return bg.crop((x, y, x + w, y + h))
    if style == "full-bleed":
        return source.convert("RGBA")  # source 已按整幅 cover 适配
    return Image.new("RGBA", (w, h), (14, 11, 9, 255))  # 无背景时用深色底


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="cardforge",
        description="本地卡牌制作工具：输入一张图，抠图并产出卡牌资产（可被其他工具调用）。",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("image", nargs="?", default=None, help="输入图像路径（--export 导出模式可省略）")
    parser.add_argument("--export", default=None, metavar="CARD_ID",
                        help="导出已有卡牌（不生成新卡）；卡 id 即 assets/output/ 下的目录名")
    parser.add_argument("--export-type", choices=["image", "html"], default="image",
                        help="导出类型：image=2D 合成图（card.png）；html=自包含单文件（图片 base64 内嵌，可离线双击打开）")
    parser.add_argument("--out", default=None, metavar="PATH",
                        help="导出文件保存路径（默认当前目录下 <卡id>.png/.html）")
    parser.add_argument("--name", default=None, help="卡片显示名（默认取文件名）")
    parser.add_argument("--slug", default=None, help="卡片 id（小写连字符，默认由名称生成）")
    parser.add_argument("--style", choices=["transparent", "full-bleed"], default="transparent",
                        help="transparent=透明主体卡(PVZ风)；full-bleed=整幅图卡面(影之诗/游戏王风)")
    parser.add_argument("--engine", default="local", help="抠图引擎（当前支持 local=rembg+BiRefNet）")
    parser.add_argument("--model", default=None, choices=[*SUPPORTED_MODELS, "api", "none"],
                        help="抠图模型。缺省时：settings.json 配置了抠图 API 则用 api，否则用默认本地模型；"
                             "显式指定则使用指定模型（api=阿里云分割抠图；none=跳过抠图，整图直接作为卡面）")
    parser.add_argument("--title", default=None, help="卡面标题文字（可选）")
    parser.add_argument("--desc", default=None, help="卡面描述文字（可选）")
    parser.add_argument("--text-type", choices=["none", "transparent", "boxed"], default="none",
                        help="卡牌类型：none=无文本型；transparent=特殊文本型（透明文本框）；boxed=文本型（带文本框边框）")
    parser.add_argument("--text-pos", default=None,
                        help="描述文本区域 'x:y:w'（归一化 0~1，中心坐标与宽度），如 0.5:0.82:0.84（默认卡牌下方）")
    parser.add_argument("--background", default=None, help="DIY 背景层（assets/backgrounds/ 下）")
    parser.add_argument("--face", default=None, help="DIY 卡面底图（assets/faces/ 下）")
    parser.add_argument("--frame", default=None, help="DIY 边框（assets/frames/ 下）")
    parser.add_argument("--seal", default=None, help="DIY 卡封层（assets/seals/ 下，如镭射覆层）")
    parser.add_argument("--seal-frame", default=None,
                        help="DIY 边框卡封（assets/seals/ 下）；传 same 表示与卡牌卡封同素材")
    parser.add_argument("--scale", type=float, default=1.0, help="卡面缩放（0.5~1.5，默认 1 不缩放）")
    parser.add_argument("--glow", default=None,
                        help="辉光特效名（如 暖金描边/RGB变色灯光/霓虹灯/樱花粉；none=关闭；缺省=默认暖金描边）")
    parser.add_argument("--glow-strength", type=float, default=1.0,
                        help="辉光整体强度（0~2，默认 1.0）")
    parser.add_argument("--effect", default="none",
                        help="动画特效（3D 网页卡触发），可用逗号分隔多个 '名称@位置'，如 love@top,sparkle@右下 或 love@0.3:0.7")
    parser.add_argument("--back", default=None, help="卡背图片（默认 assets/backs/sample-1.png）")
    parser.add_argument("--card-size", default=f"{CARD_W}x{CARD_H}", help="2D 合成卡图尺寸，如 900x1200")
    parser.add_argument("--no-compose", action="store_true", help="不生成 2D 合成预览卡图")
    parser.add_argument("--float-fg", action="store_true",
                        help="透明主体浮于边框之上（PVZ 式立体感；默认边框盖住主体）")
    parser.add_argument("--outline", action="store_true",
                        help="透明主体加白色贴纸描边（类似贴纸边缘）")
    parser.add_argument("--no-mask", dest="mask_clip", action="store_false",
                        help="不裁剪到边框内（内边框效果：主体可延伸出边框外沿，边框外留一圈图像）")
    parser.add_argument("--adaptive", type=int, choices=[0, 1, 2], default=1,
                        help="自适应：1=方式1（整幅判定，按上传图与卡面比例缩放裁剪，恒铺满，推荐）；"
                             "2=方式2（横图适配，不足留白，主体完整不裁切）；0=不使用自适应（直接拉伸填满）")
    parser.add_argument("--json", action="store_true", help="以 JSON 输出结果（供其他工具解析）")
    args = parser.parse_args(argv)

    # 导出模式：直接导出已有卡牌，无需输入图像
    if args.export:
        try:
            out = export_card(args.export, args.export_type, args.out)
        except ValueError as e:
            print(f"错误：{e}", file=sys.stderr)
            return 1
        print(f"已导出 {args.export_type}：{out}")
        return 0

    if not args.image:
        parser.error("需要输入图像路径，或使用 --export 导出已有卡牌")

    image_path = Path(args.image)
    if not image_path.exists():
        print(f"错误：找不到输入图像 {image_path}", file=sys.stderr)
        return 2

    try:
        effect_specs = [s.strip() for s in args.effect.split(",") if s.strip() and s.strip().lower() != "none"]
        text_pos = None
        if args.text_pos:
            parts = [p.strip() for p in args.text_pos.split(":")]
            if len(parts) == 3:
                try:
                    text_pos = {"x": float(parts[0]), "y": float(parts[1]), "w": float(parts[2])}
                except ValueError:
                    text_pos = None
            if not text_pos:
                print("警告：--text-pos 格式应为 'x:y:w'（如 0.5:0.82:0.84），已忽略", file=sys.stderr)
        # 模型优先级：显式 --model > settings.json 配置了抠图 API（默认用 API）> 默认本地模型
        model = args.model or ("api" if get_api_config() else DEFAULT_MODEL)
        engine_name = "api" if model == "api" else args.engine
        result = make_card(
            image_path,
            name=args.name,
            slug=args.slug,
            style=args.style,
            engine_name=engine_name,
            model=model,
            title=args.title,
            desc=args.desc,
            text_type=args.text_type,
            text_pos=text_pos,
            background=args.background,
            face=args.face,
            frame=args.frame,
            seal=args.seal,
            seal_frame=args.seal_frame,
            scale=args.scale,
            glow_name=args.glow,
            glow_strength=args.glow_strength,
            effects=effect_specs,
            back=args.back,
            card_size=args.card_size,
            compose=not args.no_compose,
            subject_over_frame=args.float_fg,
            subject_outline=args.outline,
            mask_clip=args.mask_clip,
            adaptive=args.adaptive != 0,
            adaptive_mode=args.adaptive or 1,
            progress=lambda step, total, msg: print(f"[{step}/{total}] {msg}", file=sys.stderr),
        )
    except ValueError as e:
        print(f"错误：{e}", file=sys.stderr)
        return 2
    except Exception as e:
        print(f"错误：{e}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        for k, v in result.items():
            print(f"{k}: {v}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
