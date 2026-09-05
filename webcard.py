"""3D 卡网页生成：每张卡产出自包含的单 HTML（内嵌 three.js，图片相对引用，双击即开）。

效果：
- 正面：镭射箔光（渐变光带 + 径向眩光 + 菲涅尔），随视角与指针流动
- 景深：前景层随指针/视角产生视差，主体悬浮于卡面
- 背面：卡背图 + 景深平移 + 暖光边缘
- 交互：拖拽旋转、滚轮/双指缩放、悬停倾斜、侧面薄片感
- 特效：可配置多个动画特效（粒子引擎），支持位置/数量/速度/缩放/透明度/颜色/精灵帧动画/emoji 文本
"""
from __future__ import annotations

import json
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
THREE_JS = PROJECT_ROOT / "assets" / "lib" / "three.min.js"

# ---- 粒子引擎 JS（卡片页与特效预览页共用） ----
# 依赖全局：THREE、scene、CARD_WIDTH、CARD_HEIGHT、window.__EFFECT_ANCHOR__
EFFECT_ENGINE_JS = r"""
  // ===== 特效引擎 =====
  var __fx = (function () {
    var anchor = window.__EFFECT_ANCHOR__ || null;
    var emitters = [];
    var particles = [];

    function rand(min, max) { return min + Math.random() * (max - min); }
    function rs(v) { return v ? rand(-Math.abs(v), Math.abs(v)) : 0; }
    function clamp01(v) { return v < 0 ? 0 : (v > 1 ? 1 : v); }

    function resolvePoint(pos) {
      var W = CARD_WIDTH, H = CARD_HEIGHT;
      if (!pos) return { x: rand(-W / 2, W / 2), y: rand(-H / 2, H / 2) };
      var t = pos.type;
      if (t === 'top') return { x: 0, y: H * 0.42 };
      if (t === 'bottom') return { x: 0, y: -H * 0.42 };
      if (t === 'left') return { x: -W * 0.42, y: 0 };
      if (t === 'right') return { x: W * 0.42, y: 0 };
      if (t === 'center') return { x: 0, y: 0 };
      if (t === 'topLeft') return { x: -W * 0.26, y: H * 0.26 };
      if (t === 'topRight') return { x: W * 0.26, y: H * 0.26 };
      if (t === 'bottomLeft') return { x: -W * 0.26, y: -H * 0.26 };
      if (t === 'bottomRight') return { x: W * 0.26, y: -H * 0.26 };
      if (t === 'custom') return { x: ((pos.x || 0.5) - 0.5) * W, y: ((pos.y || 0.5) - 0.5) * H };
      return { x: rand(-W / 2, W / 2), y: rand(-H / 2, H / 2) };
    }

    function makeTextTexture(ch) {
      var c = document.createElement('canvas');
      c.width = c.height = 256;
      var ctx = c.getContext('2d');
      ctx.font = '210px "Segoe UI Emoji","Apple Color Emoji","Noto Color Emoji","Microsoft YaHei",sans-serif';
      ctx.textAlign = 'center';
      ctx.textBaseline = 'middle';
      ctx.fillStyle = '#ffffff';
      ctx.fillText(ch, 128, 140);
      var t = new THREE.CanvasTexture(c);
      t.colorSpace = THREE.SRGBColorSpace;
      return t;
    }

    function makeFrameSet(image, def) {
      var fw = Math.max(1, Math.round(def.frameWidth || image.width));
      var fh = Math.max(1, Math.round(def.frameHeight || image.height));
      var cols = Math.max(1, Math.floor(image.width / fw));
      var total = def.totalFrames || Math.max(1, Math.floor(image.width / fw) * Math.floor(image.height / fh));
      var start = def.startFrame || 0;
      var end = def.endFrame != null && def.endFrame > 0 ? def.endFrame : total - 1;
      if (end < start) end = start;
      var canvas = document.createElement('canvas');
      canvas.width = fw;
      canvas.height = fh;
      var ctx = canvas.getContext('2d');
      var tex = new THREE.CanvasTexture(canvas);
      tex.colorSpace = THREE.SRGBColorSpace;
      return { image: image, fw: fw, fh: fh, cols: cols, total: total, start: start, end: end,
               pingpong: !!def.framePingPong, loop: !!def.frameLoop,
               ctx: ctx, tex: tex };
    }

    function drawFrame(fr, index) {
      var i = ((index % fr.total) + fr.total) % fr.total;
      var sx = (i % fr.cols) * fr.fw;
      var sy = Math.floor(i / fr.cols) * fr.fh;
      fr.ctx.clearRect(0, 0, fr.fw, fr.fh);
      fr.ctx.drawImage(fr.image, sx, sy, fr.fw, fr.fh, 0, 0, fr.fw, fr.fh);
      fr.tex.needsUpdate = true;
    }

    function frameIndex(p) {
      if (!p.frame) return -1;
      var fr = p.frame;
      var range = fr.end - fr.start + 1;
      var idx = fr.start + Math.floor(p.frameProgress);
      if (fr.pingpong) {
        var span = Math.max(1, range * 2 - 2);
        var pos = (((idx - fr.start) % span) + span) % span;
        return pos < range ? fr.start + pos : fr.start + (span - pos);
      }
      if (!fr.loop && idx > fr.end) return -1;
      idx = fr.start + ((((idx - fr.start) % range) + range) % range);
      return idx;
    }

    function spawnSprite(tex, color) {
      var mat = new THREE.SpriteMaterial({ map: tex, transparent: true, depthWrite: false, toneMapped: false, rotation: 0 });
      if (color) mat.color.setHex(color);
      var s = new THREE.Sprite(mat);
      anchor.add(s);
      return s;
    }

    // 预设动作：区别于偏移量（某点到某点），动作是"从某点向某方向持续运动"
    function motionParams(def) {
      var m = def.motion || '';
      var dirSign = def.motionDir === 'ccw' ? -1 : 1;
      var out = { type: m, dir: dirSign,
                  speed: Math.max(0, def.moveSpeed == null ? 2 : def.moveSpeed),
                  cd: Math.max(0, def.centerDist || 0),
                  ang: Math.random() * Math.PI * 2,
                  phase: Math.random() * Math.PI * 2 };
      if (m === 'moveL' || m === 'moveR') out.dir = (m === 'moveL' ? -1 : 1);
      else if (m === 'moveU' || m === 'moveD') out.dir = (m === 'moveU' ? 1 : -1);
      else if (m === 'moveLR' || m === 'moveUD') out.dir = dirSign * (Math.random() < 0.5 ? -1 : 1);
      else if (m === 'diagTL' || m === 'diagTR' || m === 'diagBL' || m === 'diagBR') {
        var cd = out.cd || 0.9;
        out.sx0 = (m === 'diagTL' || m === 'diagBL') ? -cd : cd;   // 左上/左下 → 从左边产生
        out.sy0 = (m === 'diagTL' || m === 'diagTR') ? cd : -cd;   // 左上/右上 → 从上方产生
        out.dx = (m === 'diagTL' || m === 'diagBL') ? 1 : -1;      // 向左上产生 → 向右下移动
        out.dy = (m === 'diagTL' || m === 'diagTR') ? -1 : 1;
      }
      return out;
    }

    function motionPos(p, t) {
      var m = p.motion, ms = m.speed, cd = m.cd, L = p.dur * ms;
      var x = p.x, y = p.y;
      switch (m.type) {
        case 'spread':
          x = p.x + Math.cos(m.ang) * L * t;
          y = p.y + Math.sin(m.ang) * L * t;
          break;
        case 'shrink':
          x = p.x + Math.cos(m.ang) * L * (1 - t);
          y = p.y + Math.sin(m.ang) * L * (1 - t);
          break;
        case 'moveL': case 'moveR': case 'moveLR':
          x = p.x + m.dir * L * t;
          break;
        case 'moveU': case 'moveD': case 'moveUD':
          y = p.y + m.dir * L * t;
          break;
        case 'diagTL': case 'diagTR': case 'diagBL': case 'diagBR':
          x = p.x + m.sx0 + m.dx * L * t;
          y = p.y + m.sy0 + m.dy * L * t;
          break;
        case 'sway':  // 波动横移：横移同时正弦摆动
          x = p.x + m.dir * L * t;
          y = p.y + Math.sin(t * Math.PI * 2 * 3 + m.phase) * (cd || 0.4);
          break;
        case 'bounce': // 弹跳：上下弹跳并缓慢横移
          y = p.y + Math.abs(Math.sin(t * Math.PI * 4 + m.phase)) * (cd || 1);
          x = p.x + m.dir * L * t * 0.3;
          break;
        case 'spiral': // 螺旋扩散：向外扩散同时旋转
          x = p.x + Math.cos(m.ang + m.dir * ms * p.life) * L * t;
          y = p.y + Math.sin(m.ang + m.dir * ms * p.life) * L * t;
          break;
        case 'orbitCircle':
          var a = m.ang + m.dir * ms * p.life;
          x = p.x + Math.cos(a) * cd;
          y = p.y + Math.sin(a) * cd;
          break;
        case 'orbitSquare':
          var a2 = m.ang + m.dir * ms * p.life;
          var norm = Math.max(Math.abs(Math.cos(a2)), Math.abs(Math.sin(a2))) || 1;
          x = p.x + Math.cos(a2) * cd / norm;
          y = p.y + Math.sin(a2) * cd / norm;
          break;
      }
      return { x: x, y: y };
    }

    function emitOne(def, base, tex, frame) {
      var dur = Math.max(0.05, (def.lifetime || 2) + rand(0, def.lifetimeRandom || 0));
      var alpha = def.alpha == null ? 1 : def.alpha;
      var fadeOut = def.fadeOut || 0;
      var spd = function (v, rv) { return (v === undefined || v === null ? 1 : v) + rs(rv); };
      var p = {
        x: base.x + (def.absOffsetX || 0) + rs(def.absOffsetXRand),
        y: base.y + (def.absOffsetY || 0) + rs(def.absOffsetYRand),
        tx: (def.relOffsetX || 0) + rs(def.relOffsetXRand),
        ty: (def.relOffsetY || 0) + rs(def.relOffsetYRand),
        sx: spd(def.speedX, def.speedXRand),
        sy: spd(def.speedY, def.speedYRand),
        angle: (def.initialAngleZero ? 0 : (def.angleOffset || 0)) + rs(def.angleOffsetRand),
        spin: (def.spin || 0) + rs(def.spinRand),
        startScale: def.startScale == null ? 1 : def.startScale,
        endScale: def.endScale == null ? 1 : def.endScale,
        life: 0, dur: dur,
        fadeIn: def.fadeIn || 0,
        fadeOut: fadeOut,
        hold: Math.max(0, (alpha - 1) * fadeOut),
        alpha: Math.min(alpha, 1),
        tex: tex, frame: frame,
        frameProgress: frame ? (frame.start + Math.round(rand(-Math.abs(def.frameRandomStart || 0), Math.abs(def.frameRandomStart || 0)))) : 0,
        frameSpeed: (def.frameSpeed || 1) * 60 + rs((def.frameSpeedRand || 0) * 60),
        sprite: null,
      };
      var mo = motionParams(def);
      if (mo.type) p.motion = mo;
      p.sprite = spawnSprite(tex, def.color);
      p.sprite.position.set(p.x, p.y, 0.02 + Math.random() * 0.02);
      particles.push(p);
    }

    function emit(em) {
      var def = em.def;
      if (def.probability != null && Math.random() >= def.probability) return;
      var base = resolvePoint(em.pos);
      var count = Math.max(1, Math.round((def.count || 1) + rand(0, def.countRandom || 0)));
      var src = def.image || '';
      if (!src || src.indexOf('text:') === 0) {
        var tex = src ? makeTextTexture(src.slice(5)) : null;
        if (!tex) return;
        for (var i = 0; i < count; i++) emitOne(def, base, tex, null);
      } else {
        var url = src.indexOf('image:') === 0 ? src.slice(6) : src;
        var loader = new THREE.TextureLoader();
        loader.load(url, function (t) {
          t.colorSpace = THREE.SRGBColorSpace;
          var frame = null;
          if ((def.frameWidth && def.frameWidth < t.image.width - 1) || (def.frameHeight && def.frameHeight < t.image.height - 1)) {
            frame = makeFrameSet(t.image, def);
            drawFrame(frame, frame.start);
          }
          for (var i = 0; i < count; i++) emitOne(def, base, t, frame);
        });
      }
    }

    function update(p, dt) {
      p.life += dt;
      var total = p.dur + p.hold;
      if (p.life >= total) {
        anchor.remove(p.sprite);
        p.sprite.material.dispose();
        var i = particles.indexOf(p);
        if (i >= 0) particles.splice(i, 1);
        return;
      }
      var t = Math.min(1, p.life / p.dur);
      if (p.motion && p.motion.type) {
        var mp = motionPos(p, t);
        p.sprite.position.set(mp.x, mp.y, p.sprite.position.z);
      } else {
        var px = clamp01(p.life * Math.max(p.sx, 0));
        var py = clamp01(p.life * Math.max(p.sy, 0));
        p.sprite.position.set(p.x + p.tx * px, p.y + p.ty * py, p.sprite.position.z);
      }
      p.sprite.material.rotation = p.angle + p.spin * p.life;
      var s = p.startScale + (p.endScale - p.startScale) * t;
      p.sprite.scale.setScalar(Math.max(0.001, s));
      if (p.fadeIn > 0 && p.life < p.fadeIn) {
        p.sprite.material.opacity = p.alpha * (p.life / p.fadeIn);
      } else if (p.fadeOut > 0 && total - p.life < p.fadeOut) {
        p.sprite.material.opacity = p.alpha * Math.max(0, (total - p.life) / p.fadeOut);
      } else {
        p.sprite.material.opacity = p.alpha;
      }
      if (p.frame) {
        p.frameProgress += p.frameSpeed * dt;
        var idx = frameIndex(p);
        if (idx < 0) {
          anchor.remove(p.sprite);
          p.sprite.material.dispose();
          var j = particles.indexOf(p);
          if (j >= 0) particles.splice(j, 1);
          return;
        }
        drawFrame(p.frame, idx);
      }
    }

    function tick(dt) {
      for (var i = emitters.length - 1; i >= 0; i--) {
        var em = emitters[i];
        em.timer += dt;
        var interval = Math.max(0.05, (em.def.delay || 0) + rand(0, em.def.delayRandom || 0));
        while (em.timer >= interval) {
          em.timer -= interval;
          emit(em);
        }
      }
      for (var j = particles.length - 1; j >= 0; j--) update(particles[j], dt);
    }

    function init(list) {
      emitters = [];
      (list || []).forEach(function (item) {
        var def = item.def || {};
        var basePos = item.pos || { type: 'random' };
        if (def.subs && def.subs.length) {
          // 组合特效（多合一）：展开为多个子发射器，各自位置与定义
          def.subs.forEach(function (sub) {
            emitters.push({ def: sub.def || {}, pos: sub.pos || basePos, timer: 0 });
          });
        } else {
          emitters.push({ def: def, pos: basePos, timer: 0 });
        }
      });
    }

    return { init: init, tick: tick };
  })();
"""

# 前景组（透明主体）两种渲染顺序：
#   默认 —— 主体被边框遮盖（renderOrder 2/3，边框 4、卡封 5、文本 6）
#   浮于边框上 —— 主体提升到边框之上、卡封之下（z 0.013/0.014，renderOrder 4.2/4.4）
CARD_HTML_TEMPLATE_FG = r"""  const foregroundGroup = new THREE.Group();
  flipGroup.add(foregroundGroup);
  const shadowMesh = new THREE.Mesh(
    new THREE.PlaneGeometry(CARD_WIDTH, CARD_HEIGHT),
    new THREE.MeshBasicMaterial({ map: foregroundTexture, color: 0x1c1008, transparent: true, opacity: 0.42, alphaTest: 0.02, depthWrite: false, side: THREE.FrontSide, toneMapped: false })
  );
  shadowMesh.position.set(-0.028, -0.034, -0.004);
  shadowMesh.renderOrder = 2;
  foregroundGroup.add(shadowMesh);
  const fgMesh = new THREE.Mesh(
    new THREE.PlaneGeometry(CARD_WIDTH, CARD_HEIGHT),
    new THREE.MeshBasicMaterial({ map: foregroundTexture, transparent: true, opacity: 0.88, alphaTest: 0.02, depthWrite: false, side: THREE.FrontSide, toneMapped: false })
  );
  fgMesh.position.set(0, 0, 0.006);
  fgMesh.renderOrder = 3;
  foregroundGroup.add(fgMesh);"""

CARD_HTML_TEMPLATE_FG_FLOAT = r"""  const foregroundGroup = new THREE.Group();
  flipGroup.add(foregroundGroup);
  const shadowMesh = new THREE.Mesh(
    new THREE.PlaneGeometry(CARD_WIDTH, CARD_HEIGHT),
    new THREE.MeshBasicMaterial({ map: foregroundTexture, color: 0x1c1008, transparent: true, opacity: 0.42, alphaTest: 0.02, depthWrite: false, side: THREE.FrontSide, toneMapped: false })
  );
  shadowMesh.position.set(-0.03, -0.036, 0.013);
  shadowMesh.renderOrder = 4.2;
  foregroundGroup.add(shadowMesh);
  const fgMesh = new THREE.Mesh(
    new THREE.PlaneGeometry(CARD_WIDTH, CARD_HEIGHT),
    new THREE.MeshBasicMaterial({ map: foregroundTexture, transparent: true, opacity: 0.88, alphaTest: 0.02, depthWrite: false, side: THREE.FrontSide, toneMapped: false })
  );
  fgMesh.position.set(0, 0, 0.014);
  fgMesh.renderOrder = 4.4;
  foregroundGroup.add(fgMesh);"""

CARD_HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1, user-scalable=no">
<title>__CARD_NAME__ · 3D 镭射卡</title>
<style>
  * { margin: 0; padding: 0; box-sizing: border-box; }
  html, body { width: 100%; height: 100%; overflow: hidden; }
  body {
    background: radial-gradient(1200px 700px at 50% 20%, #2b2119 0%, #120d09 55%, #080605 100%);
    font-family: "Microsoft YaHei", "PingFang SC", system-ui, sans-serif;
  }
  #stage { position: fixed; inset: 0; }
  .hud {
    position: fixed; top: 18px; left: 0; right: 0; text-align: center;
    color: #f0d9a8; font-size: 20px; letter-spacing: 6px; text-shadow: 0 2px 12px rgba(0,0,0,.8);
    pointer-events: none; z-index: 2; font-weight: 600;
  }
  .hint {
    position: fixed; bottom: 16px; left: 0; right: 0; text-align: center;
    color: rgba(240, 217, 168, .45); font-size: 12px; letter-spacing: 2px;
    pointer-events: none; z-index: 2;
  }
  .loading {
    position: fixed; inset: 0; display: flex; align-items: center; justify-content: center;
    color: #d8c08a; font-size: 15px; letter-spacing: 4px; z-index: 3; background: #120d09;
    transition: opacity .5s;
  }
  .loading.hide { opacity: 0; pointer-events: none; }
</style>
</head>
<body>
<div class="hud">__CARD_NAME__</div>
<div class="hint">拖动旋转 · 滚轮缩放 · 双击翻面</div>
<div id="stage"></div>
<div class="loading" id="loading">加载中…</div>
<script>
__THREE_JS__
</script>
<script>
(function () {
  const CARD_WIDTH = 2.25;
  const CARD_HEIGHT = 3;
  const MIN_DISTANCE = 2.7;
  const MAX_DISTANCE = 10.5;
  const ZOOM_SENSITIVITY = 0.00075;
  const AURA_SCALE = 1.26;

  const stage = document.getElementById('stage');
  const scene = new THREE.Scene();
  const camera = new THREE.PerspectiveCamera(38, 1, 0.1, 100);
  const renderer = new THREE.WebGLRenderer({ antialias: true, alpha: true });
  renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
  stage.appendChild(renderer.domElement);

  // ---- 纹理 ----
  const loader = new THREE.TextureLoader();
  const frontTexture = loader.load('__FRONT__');
  var foregroundTexture = loader.load('__FOREGROUND__');
  const backTexture = loader.load('__BACK__');
  for (const t of [frontTexture, foregroundTexture, backTexture]) {
    t.colorSpace = THREE.SRGBColorSpace;
    t.anisotropy = 8;
  }
  __FG_ROUND_JS__

  // ---- 主卡面 shader（复刻：镭射箔光 + 背面景深平移 + 圆角） ----
  const cardMaterial = new THREE.ShaderMaterial({
    transparent: true,
    depthWrite: false,
    side: THREE.DoubleSide,
    toneMapped: false,
    uniforms: {
      uFrontTexture: { value: frontTexture },
      uBackTexture: { value: backTexture },
      uPointer: { value: new THREE.Vector2(0.48, 0.44) },
      uHover: { value: 0 },
      uAspect: { value: CARD_WIDTH / CARD_HEIGHT },
      uSeal: { value: 1.0 },
    },
    vertexShader: `
      varying vec2 vUv;
      varying vec3 vWorldNormal;
      varying vec3 vWorldPosition;
      varying vec3 vWorldXAxis;
      varying vec3 vWorldYAxis;
      void main() {
        vUv = uv;
        vWorldNormal = normalize(mat3(modelMatrix) * normal);
        vWorldXAxis = normalize((modelMatrix * vec4(1.0, 0.0, 0.0, 0.0)).xyz);
        vWorldYAxis = normalize((modelMatrix * vec4(0.0, 1.0, 0.0, 0.0)).xyz);
        vec4 worldPosition = modelMatrix * vec4(position, 1.0);
        vWorldPosition = worldPosition.xyz;
        gl_Position = projectionMatrix * viewMatrix * worldPosition;
      }
    `,
    fragmentShader: `
      uniform sampler2D uFrontTexture;
      uniform sampler2D uBackTexture;
      uniform vec2 uPointer;
      uniform float uHover;
      uniform float uAspect;
      uniform float uSeal;
      varying vec2 vUv;
      varying vec3 vWorldNormal;
      varying vec3 vWorldPosition;
      varying vec3 vWorldXAxis;
      varying vec3 vWorldYAxis;

      float roundedCard(vec2 uv, float radius) {
        vec2 scaled = vec2((uv.x - 0.5) * uAspect, uv.y - 0.5);
        vec2 halfSize = vec2(0.5 * uAspect, 0.5);
        vec2 edge = abs(scaled) - (halfSize - vec2(radius));
        return length(max(edge, 0.0)) + min(max(edge.x, edge.y), 0.0) - radius;
      }
      vec3 antiqueFoil(float value) {
        float wave = 0.5 + 0.5 * cos(6.283185 * value);
        return mix(vec3(0.11, 0.34, 0.25), vec3(1.0, 0.68, 0.25), wave);
      }
      vec3 screenBlend(vec3 base, vec3 light) {
        return 1.0 - (1.0 - base) * (1.0 - light);
      }
      void main() {
        if (roundedCard(vUv, 0.032) > 0.0) discard;
        vec3 viewDirection = normalize(cameraPosition - vWorldPosition);
        vec3 normal = normalize(vWorldNormal);
        float facing = clamp(dot(normal, viewDirection), 0.0, 1.0);
        float fresnel = pow(1.0 - facing, 2.15);
        // 视角反射箔光：反射向量投影到卡面，随旋转平滑流动（无静态条纹）
        vec3 reflW = reflect(-viewDirection, normal);
        vec3 planeC = reflW - normal * dot(reflW, normal);
        vec2 refl2 = normalize(vec2(dot(planeC, normalize(vWorldXAxis)), dot(planeC, normalize(vWorldYAxis))) + 1e-5);
        float foilAlong = dot(refl2, normalize(vec2(0.72, 0.98)));
        vec2 uv = gl_FrontFacing ? vUv : vec2(1.0 - vUv.x, vUv.y);
        vec4 base;
        if (gl_FrontFacing) {
          base = texture2D(uFrontTexture, uv);
        } else {
          vec2 pointerLook = (uPointer - vec2(0.5)) * uHover;
          vec2 cameraLook = vec2(
            -dot(viewDirection, normalize(vWorldXAxis)),
            dot(viewDirection, normalize(vWorldYAxis))
          );
          vec2 look = pointerLook + cameraLook * 0.12;
          float left = smoothstep(0.055, 0.16, uv.x);
          float right = 1.0 - smoothstep(0.84, 0.945, uv.x);
          float top = smoothstep(0.055, 0.14, uv.y);
          float bottom = 1.0 - smoothstep(0.86, 0.945, uv.y);
          float inner = left * right * top * bottom;
          float lowerLandscape = 1.0 - smoothstep(0.28, 0.74, uv.y);
          float titleDistance = length((uv - vec2(0.5, 0.52)) * vec2(1.0, 0.82));
          float titleLock = 1.0 - smoothstep(0.17, 0.33, titleDistance);
          float sceneMask = inner * (1.0 - titleLock * 0.9);
          float depth = mix(0.24, 1.0, lowerLandscape);
          vec2 translation = look * (0.005 + 0.011 * depth);
          vec2 backUv = clamp(uv + translation * sceneMask, 0.001, 0.999);
          base = texture2D(uBackTexture, backUv);
        }
        if (!gl_FrontFacing) {
          // 复刻原版 xiaoqishuo 背面：卡背图 + 暖色边缘光（默认无箔光，卡封另加效果）
          vec3 backLight = vec3(0.72, 0.48, 0.19);
          vec3 backColor = screenBlend(base.rgb * 0.88, backLight * fresnel * 0.22);
          gl_FragColor = vec4(backColor, base.a);
          return;
        }
        vec2 lightUv = mix(vec2(0.48, 0.44), uPointer, uHover);
        vec2 centered = (uv - lightUv) * vec2(0.76, 1.0);
        float radialGlare = 0.72 * (1.0 - smoothstep(0.04, 0.9, length(centered)));
        float activity = uHover * 0.76;
        float tiltAmt = 1.0 - facing;
        float viewBoost = 1.0 + smoothstep(0.15, 0.65, tiltAmt) * 0.6;
        // 复刻原版 xiaoqishuo 默认光效：对角箔光带 + 白色反光 + 径向眩光 + 菲涅尔
        vec2 shiftedUv = uv + vec2(
          (lightUv.x - 0.5) * 0.42,
          (lightUv.y - 0.5) * 0.3
        );
        float foilFlow = shiftedUv.x * 0.72 + shiftedUv.y * 0.98 + fresnel * 0.44;
        float wideBand = pow(max(0.0, sin(foilFlow * 14.0 + 1.2)), 4.5);
        float thinBand = pow(max(0.0, sin((shiftedUv.x * 0.84 - shiftedUv.y * 0.62) * 108.0)), 16.0);
        float crossBand = pow(max(0.0, sin((shiftedUv.x * 0.52 + shiftedUv.y * 1.28) * 68.0)), 18.0);
        float foilMask = (0.14 + fresnel * 0.86) * (0.28 + radialGlare * 0.72);
        vec3 foil = antiqueFoil(foilFlow * 0.74 + shiftedUv.y * 0.22 + fresnel * 0.38);
        vec3 secondFoil = antiqueFoil(shiftedUv.x * 0.28 - shiftedUv.y * 0.8 + 0.24);

        vec3 color = base.rgb;
        color = screenBlend(
          color,
          foil * (wideBand * 0.54 + thinBand * 0.3 + crossBand * 0.18) * foilMask * (0.18 + activity * 0.42) * viewBoost * uSeal
        );
        color = screenBlend(color, secondFoil * radialGlare * (0.08 + activity * 0.28) * viewBoost * uSeal);
        color += vec3(1.0, 0.89, 0.67) * radialGlare * wideBand * (0.04 + activity * 0.11) * viewBoost * uSeal;
        color += foil * fresnel * (0.08 + activity * 0.1) * uSeal;
        gl_FragColor = vec4(color, base.a);
      }
    `,
  });

  // ---- 辉光 aura ----
  const auraMaterial = new THREE.ShaderMaterial({
    transparent: true,
    depthWrite: false,
    depthTest: false,
    side: THREE.DoubleSide,
    blending: THREE.AdditiveBlending,
    toneMapped: false,
    uniforms: { uAspect: { value: CARD_WIDTH / CARD_HEIGHT }, uScale: { value: AURA_SCALE } },
    vertexShader: `
      varying vec2 vUv;
      void main() {
        vUv = uv;
        gl_Position = projectionMatrix * modelViewMatrix * vec4(position, 1.0);
      }
    `,
    fragmentShader: `
      uniform float uAspect;
      uniform float uScale;
      varying vec2 vUv;
      float cardDistance(vec2 uv) {
        vec2 scaled = vec2((uv.x - 0.5) * uAspect * uScale, (uv.y - 0.5) * uScale);
        vec2 halfSize = vec2(0.5 * uAspect, 0.5);
        float radius = 0.032;
        vec2 edge = abs(scaled) - (halfSize - vec2(radius));
        return length(max(edge, 0.0)) + min(max(edge.x, edge.y), 0.0) - radius;
      }
      void main() {
        float distance = cardDistance(vUv);
        float outside = step(0.0, distance);
        float tightGlow = 1.0 - smoothstep(0.0, 0.014, distance);
        float softGlow = 1.0 - smoothstep(0.006, 0.06, distance);
        vec3 glowColor = mix(vec3(0.44, 0.08, 0.025), vec3(0.98, 0.63, 0.18), vUv.y);
        float alpha = outside * (tightGlow * 0.14 + softGlow * 0.045);
        gl_FragColor = vec4(glowColor, alpha);
      }
    `,
  });

  // ---- 卡片组 ----
  const floatGroup = new THREE.Group();
  const tiltGroup = new THREE.Group();
  const flipGroup = new THREE.Group();
  floatGroup.add(tiltGroup);
  tiltGroup.add(flipGroup);

  const cardMesh = new THREE.Mesh(new THREE.PlaneGeometry(CARD_WIDTH, CARD_HEIGHT), cardMaterial);
  flipGroup.add(cardMesh);

  const aura = new THREE.Mesh(
    new THREE.PlaneGeometry(CARD_WIDTH * AURA_SCALE, CARD_HEIGHT * AURA_SCALE),
    auraMaterial
  );
  aura.position.set(0, -0.026, -0.025);
  aura.renderOrder = -1;
  flipGroup.add(aura);

  __FG_BLOCK__

  // ---- 主体描边层（白色贴纸边，可选；随前景组视差移动） ----
  __OUTLINE_LAYER__

  // ---- 边框 / 卡封叠加层（可选） ----
  __FRAME_LAYER__
  __SEAL_LAYER__

  // ---- 卡面描述文字层（可选，Canvas 纹理叠加） ----
  __TEXT_LAYER__

  scene.add(floatGroup);

  // ---- 特效系统（__EFFECTS__ 注入特效列表） ----
  var effectAnchor = new THREE.Group();
  flipGroup.add(effectAnchor);
  window.__EFFECT_ANCHOR__ = effectAnchor;
__ENGINE_JS__

  var EFFECTS = __EFFECTS__;
  __fx.init(EFFECTS);

  // ---- 轨道控制（手写：拖拽旋转 + 滚轮/双指缩放 + 阻尼） ----
  const state = {
    theta: 0, phi: Math.PI / 2, radius: 5.1,
    targetTheta: 0, targetPhi: Math.PI / 2, targetRadius: 5.1,
  };
  const pointers = new Map();
  let prevPinch = 0;
  let targetFlip = 0;          // 0=正面 1=背面
  let autoRotate = false;      // 自动旋转开关
  let snapPause = 0;           // 归正期间暂停自动旋转（秒），让归正可见
  const AUTO_ROTATE_SPEED = 1.92; // 弧度/秒（约 3.3 秒一圈）

  function damp(current, target, lambda, dt) {
    return THREE.MathUtils.lerp(current, target, 1 - Math.exp(-lambda * dt));
  }

  function setRadius(r) {
    state.targetRadius = THREE.MathUtils.clamp(r, MIN_DISTANCE, MAX_DISTANCE);
  }

  const canvas = renderer.domElement;
  canvas.addEventListener('wheel', (e) => {
    e.preventDefault();
    const modeFactor = e.deltaMode === 1 ? 16 : e.deltaMode === 2 ? window.innerHeight : 1;
    const rawDelta = THREE.MathUtils.clamp(e.deltaY * modeFactor, -180, 180);
    setRadius(state.targetRadius * Math.exp(rawDelta * ZOOM_SENSITIVITY));
  }, { passive: false });

  canvas.addEventListener('pointerdown', (e) => {
    pointers.set(e.pointerId, e);
    if (pointers.size === 2) prevPinch = pinchDistance();
    canvas.setPointerCapture(e.pointerId);
    if (pointers.size === 1) tapStart = { x: e.clientX, y: e.clientY };
  });
  canvas.addEventListener('pointermove', (e) => {
    if (pointers.has(e.pointerId) && pointers.size === 1) {
      const last = pointers.get(e.pointerId);
      state.targetTheta -= (e.clientX - last.clientX) * 0.0058;
      state.targetPhi -= (e.clientY - last.clientY) * 0.0058;
      state.targetPhi = THREE.MathUtils.clamp(state.targetPhi, 0.42, Math.PI - 0.42);
      pointers.set(e.pointerId, e);
    } else if (pointers.size === 2) {
      const d = pinchDistance();
      if (prevPinch > 0 && d > 0) setRadius(state.targetRadius * (prevPinch / d));
      prevPinch = d;
    }
    updateHover(e.clientX, e.clientY);
  });
  const endPointer = (e) => {
    pointers.delete(e.pointerId);
    if (pointers.size < 2) prevPinch = 0;
  };
  // 双击反转：用 pointerup 手动检测（原生 dblclick 在 setPointerCapture 下可能不触发）
  let lastTapTime = 0, lastTapX = 0, lastTapY = 0, tapStart = null;
  function doFlip() {
    targetFlip = targetFlip ? 0 : 1;
    // 自动归正：theta 归到最近的整圈，并短暂暂停自动旋转让归正可见
    state.targetTheta = Math.round(state.theta / (2 * Math.PI)) * 2 * Math.PI;
    snapPause = 0.7;
  }
  canvas.addEventListener('pointerup', (e) => {
    const wasTap = tapStart && Math.hypot(e.clientX - tapStart.x, e.clientY - tapStart.y) < 10;
    tapStart = null;
    endPointer(e);
    if (!wasTap) return;
    const now = performance.now();
    if (now - lastTapTime < 350 && Math.hypot(e.clientX - lastTapX, e.clientY - lastTapY) < 10) {
      doFlip();
      lastTapTime = 0;
    } else {
      lastTapTime = now;
      lastTapX = e.clientX;
      lastTapY = e.clientY;
    }
  });
  canvas.addEventListener('pointercancel', endPointer);
  canvas.addEventListener('pointerleave', endPointer);

  function pinchDistance() {
    const pts = [...pointers.values()];
    if (pts.length < 2) return 0;
    return Math.hypot(pts[0].clientX - pts[1].clientX, pts[0].clientY - pts[1].clientY);
  }

  // ---- 悬停：指针 UV + 倾斜 + 前景视差 ----
  const pointer = new THREE.Vector2(0.48, 0.44);
  const hoverTarget = new THREE.Vector2();
  const hoverCurrent = new THREE.Vector2();
  const hover = { current: 0 };
  const raycaster = new THREE.Raycaster();
  const ndc = new THREE.Vector2();

  function updateHover(px, py) {
    const rect = canvas.getBoundingClientRect();
    ndc.set(((px - rect.left) / rect.width) * 2 - 1, -((py - rect.top) / rect.height) * 2 + 1);
    raycaster.setFromCamera(ndc, camera);
    const hits = raycaster.intersectObject(cardMesh);
    if (hits.length) {
      pointer.copy(hits[0].uv);
      hover.current = 1;
      hoverTarget.set((hits[0].uv.x - 0.5) * 0.4, -(hits[0].uv.y - 0.5) * 0.34);
    } else {
      hover.current = 0;
      hoverTarget.set(0, 0);
    }
  }

  // ---- 相机响应式 ----
  function resize() {
    const w = window.innerWidth, h = window.innerHeight;
    renderer.setSize(w, h);
    camera.aspect = w / h;
    const narrow = w / h < 0.72;
    camera.position.set(0, 0, narrow ? 6.8 : 5.1);
    camera.fov = narrow ? 40 : 38;
    camera.updateProjectionMatrix();
    if (narrow) state.targetRadius = 6.8;
  }
  window.addEventListener('resize', resize);
  resize();

  // ---- 主循环 ----
  const clock = new THREE.Clock();
  function animate() {
    requestAnimationFrame(animate);
    const dt = Math.min(clock.getDelta(), 0.05);

    state.theta = damp(state.theta, state.targetTheta, 8, dt);
    state.phi = damp(state.phi, state.targetPhi, 8, dt);
    state.radius = damp(state.radius, state.targetRadius, 9.5, dt);

    // 自动旋转：缓慢推进目标角度（反向）；翻面：flipGroup 绕 Y 轴转 180°
    if (autoRotate) {
      if (snapPause > 0) snapPause -= dt;
      else state.targetTheta -= AUTO_ROTATE_SPEED * dt;
    }
    flipGroup.rotation.y = damp(flipGroup.rotation.y, targetFlip * Math.PI, 4.5, dt);

    const sp = Math.sin(state.phi), cp = Math.cos(state.phi);
    camera.position.set(
      state.radius * sp * Math.sin(state.theta),
      state.radius * cp,
      state.radius * sp * Math.cos(state.theta)
    );
    camera.lookAt(0, 0, 0);

    floatGroup.scale.setScalar(0.76);
    floatGroup.position.y = damp(floatGroup.position.y, 0.035, 7.5, dt);
    floatGroup.rotation.z = damp(floatGroup.rotation.z, 0, 8, dt);

    hoverCurrent.x = damp(hoverCurrent.x, hoverTarget.x, 8.5, dt);
    hoverCurrent.y = damp(hoverCurrent.y, hoverTarget.y, 8.5, dt);
    tiltGroup.rotation.x = hoverCurrent.y * 0.72;
    tiltGroup.rotation.y = hoverCurrent.x * 0.72;

    foregroundGroup.position.x = damp(foregroundGroup.position.x, hoverCurrent.x * 0.2, 9.5, dt);
    foregroundGroup.position.y = damp(foregroundGroup.position.y, -hoverCurrent.y * 0.14, 9.5, dt);
    foregroundGroup.position.z = damp(foregroundGroup.position.z, 0.024, 9.5, dt);
    foregroundGroup.rotation.x = hoverCurrent.y * 0.18;
    foregroundGroup.rotation.y = hoverCurrent.x * 0.18;

    cardMaterial.uniforms.uHover.value = damp(cardMaterial.uniforms.uHover.value, hover.current, 11, dt);
    cardMaterial.uniforms.uPointer.value.copy(pointer);
__SEAL_TICK__

    __fx.tick(dt);

    renderer.render(scene, camera);
  }
  animate();

  // ---- 供收集册等外部页面调用的控制接口 ----
  window.__cardAPI = {
    setAutoRotate: function (enable) {
      autoRotate = !!enable;
    },
    flipCard: function () {
      doFlip();
    },
  };

  window.addEventListener('load', () => {
    const loading = document.getElementById('loading');
    setTimeout(() => loading.classList.add('hide'), 350);
  });
})();
</script>
</body>
</html>
"""

PREVIEW_HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>特效预览</title>
<style>
  * { margin: 0; padding: 0; box-sizing: border-box; }
  html, body { width: 100%; height: 100%; overflow: hidden; background: #14100c; }
  #stage { position: fixed; inset: 0; }
  .label {
    position: fixed; top: 12px; left: 0; right: 0; text-align: center;
    color: #a8916b; font: 12px "Microsoft YaHei", sans-serif; letter-spacing: 3px;
    pointer-events: none; z-index: 2;
  }
</style>
</head>
<body>
<div class="label">特效预览 · 自动循环触发</div>
<div id="stage"></div>
<script>
__THREE_JS__
</script>
<script>
(function () {
  var CARD_WIDTH = 2.25;
  var CARD_HEIGHT = 3;
  var stage = document.getElementById('stage');
  var scene = new THREE.Scene();
  var camera = new THREE.PerspectiveCamera(40, 1, 0.1, 100);
  var renderer = new THREE.WebGLRenderer({ antialias: true, alpha: true });
  renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
  stage.appendChild(renderer.domElement);

  // 占位卡面（浅色圆角矩形，代表卡牌区域）
  var cardPlane = new THREE.Mesh(
    new THREE.PlaneGeometry(CARD_WIDTH, CARD_HEIGHT),
    new THREE.ShaderMaterial({
      transparent: true, toneMapped: false, side: THREE.DoubleSide,
      uniforms: { uAspect: { value: CARD_WIDTH / CARD_HEIGHT } },
      vertexShader: 'varying vec2 vUv; void main(){ vUv=uv; gl_Position=projectionMatrix*modelViewMatrix*vec4(position,1.0); }',
      fragmentShader: [
        'uniform float uAspect; varying vec2 vUv;',
        'float rounded(vec2 uv, float r){ vec2 s=vec2((uv.x-0.5)*uAspect,uv.y-0.5); vec2 h=vec2(0.5*uAspect,0.5); vec2 e=abs(s)-(h-vec2(r)); return length(max(e,0.0))+min(max(e.x,e.y),0.0)-r; }',
        'void main(){ if(rounded(vUv,0.032)>0.0) discard;',
        '  vec3 base=mix(vec3(0.36,0.31,0.25),vec3(0.20,0.16,0.12),vUv.y);',
        '  vec3 edge=vec3(0.58,0.47,0.31)*smoothstep(0.032,0.0,rounded(vUv,0.032));',
        '  gl_FragColor=vec4(base+edge,1.0); }',
      ].join('\n'),
    })
  );
  scene.add(cardPlane);

  window.__EFFECT_ANCHOR__ = new THREE.Group();
  scene.add(window.__EFFECT_ANCHOR__);
__ENGINE_JS__

  var EFFECTS = __EFFECTS__;
  __fx.init(EFFECTS);

  function resize() {
    var w = window.innerWidth, h = window.innerHeight;
    renderer.setSize(w, h);
    camera.aspect = w / h;
    camera.position.set(0, 0, 5.6);
    camera.lookAt(0, 0, 0);
    camera.updateProjectionMatrix();
  }
  window.addEventListener('resize', resize);
  resize();

  var clock = new THREE.Clock();
  function animate() {
    requestAnimationFrame(animate);
    var dt = Math.min(clock.getDelta(), 0.05);
    __fx.tick(dt);
    renderer.render(scene, camera);
  }
  animate();
})();
</script>
</body>
</html>
"""


def _json(data) -> str:
    return json.dumps(data, ensure_ascii=False)


def _overlay_layer_js(rel: str | None, z: float, order: int, side: str = "THREE.DoubleSide", opacity: float = 1.0) -> str:
    """边框/卡封叠加层 JS 片段（rel 为空时返回空串）。"""
    if not rel:
        return ""
    return (
        "  (function () {\n"
        "    var t = loader.load(" + json.dumps(rel) + ");\n"
        "    t.colorSpace = THREE.SRGBColorSpace;\n"
        "    t.anisotropy = 8;\n"
        "    var m = new THREE.Mesh(\n"
        "      new THREE.PlaneGeometry(CARD_WIDTH, CARD_HEIGHT),\n"
        "      new THREE.MeshBasicMaterial({ map: t, transparent: true, depthWrite: false, side: " + side + ", toneMapped: false, opacity: " + str(opacity) + " })\n"
        "    );\n"
        "    m.position.set(0, 0, " + str(z) + ");\n"
        "    m.renderOrder = " + str(order) + ";\n"
        "    flipGroup.add(m);\n"
        "  })();\n"
    )


def _outline_layer_js(rel: str | None, z: float, order: float) -> str:
    """主体白色描边层 JS（贴纸边，前景组内、主体之上，仅正面渲染）。"""
    if not rel:
        return ""
    return (
        "  (function () {\n"
        "    var t = loader.load(" + json.dumps(rel) + ");\n"
        "    t.colorSpace = THREE.SRGBColorSpace;\n"
        "    t.anisotropy = 8;\n"
        "    var m = new THREE.Mesh(\n"
        "      new THREE.PlaneGeometry(CARD_WIDTH, CARD_HEIGHT),\n"
        "      new THREE.MeshBasicMaterial({ map: t, transparent: true, depthWrite: false, side: THREE.FrontSide, toneMapped: false })\n"
        "    );\n"
        "    m.position.set(0, 0, " + str(z) + ");\n"
        "    m.renderOrder = " + str(order) + ";\n"
        "    foregroundGroup.add(m);\n"
        "  })();\n"
    )


def _frame_layer_js(rel: str | None, z: float, order: int, fit_subject: bool = False) -> str:
    """边框叠加层 JS。fit_subject 时按前景主体包围盒缩放边框（否则整卡），仅正面渲染。

    主体包围盒在浏览器端从前景纹理 alpha 计算（无需服务端 numpy），
    边框缩放到包围盒 + 边距并居中，保证未开启浮于边框时边框与主体等高或略高。
    """
    if not rel:
        return ""
    if fit_subject:
        return (
            "  (function () {\n"
            "    var t = loader.load(" + json.dumps(rel) + ");\n"
            "    t.colorSpace = THREE.SRGBColorSpace;\n"
            "    t.anisotropy = 8;\n"
            "    var pad = 0.03;\n"
            "    function addFrame(fw, fh, cx, cy) {\n"
            "      var m = new THREE.Mesh(\n"
            "        new THREE.PlaneGeometry(fw * CARD_WIDTH, fh * CARD_HEIGHT),\n"
            "        new THREE.MeshBasicMaterial({ map: t, transparent: true, depthWrite: false, side: THREE.FrontSide, toneMapped: false })\n"
            "      );\n"
            "      // 卡面中心为原点、Y 轴向上：cx/cy 是图像坐标（0~1，y 向下），需反转并减 0.5 居中\n"
            "      m.position.set((cx - 0.5) * CARD_WIDTH, (0.5 - cy) * CARD_HEIGHT, " + str(z) + ");\n"
            "      m.renderOrder = " + str(order) + ";\n"
            "      flipGroup.add(m);\n"
            "    }\n"
            "    var tries = 0;\n"
            "    function fit() {\n"
            "      var img = foregroundTexture.image;\n"
            "      if (!img || !img.width) { if (++tries > 200) { addFrame(1, 1, 0.5, 0.5); return; } setTimeout(fit, 30); return; }\n"
            "      var sw = 200, sh = Math.max(1, Math.round(sw * img.height / img.width));\n"
            "      var cv = document.createElement('canvas');\n"
            "      cv.width = sw; cv.height = sh;\n"
            "      var ctx = cv.getContext('2d');\n"
            "      ctx.drawImage(img, 0, 0, sw, sh);\n"
            "      var d = ctx.getImageData(0, 0, sw, sh).data;\n"
            "      var x0 = sw, y0 = sh, x1 = -1, y1 = -1;\n"
            "      for (var y = 0; y < sh; y++) {\n"
            "        for (var x = 0; x < sw; x++) {\n"
            "          if (d[(y * sw + x) * 4 + 3] > 10) {\n"
            "            if (x < x0) x0 = x; if (x > x1) x1 = x;\n"
            "            if (y < y0) y0 = y; if (y > y1) y1 = y;\n"
            "          }\n"
            "        }\n"
            "      }\n"
            "      if (x1 < 0) { addFrame(1, 1, 0.5, 0.5); return; }\n"
            "      var fw = Math.min((x1 - x0 + 1) / sw + 2 * pad, 1.0);\n"
            "      var fh = Math.min((y1 - y0 + 1) / sh + 2 * pad, 1.0);\n"
            "      var cx = Math.min(Math.max(((x0 + x1 + 1) / 2) / sw, fw / 2), 1 - fw / 2);\n"
            "      var cy = Math.min(Math.max(((y0 + y1 + 1) / 2) / sh, fh / 2), 1 - fh / 2);\n"
            "      addFrame(fw, fh, cx, cy);\n"
            "    }\n"
            "    fit();\n"
            "  })();\n"
        )
    return _overlay_layer_js(rel, z, order, "THREE.FrontSide")


# ---- 冷裱膜动态光效（内置膜名 → 光效参数） ----
# mode: sparkle=图案点阵闪烁 / beam=定向光带 / rainbow=随角变色 / glow=柔光晕 / none=无
# pattern: canvas 图样（dot/sparkle/cross/diamond/star5/heart/snowflake/sakura/butterfly/pinwheel）
SEAL_EFFECTS = {
    "透明膜": {"mode": "none"},
    "哑光膜": {"mode": "none"},
    "磨砂膜": {"mode": "glow", "color": (214, 222, 232), "strength": 0.22, "grain": 7.0, "speed": 0.8, "rainbow": 0.3},
    "满天星闪光膜": {"mode": "sparkle", "pattern": "sparkle", "color": (255, 240, 200), "scale": 9.0, "strength": 0.72, "speed": 1.3, "rainbow": 0.65, "twinkle": 1.0, "twinkleShape": 1.0},
    "拉丝闪光膜": {"mode": "beam", "dir": (0.97, 0.26), "color": (238, 222, 190), "scale": 26.0, "power": 3.5, "strength": 0.68, "speed": 1.2, "rainbow": 0.7},
    "星幻细闪膜": {"mode": "sparkle", "pattern": "dot", "color": (255, 255, 250), "scale": 16.0, "strength": 0.5, "speed": 1.0, "rainbow": 0.55, "twinkle": 1.0, "twinkleShape": 1.0},
    "蚕丝膜": {"mode": "beam", "dir": (0.35, 0.94), "color": (226, 228, 236), "scale": 9.0, "power": 2.5, "strength": 0.7, "speed": 0.9, "rainbow": 0.6},
    "钻石膜": {"mode": "sparkle", "pattern": "diamond", "color": (255, 250, 232), "scale": 8.0, "strength": 0.68, "speed": 1.2, "rainbow": 0.65, "twinkle": 1.0, "twinkleShape": 1.0},
    "油画膜": {"mode": "glow", "color": (226, 192, 142), "strength": 0.5, "grain": 4.5, "speed": 0.7, "rainbow": 0.35},
    "十字膜": {"mode": "sparkle", "pattern": "cross", "color": (255, 248, 226), "scale": 10.0, "strength": 0.68, "speed": 1.3, "rainbow": 0.6, "twinkle": 1.0, "twinkleShape": 1.0},
    "皮纹膜": {"mode": "glow", "color": (212, 182, 142), "strength": 0.42, "grain": 12.0, "speed": 0.9, "rainbow": 0.3},
    "猫眼膜": {"mode": "sparkle", "pattern": "dot", "color": (255, 244, 212), "scale": 5.0, "strength": 0.85, "speed": 0.6, "rainbow": 0.7, "twinkle": 1.0, "twinkleShape": 1.0},
    "玻璃膜": {"mode": "beam", "dir": (0.8, 0.6), "color": (255, 255, 255), "scale": 4.0, "power": 2.0, "strength": 0.7, "speed": 0.7, "rainbow": 0.6},
    "彩虹膜": {"mode": "rainbow", "strength": 0.9, "power": 2.5, "dir": (0.87, 0.5)},
    "星空膜": {"mode": "sparkle", "pattern": "dot", "color": (196, 210, 255), "scale": 18.0, "strength": 0.8, "speed": 0.5, "rainbow": 0.55, "twinkle": 1.0, "twinkleShape": 1.0},
    "烟花膜": {"mode": "sparkle", "pattern": "sparkle", "color": (255, 182, 162), "scale": 6.0, "strength": 0.75, "speed": 1.5, "rainbow": 0.6, "twinkle": 1.0, "twinkleShape": 1.0},
    "爱心膜": {"mode": "sparkle", "pattern": "heart", "color": (255, 170, 186), "scale": 9.0, "strength": 0.7, "speed": 1.2, "rainbow": 0.65, "twinkle": 1.0, "twinkleShape": 1.0},
    "小星星膜": {"mode": "sparkle", "pattern": "star5", "color": (255, 236, 172), "scale": 12.0, "strength": 0.72, "speed": 1.1, "rainbow": 0.6, "twinkle": 1.0, "twinkleShape": 1.0},
    "星星膜": {"mode": "sparkle", "pattern": "star5", "color": (255, 230, 152), "scale": 6.0, "strength": 0.72, "speed": 1.0, "rainbow": 0.6, "twinkle": 1.0, "twinkleShape": 1.0},
    "雪花膜": {"mode": "sparkle", "pattern": "snowflake", "color": (226, 240, 255), "scale": 8.0, "strength": 0.75, "speed": 1.0, "rainbow": 0.5, "twinkle": 1.0, "twinkleShape": 1.0},
    "樱花膜": {"mode": "sparkle", "pattern": "sakura", "color": (255, 190, 206), "scale": 10.0, "strength": 0.72, "speed": 1.1, "rainbow": 0.6, "twinkle": 1.0, "twinkleShape": 1.0},
    "闪点膜": {"mode": "sparkle", "pattern": "dot", "color": (255, 250, 236), "scale": 14.0, "strength": 0.68, "speed": 1.2, "rainbow": 0.55, "twinkle": 1.0, "twinkleShape": 1.0},
    "星光细闪膜": {"mode": "sparkle", "pattern": "dot", "color": (255, 250, 235), "scale": 7.0, "strength": 0.62, "speed": 1.0, "rainbow": 0.6, "twinkle": 1.0},
    "测试膜": {"mode": "sparkle", "pattern": "dot", "color": (255, 250, 235), "scale": 7.0, "strength": 0.62, "speed": 1.0, "rainbow": 0.6, "twinkle": 1.0, "twinkleShape": 1.0},
    "流麻膜": {"mode": "sparkle", "pattern": "dot", "color": (255, 250, 235), "scale": 14.0, "strength": 0.6, "speed": 1.2, "rainbow": 0.55, "twinkleFlow": 1.0, "twinkleOnly": 1.0},
    "彩珠膜": {"mode": "sparkle", "pattern": "dot", "color": (255, 222, 182), "scale": 7.0, "strength": 0.75, "speed": 1.1, "rainbow": 0.6, "twinkle": 1.0, "twinkleShape": 1.0},
    "句号膜": {"mode": "sparkle", "pattern": "dot", "color": (255, 246, 226), "scale": 11.0, "strength": 0.7, "speed": 1.0, "rainbow": 0.5, "twinkle": 1.0, "twinkleShape": 1.0},
    "蝴蝶膜": {"mode": "sparkle", "pattern": "butterfly", "color": (255, 202, 192), "scale": 6.0, "strength": 0.72, "speed": 0.9, "rainbow": 0.6, "twinkle": 1.0, "twinkleShape": 1.0},
    "斜光柱膜": {"mode": "beam", "dir": (0.87, 0.5), "color": (255, 240, 200), "scale": 3.5, "power": 1.8, "strength": 0.68, "speed": 0.9, "rainbow": 0.7},
    "风车膜": {"mode": "sparkle", "pattern": "pinwheel", "color": (255, 212, 152), "scale": 8.0, "strength": 0.72, "speed": 1.2, "rainbow": 0.6, "twinkle": 1.0, "twinkleShape": 1.0},
    # A 类：真密铺（格子形状即图案，图案互相重叠、无方块感，整格闪光，学星光细闪膜）
    "A11三角格膜": {"mode": "grid", "gridType": "tri", "color": (255, 248, 226), "scale": 6.0, "strength": 0.72, "speed": 1.1, "rainbow": 0.6, "twinkle": 1.0},
    "A22圆格膜": {"mode": "grid", "gridType": "circle", "color": (255, 250, 235), "scale": 6.5, "strength": 0.72, "speed": 1.0, "rainbow": 0.6, "twinkle": 1.0},
    "A33六角格膜": {"mode": "grid", "gridType": "hex", "pattern": "hexagon", "color": (255, 248, 226), "scale": 5.0, "strength": 0.7, "speed": 1.0, "rainbow": 0.6, "twinkle": 1.0},
    "A44碎玻璃膜": {"mode": "voronoi", "color": (255, 250, 238), "scale": 7.0, "strength": 0.72, "speed": 1.0, "rainbow": 0.75},
    "A55雪花格膜": {"mode": "grid", "gridType": "hex", "pattern": "snowflake6", "color": (226, 240, 255), "scale": 4.5, "strength": 0.75, "speed": 1.0, "rainbow": 0.55, "twinkle": 1.0},
    "A66几何格膜": {"mode": "grid", "gridType": "hex", "pattern": "hexagram", "color": (255, 248, 226), "scale": 4.5, "strength": 0.72, "speed": 1.1, "rainbow": 0.6, "twinkle": 1.0},
    # B 类：格子平铺后格子内再画图形（仅图形闪光，学测试膜）
    "B11圆环膜": {"mode": "sparkle", "pattern": "ring", "color": (255, 250, 235), "scale": 7.0, "strength": 0.7, "speed": 1.1, "rainbow": 0.6, "twinkle": 1.0, "twinkleShape": 1.0},
    "B22闪电膜": {"mode": "sparkle", "pattern": "lightning", "color": (255, 244, 210), "scale": 7.0, "strength": 0.72, "speed": 1.3, "rainbow": 0.6, "twinkle": 1.0, "twinkleShape": 1.0},
    "B33齿轮膜": {"mode": "sparkle", "pattern": "gear", "color": (255, 246, 226), "scale": 6.0, "strength": 0.72, "speed": 1.2, "rainbow": 0.6, "twinkle": 1.0, "twinkleShape": 1.0},
    "纹路1": {"mode": "sparkle", "pattern": "circuit", "color": (255, 244, 220), "scale": 8.0, "strength": 0.72, "speed": 1.2, "rainbow": 0.65, "twinkle": 1.0, "twinkleShape": 1.0},
}

_SEAL_PATTERN_JS = r"""
  function sealStarPath(c, cx, cy, R, r) {
    c.beginPath();
    for (var i = 0; i < 10; i++) {
      var a = -Math.PI / 2 + i * Math.PI / 5;
      var rad = i % 2 === 0 ? R : r;
      var px = cx + rad * Math.cos(a), py = cy + rad * Math.sin(a);
      if (i === 0) c.moveTo(px, py); else c.lineTo(px, py);
    }
    c.closePath();
  }
  function sealHeartPath(c, cx, cy, s) {
    c.beginPath();
    for (var i = 0; i <= 60; i++) {
      var t = i / 60 * Math.PI * 2;
      var hx = 16 * Math.pow(Math.sin(t), 3);
      var hy = 13 * Math.cos(t) - 5 * Math.cos(2 * t) - 2 * Math.cos(3 * t) - Math.cos(4 * t);
      if (i === 0) c.moveTo(cx + hx * s, cy - hy * s); else c.lineTo(cx + hx * s, cy - hy * s);
    }
    c.closePath();
  }
  function sealPattern(type) {
    var cv = document.createElement('canvas');
    cv.width = cv.height = 256;
    var c = cv.getContext('2d');
    c.fillStyle = '#ffffff';
    c.strokeStyle = '#ffffff';
    c.lineCap = 'round';
    if (type === 'dot') {
      c.beginPath(); c.arc(128, 128, 34, 0, 6.2832); c.fill();
    } else if (type === 'sparkle') {
      c.lineWidth = 10;
      c.beginPath(); c.moveTo(40, 128); c.lineTo(216, 128); c.moveTo(128, 40); c.lineTo(128, 216);
      c.moveTo(62, 62); c.lineTo(194, 194); c.moveTo(62, 194); c.lineTo(194, 62); c.stroke();
      c.beginPath(); c.arc(128, 128, 22, 0, 6.2832); c.fill();
    } else if (type === 'cross') {
      c.lineWidth = 26;
      c.beginPath(); c.moveTo(128, 40); c.lineTo(128, 216); c.moveTo(40, 128); c.lineTo(216, 128); c.stroke();
    } else if (type === 'diamond') {
      c.beginPath(); c.moveTo(128, 36); c.lineTo(220, 128); c.lineTo(128, 220); c.lineTo(36, 128); c.closePath(); c.fill();
    } else if (type === 'star5') {
      sealStarPath(c, 128, 134, 88, 37); c.fill();
    } else if (type === 'heart') {
      sealHeartPath(c, 128, 138, 6.2); c.fill();
    } else if (type === 'snowflake') {
      c.lineWidth = 7;
      for (var k = 0; k < 6; k++) {
        var a = k * Math.PI / 3;
        var x2 = 128 + 96 * Math.cos(a), y2 = 128 + 96 * Math.sin(a);
        var mx = 128 + 52 * Math.cos(a), my = 128 + 52 * Math.sin(a);
        c.beginPath(); c.moveTo(128, 128); c.lineTo(x2, y2); c.stroke();
        c.beginPath(); c.moveTo(mx, my); c.lineTo(128 + 82 * Math.cos(a + 0.42), 128 + 82 * Math.sin(a + 0.42)); c.stroke();
        c.beginPath(); c.moveTo(mx, my); c.lineTo(128 + 82 * Math.cos(a - 0.42), 128 + 82 * Math.sin(a - 0.42)); c.stroke();
      }
    } else if (type === 'sakura') {
      for (var k = 0; k < 5; k++) {
        var a = k * 2 * Math.PI / 5 - Math.PI / 2;
        c.beginPath(); c.arc(128 + 58 * Math.cos(a), 128 + 58 * Math.sin(a), 42, 0, 6.2832); c.fill();
      }
      c.beginPath(); c.arc(128, 128, 10, 0, 6.2832); c.fill();
    } else if (type === 'butterfly') {
      c.beginPath(); c.ellipse(88, 82, 54, 46, 0, 0, 6.2832); c.fill();
      c.beginPath(); c.ellipse(168, 82, 54, 46, 0, 0, 6.2832); c.fill();
      c.beginPath(); c.ellipse(98, 178, 38, 30, 0, 0, 6.2832); c.fill();
      c.beginPath(); c.ellipse(158, 178, 38, 30, 0, 0, 6.2832); c.fill();
    } else if (type === 'pinwheel') {
      for (var k = 0; k < 4; k++) {
        var a0 = k * Math.PI / 2;
        c.beginPath();
        c.moveTo(128, 128);
        c.lineTo(128 + 94 * Math.cos(a0), 128 + 94 * Math.sin(a0));
        c.lineTo(128 + 54 * Math.cos(a0 + Math.PI / 4), 128 + 54 * Math.sin(a0 + Math.PI / 4));
        c.closePath(); c.fill();
      }
      c.beginPath(); c.arc(128, 128, 12, 0, 6.2832); c.fill();
    } else if (type === 'triangle') {
      c.beginPath(); c.moveTo(128, 30); c.lineTo(240, 226); c.lineTo(16, 226); c.closePath(); c.fill();
    } else if (type === 'bigcircle') {
      c.beginPath(); c.arc(128, 128, 106, 0, 6.2832); c.fill();
    } else if (type === 'hexagon') {
      c.beginPath();
      for (var i = 0; i < 6; i++) {
        var a = Math.PI / 6 + i * Math.PI / 3;
        var px = 128 + 112 * Math.cos(a), py = 128 + 112 * Math.sin(a);
        if (i === 0) c.moveTo(px, py); else c.lineTo(px, py);
      }
      c.closePath(); c.fill();
    } else if (type === 'ring') {
      c.beginPath(); c.arc(128, 128, 72, 0, 6.2832); c.fill();
      c.globalCompositeOperation = 'destination-out';
      c.beginPath(); c.arc(128, 128, 48, 0, 6.2832); c.fill();
      c.globalCompositeOperation = 'source-over';
    } else if (type === 'lightning') {
      c.beginPath();
      c.moveTo(152, 34);
      c.lineTo(92, 128);
      c.lineTo(128, 128);
      c.lineTo(102, 222);
      c.lineTo(174, 106);
      c.lineTo(136, 106);
      c.closePath(); c.fill();
    } else if (type === 'gear') {
      c.beginPath();
      for (var i = 0; i < 24; i++) {
        var a = i * Math.PI / 12;
        var rad = (i % 2 === 0) ? 86 : 62;
        var px = 128 + rad * Math.cos(a), py = 128 + rad * Math.sin(a);
        if (i === 0) c.moveTo(px, py); else c.lineTo(px, py);
      }
      c.closePath(); c.fill();
      c.globalCompositeOperation = 'destination-out';
      c.beginPath(); c.arc(128, 128, 26, 0, 6.2832); c.fill();
      c.globalCompositeOperation = 'source-over';
    } else if (type === 'circuit') {
      c.lineWidth = 8;
      c.beginPath(); c.moveTo(128, 128); c.lineTo(128, 46); c.lineTo(214, 46); c.stroke();
      c.beginPath(); c.moveTo(128, 128); c.lineTo(42, 128); c.lineTo(42, 214); c.stroke();
      c.beginPath(); c.moveTo(128, 128); c.lineTo(214, 214); c.stroke();
      c.beginPath(); c.moveTo(128, 128); c.lineTo(42, 42); c.stroke();
      c.beginPath(); c.arc(214, 46, 12, 0, 6.2832); c.fill();
      c.beginPath(); c.arc(42, 214, 12, 0, 6.2832); c.fill();
      c.beginPath(); c.arc(214, 214, 12, 0, 6.2832); c.fill();
      c.beginPath(); c.arc(42, 42, 12, 0, 6.2832); c.fill();
      c.lineWidth = 5;
      c.beginPath(); c.arc(128, 46, 11, 0, 6.2832); c.stroke();
      c.beginPath(); c.arc(42, 128, 11, 0, 6.2832); c.stroke();
      c.beginPath(); c.arc(128, 128, 9, 0, 6.2832); c.stroke();
    } else if (type === 'snowflake6') {
      // 六角大雪花撑满格子
      c.lineWidth = 9;
      for (var k = 0; k < 6; k++) {
        var a = k * Math.PI / 3;
        var x2 = 128 + 100 * Math.cos(a), y2 = 128 + 100 * Math.sin(a);
        c.beginPath(); c.moveTo(128, 128); c.lineTo(x2, y2); c.stroke();
        var mx = 128 + 50 * Math.cos(a), my = 128 + 50 * Math.sin(a);
        c.beginPath(); c.moveTo(mx, my); c.lineTo(128 + 88 * Math.cos(a + 0.5), 128 + 88 * Math.sin(a + 0.5)); c.stroke();
        c.beginPath(); c.moveTo(mx, my); c.lineTo(128 + 88 * Math.cos(a - 0.5), 128 + 88 * Math.sin(a - 0.5)); c.stroke();
        c.beginPath(); c.moveTo(x2, y2); c.lineTo(128 + 106 * Math.cos(a + 0.38), 128 + 106 * Math.sin(a + 0.38)); c.stroke();
      }
    } else if (type === 'hexagram') {
      // 六芒星：两个重叠等边三角形
      for (var k = 0; k < 2; k++) {
        var a0 = -Math.PI / 2 + k * Math.PI;
        c.beginPath();
        for (var i = 0; i < 3; i++) {
          var a = a0 + i * 2 * Math.PI / 3;
          var px = 128 + 106 * Math.cos(a), py = 128 + 106 * Math.sin(a);
          if (i === 0) c.moveTo(px, py); else c.lineTo(px, py);
        }
        c.closePath(); c.fill();
      }
    }
    return cv;
  }
"""


def _seal_layer_js(effect: dict, front_strength: float = 0.945, back_strength: float = 0.5775) -> str:
    """冷裱膜动态光效层 JS：独立 ShaderMaterial，光线随悬停聚焦、随视角与时间流动。"""
    mode = effect.get("mode", "none")
    if mode == "none":
        return ""
    color = effect.get("color", (255, 255, 255))
    strength = float(effect.get("strength", 0.5))
    scale = float(effect.get("scale", 8.0))
    speed = float(effect.get("speed", 1.0))
    power = float(effect.get("power", 4.0))
    grain = float(effect.get("grain", {"sparkle": 14.0, "beam": 12.0, "rainbow": 10.0}.get(mode, 6.0)))
    rainbow = float(effect.get("rainbow", 0.6))  # 彩色镭射强度：0=单色，1=全彩（随视角/位置色相迁移）
    pat = effect.get("pattern")
    col = int(color[0]) << 16 | int(color[1]) << 8 | int(color[2])
    d0, d1 = effect.get("dir", (0.87, 0.5))

    tex_code = ""
    uniform_decl = ""
    if mode == "sparkle" and pat:
        tex_code = (
            "    var sealPatternCanvas = sealPattern(" + json.dumps(pat) + ");\n"
            "    var sealPatternTex = new THREE.CanvasTexture(sealPatternCanvas);\n"
            "    sealPatternTex.magFilter = THREE.LinearFilter;\n"
            "    sealPatternTex.minFilter = THREE.LinearMipmapLinearFilter;\n"
            "    sealPatternTex.colorSpace = THREE.SRGBColorSpace;\n"
        )
        uniform_decl = "    uPattern: { value: sealPatternTex },\n"

    # 各模式光效：共享「薄膜基面 sheen + 边缘菲涅尔 edge + 随视角扫动的光带 bandLight」，
    # 无时间动画、无指针光球（杜绝频闪），光效由拖动旋转卡片触发
    if mode == "sparkle":
        # 稀疏大图案（爱心/蝴蝶/樱花）隔行错开半格，打破严格网格的“格子感”
        brick = bool(effect.get("brick")) or pat in ("heart", "butterfly", "sakura")
        tile_code = (
            "    vec2 tile = vUv * uScale;\n"
            + ("    tile.x += 0.5 * mod(floor(tile.y), 2.0);\n" if brick else "")
            + "    float pat = texture2D(uPattern, fract(tile)).a;\n"
        )
        tw = float(effect.get("twinkle", 0.0))
        flow = float(effect.get("twinkleFlow", 0.0))
        if tw or flow:
            if tw:
                # 分段随机：转到新角度段才重新洗牌（系数越大越灵敏）
                hc_code = ("    float seg = floor(along * 5.0 + across * 3.0);\n"
                           "    vec2 hc = floor(tile) + vec2(seg * 17.31, seg * 13.17);\n")
                dens, base, amp, tilt, k = "0.93", "0.08", "0.45", "0.47", str(tw)
            else:
                # 连续漂移：转动时亮点持续随机流动（流沙/闪粉噪点感）
                hc_code = "    vec2 hc = floor(tile) + vec2(along * 1.7, across * 1.7);\n"
                dens, base, amp, tilt, k = "0.82", "0.1", "0.5", "0.4", str(flow)
            twinkle_code = (
                "    // 星星点点（独立卡封）：视角驱动随机——静止时亮点冻结、转动时重新洗牌（无时间动画）\n"
                + hc_code
                + "    float rnd = fract(sin(dot(hc, vec2(127.1, 311.7))) * 43758.5453123);\n"
                + "    float rnd2 = fract(sin(dot(hc + vec2(19.19, 7.1), vec2(127.1, 311.7))) * 12543.3453);\n"
                + "    float osc = 0.5 + 0.5 * sin(along * 8.0 + rnd2 * 6.2831);\n"
                + "    float twinkle = step(" + dens + ", rnd) * (" + base + " + " + amp + " * osc + " + tilt + " * smoothstep(0.12, 0.6, tiltAmt)) * " + k + ";\n"
                + ("    twinkle *= smoothstep(0.5, 0.95, pat);\n" if effect.get("twinkleShape") else "")
                + "    float glow = sheen + edge * 0.9 + bandLight * 0.55 + " + ("" if effect.get("twinkleOnly") else "pat * (0.10 + 1.15 * bandLight) + ") + "twinkle;\n"
            )
        else:
            twinkle_code = "    float glow = sheen + edge * 0.9 + bandLight * 0.55 + pat * (0.10 + 1.15 * bandLight);\n"
        frag = (
            tile_code
            + twinkle_code
            + "    glow = min(glow, 1.0);\n"
            + "    vec3 effColor = mix(uColor, sealHueColor(uColor, uRainbow * (along * 0.7 + vUv.x * 0.5 + vUv.y * 0.35)), min(1.0, uRainbow * 1.2));\n"
            + "    gl_FragColor = vec4(effColor * glow * uStrength, 1.0);\n"
        )
    elif mode == "grid":
        # 真密铺：格子形状即图案（六边形蜂窝/三角形/圆形蜂窝），图案撑满单元并互相重叠，
        # 无方块格感；整格闪光按单元格 ID 哈希、随视角重洗（无时间动画，学星光细闪膜）
        gt = effect.get("gridType", "hex")
        kk = str(float(effect.get("twinkle", 1.0)))
        if gt == "tri":
            grid_js = (
                "    // 三角形真密铺：行高1.0、列宽2/√3，正倒三角形底边相接、无缝铺满平面\n"
                "    vec2 gp = vec2(vUv.x * uScale * 1.1547005, vUv.y * uScale);\n"
                "    float row = floor(gp.y);\n"
                "    float yin = gp.y - row;\n"
                "    float col = floor(gp.x / 1.1547005);\n"
                "    float xin = gp.x - col * 1.1547005;\n"
                "    float td = min(sealTriDist(vec2(xin - 0.5773503, yin - 0.3333333), 0.6666667),\n"
                "                    sealTriDist(vec2(xin - 0.5773503, 0.6666667 - yin), 0.6666667));\n"
                "    float pat = 1.0 - smoothstep(0.0, 0.06, td);\n"
                "    vec2 cid = vec2(col, row);\n"
            )
        elif gt == "circle":
            grid_js = (
                "    // 圆形密铺：蜂窝点阵，同心双环（圆形套圆形），外环与邻圆重叠\n"
                "    vec2 gp = vUv * uScale;\n"
                "    vec2 hc = sealHexCell(gp);\n"
                "    vec2 cid = sealHexId(gp);\n"
                "    float d = length(hc);\n"
                "    float pat = max(1.0 - smoothstep(0.0, 0.06, abs(d - 0.56)),\n"
                "                    1.0 - smoothstep(0.0, 0.06, abs(d - 0.30)));\n"
            )
        else:
            # hex：六边形密铺，图案按 pattern（hexagon 线框 / snowflake6 雪花 / hexagram 六芒星）
            if pat == "snowflake6":
                pat_js = (
                    "    float sd = 1e5;\n"
                    "    for (int k = 0; k < 6; k++) {\n"
                    "      vec2 dir = vec2(cos(float(k) * 1.0471976), sin(float(k) * 1.0471976));\n"
                    "      sd = min(sd, sealSegDist(hc, vec2(0.0), dir * 1.12));\n"
                    "      vec2 mid = dir * 0.72;\n"
                    "      sd = min(sd, sealSegDist(hc, mid, mid + dir * 0.38 + vec2(-dir.y, dir.x) * 0.38));\n"
                    "      sd = min(sd, sealSegDist(hc, mid, mid + dir * 0.38 + vec2(dir.y, -dir.x) * 0.38));\n"
                    "    }\n"
                    "    float pat = 1.0 - smoothstep(0.0, 0.058, sd);\n"
                )
            elif pat == "hexagram":
                pat_js = (
                    "    float sd = min(sealTriDist(hc, 1.12), sealTriDist(-hc, 1.12));\n"
                    "    float pat = 1.0 - smoothstep(0.0, 0.05, sd);\n"
                )
            else:  # hexagon 六边形线框（蜂窝网，线框外扩与相邻六边形重叠）
                pat_js = (
                    "    float sd = abs(sealHexDist(hc, 1.05));\n"
                    "    float pat = 1.0 - smoothstep(0.0, 0.055, sd);\n"
                )
            grid_js = (
                "    // 六边形密铺：蜂窝点阵，图案撑满/伸出单元互相重叠\n"
                "    vec2 gp = vUv * uScale;\n"
                "    vec2 hc = sealHexCell(gp);\n"
                "    vec2 cid = sealHexId(gp);\n"
                + pat_js
            )
        frag = (
            grid_js
            + "    // 整格闪光（A类）：按单元格 ID 哈希、随视角重洗（无时间动画）\n"
            + "    float seg = floor(along * 5.0 + across * 3.0);\n"
            + "    vec2 hc2 = cid + vec2(seg * 17.31, seg * 13.17);\n"
            + "    float rnd = fract(sin(dot(hc2, vec2(127.1, 311.7))) * 43758.5453123);\n"
            + "    float rnd2 = fract(sin(dot(hc2 + vec2(19.19, 7.1), vec2(127.1, 311.7))) * 12543.3453);\n"
            + "    float osc = 0.5 + 0.5 * sin(along * 8.0 + rnd2 * 6.2831);\n"
            + "    float twinkle = step(0.90, rnd) * (0.08 + 0.5 * osc + 0.5 * smoothstep(0.12, 0.6, tiltAmt)) * " + kk + ";\n"
            + "    float glow = sheen + edge * 0.9 + bandLight * 0.55 + pat * (0.12 + 1.15 * bandLight) + twinkle;\n"
            + "    glow = min(glow, 1.0);\n"
            + "    vec3 effColor = mix(uColor, sealHueColor(uColor, uRainbow * (along * 0.7 + vUv.x * 0.5 + vUv.y * 0.35)), min(1.0, uRainbow * 1.2));\n"
            + "    gl_FragColor = vec4(effColor * glow * uStrength, 1.0);\n"
        )
    elif mode == "voronoi":
        frag = (
            "    // 碎玻璃/碎宝石：Voronoi 泰森多边形——每块独立色相与亮度，随视角明暗变化\n"
            "    vec2 p = vUv * uScale;\n"
            "    vec2 n = floor(p);\n"
            "    vec2 f = fract(p);\n"
            "    float md = 8.0, md2 = 8.0;\n"
            "    vec2 mps = vec2(0.0);\n"
            "    for (int i = -1; i <= 1; i++) {\n"
            "      for (int j = -1; j <= 1; j++) {\n"
            "        vec2 g = vec2(float(i), float(j));\n"
            "        vec2 o = sealHash2(n + g);\n"
            "        vec2 r = g + o - f;\n"
            "        float d = dot(r, r);\n"
            "        if (d < md) { md2 = md; md = d; mps = g + o; }\n"
            "        else if (d < md2) { md2 = d; }\n"
            "      }\n"
            "    }\n"
            "    float edgeD = sqrt(md2) - sqrt(md);\n"
            "    vec2 seed = n + mps;\n"
            "    float h = fract(sin(dot(seed, vec2(127.1, 311.7))) * 43758.5453123);\n"
            "    float h2 = fract(sin(dot(seed + vec2(19.19, 7.1), vec2(127.1, 311.7))) * 12543.3453);\n"
            "    float fill = 0.3 + 0.7 * smoothstep(0.12, 0.6, tiltAmt) * (0.5 + 0.5 * sin(along * 6.0 + h2 * 6.2831));\n"
            "    float seam = 1.0 - smoothstep(0.0, 2.2 / uScale, edgeD);\n"
            "    float glow = (sheen + edge * 0.9 + bandLight * 0.55 + fill * 0.85) * (1.0 - 0.9 * seam);\n"
            "    glow = min(glow, 1.0);\n"
            "    vec3 effColor = mix(uColor, sealHueColor(uColor, uRainbow * (along * 0.7 + h)), min(1.0, uRainbow * 1.2));\n"
            "    gl_FragColor = vec4(effColor * glow * uStrength, 1.0);\n"
        )
    elif mode == "beam":
        frag = (
            "    vec2 perp = vec2(-brush.y, brush.x);\n"
            "    float lobe = pow(max(0.0, abs(dot(refl2, perp))), uPower);\n"
            "    float stripe = pow(max(0.0, 0.5 + 0.5 * sin(dot(vUv, perp) * uScale * 4.0 + along * 4.0)), 3.5);\n"
            "    float glow = sheen + edge * 0.9 + bandLight * (0.45 + 0.55 * stripe) + lobe * bandLight * 0.4;\n"
            "    glow = min(glow, 1.0);\n"
            "    vec3 effColor = mix(uColor, sealHueColor(uColor, uRainbow * (along * 0.8 + dot(vUv, perp) * 0.6)), uRainbow);\n"
            "    gl_FragColor = vec4(effColor * glow * uStrength, 1.0);\n"
        )
    elif mode == "rainbow":
        frag = (
            "    float sweep = vUv.x * 0.55 + vUv.y * 0.38 + along * 0.75;\n"
            "    float sweepBand = pow(max(0.0, sin(sweep * 6.28318)), 9.0);\n"
            "    float hue = fract(sweep * 1.7 + fres * 0.3);\n"
            "    vec3 col = sealHsv2rgb(vec3(hue, 0.85, 1.0));\n"
            "    float glow = sheen * 1.2 + edge * 1.1 + (0.2 + 1.15 * bandLight) * (0.42 + 0.58 * sweepBand);\n"
            "    glow = min(glow, 1.0);\n"
            "    gl_FragColor = vec4(col * glow * uStrength, 1.0);\n"
        )
    else:  # glow
        frag = (
            "    float n = 0.0;\n"
            "    n += sin(vUv.x * 90.0 * uGrain + along * 2.0) * 0.5;\n"
            "    n += sin(vUv.y * 110.0 * uGrain + across * 2.0) * 0.5;\n"
            "    n += sin((vUv.x + vUv.y) * 150.0 * uGrain) * 0.5;\n"
            "    n = n / 3.0 + 0.5;\n"
            "    float glow = sheen + edge * 0.9 + bandLight * (0.4 + 0.35 * n);\n"
            "    glow = min(glow, 1.0);\n"
            "    vec3 effColor = mix(uColor, sealHueColor(uColor, uRainbow * (along * 0.6 + vUv.x * 0.5 + vUv.y * 0.4)), min(1.0, uRainbow * 1.2));\n"
            "    gl_FragColor = vec4(effColor * glow * uStrength, 1.0);\n"
        )

    vertex = (
        "varying vec2 vUv;\n"
        "varying vec3 vWorldNormal;\n"
        "varying vec3 vWorldPosition;\n"
        "varying vec3 vWorldXAxis;\n"
        "varying vec3 vWorldYAxis;\n"
        "void main() {\n"
        "  vUv = uv;\n"
        "  vWorldNormal = normalize(mat3(modelMatrix) * normal);\n"
        "  vWorldXAxis = normalize((modelMatrix * vec4(1.0, 0.0, 0.0, 0.0)).xyz);\n"
        "  vWorldYAxis = normalize((modelMatrix * vec4(0.0, 1.0, 0.0, 0.0)).xyz);\n"
        "  vec4 wp = modelMatrix * vec4(position, 1.0);\n"
        "  vWorldPosition = wp.xyz;\n"
        "  gl_Position = projectionMatrix * viewMatrix * wp;\n"
        "}\n"
    )
    prefix = (
        "uniform float uAspect;\n"
        "uniform vec3 uColor;\n"
        "uniform float uStrength;\n"
        "uniform float uScale;\n"
        "uniform float uPower;\n"
        "uniform float uGrain;\n"
        "uniform vec2 uDir;\n"
        "uniform float uRainbow;\n"
        "uniform float uFrontStrength;\n"
        "uniform float uBackStrength;\n"
        + ("uniform sampler2D uPattern;\n" if mode == "sparkle" else "")
        + ("vec2 sealHash2(vec2 p) {\n  return fract(sin(vec2(dot(p, vec2(127.1, 311.7)), dot(p, vec2(269.5, 183.3)))) * 43758.5453123);\n}\n" if mode == "voronoi" else "")
        + (("vec2 sealHexCell(vec2 p) {\n"
            "  vec2 r = vec2(1.0, 1.7320508);\n"
            "  vec2 h = r * 0.5;\n"
            "  vec2 a = mod(p, r) - h;\n"
            "  vec2 b = mod(p - h, r) - h;\n"
            "  return dot(a, a) < dot(b, b) ? a : b;\n"
            "}\n"
            "vec2 sealHexId(vec2 p) {\n"
            "  vec2 r = vec2(1.0, 1.7320508);\n"
            "  vec2 h = r * 0.5;\n"
            "  vec2 a = mod(p, r) - h;\n"
            "  vec2 b = mod(p - h, r) - h;\n"
            "  return dot(a, a) < dot(b, b) ? floor(p / r) : floor((p - h) / r);\n"
            "}\n"
            "float sealSegDist(vec2 p, vec2 a, vec2 b) {\n"
            "  vec2 pa = p - a, ba = b - a;\n"
            "  float t = clamp(dot(pa, ba) / dot(ba, ba), 0.0, 1.0);\n"
            "  return length(pa - ba * t);\n"
            "}\n"
            "float sealTriDist(vec2 p, float R) {\n"
            "  vec2 v0 = vec2(0.0, R);\n"
            "  vec2 v1 = vec2(-0.8660254 * R, -0.5 * R);\n"
            "  vec2 v2 = vec2(0.8660254 * R, -0.5 * R);\n"
            "  return min(sealSegDist(p, v0, v1), min(sealSegDist(p, v1, v2), sealSegDist(p, v2, v0)));\n"
            "}\n"
            "float sealHexDist(vec2 p, float R) {\n"
            "  const vec3 k = vec3(-0.8660254, 0.5, 0.57735027);\n"
            "  p = abs(p);\n"
            "  p -= 2.0 * min(dot(k.xy, p), 0.0) * k.xy;\n"
            "  p -= vec2(clamp(p.x, -k.z * R, k.z * R), R);\n"
            "  return length(p) * sign(p.y);\n"
            "}\n") if mode == "grid" else "")
        + "varying vec2 vUv;\n"
        "varying vec3 vWorldNormal;\n"
        "varying vec3 vWorldPosition;\n"
        "varying vec3 vWorldXAxis;\n"
        "varying vec3 vWorldYAxis;\n"
        "vec3 sealHsv2rgb(vec3 c) {\n"
        "  vec3 p = abs(fract(c.xxx + vec3(0.0, 2.0 / 3.0, 1.0 / 3.0)) * 6.0 - 3.0);\n"
        "  return c.z * mix(vec3(1.0), clamp(p - 1.0, 0.0, 1.0), c.y);\n"
        "}\n"
        "vec3 sealHueColor(vec3 base, float offset) {\n"
        "  float maxc = max(base.r, max(base.g, base.b));\n"
        "  float minc = min(base.r, min(base.g, base.b));\n"
        "  float delta = maxc - minc;\n"
        "  float h = 0.0;\n"
        "  if (delta > 0.0) {\n"
        "    if (maxc == base.r) h = mod((base.g - base.b) / delta, 6.0);\n"
        "    else if (maxc == base.g) h = (base.b - base.r) / delta + 2.0;\n"
        "    else h = (base.r - base.g) / delta + 4.0;\n"
        "    h /= 6.0;\n"
        "  }\n"
        "  float s = maxc > 0.0 ? delta / maxc : 0.0;\n"
        "  s = max(s, 0.72);\n"
        "  float v = maxc;\n"
        "  return sealHsv2rgb(vec3(fract(h + offset), s, v));\n"
        "}\n"
        "float sealRounded(vec2 uv, float radius) {\n"
        "  vec2 scaled = vec2((uv.x - 0.5) * uAspect, uv.y - 0.5);\n"
        "  vec2 halfSize = vec2(0.5 * uAspect, 0.5);\n"
        "  vec2 edge = abs(scaled) - (halfSize - vec2(radius));\n"
        "  return length(max(edge, 0.0)) + min(max(edge.x, edge.y), 0.0) - radius;\n"
        "}\n"
        "void main() {\n"
        "  if (sealRounded(vUv, 0.032) > 0.0) discard;\n"
        "  vec3 viewDir = normalize(cameraPosition - vWorldPosition);\n"
        "  vec3 nrm = normalize(vWorldNormal);\n"
        "  float facing = clamp(dot(nrm, viewDir), 0.0, 1.0);\n"
        "  float fres = pow(1.0 - facing, 2.0);\n"
        "  vec3 reflW = reflect(-viewDir, nrm);\n"
        "  vec3 planeC = reflW - nrm * dot(reflW, nrm);\n"
        "  vec2 refl2 = normalize(vec2(dot(planeC, vWorldXAxis), dot(planeC, vWorldYAxis)) + 1e-5);\n"
        "  vec2 brush = normalize(uDir);\n"
        "  float along = dot(refl2, brush);\n"
        "  float across = dot(refl2, vec2(-brush.y, brush.x));\n"
        "  float tiltAmt = 1.0 - facing;\n"
        "  float bandLight = pow(max(0.0, along), uPower) * smoothstep(0.12, 0.6, tiltAmt);\n"
        "  float sheen = 0.03 + 0.045 * smoothstep(0.1, 0.7, tiltAmt);\n"
        "  float edge = fres * 0.18;\n"
        + frag
        + "  // 卡背一面减弱卡封效果（翻面后 gl_FrontFacing 为 false），强度由界面可调\n"
        + "  gl_FragColor *= gl_FrontFacing ? uFrontStrength : uBackStrength;\n"
        + "  }\n"
    )

    return (
        _SEAL_PATTERN_JS
        + "  (function () {\n"
        + tex_code
        + "    var sealMat = new THREE.ShaderMaterial({\n"
        "      transparent: true,\n"
        "      depthWrite: false,\n"
        "      side: THREE.DoubleSide,\n"
        "      blending: THREE.AdditiveBlending,\n"
        "      toneMapped: false,\n"
        "      uniforms: {\n"
      "        uAspect: { value: CARD_WIDTH / CARD_HEIGHT },\n"
      "        uColor: { value: new THREE.Color(" + str(col) + ") },\n"
      "        uStrength: { value: " + str(strength) + " },\n"
      "        uScale: { value: " + str(scale) + " },\n"
      "        uPower: { value: " + str(power) + " },\n"
      "        uGrain: { value: " + str(grain) + " },\n"
      "        uDir: { value: new THREE.Vector2(" + str(d0) + ", " + str(d1) + ") },\n"
      "        uRainbow: { value: " + str(rainbow) + " },\n"
      "        uFrontStrength: { value: " + str(front_strength) + " },\n"
      "        uBackStrength: { value: " + str(back_strength) + " },\n"
        + uniform_decl
        + "      },\n"
        "      vertexShader: " + json.dumps(vertex) + ",\n"
        "      fragmentShader: " + json.dumps(prefix) + ",\n"
        "    });\n"
        "    var sealMesh = new THREE.Mesh(new THREE.PlaneGeometry(CARD_WIDTH, CARD_HEIGHT), sealMat);\n"
        "    sealMesh.position.set(0, 0, 0.02);\n"
        "    sealMesh.renderOrder = 5.2;\n"
        "    flipGroup.add(sealMesh);\n"
        "    window.__sealMat = sealMat;\n"
        "  })();\n"
    )


def _custom_seal_layer_js(rel: str | None, front_strength: float = 0.945, back_strength: float = 0.5775) -> str:
    """自定义 PNG 卡封动态光效层：整张贴图 + 随视角扫动的箔光带 + 彩虹色相迁移，正反双面显示。

    与内置膜（_seal_layer_js）相同的视角光效架构（sheen 基面 / edge 边缘菲涅尔 /
    bandLight 视角光带 / sealHueColor 彩虹色相迁移），区别：
      - 图案直接整张采样用户 PNG（不平铺、不切割），PNG 的 alpha 作图案遮罩；
      - 图案之外完全透明（不留整卡光迹）；
      - 双面渲染，正面/背面强度由 uFrontStrength/uBackStrength 控制（与内置膜一致）。
    """
    if not rel:
        return ""
    strength, rainbow, power = 0.9, 0.7, 4.0
    d0, d1 = 0.87, 0.5
    frag = (
        "    vec4 tex = texture2D(uMap, vUv);\n"
        "    float pat = tex.a;\n"
        "    vec3 base = max(tex.rgb, vec3(0.03));\n"
        "    float glow = min(sheen * 0.6 + edge * 0.9 + bandLight * (0.4 + 0.6 * pat), 1.0);\n"
        "    vec3 effColor = mix(base, sealHueColor(base, uRainbow * (along * 0.7 + vUv.x * 0.5 + vUv.y * 0.35)), min(1.0, uRainbow * 1.2));\n"
        "    vec3 rgb = effColor * (0.55 + 0.45 * glow) * uStrength;\n"
        "    float a = pat * (gl_FrontFacing ? uFrontStrength : uBackStrength);\n"
        "    gl_FragColor = vec4(rgb, a);\n"
    )
    vertex = (
        "varying vec2 vUv;\n"
        "varying vec3 vWorldNormal;\n"
        "varying vec3 vWorldPosition;\n"
        "varying vec3 vWorldXAxis;\n"
        "varying vec3 vWorldYAxis;\n"
        "void main() {\n"
        "  vUv = uv;\n"
        "  vWorldNormal = normalize(mat3(modelMatrix) * normal);\n"
        "  vWorldXAxis = normalize((modelMatrix * vec4(1.0, 0.0, 0.0, 0.0)).xyz);\n"
        "  vWorldYAxis = normalize((modelMatrix * vec4(0.0, 1.0, 0.0, 0.0)).xyz);\n"
        "  vec4 wp = modelMatrix * vec4(position, 1.0);\n"
        "  vWorldPosition = wp.xyz;\n"
        "  gl_Position = projectionMatrix * viewMatrix * wp;\n"
        "}\n"
    )
    prefix = (
        "uniform float uAspect;\n"
        "uniform float uStrength;\n"
        "uniform float uPower;\n"
        "uniform vec2 uDir;\n"
        "uniform float uRainbow;\n"
        "uniform float uFrontStrength;\n"
        "uniform float uBackStrength;\n"
        "uniform sampler2D uMap;\n"
        "varying vec2 vUv;\n"
        "varying vec3 vWorldNormal;\n"
        "varying vec3 vWorldPosition;\n"
        "varying vec3 vWorldXAxis;\n"
        "varying vec3 vWorldYAxis;\n"
        "vec3 sealHsv2rgb(vec3 c) {\n"
        "  vec3 p = abs(fract(c.xxx + vec3(0.0, 2.0 / 3.0, 1.0 / 3.0)) * 6.0 - 3.0);\n"
        "  return c.z * mix(vec3(1.0), clamp(p - 1.0, 0.0, 1.0), c.y);\n"
        "}\n"
        "vec3 sealHueColor(vec3 base, float offset) {\n"
        "  float maxc = max(base.r, max(base.g, base.b));\n"
        "  float minc = min(base.r, min(base.g, base.b));\n"
        "  float delta = maxc - minc;\n"
        "  float h = 0.0;\n"
        "  if (delta > 0.0) {\n"
        "    if (maxc == base.r) h = mod((base.g - base.b) / delta, 6.0);\n"
        "    else if (maxc == base.g) h = (base.b - base.r) / delta + 2.0;\n"
        "    else h = (base.r - base.g) / delta + 4.0;\n"
        "    h /= 6.0;\n"
        "  }\n"
        "  float s = maxc > 0.0 ? delta / maxc : 0.0;\n"
        "  s = max(s, 0.72);\n"
        "  float v = maxc;\n"
        "  return sealHsv2rgb(vec3(fract(h + offset), s, v));\n"
        "}\n"
        "float sealRounded(vec2 uv, float radius) {\n"
        "  vec2 scaled = vec2((uv.x - 0.5) * uAspect, uv.y - 0.5);\n"
        "  vec2 halfSize = vec2(0.5 * uAspect, 0.5);\n"
        "  vec2 edge = abs(scaled) - (halfSize - vec2(radius));\n"
        "  return length(max(edge, 0.0)) + min(max(edge.x, edge.y), 0.0) - radius;\n"
        "}\n"
        "void main() {\n"
        "  if (sealRounded(vUv, 0.032) > 0.0) discard;\n"
        "  vec3 viewDir = normalize(cameraPosition - vWorldPosition);\n"
        "  vec3 nrm = normalize(vWorldNormal);\n"
        "  float facing = clamp(dot(nrm, viewDir), 0.0, 1.0);\n"
        "  float fres = pow(1.0 - facing, 2.0);\n"
        "  vec3 reflW = reflect(-viewDir, nrm);\n"
        "  vec3 planeC = reflW - nrm * dot(reflW, nrm);\n"
        "  vec2 refl2 = normalize(vec2(dot(planeC, vWorldXAxis), dot(planeC, vWorldYAxis)) + 1e-5);\n"
        "  vec2 brush = normalize(uDir);\n"
        "  float along = dot(refl2, brush);\n"
        "  float across = dot(refl2, vec2(-brush.y, brush.x));\n"
        "  float tiltAmt = 1.0 - facing;\n"
        "  float bandLight = pow(max(0.0, along), uPower) * smoothstep(0.12, 0.6, tiltAmt);\n"
        "  float sheen = 0.03 + 0.045 * smoothstep(0.1, 0.7, tiltAmt);\n"
        "  float edge = fres * 0.18;\n"
        + frag
        + "  }\n"
    )
    return (
        "  (function () {\n"
        "    var t = loader.load(" + json.dumps(rel) + ");\n"
        "    t.colorSpace = THREE.SRGBColorSpace;\n"
        "    t.anisotropy = 8;\n"
        "    var sealMat = new THREE.ShaderMaterial({\n"
        "      transparent: true,\n"
        "      depthWrite: false,\n"
        "      side: THREE.DoubleSide,\n"
        "      toneMapped: false,\n"
        "      uniforms: {\n"
        "        uAspect: { value: CARD_WIDTH / CARD_HEIGHT },\n"
        "        uStrength: { value: " + str(strength) + " },\n"
        "        uPower: { value: " + str(power) + " },\n"
        "        uDir: { value: new THREE.Vector2(" + str(d0) + ", " + str(d1) + ") },\n"
        "        uRainbow: { value: " + str(rainbow) + " },\n"
        "        uFrontStrength: { value: " + str(front_strength) + " },\n"
        "        uBackStrength: { value: " + str(back_strength) + " },\n"
        "        uMap: { value: t },\n"
        "      },\n"
        "      vertexShader: " + json.dumps(vertex) + ",\n"
        "      fragmentShader: " + json.dumps(prefix) + ",\n"
        "    });\n"
        "    var sealMesh = new THREE.Mesh(new THREE.PlaneGeometry(CARD_WIDTH, CARD_HEIGHT), sealMat);\n"
        "    sealMesh.position.set(0, 0, 0.02);\n"
        "    sealMesh.renderOrder = 5.2;\n"
        "    flipGroup.add(sealMesh);\n"
        "    window.__sealMat = sealMat;\n"
        "  })();\n"
    )


_SEAL_TICK_JS = (
    "  // 膜光效完全由视角驱动（拖动旋转触发）：无时间动画、无指针光球，杜绝频闪\n"
)


def _text_layer_js(description: str | None, text_type: str, text_pos: dict | None) -> str:
    """卡面描述文字层 JS 片段（无文本型或空描述时返回空串）。

    文本绘制到 900x1200 透明画布，默认贴在卡牌下方区域（左右各留 8% 边框宽度），
    自动换行并缩放字号适配，生成 CanvasTexture 叠加在卡封之上。
    text_pos 支持两种格式：
      {x1,x2,y1,y2} 自定义文本区域（0~1 比例，覆盖默认区域）
      {x,y,w}       旧版中心点 + 宽度（点击定位），高度固定 0.2
    """
    if not description or text_type == "none":
        return ""
    area = {"x": 0.5, "y": 0.82, "w": 0.84, "h": 0.2}
    if text_pos:
        if all(k in text_pos for k in ("x1", "x2", "y1", "y2")):
            area = {
                "x": (text_pos["x1"] + text_pos["x2"]) / 2,
                "y": (text_pos["y1"] + text_pos["y2"]) / 2,
                "w": text_pos["x2"] - text_pos["x1"],
                "h": text_pos["y2"] - text_pos["y1"],
            }
        else:
            area.update({k: text_pos[k] for k in ("x", "y", "w") if k in text_pos})
    return (
        "  (function () {\n"
        "    var desc = " + _json(description) + ";\n"
        "    var area = " + _json(area) + ";\n"
        "    var CW = 900, CH = 1200;\n"
        "    var canvas = document.createElement('canvas');\n"
        "    canvas.width = CW; canvas.height = CH;\n"
        "    var ctx = canvas.getContext('2d');\n"
        "    var aw = CW * (area.w || 0.84), ah = CH * (area.h || 0.2);\n"
        "    var ax = CW * (area.x || 0.5) - aw / 2;\n"
        "    var ay = CH * (area.y || 0.82) - ah / 2;\n"
        "    var fontName = '\"Microsoft YaHei\",\"PingFang SC\",\"Noto Sans CJK SC\",sans-serif';\n"
        "    function wrapLine(text, maxW, fs) {\n"
        "      ctx.font = fs + 'px ' + fontName;\n"
        "      var out = [], cur = '';\n"
        "      for (var i = 0; i < text.length; i++) {\n"
        "        var ch = text[i];\n"
        "        if (ch === '\\n') { out.push(cur); cur = ''; continue; }\n"
        "        if (cur && ctx.measureText(cur + ch).width > maxW) { out.push(cur); cur = ch; }\n"
        "        else { cur += ch; }\n"
        "      }\n"
        "      if (cur) out.push(cur);\n"
        "      return out;\n"
        "    }\n"
        "    var fs = 44;\n"
        "    var wrapped = [];\n"
        "    var srcLines = desc.split('\\n');\n"
        "    for (; fs > 13; fs--) {\n"
        "      wrapped = [];\n"
        "      for (var k = 0; k < srcLines.length; k++) {\n"
        "        wrapped = wrapped.concat(wrapLine(srcLines[k], aw - 24, fs));\n"
        "      }\n"
        "      if (wrapped.length * fs * 1.35 <= ah) break;\n"
        "    }\n"
        "    var lineH = fs * 1.35;\n"
        "    var totalH = wrapped.length * lineH;\n"
        "    var ty = ay + (ah - totalH) / 2 + fs * 0.6;\n"
        "    ctx.textAlign = 'center';\n"
        "    ctx.textBaseline = 'middle';\n"
        "    for (var j = 0; j < wrapped.length; j++) {\n"
        "      ctx.font = fs + 'px ' + fontName;\n"
        "      ctx.lineJoin = 'round';\n"
        "      ctx.lineWidth = Math.max(3, fs * 0.16);\n"
        "      ctx.strokeStyle = 'rgba(22,11,4,0.9)';\n"
        "      ctx.strokeText(wrapped[j], ax + aw / 2, ty);\n"
        "      var grad = ctx.createLinearGradient(0, ty - fs / 2, 0, ty + fs / 2);\n"
        "      grad.addColorStop(0, '#fff6e2'); grad.addColorStop(1, '#f2d9a5');\n"
        "      ctx.fillStyle = grad;\n"
        "      ctx.fillText(wrapped[j], ax + aw / 2, ty);\n"
        "      ty += lineH;\n"
        "    }\n"
        "    var tex = new THREE.CanvasTexture(canvas);\n"
        "    tex.colorSpace = THREE.SRGBColorSpace;\n"
        "    var m = new THREE.Mesh(\n"
    "      new THREE.PlaneGeometry(CARD_WIDTH, CARD_HEIGHT),\n"
    "      new THREE.MeshBasicMaterial({ map: tex, transparent: true, depthWrite: false, side: THREE.FrontSide, toneMapped: false })\n"
    "    );\n"
    "    m.position.set(0, 0, 0.026);\n"
    "    m.renderOrder = 6;\n"
    "    flipGroup.add(m);\n"
    "  })();\n"
)


# 整幅卡面：前景主体圆角与卡面一致（radius 0.032 与主卡 shader 一致，900x1200 下为 38.4px）
FG_ROUND_JS = r"""
  (function () {
    var orig = foregroundTexture;
    var cv = document.createElement('canvas');
    cv.width = 900;
    cv.height = 1200;
    var ctx = cv.getContext('2d');
    var t = new THREE.CanvasTexture(cv);
    t.colorSpace = THREE.SRGBColorSpace;
    t.anisotropy = 8;
    foregroundTexture = t;
    function round() {
      var img = orig.image;
      if (!img || !img.width || !img.height) { setTimeout(round, 30); return; }
      ctx.drawImage(img, 0, 0, cv.width, cv.height);
      var r = 38.4;
      ctx.globalCompositeOperation = 'destination-in';
      ctx.beginPath();
      ctx.moveTo(r, 0);
      ctx.lineTo(cv.width - r, 0);
      ctx.arcTo(cv.width, 0, cv.width, r, r);
      ctx.lineTo(cv.width, cv.height - r);
      ctx.arcTo(cv.width, cv.height, cv.width - r, cv.height, r);
      ctx.lineTo(r, cv.height);
      ctx.arcTo(0, cv.height, 0, cv.height - r, r);
      ctx.lineTo(0, r);
      ctx.arcTo(0, 0, r, 0, r);
      ctx.closePath();
      ctx.fill();
      t.needsUpdate = true;
    }
    round();
  })();
"""


def build_card_html(name: str, front_rel: str, foreground_rel: str, back_rel: str, effects=None,
                    frame_rel: str | None = None, seal_rel: str | None = None,
                    seal_name: str | None = None,
                    description: str | None = None, text_type: str = "none",
                    text_pos: dict | None = None,
                    subject_over_frame: bool = False,
                    round_foreground: bool = False,
                    frame_fit_subject: bool = False,
                    outline_rel: str | None = None,
                    seal_strength_front: float = 0.945,
                    seal_strength_back: float = 0.5775) -> str:
    """生成自包含 3D 卡网页 HTML。

    effects: 特效实例列表 [{"name":..., "def": {...}, "pos": {...}}]，def 为完整特效参数。
    frame_rel/seal_rel: 可选的边框/卡封图片相对路径（叠加在卡面之上）。
    seal_name: 卡封素材原始文件名（如「拉丝闪光膜.png」）；命中内置冷裱膜时改用动态光效层。
    description/text_type/text_pos: 卡面描述文字（text_type: none/transparent/boxed）。
    subject_over_frame: 透明主体浮于边框之上（PVZ 式立体感）；默认边框盖住主体。
    round_foreground: 整幅卡面时对前景主体做圆角遮罩（与卡面圆角一致）。
    frame_fit_subject: 未开启浮于边框时，边框按前景主体包围盒缩放（否则整卡）。
    outline_rel: 主体白色描边图（贴纸边，叠加在主体层之下、随前景视差移动）。
    seal_strength_front/back: 卡封效果强度（正面/背面，默认 0.945/0.5775）。
    """
    three_js = THREE_JS.read_text(encoding="utf-8")
    fg_js = (
        # 浮于边框之上：前景组整体提升到边框层之上、卡封之下
        CARD_HTML_TEMPLATE_FG_FLOAT
        if subject_over_frame else
        CARD_HTML_TEMPLATE_FG
    )
    seal_layer = ""
    if seal_name:
        key = Path(str(seal_name)).stem
        effect = SEAL_EFFECTS.get(key)
        if effect:
            seal_layer = _seal_layer_js(effect, seal_strength_front, seal_strength_back)
    if not seal_layer:
        seal_layer = _custom_seal_layer_js(seal_rel, seal_strength_front, seal_strength_back)
    if seal_layer:
        # 使用卡封时默认箔光弱化一半（无卡封保持 uSeal=1.0 全量）
        seal_layer += "  (function () { cardMaterial.uniforms.uSeal.value = 0.5; })();\n"
    # 边框层：未开启浮于边框且 frame_fit_subject 时按主体包围盒缩放，否则整卡；均仅正面渲染
    frame_layer = _frame_layer_js(frame_rel, 0.012, 4, frame_fit_subject and not subject_over_frame)
    # 主体描边层：渲染在主体之上（盖住主体边缘锯齿），浮起时随前景组抬升到边框之上
    outline_layer = _outline_layer_js(
        outline_rel, 0.0145 if subject_over_frame else 0.0065,
        4.45 if subject_over_frame else 3.1,
    )
    return (
        CARD_HTML_TEMPLATE
        .replace("__THREE_JS__", three_js)
        .replace("__CARD_NAME__", name)
        .replace("__FRONT__", front_rel)
        .replace("__FOREGROUND__", foreground_rel)
        .replace("__BACK__", back_rel)
        .replace("__FG_BLOCK__", fg_js)
        .replace("__FG_ROUND_JS__", FG_ROUND_JS if round_foreground else "")
        .replace("__OUTLINE_LAYER__", outline_layer)
        .replace("__FRAME_LAYER__", frame_layer)
        .replace("__SEAL_LAYER__", seal_layer)
        .replace("__SEAL_TICK__", _SEAL_TICK_JS)
        .replace("__TEXT_LAYER__", _text_layer_js(description, text_type, text_pos))
        .replace("__ENGINE_JS__", EFFECT_ENGINE_JS)
        .replace("__EFFECTS__", _json(effects or []))
    )


def build_effect_preview(effects) -> str:
    """生成特效预览页（浅色卡面舞台 + 指定特效循环播放）。"""
    three_js = THREE_JS.read_text(encoding="utf-8")
    return (
        PREVIEW_HTML_TEMPLATE
        .replace("__THREE_JS__", three_js)
        .replace("__ENGINE_JS__", EFFECT_ENGINE_JS)
        .replace("__EFFECTS__", _json(effects or []))
    )
