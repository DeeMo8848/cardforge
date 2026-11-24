#!/usr/bin/env python3
"""cardforge Web 界面 —— 本地可视化操作界面（浏览器使用）

用法：
  python webapp.py            # 启动后自动打开浏览器 http://127.0.0.1:8765
  python webapp.py --port 9000 --no-browser

页面分页：
  1. 生成卡牌：上传图片 → 抠图 → 选择素材/多特效 → 出卡
  2. 特效DIY：制作/预览/保存/删除特效（全参数，支持 emoji 与精灵帧图）
  3. 卡牌拼装：选择卡面/卡背/多特效 → 实时 3D 预览 → 保存为卡牌
  4. 素材管理：背景/卡面/边框/卡封/卡背/特效图 的增删改查
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import io
import json
import mimetypes
import os
import re
import shutil
import sys
import threading
import time
import uuid
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, quote, parse_qs

import cardforge
from carddef import build_card_def, save_card_def
from compositor import CARD_H, CARD_W, _cover, compose_card, frame_glow_boost, save_frame_interior_mask
from engine import DEFAULT_MODEL, SUPPORTED_MODELS, create_engine, get_api_config, model_status
import webcard
import packs
import lenticular
import parallax

PROJECT_ROOT = Path(__file__).resolve().parent
ASSETS = PROJECT_ROOT / "assets"
UPLOAD_DIR = ASSETS / "output" / "_uploads"
TMP_DIR = ASSETS / "output" / "_tmp"          # 临时上传（拼装前景层等）
PREVIEW_DIR = ASSETS / "output" / "_preview"  # 实时预览 HTML 缓存
# 异兽牌（RipIt 批量还原）的成品卡目录。跟 cardforge 自己的 assets/output
# 分开存放：那边是工具手动捏的试验卡，这边是 1800 版流水线产物。
RIPIT_DIR = ASSETS / "ripit"
# 成品卡可能落在两个根下，取图/列卡都要按顺序找。
CARD_ROOTS = (("assets/ripit", RIPIT_DIR), ("assets/output", ASSETS / "output"))
# 收集册只收录 cardforge 自己捏的卡（assets/output）。
# assets/ripit 是异兽牌专用目录，按约定**不进收集册**，只在「图鉴」里展示。
ALBUM_ROOTS = ("assets/output",)


def card_dir(card_id: str) -> Path:
    """按 id 找成品卡目录：先 assets/ripit，再 assets/output，都没有就返回 output 下的路径。"""
    for _rel, root in CARD_ROOTS:
        d = root / card_id
        if d.is_dir():
            return d
    return ASSETS / "output" / card_id


def _put_layer(src: Path, out_dir: Path, filename: str, *, local: bool = False):
    """素材落到卡目录，返回 (card.html 引用, 实际文件路径)。

    素材库里的原始文件 → 只写引用（如 "../../../assets/backs/sample-1.png"），不复制；
    自定义/编辑过的（缩放烘焙、上传、生成）→ 复制成 out_dir/filename。
    card.html 本来就是相对路径加载（three.js 也走 ../../shared/lib/），
    多几层 ../ 不需要改前端；http 下这些路径由 /assets/ /packs/ /fx2/ 路由接住。
    """
    ref = cardforge.put_layer(src, out_dir, filename, local=local)
    if ref is None:
        return None, None
    p = Path(src).resolve() if ref.startswith("../") else (Path(out_dir) / filename)
    return ref, p


def _card_layer(card_id: str, media: str):
    """取一张卡某个层文件的实际路径：本地副本优先，被外置成素材引用时按卡定义找回原图。

    外置后卡目录里没有 frame.png / seal.png / back.png，重开编辑（改文字、重存）
    必须能从 cards/<id>.json 记录里找回来，否则会静默丢层。
    """
    copies = {"frame": "frame.png", "seal": "seal.png", "seal_frame": "seal_frame.png",
              "back": "back.png", "face": "front.png", "background": "front.png",
              "subject": "foreground.png"}
    dirs = {"frame": ("frames", "frames-text"), "seal": ("seals",), "seal_frame": ("seals",),
            "back": ("backs",), "face": ("faces",), "background": ("backgrounds",),
            "subject": ("faces",)}
    keys = {"frame": "frame", "seal": "seal", "seal_frame": "seal_frame", "back": "back",
            "face": "face_name", "background": "background_name", "subject": "subject_name"}
    p = card_dir(card_id) / copies[media]
    if p.exists():
        return p
    info = _inherit_card(card_id)
    name = info.get(keys[media])
    if name:
        for d in dirs[media]:
            q = resolve_asset(d, name)
            if q:
                return q
    # 卡目录内的自产图层（光栅卡 lenti/f00.webp 之类）：按卡片定义里的路径找回
    try:
        _d = json.loads((PROJECT_ROOT / "cards" / f"{card_id}.json").read_text(encoding="utf-8"))
        _layers = _d.get("layers") or {}
        rel = _layers.get(media) or _layers.get("face") or _layers.get("background")
        if rel:
            q = Path(str(rel))
            if not q.is_absolute():
                q = card_dir(card_id) / q
            if q.is_file():
                return q
    except (OSError, ValueError, TypeError):
        pass
    return None


# list_cards() 的短 TTL 缓存：(时间戳, 结果)。见函数注释。
_CARDS_CACHE = None
EFFECTS_DIR = ASSETS / "effects"              # 自定义特效 JSON
EFFECT_IMG_DIR = ASSETS / "effects-images"    # 特效图像/精灵帧图
SETTINGS_FILE = PROJECT_ROOT / "settings.json"  # 界面参数持久化（如卡封强度）
ALBUMS_FILE = PROJECT_ROOT / "albums.json"      # 收集册分组（册名 + 卡牌归属）
CODEX_FILE = PROJECT_ROOT / "codex.json"        # 异兽牌图鉴：标题 / 版本顺序 / 手工覆盖
CODEX_CARDS_FILE = PROJECT_ROOT / "codex_cards.json"  # 全部主体元数据（209 张，含未渲染的）

_ASSET_TYPES = ("backgrounds", "faces", "frames", "frames-text", "seals", "backs",
                "effects-images", "effects", "frame-effects")
# 特效/边框特效不是图片文件：内置定义在源码里、自定义在 JSON 里，不支持图片上传/重命名
_EFFECT_ASSET_TYPES = ("effects", "frame-effects")
_IMG_EXTS = (".png", ".jpg", ".jpeg", ".webp", ".gif")

for _d in (UPLOAD_DIR, TMP_DIR, PREVIEW_DIR, EFFECTS_DIR, EFFECT_IMG_DIR,
           ASSETS / "frames-text"):
    _d.mkdir(parents=True, exist_ok=True)


def merge_subjects_into_faces() -> int:
    """把旧的 assets/subjects/ 合并进 assets/faces/。

    「卡面」和「主体」本来就是同一张图：不透明时铺满整卡当底图，透明（抠好的图）
    时叠成前景层。早期把两者拆成两个素材类型，导致同一张图要在两个下拉里各选一次，
    这里是那次拆分的历史数据清理（幂等：目录空了就不再进循环）。
    """
    legacy = ASSETS / "subjects"
    if not legacy.is_dir():
        return 0
    faces = ASSETS / "faces"
    faces.mkdir(parents=True, exist_ok=True)
    moved = 0
    for p in sorted(legacy.iterdir()):
        if not p.is_file() or p.suffix.lower() not in _IMG_EXTS:
            continue
        dst = faces / p.name
        n = 2
        while dst.exists():
            dst = faces / f"{p.stem}-{n}{p.suffix}"
            n += 1
        try:
            shutil.move(str(p), str(dst))
            moved += 1
        except OSError:
            pass
    try:
        legacy.rmdir()
    except OSError:
        pass
    return moved


merge_subjects_into_faces()   # 历史数据：subjects/ 并入 faces/（卡面与主体是同一个东西）

_engines: dict[str, object] = {}
_engines_lock = threading.Lock()


def get_engine(model: str):
    """进程内按模型缓存引擎实例（会话复用，避免重复加载模型）。"""
    with _engines_lock:
        if model not in _engines:
            _engines[model] = create_engine("api" if model == "api" else "local", model)
        return _engines[model]


def list_assets(subdir: str) -> list[str]:
    """素材名列表（内置 + 已启用素材包）。

    返回「素材键」：内置 = 文件名；素材包 = `包id/相对路径`（见 packs.py）。
    """
    return [a["key"] for a in packs.list_assets(subdir)]


def resolve_asset(atype: str, name: str | None):
    """把素材名/键解析为绝对路径（内置 assets/<type>/<文件名> 或素材包 packs/<id>/<相对路径>）。"""
    return packs.resolve_asset(atype, name)


def _bg_shader_of(p) -> str | None:
    """背景素材旁的 <同名>.json 若声明了 shader，返回着色器名（如 "end_portal"）。

    这样「程序化背景」是**素材**而不是硬编码卡型：把 PNG + 同名 JSON 丢进
    assets/backgrounds/ 就能在拼装页背景下拉里选中；3D 页由着色器实时生成，
    2D 成品图仍用那张 PNG（静态/动态观感一致）。
    """
    if not p:
        return None
    try:
        meta = json.loads(Path(p).with_suffix(".json").read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return None
    if not isinstance(meta, dict):
        return None
    name = str(meta.get("shader") or "").strip()
    return name or None


def _remove_builtin_seal(stem: str) -> str | None:
    """同步删除内置卡封：SEAL_EFFECTS 定义（内存 + webcard.py 源文件）与
    _generate_seals.py 的 GENERATORS 条目（防止重新生成复活）。

    返回被删除的内置卡封名；若并非内置（无 shader 定义）返回 None。
    """
    if not stem:
        return None
    removed = None
    if hasattr(webcard, "SEAL_EFFECTS") and stem in webcard.SEAL_EFFECTS:
        del webcard.SEAL_EFFECTS[stem]
        removed = stem
    for path, fmt in (
        (PROJECT_ROOT / "webcard.py", r'^[ \t]*"{stem}":.*$\n'),
        (PROJECT_ROOT / "_generate_seals.py", r'^[ \t]*\(\s*"{stem}"\s*,[^\n]*$\n'),
    ):
        try:
            txt = path.read_text(encoding="utf-8")
        except OSError:
            continue
        pat = re.compile(fmt.format(stem=re.escape(stem)), re.M)
        new = pat.sub("", txt)
        if new != txt:
            path.write_text(new, encoding="utf-8")
    return removed


def list_effect_assets(atype: str) -> list[dict]:
    """特效/边框特效列表：内置（源码定义）+ 自定义（JSON 文件），标注是否内置。"""
    if atype == "effects":
        builtin = list(cardforge.BUILTIN_EFFECTS.keys())
        custom = [p.stem for p in EFFECTS_DIR.glob("*.json") if p.stem not in builtin]
    else:  # frame-effects
        builtin = list(webcard.GLOW_EFFECTS.keys())
        custom = [p.stem for p in webcard.GLOW_CUSTOM_DIR.glob("*.json") if p.stem not in builtin]
    return ([{"name": n, "builtin": True} for n in sorted(builtin)]
            + [{"name": n, "builtin": False} for n in sorted(custom)])


def _remove_builtin_effect(stem: str) -> str | None:
    """同步删除内置特效：BUILTIN_EFFECTS 定义（内存 + cardforge.py 源文件）。

    返回被删除的内置特效名；若并非内置（无定义）返回 None。
    """
    if not stem:
        return None
    removed = None
    if hasattr(cardforge, "BUILTIN_EFFECTS") and stem in cardforge.BUILTIN_EFFECTS:
        del cardforge.BUILTIN_EFFECTS[stem]
        removed = stem
    try:
        txt = (PROJECT_ROOT / "cardforge.py").read_text(encoding="utf-8")
    except OSError:
        return removed
    # 内置特效定义是多行字典：从 `"name": {` 到独立成行的 `},`
    pat = re.compile(r'^[ \t]*"{stem}": \{{.*?\n[ \t]*\}},\n'.format(stem=re.escape(stem)),
                     re.M | re.S)
    new = pat.sub("", txt)
    if new != txt:
        (PROJECT_ROOT / "cardforge.py").write_text(new, encoding="utf-8")
    return removed


def _remove_builtin_glow(stem: str) -> str | None:
    """同步删除内置辉光：GLOW_EFFECTS 定义（内存 + webcard.py 源文件）。

    返回被删除的内置辉光名；若并非内置（无定义）返回 None。
    """
    if not stem:
        return None
    removed = None
    if hasattr(webcard, "GLOW_EFFECTS") and stem in webcard.GLOW_EFFECTS:
        del webcard.GLOW_EFFECTS[stem]
        removed = stem
    try:
        txt = (PROJECT_ROOT / "webcard.py").read_text(encoding="utf-8")
    except OSError:
        return removed
    # 内置辉光定义是两行字典条目（首行含 "name": {，续行以 }, 收尾）
    pat = re.compile(r'^[ \t]*"{stem}": \{{.*\n[ \t]*.*\}},\n'.format(stem=re.escape(stem)), re.M)
    new = pat.sub("", txt)
    if new != txt:
        (PROJECT_ROOT / "webcard.py").write_text(new, encoding="utf-8")
    return removed


def _big_name(d: Path) -> str:
    """成品大图文件名。

    新产物存 card.webp —— 同一张 1465×2048，PNG 1.75 MB、WebP(q92) 0.17 MB，
    1800 版差 2.8 GB，肉眼分辨不出。老的 PNG 产物继续兼容。
    """
    for n in ("card.webp", "card.png"):
        if (d / n).exists():
            return n
    return ""


def list_cards() -> list[dict]:
    """已有卡牌（assets/ripit + assets/output 下含成品图的目录），附收集册展示所需字段。

    加 4 秒缓存：打开收集册/图鉴时，**页面构建**和页面内 **/api/cards** 会各扫一次
    1800 个卡目录（每次 ~1.9 s），实测首屏要 3~4 s。缓存后首屏 ~2 s。
    TTL 短到不会让人看不到刚渲出来的卡，又足够覆盖“页面 + 接口”这一次连招。
    """
    global _CARDS_CACHE
    now = time.time()
    if _CARDS_CACHE and now - _CARDS_CACHE[0] < 4.0:
        return _CARDS_CACHE[1]
    cards = _list_cards_uncached()
    _CARDS_CACHE = (now, cards)
    return cards


def _list_cards_uncached() -> list[dict]:
    # 其他工具调用生成的卡牌不展示：settings.json 显式排除的 id + 16 位 hex id 通用规则
    _exclude = set((_load_settings().get("album_exclude_ids") or []))
    _is_hex_id = lambda n: bool(re.fullmatch(r"[0-9a-f]{16}", n))
    cards = []
    seen = set()
    for rel_root, out in CARD_ROOTS:
        if not out.exists():
            continue
        for d in sorted(out.iterdir()):
            if not d.is_dir() or d.name.startswith("_"):
                continue
            if d.name in _exclude or _is_hex_id(d.name) or d.name in seen:
                continue
            # ★ 一次 scandir 拿到目录内全部文件名 —— 原来逐个 (d/n).exists()
            #   是每张卡 8~10 次 stat，1802 张就是 1.8 万次，光 syscall 就要 1.2 秒。
            try:
                with os.scandir(d) as it:
                    files = {e.name for e in it}
            except OSError:
                continue
            # 第二十四轮：front.png 已提到 shared/front/（按版本复用），卡目录只剩 card.html + 大图
            # → 只要还有卡面或 3D 页，就算成品卡，别再拿 front.png 当存在性判据。
            if not ({"front.png", "card.webp", "card.png", "card.html"} & files):
                continue
            seen.add(d.name)
            name, desc, text_type = d.name, "", "none"
            text_pos = None
            data = {}
            try:
                # 直接读，别再 exists 一次（每张卡省一次 stat；读不到抛 OSError 一样兜住）
                data = json.loads((PROJECT_ROOT / "cards" / f"{d.name}.json")
                                  .read_text(encoding="utf-8"))
                name = data.get("name") or d.name
                desc = data.get("description") or ""
                text_type = (data.get("text") or {}).get("type") or "none"
                text_pos = (data.get("text") or {}).get("pos")
            except (OSError, ValueError):
                data = {}
            # 把刚解析好的 JSON 递进去，别再读第二遍（1802 张卡白读 1 秒）
            inherit = _inherit_card(d.name, _parsed=data)
            _big = ("card.webp" if "card.webp" in files
                    else "card.png" if "card.png" in files else "")
            # build_card_def 会把 extra 展开到 JSON 顶层，特效字段实际存在 data["effects"]
            effects = data.get("effects") or (data.get("extra") or {}).get("effects") or []
            # 缩略图优先 WebP（宽 320，PNG ~172 KB / WebP ~40 KB，1800 张省 240 MB 左右）；
            # 老的 thumb.png 继续兼容。
            _thumb = ("thumb.webp" if "thumb.webp" in files
                      else "thumb.png" if "thumb.png" in files else "")
            cards.append({
                "id": d.name,
                "name": name,
                "description": desc,
                "text_type": text_type,
                "text_pos": text_pos,
                # 卡目录所在的真实根（assets/ripit 或 assets/output），离线路径要从它拼。
                "root": rel_root,
                "card_png": (f"/preview/{d.name}/{_big}" if _big
                             else f"/preview/{d.name}/front.png"),
                # 图鉴网格专用缩略图（宽 320）。没有就留空，前端回落到 card_png。
                "card_thumb": f"/preview/{d.name}/{_thumb}" if _thumb else "",
                "card_html": f"/preview/{d.name}/card.html" if "card.html" in files else "",
                "frame": inherit.get("frame"),
                "seal": inherit.get("seal"),
                "seal_name": inherit.get("seal_name"),
                "seal_frame": inherit.get("seal_frame"),
                "seal_frame_name": inherit.get("seal_frame_name"),
                "seal_frame_same": inherit.get("seal_frame_same"),
                "face": inherit.get("face_name") or inherit.get("subject_name"),
                "face_as_fg": bool(inherit.get("subject_name")),
                # 视差主体 id（拼装页选这张卡时自动复原「多层主体」的下拉选项）
                "parallax": (data.get("parallax") or {}).get("id")
                            if isinstance(data.get("parallax"), dict) else None,
                "background": inherit.get("background_name"),
                "bg_scale": _parse_scale(data.get("background_scale", (data.get("extra") or {}).get("background_scale", 1.0))),
                "frame_scale": _parse_scale(data.get("frame_scale", (data.get("extra") or {}).get("frame_scale", 1.0))),
                "seal_strength_front": inherit.get("seal_strength_front"),
                "seal_strength_back": inherit.get("seal_strength_back"),
                "seal_frame_strength_front": inherit.get("seal_frame_strength_front"),
                "seal_frame_strength_back": inherit.get("seal_frame_strength_back"),
                "subject_over_frame": inherit.get("subject_over_frame"),
                "subject_outline": inherit.get("subject_outline"),
                "mask_clip": inherit.get("mask_clip"),
                "back": inherit.get("back"),
                "scale": _parse_scale(data.get("card_scale", (data.get("extra") or {}).get("card_scale", 1.0))),
                "subject_offset_x": _parse_offset(data.get("subject_offset_x", (data.get("extra") or {}).get("subject_offset_x", 0.0))),
                "subject_offset_y": _parse_offset(data.get("subject_offset_y", (data.get("extra") or {}).get("subject_offset_y", 0.0))),
                "glow": data.get("glow_name") or (data.get("extra") or {}).get("glow_name"),
                "glow_strength": data.get("glow_strength",
                                        (data.get("extra") or {}).get("glow_strength", 1.0)),
                "effects": effects,
                "lenti": data.get("lenti") or (data.get("extra") or {}).get("lenti"),
            })
    return cards


# ---------- 收集册（album / 分组） ----------
# 数据只存「册名 + 卡牌归属」，卡牌本身仍在 assets/output/<id>/ 里；
# 一张卡最多属于一个册，没归属的就是「未分类」。删册只清归属，绝不删卡。

def _load_albums() -> dict:
    """{"albums": [{"id","name"}], "members": {卡牌id: 收集册id}}"""
    try:
        if ALBUMS_FILE.exists():
            d = json.loads(ALBUMS_FILE.read_text(encoding="utf-8"))
            if isinstance(d, dict):
                albums = [{"id": str(a["id"]), "name": str(a["name"]).strip()}
                          for a in (d.get("albums") or [])
                          if isinstance(a, dict) and a.get("id") and str(a.get("name") or "").strip()]
                members = d.get("members") if isinstance(d.get("members"), dict) else {}
                return {"albums": albums,
                        "members": {str(k): str(v) for k, v in members.items() if v}}
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return {"albums": [], "members": {}}


def _save_albums(data: dict) -> None:
    try:
        ALBUMS_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        pass


def album_cards(cards: list | None = None) -> list[dict]:
    """收集册可见的卡：只取 ALBUM_ROOTS 里的根（默认 assets/output）。

    异兽牌那 1800 版住在 assets/ripit，按约定不进收集册 —— 它们只由
    「图鉴」（codex）展示。之前 list_cards() 一并扫两个根，导致收集册里
    把它们当「未分类」全列出来了，点开还能正常预览。
    """
    cards = list_cards() if cards is None else cards
    return [c for c in cards if (c.get("root") or "assets/output") in ALBUM_ROOTS]


def albums_summary(cards: list | None = None) -> dict:
    """收集册列表 + 每册卡数 + 卡牌归属（只统计当前确实存在的卡，避免删卡留下幽灵计数）。

    cards 传入已算好的 list_cards() 结果可省一次目录扫描（收集册页面同时要两者）；
    无论传进来的是什么，都只按 ALBUM_ROOTS 过滤后再统计。
    """
    d = _load_albums()
    ids = [c["id"] for c in album_cards(cards)]
    known = set(ids)
    aid = {a["id"] for a in d["albums"]}
    members = {k: v for k, v in d["members"].items() if k in known and v in aid}
    counts = {a["id"]: 0 for a in d["albums"]}
    for v in members.values():
        counts[v] += 1
    return {
        "albums": [{"id": a["id"], "name": a["name"], "count": counts.get(a["id"], 0)}
                   for a in d["albums"]],
        "members": members,
        "total": len(ids),
        "unclassified": len(known) - len(members),
    }


def _album_name(text) -> str:
    name = str(text or "").strip()
    if not name:
        raise ValueError("收集册名称不能为空")
    if len(name) > 40:
        raise ValueError("收集册名称最多 40 字")
    return name


def album_create(name: str) -> dict:
    name = _album_name(name)
    d = _load_albums()
    if any(a["name"] == name for a in d["albums"]):
        raise ValueError(f"已有同名收集册「{name}」")
    d["albums"].append({"id": uuid.uuid4().hex[:8], "name": name})
    _save_albums(d)
    return {"ok": True, "summary": albums_summary()}


def album_rename(album_id: str, name: str) -> dict:
    name = _album_name(name)
    d = _load_albums()
    target = next((a for a in d["albums"] if a["id"] == album_id), None)
    if target is None:
        raise ValueError("找不到该收集册")
    if any(a["name"] == name and a["id"] != album_id for a in d["albums"]):
        raise ValueError(f"已有同名收集册「{name}」")
    target["name"] = name
    _save_albums(d)
    return {"ok": True, "summary": albums_summary()}


def album_delete(album_id: str) -> dict:
    d = _load_albums()
    if not any(a["id"] == album_id for a in d["albums"]):
        raise ValueError("找不到该收集册")
    d["albums"] = [a for a in d["albums"] if a["id"] != album_id]
    # 只解除归属：卡牌回到「未分类」，卡本身不动
    freed = [k for k, v in d["members"].items() if v == album_id]
    for k in freed:
        d["members"].pop(k, None)
    _save_albums(d)
    return {"ok": True, "freed": len(freed), "summary": albums_summary()}


def album_move(card_ids, album_id: str) -> dict:
    """把一批卡牌移入指定收集册；album_id 为空 = 移出（回到未分类）。"""
    d = _load_albums()
    album_id = (album_id or "").strip()
    if album_id and not any(a["id"] == album_id for a in d["albums"]):
        raise ValueError("找不到目标收集册")
    if isinstance(card_ids, str):
        card_ids = [card_ids]
    ids = [str(x).strip() for x in (card_ids or []) if str(x).strip()]
    if not ids:
        raise ValueError("没有选中任何卡牌")
    for cid in ids:
        if album_id:
            d["members"][cid] = album_id
        else:
            d["members"].pop(cid, None)
    _save_albums(d)
    return {"ok": True, "moved": len(ids), "summary": albums_summary()}


def _albums_forget(card_id: str) -> None:
    """删卡时同步清掉归属记录。"""
    d = _load_albums()
    if d["members"].pop(card_id, None) is not None:
        _save_albums(d)


def build_album_page() -> str:
    """收集册页面（独立 HTML，展示所有成品卡，悬停预览 + 侧边信息 + 点击放大）。

    模板从 album_template.html 读取（git 维护的纯净版，不含卡牌数据）；
    渲染后把最新卡牌数据写回本地 album.html（被 git 忽略），这样直接双击
    album.html 也能离线查看，且不会污染版本库。
    """
    tpl = PROJECT_ROOT / "album_template.html"
    p = PROJECT_ROOT / "album.html"
    if tpl.exists():
        html = tpl.read_text(encoding="utf-8")
    elif p.exists():
        html = p.read_text(encoding="utf-8")
    else:
        return "<!DOCTYPE html><html lang='zh-CN'><head><meta charset='utf-8'><title>收集册</title></head><body><p>album.html 缺失</p></body></html>"
    offline = []
    # 只列 assets/output 的卡（ALBUM_ROOTS）。异兽牌在 assets/ripit，只进图鉴不进收集册。
    cards = album_cards()
    for c in cards:
        _r = (c.get("root") or "assets/output") + "/"
        offline.append({
            "id": c["id"],
            "name": c["name"],
            "description": c["description"],
            "text_type": c["text_type"],
            "card_png": c["card_png"].replace("/preview/", _r, 1),
            "card_html": c["card_html"].replace("/preview/", _r, 1),
            # 网格格子只有 ~200px，却拉 1465×2048 的整卡图 = 每格 1.7 MB。
            # 有缩略图（宽 320）就用它，详情弹窗仍走 card_png。
            "thumb": (c.get("card_thumb") or "").replace("/preview/", _r, 1),
        })
    data_json = json.dumps(offline, ensure_ascii=False)
    summary = albums_summary(cards)
    meta_json = json.dumps({"albums": summary["albums"], "members": summary["members"]},
                           ensure_ascii=False)
    # ★ 不能用 re.sub 的替换串：它会解释反斜杠（\" -> "），卡名/描述里只要
    #   有一个英文引号，注入的 JSON 就被吃坏、整本收集册白屏（图鉴踩过一次）。
    def _inj(_m):
        return ("/*__ALBUM_DATA_START__*/\nwindow.__ALBUM_DATA__ = " + data_json + ";\n"
                "window.__ALBUM_META__ = " + meta_json + ";\n/*__ALBUM_DATA_END__*/")
    html = re.sub(r"/\*__ALBUM_DATA_START__\*/.*?/\*__ALBUM_DATA_END__\*/",
                  _inj, html, flags=re.S)
    p.write_text(html, encoding="utf-8")
    return html


# ---------- 异兽牌图鉴（codex） ----------
# 与「收集册」是两件事：收集册是自由分组（可新建/删除/改名），图鉴是**固定顺序**的
# 展册——一个主体（一只异兽）占一页 9 格（= 9 个版本），一册页左右两页共 18 格。
# 有成品卡的槽位「点亮」，没有的留暗位（未收录）；顺序由 codex.json 定死，
# 卡牌增删只影响点亮与否，不会让整本图鉴重新排页。

_CODEX_EDITION_CN = {
    "Normal": "普通", "Foil": "闪卡", "Metallic": "金属", "GoldBorder": "金边",
    "Alpha": "阿尔法", "Beta": "贝塔", "Omega": "欧米伽", "Negative": "负片",
    "FirstEdition": "初版", "Promo": "促销",
}

_CODEX_DEFAULT = {
    "title": "异兽牌",
    "subtitle": "RipIt · 异兽图鉴",
    "edition_order": ["Normal", "Foil", "Metallic", "GoldBorder", "Alpha",
                      "Beta", "Omega", "Negative", "FirstEdition"],
    "include_promo": False,
    # 主体列表默认来自 codex_cards.json（209 张全量，按卡号排序）。
    # 这里留空表示「自动」；填了 subjects 才按手写清单覆盖。
    "subjects": [],
}


def _normalize_codex(d: dict) -> dict:
    """把 codex.json 收敛成固定字段（缺项用默认补，坏条目直接丢）。"""
    order = [str(x).strip() for x in (d.get("edition_order") or []) if str(x).strip()]
    subjects = []
    for s in (d.get("subjects") or []):
        if not isinstance(s, dict):
            continue
        slug = str(s.get("slug") or "").strip()
        if not slug:
            continue
        subjects.append({
            "slug": slug,
            "name": str(s.get("name") or slug).strip(),
            "en": str(s.get("en") or "").strip(),
            "no": str(s.get("no") or "").strip(),
            "element": str(s.get("element") or "").strip(),
            "element_cn": str(s.get("element_cn") or "").strip(),
            "rarity": str(s.get("rarity") or "").strip(),
        })
    if not order:
        order = list(_CODEX_DEFAULT["edition_order"])
    return {
        "title": str(d.get("title") or "异兽牌").strip() or "异兽牌",
        "subtitle": str(d.get("subtitle") or "").strip(),
        "edition_order": order,
        "subjects": subjects,          # 空 = 用 codex_cards.json 的全量清单
        "include_promo": bool(d.get("include_promo", False)),
    }


def _load_codex() -> dict:
    """读 codex.json；缺失/损坏时回退内置默认（顺序固定）。"""
    try:
        if CODEX_FILE.exists():
            d = json.loads(CODEX_FILE.read_text(encoding="utf-8"))
            if isinstance(d, dict):
                return _normalize_codex(d)
    except (OSError, ValueError, TypeError):
        pass
    return _normalize_codex(_CODEX_DEFAULT)


def _load_codex_cards() -> list:
    """全量主体元数据（codex_cards.json，209 张含未渲染的）。

    由 cardrip/_gen_codex_cards.py 从「卡牌总表 + cards_final」合并导出：
    名字/描述以总表为准（cards_final 里 01b/01c 变体大面积为空），
    序号与稀有度取 cards_final。缺失或损坏时返回空，退回 codex.json 的手写清单。
    """
    try:
        d = json.loads(CODEX_CARDS_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    if not isinstance(d, list):
        return []
    out = []
    for x in d:
        if not isinstance(x, dict) or not str(x.get("slug") or "").strip():
            continue
        out.append({
            "slug": str(x["slug"]).strip(),
            "name": str(x.get("name") or x["slug"]).strip(),
            "en": str(x.get("en") or "").strip(),
            "no": str(x.get("no") or "").strip(),
            "element": str(x.get("element") or "").strip(),
            "element_cn": str(x.get("element_cn") or "").strip(),
            "rarity": str(x.get("rarity") or "").strip(),
            "promo": bool(x.get("promo")),
            "animal": str(x.get("animal") or "").strip(),
            "version": str(x.get("version") or "").strip(),
        })
    return out


def codex_catalog(cards: list | None = None) -> dict:
    """图鉴数据：每主体固定 9 个版本槽位 + 该槽位是否已有成品卡（点亮）。

    特典卡（No.201-209）只有 Promo 一种版本，所以**九张合成一个条目**、
    占最后一册页的左页（3×3 正好 9 格）—— 否则每张都要开 9 格，
    会多出 72 个永远空着的格子、还把特典拆到 5 个册页里。
    """
    cfg = _load_codex()
    cards = list_cards() if cards is None else cards
    by_id = {c["id"]: c for c in cards}
    all_subjects = cfg.get("subjects") or _load_codex_cards()
    if cfg.get("include_promo", False):
        subjects = [x for x in all_subjects if not x.get("promo")]
        promos = [x for x in all_subjects if x.get("promo")]
    else:
        subjects, promos = [x for x in all_subjects if not x.get("promo")], []
    entries, total_slots, total_owned = [], 0, 0
    for s in subjects:
        slots = []
        for ed in cfg["edition_order"]:
            cid = f"{s['slug']}-{ed.lower()}"
            c = by_id.get(cid)
            cn = _CODEX_EDITION_CN.get(ed, ed)
            slots.append({
                "edition": ed,
                "edition_cn": cn,
                "id": cid,
                "label": (c or {}).get("name") or f"{s['name']} · {cn}",
                "card_png": (c or {}).get("card_png") or "",
                "thumb": (c or {}).get("card_thumb") or "",
                "card_html": (c or {}).get("card_html") or "",
                "root": (c or {}).get("root") or "assets/output",
                "unlocked": bool(c),
            })
        owned = sum(1 for x in slots if x["unlocked"])
        total_slots += len(slots)
        total_owned += owned
        entries.append({**s, "slots": slots, "owned": owned, "total": len(slots),
                        "complete": owned >= len(slots)})
    if promos:
        slots = []
        for s in promos:
            cid = f"{s['slug']}-promo"
            c = by_id.get(cid)
            slots.append({
                "edition": "Promo",
                "edition_cn": "促销",
                "id": cid,
                # label 只进 img alt；格子下方用 caption，否则 9 格全是「促销」分不清是谁
                "label": (c or {}).get("name") or f"{s['name']} · 促销",
                "caption": "No.%s %s" % (s.get("no") or "", s.get("name") or ""),
                "card_png": (c or {}).get("card_png") or "",
                "thumb": (c or {}).get("card_thumb") or "",
                "card_html": (c or {}).get("card_html") or "",
                "root": (c or {}).get("root") or "assets/ripit",
                "unlocked": bool(c),
            })
        owned = sum(1 for x in slots if x["unlocked"])
        total_slots += len(slots)
        total_owned += owned
        entries.append({
            "slug": "promo-set", "no": "201-209", "name": "特典",
            "en": "Promo", "element": "Promo", "element_cn": "特典",
            "rarity": "Promo", "promo": True,
            "slots": slots, "owned": owned, "total": len(slots),
            "complete": owned >= len(slots),
        })
    return {
        "title": cfg["title"],
        "subtitle": cfg["subtitle"],
        "total": total_slots,
        "owned": total_owned,
        "entries": entries,
    }


def build_codex_page() -> str:
    """图鉴页面：模板 + 内嵌目录数据（同时写一份 codex.html 供离线双击查看）。"""
    tpl = PROJECT_ROOT / "codex_template.html"
    p = PROJECT_ROOT / "codex.html"
    if tpl.exists():
        html = tpl.read_text(encoding="utf-8")
    elif p.exists():
        html = p.read_text(encoding="utf-8")
    else:
        return ("<!DOCTYPE html><html lang='zh-CN'><head><meta charset='utf-8'>"
                "<title>异兽牌</title></head><body><p>codex_template.html 缺失</p></body></html>")
    data = codex_catalog()
    for e in data["entries"]:
        for s in e["slots"]:
            # 同一份数据要同时喂给「HTTP」与「双击 file://」两种打开方式：
            # 在线走 /preview/，离线必须改成相对文件路径，所以两个都存，前端按协议选。
            _r = (s.get("root") or "assets/output") + "/"
            s["card_png_off"] = s["card_png"].replace("/preview/", _r, 1)
            s["card_html_off"] = s["card_html"].replace("/preview/", _r, 1)
            s["thumb_off"] = (s.get("thumb") or "").replace("/preview/", _r, 1)
    data_json = json.dumps(data, ensure_ascii=False)
    # ★ 不能直接把 data_json 拼进 re.sub 的**替换串**：re.sub 会对替换串做转义解释
    #   （`\"` → `"`、`\\` → `\`、`\n` → 换行），卡牌描述里只要有一个引号，
    #   注入的 JSON 就被吃坏，浏览器报 "Invalid or unexpected token"，
    #   于是 window.__CODEX__ / const DATA 全都没定义，图鉴白屏只剩空壳。
    #   改用函数式替换，替换内容原样插入。
    data_json = data_json.replace("</", "<\\/")   # 防止描述里出现 </script> 提前闭合
    payload = ("/*__CODEX_DATA_START__*/\nwindow.__CODEX__ = " + data_json
               + ";\n/*__CODEX_DATA_END__*/")
    html = re.sub(
        r"/\*__CODEX_DATA_START__\*/.*?/\*__CODEX_DATA_END__\*/",
        lambda _m: payload,
        html,
        flags=re.S,
    )
    p.write_text(html, encoding="utf-8")
    return html


def list_effects() -> dict:
    builtin = [{"name": k, "def": v, "builtin": True} for k, v in cardforge.BUILTIN_EFFECTS.items()]
    custom = [{"name": n, "def": cardforge.resolve_effect_def(n), "builtin": False}
              for n in cardforge.list_custom_effects()]
    return {"builtin": builtin, "custom": custom}


def _safe_name(text: str) -> str:
    """清理文件名：去除路径分隔与非法字符。"""
    text = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "", text.strip())
    return text or "untitled"


def _parse_scale(v) -> float:
    """解析卡面渲染缩放（0.5~1.5，默认 1 不缩放）。"""
    try:
        s = float(v)
    except (TypeError, ValueError):
        return 1.0
    return min(1.5, max(0.5, s))


def _scale_card_layer(src, scale: float, dst: Path, base_fill=None, cover: bool = False) -> Path:
    """把素材按卡牌中心缩放后烘焙成 CARD_W×CARD_H PNG。

    默认 stretch 到整卡（与 3D 边框层「plane 整卡 + 贴图 UV 0~1」的基准一致），
    再整体放大 scale 倍并按中心裁掉溢出。cover=True 时先把素材等比 cover 到整卡
    （背景层用：3D 背景层会按 uTexAspect 做等比处理，不能被拉伸）。
    四周空白：base_fill 给定时填充该 RGB（卡体深色底），否则保持透明（边框）。

    ★ 这里**不能**无条件用 ImageOps.fit（cover）：素材比例若与卡比例不一致
      （如 3:4 的 900×1200 边框），fit 会先放大裁切再乘 scale，等于凭空多出一档
      放大 —— 表现就是「边框倍率 1.01 就顶出卡边、形状还变了」。
    """
    from PIL import Image
    im = Image.open(src).convert("RGBA")
    im = _cover(im, CARD_W, CARD_H) if cover else im.resize((CARD_W, CARD_H), Image.LANCZOS)
    sw, sh = max(1, round(CARD_W * scale)), max(1, round(CARD_H * scale))
    im = im.resize((sw, sh), Image.LANCZOS)
    canvas = Image.new(
        "RGBA", (CARD_W, CARD_H),
        (int(base_fill[0]), int(base_fill[1]), int(base_fill[2]), 255) if base_fill else (0, 0, 0, 0),
    )
    canvas.alpha_composite(im, ((CARD_W - sw) // 2, (CARD_H - sh) // 2))
    dst.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(dst)
    return dst


def _parse_glow_strength(v, dflt: float = 1.0) -> float:
    """解析辉光整体强度（0~2，默认 1）。"""
    try:
        s = float(v)
    except (TypeError, ValueError):
        return dflt
    return min(2.0, max(0.0, s))


def _parse_offset(v, dflt: float = 0.0) -> float:
    """解析主体偏移（卡宽/卡高比例，-0.9~0.9，默认 0 不偏移）。"""
    try:
        s = float(v)
    except (TypeError, ValueError):
        return dflt
    return min(0.9, max(-0.9, s))


def _abs_effect_images(effects: list[dict]) -> list[dict]:
    """预览页特效引用的图片改写为绝对 URL（页面不在输出目录，相对路径不可用）。组合特效递归处理。"""
    def _walk_foil(defn: dict):
        """闪卡特效（engine=foil）：贴图名 → 特效DIY2 自带贴图 URL；蒙版 → 素材库 URL。
        tx_face_*（影之诗卡面差分）对 cardforge 卡牌无意义，置空即跳过该槽。"""
        for s in defn.get("slots") or []:
            t = (s.get("tex") or "").strip()
            s["tex"] = f"/fx2/assets/foil/{t}.png" if t.startswith("tx_foil_") else ""
        d = defn.get("dist") or {}
        t = (d.get("tex") or "").strip()
        d["tex"] = f"/fx2/assets/foil/{t}.png" if t.startswith("tx_foil_") else ""
        m = (defn.get("mask") or "").strip()
        if m:
            defn["mask"] = f"/assets_abs/effects-images/{Path(m).name}"

    def _walk(defn: dict):
        if not isinstance(defn, dict):
            return
        if defn.get("engine") == "foil":
            _walk_foil(defn)
            return
        src = defn.get("image") or ""
        if src.startswith("image:"):
            defn["image"] = f"image:/assets_abs/effects-images/{Path(src[6:]).name}"
        for sub in defn.get("subs") or []:
            if isinstance(sub, dict):
                _walk(sub.get("def"))
    for item in effects or []:
        if isinstance(item, dict):
            _walk(item.get("def"))
    return effects


def _cleanup_old_previews(max_age: float = 3600.0) -> None:
    now = time.time()
    try:
        for d in PREVIEW_DIR.iterdir():
            if d.is_dir() and now - d.stat().st_mtime > max_age:
                shutil.rmtree(d, ignore_errors=True)
    except OSError:
        pass


LENTI_ASPECT = 1465 / 2048          # 卡面比例，光栅帧按它规范化


def lenti_path_of(spec: str) -> Path:
    """前端 spec → 本地图片路径（tmp:<上传名> / asset:<类型>:<素材名> / 直接路径）。"""
    kind, _, val = (spec or "").partition(":")
    if kind == "tmp":
        p = TMP_DIR / _safe_name(val)
    elif kind == "asset":
        atype, _, key = val.partition(":")
        if atype not in _ASSET_TYPES:
            raise ValueError("未知素材类型: " + atype)
        p = resolve_asset(atype, key) or Path()
    else:
        p = Path(val or spec)
    p = Path(p)
    if not p.is_file():
        raise ValueError("找不到图片: " + (val or spec))
    if p.suffix.lower() not in _IMG_EXTS:
        raise ValueError("不是支持的图片格式: " + p.name)
    return p


def lenti_src_list(data: dict) -> list[Path]:
    """拼装页提交的光栅图源（读不到的跳过；最多 6 张）。"""
    out = []
    for sp in (data.get("lenti_images") or []):
        try:
            out.append(lenti_path_of(sp))
        except (ValueError, OSError):
            continue
    return out[:6]


def _lenti_frames(paths: list[Path]):
    """把光栅图源规范化成卡面比例的帧（webp），按「文件名+大小+mtime」指纹缓存。

    调边框/卡封/特效时 preview 会整页重建，缓存命中就只 stat 一次，
    不必每次重复解码 PNG（6 张大图约 0.3~0.6s）。
    返回 (目录, 文件名列表)。
    """
    sig = []
    for p in paths:
        try:
            st = p.stat()
            sig.append("%s|%d|%d" % (p.name, st.st_size, int(st.st_mtime)))
        except OSError:
            sig.append(p.name)
    key = hashlib.md5(("|".join(sig) + "|%.4f" % LENTI_ASPECT).encode("utf-8")).hexdigest()[:12]
    d = PREVIEW_DIR / "_lenti" / key
    names = sorted(x.name for x in d.glob("f*.webp")) if d.is_dir() else []
    if not names:
        tmp = PREVIEW_DIR / "_lenti" / (key + ".tmp")
        if tmp.exists():
            shutil.rmtree(tmp, ignore_errors=True)
        frames = [lenticular.load_frame(p, LENTI_ASPECT) for p in paths[:6]]
        names = lenticular.save_frames(frames, tmp)
        if d.exists():
            shutil.rmtree(d, ignore_errors=True)
        tmp.rename(d)
    return d, names


# ---- 视频 / GIF → 光栅图集 ----
# 帧数上限不是采样器数（图集只占 1 个），而是图集格子数：6×6 = 36。
# 帧数可调：帧多 ⇒ 每帧占的倾斜区间更窄、换帧更细腻，但图集更大；
# 帧少 ⇒ 体积小、跳帧感更明显。`uLGyro` 按 (N-1)*0.72 自动换算，改帧数不用动别的。
LENTI_VIDEO_DIR = lenticular.LENTI_ROOT / "_video"
LENTI_VIDEO_SRC_DIR = LENTI_VIDEO_DIR / "_src"   # 原始文件留一份，改帧数时直接重抽
LENTI_VIDEO_FRAMES = 24
LENTI_VIDEO_FRAME_MIN = 8
LENTI_VIDEO_FRAME_MAX = 36
LENTI_VIDEO_FRAME_CHOICES = (8, 12, 16, 20, 24, 30, 36)
LENTI_VIDEO_CELL_W = 288
LENTI_VIDEO_MAX_BYTES = 60 * 1024 * 1024
_VIDEO_EXTS = (".mp4", ".webm", ".mov", ".mkv", ".avi", ".m4v",
               ".gif", ".webp", ".apng")


def _lenti_vframes(v) -> int:
    """帧数参数归一化（非法/缺省走默认 24，夹在 8~36）。"""
    try:
        n = int(round(float(v)))
    except (TypeError, ValueError):
        return LENTI_VIDEO_FRAMES
    return min(LENTI_VIDEO_FRAME_MAX, max(LENTI_VIDEO_FRAME_MIN, n))


def _lenti_vgrid(n: int) -> tuple[int, int]:
    """帧数 → 图集行列。25 格以内 5 列，26~36 走 6 列（正方形图集，采样器只要 1 个）。"""
    n = max(2, int(n))
    if n <= 4:
        return n, 1
    cols = 5 if n <= 25 else 6
    return cols, (n + cols - 1) // cols


def _even_idx(total: int, want: int) -> list[int]:
    """在 total 帧里均匀取 want 个下标（含首帧，末帧留一点余量避开收尾黑帧）。"""
    if total <= 0:
        return []
    if total <= want:
        return list(range(total))
    span = total - 1
    return sorted({min(total - 1, int(round(i * span / max(1, want - 1))))
                   for i in range(want)})


def _cover_to(im, w: int, h: int, keep_alpha: bool = False):
    """等比铺满 + 居中裁剪（不拉伸）。

    keep_alpha=True 时保留透明通道（透明 GIF / 带 alpha 的 WebP）——光栅卡要靠这个
    alpha 叠到背景着色器上，否则透明区域会被填成黑色把背景整片盖住。
    """
    from PIL import Image
    if keep_alpha:
        im = im.convert("RGBA")
    else:
        im = im.convert("RGB") if im.mode not in ("RGB", "L") else im
    sw, sh = im.size
    if sw <= 0 or sh <= 0:
        return Image.new("RGBA" if keep_alpha else "RGB", (w, h),
                         (0, 0, 0, 0) if keep_alpha else (0, 0, 0))
    k = max(w / sw, h / sh)
    nw, nh = max(w, int(round(sw * k))), max(h, int(round(sh * k)))
    im = im.resize((nw, nh))
    x, y = (nw - w) // 2, (nh - h) // 2
    return im.crop((x, y, x + w, y + h))


def _gif_clear_black_bg(im):
    """修 disposal=2 的透明 GIF：Pillow 合成时会用**不透明黑**填充「恢复背景」的区域，
    于是抽出来的帧带大片黑块（透明 GIF 叠背景时就是三道黑缝）。

    只在黑块占比 > 5% 时才做色键，避免误伤本来就有黑底/黑发的素材。
    """
    try:
        import numpy as np
        from PIL import Image
        a = np.asarray(im).copy()
        dark = (a[..., 3] > 200) & (a[..., :3].max(axis=2) < 6)
        if dark.mean() < 0.05:
            return im
        a[dark] = (0, 0, 0, 0)
        return Image.fromarray(a)
    except Exception:  # noqa: BLE001
        return im


def _video_frames(src: Path, want: int):
    """均匀抽帧，返回原始分辨率的 PIL 图列表（GIF/动画 WebP 走 PIL，视频走 cv2）。

    ★ GIF / 动画 WebP 必须按 RGBA 抽：透明底的 GIF 是「主体贴片」用法，
      一旦 convert("RGB") 透明区域就变黑，卡面会整片盖住背景着色器。
    """
    from PIL import Image
    ext = src.suffix.lower()
    if ext in (".gif", ".webp", ".apng", ".png"):
        with Image.open(src) as im:
            total = int(getattr(im, "n_frames", 1) or 1)
            alpha = (im.mode in ("RGBA", "LA", "PA", "P")
                     or "transparency" in im.info)
            out = []
            for i in _even_idx(total, want):
                im.seek(i)
                fr = im.convert("RGBA" if alpha else "RGB")
                if alpha:
                    fr = _gif_clear_black_bg(fr)
                out.append(fr.copy())
            return out
    try:
        import cv2
    except ImportError as e:
        raise ValueError("处理视频需要 opencv-python：pip install opencv-python") from e
    cap = cv2.VideoCapture(str(src))
    if not cap.isOpened():
        raise ValueError("这个视频打不开（编码不支持？换成 mp4/H.264 再试）")
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    out = []
    if total > 0:
        for i in _even_idx(total, want):
            cap.set(cv2.CAP_PROP_POS_FRAMES, i)
            ok, fr = cap.read()
            if ok and fr is not None:
                out.append(Image.fromarray(cv2.cvtColor(fr, cv2.COLOR_BGR2RGB)))
    else:
        # 部分容器读不到帧数：顺序 grab 一遍再回头抽
        while True:
            ok = cap.grab()
            if not ok:
                break
            total += 1
        for i in _even_idx(total, want):
            cap.set(cv2.CAP_PROP_POS_FRAMES, i)
            ok, fr = cap.read()
            if ok and fr is not None:
                out.append(Image.fromarray(cv2.cvtColor(fr, cv2.COLOR_BGR2RGB)))
    cap.release()
    if not out:
        raise ValueError("抽帧失败：视频是空的或编码无法解码")
    return out


def _lenti_vresult(key: str, md5: str, safe: str) -> dict:
    """图集缓存的对外描述（上传与改帧数共用）。"""
    d = LENTI_VIDEO_DIR / key
    try:
        meta = json.loads((d / "info.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        meta = {"name": safe, "frames": 2, "cols": 1, "rows": 2}
    base = "/lenti/_video/" + key
    return {"ok": True, "key": key, "hash": md5, "atlas": "lvid:" + key,
            "url": base + "/atlas.webp", "front": base + "/frame0.webp",
            "frames": int(meta.get("frames") or 2),
            "atlasCols": int(meta.get("cols") or 1),
            "atlasRows": int(meta.get("rows") or 1),
            "want": int(meta.get("want") or meta.get("frames") or 2),
            "alpha": bool(meta.get("alpha")),
            "name": meta.get("name") or safe}


def _build_lenti_atlas(src: Path, key: str, want: int, safe: str) -> None:
    """抽 want 帧 → 拼一张图集存 _video/<key>/（已存在则直接复用）。"""
    from PIL import Image as _I
    d = LENTI_VIDEO_DIR / key
    if (d / "atlas.webp").exists():
        return
    LENTI_VIDEO_DIR.mkdir(parents=True, exist_ok=True)
    work = LENTI_VIDEO_DIR / (key + ".tmp")
    if work.exists():
        shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True)
    try:
        frames = _video_frames(src, want)
        n = max(2, min(len(frames), LENTI_VIDEO_FRAME_MAX))
        frames = frames[:n]
        cols, rows = _lenti_vgrid(n)
        cw = LENTI_VIDEO_CELL_W
        ch = max(2, int(round(cw / LENTI_ASPECT)))
        # 有透明像素就走 RGBA 图集：shader 用 alpha 把帧叠在背景上（透明处露背景），
        # 全不透明的视频仍存 RGB，体积小 1/3。
        has_a = False
        for f in frames:
            if f.mode == "RGBA":
                try:
                    if f.getchannel("A").getextrema()[0] < 250:
                        has_a = True
                        break
                except (OSError, ValueError):
                    pass
        cell = _cover_to(frames[0], cw, ch, keep_alpha=has_a)
        atlas = _I.new(cell.mode, (cols * cw, rows * ch),
                       (0, 0, 0, 0) if has_a else (0, 0, 0))
        for i, f in enumerate(frames):
            atlas.paste(_cover_to(f, cw, ch, keep_alpha=has_a),
                        ((i % cols) * cw, (i // cols) * ch))
        atlas.save(work / "atlas.webp", "WEBP", quality=86, method=4,
                   **({"alpha_quality": 100} if has_a else {}))
        fw = 900
        _cover_to(frames[0], fw, max(2, int(round(fw / LENTI_ASPECT))),
                  keep_alpha=has_a).save(
            work / "frame0.webp", "WEBP", quality=92, method=4,
            **({"alpha_quality": 100} if has_a else {}))
        (work / "info.json").write_text(json.dumps(
            {"name": safe, "frames": n, "want": want, "cols": cols, "rows": rows,
             "alpha": has_a, "cell": [cw, ch]}, ensure_ascii=False), encoding="utf-8")
        if d.exists():
            shutil.rmtree(d, ignore_errors=True)
        work.rename(d)
    except Exception:
        # 失败别把半成品留在缓存里，否则下次同文件会「命中」一个坏目录
        shutil.rmtree(work, ignore_errors=True)
        raise


def _make_lenti_video(raw: bytes, name: str, frames=None) -> dict:
    """视频/GIF → 光栅图集（一张图，1 个采样器）。按内容哈希+帧数缓存，同参数不重复抽帧。"""
    if not raw:
        raise ValueError("没收到文件内容")
    if len(raw) > LENTI_VIDEO_MAX_BYTES:
        raise ValueError("文件太大（>60MB），先压一下或截短")
    safe = _safe_name((name or "clip.mp4").replace("\\", "/").split("/")[-1])
    if not safe.lower().endswith(_VIDEO_EXTS):
        raise ValueError("不支持的格式：%s（支持 mp4/webm/mov/gif）" % Path(safe).suffix)
    want = _lenti_vframes(frames)
    md5 = hashlib.md5(raw).hexdigest()[:12]
    key = "%s-f%d" % (md5, want)
    # 原始文件在 _video/_src 留一份：之后改帧数只要回传 hash，不用把几十 MB 重传一遍
    src = LENTI_VIDEO_SRC_DIR / (md5 + Path(safe).suffix.lower())
    if not src.exists():
        LENTI_VIDEO_SRC_DIR.mkdir(parents=True, exist_ok=True)
        tmp = LENTI_VIDEO_SRC_DIR / (src.name + ".part")
        tmp.write_bytes(raw)
        tmp.replace(src)
        # 原名也记下来：改帧数时要用它当显示名，不然会变成 "<hash>.mp4"
        (LENTI_VIDEO_SRC_DIR / (md5 + ".json")).write_text(
            json.dumps({"name": safe}, ensure_ascii=False), encoding="utf-8")
    _build_lenti_atlas(src, key, want, safe)
    return _lenti_vresult(key, md5, safe)


def _reframe_lenti_video(hash_or_key: str, frames=None) -> dict:
    """改帧数重抽（用缓存的原始文件，不用重新上传）。"""
    md5 = re.sub(r"-f\d+$", "", str(hash_or_key or "").split(":")[-1]).strip()
    if not re.fullmatch(r"[0-9a-f]{6,32}", md5 or ""):
        raise ValueError("视频缓存标识无效，请重新上传")
    want = _lenti_vframes(frames)
    key = "%s-f%d" % (md5, want)
    d = LENTI_VIDEO_DIR / key
    if not (d / "atlas.webp").exists():
        # 只认视频扩展名：_src 里同名还有个 .json 存原始文件名，不能当成源
        cands = ([c for c in sorted(LENTI_VIDEO_SRC_DIR.glob(md5 + ".*"))
                  if c.suffix.lower() in _VIDEO_EXTS]
                 if LENTI_VIDEO_SRC_DIR.is_dir() else [])
        if not cands:
            raise ValueError("原始视频已不在缓存里（被清理过），请重新上传一次")
        try:
            safe = json.loads((LENTI_VIDEO_SRC_DIR / (md5 + ".json"))
                              .read_text(encoding="utf-8")).get("name") or ""
        except (OSError, ValueError):
            safe = ""
        safe = _safe_name(safe or cands[0].name)
        _build_lenti_atlas(cands[0], key, want, safe)
    return _lenti_vresult(key, md5, "clip")


def _lenti_video_split(raw: bytes, name: str, frames=None) -> dict:
    """视频/GIF → 2~6 张独立帧图，喂光栅卡工作页的多槽位预览。

    与 `_make_lenti_video` 的分工：
      · 那边拼一张图集（8~36 帧、只占 1 个采样器），给成品卡 shader 用；
      · 这边输出 N 张独立 webp，直接进 `_lenti_panel` 预览页的 6 个贴图槽位。
    好处是完全复用预览页现有渲染路径（不改 shader、不动 uT0..uT5），代价是
    帧数上限 6 —— 要 8 帧以上的完整效果请用拼装页「🎞️ 光栅卡卡面」。
    """
    if not raw:
        raise ValueError("没收到文件内容")
    if len(raw) > LENTI_VIDEO_MAX_BYTES:
        raise ValueError("文件太大（>60MB），先压一下或截短")
    safe = _safe_name((name or "clip.mp4").replace("\\", "/").split("/")[-1])
    if not safe.lower().endswith(_VIDEO_EXTS):
        raise ValueError("不支持的格式：%s（支持 mp4/webm/mov/gif/webp）" % Path(safe).suffix)
    try:
        want = int(round(float(frames or 6)))
    except (TypeError, ValueError):
        want = 6
    want = max(2, min(6, want))

    token = uuid.uuid4().hex[:10]
    _lenti_gc()
    work = lenticular.LENTI_ROOT / "_work" / token
    work.mkdir(parents=True, exist_ok=True)
    src = work / ("_src" + Path(safe).suffix.lower())
    src.write_bytes(raw)
    try:
        imgs = _video_frames(src, want)
    finally:
        # 原始视频别留在产物目录里：_lenti_save 是整目录 copytree，几十 MB 会被一起搬走
        try:
            src.unlink()
        except OSError:
            pass
    if not imgs:
        raise ValueError("抽帧失败：视频是空的或编码无法解码")

    has_a = False
    for f in imgs:
        if f.mode == "RGBA":
            try:
                if f.getchannel("A").getextrema()[0] < 250:
                    has_a = True
                    break
            except (OSError, ValueError):
                pass

    fw, fh = lenticular.frame_size(lenticular.DEFAULT_ASPECT)
    names = []
    for i, f in enumerate(imgs, 1):
        nm = "f%02d.webp" % i
        _cover_to(f, fw, fh, keep_alpha=has_a).save(
            work / nm, "WEBP", quality=92, method=4,
            **({"alpha_quality": 100} if has_a else {}))
        names.append(nm)
    (work / "meta.json").write_text(json.dumps(
        {"images": names, "aspect": lenticular.DEFAULT_ASPECT, "opts": None,
         "source": safe, "frames": len(names), "alpha": has_a},
        ensure_ascii=False), encoding="utf-8")
    return {"ok": True, "token": token, "base": "/lenti/_work/" + token + "/",
            "images": names, "count": len(names), "mode": "video", "alpha": has_a}


def _lenti_o(data: dict) -> dict:
    """把 lenti 与内嵌 opts 摊平（视频卡参数在 opts 下，写卡时在顶层）。"""
    o = data.get("lenti") or {}
    if not isinstance(o, dict):
        return {}
    if isinstance(o.get("opts"), dict):
        return {**o, **o["opts"]}
    return o


def _lenti_atlas_of(data: dict):
    """视频/GIF 光栅卡：找到图集与首帧的实际文件。

    两种来源：
      lenti.atlas = "lvid:<key>"      → 抽帧缓存 assets/lenticular/_video/<key>/
      lenti.atlas = "lenti/atlas.webp"→ 卡目录自持（批量脚本出的卡 / 旧卡，没有 atlas_src）
    后者要靠 data["card"] 定位卡目录；两处都没有就返回 None（当普通卡处理）。
    """
    o = _lenti_o(data)
    spec = str(o.get("atlas") or "").strip()
    frames = int(o.get("frames") or 0) or 2
    cols = int(o.get("atlasCols") or 0) or 1
    rows = int(o.get("atlasRows") or 0) or 1
    if spec.startswith("lvid:"):
        key = _safe_name(spec.split(":", 1)[1])
        d = LENTI_VIDEO_DIR / key
        if not (d / "atlas.webp").is_file():
            return None
        frame0 = d / "frame0.webp"
        if not frame0.is_file():
            frame0 = d / "atlas.webp"
        try:
            meta = json.loads((d / "info.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            meta = {}
        return {"dir": d, "atlas": d / "atlas.webp", "frame0": frame0,
                "frames": int(meta.get("frames") or frames),
                "cols": int(meta.get("cols") or cols),
                "rows": int(meta.get("rows") or rows),
                "alpha": bool(meta.get("alpha")),
                "name": str(o.get("vname") or ""),
                "atlas_url": "/lenti/_video/" + key + "/atlas.webp",
                "front_url": "/lenti/_video/" + key + "/frame0.webp"}
    if spec.startswith("lenti/"):
        card = str(data.get("card") or "").strip()
        if not card or "/" in card or "\\" in card or card in (".", ".."):
            return None
        d = ASSETS / "output" / card / "lenti"
        atlas = d / "atlas.webp"
        if not atlas.is_file():
            return None
        frame0 = d / "f00.webp"
        if not frame0.is_file():
            # 老卡（批量脚本出的）只存了图集：先从图集左上角裁一格当首帧，落地缓存
            frame0 = _atlas_first_cell(atlas, cols, rows, d / "f00.webp")
        try:
            meta = json.loads((d / "info.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            meta = {}
        return {"dir": d, "atlas": atlas, "frame0": frame0,
                "frames": int(meta.get("frames") or frames),
                "cols": int(meta.get("cols") or cols),
                "rows": int(meta.get("rows") or rows),
                "alpha": bool(meta.get("alpha")),
                "name": str(o.get("vname") or card),
                "atlas_url": f"/preview/{card}/lenti/atlas.webp",
                "front_url": (f"/preview/{card}/lenti/f00.webp"
                              if (d / "f00.webp").is_file() else f"/preview/{card}/lenti/atlas.webp")}
    return None


def _atlas_first_cell(atlas: Path, cols: int, rows: int, dst: Path) -> Path:
    """从图集裁出第 0 格（左上）当首帧；失败就退回整张图集。"""
    try:
        from PIL import Image
        cols = max(1, int(cols))
        rows = max(1, int(rows))
        with Image.open(atlas) as im:
            cw = im.width // cols
            ch = im.height // rows
            im.crop((0, 0, cw, ch)).save(dst, "WEBP", quality=92, method=4)
        return dst
    except Exception:  # noqa: BLE001
        return atlas


def _card_front_path(card_sel: str):
    """成品卡卡面实际文件：卡目录 front.webp/front.png 优先，否则按卡定义找回图层
    （光栅卡的卡面是 lenti/f00.webp，目录里没有 front.png）。"""
    d = ASSETS / "output" / card_sel
    for n in ("front.webp", "front.png"):
        if (d / n).exists():
            return d / n
    return _card_layer(card_sel, "face") or _card_layer(card_sel, "background")


def _card_front_url(card_sel: str):
    """/_preview/ 路由只映射 assets/{ripit,output}，卡目录内相对路径都能走通。"""
    p = _card_front_path(card_sel)
    if not p:
        return None
    d = ASSETS / "output" / card_sel
    try:
        rel = Path(p).resolve().relative_to(d.resolve()).as_posix()
        return f"/preview/{card_sel}/{rel}"
    except ValueError:
        return None


def _lenti_opts_of(data: dict) -> dict:
    """拼装页的光栅参数（只取认识的键，数值键全部转 float）。

    fade 是字符串（stripe/whole/cut），单独放过；cards/<id>.json 里 video-lenti
    卡把参数藏在 lenti.opts 下，这里一并展开。
    """
    o = data.get("lenti") or {}
    if isinstance(o.get("opts"), dict):
        o = {**o, **o["opts"]}
    out = {}
    fd = str(o.get("fade") or "").strip().lower()
    if fd in ("stripe", "whole", "cut"):
        out["fade"] = fd
    for k in ("angle", "lens", "stripe", "sheen", "sheenPow", "soft",
              "jitter", "gain", "bend", "gloss"):
        v = o.get(k)
        if v is None or v == "":
            continue
        try:
            out[k] = float(v)
        except (TypeError, ValueError):
            continue
    return out


def _write_preview_html(html: str) -> dict:
    """把预览页写进 _preview/<token>/ 并返回访问 URL。

    ★ 3D 预览现在自己建 token 目录（要先落视差层图再生成 html），这里只服务
      不需要额外素材的预览场景。
    """
    token = uuid.uuid4().hex[:12]
    d = PREVIEW_DIR / token
    d.mkdir(parents=True, exist_ok=True)
    (d / "preview.html").write_text(html, encoding="utf-8")
    _cleanup_old_previews()
    return {"ok": True, "url": f"/preview/_preview/{token}/preview.html"}


def _export_card(card_id: str, etype: str):
    """导出成品卡：image → card.webp；html → 自包含单文件（同目录图片 base64 内嵌）。

    返回 (body, mime, filename)；卡片缺失或类型非法返回 None。
    """
    cid = _safe_name(card_id)
    d = card_dir(cid)
    if not cid or not d.is_dir():
        return None
    if etype == "image":
        n = _big_name(d)
        if not n:
            return None
        p = d / n
        # 成品图统一 WebP（PNG 同尺寸 1.75 MB → 0.17 MB）。磁盘已是 webp 就字节直搬；
        # 老的 card.png 现转一次，别让导出入口还吐 PNG。
        if n.endswith(".webp"):
            return (p.read_bytes(), "image/webp", f"{cid}.webp")
        from PIL import Image
        buf = io.BytesIO()
        Image.open(p).save(buf, "WEBP", quality=cardforge.PREVIEW_QUALITY, method=4)
        return (buf.getvalue(), "image/webp", f"{cid}.webp")
    if etype == "html":
        p = d / "card.html"
        if not p.exists():
            return None
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
        return (html.encode("utf-8"), "text/html; charset=utf-8", f"{cid}.html")
    return None


_LENTI_PANEL = r"""
<div class="sec">
  <h2>图源（1~6 张）</h2>
  <textarea id="gPaths" spellcheck="false" placeholder="每行一个图片路径，例如：&#10;E:/Shadowverse2/Sprites/卡面/1000110100.png&#10;也可以直接「上传图片」"></textarea>
  <div class="btnrow" style="margin-top:8px">
    <div class="btn" id="gAdd">加入路径</div>
    <div class="btn" id="gPick">上传图片</div>
    <div class="btn" id="gClear">清空</div>
  </div>
  <input type="file" id="gFile" accept="image/*" multiple hidden>
  <div class="chips" id="gChips"></div>
</div>
<div class="sec">
  <h2>视频 / GIF 图源</h2>
  <div class="sub">均匀抽 2~6 帧当「变画」帧，直接进左侧 3D 预览（透明 GIF 的透明区照常透出）。
    要 8~36 帧的完整光栅卡，请用拼装页「🎞️ 光栅卡卡面」。</div>
  <div class="btnrow" style="margin-top:8px">
    <div class="btn" id="vPick">选择视频 / GIF</div>
    <div class="btn" id="vClear">清空</div>
  </div>
  <div class="btnrow" style="margin-top:8px">
    <div class="btn" data-vframes="3">3 帧</div>
    <div class="btn" data-vframes="4">4 帧</div>
    <div class="btn on" data-vframes="6">6 帧</div>
  </div>
  <input type="file" id="vFile" accept="video/*,.gif,.webp,.apng" hidden>
  <div class="sub" id="vMsg" style="margin-top:6px"></div>
</div>
<div class="sec">
  <h2>生成</h2>
  <div class="btnrow">
    <div class="btn on" data-lmode="flip">变画（N 张交替）</div>
    <div class="btn" data-lmode="3d">立体（视差序列）</div>
  </div>
  <div class="btnrow" style="margin-top:8px">
    <div class="btn" id="gBuild">生成预览</div>
    <div class="btn" id="gSave">保存到光栅卡库</div>
  </div>
  <div class="sub" id="gMsg"></div>
</div>
"""

_LENTI_JS = r"""
<script>
(function () {
  const q = [];            // [{spec, name}]
  const chips = document.getElementById('gChips');
  const msg = document.getElementById('gMsg');
  let mode = 'flip';
  let token = '';

  function say(t, err) {
    msg.textContent = t || '';
    msg.style.color = err ? 'var(--err)' : 'var(--muted)';
  }
  function draw() {
    chips.innerHTML = q.map((it, i) =>
      '<span class="chip">' + (i + 1) + '. ' + it.name +
      '<b data-del="' + i + '">×</b></span>').join('');
  }
  chips.addEventListener('click', (e) => {
    const i = e.target.dataset && e.target.dataset.del;
    if (i !== undefined) { q.splice(+i, 1); draw(); }
  });

  document.getElementById('gAdd').onclick = async () => {
    const txt = document.getElementById('gPaths').value || '';
    const lines = txt.split(/\r?\n/).map((x) => x.trim()).filter(Boolean);
    if (!lines.length) { say('先填路径，或直接上传图片', true); return; }
    for (const ln of lines) {
      try {
        const r = await fetch('/api/lenti/probe', {
          method: 'POST', headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({path: ln})
        }).then((x) => x.json());
        if (!r.ok) { say(r.error || ('读不到: ' + ln), true); continue; }
        q.push({spec: 'path:' + r.path, name: r.name});
      } catch (err) { say(String(err), true); }
    }
    document.getElementById('gPaths').value = '';
    draw();
    if (q.length) say('队列 ' + q.length + ' 张');
  };

  document.getElementById('gPick').onclick = () => document.getElementById('gFile').click();
  document.getElementById('gFile').onchange = async (e) => {
    for (const f of e.target.files) {
      const b64 = await new Promise((res) => {
        const fr = new FileReader();
        fr.onload = () => res(String(fr.result).split(',')[1]);
        fr.readAsDataURL(f);
      });
      const r = await fetch('/api/tmp_upload', {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({b64: b64, name: f.name})
      }).then((x) => x.json());
      if (r.ok) q.push({spec: 'tmp:' + r.name, name: f.name});
    }
    e.target.value = '';
    draw();
    if (q.length) say('队列 ' + q.length + ' 张');
  };
  document.getElementById('gClear').onclick = () => { q.length = 0; draw(); token = ''; say(''); };

  document.querySelectorAll('[data-lmode]').forEach((b) => {
    b.onclick = () => {
      mode = b.dataset.lmode;
      document.querySelectorAll('[data-lmode]').forEach((x) => x.classList.toggle('on', x === b));
    };
  });

  // ---- 视频 / GIF 图源：抽 N 帧当变画帧，直接进 3D 预览 ----
  const vMsg = document.getElementById('vMsg');
  let vFrames = 6;
  function vsay(t, err) {
    vMsg.textContent = t || '';
    vMsg.style.color = err ? 'var(--err)' : 'var(--muted)';
  }
  document.querySelectorAll('[data-vframes]').forEach((b) => {
    b.onclick = () => {
      vFrames = +b.dataset.vframes;
      document.querySelectorAll('[data-vframes]').forEach((x) => x.classList.toggle('on', x === b));
    };
  });
  document.getElementById('vPick').onclick = () => document.getElementById('vFile').click();
  document.getElementById('vClear').onclick = () => { vsay(''); token = ''; };
  document.getElementById('vFile').onchange = async (e) => {
    const f = e.target.files && e.target.files[0];
    e.target.value = '';
    if (!f) return;
    vsay('抽帧中…（大文件要等一会）');
    try {
      const r = await fetch('/api/lenti/video/split?frames=' + vFrames, {
        method: 'POST',
        headers: {'Content-Type': 'application/octet-stream',
                  'X-File-Name': encodeURIComponent(f.name)},
        body: f
      }).then((x) => x.json());
      if (!r.ok) { vsay(r.error || '抽帧失败', true); return; }
      token = r.token;
      await window.__lentiAPI.setImages(r.images.map((n) => ({src: r.base + n, name: n})));
      vsay('已抽 ' + r.count + ' 帧' + (r.alpha ? '（含透明通道）' : '') + ' · 左右拖动卡牌换帧');
    } catch (err) {
      vsay(String(err), true);
    }
  };

  document.getElementById('gBuild').onclick = async () => {
    if (!q.length) { say('队列是空的', true); return; }
    if (q.length > 6) { say('最多 6 张（变画卡建议 2~3 张）', true); return; }
    say('生成中…');
    const r = await fetch('/api/lenti/build', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({specs: q.map((x) => x.spec), mode: mode})
    }).then((x) => x.json()).catch((e) => ({ok: false, error: String(e)}));
    if (!r.ok) { say(r.error || '生成失败', true); return; }
    token = r.token;
    await window.__lentiAPI.setImages(r.images.map((n) => ({src: r.base + n, name: n})));
    say('已生成 ' + r.images.length + ' 帧 · ' + (mode === '3d' ? '立体视差' : '变画'));
  };

  document.getElementById('gSave').onclick = async () => {
    if (!token) { say('先生成预览', true); return; }
    const name = prompt('给这张光栅卡起个名字', q[0] ? q[0].name.replace(/\.[^.]+$/, '') : '光栅卡');
    if (!name) return;
    const r = await fetch('/api/lenti/save', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({token: token, name: name})
    }).then((x) => x.json()).catch((e) => ({ok: false, error: String(e)}));
    if (!r.ok) { say(r.error || '保存失败', true); return; }
    say('已保存：' + r.url + '（双击 assets/lenticular 里的 lenti.html 也能离线打开）');
    window.open(r.url, '_blank');
  };

  window.__lentiGenInit = () => { draw(); say(''); };
})();
</script>
"""


def build_lenti_page() -> str:
    """光栅卡工作页：右侧面板选图 / 生成 / 保存，左侧 3D 实时预览。"""
    return lenticular.build_lenti_html(
        [], title="光栅卡 · cardforge",
        three_rel=lenticular.THREE_SRC_ABS,
        top=_LENTI_PANEL + _LENTI_JS,
        boot_extra="window.__lentiGenInit && window.__lentiGenInit();",
    )


def _lenti_gc(max_age: float = 86400 * 3) -> None:
    """清掉 3 天前的临时预览目录（assets/lenticular/_work/<token>）。"""
    work = lenticular.LENTI_ROOT / "_work"
    if not work.is_dir():
        return
    now = time.time()
    for d in work.iterdir():
        try:
            if d.is_dir() and now - d.stat().st_mtime > max_age:
                shutil.rmtree(d, ignore_errors=True)
        except Exception:
            pass


def build_page() -> str:
    """生成主页面 HTML（素材列表在启动时注入，刷新即最新）。"""
    bg_options = ["", *list_assets("backgrounds")]
    frame_options = ["", *list_assets("frames")]
    seal_options = ["", *list_assets("seals")]
    model_options = [(k, v) for k, v in SUPPORTED_MODELS.items()]
    default_model = DEFAULT_MODEL
    if get_api_config():
        model_options.append(("api", "云端抠图 API（阿里云分割抠图，settings.json 已配置）"))
        default_model = "api"
    assets_json = json.dumps({
        "backgrounds": bg_options,
        "frames": frame_options,
        "frames_text": list_assets("frames-text"),
        "seals": seal_options,
        "faces": list_assets("faces"),
        "backs": list_assets("backs"),
        "effect_images": list_assets("effects-images"),
        "glows": [*webcard.GLOW_EFFECTS.keys(), *webcard.list_custom_glows()],
    }, ensure_ascii=False)
    # 素材包素材的来源标注（key → 显示标签），供下拉/缩略图打卡
    asset_meta = {}
    for _t in ("backgrounds", "frames", "seals", "faces", "backs", "frames-text"):
        for _a in packs.list_assets(_t):
            if not _a["builtin"]:
                asset_meta[_a["key"]] = {"label": f'{_a["name"]}（{_a["pack_name"]}）',
                                         "pack": _a["pack"], "pack_name": _a["pack_name"]}
    asset_meta_json = json.dumps(asset_meta, ensure_ascii=False)
    packs_json = json.dumps(_packs_for_ui(), ensure_ascii=False)
    models_json = json.dumps(model_options, ensure_ascii=False)
    effects_json = json.dumps(list_effects(), ensure_ascii=False)
    # 拼装页成品下拉只列收集册可见的卡（assets/output）。异兽牌那 1800 版住在
    # assets/ripit，按约定只在「图鉴」里展示，不进成品选择 —— 否则下拉被上千项冲垮。
    cards_json = json.dumps(album_cards(), ensure_ascii=False)
    return (
        PAGE_HTML
        .replace("__ASSETS_JSON__", assets_json)
        .replace("__ASSET_META_JSON__", asset_meta_json)
        .replace("__PACKS_JSON__", packs_json)
        .replace("__MODELS_JSON__", models_json)
        .replace("__EFFECTS_JSON__", effects_json)
        .replace("__CARDS_JSON__", cards_json)
        .replace("__DEFAULT_MODEL__", json.dumps(default_model))
    )


def _packs_for_ui() -> list[dict]:
    """素材包管理页用的精简列表。"""
    out = []
    for p in packs.scan():
        out.append({
            "id": p["id"] or p["dir"],
            "dir": p["dir"],
            "name": p["name"],
            "author": p["author"],
            "description": p["description"],
            "thumbnail": p["thumbnail"],
            "enabled": p["enabled"],
            "error": p["error"],
            "counts": p["counts"],
            "total": p["total"],
        })
    return out


def build_cutout_page() -> str:
    """生成独立抠图工具页面（与卡牌无关，仅抠图 + 下载透明 PNG）。"""
    models = [(k, v) for k, v in SUPPORTED_MODELS.items() if k not in ("none", "sam")]
    models_json = json.dumps(models, ensure_ascii=False)
    return (
        CUTOUT_HTML
        .replace("__MODELS_JSON__", models_json)
        .replace("__DEFAULT_MODEL__", json.dumps(DEFAULT_MODEL))
    )


def _seal_strengths(data: dict) -> tuple[float, float]:
    """解析卡封强度（正面/背面），非法值回退默认 0.945/0.5775。"""
    def _f(v: object, dflt: float) -> float:
        try:
            return min(2.0, max(0.0, float(v)))
        except (TypeError, ValueError):
            return dflt
    return (_f(data.get("seal_strength_front"), 0.945),
            _f(data.get("seal_strength_back"), 0.5775))


def _seal_frame_strengths(data: dict) -> tuple[float, float]:
    """解析层2 边框卡封强度（正面/背面），非法值回退默认 0.8/0.5。"""
    def _f(v: object, dflt: float) -> float:
        try:
            return min(2.0, max(0.0, float(v)))
        except (TypeError, ValueError):
            return dflt
    return (_f(data.get("seal_frame_strength_front"), 0.8),
            _f(data.get("seal_frame_strength_back"), 0.5))


def _file_digest(p: Path) -> str:
    import hashlib
    h = hashlib.md5()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _match_asset_by_bytes(digest: str, *dirs: str) -> str | None:
    """在素材目录中按文件字节匹配（旧拼装卡只存了输出副本，靠字节反查原始素材名）。"""
    for sub in dirs:
        d = ASSETS / sub
        if not d.is_dir():
            continue
        for f in sorted(d.iterdir()):
            if f.is_file() and f.suffix.lower() in _IMG_EXTS and _file_digest(f) == digest:
                return f.name
    return None


def _inherit_card(card_id: str, _parsed: dict | None = None) -> dict:
    """读取成品卡 JSON，返回其边框/卡封/牌背/强度等配置（供拼装预览与保存沿用）。

    素材名优先级：
      1) 显式标记（JSON 顶层含 frame_name/seal_name/back_name 键，值为 null 表示"确实未使用"）；
      2) layers/顶层字段的素材路径 basename（生成卡的素材路径可直接取文件名）；
      3) 旧拼装卡只存输出目录副本（frame.png 等），按字节在素材库中反查。

    `_parsed`：调用方已经解析好的 JSON。list_cards 要给 1802 张卡逐个取配置，
    传进来就能省掉一次重复读盘（实测占它 1 秒）。
    """
    if not card_id:
        return {}
    if _parsed:
        d = _parsed
    else:
        cf = PROJECT_ROOT / "cards" / f"{card_id}.json"
        if not cf.exists():
            return {}
        try:
            d = json.loads(cf.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
    layers = d.get("layers") or {}
    extra = d.get("extra") or {}
    out: dict = {}
    name_keys = {"frame": "frame_name", "seal": "seal_name", "seal_frame": "seal_frame_name", "back": "back_name"}
    for k, copy_name, dirs in (
        ("frame", "frame.png", ("frames", "frames-text")),
        ("seal", "seal.png", ("seals",)),
        ("seal_frame", "seal_frame.png", ("seals",)),
        ("back", "back.png", ("backs",)),
    ):
        nk = name_keys[k]
        # 1) 显式标记权威：拼装保存总是写入该键（未用即 null），避免旧副本被误判为已使用
        if nk in d or nk in extra:
            v = d.get(nk) if nk in d else extra.get(nk)
            out[k] = v or None
            continue
        # 2) layers 原始素材路径 basename（生成卡/拼装卡通用）
        p = layers.get(k) or (d.get("back") if k == "back" else None)
        if p:
            bn = Path(p).name
            if bn not in ("frame.png", "seal.png", "back.png"):
                out[k] = bn
                continue
        # 3) 旧拼装卡：只存输出副本，按字节在素材库反查
        cp = ASSETS / "output" / card_id / copy_name
        if cp.exists():
            m = _match_asset_by_bytes(_file_digest(cp), *dirs)
            if m:
                out[k] = m
    out["seal_name"] = d.get("seal_name") or extra.get("seal_name")
    out["seal_frame_name"] = d.get("seal_frame_name") or extra.get("seal_frame_name")
    # 边框卡封"与卡封一致"标记：边框卡封与卡牌卡封使用同一素材（只记录一次）
    out["seal_frame_same"] = bool(d.get("seal_frame_same") or extra.get("seal_frame_same"))
    # 卡面/背景回退：旧卡 JSON 未显式标记时，按 layers 里的素材路径反查
    # （front.png/foreground.png 等副本名不是素材名，跳过）
    def _layer_asset(key: str, dirs: tuple[str, ...]):
        p = layers.get(key)
        if not p:
            return None
        bn = Path(str(p)).name
        if bn in ("front.png", "foreground.png", "back.png", "frame.png", "seal.png", "seal_frame.png"):
            return None
        for _d in dirs:
            if (ASSETS / _d / bn).exists():
                return bn
        return None

    out["face_name"] = (d.get("face_name") or extra.get("face_name")) or _layer_asset("face", ("faces",))
    out["background_name"] = (d.get("background_name") or extra.get("background_name")) or _layer_asset("background", ("backgrounds",))
    # 主体与卡面是同一类素材（早期误拆成两个目录，subjects/ 已并入 faces/）
    out["subject_name"] = (d.get("subject_name") or extra.get("subject_name")) or _layer_asset("subject", ("faces",))
    out["seal_strength_front"] = d.get("seal_strength_front", extra.get("seal_strength_front"))
    out["seal_strength_back"] = d.get("seal_strength_back", extra.get("seal_strength_back"))
    out["seal_frame_strength_front"] = d.get("seal_frame_strength_front", extra.get("seal_frame_strength_front"))
    out["seal_frame_strength_back"] = d.get("seal_frame_strength_back", extra.get("seal_frame_strength_back"))
    out["subject_over_frame"] = bool(d.get("subject_over_frame", extra.get("subject_over_frame")))
    out["subject_outline"] = bool(d.get("subject_outline", extra.get("subject_outline")))
    out["mask_clip"] = bool(d.get("mask_clip", extra.get("mask_clip", True)))
    return out


def _load_settings() -> dict:
    try:
        if SETTINGS_FILE.exists():
            return json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {}


def _save_settings(data: dict) -> None:
    s = _load_settings()
    s.update(data)
    try:
        SETTINGS_FILE.write_text(json.dumps(s, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        pass


def _get_settings() -> dict:
    """界面持久化参数（含默认值兜底）。"""
    s = _load_settings()
    front, back = _seal_strengths(s)
    sf_front, sf_back = _seal_frame_strengths(s)
    return {"seal_strength_front": front, "seal_strength_back": back,
            "seal_frame_strength_front": sf_front, "seal_frame_strength_back": sf_back}


def _save_assembly(data: dict) -> dict:
    """拼装保存：成品预览/卡面/边框/背景/牌背/卡封/特效 → assets/output/<id>/ 完整卡牌。"""
    name = (data.get("name") or "").strip() or "拼装卡牌"
    card_id = cardforge.slugify(name)
    out = ASSETS / "output" / card_id
    out.mkdir(parents=True, exist_ok=True)

    detail_rel = None  # 分层成品卡的主体层（叠在卡封之上）
    card_sel = (data.get("card") or "").strip()      # 成品预览：已完成卡牌
    face_sel = (data.get("face") or "").strip()      # 卡面/主体素材（同一类）
    face_as_fg = bool(data.get("face_as_fg"))         # 只作透明前景层（底图改用背景）
    _legacy_subject = (data.get("subject") or "").strip()  # 旧前端：主体是独立下拉
    if _legacy_subject and not face_sel:
        face_sel, face_as_fg = _legacy_subject, True
    custom_bg = (data.get("custom_bg") or "").strip()  # 背景素材
    frame_sel = (data.get("frame") or "").strip()    # 边框素材
    seal_sel = (data.get("seal") or "").strip()      # 卡封素材（层1：随内容缩放）
    seal_frame_sel = (data.get("seal_frame") or "").strip()  # 边框卡封（层2：边框作蒙版）
    # 边框卡封"与卡封一致"：与卡牌卡封使用同一素材（只记录一次）
    seal_frame_same = seal_frame_sel == "same"
    if seal_frame_same:
        seal_frame_sel = seal_sel
    fg_url = (data.get("foreground") or "").strip()
    parallax_sel = (data.get("parallax") or "").strip()   # 视差主体 id（可空）
    back = (data.get("back") or "sample-1.png").strip()
    desc = (data.get("desc") or "").strip() or None
    text_type = data.get("text_type") or "none"
    if desc and text_type == "none":
        text_type = "transparent"
    text_pos = data.get("text_pos")


    def _copy_if_diff(src: Path, dst: Path) -> None:
        if src.resolve() == dst.resolve():
            return
        shutil.copy(src, dst)

    # 卡面 front：卡面素材 > 背景素材 > 成品预览（显式选择覆盖底图，均可更换/移除）
    # 卡面素材自含完整画面：选它时旧主体前景一并清空，彻底替换卡面（不再叠旧卡面）；
    # 背景素材只换底图，保留成品卡透明主体
    subject_over_frame = bool(data.get("subject_over_frame"))
    subject_outline = bool(data.get("subject_outline"))
    mask_clip = bool(data.get("mask_clip", True))
    scale = _parse_scale(data.get("scale"))
    bg_scale = _parse_scale(data.get("bg_scale"))
    frame_scale = _parse_scale(data.get("frame_scale"))
    glow_name = (data.get("glow") or "none")
    glow_strength = _parse_glow_strength(data.get("glow_strength"))
    subject_offset_x = _parse_offset(data.get("subject_offset_x"))
    subject_offset_y = _parse_offset(data.get("subject_offset_y"))
    # 卡面与主体是同一张素材的两种用法：
    #   - 默认：铺满整卡当底图（不透明图）
    #   - face_as_fg：只当前景层（抠好的透明图），底图改用「背景」
    #   - 浮于边框之上：前景层的层序提到边框之上（PVZ 式立体感）
    #   - 文本型（boxed）：卡面只在余卡面范围（卡顶到文本区顶部）呈现，底图改用背景/深色
    face_floats = face_as_fg or subject_over_frame or text_type == "boxed"
    face_used = ""
    face_p = resolve_asset("faces", face_sel)
    bg_p = resolve_asset("backgrounds", custom_bg)
    # 程序化背景（虚空式）：素材旁同名 JSON 声明 shader ⇒ 3D 卡面改由着色器生成
    bg_shader = _bg_shader_of(bg_p)
    # 背景渲染视图：背景倍率非 1 时按卡牌中心缩放并烘焙成整卡 PNG（深色底避免透明空洞）
    bg_view_p = bg_p
    # 背景素材直接引用素材库（相对路径，file:// 双击也能显示；/assets_abs 只在 HTTP 下有效）
    bg_view_url = (cardforge.lib_ref(bg_p, out) or f"/assets_abs/backgrounds/{custom_bg}") if bg_p else None
    bg_view_p = bg_p
    if bg_p and bg_scale != 1.0:
        bg_view_p = _scale_card_layer(bg_p, bg_scale, out / "background.png",
                                      base_fill=(14, 11, 9), cover=True)
        bg_view_url = "background.png"
    elif (out / "background.png").exists():
        (out / "background.png").unlink()
    # 卡面：素材库原图 → 只引用（不复制）；烘焙/生成的 → 落本地文件
    front_rel = None            # card.html 里的引用
    front_path = None           # 2D 合成 / 卡定义用的实际路径

    def _front_from_bg():
        """背景当卡面：背景被缩放过（烘焙成 background.png）时必须复制成本地卡面，
        未缩放时直接引用素材库原图。"""
        if bg_scale != 1.0:
            dst = out / "front.png"
            if Path(bg_view_p).resolve() != dst.resolve():
                shutil.copy(bg_view_p, dst)
            return "front.png", dst
        return _put_layer(bg_view_p, out, "front.png")

    # 光栅卡卡面：图源规范化成卡目录 lenti/f00.webp…（card.html 直接引用，可离线打开）；
    # 首帧同时当静态卡面（2D 成品图 / 缩略图），其余帧交给 shader 做透镜合成。
    lenti_specs = [sp for sp in (data.get("lenti_images") or []) if sp]
    lenti_opts = _lenti_opts_of(data)
    lenti_rel: list[str] = []
    lenti_video = _lenti_atlas_of(data)
    _lsrc = [] if lenti_video else lenti_src_list(data)
    if lenti_video:
        # 视频/GIF：图集 + 首帧拷进卡目录（自包含），card.html 用相对路径引用。
        # ★ 源可能就是这张卡自己的 lenti/（重开旧卡再保存）⇒ 同路径不能 copyfile（会抛
        #   SameFileError），必须比 resolve 后跳过。
        _ldst = out / "lenti"
        _ldst.mkdir(parents=True, exist_ok=True)
        _asrc = Path(lenti_video["atlas"]).resolve()
        _adst = (_ldst / "atlas.webp").resolve()
        if _asrc != _adst:
            shutil.copyfile(_asrc, _adst)
        _fsrc = Path(lenti_video["frame0"]).resolve()
        _fdst = (_ldst / "f00.webp").resolve()
        if _fsrc != _fdst:
            shutil.copyfile(_fsrc, _fdst)
        # info.json 让这张卡以后被「读取」时能复原帧数/行列/透明标记
        (_ldst / "info.json").write_text(json.dumps(
            {"name": lenti_video.get("name") or "", "frames": lenti_video["frames"],
             "cols": lenti_video["cols"], "rows": lenti_video["rows"],
             "alpha": bool(lenti_video.get("alpha"))}, ensure_ascii=False), encoding="utf-8")
        lenti_rel = ["lenti/f00.webp"]
        front_rel, front_path = lenti_rel[0], _ldst / "f00.webp"
        lenti_opts.update({"atlas": "lenti/atlas.webp", "frames": lenti_video["frames"],
                           "atlasCols": lenti_video["cols"],
                           "atlasRows": lenti_video["rows"]})
        fg_url = ""
        face_as_fg = False
    elif _lsrc:
        _ldir, _lnames = _lenti_frames(_lsrc)
        _ldst = out / "lenti"
        if _ldst.exists():
            shutil.rmtree(_ldst, ignore_errors=True)
        shutil.copytree(_ldir, _ldst)
        lenti_rel = ["lenti/" + n for n in _lnames]
        front_rel, front_path = lenti_rel[0], _ldst / _lnames[0]
        fg_url = ""
        face_as_fg = False
    if face_p and not lenti_rel:
        face_used = face_sel
        if face_as_fg or text_type == "boxed":
            # 这张图只作前景层：底图用背景，没背景时用深色底（透明图铺不了整卡）
            if bg_view_p:
                front_rel, front_path = _front_from_bg()
            else:
                Image_dark(out / "front.png")
                front_rel, front_path = "front.png", out / "front.png"
        else:
            front_rel, front_path = _put_layer(face_p, out, "front.png")
        fg_url = f"/assets_abs/faces/{face_sel}" if face_floats else ""
    elif bg_view_p and not lenti_rel:
        front_rel, front_path = _front_from_bg()
        if card_sel and not fg_url:
            fg_url = f"/preview/{card_sel}/foreground.png"
    elif card_sel and not lenti_rel and (_card_layer(card_sel, "face") or _card_layer(card_sel, "background")):
        # 成品卡当底图：源卡自己的卡面可能已外置成素材引用，走 _card_layer 找实际文件
        _src_front = _card_layer(card_sel, "face") or _card_layer(card_sel, "background")
        _copy_if_diff(_src_front, out / "front.png")
        front_rel, front_path = "front.png", out / "front.png"
        # 分层卡的主体层（立绘/图标/边框/文字），一起复制过来叠在卡封之上
        _det = ASSETS / "output" / card_sel / "detail.png"
        if _det.exists():
            _copy_if_diff(_det, out / "detail.png")
            detail_rel = "detail.png"
        if not fg_url:
            fg_url = f"/preview/{card_sel}/foreground.png"
    elif not lenti_rel:
        Image_dark(out / "front.png")
        front_rel, front_path = "front.png", out / "front.png"
    if front_rel is None:       # 兜底：绝不写坏引用
        front_rel, front_path = "front.png", out / "front.png"

    # 独立背景层素材：卡面单独缩放时背景整卡铺满（3D 独立背景层 / 2D 背景层共用）
    bg_rel_url = None
    bg_rel_path = None
    if bg_view_p:
        bg_rel_path = bg_view_p
        bg_rel_url = bg_view_url

    # 前景（无上传时留空透明层）
    if fg_url.startswith("/preview/_tmp/"):
        tmp_src = TMP_DIR / Path(fg_url).name
        if tmp_src.exists():
            _copy_if_diff(tmp_src, out / "foreground.png")
        else:
            Image_blank(out / "foreground.png")
    elif fg_url.startswith("/assets_abs/faces/"):
        _fp = resolve_asset("faces", unquote(fg_url[len("/assets_abs/faces/"):]))
        if _fp:
            _copy_if_diff(_fp, out / "foreground.png")
        else:
            Image_blank(out / "foreground.png")
    elif fg_url and (ASSETS / "output" / Path(fg_url).parts[2] / "foreground.png").exists():
        _copy_if_diff(ASSETS / "output" / Path(fg_url).parts[2] / "foreground.png", out / "foreground.png")
    else:
        Image_blank(out / "foreground.png")

    # 视差主体（视差主体编辑器里做的多层主体）：替换刚铺好的前景层。
    # 它自带每一层的透明图（栅格化到整卡尺寸）+ meta，卡里就不再是单张 foreground.png。
    # foreground.png 仍写一份各层合成图：2D 成品图、收集册缩略图、老逻辑都靠它。
    para_payload = None
    if parallax_sel:
        try:
            para_payload = parallax.bind_to_card(parallax_sel, out, CARD_W, CARD_H)
        except Exception as e:  # noqa: BLE001
            para_payload = None
            print(f"[cardforge] 视差主体应用失败（{parallax_sel}）：{e}")

    # 牌背（记录原始素材名，供再次拼装时反查）：素材库原图 → 只引用，不复制
    back_p = resolve_asset("backs", back)
    back_file = back if back_p else "sample-1.png"
    if not back_p:
        back_p = resolve_asset("backs", back_file)
    back_rel, back_path = _put_layer(back_p, out, "back.png")

    # 边框 / 卡封（边框可能来自 frames/ 或 frames-text/；layers 存原始素材路径，便于反查）
    frame_rel = seal_rel = seal_frame_rel = None
    frame_path = seal_path = seal_frame_path = None
    frame_src = seal_src = seal_frame_src = None
    seal_used = seal_sel
    # 卡封叠加层（拼装页「＋ 添加卡封层」）：每层独立素材 + 正/背强度
    seal_extra_sel = data.get("seal_extra") or []
    if not isinstance(seal_extra_sel, list):
        seal_extra_sel = []
    seal_frame_used = seal_frame_sel
    if frame_sel:
        for _d in ("frames", "frames-text"):
            _p = resolve_asset(_d, frame_sel)
            if _p:
                if frame_scale != 1.0:
                    # 缩放过的边框是这张卡独有的，必须落本地
                    _scale_card_layer(_p, frame_scale, out / "frame.png")
                    frame_rel, frame_path = "frame.png", out / "frame.png"
                else:
                    frame_rel, frame_path = _put_layer(_p, out, "frame.png")
                frame_src = str(_p)
                break
    if seal_sel:
        _p = resolve_asset("seals", seal_sel)
        if _p:
            seal_rel, seal_path = _put_layer(_p, out, "seal.png")
            seal_src = str(_p)
    if seal_frame_sel:
        _p = resolve_asset("seals", seal_frame_sel)
        if _p:
            seal_frame_rel, seal_frame_path = _put_layer(_p, out, "seal_frame.png")
            seal_frame_src = str(_p)

    # 卡封叠加层：逐层复制成 seal_<n>.png（素材只存一份，卡里只存引用）
    seal_extra_layers = []
    seal_extra_used = []
    for _i, _it in enumerate(seal_extra_sel):
        _name = (_it or {}).get("seal") or (_it or {}).get("rel") or ""
        _name = str(_name).strip()
        if not _name:
            continue
        _p = resolve_asset("seals", _name)
        if not _p:
            continue
        _fn = "seal_%d.png" % (_i + 1)
        # 叠加层同样：素材库原图只写引用，卡里存的是 ../../seals/xxx.png
        _rel, _ = _put_layer(_p, out, _fn)
        _f = float(_it.get("front", 0.6) or 0)
        _b = float(_it.get("back", 0.0) or 0)
        seal_extra_layers.append(dict(
            rel=_rel or _fn,
            front_strength=_f,
            back_strength=_b,
            base=float(_it.get("base", 0.10)),
            amp=float(_it.get("amp", 1.0)),
            tilt=float(_it.get("tilt", 0.50)),
            tilt_lo=float(_it.get("tilt_lo", 0.004)),
            tilt_hi=float(_it.get("tilt_hi", 0.060)),
            sheen_base=float(_it.get("sheen_base", 0.02)),
            band_power=float(_it.get("band_power", 2.0)),
            z=0.012 + 0.0015 * _i,
            order=4.5 + 0.1 * _i,
            add=True,
            front_only=(_b <= 0.0),
        ))
        seal_extra_used.append((_name, _f, _b))

    # 未选边框/卡封时删除旧副本，避免再次读取时被字节反查误判为仍在使用
    if not frame_rel and (out / "frame.png").exists():
        (out / "frame.png").unlink()
    if not seal_rel and (out / "seal.png").exists():
        (out / "seal.png").unlink()
    if not seal_frame_rel and (out / "seal_frame.png").exists():
        (out / "seal_frame.png").unlink()

    # 边框内区域蒙版：整卡内容仅绘制在边框外围以内（2D 合成内嵌生成；3D shader 用预生成 PNG）
    interior_rel = None
    if frame_rel and frame_path:
        try:
            from PIL import Image
            save_frame_interior_mask(Image.open(frame_path), out / "interior.png")
            interior_rel = "interior.png"
        except Exception:
            interior_rel = None
    elif (out / "interior.png").exists():
        (out / "interior.png").unlink()

    # 卡面素材是否作为整幅卡面（随缩放）：非文本型且卡面当底图时为真（背景/层1卡封不缩放）
    face_scales = bool((face_used or lenti_rel) and not face_as_fg and text_type != "boxed")

    # 特效
    effect_instances = cardforge._resolve_effects(data.get("effects") or [])
    cardforge._materialize_effect_images(effect_instances, out)
    # 主体白色描边（贴纸边）：启用时从前景生成，仅透明主体卡生效
    subject_outline = bool(data.get("subject_outline"))
    seal_front, seal_back = _seal_strengths(data)
    seal_frame_front, seal_frame_back = _seal_frame_strengths(data)
    outline_rel = None
    if subject_outline:
        fg_p = out / "foreground.png"
        if fg_p.exists() and fg_p.stat().st_size > 0:
            from PIL import Image
            from compositor import make_outline
            ol = make_outline(Image.open(fg_p))
            if ol is not None:
                ol.save(out / "outline.png")
                outline_rel = "outline.png"
    elif (out / "outline.png").exists():
        (out / "outline.png").unlink()
    (out / "card.html").write_text(
        webcard.build_card_html(name, front_rel, "foreground.png", back_rel or "back.png",
                                effects=effect_instances,
                                frame_rel=frame_rel, seal_rel=seal_rel,
                                seal_name=seal_used if seal_rel else None,
                                seal_frame_rel=seal_frame_rel,
                                seal_frame_name=seal_frame_used if seal_frame_rel else None,
                                description=desc, text_type=text_type, text_pos=text_pos,
                                subject_over_frame=subject_over_frame,
                                mask_clip=mask_clip,
                                frame_fit_subject=True,
                                round_foreground=True,
                                outline_rel=outline_rel,
                                seal_strength_front=seal_front,
                                seal_strength_back=seal_back,
                                seal_frame_strength_front=seal_frame_front,
                                seal_frame_strength_back=seal_frame_back,
                                detail_rel=detail_rel,
                                seal_extra=seal_extra_layers,
                                content_scale=scale,
                                face_scales=face_scales,
                                interior_rel=interior_rel,
                                background_rel=bg_rel_url,
                                glow_name=glow_name,
                                glow_strength=glow_strength,
                                glow_boost=frame_glow_boost(frame_path) if frame_path else 1.0,
                                subject_offset_x=subject_offset_x,
                                subject_offset_y=subject_offset_y,
                                lenti_images=lenti_rel,
                                lenti_opts=lenti_opts,
                                bg_shader=bg_shader,
                                parallax=para_payload),
        encoding="utf-8",
    )

    # 卡牌定义：路径一律指向"实际文件"（素材库原图或卡目录副本），重开编辑时才对得上
    _front_abs = str(front_path or (out / "front.png"))
    card = build_card_def(
        card_id=card_id, name=name, style="transparent",
        source=_front_abs, front=_front_abs,
        foreground=str(out / "foreground.png"), engine="assembly",
        description=desc or "",
        back=str(resolve_asset("backs", back_file)),
        layers={
            "background": _front_abs,
            # 卡面/主体共用一个素材目录：写进 face 还是 subject 只表示"当底图"还是"当前景"
            "face": str(resolve_asset("faces", face_sel)) if (face_used and not face_as_fg) else None,
            "subject": str(resolve_asset("faces", face_sel)) if (face_used and face_as_fg) else None,
            "frame": frame_src,
            "seal": seal_src,
            "seal_frame": seal_frame_src,
        },
        text={"title": name, "description": desc or "", "type": text_type, "pos": text_pos},
        extra={
            "composed": False, "assembly": True,
            "subject_over_frame": subject_over_frame,
            "subject_outline": subject_outline,
            "mask_clip": mask_clip,
            "card_scale": scale,
            "background_scale": bg_scale,
            "frame_scale": frame_scale,
            "subject_name": face_sel if (face_used and face_as_fg) else None,
            "subject_offset_x": subject_offset_x,
            "subject_offset_y": subject_offset_y,
            "seal_strength_front": seal_front,
            "seal_strength_back": seal_back,
            "seal_frame_strength_front": seal_frame_front,
            "seal_frame_strength_back": seal_frame_back,
            "seal_name": seal_used if seal_rel else None,
            "seal_frame_name": seal_frame_used if seal_frame_rel else None,
            "seal_frame_same": seal_frame_same,
            "frame_name": frame_sel if frame_rel else None,
            "back_name": back_file,
            "face_name": face_used if (face_used and not face_as_fg) else None,
            "background_name": custom_bg if custom_bg else None,
            "glow_name": glow_name,
            "glow_strength": glow_strength,
            "effects": [{"name": e["name"], "pos": e["pos"]} for e in effect_instances],
            # 视差主体：记 id + 层参数，重开成品卡时拼装页能复原「主体层」
            "parallax": ({"id": parallax_sel,
                          "amp": para_payload.get("amp"),
                          "layers": para_payload.get("layers")}
                         if para_payload else None),
            # 光栅卡：存的是原始图源 spec + 参数（重开时按源图重新规范化，不依赖临时文件）
            "lenti": (
                {"atlas": "lenti/atlas.webp",
                 # atlas_src 只给「抽帧缓存」来源：它能支撑改帧数重抽。
                 # 源本身就是这张卡的 lenti/ 时不能写（否则重开会把 "lenti" 当缓存 key）。
                 **({"atlas_src": "lvid:" + lenti_video["dir"].name}
                    if lenti_video["dir"].parent == LENTI_VIDEO_DIR else {}),
                 "frames": lenti_video["frames"], "atlasCols": lenti_video["cols"],
                 "atlasRows": lenti_video["rows"], "video_name": lenti_video["name"],
                 "alpha": bool(lenti_video.get("alpha")),
                 "opts": lenti_opts}
                if lenti_video else
                {"images": lenti_specs, "opts": lenti_opts} if lenti_rel else None),
        },
    )
    # 卡封叠加层清单（重开这张卡时回填到拼装面板）
    card["seal_extra"] = [dict(seal=n, front=f, back=b) for n, f, b in seal_extra_used]
    save_card_def(card, PROJECT_ROOT / "cards" / f"{card_id}.json")

    # 2D 预览图
    try:
        from PIL import Image
        fg = None
        if (out / "foreground.png").stat().st_size > 0:
            fg = Image.open(out / "foreground.png")
        composed = compose_card(fg, style="transparent", front=_front_abs,
                                background=str(bg_rel_path) if bg_rel_path else None,
                                frame=str(frame_path) if frame_rel else None,
                                seal=str(seal_path) if seal_rel else None,
                                seal_frame=str(seal_frame_path) if seal_frame_rel else None,
                                title=name, description=desc, text_type=text_type,
                                text_area=text_pos,
                                subject_over_frame=subject_over_frame,
                                subject_outline=subject_outline, size=(CARD_W, CARD_H),
                                content_scale=scale,
                                face_scales=face_scales,
                                face_cover=bool(lenti_rel),
                                mask_clip=mask_clip,
                                subject_offset_x=subject_offset_x,
                                subject_offset_y=subject_offset_y)
        cardforge.save_preview(composed, out)
    except Exception:
        pass

    build_album_page()  # 刷新离线收集册
    # preview 直接给前端用：成品图统一 card.webp（老卡可能是 card.png，_big_name 兜底）
    return {"ok": True, "id": card_id, "webcard": str(out / "card.html"),
            "preview": f"/preview/{card_id}/{_big_name(out)}"}


def _make_text(data: dict) -> dict:
    """基于已有卡牌重新生成文字层（添加/调整卡牌描述与位置，不重新抠图）。"""
    card_id = _safe_name((data.get("id") or "").strip())
    out = card_dir(card_id)
    # 卡面可能已被外置成素材库引用（front.png 不再落在卡目录），按卡定义找回原图
    front_p = _card_layer(card_id, "face") or _card_layer(card_id, "background")
    if not card_id or front_p is None:
        raise ValueError("找不到该卡牌")
    desc = (data.get("desc") or "").strip() or None
    text_type = data.get("text_type") or "none"
    if desc and text_type == "none":
        text_type = "transparent"  # 有描述但未选类型时按特殊文本型处理
    pos = data.get("text_pos")
    if not isinstance(pos, dict):
        pos = None

    name, title = card_id, None
    effect_instances = []
    subject_over_frame = False
    subject_outline = False
    seal_name = None
    seal_frame_name = None
    card_scale = 1.0
    subject_offset_x = 0.0
    subject_offset_y = 0.0
    glow_name = None
    glow_strength = 1.0
    follow_bg = False
    _matted = False
    cf = PROJECT_ROOT / "cards" / f"{card_id}.json"
    card_style = "transparent"
    try:
        card = json.loads(cf.read_text(encoding="utf-8"))
        name = card.get("name") or card_id
        card_style = card.get("style") or "transparent"
        title = (card.get("text") or {}).get("title")
        # 整幅+抠图+文本型：背景跟随主体变换（与 cardforge.make_card 的 follow_bg 判定一致）
        _engine = card.get("engine") or ""
        _matted = bool(_engine) and _engine != "不使用抠图"
        if not _matted:
            try:
                from PIL import Image as _Img
                _fg = _Img.open(out / "foreground.png").getchannel("A").getextrema()
                _matted = _fg[1] >= 200
            except Exception:
                pass
        follow_bg = card_style == "full-bleed" and _matted and text_type == "boxed"
        _extra = card.get("extra") or {}
        subject_over_frame = bool(card.get("subject_over_frame", _extra.get("subject_over_frame")))
        subject_outline = bool(card.get("subject_outline", _extra.get("subject_outline")))
        mask_clip = bool(card.get("mask_clip", _extra.get("mask_clip", True)))
        # build_card_def 会把 extra 展开到 JSON 顶层：顶层优先、extra 兜底（兼容新旧卡）
        card_scale = _parse_scale(card.get("card_scale", _extra.get("card_scale", 1.0)))
        subject_offset_x = _parse_offset(card.get("subject_offset_x", _extra.get("subject_offset_x", 0.0)))
        subject_offset_y = _parse_offset(card.get("subject_offset_y", _extra.get("subject_offset_y", 0.0)))
        glow_name = card.get("glow_name") or _extra.get("glow_name")
        glow_strength = _parse_glow_strength(
            card.get("glow_strength", _extra.get("glow_strength", 1.0)))
        seal_name = card.get("seal_name") or _extra.get("seal_name") or None
        seal_frame_name = card.get("seal_frame_name") or _extra.get("seal_frame_name") or None
        seal_front, seal_back = _seal_strengths({
            "seal_strength_front": card.get("seal_strength_front", _extra.get("seal_strength_front", 0.945)),
            "seal_strength_back": card.get("seal_strength_back", _extra.get("seal_strength_back", 0.5775)),
        })
        seal_frame_front, seal_frame_back = _seal_frame_strengths({
            "seal_frame_strength_front": card.get("seal_frame_strength_front", _extra.get("seal_frame_strength_front", 0.8)),
            "seal_frame_strength_back": card.get("seal_frame_strength_back", _extra.get("seal_frame_strength_back", 0.5)),
        })
        fx_summary = card.get("effects") or []
        converted = []
        for it in fx_summary:
            if isinstance(it, dict):
                p = it.get("pos") or {}
                converted.append({"name": it.get("name") or "", "posType": p.get("type") or "random",
                                  "posX": p.get("x", 0.5), "posY": p.get("y", 0.5)})
        effect_instances = cardforge._resolve_effects(converted)
    except (OSError, ValueError):
        pass

    # 三个层同样可能已被外置成素材引用：本地副本优先，否则按卡定义找回素材库原图
    frame_p = _card_layer(card_id, "frame")
    seal_p = _card_layer(card_id, "seal")
    seal_frame_p = _card_layer(card_id, "seal_frame")
    frame_rel = (cardforge.lib_ref(frame_p, out) or "frame.png") if frame_p else None
    seal_rel = (cardforge.lib_ref(seal_p, out) or "seal.png") if seal_p else None
    seal_frame_rel = (cardforge.lib_ref(seal_frame_p, out) or "seal_frame.png") if seal_frame_p else None
    # 边框内区域蒙版：缺图时从边框重新生成（旧卡没有 interior.png）
    interior_rel = None
    if frame_rel:
        if not (out / "interior.png").exists():
            try:
                from PIL import Image
                save_frame_interior_mask(Image.open(frame_p), out / "interior.png")
            except Exception:
                pass
        if (out / "interior.png").exists():
            interior_rel = "interior.png"
    # 卡面素材是否作为整幅卡面（随缩放）：非文本型且记录了卡面素材时为真
    face_scales = False
    bg_rel_url = None
    bg_rel_path = None
    try:
        _fcard = json.loads(cf.read_text(encoding="utf-8"))
        _fextra = _fcard.get("extra") or {}
        _fname = _fcard.get("face_name") or _fextra.get("face_name")
        face_scales = bool(_fname and text_type != "boxed")
        # 独立背景层素材：卡面单独缩放时背景整卡铺满（从卡牌记录恢复）
        _bgname = _fcard.get("background_name") or _fextra.get("background_name")
        _bg_p = resolve_asset("backgrounds", _bgname) if _bgname else None
        if _bg_p:
            bg_rel_path = _bg_p
            bg_rel_url = cardforge.lib_ref(_bg_p, out) or f"/assets_abs/backgrounds/{_bgname}"
    except (OSError, ValueError):
        pass
    # 主体白色描边：启用时从已有前景图重新生成
    outline_rel = None
    if subject_outline:
        fg_p = out / "foreground.png"
        if fg_p.exists() and fg_p.stat().st_size > 0:
            from PIL import Image
            from compositor import make_outline
            ol = make_outline(Image.open(fg_p))
            if ol is not None:
                ol.save(out / "outline.png")
                outline_rel = "outline.png"
    elif (out / "outline.png").exists():
        (out / "outline.png").unlink()
    cardforge._materialize_effect_images(effect_instances, out)
    # 整幅+抠图+文本型：重新生成与当前主体同几何的背景跟随图（源图按主体相同的 cover 适配，
    # 任意源图比例下主体像素级遮盖背景原主体；文本框移动后按新主体尺寸重新适配）
    bg_follow_rel = None
    if follow_bg:
        try:
            from PIL import Image as _Img
            _fg_h = _Img.open(out / "foreground.png").height
            if (out / "source.png").exists():
                _cover(_Img.open(out / "source.png"), CARD_W, _fg_h).save(out / "bg_follow.png")
                bg_follow_rel = "bg_follow.png"
        except Exception:
            bg_follow_rel = None
    elif (out / "bg_follow.png").exists():
        (out / "bg_follow.png").unlink()
    _front_rel = cardforge.lib_ref(front_p, out) or "front.png"
    _back_p = _card_layer(card_id, "back")
    _back_rel = (cardforge.lib_ref(_back_p, out) or "back.png") if _back_p else "back.png"
    (out / "card.html").write_text(
        webcard.build_card_html(name, _front_rel, "foreground.png", _back_rel,
                                effects=effect_instances,
                                frame_rel=frame_rel, seal_rel=seal_rel,
                                seal_name=seal_name,
                                seal_frame_rel=seal_frame_rel,
                                seal_frame_name=seal_frame_name if seal_frame_rel else None,
                                description=desc, text_type=text_type, text_pos=pos,
                                subject_over_frame=subject_over_frame,
                                mask_clip=mask_clip,
                                frame_fit_subject=True,
                                outline_rel=outline_rel,
                                seal_strength_front=seal_front,
                                seal_strength_back=seal_back,
                                seal_frame_strength_front=seal_frame_front,
                                seal_frame_strength_back=seal_frame_back,
                                content_scale=card_scale,
                                face_scales=face_scales,
                                interior_rel=interior_rel,
                                background_rel=bg_rel_url,
                                glow_name=glow_name,
                                glow_strength=glow_strength,
                                follow_bg=follow_bg,
                                bg_follow_rel=bg_follow_rel,
                                glow_boost=frame_glow_boost(frame_p) if frame_p else 1.0,
                                subject_offset_x=subject_offset_x,
                                subject_offset_y=subject_offset_y),
        encoding="utf-8",
    )
    try:
        from PIL import Image
        fg = None
        if (out / "foreground.png").stat().st_size > 0:
            fg = Image.open(out / "foreground.png")
        composed = compose_card(fg, style=card_style, front=str(front_p),
                                background=str(bg_rel_path) if bg_rel_path else None,
                                frame=str(frame_p) if frame_rel else None,
                                seal=str(seal_p) if seal_rel else None,
                                seal_frame=str(seal_frame_p) if seal_frame_rel else None,
                                title=title, description=desc, text_type=text_type,
                                text_area=pos,
                                subject_over_frame=subject_over_frame,
                                subject_outline=subject_outline, size=(CARD_W, CARD_H),
                                content_scale=card_scale,
                                face_scales=face_scales,
                                mask_clip=mask_clip,
                                subject_full_bleed=_matted if card_style == "full-bleed" else False,
                                follow_bg=follow_bg,
                                follow_bg_src=str(out / "bg_follow.png") if bg_follow_rel else None,
                                subject_offset_x=subject_offset_x,
                                subject_offset_y=subject_offset_y)
        cardforge.save_preview(composed, out)
    except Exception:
        pass
    if cf.exists():
        try:
            card = json.loads(cf.read_text(encoding="utf-8"))
            card["description"] = desc or ""
            card["text"] = {**(card.get("text") or {}), "title": title, "description": desc or "",
                            "type": text_type, "pos": pos}
            save_card_def(card, cf)
        except (OSError, ValueError):
            pass
    build_album_page()  # 刷新离线收集册
    # preview 直接给前端用：成品图统一 card.webp（老卡可能是 card.png，_big_name 兜底）
    return {"ok": True, "id": card_id, "webcard": str(out / "card.html"),
            "preview": f"/preview/{card_id}/{_big_name(out)}"}


def Image_blank(path: Path) -> None:
    from PIL import Image
    Image.new("RGBA", (CARD_W, CARD_H), (0, 0, 0, 0)).save(path)


def Image_dark(path: Path) -> None:
    """生成深色不透明底图（无背景素材时作为卡面底）。"""
    from PIL import Image
    Image.new("RGBA", (CARD_W, CARD_H), (14, 11, 9, 255)).save(path)


def _ensure_blank() -> str:
    """确保存在透明占位 PNG（无前景层时 3D 预览使用），返回访问 URL。"""
    p = PREVIEW_DIR / "blank.png"
    if not p.exists():
        try:
            from PIL import Image
            Image.new("RGBA", (CARD_W, CARD_H), (0, 0, 0, 0)).save(p)
        except Exception:
            pass
    return "/preview/_preview/blank.png"


def _ensure_dark() -> str:
    """确保存在深色不透明占位 PNG（无卡面/背景时 3D 预览使用），返回访问 URL。"""
    p = PREVIEW_DIR / "dark.png"
    if not p.exists():
        try:
            from PIL import Image
            Image.new("RGBA", (CARD_W, CARD_H), (14, 11, 9, 255)).save(p)
        except Exception:
            pass
    return "/preview/_preview/dark.png"


class Handler(BaseHTTPRequestHandler):
    server_version = "cardforge/1.0"

    def log_message(self, fmt, *args):
        sys.stderr.write("[web] %s\n" % (fmt % args))

    def _send_json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        try:
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError):
            # 前端主动取消（关页面 / 重新选文件）时连接已经断了，再写就是噪声 traceback
            pass

    def _send_bytes(self, data: bytes, ctype: str):
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _send_file(self, path: Path):
        if path.exists() and path.is_file():
            ctype = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
            self._send_bytes(path.read_bytes(), ctype)
            return True
        return False

    def do_GET(self):
        path = self.path.split("?")[0]

        if path in ("/", "/index.html"):
            body = build_page().encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
            return

        if path == "/album.html":
            body = build_album_page().encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
            return

        if path == "/codex.html":
            body = build_codex_page().encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
            return

        if path == "/cutout.html":
            body = build_cutout_page().encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
            return

        if path == "/favicon.ico":
            self.send_response(204)
            self.end_headers()
            return

        if path in ("/lenti.html", "/lenti"):
            body = build_lenti_page().encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
            return

        if path in ("/parallax.html", "/parallax"):
            body = parallax.PARALLAX_HTML.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
            return

        if path.startswith("/parallax/"):
            # /parallax/<主体>/<文件>、/parallax/_uploads/<文件>
            parts = [unquote(p) for p in path.strip("/").split("/")[1:]]
            if parts and all(p and p != ".." and "/" not in p and "\\" not in p for p in parts):
                f = parallax.PARALLAX_ROOT
                for p in parts:
                    f = f / p
                if self._send_file(f):
                    return
            self._send_json({"ok": False, "error": "not found"}, 404)
            return

        if path.startswith("/lenti/"):
            # /lenti/<目录>/<文件> —— 光栅卡产物（帧图 + lenti.html）
            parts = path.strip("/").split("/")
            if len(parts) >= 3 and all(p and p != ".." for p in parts[1:]):
                rel = lenticular.LENTI_ROOT
                for p in parts[1:]:
                    rel = rel / unquote(p)
                if self._send_file(rel):
                    return
            self._send_json({"ok": False, "error": "not found"}, 404)
            return

        if path in ("/fx2", "/fx2/"):
            path = "/fx2/index.html"

        if path.startswith("/fx2/"):
            rel = path[len("/fx2/"):]
            if ".." not in rel and "\\" not in rel:
                target = PROJECT_ROOT / "fx2"
                for seg in rel.split("/"):
                    if seg:
                        target = target / unquote(seg)
                if self._send_file(target):
                    return
            self._send_json({"ok": False, "error": "not found"}, 404)
            return

        if path == "/api/export":
            qs = {k: unquote(v[0]) for k, v in parse_qs(self.path.split("?", 1)[1]).items()}
            cid = qs.get("id", "")
            etype = qs.get("type", "image")
            if etype not in ("image", "html"):
                etype = "image"
            if not cid:
                self._send_json({"ok": False, "error": "缺少卡片 id"}, 400)
                return
            r = _export_card(cid, etype)
            if not r:
                self._send_json({"ok": False, "error": "卡片不存在或缺少导出文件"}, 404)
                return
            body, mime, fname = r
            self.send_response(200)
            self.send_header("Content-Type", mime)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Content-Disposition", "attachment; filename*=UTF-8''" + quote(fname))
            self.end_headers()
            self.wfile.write(body)
            return

        if path.startswith("/shared/"):
            # /shared/<role>/<file> —— 卡牌公共素材（back / fx / seal）。
            # file:// 打开时 card.html 用 ../../shared/ 直接命中 assets/shared，
            # http 下 ../../shared 会规范化成 /shared/...，这里映射回同一目录。
            parts = path.strip("/").split("/")
            if len(parts) >= 3 and all(p and p != ".." for p in parts[1:]):
                rel = ASSETS / "shared"
                for p in parts[1:]:
                    rel = rel / unquote(p)
                if self._send_file(rel):
                    return
            self._send_json({"ok": False, "error": "not found"}, 404)
            return

        if path.startswith("/preview/"):
            # /preview/<dir>/.../<file>（可嵌套，如 output/_preview/<token>/preview.html）
            # 成品卡现在分裂在两个根下：异兽牌流水线的产物在 assets/ripit，
            # cardforge 手动捏的试验卡在 assets/output —— 依次试，命中即返回。
            parts = path.strip("/").split("/")
            if len(parts) >= 3 and parts[0] == "preview" and all(p and p != ".." for p in parts[1:]):
                for _root in (ASSETS / "ripit", ASSETS / "output"):
                    rel = _root
                    for p in parts[1:]:
                        rel = rel / unquote(p)
                    if self._send_file(rel):
                        return
            self._send_json({"ok": False, "error": "not found"}, 404)
            return

        if path.startswith(("/assets/", "/packs/", "/fx2/")):
            # 卡目录里对素材库的相对引用（../../assets/backs/x.png）在 http 下会被规范化成
            # 站点根的 /assets/… —— 直接映射回项目目录，file:// 与 http 两种打开方式同源。
            parts = path.strip("/").split("/")
            if len(parts) >= 2 and all(p and p != ".." for p in parts):
                rel = PROJECT_ROOT
                ok = True
                for p in parts:
                    q = unquote(p)
                    # 解码后再拦一次（%2e%2e / %2f 之类不能被当成路径分隔符）
                    if q in ("", ".", "..") or "/" in q or "\\" in q:
                        ok = False
                        break
                    rel = rel / q
                if ok and self._send_file(rel):
                    return
            self._send_json({"ok": False, "error": "not found"}, 404)
            return

        if path.startswith("/assets_abs/"):
            # /assets_abs/<type>/<素材名或 包id/相对路径> —— 直接引用素材库文件（含素材包）
            parts = path.strip("/").split("/")
            if len(parts) >= 3 and parts[1] in _ASSET_TYPES:
                key = "/".join(unquote(p) for p in parts[2:])
                rel = resolve_asset(parts[1], key)
                if rel and self._send_file(rel):
                    return
            self._send_json({"ok": False, "error": "not found"}, 404)
            return

        if path.startswith("/packs_abs/"):
            # /packs_abs/<包id>/<相对路径> —— 素材包缩略图等
            parts = path.strip("/").split("/")
            if len(parts) >= 3:
                rel = "/".join(unquote(p) for p in parts[2:])
                f = packs.pack_file(unquote(parts[1]), rel)
                if f and self._send_file(f):
                    return
            self._send_json({"ok": False, "error": "not found"}, 404)
            return

        if path == "/api/effects":
            self._send_json(list_effects())
            return
        if path == "/api/cards":
            # scope=album：只返回收集册可见的卡（assets/output）。
            # ★ 拼装页成品下拉也吃这个 scope：异兽牌（assets/ripit，1800 版）按约定
            #   只进图鉴，不进成品选择。需要全量（含异兽牌）的脚本自己不带 scope。
            _q = self.path.split("?", 1)[1] if "?" in self.path else ""
            _album_only = any(kv == "scope=album" for kv in _q.split("&"))
            self._send_json({"ok": True,
                             "cards": album_cards() if _album_only else list_cards()})
            return
        if path == "/api/parallax/list":
            self._send_json({"ok": True, "items": parallax.list_items()})
            return
        if path == "/api/parallax/item":
            qs = parse_qs(self.path.partition("?")[2] or "")
            pid = (qs.get("id") or [""])[0]
            m = parallax.load_item(pid) if pid else None
            if not m:
                self._send_json({"ok": False, "error": "没有这个视差主体"}, 404)
                return
            self._send_json({"ok": True, "meta": m})
            return
        if path == "/api/albums":
            self._send_json({"ok": True, **albums_summary()})
            return
        if path == "/api/settings":
            self._send_json({"ok": True, "settings": _get_settings()})
            return
        if path == "/api/assets":
            t = self.path.split("?", 1)[-1]
            qs = {}
            for kv in t.split("&"):
                if "=" in kv:
                    k, v = kv.split("=", 1)
                    qs[k] = v
            atype = qs.get("type", "")
            if atype not in _ASSET_TYPES:
                self._send_json({"ok": False, "error": "未知素材类型"}, 400)
                return
            files = list_effect_assets(atype) if atype in _EFFECT_ASSET_TYPES else packs.list_assets(atype)
            self._send_json({"ok": True, "type": atype, "files": files})
            return
        if path == "/api/packs":
            self._send_json({"ok": True, "packs": _packs_for_ui()})
            return

        self._send_json({"ok": False, "error": "not found"}, 404)

    def do_POST(self):
        _path, _, _qs = self.path.partition("?")
        # 视频/GIF 走裸字节上传（base64 会把体积放大 1/3，几十 MB 的片子没必要）
        # 光栅卡工作页的视频图源：抽 2~6 张独立帧喂多槽位预览（裸字节，同 /api/lenti/video）
        # 视差主体编辑器：图层素材也走裸字节（PNG/WebP 转 RGBA 后落 _uploads/）
        if _path == "/api/parallax/upload":
            try:
                length = int(self.headers.get("Content-Length", 0))
                if length > parallax.MAX_UPLOAD_BYTES:
                    raise ValueError("图片太大（>30MB），先压一下")
                raw = self.rfile.read(length) if length else b""
                name = unquote(self.headers.get("X-File-Name", "") or "layer.png")
                self._send_json(parallax.save_upload(raw, name))
            except ValueError as e:
                self._send_json({"ok": False, "error": str(e)}, 400)
            except Exception as e:
                self._send_json({"ok": False, "error": str(e)}, 500)
            return
        if _path in ("/api/lenti/video/split", "/api/lenti/video_frames"):
            try:
                length = int(self.headers.get("Content-Length", 0))
                if length > LENTI_VIDEO_MAX_BYTES:
                    raise ValueError("文件太大（>60MB），先压一下或截短")
                raw = self.rfile.read(length) if length else b""
                name = unquote(self.headers.get("X-File-Name", "") or "clip.mp4")
                want = parse_qs(_qs).get("frames", [None])[0]
                self._send_json(_lenti_video_split(raw, name, want))
            except ValueError as e:
                self._send_json({"ok": False, "error": str(e)}, 400)
            except Exception as e:
                self._send_json({"ok": False, "error": str(e)}, 500)
            return
        if _path in ("/api/lenti/video", "/api/lenti/video_raw"):
            try:
                length = int(self.headers.get("Content-Length", 0))
                if length > LENTI_VIDEO_MAX_BYTES:
                    raise ValueError("文件太大（>60MB），先压一下或截短")
                raw = self.rfile.read(length) if length else b""
                name = unquote(self.headers.get("X-File-Name", "") or "clip.mp4")
                # ?frames=24：抽帧数（8~36），不传按默认 24
                want = parse_qs(_qs).get("frames", [None])[0]
                self._send_json(_make_lenti_video(raw, name, want))
            except ValueError as e:
                self._send_json({"ok": False, "error": str(e)}, 400)
            except Exception as e:
                self._send_json({"ok": False, "error": str(e)}, 500)
            return
        try:
            length = int(self.headers.get("Content-Length", 0))
            data = json.loads(self.rfile.read(length).decode("utf-8")) if length else {}
        except Exception as e:
            self._send_json({"ok": False, "error": f"请求解析失败: {e}"}, 400)
            return

        # 改抽帧数：源文件还在 _video/_src，不用重新上传几十 MB
        if _path in ("/api/lenti/video/reframe", "/api/lenti/video/frames"):
            try:
                self._send_json(_reframe_lenti_video(
                    data.get("hash") or data.get("src") or "", data.get("frames")))
            except ValueError as e:
                self._send_json({"ok": False, "error": str(e)}, 400)
            except Exception as e:
                self._send_json({"ok": False, "error": str(e)}, 500)
            return

        try:
            if self.path == "/api/make":
                self._send_json(self._make(data))
            elif self.path == "/api/effect/save":
                self._send_json(self._effect_save(data))
            elif self.path == "/api/parallax/save":
                self._send_json(parallax.save_item(data, (data.get("id") or "").strip() or None))
            elif self.path == "/api/parallax/delete":
                self._send_json(parallax.delete_item(data.get("id") or ""))
            elif self.path == "/api/effect/delete":
                self._send_json(self._effect_delete(data))
            elif self.path == "/api/preview_html":
                self._send_json(self._preview_html(data))
            elif self.path == "/api/tmp_upload":
                self._send_json(self._tmp_upload(data))
            elif self.path == "/api/assets/upload":
                self._send_json(self._asset_upload(data))
            elif self.path == "/api/assets/delete":
                self._send_json(self._asset_delete(data))
            elif self.path == "/api/assets/rename":
                self._send_json(self._asset_rename(data))
            elif self.path == "/api/packs/import":
                self._send_json(self._pack_import(data))
            elif self.path == "/api/packs/import_folder":
                self._send_json(self._pack_import_folder(data))
            elif self.path == "/api/packs/toggle":
                self._send_json(self._pack_toggle(data))
            elif self.path == "/api/packs/delete":
                self._send_json(self._pack_delete(data))
            elif self.path == "/api/assembly/save":
                self._send_json(_save_assembly(data))
            elif self.path == "/api/make_text":
                self._send_json(_make_text(data))
            elif self.path == "/api/settings":
                _save_settings({k: v for k, v in data.items()
                                if k in ("seal_strength_front", "seal_strength_back",
                                         "seal_frame_strength_front", "seal_frame_strength_back")})
                self._send_json({"ok": True, "settings": _get_settings()})
            elif self.path == "/api/card/delete":
                self._send_json(self._card_delete(data))
            elif self.path == "/api/albums/create":
                self._send_json(album_create(data.get("name")))
            elif self.path == "/api/albums/rename":
                self._send_json(album_rename(data.get("id"), data.get("name")))
            elif self.path == "/api/albums/delete":
                self._send_json(album_delete(data.get("id")))
            elif self.path == "/api/albums/move":
                self._send_json(album_move(data.get("ids"), data.get("album")))
            elif self.path == "/api/cutout":
                self._send_json(self._cutout(data))
            elif self.path == "/api/subject/cutout":
                self._send_json(self._subject_cutout(data))
            elif self.path == "/api/lenti/probe":
                self._send_json(self._lenti_probe(data))
            elif self.path == "/api/lenti/build":
                self._send_json(self._lenti_build(data))
            elif self.path == "/api/lenti/save":
                self._send_json(self._lenti_save(data))
            else:
                self._send_json({"ok": False, "error": "not found"}, 404)
        except ValueError as e:
            self._send_json({"ok": False, "error": str(e)}, 400)
        except Exception as e:
            self._send_json({"ok": False, "error": str(e)}, 500)

    # ---------- 业务 ----------

    # ---- 光栅卡 ----

    def _lenti_path(self, spec: str) -> Path:
        return lenti_path_of(spec)

    def _lenti_src(self, data: dict) -> list[Path]:
        return lenti_src_list(data)

    def _lenti_probe(self, data: dict) -> dict:
        p = self._lenti_path("path:" + (data.get("path") or ""))
        return {"ok": True, "path": str(p), "name": p.name}

    def _lenti_aspect(self, data: dict) -> float:
        a = data.get("aspect") or lenticular.DEFAULT_ASPECT
        if isinstance(a, str) and a in lenticular.ASPECTS:
            return lenticular.ASPECTS[a]
        try:
            v = float(a)
        except Exception:
            return lenticular.DEFAULT_ASPECT
        return v if 0.4 <= v <= 2.5 else lenticular.DEFAULT_ASPECT

    def _lenti_build(self, data: dict) -> dict:
        specs = data.get("specs") or []
        if not specs:
            raise ValueError("没有图片")
        if len(specs) > 6:
            raise ValueError("最多 6 张（立体模式会按张数分配帧数）")
        paths = [self._lenti_path(sp) for sp in specs]
        mode = data.get("mode") if data.get("mode") in ("flip", "3d") else "flip"
        frames = int(data.get("frames") or 5)
        aspect = self._lenti_aspect(data)
        token = uuid.uuid4().hex[:10]
        _lenti_gc()
        depth = float(data.get("depth") or 1.35)
        root = lenticular.build(token, paths, mode=mode, frames=frames,
                                aspect=aspect, depth=depth,
                                out_root=lenticular.LENTI_ROOT / "_work",
                                title="光栅卡预览")
        names = [p.name for p in sorted(root.glob("f*.*")) if p.suffix.lower() in (".webp", ".png", ".jpg")]
        return {"ok": True, "token": token, "base": "/lenti/_work/" + token + "/",
                "images": names, "count": len(names), "mode": mode}

    def _lenti_save(self, data: dict) -> dict:
        token = _safe_name((data.get("token") or "").strip())
        if not token or ".." in token:
            raise ValueError("无效的预览 token")
        src = lenticular.LENTI_ROOT / "_work" / token
        if not src.is_dir():
            raise ValueError("预览已失效，请重新生成")
        name = (data.get("name") or "").strip() or token
        slug = lenticular._slug(name)
        dst = (lenticular.LENTI_ROOT / slug).resolve()
        if lenticular.LENTI_ROOT.resolve() not in dst.parents:
            raise ValueError("非法目录")
        if dst.exists():
            shutil.rmtree(dst)
        shutil.copytree(src, dst)
        meta = {}
        try:
            meta = json.loads((dst / "meta.json").read_text(encoding="utf-8"))
        except Exception:
            pass
        html = lenticular.build_lenti_html(
            meta.get("images") or [], opts=meta.get("opts") or None,
            aspect=float(meta.get("aspect") or lenticular.DEFAULT_ASPECT),
            title=name, three_rel=lenticular.THREE_SRC_REL)
        (dst / "lenti.html").write_text(html, encoding="utf-8")
        return {"ok": True, "url": "/lenti/" + slug + "/lenti.html", "dir": slug}


    def _decode_image(self, b64: str, filename: str) -> Path:
        raw = base64.b64decode(b64)
        name = _safe_name(filename.replace("\\", "/").split("/")[-1])
        if not name.lower().endswith(_IMG_EXTS):
            name = "upload.png"
        UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
        tmp = UPLOAD_DIR / f"up_{int(time.time() * 1000)}_{name}"
        tmp.write_bytes(raw)
        return tmp

    def _make(self, data: dict) -> dict:
        file_b64 = data.get("file_b64", "")
        if not file_b64:
            raise ValueError("未选择图像")
        tmp = self._decode_image(file_b64, data.get("filename") or "upload.png")
        try:
            model = data.get("model") or DEFAULT_MODEL
            engine = get_engine(model) if model != "none" else None

            def progress(step, total, msg):
                bar_length = 40
                percent = step / total
                filled = int(bar_length * percent)
                bar = "=" * filled + "-" * (bar_length - filled)
                sys.stderr.write(
                    f"\r[web] [{step:>2}/{total}] [{bar}] {percent * 100:5.1f}% - {msg}"
                )
                if step >= total:
                    sys.stderr.write("\n")

            result = cardforge.make_card(
                tmp,
                name=data.get("name") or None,
                slug=data.get("slug") or None,
                style=data.get("style") or "transparent",
                model=model,
                engine=engine,
                title=data.get("title"),
                desc=data.get("desc"),
                text_type=data.get("text_type") or "none",
                text_pos=data.get("text_pos"),
                background=data.get("background"),
                frame=data.get("frame"),
                seal=data.get("seal"),
                seal_frame=data.get("seal_frame"),
                animation=data.get("effect") or "none",
                effects=data.get("effects"),
                compose=True,
                subject_over_frame=bool(data.get("subject_over_frame")),
                subject_outline=bool(data.get("subject_outline")),
                mask_clip=bool(data.get("mask_clip", True)),
                adaptive=bool(data.get("adaptive", True)),
                adaptive_mode=int(data.get("adaptive_mode", 1)),
                scale=_parse_scale(data.get("scale")),
                glow_name=(data.get("glow") or "none"),
                glow_strength=_parse_glow_strength(data.get("glow_strength")),
                progress=progress,
            )
            build_album_page()  # 刷新离线收集册
            return result
        finally:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass

    def _cutout(self, data: dict) -> dict:
        """独立抠图：上传图片 → 指定模型去背景 → 返回透明 PNG（base64）。"""
        file_b64 = data.get("file_b64", "")
        if not file_b64:
            raise ValueError("未选择图像")
        tmp = self._decode_image(file_b64, data.get("filename") or "upload.png")
        try:
            model = data.get("model") or DEFAULT_MODEL
            if model not in SUPPORTED_MODELS or model in ("none", "sam"):
                raise ValueError(f"不支持的抠图模型: {model}")
            engine = get_engine(model)
            from PIL import Image

            t0 = time.time()
            result = engine.remove_background(Image.open(tmp).convert("RGBA"))
            seconds = round(time.time() - t0, 2)
            buf = io.BytesIO()
            result.save(buf, format="PNG")
            return {
                "ok": True,
                "png_b64": base64.b64encode(buf.getvalue()).decode("ascii"),
                "width": result.width,
                "height": result.height,
                "seconds": seconds,
                "model": model,
            }
        finally:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass

    def _subject_cutout(self, data: dict) -> dict:
        """从当前背景素材/成品卡底图抠出透明主体，cover 适配到卡牌尺寸后存入 assets/faces/。

        卡面与主体是同一类素材（同一张图，透明与否决定当底图还是当前景层），
        所以抠出来的透明图直接落在卡面素材库里，供同一个下拉选用。
        """
        bg = (data.get("background") or "").strip()
        card_id = _safe_name((data.get("card") or "").strip())
        src = resolve_asset("backgrounds", bg) if bg else None
        if src is None and card_id:
            p = card_dir(card_id) / "front.png"
            if p.exists():
                src = p
        if src is None:
            raise ValueError("请先选择背景素材或成品卡")
        model = data.get("model") or DEFAULT_MODEL
        if model not in SUPPORTED_MODELS or model in ("none", "sam"):
            raise ValueError(f"不支持的抠图模型: {model}")
        from PIL import Image, ImageOps
        engine = get_engine(model)
        t0 = time.time()
        res = engine.remove_background(Image.open(src).convert("RGBA"))
        res = ImageOps.fit(res, (CARD_W, CARD_H), Image.LANCZOS)
        stem = _safe_name(Path(bg).stem if bg else f"{card_id}-front") + "-主体"
        dst = ASSETS / "faces" / f"{stem}.png"
        n = 2
        while dst.exists():
            dst = ASSETS / "faces" / f"{stem}-{n}.png"
            n += 1
        dst.parent.mkdir(parents=True, exist_ok=True)
        res.save(dst)
        return {"ok": True, "name": dst.name, "seconds": round(time.time() - t0, 2)}

    def _card_delete(self, data: dict) -> dict:
        card_id = _safe_name((data.get("id") or "").strip())
        if not card_id:
            raise ValueError("缺少卡片 ID")
        out = ASSETS / "output" / card_id
        cf = PROJECT_ROOT / "cards" / f"{card_id}.json"
        removed = False
        if out.exists() and out.is_dir():
            shutil.rmtree(out, ignore_errors=True)
            removed = True
        if cf.exists():
            try:
                cf.unlink()
                removed = True
            except OSError:
                pass
        if not removed:
            raise ValueError(f"找不到卡牌「{card_id}」")
        _albums_forget(card_id)      # 同步清掉收集册归属
        build_album_page()  # 刷新离线收集册
        return {"ok": True, "id": card_id}

    def _effect_save(self, data: dict) -> dict:
        name = (data.get("name") or "").strip()
        if not name:
            raise ValueError("请填写特效名称")
        if _safe_name(name) != name or "/" in name or "\\" in name:
            raise ValueError("特效名称包含非法字符")
        if name in cardforge.BUILTIN_EFFECTS:
            raise ValueError(f"「{name}」与内置特效重名，请换一个名称")
        defn = data.get("def") or {}
        if not isinstance(defn, dict):
            raise ValueError("特效参数无效")
        EFFECTS_DIR.mkdir(parents=True, exist_ok=True)
        (EFFECTS_DIR / f"{name}.json").write_text(
            json.dumps({"name": name, "def": defn}, ensure_ascii=False, indent=2), encoding="utf-8")
        return {"ok": True, "name": name}

    def _effect_delete(self, data: dict) -> dict:
        name = (data.get("name") or "").strip()
        f = EFFECTS_DIR / f"{name}.json"
        if not f.exists():
            raise ValueError(f"特效「{name}」不存在")
        f.unlink()
        return {"ok": True, "name": name}

    def _preview_html(self, data: dict) -> dict:
        kind = data.get("kind") or "effect"
        effects = _abs_effect_images(data.get("effects") or [])
        if kind == "effect":
            html = webcard.build_effect_preview(effects)
        else:  # assembly
            mode = (data.get("mode") or "3d").strip()
            name = (data.get("name") or "").strip() or "拼装预览"
            card_sel = (data.get("card") or "").strip()
            face_sel = (data.get("face") or "").strip()
            custom_bg = (data.get("custom_bg") or "").strip()
            frame_sel = (data.get("frame") or "").strip()
            seal_sel = (data.get("seal") or "").strip()
            seal_frame_sel = (data.get("seal_frame") or "").strip()
            # 边框卡封"与卡封一致"：与卡牌卡封使用同一素材
            if seal_frame_sel == "same":
                seal_frame_sel = seal_sel
            fg = (data.get("foreground") or "").strip()
            parallax_sel = (data.get("parallax") or "").strip()   # 视差主体 id（可空）
            back_sel = (data.get("back") or "").strip()
            back = back_sel if resolve_asset("backs", back_sel) else "sample-1.png"
            blank_url = _ensure_blank()
            desc = (data.get("desc") or "").strip() or None
            text_type = data.get("text_type") or "none"
            if desc and text_type == "none":
                text_type = "transparent"
            subject_over_frame = bool(data.get("subject_over_frame"))
            subject_outline = bool(data.get("subject_outline"))
            mask_clip = bool(data.get("mask_clip", True))
            scale = _parse_scale(data.get("scale"))
            bg_scale = _parse_scale(data.get("bg_scale"))
            frame_scale = _parse_scale(data.get("frame_scale"))
            face_as_fg = bool(data.get("face_as_fg"))
            _legacy_subject = (data.get("subject") or "").strip()
            if _legacy_subject and not face_sel:
                face_sel, face_as_fg = _legacy_subject, True
            subject_offset_x = _parse_offset(data.get("subject_offset_x"))
            subject_offset_y = _parse_offset(data.get("subject_offset_y"))
            # 底图优先级：卡面素材 > 背景素材 > 成品卡（与保存逻辑一致）；
            # 卡面与主体是同一类素材——face_as_fg 时这张图只作前景层，底图改用背景；
            # 浮于边框之上时前景层层序提到边框之上；文本型（boxed）时底图改用背景/深色
            detail_url = None  # 分层成品卡的主体层（无则 None）
            face_floats = face_as_fg or subject_over_frame or text_type == "boxed"
            face_p = resolve_asset("faces", face_sel)
            bg_p = resolve_asset("backgrounds", custom_bg)
            # 程序化背景（虚空式）：素材旁同名 JSON 声明 shader，3D 卡面改由着色器生成
            bg_shader = _bg_shader_of(bg_p)
            # 背景渲染视图：背景倍率非 1 时按卡牌中心缩放并烘焙成整卡 PNG（深色底避免透明空洞）
            bg_view_url = f"/assets_abs/backgrounds/{custom_bg}" if bg_p else None
            bg_view_p = bg_p
            if bg_p and bg_scale != 1.0:
                PREVIEW_DIR.mkdir(parents=True, exist_ok=True)
                bname = f"bgs_{uuid.uuid4().hex[:8]}.png"
                bg_view_p = _scale_card_layer(bg_p, bg_scale, PREVIEW_DIR / bname,
                                              base_fill=(14, 11, 9), cover=True)
                bg_view_url = f"/preview/_preview/{bname}"
            if face_p:
                if face_as_fg or text_type == "boxed":
                    front_url = bg_view_url if bg_p else _ensure_dark()
                else:
                    front_url = f"/assets_abs/faces/{face_sel}"
                fg = f"/assets_abs/faces/{face_sel}" if face_floats else blank_url
            elif bg_p:
                front_url = bg_view_url
                fg = fg or (f"/preview/{card_sel}/foreground.png" if card_sel else blank_url)
            elif card_sel:
                front_url = _card_front_url(card_sel) or _ensure_dark()
                fg = fg or f"/preview/{card_sel}/foreground.png"
                # 分层卡：底图只有背景，主体层单独一张（叠在卡封之上）
                if (ASSETS / "output" / card_sel / "detail.png").exists():
                    detail_url = f"/preview/{card_sel}/detail.png"
            else:
                front_url = _ensure_dark()
                fg = fg or blank_url
            # 独立背景层素材：卡面素材随缩放时背景整卡铺满（3D 独立背景层；2D 背景层共用）
            bg_url = None
            bg_path = None
            if bg_p:
                bg_path = bg_view_p
                bg_url = bg_view_url
            frame_url = None
            frame_path = None
            if frame_sel:
                for _d in ("frames", "frames-text"):
                    _fp = resolve_asset(_d, frame_sel)
                    if _fp:
                        frame_url = f"/assets_abs/{_d}/{frame_sel}"
                        frame_path = _fp
                        break
            # 边框渲染视图：边框倍率非 1 时按卡牌中心缩放并烘焙成整卡 PNG（四周透明）
            if frame_path is not None and frame_scale != 1.0:
                PREVIEW_DIR.mkdir(parents=True, exist_ok=True)
                fname = f"frs_{uuid.uuid4().hex[:8]}.png"
                frame_path = _scale_card_layer(frame_path, frame_scale, PREVIEW_DIR / fname)
                frame_url = f"/preview/_preview/{fname}"
            # 边框内区域蒙版：有边框时整卡内容仅绘制在边框外围以内（3D shader 裁剪）
            interior_url = None
            if frame_path is not None:
                from PIL import Image
                PREVIEW_DIR.mkdir(parents=True, exist_ok=True)
                mname = f"interior_{uuid.uuid4().hex[:8]}.png"
                try:
                    save_frame_interior_mask(Image.open(frame_path), PREVIEW_DIR / mname)
                    interior_url = f"/preview/_preview/{mname}"
                except Exception:
                    interior_url = None
            # 光栅卡卡面：N 张图在 shader 里按柱状透镜条带合成，首帧同时当静态卡面/2D 底图。
            # 取代上面选的卡面素材（边框/卡封/特效/文字照常叠加在它之上）。
            lenti_urls = []
            lenti_opts = _lenti_opts_of(data)
            video = _lenti_atlas_of(data)
            if video:
                # 视频/GIF：帧在图集里（1 个采样器），卡面静态图用首帧
                face_p = video["frame0"]
                face_as_fg = False
                face_sel = ""
                front_url = video["front_url"]
                fg = blank_url
                face_floats = subject_over_frame or text_type == "boxed"
                lenti_opts.update({"atlas": video["atlas_url"], "frames": video["frames"],
                                   "atlasCols": video["cols"], "atlasRows": video["rows"]})
                lenti_paths = []
            else:
                lenti_paths = self._lenti_src(data)
            if lenti_paths:
                _ld, _lnames = _lenti_frames(lenti_paths)
                lenti_urls = ["/preview/_preview/_lenti/" + _ld.name + "/" + n for n in _lnames]
                face_p = _ld / _lnames[0]
                face_as_fg = False
                face_sel = ""
                front_url = lenti_urls[0]
                fg = blank_url
                face_floats = subject_over_frame or text_type == "boxed"
            # 卡面素材是否作为整幅卡面（随缩放）：非文本型且卡面当底图时为真
            face_scales = bool(face_p and not face_as_fg and text_type != "boxed")
            seal_p = resolve_asset("seals", seal_sel) if seal_sel else None
            seal_url = f"/assets_abs/seals/{seal_sel}" if seal_p else None
            seal_path = seal_p
            seal_frame_p = resolve_asset("seals", seal_frame_sel) if seal_frame_sel else None
            seal_frame_url = f"/assets_abs/seals/{seal_frame_sel}" if seal_frame_p else None
            seal_frame_path = seal_frame_p
            back_url = f"/assets_abs/backs/{back}"
            seal_name = seal_sel if seal_url else None
            seal_frame_name = seal_frame_sel if seal_frame_url else None
            # 拼装页的特效项只有名称与位置，需先解析为完整定义（含组合展开）
            effects = _abs_effect_images(cardforge._resolve_effects(data.get("effects") or []))
            # 主体描边预览：从可定位的前景图实时生成白边图
            outline_url = None
            if subject_outline and fg:
                fg_src = None
                if fg.startswith("/preview/_tmp/"):
                    p = TMP_DIR / Path(fg).name
                    if p.exists():
                        fg_src = p
                elif fg.startswith("/assets_abs/faces/"):
                    p = resolve_asset("faces", unquote(fg[len("/assets_abs/faces/"):]))
                    if p and p.exists():
                        fg_src = p
                elif fg.startswith("/preview/") and not fg.startswith("/preview/_preview/"):
                    p = ASSETS / "output" / fg.split("/")[2] / "foreground.png"
                    if p.exists():
                        fg_src = p
                if fg_src is not None:
                    from PIL import Image
                    from compositor import make_outline
                    ol = make_outline(Image.open(fg_src))
                    if ol is not None:
                        PREVIEW_DIR.mkdir(parents=True, exist_ok=True)
                        name = f"ol_{uuid.uuid4().hex[:8]}.png"
                        ol.save(PREVIEW_DIR / name)
                        outline_url = f"/preview/_preview/{name}"
            seal_front, seal_back = _seal_strengths(data)
            seal_frame_front, seal_frame_back = _seal_frame_strengths(data)
            if mode == "2d":
                # 2D 合成预览：与保存时 card.webp 同一套 compose_card 逻辑（收集册缩略图同款）
                from PIL import Image
                front_p = None
                if face_p:
                    if face_as_fg or text_type == "boxed":
                        # 卡面只作前景层：底图用背景/深色（与 3D 预览一致）
                        if bg_p:
                            front_p = bg_view_p
                    else:
                        front_p = face_p
                elif card_sel and _card_front_path(card_sel) is not None:
                    front_p = _card_front_path(card_sel)
                elif bg_p:
                    front_p = bg_view_p
                fg_im = None
                if face_p and face_floats:
                    # 与 3D 预览对齐：浮起 / 只作前景 / 文本型时，卡面同时是前景层
                    fg_im = Image.open(face_p)
                elif card_sel:
                    fp = ASSETS / "output" / card_sel / "foreground.png"
                    if fp.exists() and fp.stat().st_size > 0:
                        fg_im = Image.open(fp)
                elif fg.startswith("/preview/_tmp/"):
                    tp = TMP_DIR / Path(fg).name
                    if tp.exists() and tp.stat().st_size > 0:
                        fg_im = Image.open(tp)
                # 视差主体：2D 预览取「中性视角」的合成图（静止看不出层次，但构图对）
                if parallax_sel:
                    try:
                        fg_im = parallax.composite(parallax.load_item(parallax_sel),
                                                   (CARD_W, CARD_H))
                    except Exception as e:  # noqa: BLE001
                        print(f"[cardforge] 视差主体 2D 预览失败：{e}")
                composed = compose_card(
                    fg_im, style="transparent", front=front_p,
                    background=str(bg_path) if bg_path else None,
                    frame=frame_path, seal=seal_path, seal_frame=seal_frame_path,
                    title=name, description=desc, text_type=text_type,
                    text_area=data.get("text_pos"),
                    subject_over_frame=subject_over_frame,
                    subject_outline=subject_outline, size=(CARD_W, CARD_H),
                    content_scale=scale,
                    face_scales=face_scales,
                    face_cover=bool(lenti_urls or video),
                    mask_clip=mask_clip,
                    subject_offset_x=subject_offset_x,
                    subject_offset_y=subject_offset_y,
                )
                PREVIEW_DIR.mkdir(parents=True, exist_ok=True)
                pname = f"asm_{uuid.uuid4().hex[:8]}.png"
                composed.save(PREVIEW_DIR / pname)
                return {"ok": True, "url": f"/preview/_preview/{pname}"}
            # 3D 实时预览：视差主体要把每层图落到本预览目录（token 要先定，
            # 所以这里自己建目录，不走 _write_preview_html）
            token = uuid.uuid4().hex[:12]
            pdir = PREVIEW_DIR / token
            pdir.mkdir(parents=True, exist_ok=True)
            para_payload = None
            if parallax_sel:
                try:
                    para_payload = parallax.prepare_for_card(parallax_sel, pdir)
                except Exception as e:  # noqa: BLE001
                    print(f"[cardforge] 预览应用视差主体失败：{e}")
            html = webcard.build_card_html(name, front_url, fg, back_url, effects=effects,
                                           frame_rel=frame_url, seal_rel=seal_url,
                                           seal_name=seal_name if seal_url else None,
                                           seal_frame_rel=seal_frame_url,
                                           seal_frame_name=seal_frame_name if seal_frame_url else None,
                                           description=desc, text_type=text_type,
                                           text_pos=data.get("text_pos"),
                                           subject_over_frame=subject_over_frame,
                                           mask_clip=mask_clip,
                                           frame_fit_subject=True,
                                           round_foreground=True,
                                           outline_rel=outline_url,
                                           seal_strength_front=seal_front,
                                           seal_strength_back=seal_back,
                                           seal_frame_strength_front=seal_frame_front,
                                           seal_frame_strength_back=seal_frame_back,
                                           content_scale=scale,
                                           face_scales=face_scales,
                                           interior_rel=interior_url,
                                           background_rel=bg_url,
                                           glow_name=(data.get("glow") or "none"),
                                           glow_strength=_parse_glow_strength(data.get("glow_strength")),
                                           glow_boost=frame_glow_boost(frame_path) if frame_path else 1.0,
                                           subject_offset_x=subject_offset_x,
                                           subject_offset_y=subject_offset_y,
                                detail_rel=detail_url,
                                lenti_images=lenti_urls,
                                lenti_opts=lenti_opts,
                                bg_shader=bg_shader,
                                # 预览页写在 assets/output/_preview/<token>/，比卡目录深一层，
                                # "../../shared/lib/" 会解析成 /preview/shared/... 而 404；
                                # 预览页只在 http 下打开，直接用 /shared/ 路由的绝对路径。
                                three_rel=webcard.THREE_SRC_ABS,
                                parallax=para_payload)
            (pdir / "preview.html").write_text(html, encoding="utf-8")
            _cleanup_old_previews()
            return {"ok": True, "url": f"/preview/_preview/{token}/preview.html"}
        # kind == "effect"（特效 DIY 预览）走这里：不需要额外素材目录
        return _write_preview_html(html)

    def _tmp_upload(self, data: dict) -> dict:
        b64 = data.get("b64", "")
        if not b64:
            raise ValueError("未收到图片数据")
        name = _safe_name((data.get("name") or "fg.png").replace("\\", "/").split("/")[-1])
        if not name.lower().endswith(_IMG_EXTS):
            name = "fg.png"
        tmp = TMP_DIR / f"{uuid.uuid4().hex[:8]}_{name}"
        tmp.write_bytes(base64.b64decode(b64))
        return {"ok": True, "url": f"/preview/_tmp/{tmp.name}", "name": tmp.name}

    def _asset_upload(self, data: dict) -> dict:
        atype = data.get("type", "")
        if atype not in _ASSET_TYPES:
            raise ValueError("未知素材类型")
        if atype in _EFFECT_ASSET_TYPES:
            raise ValueError("特效/边框特效请用「特效DIY」或代码定义，不支持图片上传")
        b64 = data.get("b64", "")
        if not b64:
            raise ValueError("未收到图片数据")
        name = _safe_name((data.get("name") or "asset.png").replace("\\", "/").split("/")[-1])
        if not name.lower().endswith(_IMG_EXTS):
            name = "asset.png"
        target = ASSETS / atype / name
        if target.exists():
            raise ValueError(f"「{name}」已存在")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(base64.b64decode(b64))
        return {"ok": True, "type": atype, "name": name}

    def _asset_delete(self, data: dict) -> dict:
        atype = data.get("type", "")
        if "/" in str(data.get("name") or ""):
            raise ValueError("素材包里的素材请在「素材包管理」中处理（不支持单独删除）")
        name = _safe_name(data.get("name", ""))
        if atype not in _ASSET_TYPES or not name:
            raise ValueError("参数错误")
        if atype in _EFFECT_ASSET_TYPES:
            # 特效/边框特效：内置（同步删源码定义）+ 自定义（删 JSON），两者都尝试
            removed_builtin = (_remove_builtin_effect(name) if atype == "effects"
                               else _remove_builtin_glow(name))
            f = (EFFECTS_DIR / f"{name}.json" if atype == "effects"
                 else webcard.GLOW_CUSTOM_DIR / f"{name}.json")
            deleted_file = False
            if f.exists():
                f.unlink()
                deleted_file = True
            if not removed_builtin and not deleted_file:
                raise ValueError(f"「{name}」不存在")
            return {"ok": True, "name": name, "removed_builtin": removed_builtin}
        target = ASSETS / atype / name
        if not target.exists():
            raise ValueError(f"「{name}」不存在")
        removed_builtin = _remove_builtin_seal(Path(name).stem) if atype == "seals" else None
        target.unlink()
        return {"ok": True, "name": name, "removed_builtin": removed_builtin}

    def _asset_rename(self, data: dict) -> dict:
        atype = data.get("type", "")
        if "/" in str(data.get("old") or "") or "/" in str(data.get("new") or ""):
            raise ValueError("素材包里的素材不支持重命名，请在「素材包管理」中处理")
        old = _safe_name(data.get("old", ""))
        new = _safe_name(data.get("new", ""))
        if atype not in _ASSET_TYPES or not old or not new:
            raise ValueError("参数错误")
        if atype in _EFFECT_ASSET_TYPES:
            raise ValueError("特效/边框特效不支持重命名")
        if not new.lower().endswith(_IMG_EXTS):
            new = new + Path(old).suffix
        src = ASSETS / atype / old
        dst = ASSETS / atype / new
        if not src.exists():
            raise ValueError(f"「{old}」不存在")
        if dst.exists():
            raise ValueError(f"「{new}」已存在")
        src.rename(dst)
        return {"ok": True, "name": new}

    # ---------- 素材包 ----------

    def _pack_import(self, data: dict) -> dict:
        name = str(data.get("name") or "pack.zip")
        b64 = data.get("b64") or ""
        if not b64:
            raise ValueError("未收到压缩包数据")
        res = packs.import_zip(name, base64.b64decode(b64))
        return res

    def _pack_import_folder(self, data: dict) -> dict:
        files = data.get("files") or []
        if not isinstance(files, list):
            raise ValueError("文件列表无效")
        return packs.import_folder(files)

    def _pack_toggle(self, data: dict) -> dict:
        pid = str(data.get("id") or "").strip()
        if not pid:
            raise ValueError("缺少素材包 id")
        packs.set_enabled(pid, bool(data.get("enabled")))
        return {"ok": True, "id": pid, "enabled": bool(data.get("enabled"))}

    def _pack_delete(self, data: dict) -> dict:
        pid = str(data.get("id") or "").strip()
        if not pid:
            raise ValueError("缺少素材包 id")
        if not packs.delete_pack(pid):
            raise ValueError(f"找不到素材包「{pid}」")
        return {"ok": True, "id": pid}


PAGE_HTML = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>cardforge · 卡片工坊</title>
<style>
  :root {
    --bg: #171310; --panel: #221b16; --panel2: #2a211a;
    --line: #3d2f24; --gold: #e3b95c; --gold2: #f3d48a;
    --text: #ece4d6; --muted: #a99b86; --err: #e07a5f;
  }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body {
    background: radial-gradient(1200px 600px at 50% -10%, #2a2018 0%, var(--bg) 60%);
    color: var(--text); font-family: "Microsoft YaHei", "PingFang SC", system-ui, sans-serif;
    min-height: 100vh; padding: 28px 16px 60px;
  }
  .wrap { max-width: 1200px; margin: 0 auto; }
  header { text-align: center; margin-bottom: 18px; }
  header h1 { font-size: 30px; letter-spacing: 2px; color: var(--gold2); }
  header p { color: var(--muted); margin-top: 6px; font-size: 14px; }
  .tabs { display: flex; gap: 10px; justify-content: center; margin-bottom: 24px; flex-wrap: wrap; }
  .tab {
    padding: 10px 22px; border-radius: 999px; border: 1px solid var(--line);
    background: var(--panel); color: var(--muted); cursor: pointer; font-size: 14px;
    transition: all .15s; user-select: none;
  }
  .tab:hover { border-color: var(--gold); color: var(--gold2); }
  .tab.on { background: linear-gradient(135deg, var(--gold2), var(--gold)); color: #2b1d08; font-weight: 700; border-color: transparent; }
  .tabpage { display: none; }
  .panel {
    background: var(--panel); border: 1px solid var(--line); border-radius: 16px;
    padding: 20px; margin-bottom: 18px;
  }
  .panel h2 { font-size: 15px; color: var(--gold); margin-bottom: 14px; font-weight: 600; }
  .drop {
    border: 2px dashed var(--line); border-radius: 12px; padding: 30px 16px;
    text-align: center; cursor: pointer; transition: border-color .2s, background .2s;
    background: var(--panel2);
  }
  .drop:hover, .drop.drag { border-color: var(--gold); background: #32281c; }
  .drop .big { font-size: 40px; }
  .drop p { color: var(--muted); margin-top: 8px; font-size: 13px; }
  .thumb { margin-top: 12px; display: flex; align-items: center; gap: 12px; }
  .thumb img { width: 72px; height: 96px; object-fit: cover; border-radius: 8px; border: 1px solid var(--line); }
  .thumb span { color: var(--muted); font-size: 13px; word-break: break-all; }
  .grid { display: grid; grid-template-columns: 1fr 1fr; gap: 14px; }
  @media (max-width: 640px) { .grid { grid-template-columns: 1fr; } }
  label { display: block; font-size: 13px; color: var(--muted); margin-bottom: 6px; }
  input[type=text], input[type=number], select, textarea {
    width: 100%; background: var(--panel2); color: var(--text); border: 1px solid var(--line);
    border-radius: 10px; padding: 10px 12px; font-size: 14px; outline: none;
  }
  input[type=text]:focus, input[type=number]:focus, select:focus, textarea:focus { border-color: var(--gold); }
  textarea { resize: vertical; font-family: inherit; line-height: 1.6; }
  .descbar { display: flex; flex-direction: column; gap: 8px; width: 100%; }
  .descbar .btn { align-self: flex-end; }
  .arearow { display: flex; gap: 6px; align-items: center; flex-wrap: wrap; }
  .arearow span { font-size: 11px; color: var(--muted); }
  .arearow input[type=number] { width: 62px; padding: 5px 6px; }
  .arearow .hint { font-size: 11px; color: var(--muted); margin-left: 2px; }
  .pickrow { display: flex; gap: 8px; align-items: center; }
  .pickrow select { flex: 1; min-width: 0; }
  .pickrow .btn { flex-shrink: 0; }
  .cardimg-placing { outline: 3px dashed var(--gold); cursor: crosshair !important; }
  .radios { display: flex; gap: 10px; }
  .radio {
    flex: 1; background: var(--panel2); border: 1px solid var(--line); border-radius: 10px;
    padding: 10px; cursor: pointer; text-align: center; font-size: 13px; color: var(--muted);
    transition: border-color .15s, color .15s, background .15s; user-select: none;
  }
  .radio.on { border-color: var(--gold); color: var(--gold2); background: #32281c; }
  .radio small { display: block; font-size: 11px; color: var(--muted); margin-top: 3px; }
  .row { margin-bottom: 14px; }
  /* 光栅卡卡面（拼装页） */
  .lenti-box { border-left: 2px solid var(--line); padding-left: 10px; }
  .lenti-box[hidden] { display: none; }
  .lenti-chips { display: flex; flex-direction: column; gap: 4px; margin: 6px 0 10px; }
  .lenti-chips .lc {
    display: flex; justify-content: space-between; gap: 8px; font-size: 11px;
    color: var(--muted); background: var(--panel2); border: 1px solid var(--line);
    border-radius: 8px; padding: 4px 8px; line-height: 1.4; word-break: break-all;
  }
  .lenti-chips .lc b { flex: 0 0 auto; color: #8d8072; cursor: pointer; }
  .lenti-chips .lc b:hover { color: #e07a5f; }
  .arearow input[type=range] {
    flex: 1 1 90px; min-width: 80px; width: auto; height: 16px;
    accent-color: var(--gold); background: transparent;
  }
  .arearow .lval { font-size: 11px; color: var(--gold); font-variant-numeric: tabular-nums; min-width: 30px; }
  .row .checkrow { display: flex; align-items: center; gap: 8px; cursor: pointer; font-size: 13px; color: var(--text); }
  .row .checkrow input { width: 16px; height: 16px; accent-color: var(--gold); cursor: pointer; }
  .row .checkrow span { line-height: 1.4; }
  .btn {
    width: 100%; padding: 14px; border: none; border-radius: 12px; cursor: pointer;
    font-size: 16px; font-weight: 700; letter-spacing: 2px; color: #2b1d08;
    background: linear-gradient(135deg, var(--gold2), var(--gold));
    box-shadow: 0 4px 18px rgba(227, 185, 92, .25); transition: transform .1s, box-shadow .2s;
  }
  .btn:hover { transform: translateY(-1px); box-shadow: 0 6px 24px rgba(227, 185, 92, .35); }
  .btn:disabled { opacity: .5; cursor: not-allowed; transform: none; }
  .btn.small { width: auto; padding: 8px 16px; font-size: 13px; letter-spacing: 1px; border-radius: 10px; }
  .btn.ghost {
    background: transparent; color: var(--muted); border: 1px solid var(--line);
    box-shadow: none; font-weight: 400; letter-spacing: 1px;
  }
  .btn.ghost:hover { color: var(--gold2); border-color: var(--gold); }
  .btn.danger { background: linear-gradient(135deg, #e07a5f, #c25a3f); color: #2b120a; }
  .status { margin-top: 12px; font-size: 14px; color: var(--muted); min-height: 20px; text-align: center; }
  .status.err { color: var(--err); }
  .result { display: none; }
  .result .main { display: flex; gap: 18px; align-items: flex-start; flex-wrap: wrap; }
  .result .main img.main-card {
    width: 270px; border-radius: 12px; border: 1px solid var(--gold); box-shadow: 0 8px 30px rgba(0,0,0,.5);
  }
  .subs { flex: 1; min-width: 200px; }
  .meta { margin-top: 14px; background: var(--panel2); border-radius: 10px; padding: 12px 14px; font-size: 12px; color: var(--muted); word-break: break-all; line-height: 1.9; }
  .meta b { color: var(--gold2); font-weight: 600; }
  .hint { text-align: center; color: var(--muted); font-size: 12px; margin-top: 18px; }
  .webcard { margin-top: 16px; text-align: center; }
  .webcard a {
    display: inline-block; padding: 12px 22px; border-radius: 12px;
    text-decoration: none; font-size: 15px; font-weight: 700; letter-spacing: 1px;
    color: #2b1d08; background: linear-gradient(135deg, var(--gold2), var(--gold));
    box-shadow: 0 4px 18px rgba(227, 185, 92, .25);
  }
  .webcard a:hover { transform: translateY(-1px); }
  iframe.preview {
    width: 100%; height: 560px; border: 1px solid var(--line); border-radius: 12px;
    background: #14100c; display: block;
  }
  iframe.preview.tall { height: 640px; }
  .prevbar { display: flex; gap: 8px; align-items: center; margin-bottom: 10px; flex-wrap: wrap; }
  .prevbar select, .prevbar input { width: auto; padding: 7px 10px; font-size: 13px; border-radius: 9px; }
  .prevbar input[type=number] { width: 74px; }
  .prevbar .grow { flex: 1; }
  .prevbar span { font-size: 12px; color: var(--muted); }
  /* 特效选择器 */
  .fx-row { display: flex; gap: 8px; flex-wrap: wrap; }
  .fx-row select { flex: 1; min-width: 120px; }
  .fx-row .btn.small { flex: 0 0 auto; }
  .fx-row input[type=number] { flex: 0 0 80px; }
  .fx-chips { display: flex; gap: 8px; flex-wrap: wrap; margin-top: 10px; min-height: 26px; }
  .chip {
    display: inline-flex; gap: 6px; align-items: center; background: var(--panel2);
    border: 1px solid var(--line); border-radius: 999px; padding: 4px 8px 4px 12px; font-size: 12px; color: var(--gold2);
  }
  .chip button { background: none; border: none; color: var(--err); cursor: pointer; font-size: 14px; line-height: 1; }
  /* 特效DIY */
  .diy-layout { display: grid; grid-template-columns: 220px minmax(0,1fr); gap: 16px; align-items: start; }
  .diy-side { display: flex; flex-direction: column; gap: 8px; }
  .fx-list { max-height: 560px; overflow-y: auto; display: flex; flex-direction: column; gap: 6px; }
  .fx-item {
    background: var(--panel2); border: 1px solid var(--line); border-radius: 10px;
    padding: 8px 10px; cursor: pointer; font-size: 13px; color: var(--muted);
    display: flex; align-items: center; gap: 6px; transition: border-color .15s, color .15s;
  }
  .fx-item:hover { border-color: var(--gold); color: var(--gold2); }
  .fx-item.on { border-color: var(--gold); color: var(--gold2); background: #32281c; }
  .fx-item .badge { font-size: 10px; color: var(--muted); border: 1px solid var(--line); border-radius: 999px; padding: 1px 7px; }
  .fx-item .del { margin-left: auto; color: var(--err); cursor: pointer; font-size: 15px; }
  .fx-sec h3 { font-size: 12px; color: var(--gold); margin: 14px 0 6px; letter-spacing: 2px; border-bottom: 1px solid var(--line); padding-bottom: 4px; }
  .fx-sec:first-child h3 { margin-top: 0; }
  .fx-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 8px 12px; }
  @media (max-width: 720px) { .fx-grid { grid-template-columns: 1fr; } }
  .frow label { font-size: 12px; margin-bottom: 3px; }
  .frow input[type=number], .frow input[type=text], .frow select { padding: 7px 10px; font-size: 13px; }
  .checkwrap { display: flex; align-items: center; gap: 8px; padding: 10px 2px; }
  .checkwrap label { margin: 0; cursor: pointer; }
  .checkwrap input { width: 17px; height: 17px; accent-color: var(--gold); }
  .imagerow { display: flex; gap: 8px; margin-bottom: 8px; align-items: center; }
  .imagerow select { flex: 1; }
  .imagerow .hint2 { font-size: 11px; color: var(--muted); line-height: 1.7; }
  .fx-imagebox { background: var(--panel2); border: 1px dashed var(--line); border-radius: 10px; padding: 10px; }
  .savebar { display: flex; gap: 10px; align-items: center; margin-top: 16px; flex-wrap: wrap; }
  .savebar input { flex: 1; min-width: 160px; }
  .savebar .btn.small { flex: 0 0 auto; }
  .preview-mode { display: flex; gap: 8px; margin-bottom: 10px; }
  .preview-mode .btn.on { background: linear-gradient(135deg, var(--gold2), var(--gold)); color: #2b1d08; border-color: transparent; font-weight: 700; }
  /* 组合特效 */
  .combo-panel { margin-top: 14px; padding: 12px 14px; }
  .combo-panel h2 { font-size: 14px; color: var(--gold); margin-bottom: 8px; }
  .combo-row { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; margin-bottom: 8px; }
  .combo-row select { flex: 0 1 220px; }
  .combo-row .hint2 { font-size: 11px; color: var(--muted); line-height: 1.6; }
  #comboList { display: flex; flex-direction: column; gap: 6px; }
  .combo-item {
    display: flex; align-items: center; gap: 8px; background: var(--panel2);
    border: 1px solid var(--line); border-radius: 8px; padding: 6px 10px; font-size: 13px;
  }
  .combo-item .combo-label { color: var(--gold2); flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .combo-item select { flex: 0 1 150px; font-size: 12px; padding: 5px 8px; }
  .combo-item .btn { padding: 4px 10px; font-size: 12px; }
  /* 特效DIY 浮动实时预览 */
  .fx-float {
    position: fixed; right: 18px; bottom: 18px; z-index: 90;
    width: 380px; background: var(--panel); border: 1px solid var(--line);
    border-radius: 12px; overflow: hidden; box-shadow: 0 14px 44px rgba(0,0,0,.55);
    display: flex; flex-direction: column; min-width: 260px;
  }
  .fx-float.collapsed { width: 260px; }
  .fx-float.collapsed .fx-float-bar, .fx-float.collapsed iframe { display: none; }
  .fx-float-head {
    display: flex; align-items: center; gap: 8px; padding: 8px 10px;
    background: linear-gradient(135deg, #2c2117, #221a13); border-bottom: 1px solid var(--line);
    cursor: move; user-select: none;
  }
  .fx-float-title { font-size: 13px; color: var(--gold2); font-weight: 700; letter-spacing: 1px; }
  .fx-float-bar {
    display: flex; gap: 6px; align-items: center; padding: 7px 10px;
    border-bottom: 1px solid var(--line); flex-wrap: wrap; background: rgba(0,0,0,.18);
  }
  .fx-float-bar span { font-size: 11px; color: var(--muted); }
  .fx-float-bar select, .fx-float-bar input { padding: 4px 6px; font-size: 12px; border-radius: 7px; width: auto; }
  .fx-float-bar input[type=number] { width: 56px; }
  .fx-float-bar #fxPrevStatus { font-size: 11px; color: var(--muted); }
  .fx-float iframe.preview { height: 420px; border: 0; border-radius: 0; flex: 1; }
  .fx-float-min {
    background: none; border: 1px solid var(--line); color: var(--muted);
    border-radius: 7px; padding: 2px 9px; cursor: pointer; font-size: 13px; line-height: 1.4;
  }
  .fx-float-min:hover { color: var(--gold2); border-color: var(--gold); }
  /* 卡牌拼装 */
  .asm-layout { display: grid; grid-template-columns: minmax(300px, 400px) 1fr; gap: 16px; align-items: start; }
  @media (max-width: 980px) { .asm-layout { grid-template-columns: 1fr; } .diy-layout { grid-template-columns: 1fr; } }
  .asm-preview { position: sticky; top: 16px; }
  .thumb-sm { display: flex; align-items: center; gap: 10px; margin-top: 10px; }
  .thumb-sm img { width: 56px; height: 74px; object-fit: cover; border-radius: 8px; border: 1px solid var(--line); }
  .thumb-sm span { font-size: 12px; color: var(--muted); }
  .adaptbox { margin-top: 12px; padding-top: 12px; border-top: 1px solid var(--line); }
  .adaptbox .checkrow { margin-bottom: 8px; }
  .adaptbox .hint { color: var(--muted); }
  .adaptmodes { display: flex; gap: 8px; flex-wrap: wrap; }
  .adaptmodes .radio { padding: 7px 14px; }
  .adaptmodes .radio small { display: block; margin-top: 2px; opacity: 0.75; }
  .adaptmodes .radio.disabled { opacity: 0.4; pointer-events: none; }
  /* 素材管理 */
  .typechips { display: flex; gap: 8px; flex-wrap: wrap; margin-bottom: 16px; align-items: center; }
  .typechip {
    padding: 7px 16px; border-radius: 999px; border: 1px solid var(--line); background: var(--panel2);
    color: var(--muted); cursor: pointer; font-size: 13px;
  }
  .typechip.on { border-color: var(--gold); color: var(--gold2); background: #32281c; }
  .asset-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(150px, 1fr)); gap: 12px; }
  .asset-tile {
    background: var(--panel2); border: 1px solid var(--line); border-radius: 12px; padding: 10px;
    display: flex; flex-direction: column; gap: 8px;
  }
  .asset-tile img { width: 100%; height: 128px; object-fit: cover; border-radius: 8px; border: 1px solid var(--line); background: repeating-conic-gradient(#26221d 0% 25%, #1d1915 0% 50%) 0 0/16px 16px; }
  .asset-tile .asset-emoji { width: 100%; height: 128px; display: flex; align-items: center; justify-content: center; font-size: 44px; border-radius: 8px; border: 1px solid var(--line); background: var(--panel2); }
  .asset-tile .aname { font-size: 12px; color: var(--muted); word-break: break-all; text-align: center; }
  .asset-tile .arow { display: flex; gap: 6px; }
  .asset-tile .arow .btn.small { flex: 1; font-size: 12px; padding: 6px 8px; }
  .asset-empty { color: var(--muted); font-size: 13px; text-align: center; padding: 30px 0; }
</style>
<link rel="stylesheet" href="/fx2/editor.css">
</head>
<body>
<div class="wrap">
  <header>
    <h1>🎴 cardforge 卡片工坊</h1>
    <p>上传一张图自动做卡牌 · 特效 DIY · 卡牌拼装 · 素材管理（本地运行，隐私安全）</p>
  </header>

  <nav class="tabs">
    <div class="tab on" data-tab="make">🎨 生成卡牌</div>
    <div class="tab" data-tab="diy">✨ 特效DIY</div>
    <div class="tab" data-tab="fx2">🌟 特效DIY2</div>
    <div class="tab" data-tab="assemble">🧩 卡牌拼装</div>
    <div class="tab" data-tab="assets">🗂️ 素材管理</div>
    <div class="tab" data-tab="packs">📦 素材包</div>
<a class="tab" href="/album.html" target="_blank" style="text-decoration:none">📖 收集册</a>
<a class="tab" href="/codex.html" target="_blank" style="text-decoration:none">🐾 异兽牌</a>
<a class="tab" href="/cutout.html" target="_blank" style="text-decoration:none">✂️ 抠图工具</a>
<a class="tab" href="/lenti.html" target="_blank" style="text-decoration:none">🎞️ 光栅卡</a>
<a class="tab" href="/parallax.html" target="_blank" style="text-decoration:none">🧅 视差主体</a>
  </nav>

  <!-- ============ 1 生成卡牌 ============ -->
  <section class="tabpage" id="page-make">
    <div class="panel">
      <h2>1 · 选择图像</h2>
      <div class="drop" id="drop">
        <div class="big">🖼️</div>
        <p>点击选择，或把图片拖到这里</p>
        <input type="file" id="file" accept="image/*" hidden>
      </div>
      <div class="thumb" id="thumb" style="display:none">
        <img id="thumbImg" alt="预览">
        <span id="thumbName"></span>
      </div>
      <div class="adaptbox" id="adaptBox">
        <label class="checkrow">
          <input type="checkbox" id="makeAdaptive" checked>
          <span>🔄 自适应裁剪</span>
        </label>
        <div class="adaptmodes" id="adaptiveOpts">
          <div class="radio on" data-adaptive-mode="1">方式1（推荐）</div>
          <div class="radio" data-adaptive-mode="2">方式2 (横图适配)</div>
        </div>
      </div>
    </div>

    <div class="panel">
      <h2>2 · 卡片设置</h2>
      <div class="grid">
        <div class="row">
          <label>卡片名称</label>
          <input type="text" id="name" placeholder="例如：莲之空双人组">
        </div>
        <div class="row">
          <label>卡片 ID（slug，可修改）</label>
          <input type="text" id="slug" placeholder="自动生成">
        </div>
        <div class="row">
          <label>卡牌风格</label>
          <div class="radios" id="styleRadios">
            <div class="radio on" data-style="transparent">🧩 透明主体卡<small>类似 PVZ 植物卡</small></div>
            <div class="radio" data-style="full-bleed">🖼️ 整幅卡面<small>类似影之诗/游戏王</small></div>
          </div>
        </div>
        <div class="row">
          <label>抠图模型</label>
          <select id="model"></select>
        </div>
        <div class="row">
          <label class="checkrow">
            <input type="checkbox" id="makeFloatFg">
            <span>✨ 主体浮于边框之上（PVZ 式立体感，透明主体卡可选）</span>
          </label>
        </div>
        <div class="row">
          <label class="checkrow">
            <input type="checkbox" id="makeOutline">
            <span>🤍 主体描边（白色贴纸边，透明主体卡可选）</span>
          </label>
        </div>
        <div class="row">
          <label class="checkrow">
            <input type="checkbox" id="makeMaskClip" checked>
            <span>✂️ 裁剪到边框内（取消后主体可延伸出边框，边框外留一圈图像）</span>
          </label>
        </div>
        <div class="row">
          <label>背景层（DIY）</label>
          <select id="background"></select>
        </div>
        <div class="row">
          <label>卡牌类型</label>
          <div class="radios" id="typeRadios">
            <div class="radio on" data-text-type="none">🚫 无文本型<small>卡面不贴文字</small></div>
            <div class="radio" data-text-type="transparent">🪟 特殊文本型<small>透明文本框</small></div>
            <div class="radio" data-text-type="boxed">📜 文本型<small>带文本框边框</small></div>
          </div>
        </div>
        <div class="row">
          <label>边框（DIY）</label>
          <select id="frame"></select>
        </div>
        <div class="row">
          <label>缩放（卡面渲染比例，0.5~1.5，默认 1 不缩放）</label>
          <input type="number" id="makeScale" min="0.5" max="1.5" step="0.05" value="1">
        </div>
        <div class="row">
          <label>卡封（DIY，如镭射覆层）</label>
          <select id="seal"></select>
        </div>
        <div class="row">
          <label>边框卡封（仅边框区域显示，需配合边框使用）</label>
          <select id="sealFrame"></select>
        </div>
        <div class="row">
          <label>辉光效果（沿卡体/边框边缘发光）</label>
          <select id="makeGlow"></select>
        </div>
        <div class="row">
          <label>辉光强度（0~2，默认 1）</label>
          <input type="number" id="makeGlowStrength" min="0" max="2" step="0.05" value="1">
        </div>
        <div class="row">
          <label>卡面标题（可选）</label>
          <input type="text" id="title" placeholder="不填则无">
        </div>
        <div class="row">
          <label>卡牌效果（选择位置，自动居中，如需更多调整请自行PS）</label>
          <div class="descbar">
            <textarea id="desc" rows="2" placeholder="例如：登场时，使我方全体攻击力 +2。生成后可点击「📍 添加文本到卡牌」在卡面上定位文字。"></textarea>
            <button class="btn small" id="descPlace" type="button">📍 添加文本到卡牌</button>
          </div>
        </div>
        <div class="row">
          <label>卡牌描述区域（可选，百分比，默认 左8 右92 上70 下92）</label>
          <div class="arearow">
            <span>左</span><input type="number" id="descAreaL" min="0" max="100" value="8">
            <span>右</span><input type="number" id="descAreaR" min="0" max="100" value="92">
            <span>上</span><input type="number" id="descAreaT" min="0" max="100" value="70">
            <span>下</span><input type="number" id="descAreaB" min="0" max="100" value="92">
            <span class="hint">文本将在此区域内自动换行缩放，超出卡边时可手动调整</span>
          </div>
        </div>
      </div>
    </div>

    <div class="panel">
      <h2>3 · 特效（3D 网页动画，可添加多个，各自选择位置）</h2>
      <div id="makeFxPicker" class="fx-picker">
        <div class="fx-row">
          <select class="fx-effect"></select>
          <select class="fx-pos"></select>
          <input class="fx-posx" type="number" min="0" max="100" placeholder="X%">
          <input class="fx-posy" type="number" min="0" max="100" placeholder="Y%">
          <button class="btn small" type="button">＋ 添加</button>
        </div>
        <div class="fx-chips"></div>
      </div>
    </div>

    <button class="btn" id="go">✨ 生成卡牌</button>
    <div class="status" id="status"></div>

    <div class="panel result" id="result">
      <h2>4 · 生成结果</h2>
      <div class="main">
        <img class="main-card" id="cardImg" alt="卡图">
        <div class="subs">
          <div class="webcard" id="webcardBox" style="display:none">
            <a id="webcardLink" href="#" target="_blank">🃏 打开 3D 网页卡</a>
          </div>
          <div class="webcard" id="asmOpenBox" style="display:none">
            <button class="btn small ghost" id="asmOpenBtn" type="button">🧩 在拼装页打开</button>
          </div>
          <div class="meta" id="meta"></div>
        </div>
      </div>
    </div>
  </section>

  <!-- ============ 2 特效DIY ============ -->
  <section class="tabpage" id="page-diy">
    <div class="diy-layout">
      <div class="diy-side">
        <button class="btn small" id="fxNew" type="button">＋ 新建特效</button>
        <div class="fx-list" id="fxList"></div>
      </div>
      <div>
        <div class="panel">
          <h2>特效参数（均可选，留空即用默认）</h2>
          <div id="fxForm"></div>
          <div class="panel combo-panel">
            <h2>🧬 组合特效（多合一）</h2>
            <div class="combo-row">
              <select id="comboAddSel"></select>
              <button class="btn small" id="comboAddBtn" type="button">＋ 添加特效</button>
              <span class="hint2">添加多个已有特效后保存，即合并为一个新特效；留空则保存上方表单参数。组合内每个特效可各自选择出现位置。</span>
            </div>
            <div id="comboList"></div>
          </div>
          <div class="savebar">
            <input type="text" id="fxName" placeholder="特效名称（保存时必填，如：樱花雨）">
            <button class="btn small" id="fxSave" type="button">💾 保存特效</button>
            <button class="btn small danger" id="fxDelete" type="button">🗑️ 删除特效</button>
          </div>
          <div class="status" id="fxStatus"></div>
        </div>
      </div>
    </div>
  </section>

  <!-- ============ 2b 特效DIY2（影之诗官方闪卡特效，原生分页） ============ -->
  <section class="tabpage" id="page-fx2">
    <div id="fx2App"></div>
  </section>

  <!-- 特效DIY 浮动实时预览（可拖动/收起，改参数即时可见） -->
  <div class="fx-float" id="fxFloat">
    <div class="fx-float-head" id="fxFloatHead">
      <span class="fx-float-title">✨ 实时预览</span>
      <span class="grow"></span>
      <button class="btn small ghost" id="fxRefresh" type="button" title="重新播放">↻</button>
      <button class="fx-float-min" id="fxFloatMin" type="button" title="收起/展开">—</button>
    </div>
    <div class="fx-float-bar">
      <span>出现位置</span>
      <select id="fxPrevPos"></select>
      <input id="fxPrevX" type="number" min="0" max="100" placeholder="X%">
      <input id="fxPrevY" type="number" min="0" max="100" placeholder="Y%">
      <span class="grow"></span>
      <span id="fxPrevStatus"></span>
    </div>
    <iframe class="preview" id="fxFrame" src="about:blank"></iframe>
  </div>

  <!-- ============ 3 卡牌拼装 ============ -->
  <section class="tabpage" id="page-assemble">
    <div class="asm-layout">
      <div class="panel asm-controls">
        <h2>拼装素材</h2>
        <div class="row">
          <label>成品预览（选一张已完成的卡牌作为底图，自动沿用其前景）</label>
          <select id="asmCard"></select>
        </div>
        <div class="row">
          <label>卡面 / 主体（同一素材：不透明的铺满整卡当底图，抠好的透明图见下方勾选）</label>
          <div class="pickrow">
            <select id="asmFace"></select>
            <button class="btn small ghost" id="asmFaceUp" type="button" title="上传图片到卡面素材">＋ 导入</button>
            <button class="btn small ghost" id="asmFaceCut" type="button" title="从当前背景/成品卡抠出透明主体，存回卡面素材">✂️ 从背景抠取</button>
            <input type="file" id="asmFaceFile" accept="image/*" hidden>
          </div>
        </div>
        <div class="row">
          <label class="checkrow">
            <input type="checkbox" id="asmFaceFg">
            <span>🪟 这张图只作透明前景层（主体）—— 勾选后底图改用「背景」，适合已经抠好的透明图</span>
          </label>
        </div>
        <div class="row">
          <label>🧅 视差主体（多层主体，各层随视角错动；选了就取代上面的前景层）</label>
          <div class="pickrow">
            <select id="asmParallax"></select>
            <button class="btn small ghost" id="asmParallaxOpen" type="button" title="打开视差主体编辑器制作/修改">✎ 制作</button>
            <button class="btn small ghost" id="asmParallaxReload" type="button" title="刷新视差主体列表">⟳</button>
          </div>
        </div>
        <div class="row">
          <label class="checkrow">
            <input type="checkbox" id="asmLenti">
            <span>🎞️ 光栅卡卡面（2~6 张图随视角交替/渐变，取代上面的卡面；边框·卡封·特效照常叠加）</span>
          </label>
        </div>
        <div class="row lenti-box" id="asmLentiBox" hidden>
          <div class="pickrow" style="margin-bottom:6px">
            <select id="asmLentiAsset"></select>
            <button class="btn small ghost" id="asmLentiAssetAdd" type="button" title="把素材库里选中的卡面加进光栅队列">＋ 加入</button>
          </div>
          <div class="pickrow" style="margin-bottom:6px">
            <button class="btn small ghost" id="asmLentiAdd" type="button">⬆️ 上传图片</button>
            <button class="btn small ghost" id="asmLentiVideoUp" type="button"
                    title="视频/GIF 自动抽帧做成图集光栅卡（整幅换帧）">🎬 上传视频 / GIF</button>
            <button class="btn small ghost" id="asmLentiClear" type="button">清空</button>
            <input type="file" id="asmLentiFile" accept="image/*" multiple hidden>
            <input type="file" id="asmLentiVideoFile"
                   accept="video/*,image/gif,image/webp,image/apng" hidden>
          </div>
          <div class="arearow" style="margin-bottom:6px">
            <span>视频抽帧数</span>
            <select id="asmLentiVFrames" style="flex:1">
              <option value="8">8 帧 · 省体积，跳帧明显</option>
              <option value="12">12 帧</option>
              <option value="16">16 帧</option>
              <option value="20">20 帧</option>
              <option value="24" selected>24 帧 · 默认</option>
              <option value="30">30 帧</option>
              <option value="36">36 帧 · 最细腻，图集最大</option>
            </select>
            <span class="lval" id="asmLentiVFramesV"></span>
          </div>
          <div class="lenti-chips" id="asmLentiChips"></div>
          <div class="arearow" style="margin-bottom:4px">
            <span>换帧方式</span>
            <select id="asmLentiFade" style="flex:1">
              <option value="stripe">条纹推进（真光栅卡）</option>
              <option value="whole">整幅柔和过渡</option>
              <option value="cut">整幅直接切换</option>
            </select>
          </div>
          <div id="asmLentiFadeHint" style="color:#8d8477;font-size:11px;line-height:1.55;margin:0 0 8px"></div>
          <div class="arearow" style="margin-bottom:6px">
            <span>条纹方向</span>
            <input type="range" id="asmLentiAngle" min="0" max="180" step="1" value="0">
            <span class="lval" id="asmLentiAngleV">0°</span>
          </div>
          <div class="arearow" style="margin-bottom:6px">
            <span>透镜密度</span><input type="number" id="asmLentiLens" min="60" max="420" step="5" value="120">
            <span>棱纹</span><input type="number" id="asmLentiStripe" min="0" max="1" step="0.05" value="0.48">
            <span>光泽</span><input type="number" id="asmLentiSheen" min="0" max="2.2" step="0.05" value="0.9">
          </div>
          <div class="arearow">
            <span>柔和</span><input type="number" id="asmLentiSoft" min="0" max="1" step="0.05" value="0.62">
            <span>错位</span><input type="number" id="asmLentiJitter" min="0" max="0.4" step="0.01" value="0.02">
            <span>灵敏度</span><input type="number" id="asmLentiGain" min="0.2" max="3" step="0.1" value="1">
          </div>
        </div>
        <div class="row">
          <label>边框（可选，叠加在卡面之上）</label>
          <div class="pickrow">
            <select id="asmFrame"></select>
            <button class="btn small ghost" id="asmFrameUp" type="button" title="上传图片到边框素材">＋ 导入</button>
            <input type="file" id="asmFrameFile" accept="image/*" hidden>
          </div>
        </div>
        <div class="row">
          <label>背景（未选卡面时作底图；文本型时作卡面底图，可与卡面并存）</label>
          <div class="pickrow">
            <select id="asmBg"></select>
            <button class="btn small ghost" id="asmBgUp" type="button" title="上传图片到背景素材">＋ 导入</button>
            <input type="file" id="asmBgFile" accept="image/*" hidden>
          </div>
        </div>
        <div class="row">
          <label>牌背</label>
          <div class="pickrow">
            <select id="asmBack"></select>
            <button class="btn small ghost" id="asmBackUp" type="button" title="上传图片到牌背素材">＋ 导入</button>
            <input type="file" id="asmBackFile" accept="image/*" hidden>
          </div>
        </div>
        <div class="row">
          <label>卡封（可选，如镭射覆层/封印，随卡面缩放、低于边框）</label>
          <select id="asmSeal"></select>
        </div>
        <div class="row">
          <label>卡封叠加层（可选多层，自下而上依次叠加；每层独立正面/背面强度）</label>
          <div id="asmSealExtra"></div>
          <div class="pickrow" style="margin-top:6px">
            <button class="btn small ghost" id="asmSealAdd" type="button" title="再加一层卡封（如稀有度描边、细闪）">＋ 添加卡封层</button>
          </div>
        </div>
        <div class="row">
          <label>边框卡封（可选，仅边框区域显示，需配合边框使用）</label>
          <select id="asmSealFrame"></select>
        </div>
        <div class="row">
          <label>卡封效果强度（正面 / 背面，0~2，默认 0.945 / 0.5775）</label>
          <div class="arearow">
            <span>正面</span><input type="number" id="asmSealFront" min="0" max="2" step="0.05" value="0.945">
            <span>背面</span><input type="number" id="asmSealBack" min="0" max="2" step="0.05" value="0.5775">
          </div>
        </div>
        <div class="row">
          <label>边框卡封强度（正面 / 背面，0~2，默认 0.8 / 0.5）</label>
          <div class="arearow">
            <span>正面</span><input type="number" id="asmSealFrameFront" min="0" max="2" step="0.05" value="0.8">
            <span>背面</span><input type="number" id="asmSealFrameBack" min="0" max="2" step="0.05" value="0.5">
          </div>
        </div>
        <div class="row">
          <label>辉光效果（沿卡体/边框边缘发光）</label>
          <select id="asmGlow"></select>
        </div>
        <div class="row">
          <label>辉光强度（0~2，默认 1）</label>
          <input type="number" id="asmGlowStrength" min="0" max="2" step="0.05" value="1">
        </div>
        <div class="row">
          <label>缩放倍率（卡面 / 背景 / 边框各自独立，0.5~1.5，默认 1）</label>
          <div class="arearow">
            <span>卡面</span><input type="number" id="asmScale" min="0.5" max="1.5" step="0.05" value="1">
            <span>背景</span><input type="number" id="asmBgScale" min="0.5" max="1.5" step="0.05" value="1">
            <span>边框</span><input type="number" id="asmFrameScale" min="0.5" max="1.5" step="0.05" value="1">
          </div>
        </div>
        <div class="row">
          <label>主体偏移（仅移动图像位置，渲染范围不跟随；左右/上下，卡宽/卡高比例 ±0.9）</label>
          <div class="arearow">
            <span>左右</span><input type="number" id="asmOffsetX" min="-0.9" max="0.9" step="0.05" value="0">
            <span>上下</span><input type="number" id="asmOffsetY" min="-0.9" max="0.9" step="0.05" value="0">
          </div>
        </div>
        <div class="row">
          <label class="checkrow">
            <input type="checkbox" id="asmFloatFg">
            <span>✨ 主体浮于边框之上（PVZ 式立体感，透明主体卡可选）</span>
          </label>
        </div>
        <div class="row">
          <label class="checkrow">
            <input type="checkbox" id="asmOutline">
            <span>🤍 主体描边（白色贴纸边，透明主体卡可选）</span>
          </label>
        </div>
        <div class="row">
          <label class="checkrow">
            <input type="checkbox" id="asmMaskClip" checked>
            <span>✂️ 裁剪到边框内（取消后主体可延伸出边框，边框外留一圈图像）</span>
          </label>
        </div>
        <div class="row">
          <label>卡牌类型</label>
          <select id="asmTextType">
            <option value="none">🚫 无文本型（卡面不贴文字）</option>
            <option value="transparent">🪟 特殊文本型（透明文本框）</option>
            <option value="boxed">📜 文本型（带文本框边框）</option>
          </select>
        </div>
        <div class="row">
          <label>卡牌效果（选择位置，自动居中，如需更多调整请自行PS）</label>
          <textarea id="asmDesc" rows="2" placeholder="例如：登场时，使我方全体攻击力 +2。文字默认贴在卡牌下方区域。"></textarea>
        </div>
        <div class="row">
          <label>卡牌描述区域（可选，百分比，默认 左8 右92 上70 下92）</label>
          <div class="arearow">
            <span>左</span><input type="number" id="asmAreaL" min="0" max="100" value="8">
            <span>右</span><input type="number" id="asmAreaR" min="0" max="100" value="92">
            <span>上</span><input type="number" id="asmAreaT" min="0" max="100" value="70">
            <span>下</span><input type="number" id="asmAreaB" min="0" max="100" value="92">
          </div>
        </div>
        <div class="row">
          <label>特效（可添加多个，各自选择位置）</label>
          <div id="asmFxPicker" class="fx-picker">
            <div class="fx-row">
              <select class="fx-effect"></select>
              <select class="fx-pos"></select>
              <input class="fx-posx" type="number" min="0" max="100" placeholder="X%">
              <input class="fx-posy" type="number" min="0" max="100" placeholder="Y%">
              <button class="btn small" type="button">＋ 添加</button>
            </div>
            <div class="fx-chips"></div>
          </div>
        </div>
        <div class="savebar">
          <input type="text" id="asmName" placeholder="拼装名称（保存时必填）">
          <button class="btn small" id="asmSave" type="button">💾 保存为卡牌</button>
          <button class="btn small ghost" id="asmExportImg" type="button">🖼️ 导出图片</button>
          <button class="btn small ghost" id="asmExportHtml" type="button">🌐 导出网页</button>
          <button class="btn small ghost" id="asmReset" type="button">🧹 重置为空卡</button>
        </div>
        <div class="status" id="asmStatus"></div>
      </div>
      <div class="asm-preview">
        <div class="panel" style="margin-bottom:0">
          <h2>实时预览</h2>
          <iframe class="preview tall" id="asmPreviewFrame" src="about:blank"></iframe>
        </div>
      </div>
    </div>
  </section>

  <!-- ============ 4 素材管理 ============ -->
  <section class="tabpage" id="page-assets">
    <div class="panel">
      <h2>素材库</h2>
      <div class="typechips">
        <div class="typechip on" data-type="backgrounds">🖼️ 背景</div>
        <div class="typechip" data-type="faces">🌄 卡面 / 主体</div>
        <div class="typechip" data-type="frames">🖌️ 边框</div>
        <div class="typechip" data-type="frames-text">📜 文本框边框</div>
        <div class="typechip" data-type="seals">🌟 卡封</div>
        <div class="typechip" data-type="backs">🔙 卡背</div>
        <div class="typechip" data-type="effects-images">✨ 特效图</div>
        <div class="typechip" data-type="effects">🎆 特效</div>
        <div class="typechip" data-type="frame-effects">💡 边框特效</div>
        <select id="assetPackFilter" title="按来源包筛选" style="margin-left:8px;padding:8px 10px;border-radius:10px;border:1px solid var(--line);background:var(--panel);color:var(--text)"></select>
        <span class="grow" style="flex:1"></span>
        <button class="btn small" id="assetUploadBtn" type="button">⬆️ 上传素材</button>
        <input type="file" id="assetFile" accept="image/*" hidden>
      </div>
      <div class="asset-grid" id="assetGrid"></div>
      <div class="status" id="assetStatus"></div>
    </div>
  </section>

  <!-- ============ 5 素材包管理 ============ -->
  <section class="tabpage" id="page-packs">
    <div class="panel">
      <h2>素材包管理</h2>
      <p class="hint" style="text-align:left;padding:0;margin:-6px 0 12px">
        素材包 = 一个文件夹（或压缩包），包根放一份 <code>pack.json</code> 索引。导入会【复制】进工具的
        <code>packs/</code> 目录，原文件不动；删除只删这里面的，不影响内置素材。
      </p>
      <div class="typechips">
        <button class="btn small" id="packImportZipBtn" type="button">📥 导入压缩包 (.zip)</button>
        <button class="btn small" id="packImportDirBtn" type="button">📁 导入文件夹</button>
        <span class="grow" style="flex:1"></span>
        <button class="btn small ghost" id="packRefreshBtn" type="button">🔄 刷新</button>
        <input type="file" id="packZipFile" accept=".zip,application/zip" hidden>
        <input type="file" id="packDirFile" webkitdirectory directory multiple hidden>
      </div>
      <div class="asset-grid" id="packList"></div>
      <div class="status" id="packStatus"></div>
    </div>
  </section>

  <p class="hint">所有处理都在本机完成 · 产物保存在 assets/output/ 目录 · 模型首次使用自动下载</p>
</div>

<script src="/fx2/editor.js"></script>
<script>
const ASSETS = __ASSETS_JSON__;
const MODELS = __MODELS_JSON__;
const EFFECTS = __EFFECTS_JSON__;
const CARDS = __CARDS_JSON__;
const ASSET_META = __ASSET_META_JSON__;
let PACKS = __PACKS_JSON__;
// 素材键 → 显示标签：素材包素材标注来源包，内置素材直接用文件名
function assetLabel(k) {
  if (!k) return k;
  const m = ASSET_META[k];
  return m ? m.label : k;
}
function assetLabels(list) { return (list || []).map(assetLabel); }
// 素材键 → 可访问 URL（内置 = 文件名；素材包 = 包id/相对路径，逐段转义）
function assetUrl(type, key) {
  const segs = String(key).split('/').map(encodeURIComponent).join('/');
  return '/assets_abs/' + type + '/' + segs;
}
const POSITIONS = [
  ['random', '🎲 随机'], ['top', '⬆️ 上'], ['bottom', '⬇️ 下'], ['left', '⬅️ 左'], ['right', '➡️ 右'],
  ['center', '🎯 中央'], ['topLeft', '↖️ 左上'], ['topRight', '↗️ 右上'],
  ['bottomLeft', '↙️ 左下'], ['bottomRight', '↘️ 右下'], ['custom', '✏️ 自定义'],
];

// ---------- 分页切换 ----------
function switchTab(name) {
  document.querySelectorAll('.tab').forEach(t => t.classList.toggle('on', t.dataset.tab === name));
  document.querySelectorAll('.tabpage').forEach(p => { p.style.display = (p.id === 'page-' + name) ? 'block' : 'none'; });
  // 特效DIY2 原生编辑器：切到该页时才挂载，避免初始渲染时 WebGL 拿到 0×0 画布
  if (name === 'fx2') {
    const host = document.getElementById('fx2App');
    if (host) requestAnimationFrame(() => window.Fx2Editor && window.Fx2Editor.mount(host));
  }
  const fxFloat = document.getElementById('fxFloat');
  if (fxFloat) fxFloat.style.display = (name === 'diy') ? 'flex' : 'none';
}
document.querySelectorAll('.tab').forEach(t => t.addEventListener('click', () => switchTab(t.dataset.tab)));
switchTab('make');

// ---------- 工具 ----------
function setStatus(id, msg, isErr) {
  const el = document.getElementById(id);
  if (el) { el.textContent = msg || ''; el.className = 'status' + (isErr ? ' err' : ''); }
}
function effectEmoji(def) {
  if (def && def.engine === 'foil') return '🌀';   // 特效DIY2 的闪卡特效
  const img = (def && def.image) || '';
  return img.startsWith('text:') ? img.slice(5).trim().slice(0, 2) : '🖼️';
}
// 闪卡特效（特效DIY2）与粒子特效走两套完全不同的渲染路径
function isFoilDef(def) { return !!(def && def.engine === 'foil'); }
function fillSelect(id, items, labels) {
  const sel = document.getElementById(id);
  sel.innerHTML = '';
  items.forEach((v, i) => {
    const op = document.createElement('option');
    op.value = v; op.textContent = (labels && labels[i] != null) ? labels[i] : v;
    sel.appendChild(op);
  });
}
function fillFxSelect(sel, skipFoil) {
  sel.innerHTML = '';
  const addGroup = (label, list, builtin) => {
    if (!list.length) return;
    const g = document.createElement('optgroup');
    g.label = label;
    list.forEach(e => {
      if (skipFoil && isFoilDef(e.def)) return;   // 组合特效只接受粒子特效
      const op = document.createElement('option');
      op.value = e.name;
      op.textContent = effectEmoji(e.def) + ' ' + e.name + (builtin ? '（内置）' : '');
      g.appendChild(op);
    });
    sel.appendChild(g);
  };
  // 特效DIY（粒子）与特效DIY2（闪卡 foil）分组展示，不混在同一组
  const custom = EFFECTS.custom || [];
  const foil = custom.filter(e => isFoilDef(e.def));
  const particle = custom.filter(e => !isFoilDef(e.def));
  addGroup('✨ 粒子特效 · 特效DIY', particle, false);
  addGroup('🌀 闪卡特效 · 特效DIY2', foil, false);
  addGroup('🧩 内置特效', EFFECTS.builtin || [], true);
}
function buildPosSelect(sel) {
  sel.innerHTML = '';
  POSITIONS.forEach(p => {
    const op = document.createElement('option');
    op.value = p[0]; op.textContent = p[1];
    sel.appendChild(op);
  });
}
function toggleCustomPos(row) {
  const custom = row.querySelector('.fx-pos').value === 'custom';
  row.querySelector('.fx-posx').style.display = custom ? '' : 'none';
  row.querySelector('.fx-posy').style.display = custom ? '' : 'none';
}

// ---------- 特效选择器（生成卡牌 / 拼装共用） ----------
function createEffectPicker(root, onChange) {
  const state = [];
  const selEff = root.querySelector('.fx-effect');
  const selPos = root.querySelector('.fx-pos');
  const inpX = root.querySelector('.fx-posx');
  const inpY = root.querySelector('.fx-posy');
  const chips = root.querySelector('.fx-chips');
  const addBtn = root.querySelector('.fx-add') || root.querySelector('.btn');

  fillFxSelect(selEff);
  buildPosSelect(selPos);
  toggleCustomPos(root);
  selPos.addEventListener('change', () => toggleCustomPos(root));

  function afterChange() { if (typeof onChange === 'function') onChange(); }
  function render() {
    chips.innerHTML = '';
    state.forEach((it, i) => {
      const c = document.createElement('span');
      c.className = 'chip';
      const label = POSITIONS.find(p => p[0] === it.posType) || ['', '未知'];
      const posTxt = it.posType === 'custom'
        ? `自定义(${it.posX}%,${it.posY}%)` : (label[1] || it.posType);
      c.innerHTML = (effectEmoji(effDef(it.name)) || '✨') + ' ' + it.name + ' @ ' + posTxt +
        '<button title="移除">×</button>';
      c.querySelector('button').addEventListener('click', () => { state.splice(i, 1); render(); afterChange(); });
      chips.appendChild(c);
    });
  }
  addBtn.addEventListener('click', () => {
    const name = selEff.value;
    if (!name) return;
    const posType = selPos.value;
    const it = { name, posType, posX: inpX.value || '50', posY: inpY.value || '50' };
    state.push(it);
    render();
    afterChange();
  });
  return {
    get: () => state.map(it => {
      if (it.posType === 'custom') {
        const x = Math.min(100, Math.max(0, parseFloat(it.posX) || 50)) / 100;
        const y = 1 - Math.min(100, Math.max(0, parseFloat(it.posY) || 50)) / 100;
        return { name: it.name, posType: 'custom', posX: x, posY: y };
      }
      return { name: it.name, posType: it.posType };
    }),
    set: list => { state.length = 0; (list || []).forEach(i => state.push(i)); render(); afterChange(); },
    clear: () => { state.length = 0; render(); afterChange(); },
  };
}
function effDef(name) {
  const c = EFFECTS.custom.find(e => e.name === name);
  if (c) return c.def;
  const b = EFFECTS.builtin.find(e => e.name === name);
  return b ? b.def : null;
}

// ================= 1 生成卡牌 =================
const drop = document.getElementById('drop');
const fileInput = document.getElementById('file');
let fileData = null;

drop.addEventListener('click', () => fileInput.click());
drop.addEventListener('dragover', e => { e.preventDefault(); drop.classList.add('drag'); });
drop.addEventListener('dragleave', () => drop.classList.remove('drag'));
drop.addEventListener('drop', e => {
  e.preventDefault(); drop.classList.remove('drag');
  if (e.dataTransfer.files.length) loadFile(e.dataTransfer.files[0]);
});
fileInput.addEventListener('change', () => { if (fileInput.files.length) loadFile(fileInput.files[0]); });

function loadFile(f) {
  if (!f.type.startsWith('image/')) { setStatus('status', '请选择图片文件', true); return; }
  const reader = new FileReader();
  reader.onload = () => {
    fileData = { b64: reader.result.split(',')[1], name: f.name };
    document.getElementById('thumbImg').src = reader.result;
    document.getElementById('thumbName').textContent = f.name + '（' + (f.size / 1024 / 1024).toFixed(2) + ' MB）';
    document.getElementById('thumb').style.display = 'flex';
    if (!document.getElementById('name').value) {
      document.getElementById('name').value = f.name.replace(/\.[^.]+$/, '');
      syncSlug();
    }
    setStatus('status', '');
  };
  reader.readAsDataURL(f);
}

const nameInput = document.getElementById('name');
const slugInput = document.getElementById('slug');
let slugEdited = false;
slugInput.addEventListener('focus', () => { slugEdited = true; });
nameInput.addEventListener('input', () => { if (!slugEdited) syncSlug(); });
function syncSlug() {
  if (slugEdited) return;
  const s = nameInput.value.trim().toLowerCase().replace(/[^\w\u4e00-\u9fff-]+/g, '-').replace(/-+/g, '-').replace(/^-|-$/g, '');
  slugInput.value = s || 'card';
}

// 自适应：开启时方式1/方式2可选（互斥单选）；关闭时两者禁用
const makeAdaptive = document.getElementById('makeAdaptive');
const adaptiveOpts = document.getElementById('adaptiveOpts');
function syncAdaptive() {
  const on = makeAdaptive.checked;
  adaptiveOpts.querySelectorAll('.radio').forEach(r => r.classList.toggle('disabled', !on));
  adaptiveOpts.style.opacity = on ? 1 : 0.45;
  adaptiveOpts.style.pointerEvents = on ? 'auto' : 'none';
}
adaptiveOpts.addEventListener('click', e => {
  const el = e.target.closest('.radio');
  if (!el || !makeAdaptive.checked) return;
  adaptiveOpts.querySelectorAll('.radio').forEach(r => r.classList.remove('on'));
  el.classList.add('on');
});
makeAdaptive.addEventListener('change', syncAdaptive);
syncAdaptive();

const styleRadios = document.getElementById('styleRadios');
const bgSel = document.getElementById('background');
function refillBgSelect() {
  const vals = ['', ...ASSETS.backgrounds.slice(1)];
  const labels = ['无背景', ...ASSETS.backgrounds.slice(1).map(b => b === 'sample-1.png' ? '默认示例背景（羊皮纸）' : assetLabel(b))];
  fillSelect('background', vals, labels);
}
refillBgSelect();
// 背景默认值跟随风格：整幅卡面（+抠图）默认无背景，底图用上传原图（主体遮盖在原图主体上形成视差）；
// 透明主体卡默认示例背景；用户手动选过背景后不再自动切换
let bgTouched = false;
bgSel.addEventListener('change', () => { bgTouched = true; });
function syncBgByStyle() {
  const style = document.querySelector('#styleRadios .radio.on').dataset.style;
  const fullBleed = style === 'full-bleed';
  if (!bgTouched) bgSel.value = fullBleed ? '' : 'sample-1.png';
  // 整幅卡面：自适应方式锁定方式1（主体与底图同为 cover 变换才能视差对齐），并禁用选择
  const makeAdaptive = document.getElementById('makeAdaptive');
  if (makeAdaptive) {
    makeAdaptive.disabled = fullBleed;
    makeAdaptive.closest('.checkrow').style.opacity = fullBleed ? 0.45 : 1;
  }
  if (fullBleed) {
    adaptiveOpts.querySelectorAll('.radio').forEach(r => r.classList.remove('on'));
    adaptiveOpts.querySelector('[data-adaptive-mode="1"]').classList.add('on');
    adaptiveOpts.querySelectorAll('.radio').forEach(r => r.classList.add('disabled'));
    adaptiveOpts.style.opacity = 0.45;
    adaptiveOpts.style.pointerEvents = 'none';
  } else {
    syncAdaptive();
  }
  const floatFg = document.getElementById('makeFloatFg');
  if (floatFg) {
    floatFg.disabled = fullBleed;
    floatFg.closest('.checkrow').style.opacity = fullBleed ? 0.45 : 1;
  }
  const outline = document.getElementById('makeOutline');
  if (outline) {
    if (fullBleed) { outline.checked = false; }
    outline.disabled = fullBleed;
    outline.closest('.checkrow').style.opacity = fullBleed ? 0.45 : 1;
  }
}
styleRadios.addEventListener('click', e => {
  const el = e.target.closest('.radio'); if (!el) return;
  styleRadios.querySelectorAll('.radio').forEach(r => r.classList.remove('on'));
  el.classList.add('on');
  syncBgByStyle();
});
syncBgByStyle();

fillSelect('model', MODELS.map(m => m[0]), MODELS.map(m => m[0] + ' — ' + m[1]));
document.getElementById('model').value = __DEFAULT_MODEL__;
fillSelect('seal', ['', ...ASSETS.seals], ['不使用卡封', ...assetLabels(ASSETS.seals)]);
fillSelect('sealFrame', ['', 'same', ...ASSETS.seals], ['不使用边框卡封', '与卡封一致', ...assetLabels(ASSETS.seals)]);
// 辉光：默认选中第一个内置预设（旧卡未填时也是默认暖金描边，保持一致）
fillSelect('makeGlow', ['', ...ASSETS.glows], ['—— 不使用 ——', ...ASSETS.glows]);
(function () { const g = document.getElementById('makeGlow'); const def = ASSETS.glows.find(x => x) || ''; if (!g.value && def) g.value = def; })();
// 边框卡封需配合边框：未选边框时禁用；选边框时默认"与卡封一致"（手动选过则不覆盖）
const sealFrameSel = document.getElementById('sealFrame');
let sealFrameTouched = false;
function syncSealFrameEnabled() {
  const hasFrame = !!document.getElementById('frame').value;
  sealFrameSel.disabled = !hasFrame;
  sealFrameSel.closest('.row').style.opacity = hasFrame ? 1 : 0.45;
}
document.getElementById('frame').addEventListener('change', () => {
  syncSealFrameEnabled();
  if (document.getElementById('frame').value && !sealFrameTouched && sealFrameSel.value === '') {
    sealFrameSel.value = 'same';
  }
});
sealFrameSel.addEventListener('change', () => { sealFrameTouched = true; });
syncSealFrameEnabled();

// 卡牌类型：文本型（boxed）只展示带文本框边框（frames-text/），其余展示普通边框
function getTextType() {
  return document.querySelector('#typeRadios .radio.on').dataset.textType;
}
function refillFrameSelect() {
  const boxed = getTextType() === 'boxed';
  let opts, labels;
  if (boxed) {
    opts = [...ASSETS.frames_text, ...ASSETS.frames];
    labels = [...ASSETS.frames_text.map(f => '文本框·' + assetLabel(f)), ...assetLabels(ASSETS.frames)];
  } else {
    opts = ASSETS.frames;
    labels = assetLabels(opts);
  }
  fillSelect('frame', ['', ...opts], ['不使用边框', ...labels]);
  syncSealFrameEnabled();
}
refillFrameSelect();
document.getElementById('typeRadios').addEventListener('click', e => {
  const el = e.target.closest('.radio'); if (!el) return;
  document.querySelectorAll('#typeRadios .radio').forEach(r => r.classList.remove('on'));
  el.classList.add('on');
  refillFrameSelect();
});

// 添加文本：点击按钮进入定位模式，点击结果卡图放置描述文字
let lastCardId = null;
let placingText = false;
function getTextArea() {
  const ids = ['descAreaL', 'descAreaR', 'descAreaT', 'descAreaB'];
  const v = ids.map(id => parseFloat(document.getElementById(id).value));
  if (v.some(x => !isFinite(x))) return null;
  const clamp = x => Math.min(1, Math.max(0, x));
  return { x1: clamp(v[0] / 100), x2: clamp(v[1] / 100), y1: clamp(v[2] / 100), y2: clamp(v[3] / 100) };
}
const descPlaceBtn = document.getElementById('descPlace');
descPlaceBtn.addEventListener('click', () => {
  const desc = document.getElementById('desc').value.trim();
  if (!desc) { setStatus('status', '请先填写卡牌描述', true); return; }
  if (!lastCardId) { setStatus('status', '请先点击「✨ 生成卡牌」，再添加文本到卡牌', true); return; }
  placingText = true;
  document.getElementById('cardImg').classList.add('cardimg-placing');
  setStatus('status', '🖱️ 请在下方卡图上点击文字放置位置（卡牌下方区域通常效果最好）');
});
document.getElementById('cardImg').addEventListener('click', e => {
  if (!placingText) return;
  const r = e.currentTarget.getBoundingClientRect();
  const y = Math.min(1, Math.max(0, (e.clientY - r.top) / r.height));
  placingText = false;
  e.currentTarget.classList.remove('cardimg-placing');
  // 以点击位置为中心，保持当前区域高度，更新上下边界
  const cur = getTextArea() || { x1: 0.08, x2: 0.92, y1: 0.70, y2: 0.92 };
  const h = cur.y2 - cur.y1;
  let y1 = y - h / 2, y2 = y + h / 2;
  if (y1 < 0) { y2 -= y1; y1 = 0; }
  if (y2 > 1) { y1 -= (y2 - 1); y2 = 1; }
  document.getElementById('descAreaT').value = Math.round(y1 * 100);
  document.getElementById('descAreaB').value = Math.round(y2 * 100);
  updateCardText();
});
async function updateCardText() {
  if (!lastCardId) return;
  const tt = getTextType();
  const desc = document.getElementById('desc').value.trim();
  const resp = await fetch('/api/make_text', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ id: lastCardId, desc, text_type: tt, text_pos: getTextArea() }),
  });
  const d = await resp.json();
  if (!d.ok) throw new Error(d.error);
  const ts = Date.now();
  document.getElementById('cardImg').src = (d.preview || ('/preview/' + d.id + '/card.png')) + '?t=' + ts;
  const wl = document.getElementById('webcardLink');
  if (wl) wl.href = '/preview/' + d.id + '/card.html';
  setStatus('status', '✅ 文字已重新放置');
}

const makePicker = createEffectPicker(document.getElementById('makeFxPicker'));

const goBtn = document.getElementById('go');
goBtn.addEventListener('click', async () => {
  if (!fileData) { setStatus('status', '请先选择一张图像', true); return; }
  const style = document.querySelector('#styleRadios .radio.on').dataset.style;
  goBtn.disabled = true;
  setStatus('status', '⏳ 处理中…（首次使用模型需下载，请稍候）');
  document.getElementById('result').style.display = 'none';
  try {
    const resp = await fetch('/api/make', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        file_b64: fileData.b64, filename: fileData.name,
        name: nameInput.value.trim(), slug: slugInput.value.trim(),
        style, model: document.getElementById('model').value,
        background: document.getElementById('background').value,
        frame: document.getElementById('frame').value,
        seal: document.getElementById('seal').value,
        seal_frame: document.getElementById('sealFrame').value,
        scale: parseFloat(document.getElementById('makeScale').value) || 1,
        subject_over_frame: document.getElementById('makeFloatFg').checked,
        subject_outline: document.getElementById('makeOutline').checked,
        mask_clip: document.getElementById('makeMaskClip').checked,
        adaptive: document.getElementById('makeAdaptive').checked,
        adaptive_mode: parseInt(document.querySelector('#adaptiveOpts .radio.on').dataset.adaptiveMode, 10),
        glow: document.getElementById('makeGlow').value,
        glow_strength: parseFloat(document.getElementById('makeGlowStrength').value) || 1,
        effects: makePicker.get(),
        title: document.getElementById('title').value.trim() || null,
        text_type: getTextType(),
        desc: document.getElementById('desc').value.trim() || null,
        text_pos: getTextArea(),
      }),
    });
    const data = await resp.json();
    if (!resp.ok || !data.ok) { setStatus('status', '生成失败：' + (data.error || resp.status), true); return; }
    showResult(data);
    setStatus('status', '✅ 卡牌已生成');
  } catch (e) {
    setStatus('status', '生成失败：' + e.message, true);
  } finally {
    goBtn.disabled = false;
  }
});

function showResult(d) {
  const ts = Date.now();
  lastCardId = d.id;
  document.getElementById('cardImg').src = (d.preview || ('/preview/' + d.id + '/card.png')) + '?t=' + ts;
  const wb = document.getElementById('webcardBox');
  const wl = document.getElementById('webcardLink');
  if (d.webcard && wb && wl) {
    wl.href = '/preview/' + d.id + '/card.html';
    wb.style.display = 'block';
  } else if (wb) {
    wb.style.display = 'none';
  }
  const ab = document.getElementById('asmOpenBox');
  if (ab) ab.style.display = 'block';
  const meta = document.getElementById('meta');
  meta.innerHTML =
    '<b>卡片 ID</b>　' + d.id + '<br>' +
    '<b>名称</b>　' + d.name + '<br>' +
    '<b>风格</b>　' + (d.style === 'transparent' ? '透明主体卡' : '整幅卡面') + '<br>' +
    '<b>引擎</b>　' + d.engine + '<br>' +
    '<b>产物目录</b>　' + d.dir + '<br>' +
    '<b>卡牌定义</b>　' + d.card_json + '<br>' +
    (d.webcard ? '<b>3D 网页</b>　' + d.webcard + '<br>' : '') +
    (d.effects && d.effects.length ? '<b>特效</b>　' + d.effects.map(e => e.name + '@' + (e.pos.type === 'custom' ? '自定义' : e.pos.type)).join('、') + '<br>' : '') +
    '预览图可右键另存为保存。';
  document.getElementById('result').style.display = 'block';
  document.getElementById('result').scrollIntoView({ behavior: 'smooth' });
}

// 生成结果 → 拼装页：刷新卡片列表（新生成的卡进下拉），选中并触发加载配置
function openInAssembly(id) {
  setStatus('asmStatus', '⏳ 正在载入拼装页…');
  fetch('/api/cards?scope=album').then(r => r.json()).then(dc => {
    if (!dc.ok) throw new Error(dc.error || '刷新卡片列表失败');
    CARDS.length = 0;
    CARDS.push(...(dc.cards || []));
    const cur = asmCardSel.value;
    fillSelect('asmCard', ['', ...CARDS.map(c => c.id)], ['—— 不使用 ——', ...CARDS.map(c => c.name + '（' + c.id + '）')]);
    if (cur && [...asmCardSel.options].some(o => o.value === cur)) asmCardSel.value = cur;
    if (id && [...asmCardSel.options].some(o => o.value === id)) {
      asmCardSel.value = id;
      asmCardSel.dispatchEvent(new Event('change'));
      switchTab('assemble');
      document.getElementById('page-assemble').scrollIntoView({ behavior: 'smooth' });
    } else {
      switchTab('assemble');
      setStatus('asmStatus', '未在下拉中找到卡片 ' + id + '，请手动选择', true);
    }
  }).catch(e => setStatus('asmStatus', '跳转失败：' + e.message, true));
}

const asmOpenBtn = document.getElementById('asmOpenBtn');
if (asmOpenBtn) asmOpenBtn.addEventListener('click', () => openInAssembly(lastCardId));

// ================= 2 特效DIY =================
const FX_FIELDS = [
  { sec: '基础', fields: [
    { key: 'image', label: '图像', type: 'image' },
    { key: 'count', label: '数量（每次出现几个）', type: 'number', step: 1, min: 1, ph: '默认 1' },
    { key: 'lifetime', label: '效果存在时间（秒）', type: 'number', step: 0.1, ph: '默认 2' },
    { key: 'lifetimeRandom', label: '存在时间随机值', type: 'number', step: 0.1 },
    { key: 'probability', label: '产生几率（0~1，小于1随机触发）', type: 'number', step: 0.05, min: 0, max: 1, ph: '默认 1' },
    { key: 'delay', label: '延迟创建（秒）', type: 'number', step: 0.1, ph: '默认 0' },
    { key: 'delayRandom', label: '延迟创建随机时间', type: 'number', step: 0.1 },
  ]},
  { sec: '初始', fields: [
    { key: 'initialAngleZero', label: '初始角度总是为零（不随机朝向）', type: 'check' },
  ]},
  { sec: '位置偏移 · 绝对（产生点位置，正数向右/向上）', fields: [
    { key: 'absOffsetX', label: 'X 绝对偏移量', type: 'number', step: 0.05 },
    { key: 'absOffsetXRand', label: 'X 绝对随机偏移量', type: 'number', step: 0.05 },
    { key: 'absOffsetY', label: 'Y 绝对偏移量（正=上）', type: 'number', step: 0.05 },
    { key: 'absOffsetYRand', label: 'Y 绝对随机偏移量', type: 'number', step: 0.05 },
  ]},
  { sec: '位置偏移 · 相对（相对起始点的终点方向，正数向右/向上）', fields: [
    { key: 'relOffsetX', label: 'X 相对偏移量', type: 'number', step: 0.05 },
    { key: 'relOffsetXRand', label: 'X 相对随机偏移量', type: 'number', step: 0.05 },
    { key: 'relOffsetY', label: 'Y 相对偏移量（正=上）', type: 'number', step: 0.05 },
    { key: 'relOffsetYRand', label: 'Y 相对随机偏移量', type: 'number', step: 0.05 },
  ]},
  { sec: '速度', fields: [
    { key: 'speedX', label: 'X 速度', type: 'number', step: 0.1, ph: '默认 1' },
    { key: 'speedY', label: 'Y 速度', type: 'number', step: 0.1, ph: '默认 1' },
    { key: 'speedXRand', label: 'X 随机速度', type: 'number', step: 0.1 },
    { key: 'speedYRand', label: 'Y 随机速度', type: 'number', step: 0.1 },
  ]},
  { sec: '角度与旋转', fields: [
    { key: 'angleOffset', label: '角度偏移（正数向右）', type: 'number', step: 1 },
    { key: 'angleOffsetRand', label: '随机角度偏移', type: 'number', step: 1 },
    { key: 'spin', label: '转速', type: 'number', step: 0.1 },
    { key: 'spinRand', label: '随机转速', type: 'number', step: 0.1 },
  ]},
  { sec: '缩放', fields: [
    { key: 'startScale', label: '初始缩放倍数', type: 'number', step: 0.05, ph: '默认 1' },
    { key: 'endScale', label: '结束缩放倍数', type: 'number', step: 0.05, ph: '默认 1' },
  ]},
  { sec: '透明度', fields: [
    { key: 'fadeIn', label: '淡入时间（秒）', type: 'number', step: 0.05, ph: '默认 0' },
    { key: 'fadeOut', label: '淡出（秒）', type: 'number', step: 0.05, ph: '默认 0' },
    { key: 'alpha', label: '透明度（0~1，大于1可延迟淡出）', type: 'number', step: 0.05, ph: '默认 1' },
  ]},
  { sec: '颜色', fields: [
    { key: 'color', label: '颜色（16进制，如 #ff8844；灰白图叠加染色效果最佳）', type: 'text', ph: '留空=原色' },
  ]},
  { sec: '预设动作（区别于偏移量：从产生点持续向某方向运动）', fields: [
    { key: 'motion', label: '动作类型', type: 'select', options: [
      ['', '无动作（使用偏移量）'],
      ['spread', '扩散（向周围散开）'],
      ['shrink', '收缩（外围向产生点聚拢）'],
      ['moveL', '横移左'],
      ['moveR', '横移右'],
      ['moveLR', '横移交错（随机左右）'],
      ['moveU', '纵移上'],
      ['moveD', '纵移下'],
      ['moveUD', '纵移交错（随机上下）'],
      ['diagTL', '斜向·左上产生→右下移动'],
      ['diagTR', '斜向·右上产生→左下移动'],
      ['diagBL', '斜向·左下产生→右上移动'],
      ['diagBR', '斜向·右下产生→左上移动'],
      ['orbitCircle', '旋转圆形（绕产生点旋转）'],
      ['orbitSquare', '旋转方形（绕产生点矩形移动）'],
      ['sway', '波动横移（横移+正弦摆动）'],
      ['spiral', '螺旋扩散（扩散+旋转）'],
      ['bounce', '弹跳（上下弹跳+缓移）'],
    ]},
    { key: 'motionDir', label: '选择方向（旋转/交错类，默认顺时针）', type: 'select', options: [
      ['', '顺时针'], ['ccw', '逆时针'],
    ]},
    { key: 'centerDist', label: '中心距离（旋转/斜向类距产生点的距离）', type: 'number', step: 0.1, ph: '默认 0.9' },
    { key: 'moveSpeed', label: '移动速度（控制移动/旋转类，不控制偏移量）', type: 'number', step: 0.1, ph: '默认 2' },
  ]},
  { sec: '精灵帧动画（选填，图片为帧图时使用）', fields: [
    { key: 'frameWidth', label: '单帧宽度（px）', type: 'number', step: 1, min: 1 },
    { key: 'frameHeight', label: '单帧高度（px）', type: 'number', step: 1, min: 1 },
    { key: 'totalFrames', label: '动画总帧数', type: 'number', step: 1, min: 1 },
    { key: 'startFrame', label: '动画开始帧（第一帧为0）', type: 'number', step: 1, min: 0 },
    { key: 'endFrame', label: '动画结束帧', type: 'number', step: 1, min: 0 },
    { key: 'framePingPong', label: '动画帧重放（正放完再倒放）', type: 'check' },
    { key: 'frameLoop', label: '动画帧循环', type: 'check' },
    { key: 'frameSpeed', label: '动画帧速度（一般 0.x）', type: 'number', step: 0.05, ph: '默认 1' },
    { key: 'frameSpeedRand', label: '动画帧随机速度', type: 'number', step: 0.05 },
    { key: 'frameRandomStart', label: '动画帧随机开始添加', type: 'number', step: 1 },
  ]},
];

let currentFx = null; // {name, builtin}

function buildFxForm() {
  const form = document.getElementById('fxForm');
  form.innerHTML = '';
  FX_FIELDS.forEach(group => {
    const sec = document.createElement('div');
    sec.className = 'fx-sec';
    sec.innerHTML = '<h3>' + group.sec + '</h3>';
    const grid = document.createElement('div');
    grid.className = 'fx-grid';
    group.fields.forEach(f => {
      const row = document.createElement('div');
      row.className = 'frow';
      if (f.type === 'check') {
        row.innerHTML = '';
        const wrap = document.createElement('div');
        wrap.className = 'checkwrap';
        const cb = document.createElement('input');
        cb.type = 'checkbox'; cb.dataset.key = f.key; cb.dataset.type = 'check';
        const lbl = document.createElement('label');
        lbl.textContent = f.label;
        wrap.appendChild(cb); wrap.appendChild(lbl);
        row.appendChild(wrap);
      } else if (f.type === 'image') {
        const lbl = document.createElement('label');
        lbl.textContent = f.label;
        row.appendChild(lbl);
        const box = document.createElement('div');
        box.className = 'fx-imagebox';
        box.innerHTML =
          '<div class="imagerow"><input type="text" id="fxImageText" placeholder="输入 emoji 文本，如 💗（文字特效）"></div>' +
          '<div class="imagerow"><select id="fxImageFile"><option value="">不用图片</option></select>' +
          '<button class="btn small ghost" id="fxImageUp" type="button">⬆️ 上传</button>' +
          '<input type="file" id="fxImageFileInput" accept="image/*" hidden></div>' +
          '<div class="imagerow hint2">选择图片后，可配合下方「精灵帧动画」参数播放帧动画；' +
          '彩色图无法叠加白色，建议使用灰度图 + 颜色染色。</div>';
        row.appendChild(box);
      } else if (f.type === 'select') {
        const lbl = document.createElement('label');
        lbl.textContent = f.label;
        const sel = document.createElement('select');
        sel.dataset.key = f.key;
        (f.options || []).forEach(o => {
          const op = document.createElement('option');
          op.value = o[0]; op.textContent = o[1];
          sel.appendChild(op);
        });
        row.appendChild(lbl); row.appendChild(sel);
      } else {
        const lbl = document.createElement('label');
        lbl.textContent = f.label;
        const el = document.createElement('input');
        el.type = f.type === 'text' ? 'text' : 'number';
        el.dataset.key = f.key;
        if (f.type === 'number') {
          el.dataset.ntype = 'number';
          if (f.step != null) el.step = f.step;
          if (f.min != null) el.min = f.min;
          if (f.max != null) el.max = f.max;
        }
        if (f.ph) el.placeholder = f.ph;
        row.appendChild(lbl); row.appendChild(el);
      }
      grid.appendChild(row);
    });
    sec.appendChild(grid);
    form.appendChild(sec);
  });
  fillSelect('fxImageFile', ['', ...ASSETS.effect_images]);
  bindFxEvents();
  bindImageUpload();
}

function bindImageUpload() {
  const up = document.getElementById('fxImageUp');
  const input = document.getElementById('fxImageFileInput');
  up.addEventListener('click', () => input.click());
  input.addEventListener('change', async () => {
    if (!input.files.length) return;
    const f = input.files[0];
    if (!f.type.startsWith('image/')) { setStatus('fxStatus', '请选择图片文件', true); return; }
    const reader = new FileReader();
    reader.onload = async () => {
      try {
        const r = await fetch('/api/assets/upload', {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ type: 'effects-images', b64: reader.result.split(',')[1], name: f.name }),
        });
        const d = await r.json();
        if (!d.ok) throw new Error(d.error || '上传失败');
        ASSETS.effect_images.push(d.name);
        const sel = document.getElementById('fxImageFile');
        const op = document.createElement('option');
        op.value = d.name; op.textContent = d.name;
        sel.appendChild(op);
        sel.value = d.name;
        setStatus('fxStatus', '✅ 图片已上传：' + d.name);
        schedulePreview();
      } catch (e) {
        setStatus('fxStatus', '上传失败：' + e.message, true);
      }
    };
    reader.readAsDataURL(f);
  });
}

function collectDef() {
  if (comboMembers.length) {
    return { subs: comboMembers.map(m => ({ name: m.name || '', def: m.def, pos: m.pos })) };
  }
  const def = {};
  document.querySelectorAll('#fxForm [data-key]').forEach(el => {
    const k = el.dataset.key;
    if (el.dataset.type === 'check') {
      if (el.checked) def[k] = true;
      return;
    }
    const v = el.value.trim();
    if (v === '') return;
    if (el.dataset.ntype === 'number') {
      const n = parseFloat(v);
      if (!isNaN(n)) def[k] = n;
    } else if (k === 'color') {
      const m = v.match(/^#?([0-9a-fA-F]{6})$/);
      if (m) def[k] = parseInt(m[1], 16);
    } else {
      def[k] = v;
    }
  });
  const t = document.getElementById('fxImageText').value.trim();
  const f = document.getElementById('fxImageFile').value;
  if (f) def.image = 'image:' + f;
  else if (t) def.image = t.startsWith('text:') ? t : 'text:' + t;
  return def;
}

function loadDef(def) {
  const subs = (def && def.subs && def.subs.length) ? def.subs : null;
  if (subs) {
    comboMembers = subs.map(s => ({ name: s.name || '', def: s.def || {}, pos: s.pos || { type: 'random' } }));
    renderComboList();
    document.querySelectorAll('#fxForm [data-key]').forEach(el => {
      if (el.dataset.type === 'check') el.checked = false;
      else el.value = '';
    });
    const tEl = document.getElementById('fxImageText'); if (tEl) tEl.value = '';
    const fEl = document.getElementById('fxImageFile'); if (fEl) fEl.value = '';
    return;
  }
  comboMembers = [];
  renderComboList();
  document.querySelectorAll('#fxForm [data-key]').forEach(el => {
    const k = el.dataset.key;
    if (el.dataset.type === 'check') { el.checked = !!def[k]; return; }
    if (k === 'color') {
      const c = def[k];
      el.value = typeof c === 'number' ? '#' + c.toString(16).padStart(6, '0')
        : (typeof c === 'string' && c.startsWith('#') ? c : '');
      return;
    }
    const v = def[k];
    el.value = (v === undefined || v === null) ? '' : v;
  });
  const img = def.image || '';
  const textEl = document.getElementById('fxImageText');
  const fileEl = document.getElementById('fxImageFile');
  if (img.startsWith('text:')) {
    textEl.value = img.slice(5);
    fileEl.value = '';
  } else if (img.startsWith('image:')) {
    textEl.value = '';
    fileEl.value = img.slice(6);
  } else {
    textEl.value = ''; fileEl.value = '';
  }
}

function renderFxList() {
  const box = document.getElementById('fxList');
  box.innerHTML = '';
  const item = (e, builtin) => {
    const d = document.createElement('div');
    d.className = 'fx-item' + (currentFx && currentFx.name === e.name && currentFx.builtin === builtin ? ' on' : '');
    d.innerHTML = effectEmoji(e.def) + ' ' + e.name +
      (builtin ? '<span class="badge">内置</span>' : '') +
      (builtin ? '' : '<span class="del" title="删除">🗑️</span>');
    d.addEventListener('click', () => {
      currentFx = { name: e.name, builtin };
      renderFxList();
      loadDef(e.def);
      document.getElementById('fxName').value = builtin ? '' : e.name;
      setStatus('fxStatus', builtin ? '内置特效（可复制参数后另存新名）' : '已载入自定义特效');
      schedulePreview();
    });
    if (!builtin) {
      d.querySelector('.del').addEventListener('click', async ev => {
        ev.stopPropagation();
        if (!confirm('删除特效「' + e.name + '」？')) return;
        try {
          const r = await fetch('/api/effect/delete', {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ name: e.name }),
          });
          const res = await r.json();
          if (!res.ok) throw new Error(res.error);
          if (currentFx && currentFx.name === e.name) { currentFx = null; document.getElementById('fxName').value = ''; }
          await refreshEffects();
          setStatus('fxStatus', '🗑️ 已删除');
        } catch (err) {
          setStatus('fxStatus', '删除失败：' + err.message, true);
        }
      });
    }
    box.appendChild(d);
  };
  // 这里是粒子特效编辑器：闪卡特效（engine=foil）由「特效DIY2」页编辑，不在此列出
  EFFECTS.custom.filter(e => !isFoilDef(e.def)).forEach(e => item(e, false));
  EFFECTS.builtin.forEach(e => item(e, true));
}

async function refreshEffects() {
  const r = await fetch('/api/effects');
  const d = await r.json();
  EFFECTS.custom = d.custom;
  EFFECTS.builtin = d.builtin;
  renderFxList();
  fillFxSelect(document.querySelector('#makeFxPicker .fx-effect'));
  fillFxSelect(document.querySelector('#asmFxPicker .fx-effect'));
  fillComboSel();
}

// ---------- 组合特效（多合一） ----------
let comboMembers = []; // [{name, def, pos}]

function findFxDef(name) {
  const e = EFFECTS.custom.find(x => x.name === name) || EFFECTS.builtin.find(x => x.name === name);
  return e ? e.def : null;
}

function addComboMember(name) {
  const def = findFxDef(name);
  if (!def) return;
  if (def.subs && def.subs.length) {
    def.subs.forEach(s => comboMembers.push({ name: s.name || '', def: s.def || {}, pos: s.pos || { type: 'random' } }));
  } else {
    comboMembers.push({ name, def, pos: { type: 'random' } });
  }
  renderComboList();
  schedulePreview();
}

function renderComboList() {
  const box = document.getElementById('comboList');
  box.innerHTML = '';
  comboMembers.forEach((m, i) => {
    const d = document.createElement('div');
    d.className = 'combo-item';
    const label = document.createElement('span');
    label.className = 'combo-label';
    label.textContent = effectEmoji(m.def) + ' ' + (m.name || '内联特效');
    const sel = document.createElement('select');
    sel.className = 'combo-pos';
    POSITIONS.forEach(p => {
      const op = document.createElement('option');
      op.value = p[0]; op.textContent = p[1];
      if (m.pos.type === p[0]) op.selected = true;
      sel.appendChild(op);
    });
    sel.addEventListener('change', () => { m.pos = { type: sel.value }; schedulePreview(); });
    const del = document.createElement('button');
    del.className = 'btn small danger ghost';
    del.textContent = '✖';
    del.title = '从组合移除';
    del.addEventListener('click', () => { comboMembers.splice(i, 1); renderComboList(); schedulePreview(); });
    d.appendChild(label); d.appendChild(sel); d.appendChild(del);
    box.appendChild(d);
  });
}

function fillComboSel() {
  fillFxSelect(document.getElementById('comboAddSel'), true);
}
document.getElementById('comboAddBtn').addEventListener('click', () => {
  const sel = document.getElementById('comboAddSel');
  if (sel.value) addComboMember(sel.value);
});
document.getElementById('comboAddSel').addEventListener('change', () => {
  const sel = document.getElementById('comboAddSel');
  if (sel.value) { addComboMember(sel.value); sel.value = ''; }
});

// 预览位置
function currentPreviewPos() {
  const v = document.getElementById('fxPrevPos').value;
  if (v === 'custom') {
    const x = Math.min(100, Math.max(0, parseFloat(document.getElementById('fxPrevX').value) || 50)) / 100;
    const y = 1 - Math.min(100, Math.max(0, parseFloat(document.getElementById('fxPrevY').value) || 50)) / 100;
    return { type: 'custom', x, y };
  }
  return { type: v };
}

let previewTimer = null;
function schedulePreview(delay) {
  clearTimeout(previewTimer);
  previewTimer = setTimeout(postFxPreview, delay == null ? 450 : delay);
}
async function postFxPreview() {
  setStatus('fxPrevStatus', '预览生成中…');
  try {
    const fx = [{ name: 'DIY预览', def: collectDef(), pos: currentPreviewPos() }];
    const r = await fetch('/api/preview_html', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ kind: 'effect', effects: fx }),
    });
    const d = await r.json();
    if (!d.ok) throw new Error(d.error);
    document.getElementById('fxFrame').src = d.url + '?t=' + Date.now();
    setStatus('fxPrevStatus', '');
  } catch (e) {
    setStatus('fxPrevStatus', '预览失败：' + e.message, true);
  }
}

function bindFxEvents() {
  document.querySelectorAll('#fxForm input, #fxForm select').forEach(el => {
    el.addEventListener('input', schedulePreview);
    el.addEventListener('change', schedulePreview);
  });
  document.getElementById('fxPrevPos').addEventListener('change', () => {
    const custom = document.getElementById('fxPrevPos').value === 'custom';
    document.getElementById('fxPrevX').style.display = custom ? '' : 'none';
    document.getElementById('fxPrevY').style.display = custom ? '' : 'none';
    schedulePreview(100);
  });
  document.getElementById('fxPrevX').addEventListener('input', schedulePreview);
  document.getElementById('fxPrevY').addEventListener('input', schedulePreview);
  document.getElementById('fxRefresh').addEventListener('click', postFxPreview);
}

document.getElementById('fxNew').addEventListener('click', () => {
  currentFx = null;
  comboMembers = [];
  renderComboList();
  buildFxForm();
  document.getElementById('fxName').value = '';
  renderFxList();
  setStatus('fxStatus', '');
  schedulePreview(100);
});

document.getElementById('fxSave').addEventListener('click', async () => {
  const name = document.getElementById('fxName').value.trim();
  if (!name) { setStatus('fxStatus', '请先填写特效名称', true); return; }
  try {
    const r = await fetch('/api/effect/save', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name, def: collectDef() }),
    });
    const d = await r.json();
    if (!d.ok) throw new Error(d.error);
    await refreshEffects();
    setStatus('fxStatus', '✅ 特效「' + name + '」已保存');
  } catch (e) {
    setStatus('fxStatus', '保存失败：' + e.message, true);
  }
});

document.getElementById('fxDelete').addEventListener('click', async () => {
  const name = document.getElementById('fxName').value.trim();
  if (!name) { setStatus('fxStatus', '请先填写要删除的特效名称', true); return; }
  if (EFFECTS.builtin.some(e => e.name === name)) {
    setStatus('fxStatus', '内置特效不能删除', true);
    return;
  }
  if (!confirm('删除特效「' + name + '」？')) return;
  try {
    const r = await fetch('/api/effect/delete', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name }),
    });
    const d = await r.json();
    if (!d.ok) throw new Error(d.error);
    if (currentFx && currentFx.name === name) currentFx = null;
    document.getElementById('fxName').value = '';
    await refreshEffects();
    setStatus('fxStatus', '🗑️ 已删除');
  } catch (e) {
    setStatus('fxStatus', '删除失败：' + e.message, true);
  }
});

// 初始化 DIY
buildFxForm();
renderFxList();
buildPosSelect(document.getElementById('fxPrevPos'));
fillComboSel();
document.getElementById('fxPrevX').style.display = 'none';
document.getElementById('fxPrevY').style.display = 'none';
document.getElementById('fxPrevX').value = '50';
document.getElementById('fxPrevY').value = '50';
document.getElementById('fxImageText').value = '💗';
schedulePreview(600);

// 浮动预览：拖动 / 收起
(function () {
  const fxFloat = document.getElementById('fxFloat');
  const head = document.getElementById('fxFloatHead');
  let dragging = false, ox = 0, oy = 0;
  head.addEventListener('pointerdown', e => {
    if (e.target.closest('button')) return;
    dragging = true;
    ox = e.clientX - fxFloat.offsetLeft;
    oy = e.clientY - fxFloat.offsetTop;
    head.setPointerCapture(e.pointerId);
  });
  head.addEventListener('pointermove', e => {
    if (!dragging) return;
    fxFloat.style.left = (e.clientX - ox) + 'px';
    fxFloat.style.top = (e.clientY - oy) + 'px';
    fxFloat.style.right = 'auto';
    fxFloat.style.bottom = 'auto';
  });
  head.addEventListener('pointerup', () => { dragging = false; });
  head.addEventListener('pointercancel', () => { dragging = false; });
  document.getElementById('fxFloatMin').addEventListener('click', () => {
    fxFloat.classList.toggle('collapsed');
    document.getElementById('fxFloatMin').textContent = fxFloat.classList.contains('collapsed') ? '▢' : '—';
  });
})();

// ================= 3 卡牌拼装 =================
fillSelect('asmCard', ['', ...CARDS.map(c => c.id)], ['—— 不使用 ——', ...CARDS.map(c => c.name + '（' + c.id + '）')]);
fillSelect('asmFace', ['', ...ASSETS.faces], ['—— 不使用 ——', ...assetLabels(ASSETS.faces)]);
fillSelect('asmBg', ['', ...ASSETS.backgrounds.slice(1)], ['无背景', ...ASSETS.backgrounds.slice(1).map(b => b === 'sample-1.png' ? '默认示例背景（羊皮纸）' : assetLabel(b))]);
fillSelect('asmBack', ASSETS.backs, assetLabels(ASSETS.backs));
fillSelect('asmSeal', ['', ...ASSETS.seals], ['—— 不使用 ——', ...assetLabels(ASSETS.seals)]);
// 边框卡封："与卡封一致"=与卡牌卡封同素材（默认）；"不使用边框卡封"=空
fillSelect('asmSealFrame', ['', 'same', ...ASSETS.seals], ['—— 不使用边框卡封 ——', '与卡封一致', ...assetLabels(ASSETS.seals)]);
// 辉光：默认选中第一个内置预设（旧卡未填时也是默认暖金描边，保持一致）
fillSelect('asmGlow', ['', ...ASSETS.glows], ['—— 不使用 ——', ...ASSETS.glows]);
(function () { const g = document.getElementById('asmGlow'); const def = ASSETS.glows.find(x => x) || ''; if (!g.value && def) g.value = def; })();

// 卡牌类型：文本型（boxed）只展示带文本框边框（frames-text/）
const asmCardSel = document.getElementById('asmCard');
const asmFaceSel = document.getElementById('asmFace');
const asmFaceFgEl = document.getElementById('asmFaceFg');
const asmFrameSel = document.getElementById('asmFrame');
const asmBgSel = document.getElementById('asmBg');
const asmSealSel = document.getElementById('asmSeal');
const asmSealFrameSel = document.getElementById('asmSealFrame');
// 视差主体：拼装页的「多层主体」来源（来自 /api/parallax/list）
const asmParallaxSel = document.getElementById('asmParallax');
let PARALLAX_ITEMS = [];
async function loadParallaxList(keep) {
  try {
    const r = await fetch('/api/parallax/list');
    const d = await r.json();
    PARALLAX_ITEMS = d.items || [];
  } catch (e) { PARALLAX_ITEMS = []; }
  const cur = (keep === undefined) ? asmParallaxSel.value : keep;
  fillSelect('asmParallax', ['', ...PARALLAX_ITEMS.map(i => i.id)],
             ['—— 不使用 ——', ...PARALLAX_ITEMS.map(i => i.name + '（' + i.layer_count + ' 层）')]);
  if (cur && [...asmParallaxSel.options].some(o => o.value === cur)) asmParallaxSel.value = cur;
}
document.getElementById('asmParallaxReload').addEventListener('click', () => loadParallaxList());
document.getElementById('asmParallaxOpen').addEventListener('click', () => window.open('/parallax.html', '_blank'));
loadParallaxList('');

// 卡封叠加层：多层独立 shader，各层正/背强度分开（正面看封、牌背不封）
const asmSealExtraBox = document.getElementById('asmSealExtra');
function asmSealExtraRow(sel, front, back) {
  const row = document.createElement('div');
  row.className = 'pickrow seal-extra-row';
  row.style.marginTop = '6px';
  const selEl = document.createElement('select');
  const opts = [['', '—— 不使用 ——'], ...ASSETS.seals.map(n => [n, assetLabel(n)])];
  for (const [v, t] of opts) {
    const o = document.createElement('option');
    o.value = v; o.textContent = t;
    selEl.appendChild(o);
  }
  selEl.value = sel || '';
  const fEl = document.createElement('input');
  fEl.type = 'number'; fEl.min = '0'; fEl.max = '2'; fEl.step = '0.05';
  fEl.title = '正面强度'; fEl.style.width = '64px';
  fEl.value = (front === undefined || front === null) ? '0.6' : front;
  const bEl = document.createElement('input');
  bEl.type = 'number'; bEl.min = '0'; bEl.max = '2'; bEl.step = '0.05';
  bEl.title = '背面强度'; bEl.style.width = '64px';
  bEl.value = (back === undefined || back === null) ? '0' : back;
  const del = document.createElement('button');
  del.type = 'button'; del.className = 'btn small ghost'; del.textContent = '✕';
  del.onclick = () => row.remove();
  const tag1 = document.createElement('span'); tag1.textContent = '正面';
  const tag2 = document.createElement('span'); tag2.textContent = '背面';
  row.append(selEl, tag1, fEl, tag2, bEl, del);
  asmSealExtraBox.appendChild(row);
  return row;
}
const asmSealExtra = {
  get() {
    return [...asmSealExtraBox.querySelectorAll('.seal-extra-row')]
      .map(r => {
        const el = r.querySelectorAll('input');
        return { seal: r.querySelector('select').value,
                 front: parseFloat(el[0].value) || 0,
                 back: parseFloat(el[1].value) || 0 };
      })
      .filter(x => x.seal);
  },
  set(list) {
    asmSealExtraBox.innerHTML = '';
    (list || []).forEach(x => asmSealExtraRow(x.seal || x.rel, x.front, x.back));
  },
};
document.getElementById('asmSealAdd').addEventListener('click', () => asmSealExtraRow('', 0.6, 0));
const asmTextTypeSel = document.getElementById('asmTextType');
// 用户是否手动选过边框卡封：手动选过后不再自动切换为"与卡封一致"
let asmSealFrameTouched = false;
function refillAsmFrame() {
  const boxed = asmTextTypeSel.value === 'boxed';
  let opts, labels;
  if (boxed) {
    opts = [...ASSETS.frames_text, ...ASSETS.frames];
    labels = [...ASSETS.frames_text.map(f => '文本框·' + assetLabel(f)), ...assetLabels(ASSETS.frames)];
  } else {
    opts = ASSETS.frames;
    labels = assetLabels(opts);
  }
  const prev = asmFrameSel.value;
  fillSelect('asmFrame', ['', ...opts], ['—— 不使用 ——', ...labels]);
  if (prev && [...asmFrameSel.options].some(o => o.value === prev)) asmFrameSel.value = prev;
  syncAsmSealFrameEnabled();
}
// 边框卡封需配合边框：未选边框时禁用
function syncAsmSealFrameEnabled() {
  const hasFrame = !!asmFrameSel.value;
  asmSealFrameSel.disabled = !hasFrame;
  asmSealFrameSel.closest('.row').style.opacity = hasFrame ? 1 : 0.45;
}
refillAsmFrame();

// 卡面 / 边框 / 牌背：快速导入素材（复用素材管理页的上传接口，成功后自动选中）
function quickImport(btnId, fileId, type, after) {
  const btn = document.getElementById(btnId);
  const inp = document.getElementById(fileId);
  if (!btn || !inp) return;
  btn.addEventListener('click', () => inp.click());
  inp.addEventListener('change', () => {
    const f = inp.files && inp.files[0];
    inp.value = '';
    if (!f) return;
    if (!f.type.startsWith('image/')) { setStatus('asmStatus', '请选择图片文件', true); return; }
    const reader = new FileReader();
    reader.onload = async () => {
      try {
        const r = await fetch('/api/assets/upload', {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ type, b64: String(reader.result).split(',')[1], name: f.name }),
        });
        const d = await r.json();
        if (!d.ok) throw new Error(d.error || '上传失败');
        after(d.name);
        setStatus('asmStatus', '✅ 已导入素材：' + d.name);
      } catch (e) {
        setStatus('asmStatus', '导入失败：' + e.message, true);
      }
    };
    reader.readAsDataURL(f);
  });
}
quickImport('asmFaceUp', 'asmFaceFile', 'faces', name => {
  if (!ASSETS.faces.includes(name)) ASSETS.faces.push(name);
  fillSelect('asmFace', ['', ...ASSETS.faces], ['—— 不使用 ——', ...assetLabels(ASSETS.faces)]);
  asmFaceSel.value = name;
  syncAsmMutex();
  scheduleAsmPreview();
});
// 从当前背景（或成品卡底图）抠出透明主体，存回卡面素材并自动选中为前景层
document.getElementById('asmFaceCut').addEventListener('click', async () => {
  const bg = asmBgSel.value;
  if (!bg && !asmCardSel.value) {
    setStatus('asmStatus', '请先选择背景素材或成品卡', true);
    return;
  }
  const btn = document.getElementById('asmFaceCut');
  btn.disabled = true;
  setStatus('asmStatus', '正在抠取主体…');
  try {
    const r = await fetch('/api/subject/cutout', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ background: bg, card: asmCardSel.value }),
    });
    const d = await r.json();
    if (!d.ok) throw new Error(d.error || '抠图失败');
    if (!ASSETS.faces.includes(d.name)) ASSETS.faces.push(d.name);
    fillSelect('asmFace', ['', ...ASSETS.faces], ['—— 不使用 ——', ...assetLabels(ASSETS.faces)]);
    asmFaceSel.value = d.name;
    asmFaceFgEl.checked = true;   // 抠出来的就是透明图 → 当前景层用
    setStatus('asmStatus', '✅ 已抠取主体：' + d.name);
    scheduleAsmPreview();
  } catch (e) {
    setStatus('asmStatus', '抠图失败：' + e.message, true);
  } finally {
    btn.disabled = false;
  }
});
quickImport('asmFrameUp', 'asmFrameFile', 'frames', name => {
  if (!ASSETS.frames.includes(name)) ASSETS.frames.push(name);
  refillAsmFrame();
  asmFrameSel.value = name;
  syncAsmSealFrameEnabled();
  scheduleAsmPreview();
});
quickImport('asmBackUp', 'asmBackFile', 'backs', name => {
  if (!ASSETS.backs.includes(name)) ASSETS.backs.push(name);
  fillSelect('asmBack', ASSETS.backs, assetLabels(ASSETS.backs));
  document.getElementById('asmBack').value = name;
  scheduleAsmPreview();
});
// ---------- 光栅卡卡面（拼装页）----------
// 图源队列最多 6 张；spec 约定：tmp:<上传文件名> / asset:faces:<素材名>
// 视频/GIF：整段抽成一张图集（1 个采样器），state 放 asmLenti.video，与队列互斥
const asmLentiEl = document.getElementById('asmLenti');
const asmLentiBox = document.getElementById('asmLentiBox');
const asmLentiChips = document.getElementById('asmLentiChips');
const asmLentiAngleEl = document.getElementById('asmLentiAngle');
const asmLentiVFramesEl = document.getElementById('asmLentiVFrames');
const asmLenti = { list: [], video: null };

function asmLentiName(spec) {
  const t = String(spec || '').split(/[\\/:]/).filter(Boolean).pop() || '图片';
  return t.replace(/^[0-9a-f]{8}_/, '');   // 去掉临时上传的前缀哈希
}
// 帧数下拉取值（8~36）；选项外的手工值补一个 option，免得设不上变空
function asmLentiVFramesVal() {
  const n = parseInt(asmLentiVFramesEl.value, 10);
  return isFinite(n) ? n : 24;
}
function asmLentiVFramesSet(n) {
  n = parseInt(n, 10) || 24;
  if (![...asmLentiVFramesEl.options].some(o => +o.value === n)) {
    const o = document.createElement('option');
    o.value = String(n);
    o.textContent = n + ' 帧（自定义）';
    asmLentiVFramesEl.appendChild(o);
  }
  asmLentiVFramesEl.value = String(n);
  asmLentiVFramesLabel();
}
function asmLentiVFramesLabel() {
  const el = document.getElementById('asmLentiVFramesV');
  if (!el) return;
  const n = asmLentiVFramesVal();
  const cols = n <= 4 ? n : (n <= 25 ? 5 : 6);
  el.textContent = cols + '×' + Math.max(1, Math.ceil(n / cols));
  el.title = '图集格子数（只占 1 个纹理单元）';
}
// 没有源视频缓存（旧卡/脚本出的卡只有卡内图集）时锁住帧数下拉：
// 改帧数要回源重抽，这种卡做不到，留着可点会让人以为能改。
function asmLentiVFramesLock(locked) {
  if (!asmLentiVFramesEl) return;
  asmLentiVFramesEl.disabled = !!locked;
  asmLentiVFramesEl.title = locked ? '这张卡只存了图集，没有源视频可重抽；想换帧数请重新上传' : '';
  const v = document.getElementById('asmLentiVFramesV');
  if (v) v.style.opacity = locked ? '0.4' : '';
}
function asmLentiDraw() {
  if (asmLenti.video) {
    const v = asmLenti.video;
    const tags = [];
    if (v.alpha) tags.push('透明');
    if (!v.hash) tags.push('卡内图集');
    asmLentiChips.innerHTML =
      '<span class="lc"><span>🎬 ' + (v.name || '视频') + ' · ' + v.frames + ' 帧（' +
      v.cols + '×' + v.rows + ' 图集' + (tags.length ? ' · ' + tags.join(' · ') : '') +
      '）</span><b id="asmLentiVideoDel" title="移除视频">×</b></span>';
    return;
  }
  if (!asmLenti.list.length) {
    asmLentiChips.innerHTML = '<span class="lc">还没有图源：上传 2~6 张，或「🎬 上传视频 / GIF」整段抽帧</span>';
    return;
  }
  asmLentiChips.innerHTML = asmLenti.list.map((sp, i) =>
    '<span class="lc"><span>' + (i + 1) + '. ' + asmLentiName(sp) + '</span>' +
    '<b data-lx="' + i + '">×</b></span>').join('');
}
function asmLentiActive() {
  return asmLentiEl.checked && (asmLenti.list.length > 0 || !!asmLenti.video);
}
function asmLentiOpts() {
  const num = (id, d) => { const v = parseFloat(document.getElementById(id).value); return isFinite(v) ? v : d; };
  const o = {
    angle: num('asmLentiAngle', 0), lens: num('asmLentiLens', 120),
    stripe: num('asmLentiStripe', 0.48), sheen: num('asmLentiSheen', 0.9),
    soft: num('asmLentiSoft', 0.62), jitter: num('asmLentiJitter', 0.02),
    gain: num('asmLentiGain', 1),
    fade: document.getElementById('asmLentiFade').value || 'stripe',
  };
  if (asmLenti.video) {
    const v = asmLenti.video;
    o.atlas = v.src; o.vname = v.name || '';
    o.atlasCols = v.cols; o.atlasRows = v.rows; o.frames = v.frames;
  }
  return o;
}
function asmLentiAngleLabel() {
  document.getElementById('asmLentiAngleV').textContent = asmLentiAngleEl.value + '°';
}
// 换帧方式的副作用：只有条纹推进用得上「错位」（每条棱纹各自偏移相位），
// 整幅两种是全卡同步，错位设置会失效——不禁用的话用户会以为参数没生效。
function asmLentiFadeSync() {
  const v = document.getElementById('asmLentiFade').value || 'stripe';
  const hint = document.getElementById('asmLentiFadeHint');
  const jit = document.getElementById('asmLentiJitter');
  const txt = {
    stripe: '相位逐条棱纹推进：倾斜时画面一条条推过去，最像实物光栅卡。静态多图用这个。',
    whole: '整张卡同时溶解换帧，没有竖条感。视频/GIF 用这个（柔和调小到 0.2 左右更干净）。',
    cut: '整张卡直接切到下一帧，绝无双影，换帧略顿挫。帧率高时最干净。',
  };
  if (hint) hint.textContent = txt[v] || '';
  if (jit) {
    jit.disabled = (v !== 'stripe');
    jit.style.opacity = jit.disabled ? '0.4' : '';
    jit.title = jit.disabled ? '整幅换帧时全卡同步，错位无效' : '';
  }
}
function asmLentiReset(lc, cardId) {
  lc = lc || {};
  asmLenti.list.length = 0;
  (Array.isArray(lc.images) ? lc.images : []).slice(0, 6).forEach((sp) => asmLenti.list.push(sp));
  // 视频/GIF 卡有两种来历，都要能复原成「视频卡」状态：
  //   1) atlas_src = "lvid:<md5>-f<N>" —— 抽帧缓存还在，改帧数也能重抽
  //   2) 只有 atlas = "lenti/atlas.webp" —— 批量脚本出的卡/旧卡，图集就在卡目录里，
  //      没有源视频可重抽（hash 置空，帧数下拉改成只读提示）
  const fromCard = !lc.atlas_src && String(lc.atlas || '').startsWith('lenti/');
  asmLenti.video = lc.atlas_src ? {
    src: lc.atlas_src,
    // key 形如 "<md5>-f24"：拆出纯哈希，之后改帧数只要传 hash 不用重传视频
    hash: String(lc.atlas_src).split(':').pop().replace(/-f\d+$/, ''),
    name: lc.video_name || '',
    frames: lc.frames || 24,
    cols: lc.atlasCols || 5,
    rows: lc.atlasRows || 5,
    alpha: !!lc.alpha,
  } : (fromCard ? {
    src: lc.atlas, hash: '', name: lc.video_name || cardId || '卡内图集',
    // 脚本出的卡写的是 cols/rows，拼装页存的是 atlasCols/atlasRows —— 两种都认
    frames: lc.frames || 24,
    cols: lc.atlasCols || lc.cols || 5, rows: lc.atlasRows || lc.rows || 5,
    alpha: !!lc.alpha,
  } : null);
  if (asmLenti.video) {
    asmLenti.list.length = 0;
    asmLentiVFramesSet(asmLenti.video.frames);
  }
  const on = asmLenti.list.length > 0 || !!asmLenti.video;
  asmLentiEl.checked = on;
  asmLentiBox.hidden = !on;
  // 参数藏在 opts 里（视频卡顶层还有 atlas/frames/cols/rows），展开后统一读
  const o = (lc.opts && typeof lc.opts === 'object') ? Object.assign({}, lc, lc.opts) : lc;
  if (on) {
    ['fade', 'angle', 'lens', 'stripe', 'sheen', 'soft', 'jitter', 'gain'].forEach((k) => {
      const el = document.getElementById('asmLenti' + k.charAt(0).toUpperCase() + k.slice(1));
      if (el && o[k] !== undefined && o[k] !== null && o[k] !== '') el.value = o[k];
    });
  }
  asmLentiVFramesLock(!!asmLenti.video && !asmLenti.video.hash);
  asmLentiAngleLabel();
  asmLentiFadeSync();
  asmLentiDraw();
}

fillSelect('asmLentiAsset', ASSETS.faces, assetLabels(ASSETS.faces));
asmLentiEl.addEventListener('change', () => {
  asmLentiBox.hidden = !asmLentiEl.checked;
  asmLentiFadeSync();
  if (asmLentiEl.checked && !asmLenti.video && asmLenti.list.length < 2) {
    setStatus('asmStatus', '光栅卡建议 2~6 张图：只有 1 张不会变画；整段视频用「🎬 上传视频 / GIF」');
  }
  scheduleAsmPreview(200);
});
asmLentiChips.addEventListener('click', (e) => {
  if (e.target && e.target.id === 'asmLentiVideoDel') {
    asmLenti.video = null;
    asmLentiDraw();
    scheduleAsmPreview(200);
    return;
  }
  const i = e.target.dataset && e.target.dataset.lx;
  if (i === undefined) return;
  asmLenti.list.splice(+i, 1);
  asmLentiDraw();
  scheduleAsmPreview(200);
});
asmLentiAngleEl.addEventListener('input', () => {
  asmLentiAngleLabel();
  scheduleAsmPreview(260);
});
asmLentiBox.addEventListener('change', (e) => {
  if (e.target && e.target.id === 'asmLentiFade') asmLentiFadeSync();
  if (e.target && e.target.id !== 'asmLentiAngle') scheduleAsmPreview(400);
});
document.getElementById('asmLentiAssetAdd').addEventListener('click', () => {
  const v = document.getElementById('asmLentiAsset').value;
  if (!v) return;
  if (asmLenti.list.length >= 6) { setStatus('asmStatus', '光栅卡最多 6 张图', true); return; }
  asmLenti.list.push('asset:faces:' + v);
  asmLentiEl.checked = true;
  asmLentiBox.hidden = false;
  asmLentiDraw();
  scheduleAsmPreview(250);
});
document.getElementById('asmLentiAdd').addEventListener('click', () => {
  document.getElementById('asmLentiFile').click();
});
document.getElementById('asmLentiFile').addEventListener('change', async (e) => {
  const files = [...(e.target.files || [])];
  e.target.value = '';
  for (const f of files) {
    if (!f.type.startsWith('image/')) continue;
    if (asmLenti.list.length >= 6) { setStatus('asmStatus', '光栅卡最多 6 张图', true); break; }
    const b64 = await new Promise((res) => {
      const fr = new FileReader();
      fr.onload = () => res(String(fr.result).split(',')[1]);
      fr.readAsDataURL(f);
    });
    try {
      const r = await fetch('/api/tmp_upload', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ b64: b64, name: f.name }),
      });
      const d = await r.json();
      if (d.ok) asmLenti.list.push('tmp:' + d.name);
    } catch (err) { /* 单张失败就跳过 */ }
  }
  if (asmLenti.list.length) {
    asmLentiEl.checked = true;
    asmLentiBox.hidden = false;
  }
  asmLentiDraw();
  scheduleAsmPreview(250);
});
document.getElementById('asmLentiClear').addEventListener('click', () => {
  asmLenti.list.length = 0;
  asmLenti.video = null;
  asmLentiDraw();
  scheduleAsmPreview(200);
});
// 视频 / GIF：整段均匀抽 N 帧 → 一张图集（shader 里只占 1 个采样器）
// N 由「视频抽帧数」下拉决定（8~36，默认 24）：帧多换帧更细腻但图集更大
document.getElementById('asmLentiVideoUp').addEventListener('click', () => {
  document.getElementById('asmLentiVideoFile').click();
});
document.getElementById('asmLentiVideoFile').addEventListener('change', async (e) => {
  const f = (e.target.files || [])[0];
  e.target.value = '';
  if (!f) return;
  const btn = document.getElementById('asmLentiVideoUp');
  const label = btn.textContent;
  const want = asmLentiVFramesVal();
  btn.disabled = true;
  btn.textContent = '抽帧中…';
  setStatus('asmStatus', '正在抽 ' + want + ' 帧：' + f.name +
            '（' + (f.size / 1048576).toFixed(1) + ' MB）…');
  try {
    const r = await fetch('/api/lenti/video?frames=' + want, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/octet-stream',
        'X-File-Name': encodeURIComponent(f.name),
      },
      body: f,
    });
    const d = await r.json();
    if (!d.ok) throw new Error(d.error || '处理失败');
    asmLenti.list.length = 0;
    asmLenti.video = {
      src: d.atlas, hash: d.hash || '', name: d.name,
      frames: d.frames, cols: d.atlasCols, rows: d.atlasRows,
      alpha: !!d.alpha,
    };
    asmLentiVFramesLock(false);
    asmLentiEl.checked = true;
    asmLentiBox.hidden = false;
    // 视频天然是「整幅换帧」，默认切到整幅柔和过渡（想要绝对干净可改直接切换）；
    // 顺手把只适合静态光栅卡的默认值换成视频用的（还停在默认值才覆盖，别冲掉手调）
    const fd = document.getElementById('asmLentiFade');
    if (fd.value === 'stripe') fd.value = 'whole';
    const tune = (id, from, to) => {
      const el = document.getElementById(id);
      if (el && String(el.value) === String(from)) el.value = to;
    };
    tune('asmLentiSoft', '0.62', '0.18');     // 帧多时窗口一大就成双影糊
    tune('asmLentiStripe', '0.48', '0.42');
    tune('asmLentiSheen', '0.9', '0.3');      // 视频卡陡角泛白凶，光泽压低
    asmLentiFadeSync();
    asmLentiDraw();
    setStatus('asmStatus', '已抽 ' + d.frames + ' 帧（' + d.atlasCols + '×' + d.atlasRows +
              ' 图集' + (d.alpha ? '，保留透明' : '') + '）：倾斜或拖动卡牌即换帧');
    scheduleAsmPreview(250);
  } catch (err) {
    setStatus('asmStatus', '视频处理失败：' + err.message, true);
  } finally {
    btn.disabled = false;
    btn.textContent = label;
  }
});
// 改帧数：源文件还留在服务端缓存里，按 hash 重抽，不用重新上传几十 MB
asmLentiVFramesEl.addEventListener('change', async () => {
  asmLentiVFramesLabel();
  const v = asmLenti.video;
  const want = asmLentiVFramesVal();
  if (!v || v.frames === want) return;
  if (!v.hash) {
    // 卡内图集（旧卡/脚本出的卡）没有源视频，重抽不了
    setStatus('asmStatus', '这张卡只存了图集、没有源视频，改不了帧数；想换请重新上传一次', true);
    asmLentiVFramesSet(v.frames);
    return;
  }
  const btn = document.getElementById('asmLentiVideoUp');
  const label = btn.textContent;
  btn.disabled = true;
  btn.textContent = '重抽中…';
  setStatus('asmStatus', '按 ' + want + ' 帧重新抽帧…');
  try {
    const r = await fetch('/api/lenti/video/reframe', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ hash: v.hash || v.src, frames: want }),
    });
    const d = await r.json();
    if (!d.ok) throw new Error(d.error || '重抽失败');
    asmLenti.video = {
      src: d.atlas, hash: d.hash || v.hash, name: d.name || v.name,
      frames: d.frames, cols: d.atlasCols, rows: d.atlasRows,
      alpha: !!d.alpha,
    };
    asmLentiDraw();
    setStatus('asmStatus', '已改为 ' + d.frames + ' 帧（' + d.atlasCols + '×' + d.atlasRows + ' 图集）');
    scheduleAsmPreview(250);
  } catch (err) {
    setStatus('asmStatus', '改帧数失败：' + err.message, true);
    asmLentiVFramesSet(v.frames);   // 回退到实际生效的帧数
  } finally {
    btn.disabled = false;
    btn.textContent = label;
  }
});
asmLentiDraw();
asmLentiAngleLabel();
asmLentiVFramesLabel();

quickImport('asmBgUp', 'asmBgFile', 'backgrounds', name => {
  if (!ASSETS.backgrounds.includes(name)) ASSETS.backgrounds.push(name);
  const items = ASSETS.backgrounds.slice(1);
  fillSelect('asmBg', ['', ...items], ['无背景', ...items.map(b => b === 'sample-1.png' ? '默认示例背景（羊皮纸）' : assetLabel(b))]);
  asmBgSel.value = name;
  scheduleAsmPreview();
});

const asmPicker = createEffectPicker(document.getElementById('asmFxPicker'), () => scheduleAsmPreview());

let asmTimer = null;

function getSealStrengths() {
  const parse = (el, dflt) => { const v = parseFloat(el.value); return isFinite(v) ? v : dflt; };
  const clamp = x => Math.min(2, Math.max(0, x));
  return {
    front: clamp(parse(document.getElementById('asmSealFront'), 0.945)),
    back: clamp(parse(document.getElementById('asmSealBack'), 0.5775)),
  };
}

function getSealFrameStrengths() {
  const parse = (el, dflt) => { const v = parseFloat(el.value); return isFinite(v) ? v : dflt; };
  const clamp = x => Math.min(2, Math.max(0, x));
  return {
    front: clamp(parse(document.getElementById('asmSealFrameFront'), 0.8)),
    back: clamp(parse(document.getElementById('asmSealFrameBack'), 0.5)),
  };
}

// 底图优先级：卡面素材 > 背景素材 > 成品卡（后端合成时判定）；
// 背景始终可选——文本型（boxed）时背景与卡面并存（背景作底图），非文本型时卡面优先、背景在未选卡面时生效
function syncAsmMutex() {
  asmBgSel.disabled = false;
}
function effectsToPicker(list) {
  // 成品卡存储格式 [{name, pos:{type,x,y}}] → 特效选择器 state [{name,posType,posX,posY}]
  return (list || []).map(it => {
    const p = it.pos || {};
    if (p.type === 'custom') {
      const xv = p.x != null ? parseFloat(p.x) : 0.5;
      const yv = p.y != null ? parseFloat(p.y) : 0.5;
      const x = Math.min(100, Math.max(0, Math.round((isFinite(xv) ? xv : 0.5) * 100)));
      const y = Math.min(100, Math.max(0, Math.round((1 - (isFinite(yv) ? yv : 0.5)) * 100)));
      return { name: it.name, posType: 'custom', posX: x, posY: y };
    }
    return { name: it.name, posType: p.type || 'random' };
  });
}
asmCardSel.addEventListener('change', () => {
  const card = CARDS.find(c => c.id === asmCardSel.value);
  if (card) {
    // 把成品卡的完整配置填充到左侧控件：所见即所得，可任意更换/移除部件后保存
    // 卡面/背景回填成品卡实际使用的素材（无则留空，底图以成品卡自身为准）
    asmFaceSel.value = (card.face && [...asmFaceSel.options].some(o => o.value === card.face)) ? card.face : '';
    asmFaceFgEl.checked = !!card.face_as_fg;
    asmBgSel.value = (card.background && [...asmBgSel.options].some(o => o.value === card.background)) ? card.background : '';
    document.getElementById('asmName').value = card.name || '';
    document.getElementById('asmDesc').value = card.description || '';
    if (card.text_type && [...asmTextTypeSel.options].some(o => o.value === card.text_type)) {
      asmTextTypeSel.value = card.text_type;
    }
    refillAsmFrame();
    // 边框：按卡牌类型补全选项后选中成品卡的边框（改选"不使用"即可移除）
    if (card.frame) {
      if (![...asmFrameSel.options].some(o => o.value === card.frame) && ASSETS.frames_text.includes(card.frame)) {
        asmTextTypeSel.value = 'boxed';
        refillAsmFrame();
      }
      if ([...asmFrameSel.options].some(o => o.value === card.frame)) {
        asmFrameSel.value = card.frame;
      } else {
        asmFrameSel.value = '';
      }
    } else {
      asmFrameSel.value = '';
    }
    // 卡封：直接选中成品卡的卡封（改选"不使用"即可移除）
    asmSealSel.value = (card.seal && [...asmSealSel.options].some(o => o.value === card.seal)) ? card.seal : '';
    if (window.asmSealExtra) asmSealExtra.set(card.seal_extra || []);
    // 边框卡封：记录过"与卡封一致"→ 同素材；有显式素材 → 用素材；只用了边框 → 默认"与卡封一致"
    asmSealFrameTouched = false;
    if (card.seal_frame_same) {
      asmSealFrameSel.value = 'same';
    } else if (card.seal_frame && [...asmSealFrameSel.options].some(o => o.value === card.seal_frame)) {
      asmSealFrameSel.value = card.seal_frame;
    } else {
      asmSealFrameSel.value = card.frame ? 'same' : '';
    }
    const backSel = document.getElementById('asmBack');
    if (card.back && [...backSel.options].some(o => o.value === card.back)) backSel.value = card.back;
    // 光栅卡：这张卡是光栅卡就把图源与参数一起回填（不是就清空）
    const lc = card.lenti || null;
    asmLentiReset(lc, card.id);
    // 视差主体：这张卡是「多层主体」做的就选中它（不在列表里说明主体已被删，清空）
    if (card.parallax && [...asmParallaxSel.options].some(o => o.value === card.parallax)) {
      asmParallaxSel.value = card.parallax;
    } else {
      asmParallaxSel.value = '';
    }
    if (typeof card.seal_strength_front === 'number' && isFinite(card.seal_strength_front)) document.getElementById('asmSealFront').value = card.seal_strength_front;
    if (typeof card.seal_strength_back === 'number' && isFinite(card.seal_strength_back)) document.getElementById('asmSealBack').value = card.seal_strength_back;
    if (typeof card.seal_frame_strength_front === 'number' && isFinite(card.seal_frame_strength_front)) document.getElementById('asmSealFrameFront').value = card.seal_frame_strength_front;
    if (typeof card.seal_frame_strength_back === 'number' && isFinite(card.seal_frame_strength_back)) document.getElementById('asmSealFrameBack').value = card.seal_frame_strength_back;
    // 辉光：旧卡未记录 → 默认辉光；记录 "none" → 不使用
    const glowSel = document.getElementById('asmGlow');
    const defGlow = ASSETS.glows.find(x => x) || '';
    if (card.glow === 'none') glowSel.value = '';
    else if (card.glow && [...glowSel.options].some(o => o.value === card.glow)) glowSel.value = card.glow;
    else glowSel.value = defGlow;
    const gs = parseFloat(card.glow_strength);
    document.getElementById('asmGlowStrength').value = (isFinite(gs) && gs >= 0 && gs <= 2) ? gs : 1;
    document.getElementById('asmFloatFg').checked = !!card.subject_over_frame;
    document.getElementById('asmOutline').checked = !!card.subject_outline;
    document.getElementById('asmMaskClip').checked = card.mask_clip !== false;
    const asmScaleEl = document.getElementById('asmScale');
    const sc = parseFloat(card.scale);
    if (isFinite(sc) && sc >= 0.5 && sc <= 1.5) asmScaleEl.value = sc;
    else asmScaleEl.value = 1;
    const bgScEl = document.getElementById('asmBgScale');
    const bgSc = parseFloat(card.bg_scale);
    if (isFinite(bgSc) && bgSc >= 0.5 && bgSc <= 1.5) bgScEl.value = bgSc;
    else bgScEl.value = 1;
    const frScEl = document.getElementById('asmFrameScale');
    const frSc = parseFloat(card.frame_scale);
    if (isFinite(frSc) && frSc >= 0.5 && frSc <= 1.5) frScEl.value = frSc;
    else frScEl.value = 1;
    const asmOffsetXEl = document.getElementById('asmOffsetX');
    const asmOffsetYEl = document.getElementById('asmOffsetY');
    const ox = parseFloat(card.subject_offset_x);
    const oy = parseFloat(card.subject_offset_y);
    asmOffsetXEl.value = (isFinite(ox) && ox >= -0.9 && ox <= 0.9) ? ox : 0;
    asmOffsetYEl.value = (isFinite(oy) && oy >= -0.9 && oy <= 0.9) ? oy : 0;
    if (card.text_pos) {
      const vals = [card.text_pos.x1, card.text_pos.x2, card.text_pos.y1, card.text_pos.y2];
      ['asmAreaL', 'asmAreaR', 'asmAreaT', 'asmAreaB'].forEach((id, idx) => {
        const el = document.getElementById(id);
        if (el && isFinite(vals[idx])) el.value = Math.round(vals[idx] * 100);
      });
    }
    // 特效：加载成品卡的特效到选择器
    asmPicker.set(effectsToPicker(card.effects));
  } else {
    asmFaceSel.value = '';
    asmFaceFgEl.checked = false;
    asmBgSel.value = '';
  }
  syncAsmSealFrameEnabled();
  syncAsmMutex();
  scheduleAsmPreview();
});
asmFaceSel.addEventListener('change', () => { syncAsmMutex(); scheduleAsmPreview(); });
asmFaceFgEl.addEventListener('change', scheduleAsmPreview);
asmFrameSel.addEventListener('change', () => {
  syncAsmSealFrameEnabled();
  // 使用边框时默认边框卡封"与卡封一致"（用户手动选过边框卡封则不再覆盖）
  if (asmFrameSel.value && !asmSealFrameTouched && asmSealFrameSel.value === '') {
    asmSealFrameSel.value = 'same';
  }
  scheduleAsmPreview();
});
asmBgSel.addEventListener('change', scheduleAsmPreview);
document.getElementById('asmBack').addEventListener('change', scheduleAsmPreview);
asmSealSel.addEventListener('change', scheduleAsmPreview);
asmSealFrameSel.addEventListener('change', () => { asmSealFrameTouched = true; scheduleAsmPreview(); });
document.getElementById('asmGlow').addEventListener('change', scheduleAsmPreview);
document.getElementById('asmGlowStrength').addEventListener('input', scheduleAsmPreview);
document.getElementById('asmFloatFg').addEventListener('change', scheduleAsmPreview);
document.getElementById('asmOutline').addEventListener('change', scheduleAsmPreview);
document.getElementById('asmMaskClip').addEventListener('change', scheduleAsmPreview);
document.getElementById('asmSealFront').addEventListener('input', () => { scheduleAsmPreview(); persistSealStrengths(); });
document.getElementById('asmSealBack').addEventListener('input', () => { scheduleAsmPreview(); persistSealStrengths(); });
document.getElementById('asmSealFrameFront').addEventListener('input', () => { scheduleAsmPreview(); persistSealStrengths(); });
document.getElementById('asmSealFrameBack').addEventListener('input', () => { scheduleAsmPreview(); persistSealStrengths(); });
asmTextTypeSel.addEventListener('change', () => { refillAsmFrame(); scheduleAsmPreview(); });
document.getElementById('asmDesc').addEventListener('input', scheduleAsmPreview);
// 描述区域四边：改动即刷新预览
['asmAreaL', 'asmAreaR', 'asmAreaT', 'asmAreaB'].forEach(id => {
  document.getElementById(id).addEventListener('input', scheduleAsmPreview);
});
// 缩放：卡面 / 背景 / 边框三个倍率各自独立，调整时实时刷新 3D 预览
['asmScale', 'asmBgScale', 'asmFrameScale'].forEach(id => {
  document.getElementById(id).addEventListener('input', scheduleAsmPreview);
});
// 主体偏移：调整时实时刷新 3D 预览（仅偏移图像位置，渲染范围不跟随）
document.getElementById('asmOffsetX').addEventListener('input', scheduleAsmPreview);
document.getElementById('asmOffsetY').addEventListener('input', scheduleAsmPreview);
syncAsmMutex();

// 预览模式：仅 3D 卡牌（含粒子特效）
const asmMode = '3d';

// 重置为空卡：清空所有部件选择，从零开始搭建
document.getElementById('asmReset').addEventListener('click', () => {
  asmCardSel.value = '';
  asmFaceSel.value = '';
  asmBgSel.value = '';
  asmFrameSel.value = '';
  asmSealSel.value = '';
  asmSealFrameSel.value = '';
  asmSealFrameTouched = false;
  document.getElementById('asmBack').selectedIndex = 0;
  document.getElementById('asmSealFront').value = 0.945;
  document.getElementById('asmSealBack').value = 0.5775;
  document.getElementById('asmSealFrameFront').value = 0.8;
  document.getElementById('asmSealFrameBack').value = 0.5;
  document.getElementById('asmFloatFg').checked = false;
  document.getElementById('asmOutline').checked = false;
  document.getElementById('asmScale').value = 1;
  document.getElementById('asmBgScale').value = 1;
  document.getElementById('asmFrameScale').value = 1;
  asmFaceFgEl.checked = false;
  document.getElementById('asmOffsetX').value = 0;
  document.getElementById('asmOffsetY').value = 0;
  asmTextTypeSel.value = 'none';
  asmParallaxSel.value = '';
  refillAsmFrame();
  document.getElementById('asmDesc').value = '';
  [['asmAreaL', 8], ['asmAreaR', 92], ['asmAreaT', 70], ['asmAreaB', 92]].forEach(([id, v]) => {
    document.getElementById(id).value = v;
  });
  asmPicker.clear();
  document.getElementById('asmGlow').value = ASSETS.glows.find(x => x) || '';
  document.getElementById('asmGlowStrength').value = 1;
  document.getElementById('asmName').value = '';
  asmLentiReset(null);
  syncAsmMutex();
  setStatus('asmStatus', '已重置为空卡，可自由搭建');
  scheduleAsmPreview(120);
});

let sealSettingsTimer = null;
function persistSealStrengths() {
  clearTimeout(sealSettingsTimer);
  sealSettingsTimer = setTimeout(async () => {
    try {
      await fetch('/api/settings', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          seal_strength_front: getSealStrengths().front,
          seal_strength_back: getSealStrengths().back,
          seal_frame_strength_front: getSealFrameStrengths().front,
          seal_frame_strength_back: getSealFrameStrengths().back,
        }),
      });
    } catch (e) { /* 忽略持久化失败 */ }
  }, 350);
}
(async () => {
  try {
    const r = await fetch('/api/settings');
    const d = await r.json();
    if (d.ok && d.settings) {
      if (typeof d.settings.seal_strength_front === 'number') {
        document.getElementById('asmSealFront').value = d.settings.seal_strength_front;
      }
      if (typeof d.settings.seal_strength_back === 'number') {
        document.getElementById('asmSealBack').value = d.settings.seal_strength_back;
      }
      if (typeof d.settings.seal_frame_strength_front === 'number') {
        document.getElementById('asmSealFrameFront').value = d.settings.seal_frame_strength_front;
      }
      if (typeof d.settings.seal_frame_strength_back === 'number') {
        document.getElementById('asmSealFrameBack').value = d.settings.seal_frame_strength_back;
      }
    }
  } catch (e) { /* 读取失败用默认值 */ }
})();

function scheduleAsmPreview(delay) {
  clearTimeout(asmTimer);
  asmTimer = setTimeout(postAsmPreview, delay == null ? 450 : delay);
}
async function postAsmPreview() {
  try {
    const r = await fetch('/api/preview_html', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        kind: 'assembly',
        mode: asmMode,
        name: document.getElementById('asmName').value.trim() || null,
        card: asmCardSel.value,
        face: asmFaceSel.value,
        face_as_fg: asmFaceFgEl.checked,
        parallax: asmParallaxSel.value,
        lenti_images: asmLentiActive() ? asmLenti.list.slice(0, 6) : [],
        lenti: asmLentiOpts(),
        frame: asmFrameSel.value,
        custom_bg: asmBgSel.value,
        back: document.getElementById('asmBack').value,
        seal: asmSealSel.value,
        seal_extra: asmSealExtra.get(),
        seal_frame: asmSealFrameSel.value,
        effects: asmPicker.get(),
        subject_over_frame: document.getElementById('asmFloatFg').checked,
        subject_outline: document.getElementById('asmOutline').checked,
        mask_clip: document.getElementById('asmMaskClip').checked,
        scale: parseFloat(document.getElementById('asmScale').value) || 1,
        bg_scale: parseFloat(document.getElementById('asmBgScale').value) || 1,
        frame_scale: parseFloat(document.getElementById('asmFrameScale').value) || 1,
        subject_offset_x: parseFloat(document.getElementById('asmOffsetX').value) || 0,
        subject_offset_y: parseFloat(document.getElementById('asmOffsetY').value) || 0,
        seal_strength_front: getSealStrengths().front,
        seal_strength_back: getSealStrengths().back,
        seal_frame_strength_front: getSealFrameStrengths().front,
        seal_frame_strength_back: getSealFrameStrengths().back,
        glow: document.getElementById('asmGlow').value,
        glow_strength: parseFloat(document.getElementById('asmGlowStrength').value) || 1,
        desc: document.getElementById('asmDesc').value.trim() || null,
        text_type: asmTextTypeSel.value,
        text_pos: (function () {
          const ids = ['asmAreaL', 'asmAreaR', 'asmAreaT', 'asmAreaB'];
          const v = ids.map(id => parseFloat(document.getElementById(id).value));
          if (v.some(x => !isFinite(x))) return null;
          const clamp = x => Math.min(1, Math.max(0, x));
          return { x1: clamp(v[0] / 100), x2: clamp(v[1] / 100), y1: clamp(v[2] / 100), y2: clamp(v[3] / 100) };
        })(),
      }),
    });
    const d = await r.json();
    if (!d.ok) throw new Error(d.error);
    document.getElementById('asmPreviewFrame').src = d.url + '?t=' + Date.now();
  } catch (e) {
    setStatus('asmStatus', '预览失败：' + e.message, true);
  }
}

document.getElementById('asmSave').addEventListener('click', async () => {
  const name = document.getElementById('asmName').value.trim();
  if (!name) { setStatus('asmStatus', '请先填写拼装名称', true); return; }
  try {
    const r = await fetch('/api/assembly/save', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        name,
        card: asmCardSel.value,
        face: asmFaceSel.value,
        face_as_fg: asmFaceFgEl.checked,
        parallax: asmParallaxSel.value,
        lenti_images: asmLentiActive() ? asmLenti.list.slice(0, 6) : [],
        lenti: asmLentiOpts(),
        frame: asmFrameSel.value,
        custom_bg: asmBgSel.value,
        back: document.getElementById('asmBack').value,
        seal: asmSealSel.value,
        seal_extra: asmSealExtra.get(),
        seal_frame: asmSealFrameSel.value,
        desc: document.getElementById('asmDesc').value.trim() || null,
        text_type: asmTextTypeSel.value,
        text_pos: (function () {
          const ids = ['asmAreaL', 'asmAreaR', 'asmAreaT', 'asmAreaB'];
          const v = ids.map(id => parseFloat(document.getElementById(id).value));
          if (v.some(x => !isFinite(x))) return null;
          const clamp = x => Math.min(1, Math.max(0, x));
          return { x1: clamp(v[0] / 100), x2: clamp(v[1] / 100), y1: clamp(v[2] / 100), y2: clamp(v[3] / 100) };
        })(),
        effects: asmPicker.get(),
        subject_over_frame: document.getElementById('asmFloatFg').checked,
        subject_outline: document.getElementById('asmOutline').checked,
        mask_clip: document.getElementById('asmMaskClip').checked,
        scale: parseFloat(document.getElementById('asmScale').value) || 1,
        bg_scale: parseFloat(document.getElementById('asmBgScale').value) || 1,
        frame_scale: parseFloat(document.getElementById('asmFrameScale').value) || 1,
        subject_offset_x: parseFloat(document.getElementById('asmOffsetX').value) || 0,
        subject_offset_y: parseFloat(document.getElementById('asmOffsetY').value) || 0,
        seal_strength_front: getSealStrengths().front,
        seal_strength_back: getSealStrengths().back,
        seal_frame_strength_front: getSealFrameStrengths().front,
        seal_frame_strength_back: getSealFrameStrengths().back,
        glow: document.getElementById('asmGlow').value,
        glow_strength: parseFloat(document.getElementById('asmGlowStrength').value) || 1,
      }),
    });
    const d = await r.json();
    if (!d.ok) throw new Error(d.error);
    setStatus('asmStatus', '✅ 已保存：' + d.webcard);
    persistSealStrengths();
    // 刷新成品列表：新卡/被编辑的卡立即出现在下拉中，方便继续编辑
    try {
      const rc = await fetch('/api/cards?scope=album');
      const dc = await rc.json();
      if (dc.ok) {
        CARDS.length = 0;
        CARDS.push(...(dc.cards || []));
        const cur = asmCardSel.value;
        fillSelect('asmCard', ['', ...CARDS.map(c => c.id)], ['—— 不使用 ——', ...CARDS.map(c => c.name + '（' + c.id + '）')]);
        if (cur && [...asmCardSel.options].some(o => o.value === cur)) asmCardSel.value = cur;
        syncAsmMutex();
      }
    } catch (e) { /* 刷新列表失败不影响保存结果 */ }
  } catch (e) {
    setStatus('asmStatus', '保存失败：' + e.message, true);
  }
});

// 拼装页导出：优先导出当前选中的成品卡；未选则用名称对应的成品卡（需已保存过）
function exportAssembly(type) {
  let id = asmCardSel.value;
  if (!id) id = document.getElementById('asmName').value.trim();
  if (!id) { setStatus('asmStatus', '请先保存为卡牌或选择一张成品卡再导出', true); return; }
  const a = document.createElement('a');
  a.href = '/api/export?id=' + encodeURIComponent(id) + '&type=' + type;
  a.download = '';
  document.body.appendChild(a);
  a.click();
  a.remove();
}
document.getElementById('asmExportImg').addEventListener('click', () => exportAssembly('image'));
document.getElementById('asmExportHtml').addEventListener('click', () => exportAssembly('html'));

scheduleAsmPreview(600);

// ================= 4 素材管理 =================
const ASSET_TYPES = ['backgrounds', 'faces', 'frames', 'frames-text', 'seals', 'backs', 'effects-images', 'effects', 'frame-effects'];
const NON_IMAGE_TYPES = ['effects', 'frame-effects'];
let currentAssetType = 'backgrounds';

function syncAssetUploadBtn() {
  const btn = document.getElementById('assetUploadBtn');
  btn.style.display = NON_IMAGE_TYPES.includes(currentAssetType) ? 'none' : '';
}

function renderAssetTypes() {
  document.querySelectorAll('.typechip').forEach(c => {
    c.classList.toggle('on', c.dataset.type === currentAssetType);
  });
}
let assetPackFilter = '';   // '' = 全部；'__builtin__' = 仅内置；否则为素材包 id

function renderAssetPackFilter() {
  const sel = document.getElementById('assetPackFilter');
  if (!sel) return;
  const prev = assetPackFilter;
  sel.innerHTML = '';
  const opts = [['', '全部来源'], ['__builtin__', '仅内置']];
  PACKS.forEach(p => { if (!p.error) opts.push([p.id, '📦 ' + p.name]); });
  opts.forEach(([v, t]) => { const o = document.createElement('option'); o.value = v; o.textContent = t; sel.appendChild(o); });
  sel.value = opts.some(o => o[0] === prev) ? prev : '';
  assetPackFilter = sel.value;
  sel.onchange = () => { assetPackFilter = sel.value; loadAssets(); };
}

function assetMatchFilter(it) {
  if (NON_IMAGE_TYPES.includes(currentAssetType)) return true;
  if (!assetPackFilter) return true;
  if (assetPackFilter === '__builtin__') return !!it.builtin;
  return it.pack === assetPackFilter;
}

async function loadAssets() {
  const grid = document.getElementById('assetGrid');
  grid.innerHTML = '<div class="asset-empty">加载中…</div>';
  const r = await fetch('/api/assets?type=' + encodeURIComponent(currentAssetType));
  const d = await r.json();
  if (!d.ok) { grid.innerHTML = ''; setStatus('assetStatus', '加载失败：' + d.error, true); return; }
  setStatus('assetStatus', '');
  grid.innerHTML = '';
  if (!d.files.length) {
    grid.innerHTML = '<div class="asset-empty">这个分类还没有素材，点右上角「上传素材」添加</div>';
    return;
  }
  // 特效/边框特效：无缩略图，用图标块展示；仅支持删除（内置同步删源码定义）
  if (NON_IMAGE_TYPES.includes(currentAssetType)) {
    const label = currentAssetType === 'effects' ? '特效' : '边框特效';
    const emoji = currentAssetType === 'effects' ? '🎆' : '💡';
    d.files.forEach(it => {
      const tile = document.createElement('div');
      tile.className = 'asset-tile';
      tile.innerHTML =
        '<div class="asset-emoji">' + emoji + '</div>' +
        '<div class="aname">' + it.name + (it.builtin ? '（内置）' : '') + '</div>' +
        '<div class="arow"><button class="btn small danger" data-act="del">🗑️</button></div>';
      tile.querySelector('[data-act=del]').addEventListener('click', async () => {
        const msg = it.builtin
          ? '删除' + label + '「' + it.name + '」？\n内置' + label + '将同步删除其源码定义（删除后需重新生成/重新上传才能恢复）。'
          : '删除' + label + '「' + it.name + '」？';
        if (!confirm(msg)) return;
        try {
          const r = await fetch('/api/assets/delete', {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ type: currentAssetType, name: it.name }),
          });
          const res = await r.json();
          if (!res.ok) throw new Error(res.error);
          if (res.removed_builtin) setStatus('assetStatus', '✅ 已删除：' + res.name + '（内置定义已同步移除）');
          else setStatus('assetStatus', '✅ 已删除：' + res.name);
          await loadAssets();
        } catch (e) {
          setStatus('assetStatus', '删除失败：' + e.message, true);
        }
      });
      grid.appendChild(tile);
    });
    return;
  }
  const items = d.files.filter(assetMatchFilter);
  if (!items.length) {
    grid.innerHTML = '<div class="asset-empty">这个分类没有符合条件的素材（可在上方切换来源筛选）</div>';
    return;
  }
  items.forEach(it => {
    const name = it.name;
    const isPack = !it.builtin;
    const tile = document.createElement('div');
    tile.className = 'asset-tile';
    const badge = isPack
      ? '<div style="font-size:11px;color:var(--gold2);margin-top:2px">📦 ' + it.pack_name + '</div>'
      : '';
    const actions = isPack
      ? '<div style="font-size:11px;color:var(--muted)">素材包提供 · 只读</div>'
      : '<button class="btn small ghost" data-act="rename">✏️ 重命名</button>' +
        '<button class="btn small danger" data-act="del">🗑️</button>';
    tile.innerHTML =
      '<img src="' + assetUrl(currentAssetType, it.key) + '?t=' + Date.now() + '" alt="">' +
      '<div class="aname">' + name + '</div>' + badge +
      '<div class="arow">' + actions + '</div>';
    const rb = tile.querySelector('[data-act=rename]');
    if (rb) rb.addEventListener('click', async () => {
      const neu = prompt('新名称（可含中文）：', name);
      if (!neu || neu === name) return;
      try {
        const r = await fetch('/api/assets/rename', {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ type: currentAssetType, old: name, new: neu }),
        });
        const res = await r.json();
        if (!res.ok) throw new Error(res.error);
        await loadAssets();
      } catch (e) {
        setStatus('assetStatus', '重命名失败：' + e.message, true);
      }
    });
    const db = tile.querySelector('[data-act=del]');
    if (db) db.addEventListener('click', async () => {
      const delMsg = currentAssetType === 'seals'
        ? '删除卡封「' + name + '」？\n内置卡封将同步删除其 3D 效果定义（删除后需重新生成/重新上传才能恢复）。'
        : '删除素材「' + name + '」？';
      if (!confirm(delMsg)) return;
      try {
        const r = await fetch('/api/assets/delete', {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ type: currentAssetType, name }),
        });
        const res = await r.json();
        if (!res.ok) throw new Error(res.error);
        if (res.removed_builtin) setStatus('assetStatus', '✅ 已删除：' + res.name + '（内置卡封定义已同步移除）');
        await loadAssets();
      } catch (e) {
        setStatus('assetStatus', '删除失败：' + e.message, true);
      }
    });
    grid.appendChild(tile);
  });
}
document.querySelectorAll('.typechip').forEach(c => {
  c.addEventListener('click', () => { currentAssetType = c.dataset.type; renderAssetTypes(); syncAssetUploadBtn(); loadAssets(); });
});

const assetFileInput = document.getElementById('assetFile');
document.getElementById('assetUploadBtn').addEventListener('click', () => assetFileInput.click());
assetFileInput.addEventListener('change', async () => {
  if (!assetFileInput.files.length) return;
  const f = assetFileInput.files[0];
  if (!f.type.startsWith('image/')) { setStatus('assetStatus', '请选择图片文件', true); return; }
  const reader = new FileReader();
  reader.onload = async () => {
    try {
      const r = await fetch('/api/assets/upload', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ type: currentAssetType, b64: reader.result.split(',')[1], name: f.name }),
      });
      const d = await r.json();
      if (!d.ok) throw new Error(d.error);
      setStatus('assetStatus', '✅ 已上传：' + d.name);
      if (currentAssetType === 'effects-images') {
        ASSETS.effect_images.push(d.name);
        const sel = document.getElementById('fxImageFile');
        const op = document.createElement('option');
        op.value = d.name; op.textContent = d.name;
        sel.appendChild(op);
      }
      await loadAssets();
    } catch (e) {
      setStatus('assetStatus', '上传失败：' + e.message, true);
    }
  };
  reader.readAsDataURL(f);
});

// ================= 5 素材包管理 =================
const TYPE_LABELS = { backgrounds: '背景', faces: '卡面/主体', frames: '边框', seals: '卡封', backs: '卡背' };

function packCountText(p) {
  const parts = Object.keys(p.counts || {}).map(t => (TYPE_LABELS[t] || t) + ' ' + p.counts[t]);
  return parts.length ? parts.join(' · ') : '未识别到素材';
}

async function loadPacks() {
  const grid = document.getElementById('packList');
  grid.innerHTML = '<div class="asset-empty">加载中…</div>';
  let d;
  try {
    const r = await fetch('/api/packs');
    d = await r.json();
  } catch (e) {
    grid.innerHTML = ''; setStatus('packStatus', '加载失败：' + e.message, true); return;
  }
  if (!d.ok) { grid.innerHTML = ''; setStatus('packStatus', '加载失败：' + d.error, true); return; }
  PACKS = d.packs;
  setStatus('packStatus', '');
  renderAssetPackFilter();
  grid.innerHTML = '';
  if (!d.packs.length) {
    grid.innerHTML = '<div class="asset-empty">还没有素材包，点上方「导入压缩包 / 导入文件夹」添加</div>';
    return;
  }
  d.packs.forEach(p => {
    const tile = document.createElement('div');
    tile.className = 'asset-tile';
    const thumb = p.thumbnail
      ? '<img src="/packs_abs/' + encodeURIComponent(p.id) + '/' + p.thumbnail.split('/').map(encodeURIComponent).join('/') + '?t=' + Date.now() + '" alt="">'
      : '<div class="asset-emoji">📦</div>';
    if (p.error) {
      tile.innerHTML = thumb +
        '<div class="aname">' + p.id + '</div>' +
        '<div style="font-size:11px;color:var(--err)">⚠️ 索引异常</div>' +
        '<div style="font-size:12px;color:var(--err);margin-top:4px">' + p.error + '</div>' +
        '<div class="arow"><button class="btn small danger" data-act="del">🗑️ 删除</button></div>';
    } else {
      const desc = p.description ? '<div style="font-size:12px;color:var(--muted);margin-top:4px">' + p.description + '</div>' : '';
      const author = p.author ? ' · ' + p.author : '';
      tile.innerHTML = thumb +
        '<div class="aname">' + p.name + '</div>' +
        '<div style="font-size:11px;color:var(--muted)">id: ' + p.id + author + '</div>' +
        desc +
        '<div style="font-size:12px;color:var(--gold2);margin-top:4px">' + packCountText(p) + '</div>' +
        '<div class="arow">' +
        '<button class="btn small ' + (p.enabled ? '' : 'ghost') + '" data-act="toggle">' + (p.enabled ? '✅ 已启用' : '⏸️ 已禁用') + '</button>' +
        '<button class="btn small danger" data-act="del">🗑️</button>' +
        '</div>';
    }
    const tb = tile.querySelector('[data-act=toggle]');
    if (tb) tb.addEventListener('click', async () => {
      try {
        const r = await fetch('/api/packs/toggle', {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ id: p.id, enabled: !p.enabled }),
        });
        const res = await r.json();
        if (!res.ok) throw new Error(res.error);
        setStatus('packStatus', (res.enabled ? '✅ 已启用：' : '⏸️ 已禁用：') + p.name + '（刷新页面后下拉列表生效）');
        await loadPacks();
      } catch (e) { setStatus('packStatus', '操作失败：' + e.message, true); }
    });
    tile.querySelector('[data-act=del]').addEventListener('click', async () => {
      if (!confirm('删除素材包「' + (p.name || p.id) + '」？\n只会删除工具 packs/ 里复制进来的这份，不影响你的原始文件夹和内置素材。')) return;
      try {
        const r = await fetch('/api/packs/delete', {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ id: p.id }),
        });
        const res = await r.json();
        if (!res.ok) throw new Error(res.error);
        setStatus('packStatus', '✅ 已删除：' + (p.name || p.id) + '（刷新页面后下拉列表生效）');
        await loadPacks();
      } catch (e) { setStatus('packStatus', '删除失败：' + e.message, true); }
    });
    grid.appendChild(tile);
  });
}

// 导入压缩包
const packZipInput = document.getElementById('packZipFile');
document.getElementById('packImportZipBtn').addEventListener('click', () => packZipInput.click());
packZipInput.addEventListener('change', () => {
  const f = packZipInput.files[0];
  if (!f) return;
  setStatus('packStatus', '正在导入压缩包：' + f.name + ' …');
  const reader = new FileReader();
  reader.onload = async () => {
    try {
      const r = await fetch('/api/packs/import', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ name: f.name, b64: reader.result.split(',')[1] }),
      });
      const d = await r.json();
      if (!d.ok) throw new Error(d.error);
      setStatus('packStatus', '✅ 已导入：' + d.name + '（刷新页面后下拉列表生效）');
      await loadPacks();
    } catch (e) { setStatus('packStatus', '导入失败：' + e.message, true); }
    packZipInput.value = '';
  };
  reader.readAsDataURL(f);
});

// 导入文件夹（webkitdirectory 逐文件上传）
const packDirInput = document.getElementById('packDirFile');
document.getElementById('packImportDirBtn').addEventListener('click', () => packDirInput.click());
packDirInput.addEventListener('change', () => {
  const files = [...packDirInput.files];
  if (!files.length) return;
  setStatus('packStatus', '正在导入文件夹（' + files.length + ' 个文件）…');
  const out = [];
  let pending = files.length;
  async function submitFolder() {
    try {
      const r = await fetch('/api/packs/import_folder', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ files: out }),
      });
      const d = await r.json();
      if (!d.ok) throw new Error(d.error);
      setStatus('packStatus', '✅ 已导入：' + d.name + '（刷新页面后下拉列表生效）');
      await loadPacks();
    } catch (e) { setStatus('packStatus', '导入失败：' + e.message, true); }
    packDirInput.value = '';
  }
  files.forEach(f => {
    const rel = f.webkitRelativePath || f.name;
    const reader = new FileReader();
    reader.onload = () => {
      out.push({ path: rel, b64: reader.result.split(',')[1] });
      if (--pending === 0) submitFolder();
    };
    reader.onerror = () => { if (--pending === 0) submitFolder(); };
    reader.readAsDataURL(f);
  });
});

document.getElementById('packRefreshBtn').addEventListener('click', loadPacks);

renderAssetPackFilter();
syncAssetUploadBtn();
loadAssets();
loadPacks();
</script>
</body>
</html>
"""


CUTOUT_HTML = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>✂️ 抠图工具</title>
<style>
  :root { --bg:#171310; --panel:#221b16; --panel2:#2a211a; --line:#3d2f24; --gold:#e3b95c; --gold2:#f3d48a; --text:#ece4d6; --muted:#a99b86; --err:#e07a5f; }
  * { box-sizing:border-box; margin:0; padding:0; }
  body { background:radial-gradient(1200px 600px at 50% -10%, #2a2018 0%, var(--bg) 60%); color:var(--text); font-family:"Microsoft YaHei","PingFang SC",system-ui,sans-serif; min-height:100vh; padding:28px 16px 60px; }
  .wrap { max-width:1100px; margin:0 auto; }
  header { text-align:center; margin-bottom:20px; }
  header h1 { font-size:28px; letter-spacing:2px; color:var(--gold2); }
  header p { color:var(--muted); margin-top:6px; font-size:14px; }
  .back { display:inline-block; margin-top:10px; color:var(--muted); font-size:13px; text-decoration:none; }
  .back:hover { color:var(--gold2); }
  .grid { display:grid; grid-template-columns:380px 1fr; gap:16px; align-items:start; }
  @media (max-width:860px){ .grid { grid-template-columns:1fr; } }
  .panel { background:var(--panel); border:1px solid var(--line); border-radius:16px; padding:18px; margin-bottom:16px; }
  .panel h2 { font-size:14px; color:var(--gold); margin-bottom:12px; font-weight:600; }
  .drop { border:2px dashed var(--line); border-radius:12px; padding:28px 14px; text-align:center; cursor:pointer; transition:border-color .2s,background .2s; background:var(--panel2); }
  .drop:hover, .drop.drag { border-color:var(--gold); background:#32281c; }
  .drop .big { font-size:38px; }
  .drop p { color:var(--muted); margin-top:8px; font-size:13px; }
  .drop p.main { color:var(--text); font-size:14px; margin-top:12px; }
  .thumb { margin-top:12px; display:flex; align-items:center; gap:12px; }
  .thumb img { width:64px; height:84px; object-fit:cover; border-radius:8px; border:1px solid var(--line); }
  .thumb span { color:var(--muted); font-size:12px; word-break:break-all; }
  label { display:block; font-size:13px; color:var(--muted); margin-bottom:6px; }
  select { width:100%; background:var(--panel2); color:var(--text); border:1px solid var(--line); border-radius:10px; padding:10px 12px; font-size:14px; outline:none; }
  select:focus { border-color:var(--gold); }
  .hint { font-size:12px; color:var(--muted); margin-top:8px; line-height:1.6; min-height:2.6em; }
  .btn { display:block; width:100%; margin-top:14px; padding:12px; border:none; border-radius:12px; font-size:15px; font-weight:700; cursor:pointer; background:linear-gradient(135deg,var(--gold2),var(--gold)); color:#2b1d08; transition:opacity .15s, transform .1s; }
  .btn:disabled { opacity:.45; cursor:not-allowed; }
  .btn:not(:disabled):active { transform:scale(.98); }
  .bgs { display:flex; gap:8px; }
  .bg { flex:1; text-align:center; padding:8px 0; border-radius:10px; border:1px solid var(--line); background:var(--panel2); color:var(--muted); font-size:13px; cursor:pointer; user-select:none; transition:all .15s; }
  .bg.on { border-color:var(--gold); color:var(--gold2); background:#32281c; }
  .stage { display:grid; grid-template-columns:1fr 1fr; gap:14px; }
  @media (max-width:640px){ .stage { grid-template-columns:1fr; } }
  .box { background:var(--panel2); border:1px solid var(--line); border-radius:12px; overflow:hidden; }
  .boxlabel { padding:8px 12px; font-size:12px; color:var(--gold); background:#1f1813; border-bottom:1px solid var(--line); }
  .boximg { height:420px; display:flex; align-items:center; justify-content:center; padding:10px; }
  .boximg img { max-width:100%; max-height:100%; object-fit:contain; }
  .checker { background:repeating-conic-gradient(#3a332c 0 25%, #2a251f 0 50%) 0 0/22px 22px; }
  .white { background:#f2ede4; }
  .black { background:#14100c; }
  .meta { font-size:12px; color:var(--muted); margin-top:10px; }
  .busy { pointer-events:none; opacity:.65; }
  .loading { text-align:center; color:var(--muted); font-size:13px; padding:40px 0; }
</style>
</head>
<body>
<div class="wrap">
  <header>
    <h1>✂️ 抠图工具</h1>
    <p>上传图片，一键去掉背景，下载透明 PNG —— 与卡牌制作无关的独立小工具</p>
    <a class="back" href="/">← 返回卡片工坊</a>
  </header>

  <div class="grid">
    <div>
      <div class="panel">
        <h2>1️⃣ 上传图片</h2>
        <div class="drop" id="drop">
          <div class="big">🖼️</div>
          <p class="main">点击选择图片，或把图片拖到这里</p>
          <p>支持 PNG / JPG / WEBP</p>
        </div>
        <input type="file" id="file" accept="image/*" hidden>
        <div class="thumb" id="thumbBox" hidden>
          <img id="thumbImg" alt="">
          <span id="fileInfo"></span>
        </div>
      </div>

      <div class="panel">
        <h2>2️⃣ 抠图设置</h2>
        <label>抠图模型</label>
        <select id="model"></select>
        <p class="hint" id="modelDesc"></p>
        <button class="btn" id="runBtn" disabled>✂️ 开始抠图</button>
        <p class="hint" id="loadHint">首次使用某个模型时需先加载，请耐心等待片刻</p>
      </div>

      <div class="panel" id="resultPanel" hidden>
        <h2>3️⃣ 结果</h2>
        <div class="bgs">
          <div class="bg on" data-bg="checker">透明底</div>
          <div class="bg" data-bg="white">白底</div>
          <div class="bg" data-bg="black">黑底</div>
        </div>
        <button class="btn" id="dlBtn">⬇️ 下载透明 PNG</button>
        <p class="meta" id="meta"></p>
      </div>
    </div>

    <div class="panel">
      <h2>预览对比</h2>
      <div class="stage">
        <div class="box">
          <div class="boxlabel">原图</div>
          <div class="boximg"><img id="srcImg" alt="原图" style="display:none"><div class="loading" id="srcEmpty">尚未上传图片</div></div>
        </div>
        <div class="box">
          <div class="boxlabel">抠图结果</div>
          <div class="boximg checker" id="outWrap"><img id="outImg" alt="结果" style="display:none"><div class="loading" id="outEmpty">抠图后显示在这里</div></div>
        </div>
      </div>
    </div>
  </div>
</div>

<script>
const MODELS = __MODELS_JSON__;
const DEFAULT_MODEL = __DEFAULT_MODEL__;
const $ = (id) => document.getElementById(id);
let srcDataUrl = null;
let outDataUrl = null;
let outName = "";

const sel = $("model");
for (const [k, v] of MODELS) {
  const o = document.createElement("option");
  o.value = k;
  o.textContent = k;
  sel.appendChild(o);
}
sel.value = DEFAULT_MODEL;
function showModelDesc() {
  for (const [k, v] of MODELS) if (k === sel.value) $("modelDesc").textContent = v;
}
sel.addEventListener("change", showModelDesc);
showModelDesc();

const drop = $("drop"), file = $("file");
drop.addEventListener("click", () => file.click());
drop.addEventListener("dragover", (e) => { e.preventDefault(); drop.classList.add("drag"); });
drop.addEventListener("dragleave", () => drop.classList.remove("drag"));
drop.addEventListener("drop", (e) => {
  e.preventDefault(); drop.classList.remove("drag");
  if (e.dataTransfer.files.length) loadFile(e.dataTransfer.files[0]);
});
file.addEventListener("change", () => { if (file.files.length) loadFile(file.files[0]); });

function loadFile(f) {
  if (!f.type.startsWith("image/")) { alert("请选择图片文件"); return; }
  const reader = new FileReader();
  reader.onload = () => {
    srcDataUrl = reader.result;
    $("srcImg").src = srcDataUrl;
    $("srcImg").style.display = "";
    $("srcEmpty").style.display = "none";
    $("thumbImg").src = srcDataUrl;
    $("thumbBox").hidden = false;
    const mb = (f.size / 1048576).toFixed(2);
    $("fileInfo").textContent = f.name + " · " + mb + " MB";
    outDataUrl = null;
    $("outImg").style.display = "none";
    $("outEmpty").style.display = "";
    $("resultPanel").hidden = true;
    $("runBtn").disabled = false;
  };
  reader.readAsDataURL(f);
}

$("runBtn").addEventListener("click", async () => {
  if (!srcDataUrl) return;
  const btn = $("runBtn");
  btn.disabled = true;
  btn.textContent = "⏳ 抠图中…";
  document.body.classList.add("busy");
  try {
    const m = srcDataUrl.match(/^data:image\/[^;]+;base64,(.*)$/s);
    const resp = await fetch("/api/cutout", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ file_b64: m[1], filename: "cutout.png", model: sel.value }),
    });
    const r = await resp.json();
    if (!r.ok) throw new Error(r.error || "抠图失败");
    outDataUrl = "data:image/png;base64," + r.png_b64;
    outName = "cutout_" + r.width + "x" + r.height + ".png";
    $("outImg").src = outDataUrl;
    $("outImg").style.display = "";
    $("outEmpty").style.display = "none";
    $("resultPanel").hidden = false;
    $("meta").textContent = "尺寸 " + r.width + " × " + r.height + " · 用时 " + r.seconds + " 秒 · " + sel.value;
  } catch (e) {
    alert("抠图失败：" + e.message);
  } finally {
    btn.disabled = false;
    btn.textContent = "✂️ 开始抠图";
    document.body.classList.remove("busy");
  }
});

document.querySelectorAll(".bg").forEach((b) => {
  b.addEventListener("click", () => {
    document.querySelectorAll(".bg").forEach((x) => x.classList.remove("on"));
    b.classList.add("on");
    $("outWrap").className = "boximg " + b.dataset.bg;
  });
});

$("dlBtn").addEventListener("click", () => {
  if (!outDataUrl) return;
  const a = document.createElement("a");
  a.href = outDataUrl;
  a.download = outName || "cutout.png";
  a.click();
});
</script>
</body>
</html>
"""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="webapp", description="cardforge 可视化界面")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--no-browser", action="store_true", help="启动后不自动打开浏览器")
    args = parser.parse_args(argv)

    server = ThreadingHTTPServer((args.host, args.port), Handler)
    url = f"http://{args.host}:{args.port}"
    print("=" * 52)
    print("  🎴 cardforge 卡片工坊 已启动")
    print(f"  打开浏览器访问: {url}")
    print("  按 Ctrl+C 停止服务")
    print("=" * 52)
    for line in model_status():
        print(line)
    print(flush=True)
    if not args.no_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止。")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
