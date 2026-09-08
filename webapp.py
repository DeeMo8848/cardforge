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
import io
import json
import mimetypes
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
from compositor import CARD_H, CARD_W, compose_card, save_frame_interior_mask
from engine import DEFAULT_MODEL, SUPPORTED_MODELS, create_engine, model_status
import webcard

PROJECT_ROOT = Path(__file__).resolve().parent
ASSETS = PROJECT_ROOT / "assets"
UPLOAD_DIR = ASSETS / "output" / "_uploads"
TMP_DIR = ASSETS / "output" / "_tmp"          # 临时上传（拼装前景层等）
PREVIEW_DIR = ASSETS / "output" / "_preview"  # 实时预览 HTML 缓存
EFFECTS_DIR = ASSETS / "effects"              # 自定义特效 JSON
EFFECT_IMG_DIR = ASSETS / "effects-images"    # 特效图像/精灵帧图
SETTINGS_FILE = PROJECT_ROOT / "settings.json"  # 界面参数持久化（如卡封强度）

_ASSET_TYPES = ("backgrounds", "faces", "frames", "frames-text", "seals", "backs", "effects-images")
_IMG_EXTS = (".png", ".jpg", ".jpeg", ".webp", ".gif")

for _d in (UPLOAD_DIR, TMP_DIR, PREVIEW_DIR, EFFECTS_DIR, EFFECT_IMG_DIR, ASSETS / "frames-text"):
    _d.mkdir(parents=True, exist_ok=True)

_engines: dict[str, object] = {}
_engines_lock = threading.Lock()


def get_engine(model: str):
    """进程内按模型缓存引擎实例（会话复用，避免重复加载模型）。"""
    with _engines_lock:
        if model not in _engines:
            _engines[model] = create_engine("local", model)
        return _engines[model]


def list_assets(subdir: str) -> list[str]:
    d = ASSETS / subdir
    if not d.exists():
        return []
    return sorted(p.name for p in d.iterdir() if p.is_file() and p.suffix.lower() in _IMG_EXTS)


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


def list_cards() -> list[dict]:
    """已有卡牌（assets/output/ 下含 front.png 的目录），附收集册展示所需字段。"""
    out = ASSETS / "output"
    cards = []
    if out.exists():
        for d in sorted(out.iterdir()):
            if not d.is_dir() or d.name.startswith("_"):
                continue
            if not (d / "front.png").exists():
                continue
            name, desc, text_type = d.name, "", "none"
            text_pos = None
            try:
                cf = PROJECT_ROOT / "cards" / f"{d.name}.json"
                if cf.exists():
                    data = json.loads(cf.read_text(encoding="utf-8"))
                    name = data.get("name") or d.name
                    desc = data.get("description") or ""
                    text_type = (data.get("text") or {}).get("type") or "none"
                    text_pos = (data.get("text") or {}).get("pos")
            except (OSError, ValueError):
                pass
            inherit = _inherit_card(d.name)
            # build_card_def 会把 extra 展开到 JSON 顶层，特效字段实际存在 data["effects"]
            effects = data.get("effects") or (data.get("extra") or {}).get("effects") or []
            cards.append({
                "id": d.name,
                "name": name,
                "description": desc,
                "text_type": text_type,
                "text_pos": text_pos,
                "card_png": f"/preview/{d.name}/card.png" if (d / "card.png").exists() else f"/preview/{d.name}/front.png",
                "card_html": f"/preview/{d.name}/card.html" if (d / "card.html").exists() else "",
                "frame": inherit.get("frame"),
                "seal": inherit.get("seal"),
                "seal_name": inherit.get("seal_name"),
                "seal_frame": inherit.get("seal_frame"),
                "seal_frame_name": inherit.get("seal_frame_name"),
                "seal_frame_same": inherit.get("seal_frame_same"),
                "face": inherit.get("face_name"),
                "background": inherit.get("background_name"),
                "seal_strength_front": inherit.get("seal_strength_front"),
                "seal_strength_back": inherit.get("seal_strength_back"),
                "seal_frame_strength_front": inherit.get("seal_frame_strength_front"),
                "seal_frame_strength_back": inherit.get("seal_frame_strength_back"),
                "subject_over_frame": inherit.get("subject_over_frame"),
                "subject_outline": inherit.get("subject_outline"),
                "back": inherit.get("back"),
                "scale": _parse_scale((data.get("extra") or {}).get("card_scale", 1.0)),
                "effects": effects,
            })
    return cards


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
    for c in list_cards():
        offline.append({
            "id": c["id"],
            "name": c["name"],
            "description": c["description"],
            "text_type": c["text_type"],
            "card_png": c["card_png"].replace("/preview/", "assets/output/", 1),
            "card_html": c["card_html"].replace("/preview/", "assets/output/", 1),
        })
    data_json = json.dumps(offline, ensure_ascii=False)
    html = re.sub(
        r"/\*__ALBUM_DATA_START__\*/.*?/\*__ALBUM_DATA_END__\*/",
        "/*__ALBUM_DATA_START__*/\nwindow.__ALBUM_DATA__ = " + data_json + ";\n/*__ALBUM_DATA_END__*/",
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


def _abs_effect_images(effects: list[dict]) -> list[dict]:
    """预览页特效引用的图片改写为绝对 URL（页面不在输出目录，相对路径不可用）。组合特效递归处理。"""
    def _walk(defn: dict):
        if not isinstance(defn, dict):
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


def _write_preview_html(html: str) -> dict:
    token = uuid.uuid4().hex[:12]
    d = PREVIEW_DIR / token
    d.mkdir(parents=True, exist_ok=True)
    (d / "preview.html").write_text(html, encoding="utf-8")
    _cleanup_old_previews()
    return {"ok": True, "url": f"/preview/_preview/{token}/preview.html"}


def _export_card(card_id: str, etype: str):
    """导出成品卡：image → card.png；html → 自包含单文件（同目录图片 base64 内嵌）。

    返回 (body, mime, filename)；卡片缺失或类型非法返回 None。
    """
    cid = _safe_name(card_id)
    d = ASSETS / "output" / cid
    if not cid or not d.is_dir():
        return None
    if etype == "image":
        p = d / "card.png"
        if not p.exists():
            return None
        return (p.read_bytes(), "image/png", f"{cid}.png")
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


def build_page() -> str:
    """生成主页面 HTML（素材列表在启动时注入，刷新即最新）。"""
    bg_options = ["", *list_assets("backgrounds")]
    frame_options = ["", *list_assets("frames")]
    seal_options = ["", *list_assets("seals")]
    model_options = [(k, v) for k, v in SUPPORTED_MODELS.items()]
    assets_json = json.dumps({
        "backgrounds": bg_options,
        "frames": frame_options,
        "frames_text": list_assets("frames-text"),
        "seals": seal_options,
        "faces": list_assets("faces"),
        "backs": list_assets("backs"),
        "effect_images": list_assets("effects-images"),
    }, ensure_ascii=False)
    models_json = json.dumps(model_options, ensure_ascii=False)
    effects_json = json.dumps(list_effects(), ensure_ascii=False)
    cards_json = json.dumps(list_cards(), ensure_ascii=False)
    return (
        PAGE_HTML
        .replace("__ASSETS_JSON__", assets_json)
        .replace("__MODELS_JSON__", models_json)
        .replace("__EFFECTS_JSON__", effects_json)
        .replace("__CARDS_JSON__", cards_json)
        .replace("__DEFAULT_MODEL__", json.dumps(DEFAULT_MODEL))
    )


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


def _inherit_card(card_id: str) -> dict:
    """读取成品卡 JSON，返回其边框/卡封/牌背/强度等配置（供拼装预览与保存沿用）。

    素材名优先级：
      1) 显式标记（JSON 顶层含 frame_name/seal_name/back_name 键，值为 null 表示"确实未使用"）；
      2) layers/顶层字段的素材路径 basename（生成卡的素材路径可直接取文件名）；
      3) 旧拼装卡只存输出目录副本（frame.png 等），按字节在素材库中反查。
    """
    if not card_id:
        return {}
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
    out["seal_strength_front"] = d.get("seal_strength_front", extra.get("seal_strength_front"))
    out["seal_strength_back"] = d.get("seal_strength_back", extra.get("seal_strength_back"))
    out["seal_frame_strength_front"] = d.get("seal_frame_strength_front", extra.get("seal_frame_strength_front"))
    out["seal_frame_strength_back"] = d.get("seal_frame_strength_back", extra.get("seal_frame_strength_back"))
    out["subject_over_frame"] = bool(d.get("subject_over_frame", extra.get("subject_over_frame")))
    out["subject_outline"] = bool(d.get("subject_outline", extra.get("subject_outline")))
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

    card_sel = (data.get("card") or "").strip()      # 成品预览：已完成卡牌
    face_sel = (data.get("face") or "").strip()      # 卡面素材
    custom_bg = (data.get("custom_bg") or "").strip()  # 背景素材
    frame_sel = (data.get("frame") or "").strip()    # 边框素材
    seal_sel = (data.get("seal") or "").strip()      # 卡封素材（层1：随内容缩放）
    seal_frame_sel = (data.get("seal_frame") or "").strip()  # 边框卡封（层2：边框作蒙版）
    # 边框卡封"与卡封一致"：与卡牌卡封使用同一素材（只记录一次）
    seal_frame_same = seal_frame_sel == "same"
    if seal_frame_same:
        seal_frame_sel = seal_sel
    fg_url = (data.get("foreground") or "").strip()
    back = (data.get("back") or "three-kingdoms-back.png").strip()
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
    scale = _parse_scale(data.get("scale"))
    # 卡面素材可作为「前景主体层」使用：
    #   - 浮于边框之上：卡面同时作底图与前景层（同一图对齐，前景浮起后主体跃出边框）
    #   - 文本型（boxed）：卡面只在余卡面范围（卡顶到文本区顶部）呈现，底图改用背景/深色
    face_floats = subject_over_frame or text_type == "boxed"
    face_used = ""
    if face_sel and (ASSETS / "faces" / _safe_name(face_sel)).exists():
        face_used = face_sel
        if text_type == "boxed":
            if custom_bg and (ASSETS / "backgrounds" / _safe_name(custom_bg)).exists():
                shutil.copy(ASSETS / "backgrounds" / _safe_name(custom_bg), out / "front.png")
            else:
                Image_dark(out / "front.png")
        else:
            shutil.copy(ASSETS / "faces" / _safe_name(face_sel), out / "front.png")
        fg_url = f"/assets_abs/faces/{face_sel}" if face_floats else ""
    elif custom_bg and (ASSETS / "backgrounds" / _safe_name(custom_bg)).exists():
        shutil.copy(ASSETS / "backgrounds" / _safe_name(custom_bg), out / "front.png")
        if card_sel and not fg_url:
            fg_url = f"/preview/{card_sel}/foreground.png"
    elif card_sel and (ASSETS / "output" / card_sel / "front.png").exists():
        _copy_if_diff(ASSETS / "output" / card_sel / "front.png", out / "front.png")
        if not fg_url:
            fg_url = f"/preview/{card_sel}/foreground.png"
    else:
        Image_dark(out / "front.png")

    # 独立背景层素材：卡面单独缩放时背景整卡铺满（3D 独立背景层 / 2D 背景层共用）
    bg_rel_url = None
    bg_rel_path = None
    if custom_bg and (ASSETS / "backgrounds" / _safe_name(custom_bg)).exists():
        bg_rel_path = ASSETS / "backgrounds" / _safe_name(custom_bg)
        bg_rel_url = f"/assets_abs/backgrounds/{_safe_name(custom_bg)}"

    # 前景（无上传时留空透明层）
    if fg_url.startswith("/preview/_tmp/"):
        tmp_src = TMP_DIR / Path(fg_url).name
        if tmp_src.exists():
            _copy_if_diff(tmp_src, out / "foreground.png")
        else:
            Image_blank(out / "foreground.png")
    elif fg_url.startswith("/assets_abs/faces/"):
        _copy_if_diff(ASSETS / "faces" / Path(fg_url).name, out / "foreground.png")
    elif fg_url and (ASSETS / "output" / Path(fg_url).parts[2] / "foreground.png").exists():
        _copy_if_diff(ASSETS / "output" / Path(fg_url).parts[2] / "foreground.png", out / "foreground.png")
    else:
        Image_blank(out / "foreground.png")

    # 牌背（记录原始素材名，供再次拼装时反查）
    back_file = _safe_name(back)
    if back_file and (ASSETS / "backs" / back_file).exists():
        shutil.copy(ASSETS / "backs" / back_file, out / "back.png")
    else:
        back_file = "three-kingdoms-back.png"
        shutil.copy(ASSETS / "backs" / back_file, out / "back.png")

    # 边框 / 卡封（边框可能来自 frames/ 或 frames-text/；layers 存原始素材路径，便于反查）
    frame_rel = seal_rel = seal_frame_rel = None
    frame_src = seal_src = seal_frame_src = None
    seal_used = seal_sel
    seal_frame_used = seal_frame_sel
    if frame_sel:
        for _d in ("frames", "frames-text"):
            _p = ASSETS / _d / _safe_name(frame_sel)
            if _p.exists():
                shutil.copy(_p, out / "frame.png")
                frame_rel = "frame.png"
                frame_src = str(_p)
                break
    if seal_sel and (ASSETS / "seals" / _safe_name(seal_sel)).exists():
        _p = ASSETS / "seals" / _safe_name(seal_sel)
        shutil.copy(_p, out / "seal.png")
        seal_rel = "seal.png"
        seal_src = str(_p)
    if seal_frame_sel and (ASSETS / "seals" / _safe_name(seal_frame_sel)).exists():
        _p = ASSETS / "seals" / _safe_name(seal_frame_sel)
        shutil.copy(_p, out / "seal_frame.png")
        seal_frame_rel = "seal_frame.png"
        seal_frame_src = str(_p)

    # 未选边框/卡封时删除旧副本，避免再次读取时被字节反查误判为仍在使用
    if not frame_rel and (out / "frame.png").exists():
        (out / "frame.png").unlink()
    if not seal_rel and (out / "seal.png").exists():
        (out / "seal.png").unlink()
    if not seal_frame_rel and (out / "seal_frame.png").exists():
        (out / "seal_frame.png").unlink()

    # 边框内区域蒙版：整卡内容仅绘制在边框外围以内（2D 合成内嵌生成；3D shader 用预生成 PNG）
    interior_rel = None
    if frame_rel:
        try:
            from PIL import Image
            save_frame_interior_mask(Image.open(out / "frame.png"), out / "interior.png")
            interior_rel = "interior.png"
        except Exception:
            interior_rel = None
    elif (out / "interior.png").exists():
        (out / "interior.png").unlink()

    # 卡面素材是否作为整幅卡面（随缩放）：非文本型选择卡面素材时为真（背景/层1卡封不缩放）
    face_scales = bool(face_used and text_type != "boxed")

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
        webcard.build_card_html(name, "front.png", "foreground.png", "back.png", effects=effect_instances,
                                frame_rel=frame_rel, seal_rel=seal_rel,
                                seal_name=seal_used if seal_rel else None,
                                seal_frame_rel=seal_frame_rel,
                                seal_frame_name=seal_frame_used if seal_frame_rel else None,
                                description=desc, text_type=text_type, text_pos=text_pos,
                                subject_over_frame=subject_over_frame,
                                frame_fit_subject=True,
                                round_foreground=True,
                                outline_rel=outline_rel,
                                seal_strength_front=seal_front,
                                seal_strength_back=seal_back,
                                seal_frame_strength_front=seal_frame_front,
                                seal_frame_strength_back=seal_frame_back,
                                content_scale=scale,
                                face_scales=face_scales,
                                interior_rel=interior_rel,
                                background_rel=bg_rel_url),
        encoding="utf-8",
    )

    # 卡牌定义
    card = build_card_def(
        card_id=card_id, name=name, style="transparent",
        source=str(out / "front.png"), front=str(out / "front.png"),
        foreground=str(out / "foreground.png"), engine="assembly",
        description=desc or "",
        back=str(ASSETS / "backs" / back_file),
        layers={
            "background": str(out / "front.png"),
            "face": str(ASSETS / "faces" / _safe_name(face_sel)) if face_used else None,
            "frame": frame_src,
            "seal": seal_src,
            "seal_frame": seal_frame_src,
        },
        text={"title": name, "description": desc or "", "type": text_type, "pos": text_pos},
        extra={
            "composed": False, "assembly": True,
            "subject_over_frame": subject_over_frame,
            "subject_outline": subject_outline,
            "card_scale": scale,
            "seal_strength_front": seal_front,
            "seal_strength_back": seal_back,
            "seal_frame_strength_front": seal_frame_front,
            "seal_frame_strength_back": seal_frame_back,
            "seal_name": seal_used if seal_rel else None,
            "seal_frame_name": seal_frame_used if seal_frame_rel else None,
            "seal_frame_same": seal_frame_same,
            "frame_name": frame_sel if frame_rel else None,
            "back_name": back_file,
            "face_name": face_used if face_used else None,
            "background_name": custom_bg if custom_bg else None,
            "effects": [{"name": e["name"], "pos": e["pos"]} for e in effect_instances],
        },
    )
    save_card_def(card, PROJECT_ROOT / "cards" / f"{card_id}.json")

    # 2D 预览图
    try:
        from PIL import Image
        fg = None
        if (out / "foreground.png").stat().st_size > 0:
            fg = Image.open(out / "foreground.png")
        composed = compose_card(fg, style="transparent", front=str(out / "front.png"),
                                background=str(bg_rel_path) if bg_rel_path else None,
                                frame=str(out / "frame.png") if frame_rel else None,
                                seal=str(out / "seal.png") if seal_rel else None,
                                seal_frame=str(out / "seal_frame.png") if seal_frame_rel else None,
                                title=name, description=desc, text_type=text_type,
                                text_area=text_pos,
                                subject_over_frame=subject_over_frame,
                                subject_outline=subject_outline, size=(CARD_W, CARD_H),
                                content_scale=scale,
                                face_scales=face_scales)
        composed.save(out / "card.png")
    except Exception:
        pass

    build_album_page()  # 刷新离线收集册
    return {"ok": True, "id": card_id, "webcard": str(out / "card.html")}


def _make_text(data: dict) -> dict:
    """基于已有卡牌重新生成文字层（添加/调整卡牌描述与位置，不重新抠图）。"""
    card_id = _safe_name((data.get("id") or "").strip())
    out = ASSETS / "output" / card_id
    if not card_id or not (out / "front.png").exists():
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
    cf = PROJECT_ROOT / "cards" / f"{card_id}.json"
    try:
        card = json.loads(cf.read_text(encoding="utf-8"))
        name = card.get("name") or card_id
        title = (card.get("text") or {}).get("title")
        _extra = card.get("extra") or {}
        subject_over_frame = bool(card.get("subject_over_frame", _extra.get("subject_over_frame")))
        subject_outline = bool(card.get("subject_outline", _extra.get("subject_outline")))
        card_scale = _parse_scale(_extra.get("card_scale", 1.0))
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

    frame_rel = "frame.png" if (out / "frame.png").exists() else None
    seal_rel = "seal.png" if (out / "seal.png").exists() else None
    seal_frame_rel = "seal_frame.png" if (out / "seal_frame.png").exists() else None
    # 边框内区域蒙版：缺图时从边框重新生成（旧卡没有 interior.png）
    interior_rel = None
    if frame_rel:
        if not (out / "interior.png").exists():
            try:
                from PIL import Image
                save_frame_interior_mask(Image.open(out / "frame.png"), out / "interior.png")
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
        if _bgname and (ASSETS / "backgrounds" / _safe_name(_bgname)).exists():
            bg_rel_path = ASSETS / "backgrounds" / _safe_name(_bgname)
            bg_rel_url = f"/assets_abs/backgrounds/{_safe_name(_bgname)}"
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
    (out / "card.html").write_text(
        webcard.build_card_html(name, "front.png", "foreground.png", "back.png", effects=effect_instances,
                                frame_rel=frame_rel, seal_rel=seal_rel,
                                seal_name=seal_name,
                                seal_frame_rel=seal_frame_rel,
                                seal_frame_name=seal_frame_name if seal_frame_rel else None,
                                description=desc, text_type=text_type, text_pos=pos,
                                subject_over_frame=subject_over_frame,
                                frame_fit_subject=True,
                                outline_rel=outline_rel,
                                seal_strength_front=seal_front,
                                seal_strength_back=seal_back,
                                seal_frame_strength_front=seal_frame_front,
                                seal_frame_strength_back=seal_frame_back,
                                content_scale=card_scale,
                                face_scales=face_scales,
                                interior_rel=interior_rel,
                                background_rel=bg_rel_url),
        encoding="utf-8",
    )
    try:
        from PIL import Image
        fg = None
        if (out / "foreground.png").stat().st_size > 0:
            fg = Image.open(out / "foreground.png")
        composed = compose_card(fg, style="transparent", front=str(out / "front.png"),
                                background=str(bg_rel_path) if bg_rel_path else None,
                                frame=str(out / "frame.png") if frame_rel else None,
                                seal=str(out / "seal.png") if seal_rel else None,
                                seal_frame=str(out / "seal_frame.png") if seal_frame_rel else None,
                                title=title, description=desc, text_type=text_type,
                                text_area=pos,
                                subject_over_frame=subject_over_frame,
                                subject_outline=subject_outline, size=(CARD_W, CARD_H),
                                content_scale=card_scale,
                                face_scales=face_scales)
        composed.convert("RGB").save(out / "card.png")
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
    return {"ok": True, "id": card_id, "webcard": str(out / "card.html")}


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
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

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

        if path == "/cutout.html":
            body = build_cutout_page().encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
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

        if path.startswith("/preview/"):
            # /preview/<dir>/.../<file>（可嵌套，如 output/_preview/<token>/preview.html）
            parts = path.strip("/").split("/")
            if len(parts) >= 3 and parts[0] == "preview" and all(p and p != ".." for p in parts[1:]):
                rel = ASSETS / "output"
                for p in parts[1:]:
                    rel = rel / unquote(p)
                if self._send_file(rel):
                    return
            self._send_json({"ok": False, "error": "not found"}, 404)
            return

        if path.startswith("/assets_abs/"):
            # /assets_abs/<type>/<file> —— 直接引用素材库文件
            parts = path.strip("/").split("/")
            if len(parts) == 3 and parts[1] in _ASSET_TYPES:
                fname = unquote(parts[2])
                if _safe_name(fname) == fname:
                    rel = ASSETS / parts[1] / fname
                    if self._send_file(rel):
                        return
            self._send_json({"ok": False, "error": "not found"}, 404)
            return

        if path == "/api/effects":
            self._send_json(list_effects())
            return
        if path == "/api/cards":
            self._send_json({"ok": True, "cards": list_cards()})
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
            self._send_json({"ok": True, "type": atype, "files": list_assets(atype)})
            return

        self._send_json({"ok": False, "error": "not found"}, 404)

    def do_POST(self):
        try:
            length = int(self.headers.get("Content-Length", 0))
            data = json.loads(self.rfile.read(length).decode("utf-8")) if length else {}
        except Exception as e:
            self._send_json({"ok": False, "error": f"请求解析失败: {e}"}, 400)
            return

        try:
            if self.path == "/api/make":
                self._send_json(self._make(data))
            elif self.path == "/api/effect/save":
                self._send_json(self._effect_save(data))
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
            elif self.path == "/api/cutout":
                self._send_json(self._cutout(data))
            else:
                self._send_json({"ok": False, "error": "not found"}, 404)
        except ValueError as e:
            self._send_json({"ok": False, "error": str(e)}, 400)
        except Exception as e:
            self._send_json({"ok": False, "error": str(e)}, 500)

    # ---------- 业务 ----------

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
                adaptive=bool(data.get("adaptive", True)),
                adaptive_mode=int(data.get("adaptive_mode", 1)),
                scale=_parse_scale(data.get("scale")),
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
            back_sel = (data.get("back") or "").strip()
            back = _safe_name(back_sel or "three-kingdoms-back.png")
            blank_url = _ensure_blank()
            desc = (data.get("desc") or "").strip() or None
            text_type = data.get("text_type") or "none"
            if desc and text_type == "none":
                text_type = "transparent"
            subject_over_frame = bool(data.get("subject_over_frame"))
            subject_outline = bool(data.get("subject_outline"))
            scale = _parse_scale(data.get("scale"))
            # 底图优先级：卡面素材 > 背景素材 > 成品卡（与保存逻辑一致）；
            # 卡面素材自含完整画面（前景清空，彻底替换）；背景素材只换底图（保留成品卡主体）
            # 卡面素材可作为「前景主体层」：浮于边框之上时卡面同时作底图与前景层（主体跃出边框）；
            # 文本型（boxed）时卡面只在余卡面范围（卡顶到文本区顶部）呈现，底图改用背景/深色
            face_floats = subject_over_frame or text_type == "boxed"
            if face_sel and _safe_name(face_sel) == face_sel and (ASSETS / "faces" / _safe_name(face_sel)).exists():
                if text_type == "boxed":
                    front_url = (f"/assets_abs/backgrounds/{_safe_name(custom_bg)}"
                                 if custom_bg and (ASSETS / "backgrounds" / _safe_name(custom_bg)).exists()
                                 else _ensure_dark())
                else:
                    front_url = f"/assets_abs/faces/{face_sel}"
                fg = f"/assets_abs/faces/{face_sel}" if face_floats else blank_url
            elif custom_bg and (ASSETS / "backgrounds" / _safe_name(custom_bg)).exists():
                front_url = f"/assets_abs/backgrounds/{_safe_name(custom_bg)}"
                fg = fg or (f"/preview/{card_sel}/foreground.png" if card_sel else blank_url)
            elif card_sel:
                front_url = f"/preview/{card_sel}/front.png"
                fg = fg or f"/preview/{card_sel}/foreground.png"
            else:
                front_url = _ensure_dark()
                fg = fg or blank_url
            # 独立背景层素材：卡面素材随缩放时背景整卡铺满（3D 独立背景层；2D 背景层共用）
            bg_url = None
            bg_path = None
            if custom_bg and (ASSETS / "backgrounds" / _safe_name(custom_bg)).exists():
                bg_path = ASSETS / "backgrounds" / _safe_name(custom_bg)
                bg_url = f"/assets_abs/backgrounds/{_safe_name(custom_bg)}"
            frame_url = None
            frame_path = None
            if frame_sel:
                frame_name = _safe_name(frame_sel)
                for _d in ("frames", "frames-text"):
                    if (ASSETS / _d / frame_name).exists():
                        frame_url = f"/assets_abs/{_d}/{frame_name}"
                        frame_path = ASSETS / _d / frame_name
                        break
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
            # 卡面素材是否作为整幅卡面（随缩放）：非文本型选择卡面素材时为真
            face_scales = bool(face_sel and _safe_name(face_sel) == face_sel
                               and (ASSETS / "faces" / _safe_name(face_sel)).exists()
                               and text_type != "boxed")
            seal_url = f"/assets_abs/seals/{_safe_name(seal_sel)}" if seal_sel else None
            seal_path = ASSETS / "seals" / _safe_name(seal_sel) if seal_sel else None
            seal_frame_url = f"/assets_abs/seals/{_safe_name(seal_frame_sel)}" if seal_frame_sel else None
            seal_frame_path = ASSETS / "seals" / _safe_name(seal_frame_sel) if seal_frame_sel else None
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
                    p = ASSETS / "faces" / Path(fg).name
                    if p.exists():
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
                # 2D 合成预览：与保存时 card.png 同一套 compose_card 逻辑（收集册缩略图同款）
                from PIL import Image
                front_p = None
                if face_sel and (ASSETS / "faces" / _safe_name(face_sel)).exists():
                    if text_type == "boxed":
                        # 文本型：底图用背景/深色，卡面作前景（与 3D 预览一致）
                        if custom_bg and (ASSETS / "backgrounds" / _safe_name(custom_bg)).exists():
                            front_p = ASSETS / "backgrounds" / _safe_name(custom_bg)
                    else:
                        front_p = ASSETS / "faces" / _safe_name(face_sel)
                elif card_sel and (ASSETS / "output" / card_sel / "front.png").exists():
                    front_p = ASSETS / "output" / card_sel / "front.png"
                elif custom_bg and (ASSETS / "backgrounds" / _safe_name(custom_bg)).exists():
                    front_p = ASSETS / "backgrounds" / _safe_name(custom_bg)
                fg_im = None
                if face_sel and text_type == "boxed" and (ASSETS / "faces" / _safe_name(face_sel)).exists():
                    fg_im = Image.open(ASSETS / "faces" / _safe_name(face_sel))
                elif card_sel:
                    fp = ASSETS / "output" / card_sel / "foreground.png"
                    if fp.exists() and fp.stat().st_size > 0:
                        fg_im = Image.open(fp)
                elif fg.startswith("/preview/_tmp/"):
                    tp = TMP_DIR / Path(fg).name
                    if tp.exists() and tp.stat().st_size > 0:
                        fg_im = Image.open(tp)
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
                )
                PREVIEW_DIR.mkdir(parents=True, exist_ok=True)
                pname = f"asm_{uuid.uuid4().hex[:8]}.png"
                composed.save(PREVIEW_DIR / pname)
                return {"ok": True, "url": f"/preview/_preview/{pname}"}
            html = webcard.build_card_html(name, front_url, fg, back_url, effects=effects,
                                           frame_rel=frame_url, seal_rel=seal_url,
                                           seal_name=seal_name if seal_url else None,
                                           seal_frame_rel=seal_frame_url,
                                           seal_frame_name=seal_frame_name if seal_frame_url else None,
                                           description=desc, text_type=text_type,
                                           text_pos=data.get("text_pos"),
                                           subject_over_frame=subject_over_frame,
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
                                           background_rel=bg_url)
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
        name = _safe_name(data.get("name", ""))
        if atype not in _ASSET_TYPES or not name:
            raise ValueError("参数错误")
        target = ASSETS / atype / name
        if not target.exists():
            raise ValueError(f"「{name}」不存在")
        removed_builtin = _remove_builtin_seal(Path(name).stem) if atype == "seals" else None
        target.unlink()
        return {"ok": True, "name": name, "removed_builtin": removed_builtin}

    def _asset_rename(self, data: dict) -> dict:
        atype = data.get("type", "")
        old = _safe_name(data.get("old", ""))
        new = _safe_name(data.get("new", ""))
        if atype not in _ASSET_TYPES or not old or not new:
            raise ValueError("参数错误")
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
  .asset-tile .aname { font-size: 12px; color: var(--muted); word-break: break-all; text-align: center; }
  .asset-tile .arow { display: flex; gap: 6px; }
  .asset-tile .arow .btn.small { flex: 1; font-size: 12px; padding: 6px 8px; }
  .asset-empty { color: var(--muted); font-size: 13px; text-align: center; padding: 30px 0; }
</style>
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
    <div class="tab" data-tab="assemble">🧩 卡牌拼装</div>
    <div class="tab" data-tab="assets">🗂️ 素材管理</div>
<a class="tab" href="/album.html" target="_blank" style="text-decoration:none">📖 收集册</a>
<a class="tab" href="/cutout.html" target="_blank" style="text-decoration:none">✂️ 抠图工具</a>
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
          <label>卡面（可选，未选成品预览时使用卡面素材）</label>
          <select id="asmFace"></select>
        </div>
        <div class="row">
          <label>边框（可选，叠加在卡面之上）</label>
          <select id="asmFrame"></select>
        </div>
        <div class="row">
          <label>背景（未选卡面时作底图；文本型时作卡面底图，可与卡面并存）</label>
          <select id="asmBg"></select>
        </div>
        <div class="row">
          <label>牌背</label>
          <select id="asmBack"></select>
        </div>
        <div class="row">
          <label>卡封（可选，如镭射覆层/封印，随卡面缩放、低于边框）</label>
          <select id="asmSeal"></select>
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
          <label>缩放（卡面渲染比例，0.5~1.5，默认 1 不缩放）</label>
          <input type="number" id="asmScale" min="0.5" max="1.5" step="0.05" value="1">
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
        <div class="typechip" data-type="faces">🌄 卡面</div>
        <div class="typechip" data-type="frames">🖌️ 边框</div>
        <div class="typechip" data-type="frames-text">📜 文本框边框</div>
        <div class="typechip" data-type="seals">🌟 卡封</div>
        <div class="typechip" data-type="backs">🔙 卡背</div>
        <div class="typechip" data-type="effects-images">✨ 特效图</div>
        <span class="grow" style="flex:1"></span>
        <button class="btn small" id="assetUploadBtn" type="button">⬆️ 上传素材</button>
        <input type="file" id="assetFile" accept="image/*" hidden>
      </div>
      <div class="asset-grid" id="assetGrid"></div>
      <div class="status" id="assetStatus"></div>
    </div>
  </section>

  <p class="hint">所有处理都在本机完成 · 产物保存在 assets/output/ 目录 · 模型首次使用自动下载</p>
</div>

<script>
const ASSETS = __ASSETS_JSON__;
const MODELS = __MODELS_JSON__;
const EFFECTS = __EFFECTS_JSON__;
const CARDS = __CARDS_JSON__;
const POSITIONS = [
  ['random', '🎲 随机'], ['top', '⬆️ 上'], ['bottom', '⬇️ 下'], ['left', '⬅️ 左'], ['right', '➡️ 右'],
  ['center', '🎯 中央'], ['topLeft', '↖️ 左上'], ['topRight', '↗️ 右上'],
  ['bottomLeft', '↙️ 左下'], ['bottomRight', '↘️ 右下'], ['custom', '✏️ 自定义'],
];

// ---------- 分页切换 ----------
function switchTab(name) {
  document.querySelectorAll('.tab').forEach(t => t.classList.toggle('on', t.dataset.tab === name));
  document.querySelectorAll('.tabpage').forEach(p => { p.style.display = (p.id === 'page-' + name) ? 'block' : 'none'; });
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
  const img = (def && def.image) || '';
  return img.startsWith('text:') ? img.slice(5).trim().slice(0, 2) : '🖼️';
}
function fillSelect(id, items, labels) {
  const sel = document.getElementById(id);
  sel.innerHTML = '';
  items.forEach((v, i) => {
    const op = document.createElement('option');
    op.value = v; op.textContent = (labels && labels[i] != null) ? labels[i] : v;
    sel.appendChild(op);
  });
}
function fillFxSelect(sel) {
  sel.innerHTML = '';
  const push = (list, builtin) => list.forEach(e => {
    const op = document.createElement('option');
    op.value = e.name;
    op.textContent = effectEmoji(e.def) + ' ' + e.name + (builtin ? '（内置）' : '');
    sel.appendChild(op);
  });
  push(EFFECTS.custom, false);
  push(EFFECTS.builtin, true);
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
  const labels = ['无背景', ...ASSETS.backgrounds.slice(1).map(b => b === 'sample-1.png' ? '默认示例背景（羊皮纸）' : b)];
  fillSelect('background', vals, labels);
}
refillBgSelect();
function syncBgByStyle() {
  const style = document.querySelector('#styleRadios .radio.on').dataset.style;
  const fullBleed = style === 'full-bleed';
  bgSel.value = bgSel.value || 'sample-1.png';
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
fillSelect('seal', ['', ...ASSETS.seals], ['不使用卡封', ...ASSETS.seals]);
fillSelect('sealFrame', ['', 'same', ...ASSETS.seals], ['不使用边框卡封', '与卡封一致', ...ASSETS.seals]);
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
    labels = [...ASSETS.frames_text.map(f => '文本框·' + f), ...ASSETS.frames];
  } else {
    opts = ASSETS.frames;
    labels = [...opts];
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
  document.getElementById('cardImg').src = '/preview/' + d.id + '/card.png?t=' + ts;
  const wl = document.getElementById('webcardLink');
  if (wl) wl.href = '/preview/' + d.id + '/card.html';
  setStatus('status', '✅ 文字已重新放置');
}

const makePicker = createEffectPicker(document.getElementById('makeFxPicker'));

const goBtn = document.getElementById('go');
goBtn.addEventListener('click', async () => {
  if (!fileData) { setStatus('status', '请先选择一张图像', true); return; }
  const style = document.querySelector('.radio.on').dataset.style;
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
        adaptive: document.getElementById('makeAdaptive').checked,
        adaptive_mode: parseInt(document.querySelector('#adaptiveOpts .radio.on').dataset.adaptiveMode, 10),
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
  document.getElementById('cardImg').src = '/preview/' + d.id + '/card.png?t=' + ts;
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
  fetch('/api/cards').then(r => r.json()).then(dc => {
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
  EFFECTS.custom.forEach(e => item(e, false));
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
  fillFxSelect(document.getElementById('comboAddSel'));
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
fillSelect('asmFace', ['', ...ASSETS.faces], ['—— 不使用 ——', ...ASSETS.faces]);
fillSelect('asmBg', ['', ...ASSETS.backgrounds.slice(1)], ['无背景', ...ASSETS.backgrounds.slice(1).map(b => b === 'sample-1.png' ? '默认示例背景（羊皮纸）' : b)]);
fillSelect('asmBack', ASSETS.backs);
fillSelect('asmSeal', ['', ...ASSETS.seals], ['—— 不使用 ——', ...ASSETS.seals]);
// 边框卡封："与卡封一致"=与卡牌卡封同素材（默认）；"不使用边框卡封"=空
fillSelect('asmSealFrame', ['', 'same', ...ASSETS.seals], ['—— 不使用边框卡封 ——', '与卡封一致', ...ASSETS.seals]);

// 卡牌类型：文本型（boxed）只展示带文本框边框（frames-text/）
const asmCardSel = document.getElementById('asmCard');
const asmFaceSel = document.getElementById('asmFace');
const asmFrameSel = document.getElementById('asmFrame');
const asmBgSel = document.getElementById('asmBg');
const asmSealSel = document.getElementById('asmSeal');
const asmSealFrameSel = document.getElementById('asmSealFrame');
const asmTextTypeSel = document.getElementById('asmTextType');
// 用户是否手动选过边框卡封：手动选过后不再自动切换为"与卡封一致"
let asmSealFrameTouched = false;
function refillAsmFrame() {
  const boxed = asmTextTypeSel.value === 'boxed';
  let opts, labels;
  if (boxed) {
    opts = [...ASSETS.frames_text, ...ASSETS.frames];
    labels = [...ASSETS.frames_text.map(f => '文本框·' + f), ...ASSETS.frames];
  } else {
    opts = ASSETS.frames;
    labels = [...opts];
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
    if (typeof card.seal_strength_front === 'number' && isFinite(card.seal_strength_front)) document.getElementById('asmSealFront').value = card.seal_strength_front;
    if (typeof card.seal_strength_back === 'number' && isFinite(card.seal_strength_back)) document.getElementById('asmSealBack').value = card.seal_strength_back;
    if (typeof card.seal_frame_strength_front === 'number' && isFinite(card.seal_frame_strength_front)) document.getElementById('asmSealFrameFront').value = card.seal_frame_strength_front;
    if (typeof card.seal_frame_strength_back === 'number' && isFinite(card.seal_frame_strength_back)) document.getElementById('asmSealFrameBack').value = card.seal_frame_strength_back;
    document.getElementById('asmFloatFg').checked = !!card.subject_over_frame;
    document.getElementById('asmOutline').checked = !!card.subject_outline;
    const asmScaleEl = document.getElementById('asmScale');
    const sc = parseFloat(card.scale);
    if (isFinite(sc) && sc >= 0.5 && sc <= 1.5) asmScaleEl.value = sc;
    else asmScaleEl.value = 1;
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
    asmBgSel.value = '';
  }
  syncAsmSealFrameEnabled();
  syncAsmMutex();
  scheduleAsmPreview();
});
asmFaceSel.addEventListener('change', () => { syncAsmMutex(); scheduleAsmPreview(); });
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
document.getElementById('asmFloatFg').addEventListener('change', scheduleAsmPreview);
document.getElementById('asmOutline').addEventListener('change', scheduleAsmPreview);
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
// 缩放：调整时实时刷新 3D 预览（与其它可调节项一致）
document.getElementById('asmScale').addEventListener('input', scheduleAsmPreview);
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
  sealFrameTouched = false;
  document.getElementById('asmBack').selectedIndex = 0;
  document.getElementById('asmSealFront').value = 0.945;
  document.getElementById('asmSealBack').value = 0.5775;
  document.getElementById('asmSealFrameFront').value = 0.8;
  document.getElementById('asmSealFrameBack').value = 0.5;
  document.getElementById('asmFloatFg').checked = false;
  document.getElementById('asmOutline').checked = false;
  document.getElementById('asmScale').value = 1;
  asmTextTypeSel.value = 'none';
  refillAsmFrame();
  document.getElementById('asmDesc').value = '';
  [['asmAreaL', 8], ['asmAreaR', 92], ['asmAreaT', 70], ['asmAreaB', 92]].forEach(([id, v]) => {
    document.getElementById(id).value = v;
  });
  asmPicker.clear();
  document.getElementById('asmName').value = '';
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
        frame: asmFrameSel.value,
        custom_bg: asmBgSel.value,
        back: document.getElementById('asmBack').value,
        seal: asmSealSel.value,
        seal_frame: asmSealFrameSel.value,
        effects: asmPicker.get(),
        subject_over_frame: document.getElementById('asmFloatFg').checked,
        subject_outline: document.getElementById('asmOutline').checked,
        scale: parseFloat(document.getElementById('asmScale').value) || 1,
        seal_strength_front: getSealStrengths().front,
        seal_strength_back: getSealStrengths().back,
        seal_frame_strength_front: getSealFrameStrengths().front,
        seal_frame_strength_back: getSealFrameStrengths().back,
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
        frame: asmFrameSel.value,
        custom_bg: asmBgSel.value,
        back: document.getElementById('asmBack').value,
        seal: asmSealSel.value,
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
        scale: parseFloat(document.getElementById('asmScale').value) || 1,
        seal_strength_front: getSealStrengths().front,
        seal_strength_back: getSealStrengths().back,
        seal_frame_strength_front: getSealFrameStrengths().front,
        seal_frame_strength_back: getSealFrameStrengths().back,
      }),
    });
    const d = await r.json();
    if (!d.ok) throw new Error(d.error);
    setStatus('asmStatus', '✅ 已保存：' + d.webcard);
    persistSealStrengths();
    // 刷新成品列表：新卡/被编辑的卡立即出现在下拉中，方便继续编辑
    try {
      const rc = await fetch('/api/cards');
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
const ASSET_TYPES = ['backgrounds', 'faces', 'frames', 'frames-text', 'seals', 'backs', 'effects-images'];
let currentAssetType = 'backgrounds';

function renderAssetTypes() {
  document.querySelectorAll('.typechip').forEach(c => {
    c.classList.toggle('on', c.dataset.type === currentAssetType);
  });
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
  d.files.forEach(name => {
    const tile = document.createElement('div');
    tile.className = 'asset-tile';
    tile.innerHTML =
      '<img src="/assets_abs/' + currentAssetType + '/' + encodeURIComponent(name) + '?t=' + Date.now() + '" alt="">' +
      '<div class="aname">' + name + '</div>' +
      '<div class="arow">' +
      '<button class="btn small ghost" data-act="rename">✏️ 重命名</button>' +
      '<button class="btn small danger" data-act="del">🗑️</button>' +
      '</div>';
    tile.querySelector('[data-act=rename]').addEventListener('click', async () => {
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
    tile.querySelector('[data-act=del]').addEventListener('click', async () => {
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
  c.addEventListener('click', () => { currentAssetType = c.dataset.type; renderAssetTypes(); loadAssets(); });
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

loadAssets();
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
