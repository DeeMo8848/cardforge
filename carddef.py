"""卡牌定义：JSON 生成与读取，DIY 字段（背景/卡面/边框/特效/文字/卡背）已预留。"""
from __future__ import annotations

import json
from pathlib import Path

DEFAULT_BACK = "assets/backs/three-kingdoms-back.png"


def build_card_def(
    card_id: str,
    name: str,
    style: str,
    source: str,
    front: str,
    foreground: str,
    engine: str,
    description: str = "",
    back: str = DEFAULT_BACK,
    layers: dict | None = None,
    text: dict | None = None,
    extra: dict | None = None,
) -> dict:
    """构造卡牌定义。layers 与 text 为空时表示对应 DIY 层未启用。"""
    return {
        "id": card_id,
        "name": name,
        "description": description,
        "style": style,              # transparent=透明主体卡(PVZ风) / full-bleed=整幅卡面(影之诗风)
        "engine": engine,
        "source": source,            # 原图副本
        "front": front,              # 3D 应用底图层
        "foreground": foreground,    # 3D 应用前景层（透明主体）
        "back": back,                # 卡背（后续可 DIY）
        "layers": {
            "background": (layers or {}).get("background"),  # assets/backgrounds/
            "face": (layers or {}).get("face"),              # assets/faces/
            "frame": (layers or {}).get("frame"),            # assets/frames/
            "seal": (layers or {}).get("seal"),              # assets/seals/（卡封，如镭射覆层）
        },
        "text": {
            "title": (text or {}).get("title"),
            "description": (text or {}).get("description"),
            "type": (text or {}).get("type", "none"),          # none=无文本型 / transparent=特殊文本型 / boxed=文本型
            "pos": (text or {}).get("pos"),                     # 文本区域 {x,y,w} 归一化坐标（None=默认下方区域）
        },
        **(extra or {}),
    }


def save_card_def(card: dict, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(card, ensure_ascii=False, indent=2), encoding="utf-8")


def load_card_def(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))
