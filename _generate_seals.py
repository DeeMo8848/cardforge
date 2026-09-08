# -*- coding: utf-8 -*-
"""冷裱膜卡封效果生成器：为 assets/seals/ 生成程序化纹理 PNG（900x1200 RGBA）。

27 种冷裱膜效果，全部由代码绘制，透明底叠加到卡面/3D 卡之上。
运行: python _generate_seals.py
"""
from __future__ import annotations

import math
import random
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter

OUT = Path(__file__).resolve().parent / "assets" / "seals"
W, H = 900, 1200
GOLD = (232, 190, 110)
SILVER = (216, 220, 228)
PINK = (245, 170, 190)
random.seed(20260905)


def new_canvas() -> Image.Image:
    return Image.new("RGBA", (W, H), (0, 0, 0, 0))


def rand_color(palette, alpha) -> tuple:
    r, g, b = random.choice(palette)
    return (r, g, b, random.randint(alpha[0], alpha[1]))


def add_glow(img: Image.Image, radius: int = 4) -> Image.Image:
    """轻微模糊，让点/线更柔和。"""
    return img.filter(ImageFilter.GaussianBlur(radius))


def draw_star(draw, cx, cy, R, color, inner: float = 0.4, rot: float = -math.pi / 2):
    r = R * inner
    pts = []
    for i in range(10):
        ang = rot + i * math.pi / 5
        rad = R if i % 2 == 0 else r
        pts.append((cx + rad * math.cos(ang), cy + rad * math.sin(ang)))
    draw.polygon(pts, fill=color)


def draw_heart(draw, cx, cy, s, color):
    pts = []
    for t in [i * 0.02 for i in range(158)]:
        x = 16 * math.sin(t) ** 3
        y = 13 * math.cos(t) - 5 * math.cos(2 * t) - 2 * math.cos(3 * t) - math.cos(4 * t)
        pts.append((cx + x * s, cy - y * s))
    draw.polygon(pts, fill=color)


def draw_snowflake(draw, cx, cy, R, color, width=2):
    for k in range(6):
        ang = k * math.pi / 3
        x2, y2 = cx + R * math.cos(ang), cy + R * math.sin(ang)
        draw.line([(cx, cy), (x2, y2)], fill=color, width=width)
        mid = R * 0.55
        mx, my = cx + mid * math.cos(ang), cy + mid * math.sin(ang)
        for off in (0.35, -0.35):
            bx, by = mx + R * 0.3 * math.cos(ang + off), my + R * 0.3 * math.sin(ang + off)
            draw.line([(mx, my), (bx, by)], fill=color, width=width)
        ex, ey = cx + R * 1.0 * math.cos(ang), cy + R * 1.0 * math.sin(ang)
        draw.ellipse([ex - 3, ey - 3, ex + 3, ey + 3], fill=color)


def draw_butterfly(draw, cx, cy, s, color1, color2):
    # 上翅 + 下翅 + 身体
    draw.ellipse([cx - 1.6 * s, cy - 1.7 * s, cx - 0.2 * s, cy - 0.3 * s], fill=color1)
    draw.ellipse([cx + 0.2 * s, cy - 1.7 * s, cx + 1.6 * s, cy - 0.3 * s], fill=color1)
    draw.ellipse([cx - 1.2 * s, cy - 0.1 * s, cx - 0.2 * s, cy + 0.9 * s], fill=color2)
    draw.ellipse([cx + 0.2 * s, cy - 0.1 * s, cx + 1.2 * s, cy + 0.9 * s], fill=color2)
    draw.ellipse([cx - 0.12 * s, cy - 0.9 * s, cx + 0.12 * s, cy + 0.9 * s], fill=(60, 40, 30, 200))


def draw_pinwheel(draw, cx, cy, R, colors):
    blades = 4
    for i in range(blades):
        ang0 = i * 2 * math.pi / blades
        p0 = (cx, cy)
        p1 = (cx + R * math.cos(ang0), cy + R * math.sin(ang0))
        p2 = (cx + R * 0.55 * math.cos(ang0 + math.pi / blades),
              cy + R * 0.55 * math.sin(ang0 + math.pi / blades))
        draw.polygon([p0, p1, p2], fill=colors[i % len(colors)])
    draw.ellipse([cx - 6, cy - 6, cx + 6, cy + 6], fill=(255, 255, 255, 220))


def draw_cateye(draw, cx, cy, rx, ry, color):
    """猫眼：同心椭圆，中心亮、边缘淡。"""
    steps = 18
    for i in range(steps, 0, -1):
        a = int(26 + (steps - i) * (255 - 26) / steps)
        ww, hh = rx * i / steps, ry * i / steps
        draw.ellipse([cx - ww, cy - hh, cx + ww, cy + hh], fill=(color[0], color[1], color[2], a))


def v_gradient(img: Image.Image, top_color, bottom_color, alpha_top, alpha_bottom):
    """垂直渐变叠加到 img 上（上→下）。"""
    g = Image.linear_gradient("L").resize((1, H))
    g = g.convert("L").point(lambda v: int(alpha_top + (alpha_bottom - alpha_top) * v / 255))
    color_top = Image.new("RGBA", (1, 1), top_color)
    color_bottom = Image.new("RGBA", (1, 1), bottom_color)
    grad = Image.blend(color_top.resize((1, H)), color_bottom.resize((1, H)), 0.5)
    grad.putalpha(g)
    grad = grad.resize((W, H))
    img.alpha_composite(grad)


# ---------------- 各膜效果 ----------------


def make_frosted():
    img = new_canvas()
    noise = Image.effect_noise((W, H), 90).convert("L")
    noise = noise.point(lambda v: int(v * 0.42))
    noise = noise.filter(ImageFilter.GaussianBlur(1.2))
    img.alpha_composite(Image.merge("RGBA", [noise, noise, noise, noise]))
    d = ImageDraw.Draw(img)
    for _ in range(14):  # 柔和光斑
        x, y = random.randint(0, W), random.randint(0, H)
        r = random.randint(80, 220)
        a = random.randint(8, 18)
        d.ellipse([x - r, y - r, x + r, y + r], fill=(255, 255, 255, a))
    return add_glow(img, 3)


def make_matte():
    """亚光膜：柔和雾面质感（比原透明膜更明显的哑光）——均匀提亮 + 细颗粒噪点压反光。"""
    img = new_canvas()
    d = ImageDraw.Draw(img)
    d.rectangle([0, 0, W, H], fill=(255, 255, 255, 42))
    noise = Image.effect_noise((W, H), 34).convert("L").point(lambda v: int(v * 0.2))
    noise = noise.filter(ImageFilter.GaussianBlur(2))
    img.alpha_composite(Image.merge("RGBA", [noise, noise, noise, noise]))
    return img


def make_starry_flash():
    img = new_canvas()
    d = ImageDraw.Draw(img)
    for _ in range(90):
        x, y = random.randint(0, W), random.randint(0, H)
        R = random.randint(6, 22)
        a = random.randint(60, 150)
        col = (255, 250, 220, a) if random.random() < 0.8 else (255, 255, 255, a)
        d.line([(x - R, y), (x + R, y)], fill=col, width=2)
        d.line([(x, y - R), (x, y + R)], fill=col, width=2)
        d.line([(x - R * 0.5, y - R * 0.5), (x + R * 0.5, y + R * 0.5)], fill=col, width=1)
        d.line([(x - R * 0.5, y + R * 0.5), (x + R * 0.5, y - R * 0.5)], fill=col, width=1)
        d.ellipse([x - 3, y - 3, x + 3, y + 3], fill=(255, 255, 255, min(255, a + 60)))
    return add_glow(img, 1)


def make_brushed():
    img = new_canvas()
    d = ImageDraw.Draw(img)
    for _ in range(1400):
        y = random.randint(0, H)
        x = random.randint(0, W)
        ln = random.randint(10, 46)
        a = random.randint(14, 38)
        col = GOLD if random.random() < 0.6 else SILVER
        d.line([(x, y), (x + ln, y)], fill=(col[0], col[1], col[2], a), width=1)
    img = img.rotate(14, resample=Image.BICUBIC, expand=False)
    return img


def make_micro_shimmer():
    img = new_canvas()
    d = ImageDraw.Draw(img)
    for _ in range(1600):
        x, y = random.randint(0, W), random.randint(0, H)
        r = random.randint(1, 2)
        a = random.randint(18, 60)
        col = (255, 255, 250, a) if random.random() < 0.7 else (255, 240, 200, a)
        d.ellipse([x - r, y - r, x + r, y + r], fill=col)
    return img


def make_silk():
    img = new_canvas()
    d = ImageDraw.Draw(img)
    base = random.randint(60, 220)
    for k in range(14):
        amp = random.randint(14, 40)
        freq = 2 * math.pi / random.randint(140, 260)
        phase = random.uniform(0, 2 * math.pi)
        y0 = random.randint(0, H)
        a = random.randint(16, 40)
        pts = []
        for x in range(0, W + 20, 20):
            y = y0 + amp * math.sin(freq * x + phase)
            pts.append((x, y))
        d.line(pts, fill=(SILVER[0], SILVER[1], SILVER[2], a), width=random.randint(1, 3))
        if k % 5 == 0:  # 少量金色丝线
            pts2 = []
            for x in range(0, W + 20, 20):
                y = y0 + 60 + amp * 1.4 * math.sin(freq * 0.6 * x + phase + 1)
                pts2.append((x, y))
            d.line(pts2, fill=(GOLD[0], GOLD[1], GOLD[2], a + 8), width=2)
    return add_glow(img, 1)


def make_diamond():
    img = new_canvas()
    d = ImageDraw.Draw(img)
    gap = 88
    for i in range(-W - H, W + H, gap):       # 45° 网格线
        d.line([(i, 0), (i + H, H)], fill=(240, 235, 225, 16), width=1)
        d.line([(i + H, 0), (i, H)], fill=(240, 235, 225, 16), width=1)
    for x in range(0, W, gap):                # 菱形高光点
        for y in range(0, H, gap):
            if (x + y) % (gap * 2) < gap:
                a = random.randint(50, 110)
                d.polygon([(x, y - 7), (x + 5, y), (x, y + 7), (x - 5, y)], fill=(255, 252, 235, a))
    return img


def make_oil_painting():
    img = new_canvas()
    d = ImageDraw.Draw(img)
    palette = [(180, 140, 90), (120, 150, 170), (190, 160, 120), (140, 110, 90),
               (160, 180, 150), (200, 180, 140), (110, 130, 150)]
    for _ in range(130):
        x, y = random.randint(0, W), random.randint(0, H)
        rw, rh = random.randint(20, 90), random.randint(12, 50)
        a = random.randint(12, 26)
        col = rand_color(palette, (a, a))
        d.ellipse([x - rw, y - rh, x + rw, y + rh], fill=col)
    return add_glow(img, 8)


def make_cross():
    img = new_canvas()
    d = ImageDraw.Draw(img)
    gap = 64
    arm = 15
    for x in range(gap // 2, W, gap):
        for y in range(gap // 2, H, gap):
            a = random.randint(40, 90)
            col = (255, 250, 235, a)
            d.line([(x - arm, y), (x + arm, y)], fill=col, width=3)
            d.line([(x, y - arm), (x, y + arm)], fill=col, width=3)
            d.ellipse([x - 3, y - 3, x + 3, y + 3], fill=(255, 255, 255, min(255, a + 50)))
    return add_glow(img, 1)


def make_leather():
    img = new_canvas()
    d = ImageDraw.Draw(img)
    noise = Image.effect_noise((W, H), 60).convert("L").point(lambda v: int(v * 0.16))
    img.alpha_composite(Image.merge("RGBA", [noise, noise, noise, noise]))
    for _ in range(900):
        x, y = random.randint(0, W), random.randint(0, H)
        a = random.randint(12, 30)
        col = (200, 170, 130, a)
        ang = random.uniform(0, 2 * math.pi)
        ln = random.randint(8, 26)
        d.line([(x, y), (x + ln * math.cos(ang), y + ln * math.sin(ang))], fill=col, width=1)
    return add_glow(img, 1)


def make_cat_eye():
    img = new_canvas()
    d = ImageDraw.Draw(img)
    for row in range(8):
        off = 0 if row % 2 == 0 else 75
        for col in range(0, W + 160, 150):
            cx, cy = col - 80 + off, row * 160 + 80
            if 0 <= cx <= W and 0 <= cy <= H:
                draw_cateye(d, cx, cy, 44, 66, (255, 244, 210))
    return add_glow(img, 2)


def make_glass():
    img = new_canvas()
    d = ImageDraw.Draw(img)
    # 边缘高光
    d.rectangle([10, 10, W - 10, H - 10], outline=(255, 255, 255, 30), width=4)
    # 斜向宽光带
    for i in range(6):
        x0 = random.randint(-300, W)
        wd = random.randint(90, 200)
        a = random.randint(14, 30)
        x1 = x0 + wd
        d.polygon([(x0, -40), (x1, -40), (x1 - wd * 0.25, H + 40), (x0 - wd * 0.25, H + 40)],
                  fill=(255, 255, 255, a))
    img = add_glow(img, 6)
    return img


def make_rainbow():
    img = new_canvas()
    colors = [(255, 80, 90), (255, 170, 60), (255, 235, 90), (110, 220, 130),
              (90, 180, 255), (140, 120, 255), (230, 110, 230)]
    band = H / len(colors)
    d = ImageDraw.Draw(img)
    for i, c in enumerate(colors):
        y0 = i * band
        for k in range(16):  # 每带柔化
            a = int(14 + (15 - abs(k - 8)) * 2)
            yy0 = y0 + k * band / 16
            d.rectangle([0, yy0, W, yy0 + band / 16 + 1], fill=(c[0], c[1], c[2], a))
    return img


def make_starry_night():
    img = new_canvas()
    v_gradient(img, (30, 45, 90), (70, 45, 110), 150, 70)
    d = ImageDraw.Draw(img)
    for _ in range(160):
        x, y = random.randint(0, W), random.randint(0, H)
        r = random.choice([1, 1, 1, 2, 2, 3])
        d.ellipse([x - r, y - r, x + r, y + r], fill=(255, 255, 255, random.randint(120, 220)))
    for _ in range(10):  # 亮星光芒
        x, y = random.randint(0, W), random.randint(0, H)
        R = random.randint(8, 20)
        col = (255, 250, 220, random.randint(140, 200))
        d.line([(x - R, y), (x + R, y)], fill=col, width=1)
        d.line([(x, y - R), (x, y + R)], fill=col, width=1)
    return add_glow(img, 1)


def make_firework():
    img = new_canvas()
    d = ImageDraw.Draw(img)
    palette = [(255, 210, 90), (255, 130, 120), (130, 220, 255), (200, 150, 255), (150, 255, 190)]
    for _ in range(7):
        cx, cy = random.randint(120, W - 120), random.randint(130, H - 120)
        R = random.randint(50, 100)
        base = random.choice(palette)
        for i in range(22):
            ang = i * 2 * math.pi / 22
            r = R * random.uniform(0.55, 1.0)
            x2, y2 = cx + r * math.cos(ang), cy + r * math.sin(ang)
            a = random.randint(120, 200)
            d.line([(cx, cy), (x2, y2)], fill=(base[0], base[1], base[2], a), width=2)
            d.ellipse([x2 - 2, y2 - 2, x2 + 2, y2 + 2], fill=(255, 255, 255, min(255, a + 40)))
        d.ellipse([cx - 5, cy - 5, cx + 5, cy + 5], fill=(255, 255, 255, 240))
    return add_glow(img, 1)


def make_heart():
    img = new_canvas()
    d = ImageDraw.Draw(img)
    palette = [(245, 150, 170), (255, 190, 140), (235, 120, 150)]
    for _ in range(46):
        x, y = random.randint(40, W - 40), random.randint(40, H - 40)
        s = random.uniform(4, 11)
        a = random.randint(70, 150)
        col = rand_color(palette, (a, a))
        draw_heart(d, x, y, s, col)
    return add_glow(img, 1)


def make_small_stars():
    img = new_canvas()
    d = ImageDraw.Draw(img)
    for _ in range(110):
        x, y = random.randint(10, W - 10), random.randint(10, H - 10)
        R = random.randint(4, 9)
        a = random.randint(70, 150)
        col = (255, 240, 190, a) if random.random() < 0.75 else (255, 255, 255, a)
        draw_star(d, x, y, R, col, inner=0.42)
    return add_glow(img, 1)


def make_stars():
    img = new_canvas()
    d = ImageDraw.Draw(img)
    for _ in range(26):
        x, y = random.randint(30, W - 30), random.randint(30, H - 30)
        R = random.randint(14, 32)
        a = random.randint(60, 130)
        col = (255, 235, 170, a) if random.random() < 0.7 else (255, 255, 255, a)
        draw_star(d, x, y, R, col, inner=0.4)
    return add_glow(img, 1)


def make_snowflake():
    img = new_canvas()
    d = ImageDraw.Draw(img)
    for _ in range(40):
        x, y = random.randint(30, W - 30), random.randint(30, H - 30)
        R = random.randint(16, 34)
        a = random.randint(70, 150)
        col = (235, 245, 255, a)
        draw_snowflake(d, x, y, R, col)
    return add_glow(img, 1)


def make_sakura():
    img = new_canvas()
    d = ImageDraw.Draw(img)
    for _ in range(80):
        cx, cy = random.randint(20, W - 20), random.randint(20, H - 20)
        s = random.uniform(5, 11)
        a = random.randint(70, 150)
        col = (250, 185, 200, a)
        for k in range(5):  # 五瓣
            ang = k * 2 * math.pi / 5 - math.pi / 2
            px = cx + s * 1.05 * math.cos(ang)
            py = cy + s * 1.05 * math.sin(ang)
            d.ellipse([px - s * 0.72, py - s * 0.72, px + s * 0.72, py + s * 0.72], fill=col)
        d.ellipse([cx - 2.2, cy - 2.2, cx + 2.2, cy + 2.2], fill=(250, 220, 150, min(255, a + 60)))
    return add_glow(img, 1)


def make_shimmer_stars():
    """星光细闪膜：细密小亮点 + 少量十字星光（星星点点、扑灵扑灵）。"""
    img = new_canvas()
    d = ImageDraw.Draw(img)
    for _ in range(220):
        x, y = random.randint(0, W), random.randint(0, H)
        r = random.randint(1, 2)
        a = random.randint(40, 110)
        d.ellipse([x - r, y - r, x + r, y + r], fill=(255, 250, 235, a))
    for _ in range(14):
        x, y = random.randint(0, W), random.randint(0, H)
        R = random.randint(5, 12)
        a = random.randint(90, 170)
        col = (255, 252, 240, a)
        d.line([(x - R, y), (x + R, y)], fill=col, width=2)
        d.line([(x, y - R), (x, y + R)], fill=col, width=2)
        d.ellipse([x - 2, y - 2, x + 2, y + 2], fill=(255, 255, 255, min(255, a + 60)))
    return add_glow(img, 1)


def make_flow_sand():
    """流麻膜：高密度细小闪粉（流沙噪点感）。"""
    img = new_canvas()
    d = ImageDraw.Draw(img)
    for _ in range(900):
        x, y = random.randint(0, W), random.randint(0, H)
        r = random.randint(1, 2)
        a = random.randint(30, 120)
        col = (255, 250, 235, a) if random.random() < 0.75 else (255, 255, 255, a)
        d.ellipse([x - r, y - r, x + r, y + r], fill=col)
    return add_glow(img, 1)


def _grid_offsets(cell):
    for y in range(-cell, H + cell, cell):
        off = ((y // cell) % 2) * cell // 2
        for x in range(-cell, W + cell, cell):
            yield x + off, y


def _hex_pixels(dx, dy):
    """蜂窝点阵：行间距 dy，行内间距 dx，奇偶行 x 偏移 dx/2。"""
    for row in range(-2, int(H / dy) + 3):
        y = row * dy
        off = (row % 2) * dx / 2
        for col in range(-2, int(W / dx) + 3):
            yield col * dx + off, y


def _hex_pts(cx, cy, r):
    return [(cx + r * math.cos(math.pi / 6 + i * math.pi / 3),
             cy + r * math.sin(math.pi / 6 + i * math.pi / 3)) for i in range(6)]


def make_triangle_grid():
    """A11 三角格膜：三角形真密铺（正倒三角形相间、边角重叠，无方块格）。"""
    img = new_canvas(); d = ImageDraw.Draw(img)
    h = 190  # 三角形高（行高）
    xr = h / math.sqrt(3)  # 底边半宽（等边三角形）
    for row in range(-2, H // h + 3):
        y = row * h
        off = (row % 2) * xr
        for col in range(-2, int(W / (2 * xr)) + 3):
            cx = col * 2 * xr + off
            if row % 2 == 0:
                pts = [(cx - xr, y), (cx + xr, y), (cx, y + h)]
            else:
                pts = [(cx - xr, y + h), (cx + xr, y + h), (cx, y)]
            d.polygon(pts, fill=(255, 248, 226, random.randint(55, 110)))
    return add_glow(img, 1)


def make_bigcircle_grid():
    """A22 圆格膜：圆形蜂窝密铺（同心双环，外环与邻圆重叠）。"""
    img = new_canvas(); d = ImageDraw.Draw(img)
    dx = 138.5  # 圆心距
    for cx, cy in _hex_pixels(dx, dx * 0.866):
        col = (255, 250, 235, random.randint(70, 130))
        d.ellipse([cx - 77.5, cy - 77.5, cx + 77.5, cy + 77.5], outline=col, width=8)
        d.ellipse([cx - 41.5, cy - 41.5, cx + 41.5, cy + 41.5], outline=col, width=7)
    return add_glow(img, 1)


def make_hexagon_grid():
    """A33 六角格膜：六边形蜂窝密铺（线框互相衔接成网）。"""
    img = new_canvas(); d = ImageDraw.Draw(img)
    R = 85  # 蜂窝单元外接半径
    dx = math.sqrt(3) * R
    for cx, cy in _hex_pixels(dx, 1.5 * R):
        a = random.randint(70, 125)
        d.polygon(_hex_pts(cx, cy, int(R * 1.11)), outline=(255, 248, 226, a), width=6)
    return add_glow(img, 1)


def make_ring_grid():
    """B11 圆环膜：圆环阵。"""
    img = new_canvas(); d = ImageDraw.Draw(img)
    for cx, cy in _grid_offsets(100):
        r = 30
        d.ellipse([cx - r, cy - r, cx + r, cy + r], outline=(255, 250, 235, 140), width=6)
    return add_glow(img, 1)


def make_lightning():
    """B22 闪电膜：闪电散布。"""
    img = new_canvas(); d = ImageDraw.Draw(img)
    for _ in range(40):
        x, y = random.randint(40, W - 40), random.randint(40, H - 40)
        s = random.uniform(0.5, 1.1)
        pts = [(x + 24 * s, y - 90 * s), (x - 40 * s, y + 4 * s), (x - 4 * s, y + 4 * s),
               (x - 26 * s, y + 90 * s), (x + 46 * s, y - 22 * s), (x + 8 * s, y - 22 * s)]
        d.polygon(pts, fill=(255, 244, 210, random.randint(70, 130)))
    return add_glow(img, 1)


def make_gear_grid():
    """B33 齿轮膜：齿轮阵。"""
    img = new_canvas(); d = ImageDraw.Draw(img)
    for cx, cy in _grid_offsets(110):
        pts = []
        for i in range(24):
            a = i * math.pi / 12
            r = 44 if i % 2 == 0 else 30
            pts.append((cx + r * math.cos(a), cy + r * math.sin(a)))
        d.polygon(pts, fill=(255, 246, 226, 95))
    return add_glow(img, 1)


def make_circuit():
    """电路板膜：走线 + 焊盘 + 过孔。"""
    img = new_canvas(); d = ImageDraw.Draw(img)
    for cx, cy in _grid_offsets(90):
        a = random.randint(50, 110)
        col = (255, 244, 220, a)
        d.line([(cx, cy), (cx, cy - 30), (cx + 34, cy - 30)], fill=col, width=6)
        d.line([(cx, cy), (cx - 30, cy), (cx - 30, cy + 34)], fill=col, width=6)
        d.line([(cx, cy), (cx + 32, cy + 32)], fill=col, width=6)
        d.ellipse([cx - 5, cy - 5, cx + 5, cy + 5], fill=col)
        d.ellipse([cx + 28, cy - 38, cx + 42, cy - 24], fill=col)
        d.ellipse([cx - 38, cy + 26, cx - 24, cy + 40], fill=col)
    return add_glow(img, 1)


def make_circuit_board():
    """电路板膜 v2：单张整卡科技感电路板纹理（芯片+放射走线+焊盘+过孔+发光，不平铺）。

    与旧「纹路1」的区别：旧版是小电路单元平铺重复；新版是一整张完整电路板，
    含主/副芯片、从芯片扇出的放射走线、独立装饰走线区、过孔与焊盘，深蓝底发光。
    """
    img = new_canvas()

    # --- 深蓝黑渐变底（半透明、铺满全卡） ---
    bg = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    gd = ImageDraw.Draw(bg)
    for y in range(0, H, 3):
        t = y / H
        a = int(52 + 40 * t)
        gd.line([(0, y), (W, y)], fill=(6 + int(9 * t), 14 + int(20 * t), 30 + int(50 * t), a))
    img.alpha_composite(bg)
    noise = Image.effect_noise((W, H), 22).convert("L").point(lambda v: int(v * 0.08))
    noise = noise.filter(ImageFilter.GaussianBlur(0.8))
    img.alpha_composite(Image.merge("RGBA", [noise, noise, noise, noise]))

    d = ImageDraw.Draw(img)
    # 亮元素统一画到 line_layer，最后整体发光叠加
    line_layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    ld = ImageDraw.Draw(line_layer)

    CYA = (135, 218, 255)
    CYA2 = (95, 192, 250)
    CYA3 = (72, 168, 242)
    GOLD = (255, 205, 120)
    WHITE = (215, 242, 255)

    DIRS8 = [(1, 0), (-1, 0), (0, 1), (0, -1), (1, 1), (1, -1), (-1, 1), (-1, -1)]

    def route(start, color, width, max_steps, step_rng=(48, 100), pad_at_end=True):
        """90°/45° 折线走线（带末端过孔/焊盘）。"""
        x, y = start
        dx, dy = random.choice(DIRS8[:4])
        pts = [(x, y)]
        for _ in range(max_steps):
            step = random.randint(*step_rng)
            nx, ny = x + dx * step, y + dy * step
            if not (-40 < nx < W + 40 and -40 < ny < H + 40):
                break
            pts.append((nx, ny))
            x, y = nx, ny
            cur = DIRS8.index((dx, dy))
            cur = (cur + random.choice((-2, -1, 0, 1, 2))) % 8
            dx, dy = DIRS8[cur]
        ld.line(pts, fill=(color[0], color[1], color[2], 235), width=width, joint="curve")
        if pad_at_end:
            via(x, y, random.choice((6, 8, 10)), color, hole=random.random() < 0.6)
        return x, y

    def via(x, y, r, color, hole=True):
        """焊盘（实心+高光）或过孔（空心环）。"""
        if hole:
            ld.ellipse([x - r, y - r, x + r, y + r],
                       outline=(color[0], color[1], color[2], 210), width=max(2, r // 3))
            ld.ellipse([x - 2, y - 2, x + 2, y + 2], fill=(WHITE[0], WHITE[1], WHITE[2], 170))
        else:
            ld.ellipse([x - r, y - r, x + r, y + r], fill=(color[0], color[1], color[2], 245))
            ld.ellipse([x - r * 0.5, y - r * 0.6, x + r * 0.1, y - r * 0.1],
                       fill=(255, 255, 255, 210))

    def chip(cx, cy, half):
        """发光芯片：辉光 + 本体 + 四边引脚 + 硅片小方块阵列。"""
        glow = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        gg = ImageDraw.Draw(glow)
        gg.rounded_rectangle([cx - half - 18, cy - half - 18, cx + half + 18, cy + half + 18],
                             radius=16, fill=(80, 180, 255, 115))
        img.alpha_composite(glow.filter(ImageFilter.GaussianBlur(26)))
        ld.rounded_rectangle([cx - half, cy - half, cx + half, cy + half], radius=8,
                             fill=(12, 24, 46, 255), outline=(122, 216, 255, 255), width=3)
        pin = max(6, half // 5)
        n = 8
        for i in range(n):
            off = int(-half + 6 + i * (2 * half - 12) / (n - 1))
            ld.rectangle([cx - half + off - pin // 2, cy - half - 15, cx - half + off + pin // 2, cy - half - 4],
                         fill=(150, 226, 255, 245))
            ld.rectangle([cx - half + off - pin // 2, cy + half + 4, cx - half + off + pin // 2, cy + half + 15],
                         fill=(150, 226, 255, 245))
            ld.rectangle([cx - half - 15, cy - half + off - pin // 2, cx - half - 4, cy - half + off + pin // 2],
                         fill=(150, 226, 255, 245))
            ld.rectangle([cx + half + 4, cy - half + off - pin // 2, cx + half + 15, cy - half + off + pin // 2],
                         fill=(150, 226, 255, 245))
        step_b = (2 * half - 22) // 4
        for i in range(4):
            for j in range(4):
                x0 = cx - half + 11 + i * step_b + 2
                y0 = cy - half + 11 + j * step_b + 2
                ld.rectangle([x0, y0, x0 + step_b - 4, y0 + step_b - 4],
                             fill=(26, 68, 120, 255), outline=(82, 172, 236, 150), width=1)

    # --- 主芯片（偏上中部）与副芯片（右下部） ---
    chip(int(W * 0.44), int(H * 0.24), 78)
    chip(int(W * 0.78), int(H * 0.68), 42)

    # --- 主芯片扇出走线（8 个方向，粗细分层） ---
    for ang, w, col, steps in (
        (-90, 8, CYA, 6), (-60, 6, CYA2, 5), (-30, 6, CYA3, 4), (0, 8, CYA, 6),
        (30, 5, CYA3, 4), (60, 6, CYA2, 5), (90, 7, CYA, 5), (180, 7, CYA, 5),
    ):
        ex = int(W * 0.44) + int(math.cos(math.radians(ang)) * 92)
        ey = int(H * 0.24) + int(math.sin(math.radians(ang)) * 92)
        route((ex, ey), col, w, steps)
    # 副芯片扇出
    for ang, w, col, steps in ((-90, 6, CYA2, 4), (0, 6, CYA2, 4), (90, 5, CYA3, 3), (180, 6, CYA2, 4)):
        ex = int(W * 0.78) + int(math.cos(math.radians(ang)) * 52)
        ey = int(H * 0.68) + int(math.sin(math.radians(ang)) * 52)
        route((ex, ey), col, w, steps)

    # --- 分支细线与金色点缀 ---
    for _ in range(14):
        sx = random.randint(60, W - 60)
        sy = random.randint(60, H - 60)
        w = random.choice((2, 3))
        col = random.choice((CYA3, GOLD, CYA2))
        route((sx, sy), col, w, random.randint(2, 4), (40, 80))

    # --- 独立装饰走线区：左上与右下密集细线阵列（内存总线感） ---
    for base_x in (70, 96, 122):
        for k in range(3):
            y0 = 40 + k * 130
            route((base_x, y0), CYA3, 2, 8, (50, 90), pad_at_end=False)
    for base_y in (H - 150, H - 100):
        for k in range(3):
            x0 = 260 + k * 130
            route((x0, base_y), CYA3, 2, 8, (50, 90), pad_at_end=False)
    # 斜向对角线主路（金色粗线）
    route((int(W * 0.10), int(H * 0.95)), GOLD, 7, 7, (60, 110))
    route((int(W * 0.92), int(H * 0.06)), GOLD, 5, 5, (60, 100))

    # --- 整体发光合成 ---
    img.alpha_composite(line_layer.filter(ImageFilter.GaussianBlur(4)))
    img.alpha_composite(line_layer)
    return img


def make_voronoi():
    """A44 碎玻璃膜：随机多边形拼块（Voronoi 近似预览）。"""
    img = new_canvas(); d = ImageDraw.Draw(img)
    for _ in range(70):
        cx, cy = random.randint(0, W), random.randint(0, H)
        r = random.randint(28, 95)
        sides = random.randint(4, 6)
        poly = []
        for i in range(sides):
            a = i * 2 * math.pi / sides + random.uniform(-0.4, 0.4)
            rr = r * random.uniform(0.6, 1.3)
            poly.append((cx + rr * math.cos(a), cy + rr * math.sin(a)))
        d.polygon(poly, outline=(255, 250, 235, 190), fill=(255, 248, 226, random.randint(40, 85)))
    return add_glow(img, 1)


def make_snowflake_grid():
    """A55 雪花格膜：六边形蜂窝密铺雪花（臂长撑出单元、与邻雪重叠）。"""
    img = new_canvas(); d = ImageDraw.Draw(img)
    R = 88  # 蜂窝单元外接半径
    dx = math.sqrt(3) * R
    for cx, cy in _hex_pixels(dx, 1.5 * R):
        a = random.randint(70, 130)
        col = (226, 240, 255, a)
        L = R * 1.13  # 臂长（> 单元外接半径 → 与邻雪重叠）
        for k in range(6):
            ang = k * math.pi / 3
            x2, y2 = cx + L * math.cos(ang), cy + L * math.sin(ang)
            d.line([(cx, cy), (x2, y2)], fill=col, width=6)
            mx, my = cx + L * 0.63 * math.cos(ang), cy + L * 0.63 * math.sin(ang)
            bx = L * 0.35
            d.line([(mx, my), (mx + bx * math.cos(ang + 0.5), my + bx * math.sin(ang + 0.5))], fill=col, width=5)
            d.line([(mx, my), (mx + bx * math.cos(ang - 0.5), my + bx * math.sin(ang - 0.5))], fill=col, width=5)
    return add_glow(img, 1)


def make_hexagram_grid():
    """A66 几何格膜：六边形蜂窝密铺六芒星（尖角伸出单元、互相重叠）。"""
    img = new_canvas(); d = ImageDraw.Draw(img)
    R = 88  # 蜂窝单元外接半径
    dx = math.sqrt(3) * R
    for cx, cy in _hex_pixels(dx, 1.5 * R):
        a = random.randint(60, 115)
        col = (255, 248, 226, a)
        for k in range(2):
            a0 = -math.pi / 2 + k * math.pi
            pts = [(cx + R * 1.13 * math.cos(a0 + i * 2 * math.pi / 3),
                    cy + R * 1.13 * math.sin(a0 + i * 2 * math.pi / 3)) for i in range(3)]
            d.polygon(pts, fill=col)
    return add_glow(img, 1)


def make_beads():
    img = new_canvas()
    d = ImageDraw.Draw(img)
    palette = [(255, 200, 120), (160, 220, 255), (255, 170, 180), (180, 255, 190),
               (230, 200, 255), (255, 255, 200), (255, 240, 240)]
    for _ in range(150):
        x, y = random.randint(10, W - 10), random.randint(10, H - 10)
        r = random.randint(5, 11)
        a = random.randint(80, 160)
        col = rand_color(palette, (a, a))
        d.ellipse([x - r, y - r, x + r, y + r], fill=col)
        d.ellipse([x - r * 0.55, y - r * 0.6, x - r * 0.05, y - r * 0.1],
                  fill=(255, 255, 255, min(255, a + 50)))  # 高光
    return add_glow(img, 1)


def make_period():
    img = new_canvas()
    d = ImageDraw.Draw(img)
    gap = 52
    for x in range(gap // 2, W, gap):
        for y in range(gap // 2, H, gap):
            r = random.randint(3, 6)
            a = random.randint(60, 130)
            d.ellipse([x - r, y - r, x + r, y + r], fill=(40, 36, 40, a))
    return img


def make_butterfly():
    img = new_canvas()
    d = ImageDraw.Draw(img)
    pal1 = [(255, 190, 120), (170, 210, 255), (250, 160, 180), (200, 180, 255)]
    pal2 = [(150, 120, 90), (120, 160, 200), (200, 110, 130), (150, 120, 200)]
    for _ in range(34):
        x, y = random.randint(40, W - 40), random.randint(40, H - 40)
        s = random.uniform(7, 14)
        a = random.randint(70, 140)
        c1 = rand_color(pal1, (a, a))
        c2 = rand_color(pal2, (a, a))
        draw_butterfly(d, x, y, s, c1, c2)
    return add_glow(img, 1)


def make_diagonal_beams():
    img = new_canvas()
    d = ImageDraw.Draw(img)
    for i in range(9):
        x0 = random.randint(-200, W)
        wd = random.randint(40, 120)
        a = random.randint(12, 26)
        col = GOLD if random.random() < 0.7 else SILVER
        x1 = x0 + wd
        d.polygon([(x0, -40), (x1, -40), (x1 - wd * 0.3, H + 40), (x0 - wd * 0.3, H + 40)],
                  fill=(col[0], col[1], col[2], a))
    return add_glow(img, 5)


def make_pinwheel():
    img = new_canvas()
    d = ImageDraw.Draw(img)
    palette = [(255, 190, 100), (255, 140, 130), (130, 210, 255), (170, 150, 255), (150, 240, 170)]
    for _ in range(42):
        x, y = random.randint(30, W - 30), random.randint(30, H - 30)
        R = random.uniform(10, 22)
        a = random.randint(70, 150)
        colors = [rand_color(palette, (a, a)) for _ in range(4)]
        draw_pinwheel(d, x, y, R, colors)
    return add_glow(img, 1)


GENERATORS = [
    ("亚光膜", make_matte),
    ("磨砂膜", make_frosted),
    ("米字膜2", make_starry_flash),
    ("菱形", make_diamond),
    ("十字膜", make_cross),
    ("玻璃膜", make_glass),
    ("彩虹膜", make_rainbow),
    ("星空膜", make_starry_night),
    ("米字膜1", make_firework),
    ("爱心膜", make_heart),
    ("小星星膜", make_small_stars),
    ("星星膜", make_stars),
    ("雪花膜", make_snowflake),
    ("樱花膜", make_sakura),
    ("星光细闪膜", make_shimmer_stars),
    ("测试膜", make_shimmer_stars),
    ("流麻膜", make_flow_sand),
    ("A33六角格膜", make_hexagon_grid),
    ("A44碎玻璃膜", make_voronoi),
    ("B11圆环膜", make_ring_grid),
    ("B22闪电膜", make_lightning),
    ("B33齿轮膜", make_gear_grid),
    ("纹路1", make_circuit),
    ("点状膜1", make_beads),
    ("点状膜2", make_period),
    ("风车膜", make_pinwheel),
]


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    for name, fn in GENERATORS:
        img = fn()
        out = OUT / f"{name}.png"
        img.save(out, optimize=True)
        print(f"OK {name}  {img.size}  {out.stat().st_size // 1024}KB")
    print(f"done -> {OUT}  ({len(GENERATORS)} 种)")


if __name__ == "__main__":
    main()
