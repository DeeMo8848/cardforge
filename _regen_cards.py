"""用已有卡牌资产重新生成 card.html（应用最新 shader 光效）与 card.webp/thumb.webp（含边框/卡封/文本预览图）。"""
import json
import shutil
import sys
from pathlib import Path

from PIL import Image

PROJECT_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJECT_ROOT))

from compositor import CARD_H, CARD_W, compose_card  # noqa: E402
from cardforge import (  # noqa: E402
    lib_ref,
    put_layer,
    save_preview,
    _materialize_effect_images,
    _resolve_effects,
)
from webcard import build_card_html  # noqa: E402


def regen(card_json: Path) -> bool:
    d = json.loads(card_json.read_text(encoding="utf-8"))
    cid = d.get("id") or card_json.stem
    out_dir = PROJECT_ROOT / "assets" / "output" / cid
    if not out_dir.is_dir():
        print(f"  [跳过] {cid}: 输出目录不存在")
        return False

    # 光栅卡的卡面来自 lenti/ 下的帧图，本脚本走的是普通 front/foreground 路径，
    # 硬跑会把光栅记录冲掉。这类卡要用拼装页保存或 CLI 单独生成。
    if d.get("lenti"):
        print(f"  [跳过] {cid}: 光栅卡（单独生成）")
        return False

    # 同理：程序化背景（虚空）靠 bg_shader 开关，脚本不传这个参数，
    # 重生成会把实时星空调回静态 PNG。按背景素材旁的 sidecar 判断。
    _bgname = d.get("background_name")
    if _bgname:
        from webapp import _bg_shader_of, resolve_asset  # noqa: F401
        if _bg_shader_of(resolve_asset("backgrounds", _bgname)):
            print(f"  [跳过] {cid}: 程序化背景卡（单独生成）")
            return False

    # 整幅风格：front.png 应为整幅原图（source.png），而非抠图主体
    # source.png 若已外置成素材库引用（本地没有副本），按卡定义里的 source 找回原图
    if d.get("style") == "full-bleed":
        src = out_dir / "source.png"
        if not src.is_file():
            _s = Path(str(d.get("source") or ""))
            src = _s if _s.is_file() else src
        if src.is_file():
            Image.open(src).convert("RGBA").save(out_dir / "front.png")
            print(f"  [front] {cid}: 已改为整幅原图")

    layers = d.get("layers") or {}
    text = d.get("text") or {}
    extra = d.get("extra") or {}
    # 边框/卡封复制到输出目录，保证 card.html 自包含（本地双击与 HTTP 访问均可用）
    frame_path = layers.get("frame")
    seal_path = layers.get("seal")
    frame_rel = seal_rel = None
    interior_rel = None
    if frame_path and Path(frame_path).exists():
        # 素材库原图只写引用（../../assets/frames/x.png），不再逐卡复制一份
        frame_rel = put_layer(frame_path, out_dir, "frame.png") or "frame.png"
        # 边框内区域蒙版：整卡内容仅绘制在边框外围以内（与 webapp 保存逻辑一致）
        try:
            from compositor import save_frame_interior_mask
            save_frame_interior_mask(Image.open(frame_path), out_dir / "interior.png")
            interior_rel = "interior.png"
        except Exception as e:  # noqa: BLE001
            print(f"  [interior] {cid}: 蒙版生成失败 {e}")
            interior_rel = None
    elif (out_dir / "interior.png").exists():
        (out_dir / "interior.png").unlink()
    if seal_path and Path(seal_path).exists():
        seal_rel = put_layer(seal_path, out_dir, "seal.png") or "seal.png"
    subject_over_frame = bool(d.get("subject_over_frame", extra.get("subject_over_frame")))
    subject_outline = bool(d.get("subject_outline", extra.get("subject_outline")))
    mask_clip = bool(d.get("mask_clip", extra.get("mask_clip", True)))
    seal_front = d.get("seal_strength_front", extra.get("seal_strength_front", 0.945))
    seal_back = d.get("seal_strength_back", extra.get("seal_strength_back", 0.5775))
    try:
        seal_front = min(2.0, max(0.0, float(seal_front)))
        seal_back = min(2.0, max(0.0, float(seal_back)))
    except (TypeError, ValueError):
        seal_front, seal_back = 0.945, 0.5775
    try:
        content_scale = min(1.5, max(0.5, float(extra.get("card_scale", 1.0))))
    except (TypeError, ValueError):
        content_scale = 1.0
    style = d.get("style") or "transparent"
    # 主体白色描边：启用时从前景图重新生成（贴纸边）
    outline_rel = None
    if subject_outline:
        fg_p = out_dir / "foreground.png"
        if fg_p.exists() and fg_p.stat().st_size > 0:
            from compositor import make_outline

            ol = make_outline(Image.open(fg_p))
            if ol is not None:
                ol.save(out_dir / "outline.png")
                outline_rel = "outline.png"
    back_p = Path(str(d.get("back") or ""))
    back_rel = (put_layer(back_p, out_dir, "back.png") if back_p.is_file() else "back.png")
    # 卡面可能已外置成素材引用（front.png 不在卡目录），按卡定义找回原图
    front_p = Path(str(d.get("front") or ""))
    if not front_p.is_file():
        front_p = out_dir / "front.png"
    front_rel = lib_ref(front_p, out_dir) or "front.png"
    # 卡定义里的 effects 必须传进去：原来漏了这一步，重生成会把特效整个丢掉
    # （card.html 里没有 __foilObj，卡面只剩静态图）。
    try:
        effects = _resolve_effects(d.get("effects"))
    except Exception as e:  # noqa: BLE001
        print(f"  [effects] {cid}: 解析失败 {e}")
        effects = []
    # ★ 解析出来的 def 里贴图还是裸名（tx_foil_smoke / foil_arrow10.png），必须过一遍
    #   _materialize_effect_images 改写成 lib_ref 引用或复制进 effects/。
    #   漏了这一步 ⇒ card.html 里全是相对裸名 ⇒ 贴图 404 ⇒ 特效整层静默不渲染
    #   （卡面正常、完全不动，控制台只有一堆 404）。
    if effects:
        try:
            effects = _materialize_effect_images(effects, out_dir)
        except Exception as e:  # noqa: BLE001
            print(f"  [effects] {cid}: 贴图落地失败 {e}")
    html = build_card_html(
        d.get("name") or cid,
        front_rel,
        "foreground.png",
        back_rel,
        effects=effects,
        frame_rel=frame_rel,
        seal_rel=seal_rel,
        seal_name=d.get("seal_name") or (Path(seal_path).name if seal_path else None),
        description=text.get("description"),
        text_type=text.get("type") or "none",
        text_pos=text.get("pos"),
        subject_over_frame=subject_over_frame,
        round_foreground=True,
        frame_fit_subject=(style != "full-bleed"),
        outline_rel=outline_rel,
        seal_strength_front=seal_front,
        seal_strength_back=seal_back,
        content_scale=content_scale,
        interior_rel=interior_rel,
        mask_clip=mask_clip,
    )
    (out_dir / "card.html").write_text(html, encoding="utf-8")

    # 2D 预览图：叠加边框/卡封/文字，与 3D 卡一致
    try:
        fg = None
        fg_p = out_dir / "foreground.png"
        if fg_p.exists() and fg_p.stat().st_size > 0:
            fg = Image.open(fg_p)
        composed = compose_card(
            fg,
            style=d.get("style") or "transparent",
            background=layers.get("background"),
            face=layers.get("face"),
            frame=frame_path,
            seal=seal_path,
            title=text.get("title"),
            description=text.get("description"),
            text_type=text.get("type") or "none",
            text_area=text.get("pos"),
            subject_over_frame=bool(d.get("subject_over_frame", extra.get("subject_over_frame"))),
            subject_outline=bool(d.get("subject_outline", extra.get("subject_outline"))),
            mask_clip=mask_clip,
            size=(CARD_W, CARD_H),
            front=str(front_p),
        )
        save_preview(composed, out_dir)
    except Exception as e:  # noqa: BLE001
        print(f"  [card.webp] {cid}: 合成失败 {e}")
    print(f"  [OK] {cid} (seal={Path(seal_path).name if seal_path else None})")
    return True


def main() -> None:
    cards_dir = PROJECT_ROOT / "cards"
    files = sorted(cards_dir.glob("*.json"))
    if not files:
        print("没有卡牌定义")
        return
    print(f"重新生成 {len(files)} 张卡牌…")
    ok = 0
    for f in files:
        if regen(f):
            ok += 1
    print(f"完成：{ok}/{len(files)} 张已更新")


if __name__ == "__main__":
    main()
