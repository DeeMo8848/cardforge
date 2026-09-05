"""2D 卡牌合成：背景 → 卡面 → 主体 → 边框 → 卡封 → 文字（自底向上叠加）。

素材目录（DIY 扩展点，示例已生成）：
  assets/backgrounds/  背景层（等比裁剪填充）
  assets/faces/        卡面底图（full-bleed 风格时即整幅卡面）
  assets/frames/       边框（透明 PNG，覆盖在最上层）
  assets/seals/        卡封（半透明叠加层，如镭射覆层）
"""
from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

PROJECT_ROOT = Path(__file__).resolve().parent
CARD_W, CARD_H = 900, 1200  # 2:3，与 3D 卡应用比例一致


def _cover(im: Image.Image, w: int, h: int) -> Image.Image:
    """等比缩放并居中裁剪填充到目标尺寸。"""
    im = im.convert("RGBA")
    scale = max(w / im.width, h / im.height)
    im = im.resize((max(1, round(im.width * scale)), max(1, round(im.height * scale))), Image.LANCZOS)
    x, y = (im.width - w) // 2, (im.height - h) // 2
    return im.crop((x, y, x + w, y + h))


def _stretch(im: Image.Image, w: int, h: int) -> Image.Image:
    """整幅拉伸铺满目标尺寸（与 3D 卡前景层 UV 映射一致，不裁剪）。"""
    return im.convert("RGBA").resize((w, h), Image.LANCZOS)


def make_outline(foreground: Image.Image, ratio: float = 0.004) -> Image.Image | None:
    """为前景主体生成白色贴纸描边（不透明核心起算 + 带符号距离场圆形膨胀）。

    针对"描边像猫猫虫 / 描边与主体有空隙"的修正：
      - 以不透明核心（alpha>=200）为白边内缘基准：主体半透明边缘带
        （alpha 10~200）由白边整片盖住，不透出卡面（无空隙），
        白边同时不再压进主体实心区域（无虫形肿胀）；
      - 内缘 1.5px 渐变与主体实心柔和过渡（贴纸感），
        外缘为 thickness+0.75px 实心 + 1.5px 渐变抗锯齿（无方核 45° 阶梯）；
      - 内部镂空洞不填白。

    ratio: 描边厚度相对图像长边的比例（卡面 1200px 时 0.004 ≈ 单侧 2~3px）。
    无主体（全透明）时返回 None。
    """
    import numpy as np
    from PIL import ImageFilter
    from scipy.ndimage import distance_transform_edt, label

    fg = foreground.convert("RGBA")
    a = np.asarray(fg)[..., 3]
    if int((a > 10).sum()) == 0:
        return None
    thickness = max(2, round(max(fg.size) * ratio))

    # 1) 不透明核心：白边内缘基准（半透明边缘带由白边覆盖，主体实心不被白侵）
    core = a >= 200

    # 2) 平滑去锯齿：核心二值 mask 模糊→127 回切（边缘位置不变），滤掉 1~3px 高频锯齿
    m = np.asarray(
        Image.fromarray((core * 255).astype(np.uint8)).filter(
            ImageFilter.GaussianBlur(max(1.0, thickness * 0.4))
        )
    ) > 127
    if not m.any():
        return None

    # 3) 只把"连通到图像边缘"的外部区域视为可描边，内部镂空洞不填白
    outside = ~m
    labels, _ = label(outside)
    border_ids = set(
        np.unique(np.concatenate([labels[0, :], labels[-1, :], labels[:, 0], labels[:, -1]]))
    )
    external = np.isin(labels, list(border_ids))

    # 4) 带符号距离场：m 内为负（距边界距离），外部为正；内部洞为 inf（不描边）
    d_in = distance_transform_edt(m)
    d_out = distance_transform_edt(external)
    signed = np.where(m, -d_in, np.where(external, d_out, np.inf))

    # 5) 描边 alpha：白边从核心边界向外 thickness+0.75px 实心 + 1.5px 渐变；
    #    内缘 1.5px 渐变微微压进核心（覆盖核心模糊晕带，贴纸过渡），核心内部不描边
    alpha = np.clip((thickness + 2.25 - signed) * (255.0 / 1.5), 0, 255)
    alpha = np.minimum(alpha, np.clip((signed + 1.5) * (255.0 / 1.5), 0, 255))
    out = Image.new("RGBA", fg.size, (255, 255, 255, 0))
    out.putalpha(Image.fromarray(alpha.astype(np.uint8)))
    return out


def _subject_bbox_uv_img(foreground: Image.Image):
    """前景主体不透明包围盒在卡面 UV 坐标 (x0,y0,x1,y1)，前景拉伸铺满整卡。"""
    import numpy as np

    a = np.asarray(foreground.convert("RGBA"))[..., 3]
    ys, xs = np.where(a > 10)
    if len(xs) == 0:
        return None
    fg_w, fg_h = foreground.size
    return (xs.min() / fg_w, ys.min() / fg_h, (xs.max() + 1) / fg_w, (ys.max() + 1) / fg_h)


def _frame_fit(frame: Image.Image, bbox_uv, w: int, h: int, pad: float = 0.03):
    """边框图缩放到主体包围盒 + 边距并居中，返回 (缩放图, 左上角x, 左上角y)。"""
    x0, y0, x1, y1 = bbox_uv
    fw = min(x1 - x0 + 2 * pad, 1.0)
    fh = min(y1 - y0 + 2 * pad, 1.0)
    cx = min(max((x0 + x1) / 2, fw / 2), 1 - fw / 2)
    cy = min(max((y0 + y1) / 2, fh / 2), 1 - fh / 2)
    fw_px = max(1, round(fw * w))
    fh_px = max(1, round(fh * h))
    scaled = frame.convert("RGBA").resize((fw_px, fh_px), Image.LANCZOS)
    x = round((cx - fw / 2) * w)
    y = round((cy - fh / 2) * h)
    return scaled, x, y


def _apply_static_foil(canvas: Image.Image, strength: float = 3.2) -> Image.Image:
    """叠加静态箔光，模拟 3D 卡牌正面镭射效果（对角箔光带 + 径向眩光）。

    复刻 webcard.py 主卡 shader 的光效（无时间动画，避免频闪），用 screen 混合叠加。
    """
    import numpy as np

    w, h = canvas.size
    arr = np.asarray(canvas.convert("RGBA"), dtype=np.float32) / 255.0
    y, x = np.mgrid[0:h, 0:w]
    u = (x + 0.5) / w
    v = (y + 0.5) / h

    # 径向眩光（中心 0.48,0.44）
    cu, cv = 0.48, 0.44
    dist = np.sqrt(((u - cu) * 0.76) ** 2 + (v - cv) ** 2)
    radial = 0.72 * (1.0 - np.clip((dist - 0.04) / 0.86, 0.0, 1.0))

    # 对角箔光带
    shifted_u = u + (cu - 0.5) * 0.42
    shifted_v = v + (cv - 0.5) * 0.3
    foil_flow = shifted_u * 0.72 + shifted_v * 0.98
    wide = np.maximum(0.0, np.sin(foil_flow * 14.0 + 1.2)) ** 4.5
    thin = np.maximum(0.0, np.sin((shifted_u * 0.84 - shifted_v * 0.62) * 108.0)) ** 16.0
    cross = np.maximum(0.0, np.sin((shifted_u * 0.52 + shifted_v * 1.28) * 68.0)) ** 18.0
    band = wide * 0.54 + thin * 0.3 + cross * 0.18

    foil_mask = 0.14 * (0.28 + radial * 0.72)

    # 古董箔光色（深青 ↔ 金色）
    wave = 0.5 + 0.5 * np.cos(6.283185 * (foil_flow * 0.74 + shifted_v * 0.22))
    foil = np.stack([
        0.11 + (1.0 - 0.11) * wave,
        0.34 + (0.68 - 0.34) * wave,
        0.25 + (0.25 - 0.25) * wave,
    ], axis=-1)
    second_wave = 0.5 + 0.5 * np.cos(6.283185 * (shifted_u * 0.28 - shifted_v * 0.8 + 0.24))
    second_foil = np.stack([
        0.11 + (1.0 - 0.11) * second_wave,
        0.34 + (0.68 - 0.34) * second_wave,
        0.25 + (0.25 - 0.25) * second_wave,
    ], axis=-1)

    # 光效叠加（screen blend）
    light = (
        foil * (band * foil_mask * 0.18)[..., None]
        + second_foil * (radial * 0.08)[..., None]
        + np.array([1.0, 0.89, 0.67], dtype=np.float32) * (radial * wide * 0.04)[..., None]
    ) * strength
    rgb = arr[..., :3]
    blended = 1.0 - (1.0 - rgb) * (1.0 - light)
    arr[..., :3] = np.clip(blended, 0.0, 1.0)
    return Image.fromarray((arr * 255.0).astype(np.uint8), "RGBA")


def _load_optional(path: str | Path | None) -> Image.Image | None:
    if not path:
        return None
    p = Path(path)
    if not p.is_absolute():
        p = PROJECT_ROOT / p
    if not p.exists():
        return None
    return Image.open(p).convert("RGBA")


def compose_card(
    foreground: Image.Image | None,
    style: str = "transparent",
    background: str | Path | None = None,
    face: str | Path | None = None,
    frame: str | Path | None = None,
    seal: str | Path | None = None,
    title: str | None = None,
    description: str | None = None,
    text_type: str = "none",
    text_area: dict | None = None,
    subject_over_frame: bool = False,
    subject_outline: bool = False,
    size: tuple[int, int] = (CARD_W, CARD_H),
    front: str | Path | None = None,
) -> Image.Image:
    w, h = size
    canvas = Image.new("RGBA", (w, h), (14, 11, 9, 255))

    # 底图：优先用 front.png（与 3D 卡正面贴图一致），否则回退 background/face
    if style == "full-bleed":
        base = _load_optional(front) or _load_optional(face) or foreground
        if base is not None:
            canvas = _cover(base, w, h)
    else:
        base = _load_optional(front)
        if base is not None:
            canvas = _cover(base, w, h)
        else:
            bg = _load_optional(background)
            if bg is not None:
                canvas = _cover(bg, w, h)
            fc = _load_optional(face)
            if fc is not None:
                canvas.alpha_composite(_cover(fc, w, h))

    # 静态箔光：叠加在底图之上（与 3D 卡一致，主体/边框/卡封/文字在其上层）
    canvas = _apply_static_foil(canvas)

    # 主体：整幅拉伸铺满（与 3D 卡前景层一致），默认被边框遮盖
    if style != "full-bleed" and foreground is not None:
        subject = _stretch(foreground, w, h)
        if subject_over_frame:
            fr0 = _load_optional(frame)
            if fr0 is not None:
                canvas.alpha_composite(_cover(fr0, w, h))
        if subject_outline:
            ol = make_outline(foreground)
            if ol is not None:
                canvas.alpha_composite(_stretch(ol, w, h), (0, 0))
        canvas.alpha_composite(subject, (0, 0))

    fr = _load_optional(frame)
    if fr is not None and not (subject_over_frame and foreground is not None and style != "full-bleed"):
        if subject_over_frame:
            canvas.alpha_composite(_cover(fr, w, h))
        elif style != "full-bleed" and foreground is not None:
            # 未开启浮于边框：边框按主体包围盒缩放（与 3D 卡一致），而非整卡
            bbox_uv = _subject_bbox_uv_img(foreground)
            if bbox_uv:
                scaled, x, y = _frame_fit(fr, bbox_uv, w, h)
                canvas.alpha_composite(scaled, (x, y))
            else:
                canvas.alpha_composite(_cover(fr, w, h))
        else:
            canvas.alpha_composite(_cover(fr, w, h))

    fx = _load_optional(seal)
    if fx is not None:
        canvas.alpha_composite(_cover(fx, w, h))

    _draw_text(canvas, title, description, text_type, text_area)
    return canvas


def _wrap_text(draw: ImageDraw.ImageDraw, text: str, font, max_w: int) -> list[str]:
    """按字符宽度自动换行（保留显式换行）。"""
    lines: list[str] = []
    for raw in text.split("\n"):
        cur = ""
        for ch in raw:
            if draw.textlength(cur + ch, font=font) > max_w:
                if cur:
                    lines.append(cur)
                cur = ch
            else:
                cur += ch
        if cur:
            lines.append(cur)
    return lines


def _draw_text(canvas: Image.Image, title: str | None, description: str | None,
               text_type: str = "none", text_area: dict | None = None) -> None:
    """绘制卡面文字。text_type: none=无文本型（不画）/ transparent=特殊文本型 / boxed=文本型。

    描述文字默认贴在卡牌下方区域（左右各留 8% 边框宽度），自动换行并缩放字号适配。
    text_area: 可选的自定义文本区域 {x1,x2,y1,y2}（0~1 比例），覆盖默认区域。
    """
    if text_type == "none" or (not title and not description):
        return
    w, h = canvas.size
    draw = ImageDraw.Draw(canvas)
    try:
        font_t = ImageFont.truetype("msyh.ttc", 46)
        font_d = ImageFont.truetype("msyh.ttc", 34)
    except OSError:
        font_t = font_d = ImageFont.load_default()

    if title:
        box = draw.textbbox((0, 0), title, font=font_t)
        draw.text(
            ((w - (box[2] - box[0])) // 2, h - 168),
            title,
            font=font_t,
            fill=(255, 238, 196, 255),
            stroke_width=3,
            stroke_fill=(38, 20, 8, 255),
        )

    if not description:
        return

    # 描述文本区域：默认卡牌下方（扣除左右边框宽度），可用 text_area 覆盖
    if text_area and all(k in text_area for k in ("x1", "x2", "y1", "y2")):
        area_x0 = round(w * min(1.0, max(0.0, text_area["x1"])))
        area_x1 = round(w * min(1.0, max(0.0, text_area["x2"])))
        area_y0 = round(h * min(1.0, max(0.0, text_area["y1"])))
        area_y1 = round(h * min(1.0, max(0.0, text_area["y2"])))
    else:
        area_x0, area_x1 = round(w * 0.08), round(w * 0.92)
        area_y0, area_y1 = round(h * 0.70), round(h * 0.92)
    if area_x1 <= area_x0 or area_y1 <= area_y0:
        return
    area_w, area_h = area_x1 - area_x0, area_y1 - area_y0
    fs = 34
    while fs > 13:
        font = ImageFont.truetype("msyh.ttc", fs)
        lines = _wrap_text(draw, description, font, area_w - 20)
        if len(lines) * round(fs * 1.4) <= area_h:
            break
        fs -= 2
    line_h = round(fs * 1.4)
    total_h = len(lines) * line_h
    y = area_y0 + (area_h - total_h) // 2 + (line_h - fs) // 2
    for ln in lines:
        box = draw.textbbox((0, 0), ln, font=font)
        draw.text(
            ((w - (box[2] - box[0])) // 2, y),
            ln,
            font=font,
            fill=(255, 244, 214, 250),
            stroke_width=max(2, fs // 12),
            stroke_fill=(24, 12, 4, 235),
        )
        y += line_h
