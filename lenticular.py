"""光栅卡（lenticular / 立体变画卡）效果。

物理原理：卡面是一排竖向柱状透镜（lenslet），每条透镜下面压着 N 条图像条带。
换视角时透镜把不同条带折射进眼睛 —— 所以倾斜到不同角度会看到不同画面。
本模块用 WebGL 复刻这一现象：

  * 每像素按透镜坐标算「折射后的条带序号」，在 N 张图之间取相邻两张做混合；
    混合区间宽度由「切换锐度」控制（0 = 硬切变画卡，1 = 全渐变）。
  * 每条透镜加一点相位抖动 → 真实卡的条带错位感（切换时呈竖向带状，而非整卡齐刷）。
  * 视角（朝向法线的 facing）+ 卡片轻微弯曲 → 相位随像素变化，
    于是倾斜时出现「光带扫过卡面」的真实观感。
  * 柱状透镜阵列的镜面反射被近似成沿卡片 X 方向的扫光带，叠在棱线脊上 —— 就是
    视频里那种一道亮光斜掠过的塑料感。
  * 可选「立体」模式：用抠图得到主体遮罩，按深度做多层视差，生成 N 帧真正的
    3D 光栅序列（倾斜时主体相对背景位移 = 立体出屏感）。

CLI:
    python lenticular.py --images a.png b.png c.png --name demo
    python lenticular.py --images a.png --mode 3d --frames 5 --name demo3d

产物：assets/lenticular/<name>/lenti.html + 帧图（相对引用，双击即开）。
"""
from __future__ import annotations

import argparse
import json
import math
import re
import shutil
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
LENTI_ROOT = PROJECT_ROOT / "assets" / "lenticular"
THREE_SRC_REL = "../../shared/lib/three.min.js"
THREE_SRC_ABS = "/shared/lib/three.min.js"

# 画幅比例（宽/高）。3:4 最接近视频里那张卡；也支持 2:3 与公版卡比例。
ASPECTS = {"3:4": 0.75, "2:3": 0.6666667, "1:1": 1.0, "card": 1465 / 2048}
DEFAULT_ASPECT = 0.75
FRAME_W = 1200          # 帧图输出宽（3:4 → 1200x1600）

DEFAULT_OPTS = {
    "angle": 0.0,       # 条纹方向（度）：0=竖直，90=水平，其余为斜向
    "lens": 140,        # 每卡宽度的透镜条数
    "stripe": 0.50,     # 棱纹暗谷强度
    "sheen": 0.95,      # 镜面扫光强度
    "sheenPow": 3.2,    # 扫光锐度
    "switch": 0.68,     # 切换柔和度 0=硬切 1=全渐变
    "jitter": 0.02,     # 每条透镜的相位错位（必须远小于切换窗口，否则撕成竖条）
    "sweep": 2.4,       # 视角增益（透视带来的相位梯度）
    "span": 0.0,        # 倾角行程，0 = 自动 (N-1)/2
    "bend": 0.018,      # 卡片弯曲量（轻微弓形 → 扫光成带状）
    "gloss": 1.0,       # 整体高光
    "grain": 0.018,     # 印刷颗粒
    "vignette": 0.09,   # 边缘压暗
    "tiltMax": 0.40,    # 满倾角（弧度）
    "auto": True,       # 自动扫动
    "autoSpeed": 0.34,
    "phase": 0.0,       # 手动相位偏移（图单位）
    "bg": "#0d0b09",
}


# ---------------------------------------------------------------- 图像预处理

def _cover(im, w: int, h: int):
    """等比铺满再中心裁切（不做拉伸）。"""
    from PIL import Image
    im = im.convert("RGB")
    sw, sh = im.size
    s = max(w / sw, h / sh)
    nw, nh = max(w, int(round(sw * s))), max(h, int(round(sh * s)))
    im = im.resize((nw, nh), Image.LANCZOS)
    x = (nw - w) // 2
    y = (nh - h) // 2
    return im.crop((x, y, x + w, y + h))


def frame_size(aspect: float) -> tuple[int, int]:
    return FRAME_W, int(round(FRAME_W / aspect))


def load_frame(path, aspect: float = DEFAULT_ASPECT):
    from PIL import Image
    im = Image.open(path)
    return _cover(im, *frame_size(aspect))


_MATTE_DIR = LENTI_ROOT / "_mat"
_ENGINE = None


def _engine():
    """抠图引擎单例 —— 每次新建 RembgEngine 都会重新载入 onnx 会话（数秒级）。"""
    global _ENGINE
    if _ENGINE is None:
        from engine import create_engine
        _ENGINE = create_engine("local")
    return _ENGINE


def _matte(im, cache_key: str | None = None):
    """主体遮罩（255=主体）。失败时退回「整图都是主体」。"""
    from PIL import Image
    if cache_key:
        cf = _MATTE_DIR / (cache_key + ".png")
        if cf.exists():
            try:
                a = Image.open(cf)
                if a.size == im.size:
                    return a.convert("L")
            except Exception:
                pass
    else:
        cf = None
    try:
        cut = _engine().remove_background(im.convert("RGB"))
        a = cut.split()[3]
        if a.size != im.size:
            a = a.resize(im.size, Image.LANCZOS)
        # 遮罩全白 = 没抠出来（引擎降级），认为无深度信息
        if a.getextrema()[0] > 250:
            return None
        if cf is not None:
            _MATTE_DIR.mkdir(parents=True, exist_ok=True)
            a.save(cf)
        return a
    except Exception:
        return None


def _inpaint(img, mask, passes: int = 5):
    """把 mask 区域用周围像素多尺度填平（金字塔模糊填补），用作背景底板。

    只露出几像素的边缘，模糊填补完全够用，而且天然像景深虚化。
    """
    from PIL import Image, ImageFilter
    base = img.convert("RGB").copy()
    # 半分辨率做金字塔（板子最终被主体盖住，细节无所谓，速度差一倍）
    hw, hh = max(2, base.width // 2), max(2, base.height // 2)
    small0 = base.resize((hw, hh), Image.LANCZOS)
    m = mask.filter(ImageFilter.GaussianBlur(1.0)).resize((hw, hh), Image.LANCZOS)
    cur = small0
    for k in range(4, -1, -1):
        f = 2 ** k
        sw, sh = max(2, hw // f), max(2, hh // f)
        small = cur.resize((sw, sh), Image.LANCZOS)
        sm = m.resize((sw, sh), Image.LANCZOS)
        blurred = small.filter(ImageFilter.GaussianBlur(max(1.0, 2.4 * 8 / f)))
        small = Image.composite(blurred, small, sm)
        cur = small.resize((hw, hh), Image.LANCZOS)
    return Image.composite(cur.resize(base.size, Image.LANCZOS), base,
                           mask.filter(ImageFilter.GaussianBlur(2.0)))


def _shift(im, dx: float, dy: float):
    from PIL import Image
    return im.transform(im.size, Image.AFFINE, (1, 0, -dx, 0, 1, -dy),
                        resample=Image.BICUBIC)


def parallax_frames(path, n: int = 5, aspect: float = DEFAULT_ASPECT,
                    depth: float = 1.0, pop: float = 0.55,
                    bg_shift: float = 0.22):
    """把一张画生成 n 帧横向视差序列（模拟立体光栅）。

    depth: 位移幅度（占画宽的百分比，1.0 ≈ 2.6%）
    pop:   主体随视差缩放（负值向内、正值向外凸）
    """
    from PIL import Image
    base = load_frame(path, aspect)
    W, H = base.size
    try:
        st = Path(path).stat()
        key = "%s_%dx%d_%d" % (Path(path).stem, W, H, int(st.st_mtime))
        key = re.sub(r"[^\w\-]+", "_", key)[:80]
    except Exception:
        key = None
    matte = _matte(base, key)
    frames = []
    if matte is None:
        # 没抠出主体：退化成轻微的整体斜切（仍有一点伪 3D）
        for i in range(n):
            u = i / max(1, n - 1) - 0.5
            big = base.resize((int(W * 1.12), int(H * 1.12)), Image.LANCZOS)
            dx = u * W * 0.026 * depth
            frames.append(big.crop((int(big.width * 0.06 - dx), int(big.height * 0.06),
                                    int(big.width * 0.06 - dx) + W,
                                    int(big.height * 0.06) + H)))
        return frames

    from PIL import ImageFilter
    plate = _inpaint(base, matte)                      # 背景底板（主体区被填平）
    fg = base.convert("RGBA")
    fg.putalpha(matte.filter(ImageFilter.GaussianBlur(1.2)))
    S = 1.10                                            # 放大工作画布，位移不露边
    bw, bh = int(W * S), int(H * S)
    plate_b = plate.resize((bw, bh), Image.LANCZOS)
    fg_b = fg.resize((bw, bh), Image.LANCZOS)
    amp = W * 0.026 * depth
    for i in range(n):
        u = i / max(1, n - 1) - 0.5                     # -0.5 .. +0.5
        dx_fg = u * 2.0 * amp
        dx_bg = dx_fg * bg_shift
        canvas = Image.new("RGB", (bw, bh), (0, 0, 0))
        canvas.paste(_shift(plate_b, dx_bg, 0), (0, 0))
        f = _shift(fg_b, dx_fg, 0)
        if pop:
            sc = 1.0 + pop * 0.02 * u * 2.0
            nw, nh = int(round(bw * sc)), int(round(bh * sc))
            f = f.resize((nw, nh), Image.LANCZOS)
            f = f.crop(((nw - bw) // 2, (nh - bh) // 2,
                        (nw - bw) // 2 + bw, (nh - bh) // 2 + bh))
        canvas = canvas.convert("RGBA")
        canvas.alpha_composite(f)
        x0 = (bw - W) // 2
        y0 = (bh - H) // 2
        frames.append(canvas.crop((x0, y0, x0 + W, y0 + H)).convert("RGB"))
    return frames


def save_frames(frames, out_dir: Path, fmt: str = "webp", quality: int = 92):
    out_dir.mkdir(parents=True, exist_ok=True)
    names = []
    for i, fr in enumerate(frames):
        name = f"f{i:02d}.{fmt}"
        if fmt == "webp":
            fr.save(out_dir / name, "WEBP", quality=quality, method=4)
        else:
            fr.save(out_dir / name)
        names.append(name)
    return names


# ---------------------------------------------------------------- 交错印刷图

def interlace(paths, out_path, lpi: float = 75.0, dpi: float = 600.0,
              aspect: float = DEFAULT_ASPECT):
    """生成真正的光栅印刷原图：按 lpi/dpi 把 N 张图切成竖条交错。

    这是拿去印刷用的文件 —— 上机时光栅膜要跟条纹对齐（按 lpi 买对应膜）。
    """
    import numpy as np
    from PIL import Image

    W, H = frame_size(aspect)
    # 印刷尺寸按 3:4 卡宽 5 英寸估：像素宽 = 5*dpi
    pw = int(round(5.0 * dpi))
    ph = int(round(pw / aspect))
    imgs = [np.asarray(load_frame(p, aspect).resize((pw, ph), Image.LANCZOS)) for p in paths]
    n = len(imgs)
    lens_px = dpi / lpi                    # 每条透镜的像素宽
    out = np.zeros((ph, pw, 3), np.uint8)
    for y0 in range(0, ph, 256):
        y1 = min(ph, y0 + 256)
        cols = np.arange(pw)
        # 每列属于第 k 条透镜的第 j 个条带
        k = np.floor(cols / lens_px).astype(np.int64)
        sub = np.clip(((cols - k * lens_px) / lens_px * n).astype(np.int64), 0, n - 1)
        for j in range(n):
            sel = sub == j
            if not sel.any():
                continue
            out[y0:y1, sel] = imgs[j][y0:y1][:, sel]
    Image.fromarray(out).save(out_path, quality=95)
    return Path(out_path)


# ---------------------------------------------------------------- 网页

_SHELL = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>__TITLE__</title>
<style>
:root {
  --bg:#171310; --panel:#221b16; --panel2:#2a211a; --line:#3d2f24;
  --gold:#e3b95c; --gold2:#f3d48a; --text:#ece4d6; --muted:#a99b86;
}
* { box-sizing:border-box; margin:0; padding:0; }
body {
  background:#0b0908; color:var(--text);
  font-family:"Microsoft YaHei","PingFang SC",system-ui,sans-serif;
  min-height:100vh; overflow:hidden;
}
#app { display:flex; height:100vh; }
#stage {
  position:relative; flex:1 1 auto; min-width:0;
  background:radial-gradient(1100px 780px at 50% 34%, #241d17 0%, #14100d 52%, #080706 100%);
  cursor:grab; overflow:hidden;
}
#stage.drag { cursor:grabbing; }
#c { display:block; width:100%; height:100%; }
#hud {
  position:absolute; left:22px; bottom:18px; display:flex; gap:10px; align-items:center;
  font-size:12px; color:#8d8072; letter-spacing:.5px; pointer-events:none;
}
#hud b { color:var(--gold); font-weight:600; }
#idx {
  position:absolute; right:22px; top:18px; font-size:12px; color:#8d8072;
  letter-spacing:1px; pointer-events:none;
}
#idx b { color:var(--gold2); font-size:16px; letter-spacing:2px; }

aside {
  width:316px; flex:0 0 316px; background:var(--panel); border-left:1px solid var(--line);
  overflow-y:auto; padding:18px 16px 40px;
}
aside h1 { font-size:15px; color:var(--gold2); letter-spacing:1.5px; font-weight:600; }
aside .sub { font-size:11.5px; color:var(--muted); line-height:1.7; margin-top:6px; }
.sec { margin-top:18px; border-top:1px solid var(--line); padding-top:14px; }
.sec h2 {
  font-size:12px; color:#9c8f7c; letter-spacing:1px; font-weight:600;
  text-transform:uppercase; margin-bottom:10px;
}
.row { display:flex; align-items:center; justify-content:space-between; font-size:12.5px; color:#cfc4b3; margin-bottom:4px; }
.row span.v { color:var(--gold); font-variant-numeric:tabular-nums; font-size:12px; }
input[type=range] {
  -webkit-appearance:none; width:100%; height:16px; background:transparent; margin-bottom:9px;
}
input[type=range]::-webkit-slider-runnable-track { height:3px; background:#3a2d22; border-radius:2px; }
input[type=range]::-webkit-slider-thumb {
  -webkit-appearance:none; width:12px; height:12px; border-radius:50%; background:var(--gold);
  margin-top:-4.5px; box-shadow:0 0 0 3px rgba(227,185,92,.14);
}
.btn {
  display:inline-flex; align-items:center; justify-content:center; gap:6px;
  padding:8px 12px; border-radius:8px; border:1px solid var(--line);
  background:var(--panel2); color:#cfc4b3; font-size:12.5px; cursor:pointer;
  transition:.15s; user-select:none;
}
.btn:hover { border-color:#5a4632; color:var(--text); }
.btn.on { border-color:var(--gold); color:var(--gold2); background:#32281c; }
.btnrow { display:flex; gap:8px; flex-wrap:wrap; }
textarea {
  width:100%; height:84px; resize:vertical; background:#1b1613; color:var(--text);
  border:1px solid var(--line); border-radius:8px; padding:8px 9px; font-size:12px;
  font-family:Consolas,"Courier New",monospace; line-height:1.55;
}
textarea:focus { outline:none; border-color:#5a4632; }
.chips { display:flex; flex-direction:column; gap:5px; margin-top:9px; }
.chip {
  display:flex; justify-content:space-between; gap:8px; font-size:11.5px;
  color:#cfc4b3; background:var(--panel2); border:1px solid var(--line);
  border-radius:6px; padding:5px 8px; word-break:break-all; line-height:1.4;
}
.chip b { color:#8d8072; cursor:pointer; flex:0 0 auto; }
.chip b:hover { color:#e07a5f; }
.thumbs { display:flex; gap:8px; flex-wrap:wrap; margin-top:4px; }
.thumbs img {
  width:52px; height:69px; object-fit:cover; border-radius:5px;
  border:1px solid var(--line); background:#1b1613;
}
.thumbs .ph {
  width:52px; height:69px; border-radius:5px; border:1px dashed #4a3a2c;
  color:#6d6154; font-size:18px; display:flex; align-items:center; justify-content:center;
}
</style>
</head>
<body>
<div id="app">
  <section id="stage">
    <canvas id="c"></canvas>
    <div id="idx"><b>1</b> / __N__</div>
    <div id="hud">拖动或移动鼠标倾斜卡片 · <b>光栅变画</b></div>
  </section>
  <aside>
    <h1>光栅卡 · Lenticular</h1>
    <div class="sub">柱状透镜阵列 + N 张图像交错。倾斜时透镜把不同条带折射进眼睛，
      于是画面切换；棱纹带来塑料反光与立体感。</div>
    __TOP__
    <div class="thumbs" id="thumbs"></div>
    <div class="sec" id="ctl"></div>
    <div class="sec">
      <h2>展示</h2>
      <div class="btnrow">
        <div class="btn on" id="bAuto">自动扫动</div>
        <div class="btn" id="bReset">回到正面</div>
      </div>
    </div>
    __EXTRAS__
  </aside>
</div>
<script src="__THREE_SRC__"></script>
<script>
const CFG = __CONFIG__;

const VERT = `
uniform float uBend, uW;
varying vec2 vUv;
varying vec3 vWN, vWP, vWX, vWY;
void main() {
  vUv = uv;
  vec3 p = position;
  float cx = (uv.x - 0.5) * 2.0;
  p.z += uBend * (1.0 - cx * cx);
  float dzdx = -4.0 * uBend * cx / uW;
  vec3 n = normalize(vec3(-dzdx, 0.0, 1.0));
  vec4 wp = modelMatrix * vec4(p, 1.0);
  vWP = wp.xyz;
  vWN = normalize(mat3(modelMatrix) * n);
  vWX = normalize((modelMatrix * vec4(1.0, 0.0, 0.0, 0.0)).xyz);
  vWY = normalize((modelMatrix * vec4(0.0, 1.0, 0.0, 0.0)).xyz);
  gl_Position = projectionMatrix * viewMatrix * wp;
}`;

const FRAG = `
precision highp float;
varying vec2 vUv;
varying vec3 vWN, vWP, vWX, vWY;
uniform sampler2D uT0, uT1, uT2, uT3, uT4, uT5;
uniform float uN, uPhase, uSweep, uLens, uStripe, uSheen, uSheenPow;
uniform float uSwitch, uJitter, uAsp, uGloss, uGrain, uVig, uR;
uniform float uAngle, uSpan;
uniform vec2 uTexel;

float hash11(float p) {
  p = fract(p * 0.1031);
  p *= p + 33.33;
  p *= p + p;
  return fract(p);
}

vec3 frameAt(float idx, vec2 uv) {
  float k = mod(idx, uN);
  if (k < 0.5) return texture2D(uT0, uv).rgb;
  if (k < 1.5) return texture2D(uT1, uv).rgb;
  if (k < 2.5) return texture2D(uT2, uv).rgb;
  if (k < 3.5) return texture2D(uT3, uv).rgb;
  if (k < 4.5) return texture2D(uT4, uv).rgb;
  return texture2D(uT5, uv).rgb;
}

float sdBox(vec2 p, vec2 b) {
  vec2 d = abs(p) - b;
  return length(max(d, 0.0)) + min(max(d.x, d.y), 0.0);
}

void main() {
  // ---- 圆角卡片剪裁 ----
  vec2 q = (vUv - 0.5) * vec2(uAsp, 1.0);
  float halfW = uAsp * 0.5;
  float d = sdBox(q, vec2(halfW - uR, 0.5 - uR)) - uR;
  float aa = max(fwidth(d), 1e-5);
  float alpha = 1.0 - smoothstep(-aa, aa, d);
  if (alpha <= 0.001) discard;

  vec3 nrm = normalize(vWN);
  vec3 vd = normalize(cameraPosition - vWP);
  float facing = clamp(dot(nrm, vd), 0.0, 1.0);
  vec2 uv = vUv;

  // ---- 透镜坐标：每像素落在第几条透镜、透镜内第几个条带 ----
  // 条纹方向由 uAngle 控制（0=竖条纹，90°=横条纹，其余为斜向）：
  //   axP = 条纹推进方向（垂直于透镜轴）—— 透镜编号沿它递增
  //   axL = 透镜轴（条纹走向）
  float ca = cos(uAngle);
  float sa = sin(uAngle);
  vec2 axP = vec2(ca, -sa);
  vec2 axL = vec2(sa, ca);
  vec2 qc = (uv - 0.5) * vec2(uAsp, 1.0);
  float lx = dot(qc, axP) * (uLens / uAsp);
  float lid = floor(lx);
  float sub = fract(lx);
  float h = hash11(lid + 0.5);

  // 相位：视角在「条纹推进方向」上的有符号投影（画内单位 ±1）。
  // 旧版靠 JS 传 rotY 驱动，条纹一转 90° 就变成「横条纹却在横向倾斜时变画」——
  // 改成在 shader 里按条纹轴投影，任意角度都对。
  vec3 axPw = normalize(vWX * axP.x + vWY * axP.y);
  vec3 axLw = normalize(vWX * axL.x + vWY * axL.y);
  float sv = dot(vd, axPw);
  // uSpan（倾角行程，自动 = (N-1)/2）× 2.2 ≈ 满倾角走过 N-1 张图；(1-facing) 项给逐像素梯度
  float ph = uPhase + sv * uSpan * 2.2 + (1.0 - facing) * uSweep;
  float t = ph + (h - 0.5) * uJitter;
  float i0 = floor(t);
  float w = fract(t);

  // 切换锐度：0 → 硬切（经典变画卡），1 → 全渐变（渐进显影）
  float sw = max(uSwitch, 0.002);
  w = smoothstep(0.5 - 0.5 * sw, 0.5 + 0.5 * sw, w);

  vec3 c0 = frameAt(i0, uv);
  vec3 c1 = frameAt(i0 + 1.0, uv);
  vec3 col = mix(c0, c1, w);
  float trans = 1.0 - abs(w * 2.0 - 1.0);

  // ---- 棱纹：每透镜一道余弦，AA 后过密会退化成平场，避免摩尔纹反噬 ----
  float laa = clamp(fwidth(lx) * 0.70, 0.0, 1.0);
  float line = 0.5 + 0.5 * cos(6.28318530718 * sub);
  line = mix(line, 0.5, laa);
  float ridge = pow(line, 7.0);

  // ---- 扫光：柱状透镜的镜面反射近似成斜掠的高光带 ----
  vec3 refl = reflect(-vd, nrm);
  vec3 pl = refl - nrm * dot(refl, nrm);
  float pll = length(pl);
  // ★ pl 是未归一化的面内反射分量，模长 ∝ sin(2θ) —— 正好当「倾斜量」用：
  //   归一化会让 a1 = cos(方向夹角)，一点点倾斜就跳到 1（退化成开关），
  //   于是 0° 死平、9° 就满强度泛白。必须拿模长做包络，才有渐变扫光。
  // 扫光方向跟着条纹转（Pw/Lw 即旧版的 X/Y；angle=0 时与旧版完全一致）
  vec3 brushDir = normalize(axPw * 0.942 + axLw * 0.335);
  vec3 crossDir = normalize(axPw * -0.335 + axLw * 0.942);
  float g1 = clamp(abs(dot(pl, brushDir)) * 2.6, 0.0, 1.0);
  float g2 = clamp(abs(dot(pl, crossDir)) * 2.6, 0.0, 1.0);
  float band = pow(g1, uSheenPow);
  float core = pow(g1, uSheenPow * 3.0);
  float band2 = pow(g2, uSheenPow * 2.0) * 0.45;

  col *= 1.0 - uStripe * (1.0 - line) * 0.42;
  col += uStripe * 0.055 * ridge;                  // 棱脊微高光，棱纹才立得住
  col *= 1.0 + (0.10 * band + 0.05 * core) * uSheen;
  col += vec3(1.0, 0.992, 0.972) * uSheen * uGloss
         * (band * (0.09 + 0.18 * line) + core * 0.22
            + band2 * (0.055 + 0.10 * line)) * mix(0.62, 1.0, ridge);
  col += 0.03 * trans;

  // ---- 印刷质感 ----
  if (uGrain > 0.0) {
    float g = hash11(dot(uv, vec2(1024.0, 1536.0)));
    col += (g - 0.5) * uGrain;
  }
  float vig = length(q) / (halfW * 1.30);
  col *= 1.0 - uVig * pow(clamp(vig, 0.0, 1.0), 2.0);
  // 边缘内投影（卡边压深一点，避免看起来像屏幕）
  col *= mix(1.0, 0.90, smoothstep(-0.013, 0.0, d));

  // 覆膜对比：塑料面比原画更「通透」，靠轻微 S 曲线而不是整体提亮（提亮会发灰）
  col = mix(vec3(0.42), col, 1.07);
  col += 0.006;

  gl_FragColor = vec4(clamp(col, 0.0, 1.0), alpha);
}`;

const RIM_FRAG = `
precision highp float;
varying vec2 vUv;
uniform float uAsp, uR, uScale;
float sdBox(vec2 p, vec2 b) {
  vec2 d = abs(p) - b;
  return length(max(d, 0.0)) + min(max(d.x, d.y), 0.0);
}
void main() {
  vec2 q = (vUv - 0.5) * vec2(uAsp, 1.0) * uScale;
  float halfW = uAsp * 0.5 * uScale;
  float d = sdBox(q, vec2(halfW - uR, 0.5 * uScale - uR)) - uR;
  float a = 1.0 - smoothstep(-0.002, 0.001, d);
  if (a <= 0.001) discard;
  gl_FragColor = vec4(vec3(0.055, 0.045, 0.038), a);
}`;

// ------------------------------------------------------------ 场景
const stage = document.getElementById('stage');
const canvas = document.getElementById('c');
const renderer = new THREE.WebGLRenderer({canvas, antialias: true, alpha: true});
renderer.setPixelRatio(Math.min(devicePixelRatio || 1, 2));
if ('outputColorSpace' in renderer) renderer.outputColorSpace = THREE.SRGBColorSpace;
const scene = new THREE.Scene();
const camera = new THREE.PerspectiveCamera(26, 1, 0.1, 100);
camera.position.set(0, 0, 6.2);

const ASP = CFG.aspect;
const CARD_H = 2.0;
const CARD_W = CARD_H * ASP;

const group = new THREE.Group();     // 倾斜载体
scene.add(group);

const uniforms = {
  uBend: {value: CFG.opts.bend}, uW: {value: CARD_W},
  uN: {value: 1}, uPhase: {value: 0}, uSweep: {value: CFG.opts.sweep},
  uLens: {value: CFG.opts.lens}, uStripe: {value: CFG.opts.stripe},
  uSheen: {value: CFG.opts.sheen}, uSheenPow: {value: CFG.opts.sheenPow},
  uSwitch: {value: CFG.opts.switch}, uJitter: {value: CFG.opts.jitter},
  uAngle: {value: (CFG.opts.angle || 0) * Math.PI / 180}, uSpan: {value: CFG.opts.span},
  uAsp: {value: ASP}, uGloss: {value: CFG.opts.gloss}, uGrain: {value: CFG.opts.grain},
  uVig: {value: CFG.opts.vignette}, uR: {value: 0.014},
  uTexel: {value: new THREE.Vector2(1 / 1200, 1 / 1600)},
  uT0: {value: null}, uT1: {value: null}, uT2: {value: null},
  uT3: {value: null}, uT4: {value: null}, uT5: {value: null},
};

const geo = new THREE.PlaneGeometry(CARD_W, CARD_H, 64, 64);
const mat = new THREE.ShaderMaterial({
  vertexShader: VERT, fragmentShader: FRAG, uniforms,
  transparent: true, side: THREE.DoubleSide,
});
const card = new THREE.Mesh(geo, mat);
group.add(card);

// 卡边（薄薄一圈深色，倾斜时才有存在感）
const rimMat = new THREE.ShaderMaterial({
  vertexShader: VERT, fragmentShader: RIM_FRAG,
  uniforms: {uAsp: {value: ASP}, uR: {value: 0.014}, uScale: {value: 1.005},
             uBend: uniforms.uBend, uW: uniforms.uW},
  transparent: true, depthWrite: false,
});
const rim = new THREE.Mesh(geo, rimMat);
rim.position.z = -0.006;
group.add(rim);

// 背景柔光（衬托卡片，模拟桌面反射）
const shadowCv = document.createElement('canvas');
shadowCv.width = shadowCv.height = 256;
{
  const g = shadowCv.getContext('2d');
  const grd = g.createRadialGradient(128, 128, 8, 128, 128, 126);
  grd.addColorStop(0, 'rgba(0,0,0,0.55)');
  grd.addColorStop(0.55, 'rgba(0,0,0,0.26)');
  grd.addColorStop(1, 'rgba(0,0,0,0)');
  g.fillStyle = grd; g.fillRect(0, 0, 256, 256);
}
const shadowTex = new THREE.CanvasTexture(shadowCv);
const shadow = new THREE.Mesh(
  new THREE.PlaneGeometry(CARD_W * 1.7, CARD_H * 1.5),
  new THREE.MeshBasicMaterial({map: shadowTex, transparent: true, opacity: 0.9, depthWrite: false})
);
shadow.position.z = -0.55;
scene.add(shadow);

// ------------------------------------------------------------ 贴图加载
const loader = new THREE.TextureLoader();
const BLACK = new THREE.DataTexture(new Uint8Array([0, 0, 0, 255]), 1, 1);
BLACK.needsUpdate = true;
const SLOT = ['uT0', 'uT1', 'uT2', 'uT3', 'uT4', 'uT5'];
for (const k of SLOT) uniforms[k].value = BLACK;

function loadTex(src) {
  return new Promise((res) => {
    loader.load(src, (t) => {
      if ('colorSpace' in t) t.colorSpace = THREE.SRGBColorSpace;
      else if ('encoding' in t) t.encoding = THREE.sRGBEncoding;
      t.minFilter = THREE.LinearMipmapLinearFilter;
      t.magFilter = THREE.LinearFilter;
      t.generateMipmaps = true;
      t.anisotropy = renderer.capabilities.getMaxAnisotropy();
      res(t);
    }, undefined, () => res(null));
  });
}

async function setImages(list) {
  const texs = await Promise.all(list.slice(0, 6).map((it) => loadTex(it.src)));
  const ok = texs.filter(Boolean);
  const n = Math.max(1, ok.length);
  for (let i = 0; i < 6; i++) uniforms[SLOT[i]].value = ok[i % ok.length] || BLACK;
  uniforms.uN.value = n;
  if (ok[0] && ok[0].image) {
    uniforms.uTexel.value.set(1 / ok[0].image.width, 1 / ok[0].image.height);
  }
  document.getElementById('idx').innerHTML = '<b id="cur">1</b> / ' + n;
  document.getElementById('thumbs').innerHTML = list.slice(0, 6).map(
    (it) => '<img src="' + it.src + '" title="' + (it.name || '') + '">'
  ).join('');
  state.n = n;
  state.span = (n - 1) / 2;
  applySpan();
}

// ------------------------------------------------------------ 交互
const state = {
  auto: CFG.opts.auto, drag: false, px: 0, py: 0,
  rotX: 0, rotY: 0, tRotX: 0, tRotY: 0,
  last: performance.now(), n: 1, span: CFG.opts.span, phase: CFG.opts.phase || 0,
};

function applySpan() {
  const s = document.querySelector('[data-k="span"]');
  if (s) s.value = state.span;
  const el = document.getElementById('v_span');
  if (el) el.textContent = state.span.toFixed(1);
}

function fit() {
  const r = stage.getBoundingClientRect();
  renderer.setSize(r.width, r.height, false);
  camera.aspect = r.width / Math.max(1, r.height);
  // 让卡片在任意窗口比例下都留出边距
  const vFov = camera.fov * Math.PI / 180;
  const fitH = (CARD_H * 1.34) / (2 * Math.tan(vFov / 2));
  const fitW = (CARD_W * 1.34) / (2 * Math.tan(vFov / 2) * camera.aspect);
  camera.position.z = Math.max(fitH, fitW);
  camera.updateProjectionMatrix();
}
addEventListener('resize', fit);

stage.addEventListener('pointermove', (e) => {
  const r = stage.getBoundingClientRect();
  const nx = (e.clientX - r.left) / r.width * 2 - 1;
  const ny = (e.clientY - r.top) / r.height * 2 - 1;
  state.tRotY = nx * CFG.opts.tiltMax;
  state.tRotX = -ny * CFG.opts.tiltMax * 0.62;
  state.last = performance.now();
  state.px = nx; state.py = ny;
});
stage.addEventListener('pointerleave', () => { state.last = 0; });

// ------------------------------------------------------------ 控制面板
const CTRL = [
  {k: 'angle', label: '条纹方向', min: 0, max: 180, step: 1, d: 0, fmt: (v) => v.toFixed(0) + '°'},
  {k: 'lens', label: '透镜密度', min: 80, max: 900, step: 10, d: 1, fmt: (v) => v.toFixed(0) + ' LPI'},
  {k: 'stripe', label: '棱纹强度', min: 0, max: 1, step: 0.01, d: 2},
  {k: 'sheen', label: '塑料光泽', min: 0, max: 2.2, step: 0.01, d: 2},
  {k: 'sheenPow', label: '光泽锐度', min: 1, max: 9, step: 0.1, d: 1},
  {k: 'switch', label: '切换柔和度', min: 0, max: 1, step: 0.01, d: 2},
  {k: 'jitter', label: '条带错位', min: 0, max: 1.2, step: 0.01, d: 2},
  {k: 'sweep', label: '视角增益', min: 0, max: 6, step: 0.1, d: 1},
  {k: 'span', label: '倾角行程', min: 0.5, max: 4, step: 0.1, d: 1},
  {k: 'phase', label: '相位偏移', min: -2, max: 2, step: 0.01, d: 2},
  {k: 'bend', label: '卡片弯曲', min: -0.07, max: 0.07, step: 0.002, d: 3},
  {k: 'autoSpeed', label: '自动速度', min: 0, max: 1.2, step: 0.01, d: 2},
];

const ctlBox = document.getElementById('ctl');
ctlBox.innerHTML = '<h2>参数</h2>' + CTRL.map((c) => {
  const v = c.k === 'span' ? state.span : CFG.opts[c.k];
  return '<div class="row"><span>' + c.label + '</span>' +
    '<span class="v" id="v_' + c.k + '">' + fmtv(c, v) + '</span></div>' +
    '<input type="range" data-k="' + c.k + '" min="' + c.min + '" max="' + c.max +
    '" step="' + c.step + '" value="' + v + '">';
}).join('');

function fmtv(c, v) {
  return c.fmt ? c.fmt(+v) : (+v).toFixed(c.d);
}

ctlBox.addEventListener('input', (e) => {
  const el = e.target;
  const k = el.dataset.k;
  if (!k) return;
  const c = CTRL.find((x) => x.k === k);
  const v = +el.value;
  document.getElementById('v_' + k).textContent = fmtv(c, v);
  if (k === 'span') { state.span = v; uniforms.uSpan.value = v; return; }
  if (k === 'phase') { state.phase = v; return; }
  if (k === 'angle') { uniforms.uAngle.value = v * Math.PI / 180; return; }
  if (k === 'autoSpeed') { CFG.opts.autoSpeed = v; return; }
  const map = {lens: 'uLens', stripe: 'uStripe', sheen: 'uSheen', sheenPow: 'uSheenPow',
               switch: 'uSwitch', jitter: 'uJitter', sweep: 'uSweep', bend: 'uBend'};
  if (map[k]) uniforms[map[k]].value = v;
});

const bAuto = document.getElementById('bAuto');
bAuto.onclick = () => {
  state.auto = !state.auto;
  bAuto.classList.toggle('on', state.auto);
  state.last = 0;
};
document.getElementById('bReset').onclick = () => {
  state.auto = false;
  bAuto.classList.remove('on');
  state.tRotX = state.tRotY = 0;
  state.last = 0;
};

// ------------------------------------------------------------ 主循环
let t0 = performance.now();
function tick(now) {
  const dt = Math.min(0.05, (now - t0) / 1000);
  t0 = now;
  if (state.auto && now - state.last > 900) {
    const t = now / 1000 * CFG.opts.autoSpeed * 2.0;
    state.tRotY = Math.sin(t) * CFG.opts.tiltMax * 0.92;
    state.tRotX = Math.sin(t * 0.63 + 1.1) * CFG.opts.tiltMax * 0.30;
  }
  state.rotX += (state.tRotX - state.rotX) * 0.14;
  state.rotY += (state.tRotY - state.rotY) * 0.14;
  group.rotation.x = state.rotX;
  group.rotation.y = state.rotY;

  uniforms.uPhase.value = state.phase;
  uniforms.uSpan.value = state.span;
  // 画面上「当前是第几张」：与 shader 同一套近似（仅用于角标显示）
  // 注意符号：shader 里的 sv 是「表面→相机」方向在条纹推进方向上的投影，与 rotY 反号
  const ph = state.phase - Math.sin(state.rotY) * state.span * 2.2;
  const n = uniforms.uN.value;
  let cur = Math.round(ph) % n;
  if (cur < 0) cur += n;
  const ce = document.getElementById('cur');
  if (ce) ce.textContent = String(cur + 1);

  renderer.render(scene, camera);
  requestAnimationFrame(tick);
}

// 供截图/自动化
window.__lentiAPI = {
  setAuto(v) { state.auto = !!v; bAuto.classList.toggle('on', state.auto); state.last = v ? 0 : performance.now(); },
  setTilt(y, x) { state.auto = false; state.tRotY = y; state.tRotX = x || 0; state.last = performance.now(); },
  setOpt(k, v) {
    if (k === 'phase') { state.phase = v; return; }
    if (k === 'span') { state.span = v; uniforms.uSpan.value = v; return; }
    if (k === 'angle') { uniforms.uAngle.value = v * Math.PI / 180; return; }
    const map = {lens: 'uLens', stripe: 'uStripe', sheen: 'uSheen', sheenPow: 'uSheenPow',
                 switch: 'uSwitch', jitter: 'uJitter', sweep: 'uSweep', bend: 'uBend',
                 gloss: 'uGloss', grain: 'uGrain'};
    if (map[k]) uniforms[map[k]].value = v;
  },
  setImages,
  n: () => uniforms.uN.value,
};

(async function boot() {
  fit();
  await setImages(CFG.images || []);
  __BOOT_EXTRA__
  requestAnimationFrame(tick);
})();
</script>
</body>
</html>
"""


def build_lenti_html(images, opts=None, aspect: float = DEFAULT_ASPECT,
                     title: str = "光栅卡", three_rel: str = THREE_SRC_REL,
                     extras: str = "", boot_extra: str = "",
                     top: str = "") -> str:
    o = dict(DEFAULT_OPTS)
    if opts:
        o.update({k: v for k, v in opts.items() if v is not None})
    cfg = {"title": title, "aspect": aspect, "images": images, "opts": o}
    n = max(1, len(images))
    return (
        _SHELL
        .replace("__TITLE__", title)
        .replace("__N__", str(n))
        .replace("__THREE_SRC__", three_rel)
        .replace("__EXTRAS__", extras)
        .replace("__TOP__", top)
        .replace("__BOOT_EXTRA__", boot_extra)
        .replace("__CONFIG__", json.dumps(cfg, ensure_ascii=False))
    )


def _slug(name: str) -> str:
    s = re.sub(r"[^\w\u4e00-\u9fff\-]+", "_", (name or "").strip())
    return s.strip("_") or ("lenti_%d" % int(time.time()))


def build(name: str, paths, mode: str = "flip", frames: int = 5,
          aspect: float = DEFAULT_ASPECT, opts=None, out_root: Path | None = None,
          interlace_lpi: float = 0.0, dpi: float = 600.0,
          title: str | None = None, depth: float = 1.35,
          pop: float = 0.55) -> Path:
    """生成一张光栅卡网页，返回产物目录。

    mode: flip = N 张图直接交替（变画卡）
          3d   = 每张图展开成 frames 帧视差（立体卡），张数 × 帧数 ≤ 6
    """
    root = Path(out_root or LENTI_ROOT) / _slug(name)
    root.mkdir(parents=True, exist_ok=True)

    frames_imgs = []
    if mode == "3d":
        per = max(2, min(6, 6 // max(1, len(paths))))
        for p in paths:
            frames_imgs.extend(parallax_frames(p, per, aspect, depth=depth, pop=pop))
    else:
        frames_imgs = [load_frame(p, aspect) for p in paths]

    frames_imgs = frames_imgs[:6]
    names = save_frames(frames_imgs, root)
    if mode == "3d":
        images = [{"src": nm, "name": f"帧 {i + 1}"} for i, nm in enumerate(names)]
    else:
        images = [{"src": nm, "name": Path(p).stem}
                  for nm, p in zip(names, paths)][:len(names)]

    html = build_lenti_html(images, opts=opts, aspect=aspect,
                            title=title or Path(root).name)
    (root / "lenti.html").write_text(html, encoding="utf-8")
    (root / "meta.json").write_text(json.dumps({
        "images": images, "opts": opts or {}, "aspect": aspect, "mode": mode,
    }, ensure_ascii=False, indent=1), encoding="utf-8")

    if interlace_lpi > 0:
        outline = interlace(paths, root / "print_interlaced.jpg",
                            lpi=interlace_lpi, dpi=dpi, aspect=aspect)
        print("[lenticular] 印刷原图:", outline)

    print(f"[lenticular] {root} · {len(names)} 帧 · mode={mode}")
    return root


def main(argv=None):
    ap = argparse.ArgumentParser(description="生成光栅卡网页")
    ap.add_argument("--images", nargs="+", required=True)
    ap.add_argument("--name", default="")
    ap.add_argument("--mode", default="flip", choices=["flip", "3d"])
    ap.add_argument("--frames", type=int, default=5)
    ap.add_argument("--aspect", default="3:4")
    ap.add_argument("--out", default="")
    ap.add_argument("--lpi", type=float, default=0.0, help="同时导出印刷交错图")
    ap.add_argument("--depth", type=float, default=1.35, help="3d 模式视差强度（1.0≈2.6%% 画宽）")
    ap.add_argument("--pop", type=float, default=0.55, help="3d 模式主体缩放（出屏/内凹）")
    a = ap.parse_args(argv)

    aspect = ASPECTS.get(a.aspect, float(a.aspect) if re.match(r"^[\d.]+$", a.aspect) else DEFAULT_ASPECT)
    name = a.name or Path(a.images[0]).stem
    root = build(name, a.images, mode=a.mode, frames=a.frames, aspect=aspect,
                 out_root=Path(a.out) if a.out else None,
                 interlace_lpi=a.lpi, depth=a.depth, pop=a.pop)
    print(root / "lenti.html")


if __name__ == "__main__":
    main()
