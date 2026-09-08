# -*- coding: utf-8 -*-
"""生成自包含的 GitHub 在线预览页（图片内嵌 base64，供 htmlpreview 链接使用）。

用法:
    .venv\\Scripts\\python.exe scripts\\make_gh_preview.py hasunosora-duo
"""
from __future__ import annotations

import base64
import io
import json
import sys
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import webcard

PREVIEW_DIR = ROOT / "preview"
PREVIEW_SIZE = (450, 600)  # 预览图缩放尺寸（原 900x1200 的一半，控制 HTML 体积）
OUT = ROOT / "assets" / "output"


def _b64(rel_path: Path) -> str:
    img = Image.open(rel_path).convert("RGBA")
    img = img.resize(PREVIEW_SIZE, Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode("ascii")


def build(card_id: str) -> None:
    card = json.loads((ROOT / "cards" / f"{card_id}.json").read_text(encoding="utf-8"))
    out = OUT / card_id
    front = _b64(out / "front.png")
    foreground = _b64(out / "foreground.png")
    back = _b64(out / "back.png")

    layers = card.get("layers") or {}
    frame_rel = _b64(out / "frame.png") if layers.get("frame") else None
    seal_rel = None
    seal_name = None
    if layers.get("seal"):
        seal_path = Path(str(layers["seal"]))
        seal_rel = _b64(seal_path if seal_path.is_absolute() else ROOT / seal_path)
        seal_name = card.get("seal_name") or seal_path.name

    text = card.get("text") or {}
    html = webcard.build_card_html(
        card.get("name") or card_id, front, foreground, back,
        frame_rel=frame_rel, seal_rel=seal_rel, seal_name=seal_name,
        description=text.get("description"),
        text_type=text.get("type", "none"),
        text_pos=text.get("pos"),
        subject_over_frame=bool(card.get("subject_over_frame")),
        round_foreground=True,
        seal_strength_front=float(card.get("seal_strength_front", 0.945)),
        seal_strength_back=float(card.get("seal_strength_back", 0.5775)),
    )
    PREVIEW_DIR.mkdir(exist_ok=True)
    dest = PREVIEW_DIR / f"{card_id}.html"
    dest.write_text(html, encoding="utf-8")
    print(f"written: {dest} ({dest.stat().st_size // 1024} KB)")


if __name__ == "__main__":
    for cid in sys.argv[1:] or ["hasunosora-duo"]:
        build(cid)
