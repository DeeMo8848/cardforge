"""用已有卡牌资产重新生成 card.html（应用最新 shader 光效）与 card.png（含边框/卡封/文本预览图）。"""
import json
import shutil
import sys
from pathlib import Path

from PIL import Image

PROJECT_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJECT_ROOT))

from compositor import CARD_H, CARD_W, compose_card  # noqa: E402
from webcard import build_card_html  # noqa: E402


def regen(card_json: Path) -> bool:
    d = json.loads(card_json.read_text(encoding="utf-8"))
    cid = d.get("id") or card_json.stem
    out_dir = PROJECT_ROOT / "assets" / "output" / cid
    if not out_dir.is_dir():
        print(f"  [跳过] {cid}: 输出目录不存在")
        return False

    # 整幅风格：front.png 应为整幅原图（source.png），而非抠图主体
    if d.get("style") == "full-bleed":
        src = out_dir / "source.png"
        if src.exists():
            Image.open(src).convert("RGBA").save(out_dir / "front.png")
            print(f"  [front] {cid}: 已改为整幅原图")

    layers = d.get("layers") or {}
    text = d.get("text") or {}
    extra = d.get("extra") or {}
    # 边框/卡封复制到输出目录，保证 card.html 自包含（本地双击与 HTTP 访问均可用）
    frame_path = layers.get("frame")
    seal_path = layers.get("seal")
    frame_rel = seal_rel = None
    if frame_path and Path(frame_path).exists():
        shutil.copy(frame_path, out_dir / "frame.png")
        frame_rel = "frame.png"
    if seal_path and Path(seal_path).exists():
        shutil.copy(seal_path, out_dir / "seal.png")
        seal_rel = "seal.png"
    subject_over_frame = bool(d.get("subject_over_frame", extra.get("subject_over_frame")))
    subject_outline = bool(d.get("subject_outline", extra.get("subject_outline")))
    seal_front = d.get("seal_strength_front", extra.get("seal_strength_front", 0.945))
    seal_back = d.get("seal_strength_back", extra.get("seal_strength_back", 0.5775))
    try:
        seal_front = min(2.0, max(0.0, float(seal_front)))
        seal_back = min(2.0, max(0.0, float(seal_back)))
    except (TypeError, ValueError):
        seal_front, seal_back = 0.945, 0.5775
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
    html = build_card_html(
        d.get("name") or cid,
        "front.png",
        "foreground.png",
        "back.png",
        frame_rel=frame_rel,
        seal_rel=seal_rel,
        seal_name=Path(seal_path).name if seal_path else None,
        description=text.get("description"),
        text_type=text.get("type") or "none",
        text_pos=text.get("pos"),
        subject_over_frame=subject_over_frame,
        round_foreground=(style == "full-bleed"),
        frame_fit_subject=(style != "full-bleed"),
        outline_rel=outline_rel,
        seal_strength_front=seal_front,
        seal_strength_back=seal_back,
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
            size=(CARD_W, CARD_H),
            front=str(out_dir / "front.png"),
        )
        composed.convert("RGB").save(out_dir / "card.png")
    except Exception as e:  # noqa: BLE001
        print(f"  [card.png] {cid}: 合成失败 {e}")
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
