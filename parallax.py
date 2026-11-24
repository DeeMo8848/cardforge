# -*- coding: utf-8 -*-
"""视差主体（parallax subject）：把「透明主体」拆成多层，各层按深度做不同幅度视差。

数据落在 `assets/parallax/<id>/`：

    meta.json     主体定义（画布尺寸 / 全局幅度 / 图层数组，数组顺序 = 后 → 前）
    L00.webp ...  每层素材（带 alpha，原尺寸）
    thumb.webp    列表缩略图（合成图）

拼装页当作「主体层」使用时，`prepare_for_card()` 会把每层按整卡画布栅格化到
卡目录的 `parallax/` 下（每层一张与卡同尺寸的透明图），再合成一张
`foreground.png` 供 2D 预览 / 老逻辑兜底。

★ 为什么栅格化成「整卡尺寸」而不是在 card.html 里保留原始尺寸 + 变换：
  card.html 里每层是一个 PlaneGeometry(CARD_WIDTH, CARD_HEIGHT) 的 mesh，
  位置/缩放全烘进图里 ⇒ JS 端只管视差位移，不用同步两套坐标（卡坐标 vs 层坐标）。
  代价是每层一张整卡图，但层数是个位数，可接受。
"""
from __future__ import annotations

import hashlib
import json
import re
import shutil
import time
from pathlib import Path

from PIL import Image, ImageFilter

PROJECT_ROOT = Path(__file__).resolve().parent
PARALLAX_ROOT = PROJECT_ROOT / "assets" / "parallax"
UPLOAD_ROOT = PARALLAX_ROOT / "_uploads"

# 编辑画布基准尺寸（3:4，与卡面同比例）。拼装页使用时按目标卡尺寸等比缩放，
# 所以这里只是「设计基准」，不影响清晰度上限（层素材本身要保持大图）。
BASE_W, BASE_H = 900, 1200

# 视差默认幅度：满视角（卡面转到侧面）时的基准位移，占卡宽比例。
# 0.16 ≈ 卡宽的 16%，三层叠起来已经很像卡雕；再大就开始露边。
DEFAULT_AMP = 0.16
MAX_LAYERS = 12          # 每层一张整卡图，太多会拖纹理与显存
MAX_UPLOAD_BYTES = 30 * 1024 * 1024


# ---------------------------------------------------------------- 基础工具
def _slug(text: str) -> str:
    s = re.sub(r"[^\w\u4e00-\u9fff\-]+", "_", str(text or "").strip())
    return s.strip("_")[:60] or "subject"


def _unique_id(want: str) -> str:
    base = _slug(want)
    if not (PARALLAX_ROOT / base).exists():
        return base
    for i in range(2, 200):
        cand = "%s-%d" % (base, i)
        if not (PARALLAX_ROOT / cand).exists():
            return cand
    return "%s-%d" % (base, int(time.time()))


def _clamp(v, lo, hi, dflt=0.0):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return dflt
    return max(lo, min(hi, f))


def normalize_layer(raw: dict, idx: int = 0) -> dict:
    """把前端来的图层参数收成可信形状（缺字段补默认，数值夹到合理范围）。"""
    return {
        "file": str(raw.get("file") or "").strip(),
        "name": str(raw.get("name") or ("图层 %d" % (idx + 1))).strip()[:40],
        # 层中心在卡面上的归一化位置（0~1 = 卡面内，允许越界放装饰）
        "x": _clamp(raw.get("x", 0.5), -1.5, 2.5, 0.5),
        "y": _clamp(raw.get("y", 0.5), -1.5, 2.5, 0.5),
        # 显示宽度 / 卡宽
        "scale": _clamp(raw.get("scale", 1.0), 0.02, 6.0, 1.0),
        "opacity": _clamp(raw.get("opacity", 1.0), 0.0, 1.0, 1.0),
        "visible": bool(raw.get("visible", True)),
        # 视差倍率：0 = 钉在卡面不动，1 = 与原主体一样，2 = 两倍（越靠前越大）；
        # 负数 = 反方向偏移（比如「陷进卡面里」的一层，倾斜时与主体反向走）
        "depth": _clamp(raw.get("depth", 1.0), -3.0, 3.0, 1.0),
        # 层自身投影（卡雕的「厚度」主要靠它）
        "shadow": _clamp(raw.get("shadow", 0.0), 0.0, 1.0, 0.0),
        "src_w": int(_clamp(raw.get("src_w", 0), 0, 20000, 0)),
        "src_h": int(_clamp(raw.get("src_h", 0), 0, 20000, 0)),
    }


def normalize_meta(raw: dict, pid: str | None = None) -> dict:
    layers = [normalize_layer(x, i) for i, x in enumerate(raw.get("layers") or [])
              if isinstance(x, dict)]
    return {
        "id": pid or _slug(raw.get("id") or raw.get("name") or "subject"),
        "name": str(raw.get("name") or (pid or "视差主体")).strip()[:40],
        "card_w": int(_clamp(raw.get("card_w", BASE_W), 200, 4000, BASE_W)),
        "card_h": int(_clamp(raw.get("card_h", BASE_H), 200, 6000, BASE_H)),
        "amp": _clamp(raw.get("amp", DEFAULT_AMP), 0.0, 0.5, DEFAULT_AMP),
        "layers": layers[:MAX_LAYERS],
        "updated": int(raw.get("updated") or time.time()),
    }


def max_depth(meta: dict) -> float:
    """归一化用的基准深度 = 可见层里深度的最大绝对值。

    ★ 必须取绝对值：depth 允许负数（反方向偏移），若按最大值算，全是负数时
      最大值为负，除以负数会把所有层的方向再翻一次。
    """
    ds = [abs(L["depth"]) for L in meta.get("layers") or [] if L.get("visible")]
    d = max(ds) if ds else 0.0
    return d if d > 1e-6 else 1.0   # 全 0（都钉死）时给 1.0，别让下游除 0


def meta_of(pid: str) -> Path:
    return PARALLAX_ROOT / pid / "meta.json"


def load_item(pid: str) -> dict | None:
    p = meta_of(pid)
    if not p.is_file():
        return None
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    d["id"] = pid
    return normalize_meta(d, pid)


def list_items() -> list[dict]:
    if not PARALLAX_ROOT.is_dir():
        return []
    out = []
    for d in sorted(PARALLAX_ROOT.iterdir()):
        if not d.is_dir() or d.name.startswith("_"):
            continue
        m = load_item(d.name)
        if not m:
            continue
        m["layer_count"] = len(m["layers"])
        m["thumb"] = ("/parallax/%s/thumb.webp" % d.name) if (d / "thumb.webp").is_file() else ""
        out.append(m)
    return out


# ---------------------------------------------------------------- 图层上传
def save_upload(raw: bytes, name: str = "layer.png") -> dict:
    """收一张图层素材：转 RGBA WebP 落到 _uploads/，返回可直接喂前端的记录。

    页面里拖动/缩放都只是改参数，素材本体不再重编码；保存主体时才按 id 归档。
    """
    if not raw:
        raise ValueError("空文件")
    if len(raw) > MAX_UPLOAD_BYTES:
        raise ValueError("图片太大（>30MB），先压一下")
    import io

    try:
        im = Image.open(io.BytesIO(raw))
        im.load()
    except Exception as e:  # noqa: BLE001
        raise ValueError("不是能识别的图片：%s" % e) from e
    im = im.convert("RGBA")
    UPLOAD_ROOT.mkdir(parents=True, exist_ok=True)
    h = hashlib.md5(raw).hexdigest()[:16]
    dst = UPLOAD_ROOT / ("%s.webp" % h)
    if not dst.is_file():
        im.save(dst, "WEBP", lossless=True, method=6)
    return {"ok": True, "file": "_uploads/%s.webp" % h, "w": im.width, "h": im.height,
            "name": Path(name).stem[:40] or "图层"}


def _resolve_layer_file(meta_id: str, rel: str) -> Path | None:
    """图层文件可能来自主体目录，也可能还挂在 _uploads（刚上传、尚未归档）。"""
    rel = str(rel or "").strip().lstrip("/").replace("\\", "/")
    if not rel or ".." in rel:
        return None
    if rel.startswith("_uploads/"):
        p = PARALLAX_ROOT / rel
    else:
        p = PARALLAX_ROOT / meta_id / Path(rel).name
    return p if p.is_file() else None


# ---------------------------------------------------------------- 渲染
def _layer_image(meta: dict, L: dict) -> tuple[Image.Image | None, dict]:
    """取该层缩放后的 RGBA 图与它在画布上的落点（左上角像素坐标）。"""
    src = _resolve_layer_file(meta["id"], L["file"])
    if not src:
        return None, {}
    im = Image.open(src).convert("RGBA")
    cw, ch = meta["card_w"], meta["card_h"]
    w = max(1, int(round(cw * L["scale"])))
    h = max(1, int(round(im.height * (w / max(1, im.width)))))
    if (w, h) != im.size:
        im = im.resize((w, h), Image.LANCZOS)
    if L["opacity"] < 0.999:
        a = im.getchannel("A").point(lambda v: int(v * L["opacity"]))
        im.putalpha(a)
    dx = int(round(L["x"] * cw - w / 2.0))
    dy = int(round(L["y"] * ch - h / 2.0))
    return im, {"dx": dx, "dy": dy, "w": w, "h": h}


def _paste_alpha(canvas: Image.Image, im: Image.Image, dx: int, dy: int) -> None:
    """把图 alpha 合成到画布（支持负坐标 / 越界裁剪）。

    不能直接用 canvas.paste(im, (dx,dy), im)：带 mask 的 paste 是「覆盖」而不是
    「按 alpha 混合」，重叠层会挖掉下层。alpha_composite 又要求 dest 非负，
    所以先裁到画布范围内再合。
    """
    cw, ch = canvas.size
    x0, y0 = max(0, dx), max(0, dy)
    x1, y1 = min(cw, dx + im.width), min(ch, dy + im.height)
    if x1 <= x0 or y1 <= y0:
        return
    crop = im.crop((x0 - dx, y0 - dy, x1 - dx, y1 - dy))
    canvas.alpha_composite(crop, (x0, y0))


def _shadow_of(im: Image.Image, strength: float) -> Image.Image | None:
    """层的落影：把 alpha 平移一点、染深、再按 strength 压 alpha。

    卡雕的「厚度」不能只靠错位 —— 层与层之间没有影子就是一叠透明贴纸。
    """
    if strength <= 0.01:
        return None
    a = im.getchannel("A")
    off = max(2, int(round(im.width * 0.006)))
    shift = Image.new("L", a.size, 0)
    shift.paste(a, (off, off))
    shift = shift.filter(ImageFilter.GaussianBlur(max(1.0, off * 0.8)))
    alpha = shift.point(lambda v: int(v * 0.62 * strength))
    out = Image.new("RGBA", im.size, (18, 12, 8, 0))
    out.putalpha(alpha)
    return out


def composite(meta: dict, size: tuple[int, int] | None = None) -> Image.Image:
    """把全部可见层合成一张 RGBA（深度取 1.0 的中性视角），供 2D 预览 / 缩略图。"""
    cw, ch = size or (meta["card_w"], meta["card_h"])
    canvas = Image.new("RGBA", (cw, ch), (0, 0, 0, 0))
    sx = cw / float(meta["card_w"])
    sy = ch / float(meta["card_h"])
    for L in meta["layers"]:
        if not L["visible"]:
            continue
        im, g = _layer_image(meta, L)
        if im is None:
            continue
        if (sx, sy) != (1.0, 1.0):
            im = im.resize((max(1, int(round(im.width * sx))),
                            max(1, int(round(im.height * sy)))), Image.LANCZOS)
        sh = _shadow_of(im, L["shadow"])
        dx, dy = int(round(g["dx"] * sx)), int(round(g["dy"] * sy))
        if sh is not None:
            _paste_alpha(canvas, sh, dx, dy)
        _paste_alpha(canvas, im, dx, dy)
    return canvas


def _save_thumb(pid: str) -> None:
    m = load_item(pid)
    if not m:
        return
    im = composite(m)
    im.thumbnail((320, 320), Image.LANCZOS)
    im.save(PARALLAX_ROOT / pid / "thumb.webp", "WEBP", quality=88, method=6)


def save_item(raw: dict, pid: str | None = None) -> dict:
    """保存（新建或覆盖）一个视差主体。

    前端传的 layer.file 可能是：
      - `_uploads/<hash>.webp`（刚上传的）→ 归档成 `<id>/L<nn>.webp`
      - `L<nn>.webp`（已在主体目录里）→ 保持不动
    ★ 归档顺序必须与 layers 数组一致，重排/删除后重新编号，否则文件与层对不上。
    """
    meta = normalize_meta(raw, None)
    target = pid or (raw.get("id") or "").strip()
    new_created = False
    if target:
        target = _slug(target)
        if not meta_of(target).is_file():
            new_created = True
    else:
        target = _unique_id(raw.get("name") or "subject")
        new_created = True
    meta["id"] = target
    out = PARALLAX_ROOT / target
    out.mkdir(parents=True, exist_ok=True)

    if not meta["layers"]:
        raise ValueError("至少放一层再保存")

    # 两阶段归档：先全部复制到 _tmpNN，再删旧 L*.webp、最后统一改名。
    # ★ 不能「边算边写 LNN」：层重排时 L00↔L01 会互相覆盖，素材直接串味。
    staged = []
    for i, L in enumerate(meta["layers"]):
        src = _resolve_layer_file(target, L["file"])
        if src is None:
            raise ValueError("第 %d 层素材找不到：%s" % (i + 1, L["file"]))
        staged.append((i, src, L))
    for i, src, _L in staged:
        tmp = out / ("_tmp%02d.webp" % i)
        if src.resolve() != tmp.resolve():
            shutil.copyfile(src, tmp)
    for p in out.glob("L*.webp"):
        p.unlink(missing_ok=True)
    for i, _src, L in staged:
        dst = out / ("L%02d.webp" % i)
        (out / ("_tmp%02d.webp" % i)).replace(dst)
        L["file"] = dst.name
        if not L["src_w"] or not L["src_h"]:
            with Image.open(dst) as im:
                L["src_w"], L["src_h"] = im.width, im.height

    meta["updated"] = int(time.time())
    meta_of(target).write_text(json.dumps(meta, ensure_ascii=False, indent=2),
                               encoding="utf-8")
    try:
        _save_thumb(target)
    except Exception:  # noqa: BLE001  缩略图失败不该挡住保存
        pass
    try:
        _bake_pre(target)   # 整卡尺寸层图缓存，拼装页/预览直接用
    except Exception:  # noqa: BLE001
        pass
    return {"ok": True, "id": target, "created": new_created,
            "meta": meta, "thumb": "/parallax/%s/thumb.webp" % target}


def delete_item(pid: str) -> dict:
    p = PARALLAX_ROOT / _slug(pid)
    if not p.is_dir():
        return {"ok": False, "error": "没有这个视差主体"}
    shutil.rmtree(p, ignore_errors=True)
    return {"ok": True}


# ---------------------------------------------------------------- 给卡用
def _bake_layers(meta: dict, dst: Path, size: tuple[int, int] | None = None) -> list[dict]:
    """把每层栅格化成「整卡画布」的透明图写进 dst，返回层清单（后 → 前）。

    位置/缩放/投影全烘进图里，这样 card.html 里每层都是一个和卡同尺寸的平面，
    JS 只管视差位移，不用同步两套坐标系。
    """
    dst.mkdir(parents=True, exist_ok=True)
    cw, ch = size or (meta["card_w"], meta["card_h"])
    out = []
    for i, L in enumerate(meta["layers"]):
        if not L["visible"]:
            continue
        im, g = _layer_image(meta, L)
        if im is None:
            continue
        one = Image.new("RGBA", (meta["card_w"], meta["card_h"]), (0, 0, 0, 0))
        sh = _shadow_of(im, L["shadow"])
        if sh is not None:
            _paste_alpha(one, sh, g["dx"], g["dy"])
        _paste_alpha(one, im, g["dx"], g["dy"])
        if (meta["card_w"], meta["card_h"]) != (cw, ch):
            one = one.resize((cw, ch), Image.LANCZOS)
        name = "L%02d.webp" % i
        one.save(dst / name, "WEBP", lossless=True, method=6)
        out.append({"file": name, "depth": round(L["depth"], 3), "name": L["name"]})
    return out


def _bake_pre(pid: str) -> None:
    """保存主体时预烤一份「整卡尺寸」的层图缓存（<id>/pre/）。

    拼装页保存与拼装页实时预览都从这里复制文件 —— 预览是每拖一下滑块就重来一次，
    现场用 Pillow 栅格化 3~10 层会明显卡；复制几张小文件是毫秒级。
    """
    meta = load_item(pid)
    if not meta:
        return
    dst = PARALLAX_ROOT / pid / "pre"
    if dst.exists():
        shutil.rmtree(dst, ignore_errors=True)
    _bake_layers(meta, dst)


def prepare_for_card(pid: str, out_dir: Path, sub: str = "") -> dict:
    """把视差主体落到成品卡目录，返回 {} 供 build_card_html(parallax=...)。

    产物：
        <out_dir>/parallax/L00.webp ...   每层一张「整卡尺寸」透明图
        <out_dir>/parallax/meta.json      层顺序 / depth / opacity
        <out_dir>/foreground.png          各层合成（2D 预览与老逻辑兜底）

    sub: 落到 <out_dir>/parallax/<sub>/ 下。光栅卡「每帧一个视差主体」要用它 ——
         一帧一套层图，各自独立目录；src 里也带上子目录。

    返回的 layers 是「后 → 前」，src 是相对卡目录的路径，直接进 card.html。
    """
    meta = load_item(pid)
    if not meta:
        raise ValueError("视差主体不存在：%s" % pid)
    sub = _slug(sub) if sub else ""
    base = Path(out_dir) / "parallax"
    dst = (base / sub) if sub else base
    if dst.exists():
        shutil.rmtree(dst, ignore_errors=True)
    dst.mkdir(parents=True, exist_ok=True)
    rel = ("parallax/" + sub + "/") if sub else "parallax/"

    # 优先用保存时烤好的 pre/ 缓存；缺失或与当前层数/尺寸对不上就现场重烤
    pre = PARALLAX_ROOT / pid / "pre"
    want = len([L for L in meta["layers"] if L["visible"]])
    cached = sorted(pre.glob("L*.webp")) if pre.is_dir() else []
    layers: list[dict] = []
    recs = _bake_layers_meta(meta)
    if len(cached) == want and want:
        for r in recs:
            im = Image.open(pre / r["file"]).convert("RGBA")
            if im.size != (meta["card_w"], meta["card_h"]):
                im = im.resize((meta["card_w"], meta["card_h"]), Image.LANCZOS)
            im.save(dst / r["file"], "WEBP", lossless=True, method=6)
            layers.append({"src": rel + r["file"], "depth": r["depth"],
                           "opacity": 1.0, "name": r["name"]})
    else:
        for rec in _bake_layers(meta, dst):
            layers.append({"src": rel + rec["file"], "depth": rec["depth"],
                           "opacity": 1.0, "name": rec["name"]})

    if not layers:
        raise ValueError("这个视差主体没有可见图层")

    payload = {"amp": meta["amp"], "max_depth": max_depth(meta),
               "card_w": meta["card_w"], "card_h": meta["card_h"],
               "layers": layers}
    (dst / "meta.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                                   encoding="utf-8")
    return payload


def _bake_layers_meta(meta: dict) -> list[dict]:
    """可见层的文件名/depth/name（与 _bake_layers 的编号规则一致：按 layers 下标）。"""
    out = []
    for i, L in enumerate(meta["layers"]):
        if not L["visible"]:
            continue
        out.append({"file": "L%02d.webp" % i, "depth": round(L["depth"], 3), "name": L["name"]})
    return out


def bind_to_card(pid: str, out_dir: Path, card_w: int = BASE_W, card_h: int = BASE_H) -> dict:
    """prepare_for_card + 写 foreground.png，返回 build_card_html 需要的 parallax dict。"""
    payload = prepare_for_card(pid, out_dir)
    meta = load_item(pid)
    comp = composite(meta, (card_w, card_h))
    # 必须是真 PNG：文件名 foreground.png 被全流程当 PNG 用（compositor / 前端预览）
    comp.save(Path(out_dir) / "foreground.png", "PNG")
    return payload


# ---------------------------------------------------------------- 编辑器页面
PARALLAX_HTML = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>视差主体 · cardforge</title>
<style>
  * { margin: 0; padding: 0; box-sizing: border-box; }
  html, body { height: 100%; }
  body {
    background: #14100c; color: #e8dcc6; overflow: hidden;
    font-family: "Microsoft YaHei", "PingFang SC", system-ui, sans-serif; font-size: 13px;
  }
  #wrap { display: flex; height: 100vh; }
  /* ---------- 左：画布 ---------- */
  #stageCol { flex: 1; display: flex; flex-direction: column; min-width: 0; background: #0f0c09; }
  #topbar {
    display: flex; align-items: center; gap: 10px; padding: 8px 14px;
    border-bottom: 1px solid #2a2119; background: #17120d; flex-wrap: wrap;
  }
  #topbar .sp { flex: 1; }
  .badge {
    padding: 2px 8px; border-radius: 10px; font-size: 11px; letter-spacing: .5px;
    background: #2a2016; color: #c9a86a; border: 1px solid #3a2c1d;
  }
  #canvasBox {
    flex: 1; display: flex; align-items: center; justify-content: center;
    padding: 14px; min-height: 0; position: relative;
  }
  #cv {
    display: block; background: #1a1510; border-radius: 10px;
    box-shadow: 0 10px 40px rgba(0,0,0,.6), 0 0 0 1px #2f2519 inset;
    touch-action: none; cursor: crosshair;
  }
  #hintbar {
    padding: 6px 14px 10px; color: #7d6e58; font-size: 11.5px; text-align: center; letter-spacing: .3px;
  }
  /* ---------- 右：面板 ---------- */
  #panel {
    width: 336px; flex: 0 0 336px; overflow-y: auto; overflow-x: hidden;
    background: #17120d; border-left: 1px solid #2a2119; padding: 12px 12px 40px;
  }
  .sec { margin-bottom: 14px; }
  .sec > h3 {
    font-size: 12px; font-weight: 600; color: #c9a86a; letter-spacing: 1px;
    margin-bottom: 7px; display: flex; align-items: center; gap: 6px;
  }
  .sec > h3 .n { color: #6d5f4a; font-weight: 400; letter-spacing: 0; }
  .row { display: flex; align-items: center; gap: 6px; margin-bottom: 6px; }
  .row > label { flex: 0 0 62px; color: #9d8f79; font-size: 12px; }
  input[type=text], input[type=number], select {
    background: #0f0b08; color: #e8dcc6; border: 1px solid #3a2c1d; border-radius: 5px;
    padding: 5px 7px; font-size: 12.5px; font-family: inherit; min-width: 0; flex: 1;
  }
  input[type=range] { flex: 1; accent-color: #c9a86a; min-width: 0; }
  button {
    background: #241b13; color: #e2d3b4; border: 1px solid #3d2f20; border-radius: 6px;
    padding: 6px 11px; font-size: 12.5px; cursor: pointer; font-family: inherit;
    transition: background .12s, border-color .12s;
  }
  button:hover { background: #2f2418; border-color: #5a452c; }
  button.pri { background: #6b4d21; border-color: #8a6a30; color: #fff4dd; }
  button.pri:hover { background: #7d5a28; }
  button.danger { color: #d98a7a; border-color: #523024; }
  button.sm { padding: 3px 7px; font-size: 11.5px; }
  button:disabled { opacity: .4; cursor: default; }
  .val { flex: 0 0 44px; text-align: right; color: #b9a888; font-variant-numeric: tabular-nums; font-size: 11.5px; }
  /* 图层列表 */
  #lyrList { border: 1px solid #2a2119; border-radius: 7px; overflow: hidden; }
  .lyr {
    display: flex; align-items: center; gap: 7px; padding: 5px 7px; cursor: pointer;
    background: #1b1510; border-bottom: 1px solid #241b14;
  }
  .lyr:last-child { border-bottom: 0; }
  .lyr:hover { background: #221a13; }
  .lyr.on { background: #3a2a16; box-shadow: inset 2px 0 0 #c9a86a; }
  .lyr img { width: 30px; height: 30px; object-fit: contain; background:
    repeating-conic-gradient(#221a13 0 25%, #1a140f 0 50%) 0 0/10px 10px; border-radius: 3px; }
  .lyr .nm { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .lyr .dp { font-size: 10.5px; color: #8a7a62; font-variant-numeric: tabular-nums; }
  .lyr .eye { opacity: .55; font-size: 12px; }
  .lyr.hid .nm { opacity: .4; text-decoration: line-through; }
  .lyr.hid .eye { opacity: .25; }
  #emptyTip { padding: 14px 8px; text-align: center; color: #6d5f4a; font-size: 12px; line-height: 1.7; }
  .drop { outline: 2px dashed #8a6a30; outline-offset: -4px; }
  #msg { font-size: 12px; color: #9d8f79; padding: 4px 2px 0; min-height: 17px; line-height: 1.5; }
  #msg.err { color: #d98a7a; }
  #msg.ok { color: #93b878; }
  .mini { color: #6d5f4a; font-size: 11px; line-height: 1.6; }
  .sway { display: flex; gap: 6px; align-items: center; }
  /* 顶栏开关（跟随鼠标 / 卡牌模式）：label 包 checkbox，点文字也能切 */
  .tog {
    display: flex; align-items: center; gap: 5px; font-size: 11.5px; color: #a4947a;
    background: #1c1610; border: 1px solid #33291d; border-radius: 6px;
    padding: 3px 8px; cursor: pointer; user-select: none; white-space: nowrap;
  }
  .tog:hover { border-color: #4a3927; color: #cdbb9c; }
  .tog input { accent-color: #c9a86a; margin: 0; }
  .tog.on { color: #f2e5c8; border-color: #6b4d21; background: #2b2015; }
  /* 卡牌模式：画面按 3D 倾斜，阴影跟着卡面轮廓（圆角外是透明的，所以用 drop-shadow） */
  #cv.card3d {
    filter: drop-shadow(0 18px 34px rgba(0,0,0,.75));
    cursor: grab;
  }
  #cv.card3d.drag { cursor: grabbing; }
</style>
</head>
<body>
<div id="wrap">
  <div id="stageCol">
    <div id="topbar">
      <span class="badge" id="modeTag">视差主体</span>
      <span id="statTag" class="mini">0 层</span>
      <span class="sp"></span>
      <button id="swayBtn" class="sm" title="自动左右摆动，检查视差层次">▶ 摆动预览</button>
      <label class="tog on" id="followTog" title="鼠标左右移动就换视角；关掉后视角只由滑杆/拖动决定（对齐构图时先关掉）">
        <input type="checkbox" id="followChk" checked><span>🖱 跟随鼠标</span>
      </label>
      <label class="tog" id="cardTog" title="把画面按 3D 卡牌倾斜显示（约 41°，与 card.html 的翻卡角度一致）">
        <input type="checkbox" id="cardChk"><span>🃏 卡牌模式</span>
      </label>
      <span class="mini">视角</span>
      <input type="range" id="viewRange" min="-100" max="100" value="0" style="width:110px">
      <span class="val" id="viewVal">0</span>
      <button id="viewReset" class="sm" title="视角回到正面（0°）">⟲ 复位</button>
    </div>
    <div id="canvasBox"><canvas id="cv" width="900" height="1200"></canvas></div>
    <div id="hintbar">拖动图层移动 · 滚轮缩放 · 鼠标左右移画面看视差 · 选中层可调深度/投影</div>
  </div>

  <aside id="panel">
    <div class="sec">
      <h3>主体</h3>
      <div class="row">
        <label>名称</label>
        <input type="text" id="pName" placeholder="例如 雪华-前景">
      </div>
      <div class="row">
        <label>已有</label>
        <select id="pList"><option value="">— 新建 —</option></select>
        <button class="sm" id="pReload" title="刷新列表">⟳</button>
      </div>
      <div class="row">
        <button class="pri" id="pSave" style="flex:1">保存</button>
        <button id="pSaveAs" title="另存为新主体">另存为</button>
        <button class="danger" id="pDel">删除</button>
      </div>
      <div id="msg"></div>
    </div>

    <div class="sec">
      <h3>视差 <span class="n">整体幅度</span></h3>
      <div class="row">
        <label>幅度</label>
        <input type="range" id="ampRange" min="0" max="0.4" step="0.005" value="0.16">
        <span class="val" id="ampVal">0.16</span>
      </div>
      <div class="mini">各层实际位移 = 幅度 × 层深度 ÷ 最大深度。深度 0 = 钉在卡面不动，1 = 随卡面一起动，越大越靠前；<b style="color:#c9a86a">负数是反方向</b>（倾斜时往里沉）。</div>
    </div>

    <div class="sec">
      <h3>参考底图 <span class="n">仅预览，不导出</span></h3>
      <div class="row">
        <select id="bgSel"><option value="">— 无 —</option></select>
      </div>
    </div>

    <div class="sec">
      <h3>图层 <span class="n">上 = 靠前</span></h3>
      <div class="row">
        <button id="addBtn" style="flex:1">＋ 导入图层（可多选）</button>
      </div>
      <input type="file" id="fileIn" accept="image/*" multiple hidden>
      <div id="lyrList"><div id="emptyTip">还没有图层<br>导入抠好的透明 PNG / WebP</div></div>
    </div>

    <div class="sec" id="propSec" style="display:none">
      <h3>选中层 <span class="n" id="selName"></span></h3>
      <div class="row"><label>X</label><input type="range" id="lx" min="-50" max="150" step="0.5"><span class="val" id="lxv"></span></div>
      <div class="row"><label>Y</label><input type="range" id="ly" min="-50" max="150" step="0.5"><span class="val" id="lyv"></span></div>
      <div class="row"><label>缩放</label><input type="range" id="ls" min="2" max="300" step="0.5"><span class="val" id="lsv"></span></div>
      <div class="row"><label>透明度</label><input type="range" id="lo" min="0" max="100" step="1"><span class="val" id="lov"></span></div>
      <div class="row"><label>深度</label><input type="range" id="ld" min="-300" max="300" step="1"><span class="val" id="ldv"></span></div>
      <div class="row"><label>投影</label><input type="range" id="lsh" min="0" max="100" step="1"><span class="val" id="lshv"></span></div>
      <div class="row">
        <button class="sm" id="upBtn" style="flex:1">↑ 上移一层</button>
        <button class="sm" id="downBtn" style="flex:1">↓ 下移一层</button>
      </div>
    </div>
  </aside>
</div>

<script>
(function () {
  const CV = document.getElementById('cv');
  const ctx = CV.getContext('2d');
  const CW = CV.width, CH = CV.height;      // 内部基准分辨率（= meta.card_w/h）
  let META = { id: '', name: '', amp: 0.16, card_w: CW, card_h: CH, layers: [] };
  let IMGS = {};          // file -> HTMLImageElement
  let sel = -1;
  let view = 0;           // -1..1，模拟视角水平偏移
  let sway = false, swayT = 0;
  let follow = true;      // 鼠标跟随（关掉后鼠标移动不改视角，方便对齐构图）
  let cardMode = false;   // 卡牌模式：画面按 3D 倾斜显示，拖动=转卡
  let cardDrag = null;    // 卡牌模式下拖拽起点的 { x, v0 }
  let dragging = false, dragOff = { x: 0, y: 0 };
  let bgImg = null;

  const $ = (id) => document.getElementById(id);
  const msg = (t, cls) => { const m = $('msg'); m.textContent = t; m.className = cls || ''; };

  // ---------- 工具 ----------
  function maxDepth() {
    // ★ 绝对值：深度可为负（反向偏移），按最大值算会让「全负」的一组整体掉头
    let d = 0;
    for (const L of META.layers) if (L.visible) d = Math.max(d, Math.abs(L.depth));
    return d > 1e-6 ? d : 1;
  }
  // 层的素材地址有三种来源：
  //   _uploads/xxx.webp —— 刚上传、还没归档（/parallax/_uploads/xxx.webp）
  //   L00.webp          —— 已保存进某个主体（/parallax/<id>/L00.webp）
  function urlOf(L) {
    const f = String(L.file || '');
    if (!f) return '';
    if (f.indexOf('/') >= 0) return '/parallax/' + f.split('/').map(encodeURI).join('/');
    return '/parallax/' + encodeURI(window.__pid || '') + '/' + encodeURI(f);
  }
  function imgOf(L) {
    const u = urlOf(L);
    if (!IMGS[u]) {
      const im = new Image();
      im.onload = () => requestDraw();
      im.src = u;
      IMGS[u] = im;
    }
    return IMGS[u];
  }
  function layerW(L) { return CW * L.scale; }
  function layerH(L) {
    const im = imgOf(L);
    const ar = (L.src_h && L.src_w) ? (L.src_h / L.src_w)
             : (im && im.naturalWidth ? im.naturalHeight / im.naturalWidth : CH / CW);
    return layerW(L) * ar;
  }
  // 与 card.html 同一公式：位移 = view × amp × 卡宽 ÷ 最大深度 × 层深度
  function offsetX(L) { return view * META.amp * CW / maxDepth() * L.depth; }

  function draw() {
    ctx.clearRect(0, 0, CW, CH);
    // 卡面示意（参考底图 or 空卡框）
    ctx.save();
    const r = 26;
    ctx.beginPath();
    ctx.moveTo(r, 0); ctx.lineTo(CW - r, 0); ctx.quadraticCurveTo(CW, 0, CW, r);
    ctx.lineTo(CW, CH - r); ctx.quadraticCurveTo(CW, CH, CW - r, CH);
    ctx.lineTo(r, CH); ctx.quadraticCurveTo(0, CH, 0, CH - r);
    ctx.lineTo(0, r); ctx.quadraticCurveTo(0, 0, r, 0);
    ctx.closePath(); ctx.clip();
    if (bgImg) ctx.drawImage(bgImg, 0, 0, CW, CH);
    else {
      ctx.fillStyle = '#211a13'; ctx.fillRect(0, 0, CW, CH);
      ctx.strokeStyle = '#2f2519'; ctx.lineWidth = 2;
      for (let i = -CH; i < CW; i += 48) { ctx.beginPath(); ctx.moveTo(i, 0); ctx.lineTo(i + CH, CH); ctx.stroke(); }
    }
    ctx.restore();

    // 图层：数组 0 = 最后面
    for (let i = 0; i < META.layers.length; i++) {
      const L = META.layers[i];
      if (!L.visible) continue;
      const im = imgOf(L);
      if (!im.complete || !im.naturalWidth) continue;
      const w = layerW(L), h = layerH(L);
      const x = L.x * CW - w / 2 + offsetX(L);
      const y = L.y * CH - h / 2;
      ctx.save();
      if (L.shadow > 0.01) {
        ctx.shadowColor = 'rgba(16,10,6,' + (0.66 * L.shadow).toFixed(3) + ')';
        ctx.shadowBlur = Math.max(2, w * 0.014);
        ctx.shadowOffsetX = w * 0.008;
        ctx.shadowOffsetY = w * 0.008;
      }
      ctx.globalAlpha = L.opacity;
      ctx.drawImage(im, x, y, w, h);
      ctx.restore();
    }

    // 选中层描边
    if (sel >= 0 && META.layers[sel] && META.layers[sel].visible) {
      const L = META.layers[sel];
      const w = layerW(L), h = layerH(L);
      const x = L.x * CW - w / 2 + offsetX(L), y = L.y * CH - h / 2;
      ctx.save();
      ctx.strokeStyle = 'rgba(201,168,106,.9)'; ctx.lineWidth = 2;
      ctx.setLineDash([9, 7]);
      ctx.strokeRect(x, y, w, h);
      ctx.restore();
    }
    applyTilt();
  }

  let pending = false;
  function requestDraw() {
    if (pending) return;
    pending = true;
    requestAnimationFrame(() => { pending = false; draw(); });
  }

  // 视角（-1..1）统一入口：滑杆、鼠标、摆动、卡牌拖拽都走这里，免得各改各的
  function setView(v) {
    view = Math.max(-1, Math.min(1, v));
    $('viewRange').value = Math.round(view * 100);
    $('viewVal').textContent = Math.round(view * 100);
    requestDraw();
  }

  // 卡牌模式：把画布当成一张斜着的卡。
  // 41° ≈ card.html 里 tiltGroup.rotation.y = hover.x * 0.72（rad）的最大值，
  // 层内位移也是同一条公式（view × 幅度 × 卡宽）⇒ 这里就是成品卡上的观感。
  function applyTilt() {
    if (cardMode) {
      CV.style.transform = 'perspective(1200px) rotateY(' + (view * 41).toFixed(2) + 'deg)';
    } else if (CV.style.transform) {
      CV.style.transform = '';
    }
  }
  function setCardMode(on) {
    cardMode = !!on;
    CV.classList.toggle('card3d', cardMode);
    $('cardTog').classList.toggle('on', cardMode);
    $('cardChk').checked = cardMode;
    // 卡牌模式下画布是 3D 变换过的：getBoundingClientRect 会失真，拖图层会算歪。
    // 所以那个模式只用来「看」，拖动改成转卡（按 clientX 位移增量，不依赖坐标映射）。
    dragging = false;
    cardDrag = null;
    $('hintbar').textContent = cardMode
      ? '卡牌模式：左右拖动画面 = 转动卡牌 · 滚轮/拖动图层需切回平面模式 · 视角滑杆同样可用'
      : '拖动图层移动 · 滚轮缩放 · 鼠标左右移画面看视差 · 选中层可调深度/投影';
    requestDraw();
  }

  // ---------- 视差摆动 ----------
  function tick() {
    if (sway) {
      swayT += 0.012;
      view = Math.sin(swayT) * 0.95;
      $('viewRange').value = Math.round(view * 100);
      $('viewVal').textContent = Math.round(view * 100);
      requestDraw();
    }
    requestAnimationFrame(tick);
  }

  // ---------- 画布交互 ----------
  function toCanvas(e) {
    const r = CV.getBoundingClientRect();
    return {
      x: (e.clientX - r.left) / r.width * CW,
      y: (e.clientY - r.top) / r.height * CH,
    };
  }
  function hitTest(p) {
    for (let i = META.layers.length - 1; i >= 0; i--) {
      const L = META.layers[i];
      if (!L.visible) continue;
      const w = layerW(L), h = layerH(L);
      const x = L.x * CW - w / 2 + offsetX(L), y = L.y * CH - h / 2;
      if (p.x >= x && p.x <= x + w && p.y >= y && p.y <= y + h) return i;
    }
    return -1;
  }
  CV.addEventListener('pointerdown', (e) => {
    if (cardMode) {
      // 卡牌模式不编辑图层：左右拖动 = 转卡
      cardDrag = { x: e.clientX, v0: view };
      CV.classList.add('drag');
      try { CV.setPointerCapture(e.pointerId); } catch (_) {}
      return;
    }
    const p = toCanvas(e);
    const i = hitTest(p);
    if (i >= 0) {
      sel = i; syncPanel(); renderList();
      const L = META.layers[i];
      dragging = true;
      dragOff.x = p.x - (L.x * CW + offsetX(L));
      dragOff.y = p.y - L.y * CH;
      CV.setPointerCapture(e.pointerId);
      CV.style.cursor = 'grabbing';
    }
  });
  CV.addEventListener('pointermove', (e) => {
    if (cardMode) {
      if (cardDrag) {
        const w = CV.clientWidth || 1;
        setView(cardDrag.v0 + (e.clientX - cardDrag.x) / (w * 1.15));
      }
      return;
    }
    const p = toCanvas(e);
    if (dragging && sel >= 0) {
      const L = META.layers[sel];
      L.x = Math.round(((p.x - dragOff.x) / CW) * 1000) / 1000;
      L.y = Math.round(((p.y - dragOff.y) / CH) * 1000) / 1000;
      syncPanel(); requestDraw();
      return;
    }
    // 未拖动时：鼠标左右移动 = 换视角，实时看视差（可关）
    if (!sway && follow) {
      const v = Math.max(-1, Math.min(1, (p.x / CW - 0.5) * 2.4));
      view = v;
      $('viewRange').value = Math.round(v * 100);
      $('viewVal').textContent = Math.round(v * 100);
      requestDraw();
    }
  });
  CV.addEventListener('pointerup', (e) => {
    dragging = false; cardDrag = null;
    CV.classList.remove('drag');
    if (!cardMode) CV.style.cursor = 'crosshair';
    try { CV.releasePointerCapture(e.pointerId); } catch (_) {}
  });
  CV.addEventListener('wheel', (e) => {
    if (sel < 0 || cardMode) return;
    e.preventDefault();
    const L = META.layers[sel];
    const k = e.deltaY > 0 ? 0.96 : 1.042;
    L.scale = Math.max(0.02, Math.min(6, L.scale * k));
    syncPanel(); requestDraw();
  }, { passive: false });

  // ---------- 图层列表 ----------
  function renderList() {
    const box = $('lyrList');
    if (!META.layers.length) {
      box.innerHTML = '<div id="emptyTip">还没有图层<br>导入抠好的透明 PNG / WebP</div>';
      return;
    }
    const rows = [];
    for (let i = META.layers.length - 1; i >= 0; i--) {
      const L = META.layers[i];
      rows.push(
        '<div class="lyr' + (i === sel ? ' on' : '') + (L.visible ? '' : ' hid') + '" data-i="' + i + '">' +
        '<img src="' + urlOf(L) + '" alt="">' +
        '<span class="nm">' + esc(L.name) + '</span>' +
        '<span class="dp">×' + L.depth.toFixed(1) + '</span>' +
        '<span class="eye" data-eye="' + i + '" title="显示/隐藏">' + (L.visible ? '👁' : '🚫') + '</span>' +
        '</div>');
    }
    box.innerHTML = rows.join('');
    box.querySelectorAll('.lyr').forEach((el) => {
      el.addEventListener('click', (e) => {
        if (e.target.dataset.eye !== undefined) return;
        sel = +el.dataset.i; renderList(); syncPanel(); requestDraw();
      });
    });
    box.querySelectorAll('.eye').forEach((el) => {
      el.addEventListener('click', (e) => {
        e.stopPropagation();
        const i = +el.dataset.eye;
        META.layers[i].visible = !META.layers[i].visible;
        if (sel === i && !META.layers[i].visible) sel = -1;
        renderList(); syncPanel(); requestDraw();
      });
    });
    const n = META.layers.filter((L) => L.visible).length;
    $('statTag').textContent = META.layers.length + ' 层（' + n + ' 可见）';
  }
  function esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"]/g, (c) =>
      ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
  }

  function syncPanel() {
    const L = META.layers[sel];
    $('propSec').style.display = L ? '' : 'none';
    if (!L) return;
    $('selName').textContent = L.name;
    $('lx').value = L.x * 100; $('lxv').textContent = (L.x * 100).toFixed(1);
    $('ly').value = L.y * 100; $('lyv').textContent = (L.y * 100).toFixed(1);
    $('ls').value = L.scale * 100; $('lsv').textContent = (L.scale * 100).toFixed(0) + '%';
    $('lo').value = L.opacity * 100; $('lov').textContent = Math.round(L.opacity * 100);
    $('ld').value = L.depth * 100; $('ldv').textContent = L.depth.toFixed(2);
    $('lsh').value = L.shadow * 100; $('lshv').textContent = Math.round(L.shadow * 100);
  }

  // 选中层属性绑定
  const bindRange = (id, valId, fn, fmt) => {
    $(id).addEventListener('input', () => {
      if (sel < 0) return;
      fn(META.layers[sel], +$(id).value);
      $(valId).textContent = fmt(META.layers[sel]);
      renderList(); requestDraw();
    });
  };
  bindRange('lx', 'lxv', (L, v) => { L.x = v / 100; }, (L) => (L.x * 100).toFixed(1));
  bindRange('ly', 'lyv', (L, v) => { L.y = v / 100; }, (L) => (L.y * 100).toFixed(1));
  bindRange('ls', 'lsv', (L, v) => { L.scale = Math.max(0.02, v / 100); }, (L) => Math.round(L.scale * 100) + '%');
  bindRange('lo', 'lov', (L, v) => { L.opacity = v / 100; }, (L) => Math.round(L.opacity * 100));
  bindRange('ld', 'ldv', (L, v) => { L.depth = v / 100; }, (L) => L.depth.toFixed(2));
  bindRange('lsh', 'lshv', (L, v) => { L.shadow = v / 100; }, (L) => Math.round(L.shadow * 100));

  $('upBtn').onclick = () => {
    if (sel < 0 || sel >= META.layers.length - 1) return;
    const t = META.layers[sel]; META.layers[sel] = META.layers[sel + 1];
    META.layers[sel + 1] = t; sel++;
    renderList(); syncPanel(); requestDraw();
  };
  $('downBtn').onclick = () => {
    if (sel <= 0) return;
    const t = META.layers[sel]; META.layers[sel] = META.layers[sel - 1];
    META.layers[sel - 1] = t; sel--;
    renderList(); syncPanel(); requestDraw();
  };

  // ---------- 全局控件 ----------
  $('viewRange').addEventListener('input', () => {
    sway = false; $('swayBtn').textContent = '▶ 摆动预览';
    setView(+$('viewRange').value / 100);
  });
  $('swayBtn').onclick = () => {
    sway = !sway;
    $('swayBtn').textContent = sway ? '⏸ 停止' : '▶ 摆动预览';
    if (sway) swayT = Math.asin(Math.max(-1, Math.min(1, view)));
  };
  $('viewReset').onclick = () => {
    sway = false; $('swayBtn').textContent = '▶ 摆动预览';
    setView(0);
  };
  $('followChk').onchange = () => {
    follow = $('followChk').checked;
    $('followTog').classList.toggle('on', follow);
    if (!follow) msg('已暂停跟随鼠标：视角只用滑杆 / 摆动 / 卡牌模式拖动来调', '');
  };
  $('cardChk').onchange = () => setCardMode($('cardChk').checked);
  $('ampRange').addEventListener('input', () => {
    META.amp = +$('ampRange').value;
    $('ampVal').textContent = META.amp.toFixed(3).replace(/0+$/, '').replace(/\.$/, '');
    requestDraw();
  });

  // ---------- 导入 ----------
  $('addBtn').onclick = () => $('fileIn').click();
  $('fileIn').onchange = async (e) => {
    const files = [...e.target.files];
    e.target.value = '';
    if (files.length) await importFiles(files);
  };
  async function importFiles(files) {
    let first = -1;
    for (const f of files) {
      try {
        msg('上传 ' + f.name + ' …');
        const r = await fetch('/api/parallax/upload', {
          method: 'POST', body: f, headers: { 'X-File-Name': encodeURIComponent(f.name) },
        });
        const d = await r.json();
        if (!d.ok) throw new Error(d.error || '上传失败');
        const scale = Math.min(1, d.w / CW);
        const rec = {
          file: d.file, name: d.name, x: 0.5, y: 0.5, scale: Math.max(0.08, scale),
          opacity: 1, visible: true, depth: 1, shadow: 0, src_w: d.w, src_h: d.h,
        };
        META.layers.push(rec);
        if (first < 0) first = META.layers.length - 1;
        await new Promise((res) => {
          const im = new Image(); im.onload = res; im.onerror = res; im.src = urlOf(rec);
        });
      } catch (err) { msg('导入失败：' + err.message, 'err'); }
    }
    if (first >= 0) { sel = first; }
    renderList(); syncPanel(); requestDraw();
    if (first >= 0) msg('已导入 ' + files.length + ' 层', 'ok');
  }
  // 画布拖拽文件
  const box = $('canvasBox');
  let dragDepth = 0;
  box.addEventListener('dragenter', (e) => { e.preventDefault(); dragDepth++; box.classList.add('drop'); });
  box.addEventListener('dragover', (e) => { e.preventDefault(); });
  box.addEventListener('dragleave', (e) => {
    e.preventDefault(); dragDepth--;
    if (dragDepth <= 0) { dragDepth = 0; box.classList.remove('drop'); }
  });
  box.addEventListener('drop', async (e) => {
    e.preventDefault(); dragDepth = 0; box.classList.remove('drop');
    const files = [...e.dataTransfer.files].filter((f) => /^image\//.test(f.type));
    if (files.length) await importFiles(files);
  });

  // ---------- 参考底图 ----------
  fetch('/api/assets?type=backgrounds').then((r) => r.json()).then((d) => {
    const arr = (d.files || []).map((x) => (typeof x === 'string' ? x : (x.key || '')));
    const sel = $('bgSel');
    for (const name of arr) {
      // 素材包的 key 形如 "包id/子目录/名.png"（要走 /assets/ 的另一条路），这里只列内置素材
      if (!name || name.indexOf('/') >= 0) continue;
      const o = document.createElement('option');
      o.value = name; o.textContent = name;
      sel.appendChild(o);
    }
  }).catch(() => {});
  $('bgSel').onchange = () => {
    const n = $('bgSel').value;
    if (!n) { bgImg = null; requestDraw(); return; }
    const im = new Image();
    im.onload = () => { bgImg = im; requestDraw(); };
    im.onerror = () => { msg('底图加载失败：' + n, 'err'); bgImg = null; requestDraw(); };
    im.src = '/assets/backgrounds/' + encodeURI(n);
  };

  // ---------- 存取 ----------
  function metaPayload() {
    return {
      id: META.id, name: META.name || '视差主体', amp: META.amp,
      card_w: CW, card_h: CH,
      layers: META.layers.map((L) => ({
        file: L.file, name: L.name, x: L.x, y: L.y, scale: L.scale,
        opacity: L.opacity, visible: L.visible, depth: L.depth, shadow: L.shadow,
        src_w: L.src_w, src_h: L.src_h,
      })),
    };
  }
  async function saveItem(asNew) {
    if (!META.layers.length) { msg('至少放一层再保存', 'err'); return; }
    META.name = $('pName').value.trim() || '视差主体';
    const p = metaPayload();
    if (asNew) p.id = '';
    else if (META.id) p.id = META.id;
    try {
      msg('保存中…');
      const r = await fetch('/api/parallax/save', {
        method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(p),
      });
      const d = await r.json();
      if (!d.ok) throw new Error(d.error || '保存失败');
      META.id = d.id;
      window.__pid = d.id;
      (d.meta.layers || []).forEach((L, i) => { if (META.layers[i]) META.layers[i].file = L.file; });
      IMGS = {};   // 层文件已从 _uploads 归档成 L00.webp，URL 变了
      META.layers.forEach((L) => imgOf(L));
      renderList();
      await loadList(d.id);
      msg('已保存：' + d.id + ' · 拼装页「主体层」里能选到', 'ok');
    } catch (e) { msg('保存失败：' + e.message, 'err'); }
  }
  $('pSave').onclick = () => saveItem(false);
  $('pSaveAs').onclick = () => saveItem(true);

  async function loadList(selectId) {
    try {
      const r = await fetch('/api/parallax/list');
      const d = await r.json();
      const sel = $('pList');
      sel.innerHTML = '<option value="">— 新建 —</option>';
      for (const it of d.items || []) {
        const o = document.createElement('option');
        o.value = it.id; o.textContent = it.name + '（' + it.layer_count + ' 层）';
        sel.appendChild(o);
      }
      if (selectId) sel.value = selectId;
    } catch (_) {}
  }
  async function loadItem(id) {
    if (!id) {
      META = { id: '', name: '', amp: 0.16, card_w: CW, card_h: CH, layers: [] };
      window.__pid = '';
      sel = -1; IMGS = {}; $('pName').value = ''; $('ampRange').value = 0.16;
      $('ampVal').textContent = '0.16';
      renderList(); syncPanel(); requestDraw();
      return;
    }
    try {
      const r = await fetch('/api/parallax/item?id=' + encodeURIComponent(id));
      const d = await r.json();
      if (!d.ok) throw new Error(d.error);
      META = d.meta;
      META.card_w = CW; META.card_h = CH;
      window.__pid = id;
      IMGS = {};
      // 服务端返回的 file 是主体目录内的纯文件名（L00.webp），urlOf() 会补上 /parallax/<id>/
      sel = META.layers.length ? META.layers.length - 1 : -1;
      $('pName').value = META.name;
      $('ampRange').value = META.amp; $('ampVal').textContent = (+META.amp).toFixed(3);
      // 预热：先加载一遍，避免首帧空白
      META.layers.forEach((L) => imgOf(L));
      renderList(); syncPanel(); requestDraw();
      msg('已载入 ' + META.name, 'ok');
    } catch (e) { msg('载入失败：' + e.message, 'err'); }
  }
  $('pList').onchange = () => loadItem($('pList').value);
  $('pReload').onclick = () => loadList(META.id);
  $('pDel').onclick = async () => {
    if (!META.id) { msg('还没保存过', 'err'); return; }
    if (!confirm('删除视差主体「' + META.name + '」？不可撤销')) return;
    const r = await fetch('/api/parallax/delete', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ id: META.id }),
    });
    const d = await r.json();
    if (!d.ok) { msg('删除失败：' + d.error, 'err'); return; }
    msg('已删除', 'ok');
    await loadList('');
    await loadItem('');
  };

  // 自适应：画布按可用高度缩放
  function fit() {
    const bx = $('canvasBox');
    const pad = 28;
    const availW = bx.clientWidth - pad;
    const availH = bx.clientHeight - pad;
    const k = Math.min(availW / CW, availH / CH);
    CV.style.width = Math.round(CW * k) + 'px';
    CV.style.height = Math.round(CH * k) + 'px';
  }
  window.addEventListener('resize', fit);

  // ---------- 启动 ----------
  fit();
  loadList('');
  renderList();
  requestDraw();
  tick();
  window.__parallaxAPI = {
    meta: () => META, sel: () => sel, view: (v) => { view = v; requestDraw(); },
    // 只读：当前视角 / 是否卡牌模式（自动验证与外部脚调用）
    getView: () => view, cardMode: () => cardMode,
    setCardMode: (on) => setCardMode(on), setFollow: (on) => { follow = !!on; $('followChk').checked = follow; $('followTog').classList.toggle('on', follow); },
    addLayer: (rec) => { META.layers.push(rec); renderList(); requestDraw(); },
    save: saveItem, load: loadItem,
  };
})();
</script>
</body>
</html>
"""
