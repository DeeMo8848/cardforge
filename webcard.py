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
      s.renderOrder = 7;
      anchor.add(s);
      return s;
    }

    "// 预设动作：区别于偏移量（某点到某点），动作是\"从某点向某方向持续运动\"\n"
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
      p.sprite.position.set(p.x, p.y, 0.03);
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
      // 前景纹理异步加载完成后再补建主体特效层（含 FG_ROUND_JS 重绘圆角后的画布）
      if (foilSubjDef && foregroundTexture && foregroundTexture.image && foregroundTexture.image.width) {
        var subDef = foilSubjDef;
        foilSubjDef = null;
        foilBuildSubject(subDef);
      }
      if (foilObj) foilUpdate(foilObj, dt);
      if (foilSubj) {
        // 主体层跟随 fgMesh 的几何/位置（文本型 contain 适配会改几何），
        // 并把网格 uv 换算成卡片 uv（含前景组视差位移），特效图案才与整卡层对齐
        var fsMesh = foilSubj.mesh;
        if (fsMesh.geometry !== fgMesh.geometry) fsMesh.geometry = fgMesh.geometry;
        fsMesh.position.x = fgMesh.position.x;
        fsMesh.position.y = fgMesh.position.y;
        var gp = fgMesh.geometry.parameters || {};
        var cs = cardContent.scale.x;
        var mw = (gp.width || CARD_WIDTH) * cs, mh = (gp.height || CARD_HEIGHT) * cs;
        foilSubj.uniforms.uMeshRect.value.set(
          ((foregroundGroup.position.x + fgMesh.position.x - mw / 2) / CARD_WIDTH) + 0.5,
          ((foregroundGroup.position.y + fgMesh.position.y - mh / 2) / CARD_HEIGHT) + 0.5,
          mw / CARD_WIDTH, mh / CARD_HEIGHT);
        foilUpdate(foilSubj, dt);
      }
    }

    function init(list) {
      emitters = [];
      (list || []).forEach(function (item) {
        var def = item.def || {};
        if (def.engine === 'foil') return;   // 闪卡特效不参与粒子发射，由 foilApply 单独接管卡面
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
      foilApply(list);   // 闪卡特效：整面接管卡面渲染（无 cardMesh 时自动跳过）
    }

    // ===== 影之诗闪卡特效（engine:'foil'）：整面接管卡面渲染 =====
    // 由特效DIY2 保存，落到卡牌上时用 5 槽着色器重绘整张卡面：
    // 底层画面 = 卡面素材，叠加官方语义的 加法/减法/透明 槽位 + 扭曲 + 虹光。
    var foilObj = null;
    var foilSubj = null;       // 前景主体层同步特效（挂在 foregroundGroup，随主体视差移动）
    var foilSubjDef = null;    // 前景纹理尚未加载完成时暂存待建的特效定义
    var foilWhiteTex = null;
    var FOIL_LUT = {
      fire:    ['0 0.35 0.85 1 1', '0 0.02 0.25 0.65 0.95', '0 0 0.02 0.1 0.7'],
      ice:     ['0 0.1 0.3 0.6 1', '0 0.4 0.7 0.9 1', '0 0.7 0.9 1 1'],
      gold:    ['0 0.3 0.7 1 1', '0 0.15 0.45 0.85 1', '0 0 0.05 0.3 0.85'],
      magic:   ['0 0.2 0.6 0.9 1', '0 0 0.1 0.5 0.95', '0 0.3 0.8 1 1'],
      official:['0.031 0.078 0.165 0.251 0.325', '0.027 0.082 0.184 0.329 0.490', '0.165 0.235 0.369 0.557 0.769']
    };
    var FOIL_SHEEN = {
      rainbow: [[1, 0.31, 0.31], [1, 0.90, 0.35], [0.35, 1, 0.55], [0.31, 0.71, 1]],
      gold:    [[1, 0.75, 0.35], [1, 0.93, 0.75], [1, 0.59, 0.24], [1, 0.86, 0.59]],
      ice:     [[0.47, 0.78, 1], [0.82, 0.96, 1], [0.35, 0.63, 1], [0.88, 0.98, 1]]
    };
    var FOIL_ORDER = { Back1: 0, Back2: 1, Middle: 2, Front1: 3, Front2: 4 };
    var PI_JS = Math.PI;

    function foilWhite() {
      if (foilWhiteTex) return foilWhiteTex;
      var c = document.createElement('canvas');
      c.width = c.height = 1;
      var x = c.getContext('2d');
      x.fillStyle = '#ffffff';
      x.fillRect(0, 0, 1, 1);
      foilWhiteTex = new THREE.CanvasTexture(c);
      foilWhiteTex.colorSpace = THREE.NoColorSpace;
      return foilWhiteTex;
    }

    function foilLoad(url, clamp) {
      if (!url) return foilWhite();
      var t = new THREE.TextureLoader().load(url);
      t.colorSpace = THREE.NoColorSpace;
      t.wrapS = t.wrapT = clamp ? THREE.ClampToEdgeWrapping : THREE.RepeatWrapping;
      t.anisotropy = 4;
      return t;
    }

    function foilLut(name) {
      var tab = FOIL_LUT[name];
      if (!tab) return foilWhite();
      var n = 256, data = new Uint8Array(n * 4);
      var interp = function (stops, x) {
        var f = Math.min(0.99999, Math.max(0, x)) * (stops.length - 1);
        var i = Math.floor(f), k = f - i;
        var a = stops[i], b = stops[Math.min(i + 1, stops.length - 1)];
        return (a + (b - a) * k) * 255;
      };
      var rows = tab.map(function (s) {
        return s.trim().split(/ +/).map(Number);
      });
      for (var x = 0; x < n; x++) {
        data[x * 4] = Math.round(interp(rows[0], x / (n - 1)));
        data[x * 4 + 1] = Math.round(interp(rows[1], x / (n - 1)));
        data[x * 4 + 2] = Math.round(interp(rows[2], x / (n - 1)));
        data[x * 4 + 3] = 255;
      }
      var t = new THREE.DataTexture(data, n, 1, THREE.RGBAFormat);
      t.minFilter = THREE.LinearFilter;
      t.magFilter = THREE.LinearFilter;
      t.wrapS = t.wrapT = THREE.ClampToEdgeWrapping;
      t.colorSpace = THREE.NoColorSpace;
      t.needsUpdate = true;
      return t;
    }

    function v4(a, b, c, d) { return new THREE.Vector4(a || 0, b || 0, c || 0, d || 0); }

    var FOIL_VS = 'varying vec2 vUv;\n' +
      'void main(){ vUv = uv; gl_Position = projectionMatrix * modelViewMatrix * vec4(position, 1.0); }';

    var FOIL_FS = 'precision highp float;\n' +
      'uniform float uTime;\n' +
      'uniform sampler2D uMain, uMask, uDist, uInteriorMap;\n' +
      'uniform sampler2D uBgTex;\n' +
      'uniform float uBgOn, uBgHasTex, uBgAspect;\n' +
      'uniform vec3 uBgColor;\n' +
      'uniform float uMaskClip, uAspect, uFaceContain, uFaceAspect, uContentScale;\n' +
      'uniform float uSubject, uOpacity;\n' +
      'uniform vec4 uMeshRect;\n' +
      'uniform vec4 uFaceBox;\n' +
      'uniform vec4 uDistCtl, uDistCtl2, uGlob, uGlob2, uDistTile, uTone, uSheen;\n' +
      'uniform vec4 uSheenA, uSheenB, uSheenC, uSheenD;\n' +
      'uniform sampler2D uFlowMask, uFlowNoise;\n' +
      'uniform float uFlowAmt, uFlowSpeed;\n' +
      'uniform sampler2D uTex0, uTex1, uTex2, uTex3, uTex4;\n' +
      'uniform vec4 uSlot0, uSlot1, uSlot2, uSlot3, uSlot4;\n' +
      'uniform vec4 uTile0, uTile1, uTile2, uTile3, uTile4;\n' +
      'uniform vec4 uMove0, uMove1, uMove2, uMove3, uMove4;\n' +
      'uniform vec4 uFx0, uFx1, uFx2, uFx3, uFx4;\n' +
      'uniform vec4 uCtl0, uCtl1, uCtl2, uCtl3, uCtl4;\n' +
      'uniform vec4 uF0, uF1, uF2, uF3, uF4;\n' +
      'uniform vec4 uPos0, uPos1, uPos2, uPos3, uPos4;\n' +
      'uniform vec4 uTint0, uTint1, uTint2, uTint3, uTint4;\n' +
      'uniform vec4 uLx0, uLx1, uLx2, uLx3, uLx4;\n' +
      'uniform vec4 uBlink0, uBlink1, uBlink2, uBlink3, uBlink4;\n' +
      'uniform sampler2D uLut0, uLut1, uLut2, uLut3, uLut4;\n' +
      'varying vec2 vUv;\n' +
      'const float PI = 3.14159265359;\n' +
      'const float INV2PI = 0.15915494309;\n' +
      'mat2 rotM(float a){ float c = cos(a), s = sin(a); return mat2(c, s, -s, c); }\n' +
      'float roundedCard(vec2 uv, float radius){\n' +
      '  vec2 scaled = vec2((uv.x - 0.5) * uAspect, uv.y - 0.5);\n' +
      '  vec2 halfSize = vec2(0.5 * uAspect, 0.5);\n' +
      '  vec2 edge = abs(scaled) - (halfSize - vec2(radius));\n' +
      '  return length(max(edge, 0.0)) + min(max(edge.x, edge.y), 0.0) - radius;\n' +
      '}\n' +
      'float maskChVal(vec2 uv, float ch){\n' +
      '  if (uGlob2.z < 0.5) return 1.0;\n' +   // 未上传蒙版：忽略通道，恒为 1
      '  vec4 m = texture2D(uMask, uv);\n' +
      '  float v;\n' +
      '  if (ch < 0.5) v = m.r; else if (ch < 1.5) v = m.g; else if (ch < 2.5) v = m.b;\n' +
      '  else if (ch < 3.5) v = m.a; else if (ch < 4.5) v = 1.0; else v = 1.0 - m.r;\n' +
      '  return clamp(v, 0.0, 1.0);\n' +
      '}\n' +
      'float areaVal(vec2 uv, float ty){\n' +
      '  if (ty < 0.5) return 1.0;\n' +
      '  if (ty < 1.5){ float d = 1.0 - uv.y;\n' +
      '    if (d < 0.18) return mix(1.0, 0.5, d / 0.18);\n' +
      '    if (d < 0.44) return mix(0.5, 0.12, (d - 0.18) / 0.26);\n' +
      '    if (d < 0.70) return mix(0.12, 0.0, (d - 0.44) / 0.26);\n' +
      '    return 0.0; }\n' +
      '  if (ty < 2.5){ float d = uv.y;\n' +
      '    if (d < 0.16) return mix(1.0, 0.92, d / 0.16);\n' +
      '    if (d < 0.40) return mix(0.92, 0.55, (d - 0.16) / 0.24);\n' +
      '    if (d < 0.64) return mix(0.55, 0.16, (d - 0.40) / 0.24);\n' +
      '    if (d < 0.86) return mix(0.16, 0.0, (d - 0.64) / 0.22);\n' +
      '    return 0.0; }\n' +
      '  vec2 e = vec2((uv.x - 0.5) / 0.64, (uv.y - 0.44) / 0.54);\n' +
      '  float r = length(e);\n' +
      '  if (r < 0.42) return mix(1.0, 0.7, r / 0.42);\n' +
      '  if (r < 0.70) return mix(0.7, 0.22, (r - 0.42) / 0.28);\n' +
      '  if (r < 0.92) return mix(0.22, 0.0, (r - 0.70) / 0.22);\n' +
      '  return 0.0;\n' +
      '}\n' +
      'vec4 slotUV(vec4 mv, vec4 fx, float mt, vec4 tile, vec2 uv, float te){\n' +
      '  vec2 T = vec2(tile.x == 0.0 ? 1e-4 : tile.x, tile.y == 0.0 ? 1e-4 : tile.y);\n' +
      '  vec2 O = tile.zw;\n' +
      '  vec2 tuv = uv * T + O;\n' +
      '  if (mt > 3.5) return vec4(tuv, fract(-mv.y * te), fract(-mv.x * te));\n' +
      '  if (mt > 2.5) { vec2 c = tuv - T * 0.5; return vec4(rotM(mv.w * te) * c + T * 0.5, 0.0, 0.0); }\n' +
      '  if (mt > 1.5) {\n' +
      '    vec2 r = rotM(-mv.z * PI / 180.0) * (tuv / T) * T;\n' +
      '    return vec4(r + vec2(fract(-mv.x * te), fract(-mv.y * te)), 0.0, 0.0);\n' +
      '  }\n' +
      '  return vec4(rotM(-fx.y) * tuv, 0.0, 0.0);\n' +
      '}\n' +
      'vec2 polarUV(vec4 muv, float spiral, vec4 tile){\n' +
      '  vec2 T = tile.xy;\n' +
      '  vec2 d = T * 0.5 - muv.xy;\n' +
      '  float r = length(d);\n' +
      '  float ang = atan(d.y, d.x) - r * spiral + PI;\n' +
      '  return vec2(r * INV2PI + muv.z, ang * INV2PI + muv.w);\n' +
      '}\n' +
      'vec3 blend(sampler2D tex, sampler2D lutT, vec4 col, vec4 mv, vec4 fx, vec4 ctl, vec4 f,\n' +
      '           vec4 tile, vec4 pos, vec4 tint, vec4 lx, vec4 bl, vec2 uv, float te, vec3 acc){\n' +
      '  if (ctl.w < 0.5) return acc;\n' +
      '  vec4 muv = slotUV(mv, fx, ctl.x, tile, uv, te);\n' +
      '  vec2 suv = (ctl.x > 3.5) ? polarUV(muv, fx.x, tile) : muv.xy;\n' +
      '  vec4 tx = texture2D(tex, suv);\n' +
      '  tx.rgb = (tx.rgb - 0.5) * tint.w + 0.5;\n' +
      '  tx.rgb *= tint.rgb;\n' +
      '  if (lx.x > 0.5){\n' +
      '    float l = clamp(dot(tx.rgb, vec3(0.213, 0.715, 0.072)), 0.0, 1.0);\n' +
      '    tx.rgb = texture2D(lutT, vec2(l, 0.5)).rgb;\n' +
      '  }\n' +
      '  float g = maskChVal(uv, f.y) * f.z * uDistCtl2.w * areaVal(uv, lx.y);\n' +
      '  float a = tx.a * col.a * g;\n' +
      '  float st = uGlob.x;\n' +
      '  if (ctl.z > 0.5){\n' +
      '    float w = 0.5 + 0.5 * sin(2.0 * PI * (f.x * te + pos.w));\n' +
      '    float lo = fx.z, hi = fx.w;\n' +
      '    if (pos.z > 0.5){ lo *= uGlob2.x; hi *= uGlob2.x; }\n' +
      '    a *= lo + (hi - lo) * w;\n' +
      '  }\n' +
      '  if (bl.x > 0.5){\n' +
      '    if (any(lessThan(suv, vec2(0.0))) || any(greaterThan(suv, vec2(1.0)))) return acc;\n' +
      '    float per = max(bl.y, 0.05);\n' +
      '    float halfD = clamp(bl.z * 0.5 / per, 0.001, 0.3);\n' +
      '    float ph = fract((te + bl.w) / per);\n' +
      '    float pulse = smoothstep(0.0, halfD, ph) * (1.0 - smoothstep(halfD, halfD * 2.0, ph));\n' +
      '    float blinkA = smoothstep(0.10, 0.80, tx.a) * clamp(f.w, 0.0, 1.0) * pulse;\n' +
      '    return mix(acc, tx.rgb, blinkA);\n' +
      '  }\n' +
      '  vec3 cMask  = tx.rgb * (col.rgb * g) * 2.0 * st;\n' +
      '  vec3 cPlain = tx.rgb * col.rgb * 2.0 * st;\n' +
      '  if (ctl.y > 2.5) return mix(acc, cPlain, a);\n' +
      '  if (ctl.y > 1.5) return acc - cMask;\n' +
      '  return acc + cMask;\n' +
      '}\n' +
      'void main(){\n' +
      // 主体层网格可能小于整卡（文本型 contain 适配）：uv 换算到卡片坐标供特效/蒙版采样
      '  vec2 uv = vUv;\n' +
      '  if (uSubject > 0.5) uv = vUv * uMeshRect.zw + uMeshRect.xy;\n' +
      '  if (roundedCard(uv, 0.032) > 0.0) discard;\n' +
      '  vec2 faceUv = vUv;\n' +
      '  if (uFaceContain > 0.5){\n' +
      '    vec2 cardUv2 = (uv - 0.5) * max(uContentScale, 1e-4) + 0.5;\n' +
      '    vec2 boxLo = uFaceBox.xy;\n' +
      '    vec2 boxSize = max(uFaceBox.zw - uFaceBox.xy, vec2(1e-6));\n' +
      '    vec2 inBox = (cardUv2 - boxLo) / boxSize;\n' +
      '    if (inBox.x < 0.0 || inBox.x > 1.0 || inBox.y < 0.0 || inBox.y > 1.0) discard;\n' +
      '    float boxAspect = boxSize.x * uAspect / boxSize.y;\n' +
      '    float fa = max(uFaceAspect, 1e-6);\n' +
      '    vec2 sc = vec2(min(1.0, fa / boxAspect), min(1.0, boxAspect / fa));\n' +
      '    vec2 zc = sc * max(uContentScale, 1e-4);\n' +
      '    faceUv = (inBox - 0.5) / zc + 0.5;\n' +
      '    if (faceUv.x < 0.0 || faceUv.x > 1.0 || faceUv.y < 0.0 || faceUv.y > 1.0) discard;\n' +
      '  }\n' +
      '  float te = uTime * uGlob2.y;\n' +
      '  vec2 disp = vec2(0.0);\n' +
      '  if (uDistCtl2.y > 0.5){\n' +
      '    float dmt = uDistCtl2.y;\n' +
      '    vec4 muv = slotUV(vec4(uDistCtl.xy, uDistCtl.z, 0.0), vec4(0.0), dmt, uDistTile, uv, te);\n' +
      '    vec2 duv = (dmt > 3.5) ? polarUV(muv, 0.0, uDistTile) : muv.xy;\n' +
      '    vec2 d = texture2D(uDist, duv).rg - 0.5;\n' +
      '    disp = clamp(d * vec2(uDistCtl.w, uDistCtl2.x), -0.06, 0.06);\n' +
      '    disp *= maskChVal(uv, uGlob2.w);\n' +
      '  }\n' +
      // 主体层：按自身 uv 采样纹理（位移需换算回网格 uv 尺度），特效图案仍按卡片坐标采样
      '  vec2 dispUv = (uSubject > 0.5) ? (disp * uMeshRect.zw) : disp;\n' +
      '  vec2 fuv = faceUv + dispUv;\n' +
      // 流动背景：只在「纯背景」掩码内给 UV 加慢速漂移 + 低频揉动。
      // 掩码为 0（立绘/UI）处 fuv 不变 → 立绘/文字/图标原地不动；
      // 只有一张贴图被揉，没有第二层覆盖，所以不会重影、也不会整体变亮。
      '  if (uFlowAmt > 0.0) {\n' +
'    float fmask = smoothstep(0.02, 0.55, texture2D(uFlowMask, faceUv).a);\n' +
'    vec2 efv = vec2(min(faceUv.x, 1.0 - faceUv.x), min(faceUv.y, 1.0 - faceUv.y));\n' +
'    float ef = smoothstep(0.0, 0.055, min(efv.x, efv.y));\n' +
'    float ft = uTime * uFlowSpeed;\n' +
'    // 推进波：两层不同速度的云团沿同一方向走，叠一个轻微涡流。\n' +
'    // 位移是行波（相位随时间推进），平滑底色上也能看出有东西在走。\n' +
'    vec2 dir = normalize(vec2(1.0, 0.32));\n' +
'    float p1 = dot(faceUv, vec2(3.10, 2.20)) - ft * 1.55;\n' +
'    float p2 = dot(faceUv, vec2(5.30, -3.70)) - ft * 2.85;\n' +
'    vec2 sw = dir * (sin(p1) * 0.55 + sin(p2) * 0.30) * 0.020\n' +
'            + vec2(cos(p2), sin(p1)) * 0.0060;\n' +
'    fuv += sw * fmask * ef * uFlowAmt;\n' +
      '  }\n' +
      '  vec2 euv = (uSubject > 0.5) ? uv : faceUv;\n' +
      '  vec4 mainTex = texture2D(uMain, fuv);\n' +
      '  vec3 col = mainTex.rgb;\n' +
      // 独立背景层存在时（卡面素材自带透明区），先在特效层内部合成背景底色，
      // 否则 uMain 的透明像素（RGB=黑）会被当成不透明黑块盖住背景。
      '  if (uBgOn > 0.5){\n' +
      '    vec2 bv = uv;\n' +
      '    if (uBgAspect > uAspect) { bv.x = (uv.x - 0.5) * (uAspect / uBgAspect) + 0.5; }\n' +
      '    else { bv.y = (uv.y - 0.5) * (uBgAspect / uAspect) + 0.5; }\n' +
      '    vec3 bgc = (uBgHasTex > 0.5) ? texture2D(uBgTex, clamp(bv, 0.001, 0.999)).rgb : uBgColor;\n' +
      '    col = mix(bgc, mainTex.rgb, mainTex.a);\n' +
      '  }\n' +
      '  col = blend(uTex0, uLut0, uSlot0, uMove0, uFx0, uCtl0, uF0, uTile0, uPos0, uTint0, uLx0, uBlink0, euv, te, col);\n' +
      '  col = blend(uTex1, uLut1, uSlot1, uMove1, uFx1, uCtl1, uF1, uTile1, uPos1, uTint1, uLx1, uBlink1, euv, te, col);\n' +
      '  col = blend(uTex2, uLut2, uSlot2, uMove2, uFx2, uCtl2, uF2, uTile2, uPos2, uTint2, uLx2, uBlink2, euv, te, col);\n' +
      '  col = blend(uTex3, uLut3, uSlot3, uMove3, uFx3, uCtl3, uF3, uTile3, uPos3, uTint3, uLx3, uBlink3, euv, te, col);\n' +
      '  col = blend(uTex4, uLut4, uSlot4, uMove4, uFx4, uCtl4, uF4, uTile4, uPos4, uTint4, uLx4, uBlink4, euv, te, col);\n' +
      '  vec3 c = col * uTone.z;\n' +
      '  float knee = clamp(uTone.x, 0.05, 0.999);\n' +
      '  if (uTone.y > 0.5){\n' +
      '    vec3 over = max(c - knee, 0.0) / (1.0 - knee);\n' +
      '    c = min(c, vec3(knee)) + (1.0 - knee) * (over / (over + 1.0));\n' +
      '  }\n' +
      '  if (uSheen.w > 0.5 && uSheen.y > 0.0){\n' +
      '    vec2 dir = vec2(cos(uSheen.z), sin(uSheen.z));\n' +
      '    float q = dot(uv - 0.5, dir) / 0.85 + 0.5;\n' +
      '    float band = smoothstep(0.34, 0.0, abs(q - uSheen.x));\n' +
      '    float k = clamp((q - (uSheen.x - 0.34)) / 0.68, 0.0, 1.0);\n' +
      '    vec3 sc;\n' +
      '    if (k < 0.25) sc = mix(uSheenA.rgb, uSheenB.rgb, k / 0.25);\n' +
      '    else if (k < 0.5) sc = mix(uSheenB.rgb, uSheenC.rgb, (k - 0.25) / 0.25);\n' +
      '    else if (k < 0.75) sc = mix(uSheenC.rgb, uSheenD.rgb, (k - 0.5) / 0.25);\n' +
      '    else sc = mix(uSheenD.rgb, uSheenA.rgb, (k - 0.75) / 0.25);\n' +
      '    c += sc * band * uSheen.y;\n' +
      '  }\n' +
      '  vec2 cardUv = (uv - 0.5) * max(uContentScale, 1e-4) + 0.5;\n' +
      '  float a;\n' +
      // 主体层（前景抠图）叠在整卡特效层之上：只按主体自身 alpha 绘制，
      // 不套边框内蒙版、不合成背景，否则主体透明区会被整块盖黑。
      '  if (uSubject > 0.5){\n' +
      '    a = mainTex.a * uOpacity;\n' +
      '  } else {\n' +
      '    a = mix(1.0, texture2D(uInteriorMap, cardUv).a, uMaskClip);\n' +
      // 没有独立背景层时跟随卡面自身 alpha（透明抠图卡不再被整块黑色特效层遮住）
      '    if (uBgOn < 0.5) a *= mainTex.a;\n' +
      // 卡体圆角：整卡网格是矩形，底图也是满幅矩形，不裁就会在圆角外露出直角小方块。
      // 与 cardMaterial 同一个函数、同一个半径（0.032），两条渲染路径轮廓才一致。
      '    a *= mix(1.0, clamp(-roundedCard(vUv, 0.032) / 0.004, 0.0, 1.0), uMaskClip);\n' +
      '    a *= uOpacity;\n' +
      '  }\n' +
      '  gl_FragColor = vec4(clamp(c, 0.0, 1.0), a);\n' +
      '}\n';

    function foilBuild(def, opts) {
      var o = opts || {};
      var isSubj = !!o.subject;      // 主体层：只画前景抠图，不合成背景、不套边框蒙版
      var mainTex = o.main || frontTexture;
      var g = def.globals || {};
      var d = def.dist || {};
      var slots = def.slots || [];
      var ord = slots.map(function (s, i) { return { s: s, i: i, l: (s._layer != null ? s._layer : (FOIL_ORDER[s.slot] == null ? 9 : FOIL_ORDER[s.slot])) }; })
        .sort(function (a, b) { return a.l - b.l || a.i - b.i; })
        .slice(0, 5);
      var cm = (typeof cardMaterial !== 'undefined') ? cardMaterial.uniforms : null;
      var interior = (typeof interiorTexture !== 'undefined') ? interiorTexture : foilWhite();
      // 独立背景层（卡面素材自带透明区时由 _bg_layer_js 创建）：特效层内部合成该背景，
      // 共享它的纹理 / 比例 / 底色 uniform，加载完成后自动跟随。
      var bgMesh = (typeof window !== 'undefined' && window.__BG_LAYER__) ? window.__BG_LAYER__ : null;
      var bgU = (bgMesh && bgMesh.material) ? bgMesh.material.uniforms : null;
      var bgTex = (bgU && bgU.uBgTexture) ? bgU.uBgTexture.value : null;
      var U = {
        uTime: { value: 0 },
        uMain: { value: mainTex },
      uFlowMask: { value: (def.flow && def.flow.mask) ? foilLoad(def.flow.mask, true) : foilWhite() },
      uFlowNoise: { value: (def.flow && def.flow.noise) ? foilLoad(def.flow.noise, false) : foilWhite() },
      uFlowAmt: { value: (def.flow && def.flow.amt) ? def.flow.amt : 0.0 },
      uFlowSpeed: { value: (def.flow && def.flow.speed) ? def.flow.speed : 1.0 },
        uSubject: { value: isSubj ? 1 : 0 },
        uOpacity: { value: o.opacity == null ? 1 : o.opacity },
        uMask: { value: def.mask ? foilLoad(def.mask, true) : foilWhite() },
        uDist: { value: (d.tex ? foilLoad(d.tex, false) : foilWhite()) },
        uInteriorMap: { value: interior },
        uBgOn: { value: (bgMesh && !isSubj) ? 1 : 0 },
        uBgHasTex: { value: bgTex ? 1 : 0 },
        uBgTex: { value: bgTex || foilWhite() },
        uBgAspect: (bgU && bgU.uTexAspect) ? bgU.uTexAspect : { value: 1 },
        uBgColor: (bgU && bgU.uColor) ? bgU.uColor : { value: new THREE.Color(0x0e0b09) },
        // 卡面窗口（边框内沿 contain）：直接共享 cardMaterial 的 uniform 对象，
        // 这样边框加载后异步重算的 uFaceBox/uFaceContain/uFaceAspect/uContentScale 会同步生效，
        // 特效层始终跟随主卡面的取景映射，不会与边框内的画面错位。
        uMaskClip: isSubj ? { value: 0.0 } : ((cm && cm.uMaskClip) ? cm.uMaskClip : { value: 1.0 }),
        uAspect: { value: CARD_WIDTH / CARD_HEIGHT },
        uFaceContain: isSubj ? { value: 0.0 } : ((cm && cm.uFaceContain) ? cm.uFaceContain : { value: 0.0 }),
        uFaceAspect: (cm && cm.uFaceAspect) ? cm.uFaceAspect : { value: 1.0 },
        uContentScale: (cm && cm.uContentScale) ? cm.uContentScale : { value: 1.0 },
        uFaceBox: (cm && cm.uFaceBox) ? cm.uFaceBox : { value: v4(0, 0, 1, 1) },
        uMeshRect: { value: v4(0, 0, 1, 1) },
        uDistCtl: { value: v4(d.su, d.sv, d.sa, (d.iu || 0) * (g.distPow == null ? 1 : g.distPow)) },
        uDistCtl2: { value: v4((d.iv || 0) * (g.distPow == null ? 1 : g.distPow), d.mt || 0, 0, (g.maskPow == null ? 1 : g.maskPow)) },
        uDistTile: { value: v4(d.tx == null ? 1 : d.tx, d.ty == null ? 1 : d.ty, d.ox, d.oy) },
        uGlob: { value: v4(g.fxPow == null ? 0.6 : g.fxPow, g.flashPow == null ? 1 : g.flashPow, 0, 0) },
        uGlob2: { value: v4(g.blink == null ? 1 : g.blink, g.motionDir == null ? 1 : g.motionDir, def.mask ? 1 : 0, d.maskCh == null ? 4 : d.maskCh) },
        uTone: { value: v4(g.toneKnee == null ? 0.85 : g.toneKnee, g.toneOn === 0 ? 0 : 1, g.exposure == null ? 1 : g.exposure, 0) },
        uSheen: { value: v4(1.2, 0, (g.sheenAng || 0) * PI_JS / 180, 0) },
        uSheenA: { value: v4(1, 1, 1, 1) },
        uSheenB: { value: v4(1, 1, 1, 1) },
        uSheenC: { value: v4(1, 1, 1, 1) },
        uSheenD: { value: v4(1, 1, 1, 1) }
      };
      var pal = FOIL_SHEEN[g.sheen] || FOIL_SHEEN.rainbow;
      U.uSheen.value.y = (g.sheen && g.sheen !== 'off') ? (g.sheenPow == null ? 0.25 : g.sheenPow) : 0;
      U.uSheen.value.w = (U.uSheen.value.y > 0.0) ? 1.0 : 0.0;
      U.uSheenA.value.set(pal[0][0], pal[0][1], pal[0][2], 1);
      U.uSheenB.value.set(pal[1][0], pal[1][1], pal[1][2], 1);
      U.uSheenC.value.set(pal[2][0], pal[2][1], pal[2][2], 1);
      U.uSheenD.value.set(pal[3][0], pal[3][1], pal[3][2], 1);
      for (var i = 0; i < 5; i++) {
        U['uTex' + i] = { value: foilWhite() };
        U['uLut' + i] = { value: foilWhite() };
        U['uSlot' + i] = { value: v4(0, 0, 0, 0) };
        U['uTile' + i] = { value: v4(1, 1, 0, 0) };
        U['uMove' + i] = { value: v4(0, 0, 0, 0) };
        U['uFx' + i] = { value: v4(0, 0, 0, 1) };
        U['uCtl' + i] = { value: v4(1, 1, 0, 0) };
        U['uF' + i] = { value: v4(1, 4, 0, 0) };
        U['uPos' + i] = { value: v4(0, 0, 0, 0) };
        U['uTint' + i] = { value: v4(1, 1, 1, 1) };
        U['uLx' + i] = { value: v4(0, 0, 0, 0) };
        U['uBlink' + i] = { value: v4(0, 0, 0, 0) };
      }
      ord.forEach(function (o, i) {
        var s = o.s;
        var c = s.color || [1, 1, 1, 1];
        var tint = s.tint || [1, 1, 1];
        U['uTex' + i].value = foilLoad(s.tex, s.slot === 'Middle');
        U['uLut' + i].value = (s.lut && FOIL_LUT[s.lut]) ? foilLut(s.lut) : foilWhite();
        U['uSlot' + i].value.set(c[0], c[1], c[2], c[3] == null ? 1 : c[3]);
        U['uTile' + i].value.set(s.tx == null ? 1 : s.tx, s.ty == null ? 1 : s.ty, s.ox || 0, s.oy || 0);
        U['uMove' + i].value.set(s.su || 0, s.sv || 0, s.sa || 0, (s.rot || 0) + (s.spin || 0) * PI_JS / 180);
        U['uFx' + i].value.set(s.spiral || 0, s.fixrot || 0, s.fmin || 0, s.fmax == null ? 1 : s.fmax);
        // 无贴图的槽位（如 tx_face_* 卡面差分在 cardforge 无对应素材）直接关闭，
        // 否则会采样到 1×1 白贴图，加法混合会把整张卡面刷白。
        U['uCtl' + i].value.set(s.mt == null ? 1 : s.mt, s.rt == null ? 1 : s.rt, s.flash ? 1 : 0,
          (s.tex && s.en ? 1 : 0));
        U['uF' + i].value.set(s.fspd == null ? 1 : s.fspd, s.maskCh == null ? 4 : s.maskCh, s.maskStr == null ? 1 : s.maskStr, s.blink ? (s.bpeak == null ? 1 : s.bpeak) : 0);
        U['uPos' + i].value.set(0, 0, (s.slot === 'Middle') ? 1 : 0, s.foff || 0);
        U['uLx' + i].value.set((s.lut && FOIL_LUT[s.lut]) ? 1 : 0, s.area || 0, 0, 0);
        U['uBlink' + i].value.set(s.blink ? 1 : 0, s.bper == null ? 3.2 : s.bper, s.bdur == null ? 0.12 : s.bdur, s.bphase || 0);
        U['uTint' + i].value.set(tint[0], tint[1], tint[2], s.contrast == null ? 1 : s.contrast);
        U['uTex' + i].value.wrapS = U['uTex' + i].value.wrapT =
          (s.slot === 'Middle') ? THREE.ClampToEdgeWrapping : THREE.RepeatWrapping;
      });
      var mat = new THREE.ShaderMaterial({
        uniforms: U,
        vertexShader: FOIL_VS,
        fragmentShader: FOIL_FS,
        transparent: true,
        depthWrite: false,
        side: THREE.FrontSide,
        toneMapped: false
      });
      var mesh = new THREE.Mesh(new THREE.PlaneGeometry(CARD_WIDTH, CARD_HEIGHT), mat);
      mesh.position.set(0, 0, o.z == null ? 0.004 : o.z);
      mesh.renderOrder = o.renderOrder == null ? 1 : o.renderOrder;
      var parent = o.parent || cardMesh;
      parent.add(mesh);
      return { mesh: mesh, uniforms: U, def: def, t: 0, speed: (g.speed == null ? 1 : g.speed), parent: parent };
    }

    function foilUpdate(fo, dt) {
      fo.t += dt * (fo.speed || 1);
      fo.uniforms.uTime.value = fo.t;
      var g = fo.def.globals || {};
      if (fo.uniforms.uSheen.value.w > 0.5) {
        // 虹光扫光：每周期前 55% 扫过（1.2 → -0.2），其余时间停在起点外
        var per = Math.max(1, g.sheenSpd == null ? 4.5 : g.sheenSpd);
        var ph = (fo.t % per) / per;
        var f = ph < 0.55 ? ph / 0.55 : 1;
        var e = f * f * (3 - 2 * f);
        fo.uniforms.uSheen.value.x = 1.2 - 1.4 * e;
      }
    }

    // 前景主体（收集册成品卡的 foreground.png）浮在整卡特效层之上，不补这一层主体就没有特效。
    // 主体纹理用 NoColorSpace 直通（与 frontTexture 一致），只按自身 alpha 绘制；
    // mesh 挂在 foregroundGroup 内、紧跟 fgMesh 之后（低于描边层），随主体视差一起移动。
    function foilBuildSubject(def) {
      if (typeof foregroundGroup === 'undefined' || typeof fgMesh === 'undefined') return;
      var img = foregroundTexture && foregroundTexture.image;
      if (!img || !img.width) return;
      if (foilSubj) { (foilSubj.parent || cardMesh).remove(foilSubj.mesh); foilSubj = null; }
      var stex = new THREE.Texture();
      stex.image = img;
      stex.colorSpace = THREE.NoColorSpace;
      stex.anisotropy = 8;
      stex.needsUpdate = true;
      foilSubj = foilBuild(def, {
        main: stex,
        parent: foregroundGroup,
        subject: true,
        z: fgMesh.position.z + 0.0002,
        renderOrder: fgMesh.renderOrder + 0.02,
        opacity: (fgMesh.material && fgMesh.material.opacity != null) ? fgMesh.material.opacity : 1
      });
      // 挂上 map 让文本型 contain 适配（_fg_adapt_js）能按源图比例同步缩放/定位本层
      foilSubj.mesh.material.map = stex;
      // 与整卡特效层保持同一动画相位
      if (foilObj) foilSubj.t = foilObj.t;
    }

    function foilApply(list) {
      if (typeof cardMesh === 'undefined' || typeof frontTexture === 'undefined') return;
      if (foilObj) { (foilObj.parent || cardMesh).remove(foilObj.mesh); foilObj = null; }
      if (foilSubj) { (foilSubj.parent || cardMesh).remove(foilSubj.mesh); foilSubj = null; }
      foilSubjDef = null;
      var item = null;
      for (var i = 0; i < (list || []).length; i++) {
        var dd = (list[i] || {}).def || {};
        if (dd.engine === 'foil') { item = list[i]; break; }
      }
      if (item) {
        foilObj = foilBuild(item.def);
      window.__foilObj = foilObj;
        if (typeof foregroundGroup !== 'undefined' && typeof fgMesh !== 'undefined') {
          if (foregroundTexture && foregroundTexture.image && foregroundTexture.image.width) {
            foilBuildSubject(item.def);
          } else {
            foilSubjDef = item.def;   // 纹理异步加载中：tick 里等就绪再建
          }
        }
      }
    }

    return { init: init, tick: tick };
  })();
"""

# 前景组（透明主体）两种渲染顺序：
#   默认 —— 主体被边框遮盖（renderOrder 2/3，边框 4、卡封 5、文本 6）
#   浮于边框上 —— 主体提升到边框之上、卡封之下（z 0.013/0.014，renderOrder 4.2/4.4）
CARD_HTML_TEMPLATE_FG = r"""  const foregroundGroup = new THREE.Group();
  cardContent.add(foregroundGroup);
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
  cardContent.add(foregroundGroup);
  const shadowMesh = new THREE.Mesh(
    new THREE.PlaneGeometry(CARD_WIDTH, CARD_HEIGHT),
    new THREE.MeshBasicMaterial({ map: foregroundTexture, color: 0x1c1008, transparent: true, opacity: 0.42, alphaTest: 0.02, depthWrite: false, side: THREE.FrontSide, toneMapped: false })
  );
  shadowMesh.position.set(-0.03, -0.036, 0.013);
  shadowMesh.renderOrder = 4.2;
  foregroundGroup.add(shadowMesh);
  const fgMesh = new THREE.Mesh(
    new THREE.PlaneGeometry(CARD_WIDTH, CARD_HEIGHT),
    new THREE.MeshBasicMaterial({ map: foregroundTexture, transparent: true, opacity: 1.0, alphaTest: 0.02, depthWrite: false, side: THREE.FrontSide, toneMapped: false })
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
  html, body { width: 100%; height: 100%; overflow: hidden; overscroll-behavior: none; }
  body {
    background: radial-gradient(1200px 700px at 50% 20%, #2b2119 0%, #120d09 55%, #080605 100%);
    font-family: "Microsoft YaHei", "PingFang SC", system-ui, sans-serif;
    -webkit-user-select: none; user-select: none; -webkit-touch-callout: none;
  }
  canvas { touch-action: none; }
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
  const CARD_WIDTH = __CARD_WIDTH__;
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
  __BG_FOLLOW_LOAD__
  // 纹理色彩空间：cardMaterial 等 ShaderMaterial 的输出不会做 sRGB 编码（r152 起仅内置材质自动
  // linearToOutputTexel），若纹理再被 GPU 解码为线性，画面会整体变暗、暗部压黑、暖色加重。
  // 因此 ShaderMaterial 采样的 front/back 保持 sRGB 直通（NoColorSpace）；
  // foreground 由内置 MeshBasicMaterial（前景/阴影）采样，保留 sRGB 解码+编码。
  for (const t of [frontTexture, backTexture]) {
    t.colorSpace = THREE.NoColorSpace;
    t.anisotropy = 8;
  }
  foregroundTexture.colorSpace = THREE.SRGBColorSpace;
  foregroundTexture.anisotropy = 8;
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
      uContentScale: { value: 1.0 },
      uSide: { value: 2.0 },
      uFaceContain: { value: 0.0 },
      uFaceBox: { value: new THREE.Vector4(0, 0, 1, 1) },
      uFaceAspect: { value: 1.0 },
      __FLOW_UNIFORM_VALUES__
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
      uniform sampler2D uInteriorMap;
      uniform float uMaskClip;
      uniform vec2 uPointer;
      uniform float uHover;
      uniform float uAspect;
      uniform float uSeal;
      uniform float uContentScale;
      uniform float uSide;
      uniform float uFaceContain;
      uniform vec4 uFaceBox;
      uniform float uFaceAspect;
      __FLOW_GLSL__
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
          // uSide：2=双面（默认）0=仅卡面网格 1=仅卡背网格
          // 仅卡背网格（uSide=1）的正面片段丢弃，让位给卡面网格；双面（2）与仅卡面（0）保留
          if (uSide > 0.5 && uSide < 1.5) discard;
          vec2 faceUv = uv;
          // 非浮起+边框+卡面素材：卡面 contain 到边框内沿（中空区域）以内，完整可见不超框；
          // 保持宽高比居中放入窗口，content_scale 围绕窗口中心缩放（与 2D compositor 一致）
          if (uFaceContain > 0.5) {
            // 卡面网格随 content_scale 缩放时，UV 反算回卡片坐标（与下方蒙版采样一致）
            vec2 cardUv2 = (uv - 0.5) * max(uContentScale, 1e-4) + 0.5;
            vec2 boxLo = uFaceBox.xy;
            vec2 boxSize = max(uFaceBox.zw - uFaceBox.xy, 1e-6);
            vec2 inBox = (cardUv2 - boxLo) / boxSize;
            if (inBox.x < 0.0 || inBox.x > 1.0 || inBox.y < 0.0 || inBox.y > 1.0) discard;
            float boxAspect = boxSize.x * uAspect / boxSize.y;
            float faceAspect = max(uFaceAspect, 1e-6);
            vec2 sc = vec2(min(1.0, faceAspect / boxAspect), min(1.0, boxAspect / faceAspect));
            vec2 zc = sc * max(uContentScale, 1e-4);
            faceUv = (inBox - 0.5) / zc + 0.5;
            if (faceUv.x < 0.0 || faceUv.x > 1.0 || faceUv.y < 0.0 || faceUv.y > 1.0) discard;
          }
          __FLOW_WARP__
          base = texture2D(uFrontTexture, flowUv);
          // 边框内区域蒙版：正面内容仅绘制在边框外围以内（含中空）；卡背不受蒙版限制。
          // 卡面随 content_scale 缩放时（网格在 cardContent 组内），UV 需反算回卡片坐标，
          // 否则蒙版随卡面一起缩放导致边缘错位（采样点偏离真实卡片位置）
          // 缩放后网格 vUv 0..1 覆盖整卡几何，卡片坐标 = (vUv-0.5)*s + 0.5（s=uContentScale）
          vec2 cardUv = (vUv - 0.5) * max(uContentScale, 1e-4) + 0.5;
          // mask_clip=false（内边框效果）时正面卡面不裁到边框内，可延伸出边框外沿一圈图像
          base.a *= mix(1.0, texture2D(uInteriorMap, cardUv).a, uMaskClip);
        } else {
          // 仅卡面网格的背面片段丢弃（uSide=0）
          if (uSide < 0.5) discard;
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
          // 卡背同样按边框内区域蒙版裁剪（与正面一致：翻面后轮廓跟随边框形状）；
          // mask_clip=False（内边框效果）时卡背不裁到边框内，可延伸出边框外沿（与正面同步）
          // 蒙版按物理坐标 vUv 采样（不随视差平移、不镜像）——镜像会导致蒙版左右颠倒，露出黑底
          base.a *= mix(1.0, texture2D(uInteriorMap, vUv).a, uMaskClip);
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

  // ---- 辉光 aura（沿蒙版边缘绘制：采样蒙版有符号距离场，不再用固定圆角矩形） ----
  // 样式由 GLOW_EFFECTS 预设注入：uGlowTop/Bottom 颜色渐变、uGlowTight/uGlowSoft 距离、
  // uGlowMode 形状模式（0渐变/1四角/2双色/3菲涅尔/4双环/5RGB循环/6霓虹/7发廊螺纹）、
  // uGlowFresnel 视角权重（1=正对 0=侧视，由渲染循环每帧更新）、uTime 动画时间
  const auraMaterial = new THREE.ShaderMaterial({
    transparent: true,
    depthWrite: false,
    depthTest: false,
    side: THREE.DoubleSide,
    blending: THREE.AdditiveBlending,
    toneMapped: false,
    uniforms: {
      uSdfMap: { value: null },
      uGlowTop: { value: new THREE.Vector3(0.98, 0.63, 0.18) },
      uGlowBottom: { value: new THREE.Vector3(0.44, 0.08, 0.025) },
      uGlowTight: { value: 0.014 },
      uGlowSoft: { value: 0.045 },
      uGlowMode: { value: 0 },
      uGlowFacing: { value: 1.0 },
      uGlowFresnel: { value: 0.0 },
      uGlowSpeed: { value: 0.0 },
      uTime: { value: 0.0 },
      uGlowStrength: { value: 1.0 },
    },
    vertexShader: `
      varying vec2 vUv;
      void main() {
        vUv = uv;
        gl_Position = projectionMatrix * modelViewMatrix * vec4(position, 1.0);
      }
    `,
    fragmentShader: `
      uniform sampler2D uSdfMap;
      uniform vec3 uGlowTop;
      uniform vec3 uGlowBottom;
      uniform float uGlowTight;
      uniform float uGlowSoft;
      uniform float uGlowMode;
      uniform float uGlowFacing;
      uniform float uGlowFresnel;
      uniform float uGlowSpeed;
      uniform float uTime;
      uniform float uGlowStrength;
      varying vec2 vUv;
      // 蒙版有符号距离（卡高 UV 单位，正=蒙版外）：由 SDF 画布解码
      // 编码约定：128=蒙版边缘；>128=外部距离（127 分度对应 0.15 卡高）；<128=内部（不发光）
      // SDF 画布覆盖整张 aura 网格（uv 0..1），卡体位于画布中央、四周留辉光余量
      float maskDistance(vec2 uv) {
        float v = texture2D(uSdfMap, uv).r;
        return (v * 255.0 - 128.0) / 127.0 * 0.15;
      }
      void main() {
        float dist = maskDistance(vUv);
        float outside = step(0.0, dist);
        // mode 4 双环：紧致高亮环 + 外围大范围淡光（两层 smoothstep 独立调节）
        float tight, soft;
        if (uGlowMode > 3.5 && uGlowMode < 4.5) {
          tight = 1.0 - smoothstep(0.0, uGlowTight * 0.5, dist);
          soft = 1.0 - smoothstep(0.004, uGlowSoft * 1.6, dist);
        } else {
          tight = 1.0 - smoothstep(0.0, uGlowTight, dist);
          soft = 1.0 - smoothstep(0.006, uGlowSoft, dist);
        }
        // 基础色：纵向渐变
        vec3 glowColor = mix(uGlowBottom, uGlowTop, vUv.y);
        // mode 1 四角聚焦：四角更亮、边中点较弱（卡体四角在画布 uv 约 0.103/0.897）
        if (uGlowMode > 0.5 && uGlowMode < 1.5) {
          float corner = 0.0;
          corner += pow(max(0.0, 1.0 - distance(vUv, vec2(0.103, 0.103)) * 3.2), 2.0);
          corner += pow(max(0.0, 1.0 - distance(vUv, vec2(0.897, 0.103)) * 3.2), 2.0);
          corner += pow(max(0.0, 1.0 - distance(vUv, vec2(0.103, 0.897)) * 3.2), 2.0);
          corner += pow(max(0.0, 1.0 - distance(vUv, vec2(0.897, 0.897)) * 3.2), 2.0);
          glowColor *= 0.35 + 1.65 * corner;
        }
        // mode 5 RGB循环变色：三通道正弦错相，平滑循环衔接（非脉冲）
        if (uGlowMode > 4.5 && uGlowMode < 5.5) {
          float t = uTime * uGlowSpeed;
          glowColor = vec3(0.5 + 0.5 * sin(t), 0.5 + 0.5 * sin(t + 2.094), 0.5 + 0.5 * sin(t + 4.189));
          glowColor = glowColor * 0.8 + 0.2;
        }
        // mode 6 霓虹灯：两种灯交替明暗（平滑正弦错相）+ 轻微灯管频闪
        if (uGlowMode > 5.5 && uGlowMode < 6.5) {
          float ph = uTime * uGlowSpeed * 6.2832;
          float a = 0.5 + 0.5 * sin(ph);
          float b = 0.5 + 0.5 * sin(ph + 3.1416);
          float flicker = 0.92 + 0.08 * sin(uTime * 23.0) * sin(uTime * 17.0);
          glowColor = (uGlowTop * (0.25 + 0.75 * a) + uGlowBottom * (0.25 + 0.75 * b)) * flicker * 1.5;
        }
        // mode 7 发廊螺纹：沿边框旋转的红白蓝三色彩条（角度驱动）
        if (uGlowMode > 6.5 && uGlowMode < 7.5) {
          float ang = atan(vUv.y - 0.5, vUv.x - 0.5);
          float stripe = fract(ang / 6.2832 * 3.0 + uTime * uGlowSpeed);
          vec3 c1 = vec3(1.0, 0.3, 0.45);
          vec3 c2 = vec3(1.0, 1.0, 1.0);
          vec3 c3 = vec3(0.35, 0.65, 1.0);
          glowColor = stripe < 0.333 ? c1 : (stripe < 0.666 ? c2 : c3);
        }
        // 菲涅尔描边：倾斜（侧视）时边缘光增强，仅视角驱动、无频闪
        float alphaBase = tight * 0.14 + soft * 0.045;
        if (uGlowFresnel > 0.01) {
          float tilt = 1.0 - uGlowFacing;
          alphaBase *= 1.0 + uGlowFresnel * (0.5 + 1.7 * smoothstep(0.3, 0.85, tilt));
        }
        float alpha = outside * alphaBase * uGlowStrength;
        gl_FragColor = vec4(glowColor, alpha);
      }
    `,
  });
  // 辉光朝向/时间每帧更新辅助向量
  const _qGlow = new THREE.Quaternion();
  const _vN = new THREE.Vector3();
  const _vGlow = new THREE.Vector3();
__GLOW_INIT__

  // ---- 边框内区域蒙版画布（白=边框环+中空，黑=边框外围）----
  // 正面整卡内容（卡面/主体/层1卡封）仅绘制在边框外围以内；默认全白（无边框时内容完整）
  const interiorCanvas = document.createElement('canvas');
  interiorCanvas.width = 450;
  interiorCanvas.height = 600;
  const interiorCtx = interiorCanvas.getContext('2d');
  interiorCtx.fillStyle = '#ffffff';
  interiorCtx.fillRect(0, 0, 450, 600);
  const interiorTexture = new THREE.CanvasTexture(interiorCanvas);
  cardMaterial.uniforms.uInteriorMap = { value: interiorTexture };
  // 主 shader 正面卡面按边框内蒙版裁剪的开关（默认裁剪；mask_clip=False 时由 mask_js 置 0）
  cardMaterial.uniforms.uMaskClip = { value: 1.0 };

  // ---- 蒙版有符号距离场画布（辉光沿蒙版边缘绘制，随蒙版重绘同步更新） ----
  // 编码：128=蒙版边缘；>128=外部距离（127 分度对应 0.15 卡高 UV，≈0.35px/分度）；<128=内部（不发光）
  const sdfCanvas = document.createElement('canvas');
  sdfCanvas.width = 450;
  sdfCanvas.height = 600;
  const sdfCtx = sdfCanvas.getContext('2d');
  const sdfTexture = new THREE.CanvasTexture(sdfCanvas);
  sdfTexture.colorSpace = THREE.NoColorSpace;
  function updateInteriorSdf() {
    const W = 450, H = 600;
    const N = W * H;
    const out = sdfCtx.createImageData(W, H);
    const od = out.data;
    const src = interiorCtx.getImageData(0, 0, W, H).data;
    let hasOutside = false;
    for (let i = 0; i < N; i++) { if (src[i * 4 + 3] <= 8) { hasOutside = true; break; } }
    // 画布覆盖整张 aura 网格（uv 0..1），卡体位于画布中央、四周留辉光余量（卡体占 1/AURA_SCALE）
    const axScale = CARD_WIDTH / CARD_HEIGHT * AURA_SCALE; // 网格 x 范围（卡高单位）
    const ayScale = AURA_SCALE;                            // 网格 y 范围（卡高单位）
    const halfW = 0.5 * CARD_WIDTH / CARD_HEIGHT;          // 卡体半宽（卡高单位）
    const halfH = 0.5;                                     // 卡体半高（卡高单位）
    const radius = 0.032;                                  // 圆角半径（卡高单位），与原版辉光一致
    if (!hasOutside) {
      // 无边框（蒙版全白铺满）：回退为卡体圆角矩形距离，与旧辉光一致（卡体中央、四周为正距离）
      parallaxDist = -1; // 无边框不限制视差
      for (let y = 0; y < H; y++) {
        for (let x = 0; x < W; x++) {
          const i = y * W + x;
          const ax = (x / W - 0.5) * axScale, ay = (y / H - 0.5) * ayScale;
          const ex = Math.abs(ax) - (halfW - radius), ey = Math.abs(ay) - (halfH - radius);
          const s = Math.hypot(Math.max(ex, 0), Math.max(ey, 0)) + Math.min(Math.max(ex, ey), 0) - radius;
          const v8 = Math.max(0, Math.min(255, Math.round(128 + s / 0.15 * 127)));
          od[i * 4] = od[i * 4 + 1] = od[i * 4 + 2] = v8;
          od[i * 4 + 3] = 255;
        }
      }
      sdfCtx.putImageData(out, 0, 0);
      sdfTexture.needsUpdate = true;
      return;
    }
    // 有蒙版：把卡尺寸内蒙版嵌入画布中央（卡体占 1/AURA_SCALE），再做 3-4 倒角距离变换。
    // 内部（蒙版白区）作为距离源 0 向四周传播；编码时内部仍为负值（不发光），外部为正距离
    const d = new Float32Array(N);
    const INF = 1e6;
    const insideFlag = new Uint8Array(N);
    const offX = W * 0.5 * (1 - 1 / AURA_SCALE);
    const offY = H * 0.5 * (1 - 1 / AURA_SCALE);
    for (let y = 0; y < H; y++) {
      for (let x = 0; x < W; x++) {
        const i = y * W + x;
        const mx = Math.round((x - offX) * AURA_SCALE);
        const my = Math.round((y - offY) * AURA_SCALE);
        const isIn = (mx >= 0 && mx < W && my >= 0 && my < H && src[(my * W + mx) * 4 + 3] > 8);
        insideFlag[i] = isIn ? 1 : 0;
        d[i] = isIn ? 0 : INF;
      }
    }
    for (let y = 0; y < H; y++) {
      for (let x = 0; x < W; x++) {
        const i = y * W + x;
        let best = d[i], v;
        if (x > 0 && (v = d[i - 1] + 3) < best) best = v;
        if (x > 0 && y > 0 && (v = d[i - 451] + 4) < best) best = v;
        if (y > 0 && (v = d[i - 450] + 3) < best) best = v;
        if (x < 449 && y > 0 && (v = d[i - 449] + 4) < best) best = v;
        d[i] = best;
      }
    }
    for (let y = 599; y >= 0; y--) {
      for (let x = 449; x >= 0; x--) {
        const i = y * W + x;
        let best = d[i], v;
        if (x < 449 && (v = d[i + 1] + 3) < best) best = v;
        if (x < 449 && y < 599 && (v = d[i + 451] + 4) < best) best = v;
        if (y < 599 && (v = d[i + 450] + 3) < best) best = v;
        if (x > 0 && y < 599 && (v = d[i + 449] + 4) < best) best = v;
        d[i] = best;
      }
    }
    const pxToChu = ayScale / H; // 1 画布像素 = ayScale/H 卡高
    for (let i = 0; i < N; i++) {
      // 内部像素 → -1.0（不发光）；外部：倒角权重÷3 得像素数，再换算卡高 UV 距离
      const s = insideFlag[i] ? -1.0 : d[i] / 3 * pxToChu;
      const v8 = Math.max(0, Math.min(255, Math.round(128 + s / 0.15 * 127)));
      od[i * 4] = od[i * 4 + 1] = od[i * 4 + 2] = v8;
      od[i * 4 + 3] = 255;
    }
    // 视差安全距离：从画布中心（卡体中心）8 邻域 BFS 到最近蒙版边缘，换算场景单位
    const cx = 225, cy = 300;
    if (src[(cy * W + cx) * 4 + 3] <= 8) {
      parallaxDist = 0; // 中心不在边框内部：彻底关闭视差，避免穿帮
    } else {
      const seen = new Uint8Array(N);
      const qx = new Int32Array(N), qy = new Int32Array(N);
      let head = 0, tail = 0;
      qx[tail] = cx; qy[tail] = cy; tail++;
      seen[cy * W + cx] = 1;
      let found = -1;
      const DX8 = [1, 1, 0, -1, -1, -1, 0, 1];
      const DY8 = [0, 1, 1, 1, 0, -1, -1, -1];
      while (head < tail && found < 0) {
        const x = qx[head], y = qy[head]; head++;
        for (let k = 0; k < 8; k++) {
          const nx = x + DX8[k], ny = y + DY8[k];
          if (nx < 0 || nx >= W || ny < 0 || ny >= H) continue;
          const ni = ny * W + nx;
          if (seen[ni]) continue;
          if (src[ni * 4 + 3] <= 8) { found = Math.hypot(nx - cx, ny - cy); break; }
          seen[ni] = 1;
          qx[tail] = nx; qy[tail] = ny; tail++;
        }
      }
      parallaxDist = (found > 0 ? found : 0) * pxToChu * CARD_HEIGHT;
    }
    sdfCtx.putImageData(out, 0, 0);
    sdfTexture.needsUpdate = true;
  }
  auraMaterial.uniforms.uSdfMap.value = sdfTexture;
  updateInteriorSdf();

  // ---- 卡片组 ----
  const floatGroup = new THREE.Group();
  const tiltGroup = new THREE.Group();
  const flipGroup = new THREE.Group();
  window.__flipGroup = flipGroup;
  floatGroup.add(tiltGroup);
  tiltGroup.add(flipGroup);

  // 卡牌内容组：卡面/主体/特效锚点随 content_scale 缩放（背景/层1卡封/边框/文字/层2卡封整卡尺寸），
  "// 由界面\"缩放\"输入控制，默认 1.0 不缩放；卡面网格仅在卡面素材时随缩放组（背景底图不缩放）\n"
  const cardContent = new THREE.Group();
  flipGroup.add(cardContent);

  window.__cardMat = cardMaterial;
  const cardMesh = new THREE.Mesh(new THREE.PlaneGeometry(CARD_WIDTH, CARD_HEIGHT), cardMaterial);
  flipGroup.add(cardMesh);
  const hitMeshes = [cardMesh];
  __FACE_MESH_JS__
  __BG_LAYER__

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
  __FG_ADAPT_JS__

  // ---- 边框 / 卡封叠加层（可选） ----
  __FRAME_LAYER__
  __SEAL_LAYER__

  // ---- 卡面描述文字层（可选，Canvas 纹理叠加） ----
  __TEXT_LAYER__

  scene.add(floatGroup);

  // ---- 特效系统（__EFFECTS__ 注入特效列表） ----
  var effectAnchor = new THREE.Group();
  cardContent.add(effectAnchor);
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
      pointers.set(e.pointerId, e);
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

  // ---- 视差安全边界（方案A：按蒙版距离场限制主体位移，窄边框时自动收窄，绝不穿帮） ----
  // parallaxDist = 卡体中心到边框内沿距离（场景单位）；-1 表示无边框，不限制
  var parallaxDist = -1;
  var subjectBox = null; // { halfW, halfH, offX, offY } 主体可见包围盒（场景单位，Y 向上）
  function computeSubjectBox() {
    var img = foregroundTexture.image;
    if (!img || !img.width) return null;
    var sw = 200, sh = Math.max(1, Math.round(sw * img.height / img.width));
    var cv = document.createElement('canvas');
    cv.width = sw; cv.height = sh;
    var ctx = cv.getContext('2d');
    ctx.drawImage(img, 0, 0, sw, sh);
    var d = ctx.getImageData(0, 0, sw, sh).data;
    var x0 = sw, y0 = sh, x1 = -1, y1 = -1;
    for (var y = 0; y < sh; y++) {
      for (var x = 0; x < sw; x++) {
        if (d[(y * sw + x) * 4 + 3] > 10) {
          if (x < x0) x0 = x; if (x > x1) x1 = x;
          if (y < y0) y0 = y; if (y > y1) y1 = y;
        }
      }
    }
    if (x1 < 0) return null;
    var cxn = (x0 + x1 + 1) / 2 / sw, cyn = (y0 + y1 + 1) / 2 / sh;
    return {
      halfW: ((x1 - x0 + 1) / sw) * 0.5 * CARD_WIDTH,
      halfH: ((y1 - y0 + 1) / sh) * 0.5 * CARD_HEIGHT,
      offX: (cxn - 0.5) * CARD_WIDTH,
      offY: (0.5 - cyn) * CARD_HEIGHT
    };
  }
  // 限制后的目标位移：有边框时主体可见包围盒（含位移）不越过卡体外沿（卡体外沿=边框外沿），
  // 否则主体边缘会从边框旁露出穿帮；边框越窄余量越小，位移自动收窄
  function clampParallax(rawX, rawY) {
    if (parallaxDist < 0) return { x: rawX, y: rawY };        // 无边框：保留原视差漂浮
    if (parallaxDist === 0 || !subjectBox) return { x: 0, y: 0 }; // 中心在边框外或无主体：关闭视差
    var b = subjectBox;
    var hw = CARD_WIDTH / 2, hh = CARD_HEIGHT / 2;
    return {
      x: Math.min(Math.max(rawX, b.halfW - b.offX - hw), hw - b.offX - b.halfW),
      y: Math.min(Math.max(rawY, b.halfH - b.offY - hh), hh - b.offY - b.halfH)
    };
  }

  function updateHover(px, py) {
    const rect = canvas.getBoundingClientRect();
    ndc.set(((px - rect.left) / rect.width) * 2 - 1, -((py - rect.top) / rect.height) * 2 + 1);
    raycaster.setFromCamera(ndc, camera);
    const hits = raycaster.intersectObjects(hitMeshes);
    if (hits.length) {
      pointer.copy(hits[0].uv);
      hover.current = 1;
      hoverTarget.set((hits[0].uv.x - 0.5) * 0.4, -(hits[0].uv.y - 0.5) * 0.34);
    } else {
      hover.current = 0;
      hoverTarget.set(0, 0);
    }
  }

  // ---- 相机响应式（移动端用 visualViewport，避免地址栏伸缩触发缩放重置） ----
  let wasNarrow = false;
  function resize() {
    const vv = window.visualViewport;
    const w = vv ? vv.width : window.innerWidth;
    const h = vv ? vv.height : window.innerHeight;
    renderer.setSize(w, h);
    camera.aspect = w / h;
    const narrow = w / h < 0.72;
    camera.position.set(0, 0, narrow ? 6.8 : 5.1);
    camera.fov = narrow ? 40 : 38;
    camera.updateProjectionMatrix();
    if (narrow && !wasNarrow) state.targetRadius = 6.8;
    wasNarrow = narrow;
  }
  window.addEventListener('resize', resize);
  if (window.visualViewport) window.visualViewport.addEventListener('resize', resize);
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

    // 视差安全限制：主体可见包围盒（含位移）不越过边框内沿，窄边框自动收窄位移；
    // 主体偏移（用户手动调位）叠加进视差目标——仅偏移图像位置，蒙版随 syncWorld 钉在卡片坐标
    if (!subjectBox) subjectBox = computeSubjectBox();
    const par = clampParallax(hoverCurrent.x * 0.2 + __SUBJ_OFFSET_X__ * CARD_WIDTH,
                              -hoverCurrent.y * 0.14 + __SUBJ_OFFSET_Y__ * CARD_HEIGHT);
    foregroundGroup.position.x = damp(foregroundGroup.position.x, par.x, 9.5, dt);
    foregroundGroup.position.y = damp(foregroundGroup.position.y, par.y, 9.5, dt);
    foregroundGroup.position.z = damp(foregroundGroup.position.z, 0.024, 9.5, dt);
    foregroundGroup.rotation.x = hoverCurrent.y * 0.18;
    foregroundGroup.rotation.y = hoverCurrent.x * 0.18;
    // 蒙版裁剪边界固定于卡片坐标系：随视差组位移反算 alphaMap 矩阵，主体永远不超出边框外沿
    if (window.__syncFgMask) window.__syncFgMask();

    cardMaterial.uniforms.uHover.value = damp(cardMaterial.uniforms.uHover.value, hover.current, 11, dt);
    cardMaterial.uniforms.uPointer.value.copy(pointer);
__SEAL_TICK__
__GLOW_TICK__

    __fx.tick(dt);
    // 特效属于正面装饰：翻到背面（绕 Y 转过 90°）时隐藏，避免叠在卡背上
    effectAnchor.visible = Math.abs(flipGroup.rotation.y) < Math.PI * 0.5;

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
    // 调试用：把流动时间钉到指定秒数，便于逐帧比对（不影响正常播放）
    setFlowTime: function (t) {
      var fo = window.__foilObj;
      if (fo && fo.uniforms && fo.uniforms.uTime) { fo.uniforms.uTime.value = t; fo.t = t; }
      if (window.__foilSubj && window.__foilSubj.uniforms) window.__foilSubj.uniforms.uTime.value = t;
      if (cardMaterial && cardMaterial.uniforms && cardMaterial.uniforms.uFlowTime)
        cardMaterial.uniforms.uFlowTime.value = t;
      return t;
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
  var CARD_WIDTH = __CARD_WIDTH__;
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


# 卡面 contain 到边框内沿（中空区域）：非浮起+边框+卡面素材时，主 shader 按 uFaceBox
# 把卡面等比缩放居中放进中空窗口，整图完整可见不超框（2D compositor 同步实现）。
# 中空 = 边框内区域蒙版（白=边框环+中空）∩ 边框图透明区（镂空），两者叠加后取白区 bbox。
_FACE_CONTAIN_JS = r"""
    var frameImg = null;
    (function pollFrame() {
      if (t.image && t.image.width) frameImg = t.image;
      else setTimeout(pollFrame, 30);
    })();
    function setFaceBox(f) {
      if (!interiorMaskImg || !frameImg || !frameImg.width) { setTimeout(function () { setFaceBox(f); }, 30); return; }
      var cv = document.createElement('canvas');
      cv.width = 450; cv.height = 600;
      var ctx = cv.getContext('2d');
      var rx = (f.cx - f.fw / 2) * 450, ry = (f.cy - f.fh / 2) * 600, rw = f.fw * 450, rh = f.fh * 600;
      ctx.clearRect(0, 0, 450, 600);
      ctx.drawImage(interiorMaskImg, rx, ry, rw, rh);
      var di = ctx.getImageData(0, 0, 450, 600).data;
      ctx.clearRect(0, 0, 450, 600);
      ctx.drawImage(frameImg, rx, ry, rw, rh);
      var df = ctx.getImageData(0, 0, 450, 600).data;
      var x0 = 450, y0 = 600, x1 = -1, y1 = -1;
      for (var y = 0; y < 600; y++) {
        for (var x = 0; x < 450; x++) {
          var i = (y * 450 + x) * 4;
          if (di[i + 3] > 128 && df[i + 3] <= 8) {
            if (x < x0) x0 = x;
            if (x > x1) x1 = x;
            if (y < y0) y0 = y;
            if (y > y1) y1 = y;
          }
        }
      }
      if (x1 < 0) return;
      // canvas y 自上而下，纹理 UV y 自下而上（CanvasTexture 默认 flipY）：翻转 y 后再写入
      cardMaterial.uniforms.uFaceBox.value.set(x0 / 450, 1.0 - (y1 + 1) / 600, (x1 + 1) / 450, 1.0 - y0 / 600);
      var fi = frontTexture.image;
      cardMaterial.uniforms.uFaceAspect.value = (fi && fi.width) ? (fi.width / fi.height) : 1.0;
      cardMaterial.uniforms.uFaceContain.value = 1.0;
    }
    function setFaceBoxRetry(f) {
      if (cardMaterial) setFaceBox(f);
      else setTimeout(function () { setFaceBoxRetry(f); }, 30);
    }
"""


def _frame_layer_js(rel: str | None, z: float, order: int, fit_subject: bool = False,
                    adapt_limit: float | None = None, interior_rel: str | None = None,
                    contain: bool = False) -> str:
    """边框叠加层 JS。fit_subject 时按前景主体包围盒缩放边框（否则整卡），仅正面渲染。

    主体包围盒在浏览器端从前景纹理 alpha 计算（无需服务端 numpy），
    边框缩放到包围盒 + 边距并居中，保证未开启浮于边框时边框与主体等高或略高。
    adapt_limit: 文本型主体自适应缩放比例（<1 时边框随主体同步缩小上移）。
    interior_rel: 边框内区域蒙版图（白=边框环+中空，黑=边框外围），按边框实际绘制
    形态缩放到全卡画布，作为整卡内容的裁剪蒙版（卡面/主体/层1卡封不超出边框外围）。
    contain: 非浮起+边框+卡面素材时，按「边框内沿（中空区域）」bbox 计算卡面窗口
    （uFaceBox/uFaceAspect），主 shader 把卡面 contain 进窗口内完整可见不超框。
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
            "    var adaptLimit = " + ("null" if adapt_limit is None else json.dumps(adapt_limit)) + ";\n"
            "    var lastFit = null;\n"
            "    var interiorMaskImg = null;\n"
            + ("    loader.load(" + json.dumps(interior_rel) + ", function (it) { interiorMaskImg = it.image; if (lastFit) drawInterior(lastFit); });\n" if interior_rel else "")
            + ("\n" + _FACE_CONTAIN_JS if contain else "")
            + "    function drawInterior(f) {\n"
            "      if (!interiorMaskImg || !interiorMaskImg.width) return;\n"
            "      interiorCtx.clearRect(0, 0, 450, 600);\n"
            "      interiorCtx.drawImage(interiorMaskImg, (f.cx - f.fw / 2) * 450, (f.cy - f.fh / 2) * 600, f.fw * 450, f.fh * 600);\n"
            "      interiorTexture.needsUpdate = true;\n"
            "      updateInteriorSdf();\n"
            + ("      setFaceBoxRetry(f);\n" if contain else "")
            + "    }\n"
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
            "      if (!img || !img.width) { if (++tries > 200) { var dl = adaptLimit || 1; lastFit = { fw: dl, fh: dl, cx: 0.5, cy: 0.5 * dl }; addFrame(dl, dl, 0.5, 0.5 * dl); drawInterior(lastFit); return; } setTimeout(fit, 30); return; }\n"
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
            "      if (x1 < 0) { var dl = adaptLimit || 1; lastFit = { fw: dl, fh: dl, cx: 0.5, cy: 0.5 * dl }; addFrame(dl, dl, 0.5, 0.5 * dl); drawInterior(lastFit); return; }\n"
            "      var fw = Math.min((x1 - x0 + 1) / sw + 2 * pad, 1.0);\n"
            "      var fh = Math.min((y1 - y0 + 1) / sh + 2 * pad, 1.0);\n"
            "      var cx = Math.min(Math.max(((x0 + x1 + 1) / 2) / sw, fw / 2), 1 - fw / 2);\n"
            "      var cy = Math.min(Math.max(((y0 + y1 + 1) / 2) / sh, fh / 2), 1 - fh / 2);\n"
            "      if (adaptLimit) {\n"
            "        fw = Math.min(fw * adaptLimit, 1);\n"
            "        fh = Math.min(fh * adaptLimit, 1);\n"
            "        cx = Math.min(Math.max(cx, fw / 2), 1 - fw / 2);\n"
            "        cy = cy * adaptLimit;\n"
            "      }\n"
            "      lastFit = { fw: fw, fh: fh, cx: cx, cy: cy };\n"
            "      addFrame(fw, fh, cx, cy);\n"
            "      drawInterior(lastFit);\n"
            "    }\n"
            "    fit();\n"
            "  })();\n"
        )
    overlay = _overlay_layer_js(rel, z, order, "THREE.FrontSide")
    if interior_rel:
        overlay += (
            "  (function () {\n"
            "    loader.load(" + json.dumps(interior_rel) + ", function (t) {\n"
            "      interiorCtx.clearRect(0, 0, 450, 600);\n"
            "      interiorCtx.drawImage(t.image, 0, 0, 450, 600);\n"
            "      interiorTexture.needsUpdate = true;\n"
            "      updateInteriorSdf();\n"
            "    });\n"
            "  })();\n"
        )
    return overlay


# ---- 冷裱膜动态光效（内置膜名 → 光效参数） ----
# mode: sparkle=图案点阵闪烁 / beam=定向光带 / rainbow=随角变色 / glow=柔光晕 / matte=哑光雾面 / none=无
# pattern: canvas 图样（dot/sparkle/cross/diamond/star5/heart/snowflake/sakura/butterfly/pinwheel）
# jitter/randScale: sparkle 不规则分布（每格随机偏移 + 随机大小，整体仍均匀铺满）
SEAL_EFFECTS = {
    "亚光膜": {"mode": "matte", "color": (255, 255, 250), "strength": 0.36, "grain": 7.0},
    "磨砂膜": {"mode": "glow", "color": (214, 222, 232), "strength": 0.22, "grain": 7.0, "speed": 0.8, "rainbow": 0.3},
    "米字膜2": {"mode": "sparkle", "pattern": "sparkle", "color": (255, 240, 200), "scale": 9.0, "strength": 0.72, "speed": 1.3, "rainbow": 0.65, "twinkle": 1.0, "twinkleShape": 1.0},
    "菱形": {"mode": "sparkle", "pattern": "diamond", "color": (255, 250, 232), "scale": 8.0, "strength": 0.68, "speed": 1.2, "rainbow": 0.65, "twinkle": 1.0, "twinkleShape": 1.0},
    "十字膜": {"mode": "sparkle", "pattern": "cross", "color": (255, 248, 226), "scale": 10.0, "strength": 0.68, "speed": 1.3, "rainbow": 0.6, "twinkle": 1.0, "twinkleShape": 1.0},
    "玻璃膜": {"mode": "beam", "dir": (0.8, 0.6), "color": (255, 255, 255), "scale": 4.0, "power": 2.0, "strength": 0.7, "speed": 0.7, "rainbow": 0.6},
    "彩虹膜": {"mode": "rainbow", "strength": 0.9, "power": 2.5, "dir": (0.87, 0.5)},
    "星空膜": {"mode": "sparkle", "pattern": "dot", "color": (196, 210, 255), "scale": 20.0, "strength": 0.8, "speed": 0.5, "rainbow": 0.55, "twinkle": 1.0, "twinkleShape": 1.0, "jitter": 0.32, "randScale": 0.65},
    "米字膜1": {"mode": "sparkle", "pattern": "sparkle", "color": (255, 182, 162), "scale": 6.0, "strength": 0.75, "speed": 1.5, "rainbow": 0.6, "twinkle": 1.0, "twinkleShape": 1.0},
    "爱心膜": {"mode": "sparkle", "pattern": "heart", "color": (255, 170, 186), "scale": 9.0, "strength": 0.7, "speed": 1.2, "rainbow": 0.65, "twinkle": 1.0, "twinkleShape": 1.0},
    "小星星膜": {"mode": "sparkle", "pattern": "star5", "color": (255, 236, 172), "scale": 12.0, "strength": 0.72, "speed": 1.1, "rainbow": 0.6, "twinkle": 1.0, "twinkleShape": 1.0},
    "星星膜": {"mode": "sparkle", "pattern": "star5", "color": (255, 230, 152), "scale": 6.0, "strength": 0.72, "speed": 1.0, "rainbow": 0.6, "twinkle": 1.0, "twinkleShape": 1.0},
    "雪花膜": {"mode": "sparkle", "pattern": "snowflake", "color": (226, 240, 255), "scale": 8.0, "strength": 0.75, "speed": 1.0, "rainbow": 0.5, "twinkle": 1.0, "twinkleShape": 1.0, "jitter": 0.24, "randScale": 0.38},
    "樱花膜": {"mode": "sparkle", "pattern": "sakura", "color": (255, 190, 206), "scale": 10.0, "strength": 0.72, "speed": 1.1, "rainbow": 0.6, "twinkle": 1.0, "twinkleShape": 1.0},
    "星光细闪膜": {"mode": "sparkle", "pattern": "dot", "color": (255, 250, 235), "scale": 7.0, "strength": 0.62, "speed": 1.0, "rainbow": 0.6, "twinkle": 1.0},
    "测试膜": {"mode": "sparkle", "pattern": "dot", "color": (255, 250, 235), "scale": 7.0, "strength": 0.62, "speed": 1.0, "rainbow": 0.6, "twinkle": 1.0, "twinkleShape": 1.0},
    "流麻膜": {"mode": "sparkle", "pattern": "dot", "color": (255, 250, 235), "scale": 14.0, "strength": 0.6, "speed": 1.2, "rainbow": 0.55, "twinkleFlow": 1.0, "twinkleOnly": 1.0},
    "点状膜1": {"mode": "sparkle", "pattern": "dot", "color": (255, 222, 182), "scale": 7.0, "strength": 0.75, "speed": 1.1, "rainbow": 0.6, "twinkle": 1.0, "twinkleShape": 1.0},
    "点状膜2": {"mode": "sparkle", "pattern": "dot", "color": (255, 246, 226), "scale": 11.0, "strength": 0.7, "speed": 1.0, "rainbow": 0.5, "twinkle": 1.0, "twinkleShape": 1.0},
    "风车膜": {"mode": "sparkle", "pattern": "pinwheel", "color": (255, 212, 152), "scale": 8.0, "strength": 0.72, "speed": 1.2, "rainbow": 0.6, "twinkle": 1.0, "twinkleShape": 1.0, "jitter": 0.26, "randScale": 0.45},
    # A 类：真密铺（格子形状即图案，图案互相重叠、无方块感，整格闪光，学星光细闪膜）
    "A33六角格膜": {"mode": "grid", "gridType": "hex", "pattern": "hexagon", "color": (255, 248, 226), "scale": 5.0, "strength": 0.7, "speed": 1.0, "rainbow": 0.6, "twinkle": 1.0},
    "A44碎玻璃膜": {"mode": "voronoi", "color": (255, 250, 238), "scale": 7.0, "strength": 0.72, "speed": 1.0, "rainbow": 0.75},
    # B 类：格子平铺后格子内再画图形（仅图形闪光，学测试膜）
    "B11圆环膜": {"mode": "sparkle", "pattern": "ring", "color": (255, 250, 235), "scale": 7.0, "strength": 0.7, "speed": 1.1, "rainbow": 0.6, "twinkle": 1.0, "twinkleShape": 1.0},
    "B22闪电膜": {"mode": "sparkle", "pattern": "lightning", "color": (255, 244, 210), "scale": 7.0, "strength": 0.72, "speed": 1.3, "rainbow": 0.6, "twinkle": 1.0, "twinkleShape": 1.0},
    "B33齿轮膜": {"mode": "sparkle", "pattern": "gear", "color": (255, 246, 226), "scale": 6.0, "strength": 0.72, "speed": 1.2, "rainbow": 0.6, "twinkle": 1.0, "twinkleShape": 1.0},
    "纹路1": {"mode": "sparkle", "pattern": "circuit", "color": (255, 244, 220), "scale": 8.0, "strength": 0.72, "speed": 1.2, "rainbow": 0.65, "twinkle": 1.0, "twinkleShape": 1.0},
}

# ---- 辉光特效预设（沿卡体/边框边缘发光） ----
# mode: 0=纵向渐变（默认） / 1=四角聚焦 / 2=上下双色 / 3=菲涅尔描边（视角驱动） /
#       4=双环辉光 / 5=RGB循环变色 / 6=霓虹灯交替 / 7=发廊螺纹旋转
# mode>=5 的效果使用时间动画（uTime），为动画类辉光；其余仅静态或视角驱动（无频闪）
GLOW_EFFECTS = {
    "暖金描边": {"colorTop": (0.98, 0.63, 0.18), "colorBottom": (0.44, 0.08, 0.025),
                "tight": 0.014, "soft": 0.045, "mode": 0, "fresnel": 0.0, "speed": 0.0},
    "冰蓝冷光": {"colorTop": (0.45, 0.8, 1.0), "colorBottom": (0.05, 0.25, 0.55),
                "tight": 0.008, "soft": 0.07, "mode": 0, "fresnel": 0.2, "speed": 0.0},
    "紫罗兰夜光": {"colorTop": (0.78, 0.42, 1.0), "colorBottom": (0.28, 0.05, 0.52),
                  "tight": 0.01, "soft": 0.06, "mode": 0, "fresnel": 0.15, "speed": 0.0},
    "四角聚焦": {"colorTop": (0.98, 0.63, 0.18), "colorBottom": (0.44, 0.08, 0.025),
                "tight": 0.012, "soft": 0.05, "mode": 1, "fresnel": 0.0, "speed": 0.0},
    "上下双色": {"colorTop": (0.3, 0.78, 1.0), "colorBottom": (1.0, 0.55, 0.2),
                "tight": 0.012, "soft": 0.05, "mode": 2, "fresnel": 0.0, "speed": 0.0},
    "菲涅尔描边": {"colorTop": (0.9, 0.86, 0.65), "colorBottom": (0.5, 0.28, 0.14),
                  "tight": 0.012, "soft": 0.05, "mode": 3, "fresnel": 1.0, "speed": 0.0},
    "RGB变色灯光": {"colorTop": (1.0, 1.0, 1.0), "colorBottom": (1.0, 1.0, 1.0),
                   "tight": 0.01, "soft": 0.06, "mode": 5, "fresnel": 0.0, "speed": 0.6},
    "霓虹灯": {"colorTop": (1.0, 0.32, 0.38), "colorBottom": (0.35, 0.8, 1.0),
              "tight": 0.008, "soft": 0.05, "mode": 6, "fresnel": 0.0, "speed": 1.1},
    "发廊螺纹": {"colorTop": (1.0, 0.25, 0.5), "colorBottom": (0.25, 0.6, 1.0),
                "tight": 0.014, "soft": 0.045, "mode": 7, "fresnel": 0.0, "speed": 0.9},
    "樱花粉": {"colorTop": (1.0, 0.78, 0.88), "colorBottom": (0.72, 0.3, 0.5),
               "tight": 0.012, "soft": 0.06, "mode": 0, "fresnel": 0.2, "speed": 0.0},
}

# 自定义辉光特效 JSON（名称.json → {"def": {...}}，与 GLOW_EFFECTS 预设同构）
GLOW_CUSTOM_DIR = PROJECT_ROOT / "assets" / "effects-glow"


def list_custom_glows() -> list[str]:
    """返回已保存的自定义辉光特效名称。"""
    if not GLOW_CUSTOM_DIR.exists():
        return []
    return sorted(p.stem for p in GLOW_CUSTOM_DIR.glob("*.json"))


def resolve_glow_def(name: str):
    """取辉光特效定义：内置预设优先，其次自定义 JSON。找不到返回 None。"""
    if name in GLOW_EFFECTS:
        return GLOW_EFFECTS[name]
    f = GLOW_CUSTOM_DIR / f"{name}.json"
    if f.exists():
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            defn = data.get("def") if isinstance(data, dict) else None
            if isinstance(defn, dict):
                return defn
        except (OSError, ValueError):
            pass
    return None

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


def _seal_layer_js(effect: dict, front_strength: float = 0.945, back_strength: float = 0.5775,
                   parent: str = "flipGroup", z: float = 0.02, order: float = 5.2,
                   mask_rel: str | None = None, front_only: bool = False,
                   mask_canvas: str | None = None, mask_clip: bool = True) -> str:
    """冷裱膜动态光效层 JS：独立 ShaderMaterial，光线随悬停聚焦、随视角与时间流动。

    front_only: True 时仅正面渲染（有边框时层1卡封不再出现在卡背，卡背统一用边框卡封）。
    mask_rel: 静态蒙版图路径（如边框 PNG 的 alpha 作层2 卡封裁剪）。
    mask_canvas: 共享动态蒙版画布纹理的 JS 变量名（如 interiorTexture，层1 卡封按
    边框内区域裁剪，随边框实际绘制形态由 drawInterior 更新），与 mask_rel 二选一。
    mask_clip: 层1 卡封（mask_canvas 为边框内蒙版）是否随「裁剪到边框内」开关裁剪；
    层2 边框卡封（mask_rel 为边框 alpha）恒裁剪在边框上，不受此参数影响。
    """
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
    # 不规则分布（雪花/风车/星空等）：每格随机偏移 + 随机大小，打破规则网格（整体仍均匀铺满）
    jitter = float(effect.get("jitter", 0.0))
    rand_scale = float(effect.get("randScale", 0.0))
    irregular = jitter > 0.0 or rand_scale > 0.0

    tex_code = ""
    uniform_decl = ""
    if mode == "sparkle" and pat:
        tex_code = (
            "    var sealPatternCanvas = sealPattern(" + json.dumps(pat) + ");\n"
            "    var sealPatternTex = new THREE.CanvasTexture(sealPatternCanvas);\n"
            "    sealPatternTex.magFilter = THREE.LinearFilter;\n"
            "    sealPatternTex.minFilter = THREE.LinearMipmapLinearFilter;\n"
            "    sealPatternTex.colorSpace = THREE.NoColorSpace;\n"
        )
        uniform_decl = "    uPattern: { value: sealPatternTex },\n"
        if irregular:
            uniform_decl += "    uJitter: { value: " + str(jitter) + " },\n"
            uniform_decl += "    uRandScale: { value: " + str(rand_scale) + " },\n"

    # 各模式光效：共享「薄膜基面 sheen + 边缘菲涅尔 edge + 随视角扫动的光带 bandLight」，
    # 无时间动画、无指针光球（杜绝频闪），光效由拖动旋转卡片触发
    if mode == "sparkle":
        # 稀疏大图案（爱心/蝴蝶/樱花）隔行错开半格，打破严格网格的“格子感”
        brick = bool(effect.get("brick")) or pat in ("heart", "butterfly", "sakura")
        tile_code = (
            "    vec2 tile = vUv * uScale;\n"
            + ("    tile.x += 0.5 * mod(floor(tile.y), 2.0);\n" if brick else "")
            + ("    // 不规则：每格按 cell hash 随机偏移与缩放（大小不一、分布不均，整体仍均匀铺满）\n"
               "    vec2 cellId = floor(tile);\n"
               "    vec2 jh = sealHash2(cellId);\n"
               "    vec2 puv = (fract(tile + (jh - 0.5) * uJitter) - 0.5) * (1.0 + (jh.x * 2.0 - 1.0) * uRandScale) + 0.5;\n"
               "    float pat = texture2D(uPattern, puv).a;\n" if irregular else
               "    float pat = texture2D(uPattern, fract(tile)).a;\n")
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
            + "    glow = 1.0 - exp(-1.25 * max(glow, 0.0));\n"
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
            + "    glow = 1.0 - exp(-1.25 * max(glow, 0.0));\n"
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
            "    glow = 1.0 - exp(-1.25 * max(glow, 0.0));\n"
            "    vec3 effColor = mix(uColor, sealHueColor(uColor, uRainbow * (along * 0.7 + h)), min(1.0, uRainbow * 1.2));\n"
            "    gl_FragColor = vec4(effColor * glow * uStrength, 1.0);\n"
        )
    elif mode == "beam":
        frag = (
            "    vec2 perp = vec2(-brush.y, brush.x);\n"
            "    float lobe = pow(max(0.0, abs(dot(refl2, perp))), uPower);\n"
            "    float stripe = pow(max(0.0, 0.5 + 0.5 * sin(dot(vUv, perp) * uScale * 4.0 + along * 4.0)), 3.5);\n"
            "    float glow = sheen + edge * 0.9 + bandLight * (0.45 + 0.55 * stripe) + lobe * bandLight * 0.4;\n"
            "    glow = 1.0 - exp(-1.25 * max(glow, 0.0));\n"
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
            "    glow = 1.0 - exp(-1.25 * max(glow, 0.0));\n"
            "    gl_FragColor = vec4(col * glow * uStrength, 1.0);\n"
        )
    elif mode == "matte":
        # 亚光：压掉高光反射，柔和漫反射雾面（随视角轻微变化，无时间动画）
        frag = (
            "    float matteN = 0.5 + 0.5 * sin(vUv.x * 80.0 * uGrain + vUv.y * 97.0 * uGrain + along * 1.8);\n"
            "    float glow = 0.10 + 0.20 * facing + matteN * 0.07 + fres * 0.08;\n"
            "    glow = 1.0 - exp(-1.25 * max(glow, 0.0));\n"
            "    gl_FragColor = vec4(uColor * glow * uStrength, 1.0);\n"
        )
    else:  # glow
        frag = (
            "    float n = 0.0;\n"
            "    n += sin(vUv.x * 90.0 * uGrain + along * 2.0) * 0.5;\n"
            "    n += sin(vUv.y * 110.0 * uGrain + across * 2.0) * 0.5;\n"
            "    n += sin((vUv.x + vUv.y) * 150.0 * uGrain) * 0.5;\n"
            "    n = n / 3.0 + 0.5;\n"
            "    float glow = sheen + edge * 0.9 + bandLight * (0.4 + 0.35 * n);\n"
            "    glow = 1.0 - exp(-1.25 * max(glow, 0.0));\n"
            "    vec3 effColor = mix(uColor, sealHueColor(uColor, uRainbow * (along * 0.6 + vUv.x * 0.5 + vUv.y * 0.4)), min(1.0, uRainbow * 1.2));\n"
            "    gl_FragColor = vec4(effColor * glow * uStrength, 1.0);\n"
        )

    # 卡封蒙版：frag 追加 alpha 裁剪（须在 prefix 拼接前定义，否则不生效）
    # 正面按当前蒙版源裁剪（层2=边框纹理 alpha，层1=边框内区域蒙版）；
    # 卡背同样按边框内区域蒙版限制（与卡背主画面一致，不再整卡容器铺满）；蒙版统一按物理坐标 vUv 采样（不镜像）。
    # uMaskClip 控制「边框内蒙版」类裁剪（层1 卡封跟随开关；层2 边框卡封恒为 1.0，边框 alpha 蒙版始终生效）
    mask_frag = ""
    if mask_rel or mask_canvas:
        mask_frag = ("  if (gl_FrontFacing) {\n"
                     "    gl_FragColor.a *= mix(1.0, texture2D(uFrameMap, vUv).a, uMaskClip);\n"
                     "  } else {\n"
                     "    gl_FragColor.a *= mix(1.0, texture2D(uInteriorMap, vUv).a, uMaskClip);\n"
                     "  }\n")

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
        + ("uniform float uJitter;\nuniform float uRandScale;\n" if irregular else "")
        + ("uniform sampler2D uPattern;\n" if mode == "sparkle" else "")
        + ("vec2 sealHash2(vec2 p) {\n  return fract(sin(vec2(dot(p, vec2(127.1, 311.7)), dot(p, vec2(269.5, 183.3)))) * 43758.5453123);\n}\n" if (mode == "voronoi" or irregular) else "")
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
        + frag + mask_frag
        + "  // 卡背一面减弱卡封效果（翻面后 gl_FrontFacing 为 false），强度由界面可调\n"
        + "  gl_FragColor *= gl_FrontFacing ? uFrontStrength : uBackStrength;\n"
        + "  }\n"
    )

    # 层2 边框蒙版：加载边框纹理，alpha 作蒙版（卡封只出现在边框非透明区域）
    mask_js = ""
    if mask_canvas:
        # 共享动态蒙版画布（层1 卡封按边框内区域裁剪，随边框实际绘制形态更新）
        mask_js = "    var sealFrameTex = " + mask_canvas + ";\n"
        uniform_decl += "    uFrameMap: { value: sealFrameTex },\n"
        prefix = prefix.replace("void main() {", "uniform sampler2D uFrameMap;\nvoid main() {", 1)
    elif mask_rel:
        mask_js = (
            "    var sealFrameTex = loader.load(" + json.dumps(mask_rel) + ");\n"
            "    sealFrameTex.colorSpace = THREE.SRGBColorSpace;\n"
        )
        uniform_decl += "    uFrameMap: { value: sealFrameTex },\n"
        prefix = prefix.replace("void main() {", "uniform sampler2D uFrameMap;\nvoid main() {", 1)
    if mask_rel or mask_canvas:
        # 卡背蒙版：边框内区域蒙版（与卡背主画面一致），整卡脚本内定义的 interiorTexture
        uniform_decl += "    uInteriorMap: { value: interiorTexture },\n"
        uniform_decl += "    uMaskClip: { value: " + ("1.0" if mask_clip else "0.0") + " },\n"
        prefix = prefix.replace("void main() {", "uniform sampler2D uInteriorMap;\nuniform float uMaskClip;\nvoid main() {", 1)

    return (
        _SEAL_PATTERN_JS
        + "  (function () {\n"
        + tex_code
        + mask_js
        + "    var sealMat = new THREE.ShaderMaterial({\n"
        "      transparent: true,\n"
        "      depthWrite: false,\n"
        + ("      side: THREE.FrontSide,\n" if front_only else "      side: THREE.DoubleSide,\n")
        + "      blending: THREE.AdditiveBlending,\n"
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
        "    sealMesh.position.set(0, 0, " + str(z) + ");\n"
        "    sealMesh.renderOrder = " + str(order) + ";\n"
        "    " + parent + ".add(sealMesh);\n"
        "    window.__sealMat = sealMat;\n"
        "  })();\n"
    )


def _flow_layer_js(rel: str | None, strength: float = 0.62, speed: float = 1.0,
                   warp: float = 1.0, z: float = 0.013, order: float = 5.05,
                   parent: str = "flipGroup", drift: float = 1.0) -> str:
    """卡面「流动背景」层：只有印刷背景在缓慢流动，立绘/文字/图标不动。

    贴图是 `flow_bg.make_plate()` 生成的整卡尺寸 RGBA：
        RGB = 只有印刷背景（底色 + 元素烟雾 + 版本图案 + 纸纹，无立绘/UI）
        A   = 该像素**属于背景**的掩码（立绘/UI 处为 0）

    采样方式决定了「静止时像素级不变」：
        RGB 按**漂移后**的 UV 采样 → 内容在动；
        A   按**原位** UV 采样     → 覆盖范围固定，不会绕着立绘漏色。
    不漂移时覆盖层 = 底层逐像素相同，正常混合后画面完全不变（不会糊一层雾）。
    慢速多正弦（周期 20~70s）+ 轻微缩放呼吸 + 涡流扰动 = 纸面下的雾在走。
    """
    if not rel:
        return ""
    vertex = (
        "varying vec2 vUv;\n"
        "void main() {\n"
        "  vUv = uv;\n"
        "  gl_Position = projectionMatrix * modelViewMatrix * vec4(position, 1.0);\n"
        "}\n"
    )
    frag = (
        "uniform sampler2D uMap;\n"
        "uniform float uTime, uStrength, uSpeed, uWarp, uAspect, uDrift;\n"
        "varying vec2 vUv;\n"
        "float flowRound(vec2 uv, float radius) {\n"
        "  vec2 scaled = vec2((uv.x - 0.5) * uAspect, uv.y - 0.5);\n"
        "  vec2 halfSize = vec2(0.5 * uAspect, 0.5);\n"
        "  vec2 edge = abs(scaled) - (halfSize - vec2(radius));\n"
        "  return length(max(edge, 0.0)) + min(max(edge.x, edge.y), 0.0) - radius;\n"
        "}\n"
        "void main() {\n"
        "  if (flowRound(vUv, 0.032) > 0.0) discard;\n"
        "  float t = uTime * uSpeed;\n"
        "  vec2 p = vUv - 0.5;\n"
        "  vec2 off = vec2(\n"
        "    sin(t * 0.131) * 0.0165 + sin(t * 0.057 + 1.7) * 0.0085,\n"
        "    cos(t * 0.113) * 0.0135 + sin(t * 0.041 + 0.4) * 0.0070) * uDrift;\n"
        "  vec2 wob = vec2(sin(vUv.y * 4.2 + t * 0.29),\n"
        "                  cos(vUv.x * 3.6 - t * 0.24)) * (0.0035 * uWarp * uDrift);\n"
        "  float sc = 1.0 + 0.008 * sin(t * 0.087);\n"
        "  vec2 uv = p * sc + 0.5 + off + wob;\n"
        "  vec3 c = texture2D(uMap, uv).rgb;\n"
        "  float m = texture2D(uMap, vUv).a;\n"
        "  gl_FragColor = vec4(c, m * uStrength);\n"
        "}\n"
    )
    return (
        "  (function () {\n"
        "    var t = loader.load(" + json.dumps(rel) + ");\n"
        "    t.colorSpace = THREE.NoColorSpace;\n"
        "    t.anisotropy = 8;\n"
        "    t.wrapS = THREE.MirroredRepeatWrapping;\n"
        "    t.wrapT = THREE.MirroredRepeatWrapping;\n"
        "    var flowMat = new THREE.ShaderMaterial({\n"
        "      transparent: true,\n"
        "      depthWrite: false,\n"
        "      side: THREE.FrontSide,\n"
        "      toneMapped: false,\n"
        "      uniforms: {\n"
        "        uMap: { value: t },\n"
        "        uTime: { value: 0 },\n"
        "        uStrength: { value: " + str(strength) + " },\n"
        "        uSpeed: { value: " + str(speed) + " },\n"
        "        uWarp: { value: " + str(warp) + " },\n"
        "        uAspect: { value: CARD_WIDTH / CARD_HEIGHT },\n"
        "        uDrift: { value: " + str(drift) + " },\n"
        "      },\n"
        "      vertexShader: " + json.dumps(vertex) + ",\n"
        "      fragmentShader: " + json.dumps(frag) + ",\n"
        "    });\n"
        "    var flowMesh = new THREE.Mesh(new THREE.PlaneGeometry(CARD_WIDTH, CARD_HEIGHT), flowMat);\n"
        "    flowMesh.position.set(0, 0, " + str(z) + ");\n"
        "    flowMesh.renderOrder = " + str(order) + ";\n"
        "    " + parent + ".add(flowMesh);\n"
        "    (window.__flowMats = window.__flowMats || []).push(flowMat);\n"
        "  })();\n"
    )


def _custom_seal_layer_js(rel: str | None, front_strength: float = 0.945, back_strength: float = 0.5775,
                          parent: str = "flipGroup", z: float = 0.02, order: float = 5.2,
                          mask_rel: str | None = None, front_only: bool = False,
                          mask_canvas: str | None = None, mask_clip: bool = True,
                          base: float = 0.55, amp: float = 0.45,
                          tilt_gain: float = 0.045, blend_add: bool = False,
                          tilt_lo: float = 0.12, tilt_hi: float = 0.6,
                          sheen_base: float = 0.03, band_power: float = 4.0,
                          rainbow: float = 0.7) -> str:
    """自定义 PNG 卡封动态光效层：整张贴图 + 随视角扫动的箔光带 + 彩虹色相迁移，正反双面显示。

    与内置膜（_seal_layer_js）相同的视角光效架构（sheen 基面 / edge 边缘菲涅尔 /
    bandLight 视角光带 / sealHueColor 彩虹色相迁移），区别：
      - 图案直接整张采样用户 PNG（不平铺、不切割），PNG 的 alpha 作图案遮罩；
      - 图案之外完全透明（不留整卡光迹）；
      - 双面渲染，正面/背面强度由 uFrontStrength/uBackStrength 控制（与内置膜一致）。

    front_only: True 时仅正面渲染（有边框时层1卡封不再出现在卡背，卡背统一用边框卡封）。
    mask_canvas: 共享动态蒙版画布纹理的 JS 变量名（层1 卡封按边框内区域裁剪），与 mask_rel 二选一。
    mask_clip: 层1 卡封（mask_canvas 为边框内蒙版）是否随「裁剪到边框内」开关裁剪；
    层2 边框卡封（mask_rel 为边框 alpha）恒裁剪在边框上，不受此参数影响。
    """
    if not rel:
        return ""
    strength, power = 0.9, 4.0
    rainbow = float(rainbow)
    d0, d1 = 0.87, 0.5
    frag = (
        "    vec4 tex = texture2D(uMap, vUv);\n"
        "    float pat = tex.a;\n"
        "    vec3 base = max(tex.rgb, vec3(0.03));\n"
        # 平滑饱和：min(...,1.0) 的硬截断会在卡面拉出一条随视角扫动的直线
        "    float glowRaw = sheen * (0.5 + 0.5 * pat) + edge * 0.9\n"
        "                     + bandLight * (0.5 + 0.9 * pat);\n"
        "    float glow = 1.0 - exp(-1.25 * max(glowRaw, 0.0));\n"
        "    vec3 effColor = mix(base, sealHueColor(base, uRainbow * (along * 0.7 + vUv.x * 0.5 + vUv.y * 0.35)), min(1.0, uRainbow * 1.2));\n"
        "    vec3 rgb = effColor * (uBase + uAmp * glow) * uStrength;\n"
        "    float a = pat * (gl_FrontFacing ? uFrontStrength : uBackStrength);\n"
        "    gl_FragColor = vec4(rgb, a);\n"
    )
    # 卡封蒙版：frag 追加 alpha 裁剪（须在 prefix 拼接前定义，否则不生效）
    # 正面按当前蒙版源裁剪（层2=边框纹理 alpha，层1=边框内区域蒙版）；
    # 卡背同样按边框内区域蒙版限制（与卡背主画面一致，不再整卡容器铺满）；蒙版统一按物理坐标 vUv 采样（不镜像）。
    # uMaskClip 控制「边框内蒙版」类裁剪（层1 卡封跟随开关；层2 边框卡封恒为 1.0，边框 alpha 蒙版始终生效）
    mask_frag = ""
    if mask_rel or mask_canvas:
        mask_frag = ("  if (gl_FrontFacing) {\n"
                     "    gl_FragColor.a *= mix(1.0, texture2D(uFrameMap, vUv).a, uMaskClip);\n"
                     "  } else {\n"
                     "    gl_FragColor.a *= mix(1.0, texture2D(uInteriorMap, vUv).a, uMaskClip);\n"
                     "  }\n")
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
        "uniform float uBase;\n"
        "uniform float uAmp;\n"
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
        "  float tiltG = smoothstep(" + str(tilt_lo) + ", " + str(tilt_hi) + ", tiltAmt);\n"
        "  float bandLight = pow(max(0.0, along), uPower) * tiltG;\n"
        "  float sheen = " + str(sheen_base) + " + " + str(tilt_gain) + " * tiltG;\n"
        "  float edge = fres * 0.18;\n"
        + frag + mask_frag
        + "  }\n"
    )
    # 层2 边框蒙版：加载边框纹理，alpha 作蒙版（卡封只出现在边框非透明区域）
    mask_js = ""
    if mask_canvas:
        mask_js = "    var sealFrameTex = " + mask_canvas + ";\n"
        prefix = prefix.replace("void main() {", "uniform sampler2D uFrameMap;\nvoid main() {", 1)
    elif mask_rel:
        mask_js = (
            "    var sealFrameTex = loader.load(" + json.dumps(mask_rel) + ");\n"
            "    sealFrameTex.colorSpace = THREE.SRGBColorSpace;\n"
        )
        prefix = prefix.replace("void main() {", "uniform sampler2D uFrameMap;\nvoid main() {", 1)
    if mask_rel or mask_canvas:
        # 卡背蒙版：边框内区域蒙版（与卡背主画面一致），整卡脚本内定义的 interiorTexture
        prefix = prefix.replace("void main() {", "uniform sampler2D uInteriorMap;\nuniform float uMaskClip;\nvoid main() {", 1)

    return (
        "  (function () {\n"
        "    var t = loader.load(" + json.dumps(rel) + ");\n"
        "    t.colorSpace = THREE.NoColorSpace;\n"
        "    t.anisotropy = 8;\n"
        + mask_js
        + "    var sealMat = new THREE.ShaderMaterial({\n"
        "      transparent: true,\n"
        "      depthWrite: false,\n"
        + ("      side: THREE.FrontSide,\n" if front_only else "      side: THREE.DoubleSide,\n")
        + ("      blending: THREE.AdditiveBlending,\n" if blend_add else "")
        + "      toneMapped: false,\n"
        "      uniforms: {\n"
        "        uAspect: { value: CARD_WIDTH / CARD_HEIGHT },\n"
        "        uStrength: { value: " + str(strength) + " },\n"
        "        uPower: { value: " + str(band_power) + " },\n"
        "        uDir: { value: new THREE.Vector2(" + str(d0) + ", " + str(d1) + ") },\n"
        "        uRainbow: { value: " + str(rainbow) + " },\n"
        "        uFrontStrength: { value: " + str(front_strength) + " },\n"
        "        uBackStrength: { value: " + str(back_strength) + " },\n"
        "        uBase: { value: " + str(base) + " },\n"
        "        uAmp: { value: " + str(amp) + " },\n"
        "        uMap: { value: t },\n"
        + ("        uFrameMap: { value: sealFrameTex },\n" if (mask_rel or mask_canvas) else "")
        + ("        uInteriorMap: { value: interiorTexture },\n" if (mask_rel or mask_canvas) else "")
        + ("        uMaskClip: { value: " + ("1.0" if mask_clip else "0.0") + " },\n" if (mask_rel or mask_canvas) else "")
        + "      },\n"
        "      vertexShader: " + json.dumps(vertex) + ",\n"
        "      fragmentShader: " + json.dumps(prefix) + ",\n"
        "    });\n"
        "    var sealMesh = new THREE.Mesh(new THREE.PlaneGeometry(CARD_WIDTH, CARD_HEIGHT), sealMat);\n"
        "    sealMesh.position.set(0, 0, " + str(z) + ");\n"
        "    sealMesh.renderOrder = " + str(order) + ";\n"
        "    " + parent + ".add(sealMesh);\n"
        "    window.__sealMat = sealMat;\n"
        "  })();\n"
    )



def _detail_layer_js(rel: str | None, z: float = 0.030, order: float = 6.0) -> str:
    """主体层：立绘 + 三圆图标 + 边框 + 文字，叠在**所有卡封之上**。

    卡封因此不再需要抠主体轮廓/图标洞（那要逐卡掩码，1800 张不可能维护）。
    用 raw ShaderMaterial + NoColorSpace 直通采样，和卡面贴图（front.png）
    完全同一条色彩路径，颜色与 2D 卡面逐像素一致。
    """
    if not rel:
        return ""
    vert = (
        "varying vec2 vUv;\n"
        "void main() { vUv = uv; gl_Position = projectionMatrix * modelViewMatrix * vec4(position, 1.0); }\n"
    )
    frag = (
        "uniform sampler2D uMap;\n"
        "uniform float uAspect;\n"
        "varying vec2 vUv;\n"
        "float cardRound(vec2 uv, float r) {\n  vec2 sc = vec2((uv.x - 0.5) * uAspect, uv.y - 0.5);\n  vec2 hs = vec2(0.5 * uAspect, 0.5) - vec2(r);\n  vec2 e = abs(sc) - hs;\n  return length(max(e, 0.0)) + min(max(e.x, e.y), 0.0) - r;\n}\n"
        "void main() {\n"
"  if (cardRound(vUv, 0.032) > 0.0) discard;\n"
        "  gl_FragColor = texture2D(uMap, vUv);\n"
        "}\n"
    )
    return (
        "(function () {\n"
        "  var t = new THREE.TextureLoader().load(" + json.dumps(rel) + ");\n"
        "  t.colorSpace = THREE.NoColorSpace;\n"
        "  t.anisotropy = 8;\n"
        "  t.minFilter = THREE.LinearMipmapLinearFilter;\n"
        "  var m = new THREE.ShaderMaterial({\n"
        "    uniforms: { uMap: { value: t }, uAspect: { value: CARD_WIDTH / CARD_HEIGHT } },\n"
        "    vertexShader: " + json.dumps(vert) + ",\n"
        "    fragmentShader: " + json.dumps(frag) + ",\n"
        "    transparent: true, depthWrite: false, depthTest: true,\n"
        "    blending: THREE.NormalBlending\n"
        "  });\n"
        "  var mesh = new THREE.Mesh(new THREE.PlaneGeometry(CARD_WIDTH, CARD_HEIGHT), m);\n"
        "  mesh.position.set(0, 0, " + str(z) + ");\n"
        "  mesh.renderOrder = " + str(order) + ";\n"
        "  flipGroup.add(mesh);\n"
        "  window.__detailMesh = mesh;\n"
        "})();\n"
    )


def _pattern_layer_js(rel: str | None, z: float = 0.014, order: float = 4.6) -> str:
    """版本图案层（闪卡星点 / αβΩ 字样 / Promo 花纹）。

    以前这些图案印在卡面底图里，3D 上又叠了一份卡封 → αβΩ 出现两份。
    现在单独成层：加色混合（RGB 已经是「该加多少光」，黑底合成出来的），
    位置在烟雾之上、主体层之下 —— 立绘照样把它遮住，与游戏一致。
    """
    if not rel:
        return ""
    vert = (
        "varying vec2 vUv;\n"
        "void main() { vUv = uv; gl_Position = projectionMatrix * modelViewMatrix * vec4(position, 1.0); }\n"
    )
    frag = (
        "uniform sampler2D uMap;\n"
        "uniform float uAspect;\n"
        "varying vec2 vUv;\n"
        "float cardRound(vec2 uv, float r) {\n  vec2 sc = vec2((uv.x - 0.5) * uAspect, uv.y - 0.5);\n  vec2 hs = vec2(0.5 * uAspect, 0.5) - vec2(r);\n  vec2 e = abs(sc) - hs;\n  return length(max(e, 0.0)) + min(max(e.x, e.y), 0.0) - r;\n}\n"
        "void main() {\n"
        "  if (cardRound(vUv, 0.032) > 0.0) discard;\n"
        "  vec4 c = texture2D(uMap, vUv); gl_FragColor = vec4(c.rgb, 1.0);\n"
        "}\n"
    )
    return (
        "(function () {\n"
        "  var t = new THREE.TextureLoader().load(" + json.dumps(rel) + ");\n"
        "  t.colorSpace = THREE.NoColorSpace;\n"
        "  t.anisotropy = 8;\n"
        "  t.minFilter = THREE.LinearMipmapLinearFilter;\n"
        "  var m = new THREE.ShaderMaterial({\n"
        "    uniforms: { uMap: { value: t } },\n"
        "    vertexShader: " + json.dumps(vert) + ",\n"
        "    fragmentShader: " + json.dumps(frag) + ",\n"
        "    transparent: true, depthWrite: false, depthTest: true,\n"
        "    blending: THREE.AdditiveBlending\n"
        "  });\n"
        "  var mesh = new THREE.Mesh(new THREE.PlaneGeometry(CARD_WIDTH, CARD_HEIGHT), m);\n"
        "  mesh.position.set(0, 0, " + str(z) + ");\n"
        "  mesh.renderOrder = " + str(order) + ";\n"
        "  flipGroup.add(mesh);\n"
        "})();\n"
    )


def _smoke_layer_js(rel: str | None, noise_rel: str | None = None,
                    strength: float = 1.0, speed: float = 1.0, amt: float = 1.0,
                    z: float = 0.008, order: float = 3.0) -> str:
    """元素烟雾层：**完全静止**的云纹底纹。

    烟雾图不可平铺，任何位移只能在卡边 clamp 采样、拉出扫描线；
    所以这一层只提供静止质感，真正的「动」交给卡面 shader 对
    **印刷背景**做的原地 UV 扭曲（预设里的 flow 字段 / uFlowAmt）。
    """
    if not rel:
        return ""
    return _detail_layer_js(rel, z=z, order=order)


_SEAL_TICK_JS = (
    "  // 膜光效完全由视角驱动（拖动旋转触发）：无时间动画、无指针光球，杜绝频闪\n"
    "  // 例外：流动背景层只让**印刷背景**随时间漂移（立绘/UI 由掩码固定），\n"
    "  // 慢速多正弦、无亮度脉动，不会自闪。\n"
    "  if (typeof cardMaterial !== 'undefined' && cardMaterial.uniforms.uFlowTime) {\n"
    "    cardMaterial.uniforms.uFlowTime.value += dt;\n"
    "  }\n"
    "  if (window.__flowMats) {\n"
    "    for (var _fi = 0; _fi < window.__flowMats.length; _fi++) {\n"
    "      var _fm = window.__flowMats[_fi];\n"
    "      if (_fm && _fm.uniforms && _fm.uniforms.uTime) _fm.uniforms.uTime.value += dt;\n"
    "    }\n"
    "  }\n"
)

# 辉光每帧更新：动画时间推进 + 卡面朝向（1=正对视角，供菲涅尔描边使用）
_GLOW_TICK_JS = (
    "  (function () {\n"
    "    if (!auraMaterial.uniforms.uTime) return;\n"
    "    auraMaterial.uniforms.uTime.value += dt;\n"
    "    flipGroup.updateWorldMatrix(true, false);\n"
    "    flipGroup.getWorldQuaternion(_qGlow);\n"
    "    _vN.set(0, 0, 1).applyQuaternion(_qGlow);\n"
    "    _vGlow.copy(camera.position).normalize();\n"
    "    auraMaterial.uniforms.uGlowFacing.value = Math.abs(_vN.dot(_vGlow));\n"
    "  })();\n"
)


def _glow_init_js(glow_name: str | None, glow_strength: float, glow_boost: float = 1.0) -> str:
    """按辉光预设注入参数（none=隐藏辉光；未知名回退默认预设）。

    glow_boost: 异形边框强度补偿（>1 时允许有效强度超过 2.0 上限）。
    """
    if glow_name == "none":
        return "  (function () { auraMaterial.visible = false; })();\n"
    glow = resolve_glow_def(glow_name) if glow_name else None
    if glow is None:
        glow = (resolve_glow_def(next(iter(GLOW_EFFECTS))) if GLOW_EFFECTS else {
            "colorTop": (0.98, 0.63, 0.18), "colorBottom": (0.44, 0.08, 0.025),
            "tight": 0.014, "soft": 0.045, "mode": 0, "fresnel": 0.0, "speed": 0.0})
        glow_strength = 1.0
    strength = min(2.0, max(0.0, glow_strength)) * max(0.0, glow_boost)
    top = tuple(glow.get("colorTop") or (0.98, 0.63, 0.18))
    bottom = tuple(glow.get("colorBottom") or (0.44, 0.08, 0.025))
    mode = int(glow.get("mode") or 0)
    fresnel = float(glow.get("fresnel") or 0.0)
    speed = float(glow.get("speed") or 0.0)
    tight = float(glow.get("tight") or 0.014)
    soft = float(glow.get("soft") or 0.045)
    return (
        "  (function () {\n"
        "    auraMaterial.uniforms.uGlowTop.value.set(" + ", ".join(repr(v) for v in top) + ");\n"
        "    auraMaterial.uniforms.uGlowBottom.value.set(" + ", ".join(repr(v) for v in bottom) + ");\n"
        "    auraMaterial.uniforms.uGlowTight.value = " + repr(tight) + ";\n"
        "    auraMaterial.uniforms.uGlowSoft.value = " + repr(soft) + ";\n"
        "    auraMaterial.uniforms.uGlowMode.value = " + repr(mode) + ";\n"
        "    auraMaterial.uniforms.uGlowFresnel.value = " + repr(fresnel) + ";\n"
        "    auraMaterial.uniforms.uGlowSpeed.value = " + repr(speed) + ";\n"
        "    auraMaterial.uniforms.uGlowStrength.value = " + repr(strength) + ";\n"
        "    auraMaterial.uniforms.uTime.value = 0.0;\n"
        "  })();\n"
    )


def _text_adapt_limit(text_type: str, description: str | None, text_pos: dict | None) -> float | None:
    """文本型主体自适应缩放比例（文本区顶部 y1，默认 0.70）；非文本型返回 None。

    文本型（boxed）无论是否已输入描述都缩放到卡图区（切换类型立即生效）；
    无文本型/特殊文本型即使带描述也整幅显示。
    """
    if text_type != "boxed":
        return None
    limit = 0.70
    if text_pos and all(k in text_pos for k in ("x1", "x2", "y1", "y2")):
        try:
            limit = min(1.0, max(0.05, float(text_pos["y1"])))
        except (TypeError, ValueError):
            pass
    return limit


def _fg_mask_js(content_scale: float = 1.0, mask_tex: str = "interiorTexture") -> str:
    """边框内蒙版裁剪主体 JS：前景主体/描边/阴影不超出边框外围（与 2D 一致）。

    把蒙版纹理挂为主体材质 alphaMap（乘算 alpha），并用纹理 offset/repeat
    把网格局部 UV 换算到卡片坐标：cardUv = (局部位置 / CARD_W) * s + 0.5。
    主体组随 cardContent 缩放（s=content_scale），文本型几何还会被 _fg_adapt_js
    运行时缩放/移位，这里轮询到稳定后同步变换（同 _fg_adapt_js 的节奏）。
    mask_tex: 蒙版纹理变量名，生成的 JS 直接引用该变量（运行时求值，避免值拷贝
    导致浮起时 edgeTex 生成前被固定为 null）——未浮于边框时用 interiorTexture
    （边框环+中空），浮于边框之上时用 _fg_edge_mask_js 合成的 edgeTex（仅中空）。
    """
    return (
        "  (function () {\n"
        "    var s = " + str(content_scale) + ";\n"
        "    function applyMask(m) {\n"
        "      if (!m.isMesh || !m.material || !m.geometry || m.geometry.type !== 'PlaneGeometry') return;\n"
        "      if (!" + mask_tex + ") return;\n"
        "      if (m.material.alphaMap === " + mask_tex + ") return;\n"
        "      m.material.alphaMap = " + mask_tex + ";\n"
        "      " + mask_tex + ".center.set(0, 0);\n"
        "      m.material.needsUpdate = true;\n"
        "    }\n"
        "    // 蒙版纹理矩阵固定于卡片坐标系：offset 含前景组位移（视差），\n"
        "    // 否则组移动时蒙版图案跟着主体走，裁剪边界不固定在卡片上，主体会滑出边框外沿\n"
        "    function syncWorld() {\n"
        "      if (!" + mask_tex + ") return;\n"
        "      var gx = foregroundGroup.position.x, gy = foregroundGroup.position.y;\n"
        "      foregroundGroup.children.forEach(function (m) {\n"
        "        if (!m.isMesh || !m.material || m.material.alphaMap !== " + mask_tex + ") return;\n"
        "        var g = m.geometry.parameters;\n"
        "        if (!g || !g.width) return;\n"
        "        var t = m.material.alphaMap;\n"
        "        t.repeat.set((g.width * s) / CARD_WIDTH, (g.height * s) / CARD_HEIGHT);\n"
        "        t.offset.set((gx + m.position.x - g.width / 2) * s / CARD_WIDTH + 0.5,\n"
        "                     (gy + m.position.y - g.height / 2) * s / CARD_HEIGHT + 0.5);\n"
        "      });\n"
        "    }\n"
        "    window.__syncFgMask = syncWorld;\n"
        "    // 主体描边层在本段之后才创建：轮询直到所有网格挂上蒙版；变换由 __syncFgMask 每帧维护\n"
        "    (function tick() {\n"
        "      var pending = false;\n"
        "      foregroundGroup.children.forEach(function (m) {\n"
        "        if (!m.isMesh || !m.material || !m.geometry || m.geometry.type !== 'PlaneGeometry') return;\n"
        "        if (!m.material.alphaMap || m.material.alphaMap !== " + mask_tex + ") { applyMask(m); pending = true; }\n"
        "      });\n"
        "      if (pending) setTimeout(tick, 30);\n"
        "    })();\n"
        "  })();\n"
    )


def _fg_edge_mask_js(content_scale: float = 1.0, frame_rel: str | None = None,
                     interior_rel: str | None = None) -> str:
    """浮于边框之上时主体的「边框内沿蒙版」JS：主体裁到边框内沿（中空区域），
    不压住边框装饰、不超出边框外沿（与 2D frame_inner_edge_mask 一致）。

    浏览器端从边框图与 interior 蒙版合成中空蒙版（interior ∩ 非边框）：
    浮起时边框恒为整卡（fit_subject 仅未浮起启用），interior 图也按整卡拉伸生成，
    两者拉伸对齐；合成一次后由 _fg_mask_js 挂到前景组网格并每帧同步矩阵。
    """
    mask_js = _fg_mask_js(content_scale, mask_tex="edgeTex")
    return (
        "  (function () {\n"
        "    var edgeTex = null;\n"
        "    var frameImg = null, interiorImg = null;\n"
        "    function build() {\n"
        "      if (edgeTex || !frameImg || !interiorImg) return;\n"
        "      var cv = document.createElement('canvas');\n"
        "      cv.width = 450; cv.height = 600;\n"
        "      var ctx = cv.getContext('2d');\n"
        "      var cvA = document.createElement('canvas'); cvA.width = 450; cvA.height = 600;\n"
        "      var cta = cvA.getContext('2d');\n"
        "      cta.drawImage(interiorImg, 0, 0, 450, 600);\n"
        "      var dA = cta.getImageData(0, 0, 450, 600).data;\n"
        "      var cvB = document.createElement('canvas'); cvB.width = 450; cvB.height = 600;\n"
        "      var ctb = cvB.getContext('2d');\n"
        "      ctb.drawImage(frameImg, 0, 0, 450, 600);\n"
        "      var dB = ctb.getImageData(0, 0, 450, 600).data;\n"
        "      var out = ctx.createImageData(450, 600);\n"
        "      var od = out.data;\n"
        "      for (var i = 0; i < od.length; i += 4) {\n"
        "        var keep = (dA[i + 3] > 128 && dB[i + 3] <= 8) ? 255 : 0;\n"
        "        od[i] = od[i + 1] = od[i + 2] = 255; od[i + 3] = keep;\n"
        "      }\n"
        "      ctx.putImageData(out, 0, 0);\n"
        "      edgeTex = new THREE.CanvasTexture(cv);\n"
        "      edgeTex.colorSpace = THREE.NoColorSpace;\n"
        "    }\n"
        "    loader.load(" + json.dumps(frame_rel) + ", function (t) { frameImg = t.image; build(); });\n"
        "    loader.load(" + json.dumps(interior_rel) + ", function (t) { interiorImg = t.image; build(); });\n"
        + mask_js
        + "  })();\n"
    )


def _fg_adapt_js(adapt_limit: float | None, content_scale: float = 1.0, follow_bg: bool = False,
                 bg_follow_rel: str | None = None) -> str:
    """文本型前景适配 JS：前景平面按源图比例等比缩放（contain），完整显示、不裁剪、不变形，
    居中于卡图区（卡顶到文本区顶部 y1），主体中心 = 余卡面范围中心（文本区下移时主体随之上移）。

    与 2D 合成（compositor._contain）保持一致。
    content_scale: 卡面缩放系数，同步用于 alphaMap 蒙版换算（主体随卡面缩放后蒙版不错位）。
    follow_bg: 整幅+抠图+文本型：背景跟随主体——额外创建「背景跟随网格」（bg_follow.png，
    与主体同尺寸同几何，任意源图比例下像素级对齐）与「虚化整卡底」（填充文本区留白），
    与 2D 合成（compositor.follow_bg）一致。
    """
    if adapt_limit is None:
        return ""
    # 背景跟随网格：显示预适配背景图（与主体同几何/同位置，主体精确遮盖背景原主体），
    # 圆角+边框内蒙版按卡片坐标裁剪
    bg_follow_js = (
        "    var bgFollow = null;\n"
        "    function ensureBgFollow() {\n"
        "      if (bgFollow) return true;\n"
        "      var bgfMat = new THREE.ShaderMaterial({\n"
        "        transparent: true, depthWrite: false, side: THREE.FrontSide, toneMapped: false,\n"
        "        uniforms: {\n"
        "          uBgTexture: { value: bgFollowTexture },\n"
        "          uAspect: { value: CARD_WIDTH / CARD_HEIGHT },\n"
        "          uMeshX: { value: 0 }, uMeshW: { value: 1 },\n"
        "          uMeshY: { value: 0 }, uMeshH: { value: 1 },\n"
        "          uInteriorMap: { value: interiorTexture },\n"
        "        },\n"
        "        vertexShader: 'varying vec2 vUv; void main(){ vUv = uv; gl_Position = projectionMatrix * modelViewMatrix * vec4(position,1.0); }',\n"
        "        fragmentShader: [\n"
        "          'uniform sampler2D uBgTexture; uniform float uAspect; uniform float uMeshX; uniform float uMeshW; uniform float uMeshY; uniform float uMeshH; uniform sampler2D uInteriorMap; varying vec2 vUv;',\n"
        "          'float bgRounded(vec2 uv, float radius){ vec2 s = vec2((uv.x-0.5)*uAspect, uv.y-0.5); vec2 hs = vec2(0.5*uAspect, 0.5); vec2 e = abs(s) - (hs - vec2(radius)); return length(max(e,0.0)) + min(max(e.x,e.y),0.0) - radius; }',\n"
        "          'void main(){ vec2 cardUv = vec2(vUv.x * uMeshW + uMeshX, vUv.y * uMeshH + uMeshY); if (bgRounded(cardUv, 0.032) > 0.0) discard; vec4 c = texture2D(uBgTexture, vUv); c.a *= texture2D(uInteriorMap, cardUv).a; gl_FragColor = c; }'\n"
        "        ].join('\\n'),\n"
        "      });\n"
        "      bgFollow = new THREE.Mesh(new THREE.PlaneGeometry(CARD_WIDTH, adaptH), bgfMat);\n"
        "      bgFollow.position.z = 0.003;\n"
        "      bgFollow.renderOrder = 1.5;\n"
        "      cardContent.add(bgFollow);\n"
        "      window.__BG_FOLLOW__ = bgFollow;\n"
        "      return true;\n"
        "    }\n"
        "    // 虚化整卡底：填充背景跟随区域之外的留白（文本区），与 2D follow_bg 的模糊底一致\n"
        "    var blurBase = null;\n"
        "    function ensureBlurBase() {\n"
        "      if (blurBase) return true;\n"
        "      var img = frontTexture.image;\n"
        "      if (!img || !img.width || !img.height) return false;\n"
        "      var cw2 = 300, ch2 = 400;\n"
        "      var cv = document.createElement('canvas');\n"
        "      cv.width = cw2; cv.height = ch2;\n"
        "      var ctx = cv.getContext('2d');\n"
        "      var bs = Math.max(cw2 / img.width, ch2 / img.height);\n"
        "      var dw = img.width * bs, dh = img.height * bs;\n"
        "      ctx.filter = 'blur(16px)';\n"
        "      ctx.drawImage(img, (cw2 - dw) / 2, (ch2 - dh) / 2, dw, dh);\n"
        "      var bt = new THREE.CanvasTexture(cv);\n"
        "      bt.colorSpace = THREE.NoColorSpace;\n"
        "      var bMat = new THREE.ShaderMaterial({\n"
        "        transparent: true, depthWrite: false, side: THREE.FrontSide, toneMapped: false,\n"
        "        uniforms: {\n"
        "          uBgTexture: { value: bt },\n"
        "          uAspect: { value: CARD_WIDTH / CARD_HEIGHT },\n"
        "          uInteriorMap: { value: interiorTexture },\n"
        "        },\n"
        "        vertexShader: 'varying vec2 vUv; void main(){ vUv = uv; gl_Position = projectionMatrix * modelViewMatrix * vec4(position,1.0); }',\n"
        "        fragmentShader: [\n"
        "          'uniform sampler2D uBgTexture; uniform float uAspect; uniform sampler2D uInteriorMap; varying vec2 vUv;',\n"
        "          'float bgRounded(vec2 uv, float radius){ vec2 s = vec2((uv.x-0.5)*uAspect, uv.y-0.5); vec2 hs = vec2(0.5*uAspect, 0.5); vec2 e = abs(s) - (hs - vec2(radius)); return length(max(e,0.0)) + min(max(e.x,e.y),0.0) - radius; }',\n"
        "          'void main(){ if (bgRounded(vUv, 0.032) > 0.0) discard; vec4 c = texture2D(uBgTexture, vUv); c.a *= texture2D(uInteriorMap, vUv).a; gl_FragColor = c; }'\n"
        "        ].join('\\n'),\n"
        "      });\n"
        "      var bMesh = new THREE.Mesh(new THREE.PlaneGeometry(CARD_WIDTH, CARD_HEIGHT), bMat);\n"
        "      bMesh.position.z = 0.002;\n"
        "      bMesh.renderOrder = 0.5;\n"
        "      flipGroup.add(bMesh);\n"
        "      blurBase = bMesh;\n"
        "      return true;\n"
        "    }\n"
        "    function syncBg(f) {\n"
        "      bgFollow.geometry = new THREE.PlaneGeometry(f.w, f.h);\n"
        "      bgFollow.position.set(f.cx + (window.__SUBJ_OFFSET_X_VAL__ || 0) * CARD_WIDTH, f.cy + (window.__SUBJ_OFFSET_Y_VAL__ || 0) * CARD_HEIGHT, 0.003);\n"
        "      var u = bgFollow.material.uniforms;\n"
        "      u.uMeshX.value = (f.cx - f.w / 2) * s / CARD_WIDTH + 0.5;\n"
        "      u.uMeshW.value = f.w * s / CARD_WIDTH;\n"
        "      u.uMeshY.value = (f.cy - f.h / 2) * s / CARD_HEIGHT + 0.5;\n"
        "      u.uMeshH.value = f.h * s / CARD_HEIGHT;\n"
        "    }\n"
    ) if follow_bg else ""
    adapt_extra = (
        "      if (bgFollow) {\n"
        "        if (fit) { syncBg(fit); } else { pending = true; }\n"
        "      }\n"
        "      if (!ensureBlurBase()) pending = true;\n"
    ) if follow_bg else ""
    ensure_init = "      if (!ensureBgFollow()) pending = true;\n" if follow_bg else ""
    return (
        "  (function () {\n"
        "    var limit = " + json.dumps(adapt_limit) + ";\n"
        "    var s = " + str(content_scale) + ";\n"
        "    var adaptH = CARD_HEIGHT * limit;\n"
        + bg_follow_js +
        "    function fitMesh(m) {\n"
        "      var tex = m.material && m.material.map;\n"
        "      var img = tex && tex.image;\n"
        "      if (!img || !img.width || !img.height) return null;\n"
        "      var scale = Math.min(CARD_WIDTH / img.width, adaptH / img.height);\n"
        "      var w = img.width * scale, h = img.height * scale;\n"
        "      // px/py 是卡图区（卡顶到 y1，y 向下）内主体的左上角坐标，转成 three.js 中心坐标（y 向上）\n"
        "      var px = (CARD_WIDTH - w) / 2;\n"
        "      var py = (adaptH - h) / 2;\n"
        "      var cx = px + w / 2 - CARD_WIDTH / 2;\n"
        "      var cy = CARD_HEIGHT / 2 - (py + h / 2);\n"
        "      return { w: w, h: h, cx: cx, cy: cy };\n"
        "    }\n"
        "    function applyFit(m, f) {\n"
        "      var tex = m.material && m.material.map;\n"
        "      if (Math.abs(m.geometry.parameters.width - f.w) > 1e-4 ||\n"
        "          Math.abs(m.geometry.parameters.height - f.h) > 1e-4) {\n"
        "        m.geometry = new THREE.PlaneGeometry(f.w, f.h);\n"
        "      }\n"
        "      m.position.x = f.cx;\n"
        "      m.position.y = f.cy + (m.renderOrder <= 2 ? -0.034 : 0);\n"
        "      tex.offset.set(0, 0);\n"
        "      tex.repeat.set(1, 1);\n"
        "      // 边框内蒙版：随几何/位置同步换算（主体缩放后仍精确裁剪在边框外围以内）\n"
        "      var am = m.material.alphaMap;\n"
        "      if (am) {\n"
        "        am.center.set(0, 0);\n"
        "        am.repeat.set((f.w * s) / CARD_WIDTH, (f.h * s) / CARD_HEIGHT);\n"
        "        am.offset.set((f.cx - f.w / 2) * s / CARD_WIDTH + 0.5, (f.cy - f.h / 2) * s / CARD_HEIGHT + 0.5);\n"
        "        am.needsUpdate = true;\n"
        "      }\n"
        "    }\n"
        "    function adapt() {\n"
        "      var pending = false;\n"
        "      var fit = null;\n"
        "      foregroundGroup.children.forEach(function (m) {\n"
        "        if (!m.isMesh || !m.geometry || m.geometry.type !== 'PlaneGeometry') return;\n"
        "        var f = fitMesh(m);\n"
        "        if (!f) { pending = true; return; }\n"
        "        if (!fit) fit = f;\n"
        "        applyFit(m, f);\n"
        "      });\n"
        + ensure_init +
        adapt_extra +
        "      if (pending) setTimeout(adapt, 30);\n"
        "    }\n"
        "    adapt();\n"
        "  })();\n"
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


# 主体圆角与卡面一致（radius 0.032 与主卡 shader 一致，900x1200 下为 38.4px）。
# 按纹理实际尺寸重绘（不拉伸变形），圆角半径随宽度等比换算。
FG_ROUND_JS = r"""
  (function () {
    var orig = foregroundTexture;
    var t = new THREE.Texture();
    t.colorSpace = THREE.SRGBColorSpace;
    t.anisotropy = 8;
    foregroundTexture = t;
    function round() {
      var img = orig.image;
      if (!img || !img.width || !img.height) { setTimeout(round, 30); return; }
      var nv = document.createElement('canvas');
      nv.width = img.width;
      nv.height = img.height;
      var ctx = nv.getContext('2d');
      ctx.clearRect(0, 0, nv.width, nv.height);
      ctx.drawImage(img, 0, 0);
      var r = Math.min(38.4 * nv.width / 900, nv.height * 0.5);
      ctx.globalCompositeOperation = 'destination-in';
      ctx.beginPath();
      ctx.moveTo(r, 0);
      ctx.lineTo(nv.width - r, 0);
      ctx.arcTo(nv.width, 0, nv.width, r, r);
      ctx.lineTo(nv.width, nv.height - r);
      ctx.arcTo(nv.width, nv.height, nv.width - r, nv.height, r);
      ctx.lineTo(r, nv.height);
      ctx.arcTo(0, nv.height, 0, nv.height - r, r);
      ctx.lineTo(0, r);
      ctx.arcTo(0, 0, r, 0, r);
      ctx.closePath();
      ctx.fill();
      "// three.js 对\"创建纹理后再改画布尺寸\"的重传不可靠，故加载完成后新建画布再挂载\n"
      t.image = nv;
      t.needsUpdate = true;
    }
    round();
  })();
"""


def _bg_layer_js(rel: str | None, face_scales: bool) -> str:
    """独立背景层 JS：卡面单独缩放时背景/深色底整卡铺满（与 2D 合成一致）。

    face_scales（卡面素材随缩放）时创建：整卡尺寸、挂 flipGroup 不随缩放、仅正面渲染；
    有背景图时 cover 裁剪铺满整卡，无图时用深色底（14,11,9，同 2D 画布底色）；
    圆角 + 边框内蒙版裁剪（与正面整卡内容一致）。返回空串时无需背景层。
    """
    if not face_scales:
        return ""
    if rel:
        return (
            "  (function () {\n"
            "    var bgTex = loader.load(" + json.dumps(rel) + ");\n"
            "    bgTex.colorSpace = THREE.SRGBColorSpace;\n"
            "    bgTex.anisotropy = 8;\n"
            "    var bgMat = new THREE.ShaderMaterial({\n"
            "      transparent: true, depthWrite: false, side: THREE.FrontSide, toneMapped: false,\n"
            "      uniforms: {\n"
            "        uBgTexture: { value: bgTex },\n"
            "        uTexAspect: { value: 0.75 },\n"
            "        uAspect: { value: CARD_WIDTH / CARD_HEIGHT },\n"
            "        uInteriorMap: { value: interiorTexture },\n"
            "      },\n"
            "      vertexShader: 'varying vec2 vUv; void main(){ vUv = uv; gl_Position = projectionMatrix * modelViewMatrix * vec4(position,1.0); }',\n"
            "      fragmentShader: [\n"
            "        'uniform sampler2D uBgTexture; uniform float uTexAspect; uniform float uAspect; uniform sampler2D uInteriorMap; varying vec2 vUv;',\n"
            "        'float bgRounded(vec2 uv, float radius){ vec2 s = vec2((uv.x-0.5)*uAspect, uv.y-0.5); vec2 hs = vec2(0.5*uAspect, 0.5); vec2 e = abs(s) - (hs - vec2(radius)); return length(max(e,0.0)) + min(max(e.x,e.y),0.0) - radius; }',\n"
            "        'void main(){ if (bgRounded(vUv, 0.032) > 0.0) discard; vec2 bv = vUv; if (uTexAspect > uAspect) { bv.x = (vUv.x-0.5)*(uAspect/uTexAspect)+0.5; } else { bv.y = (vUv.y-0.5)*(uTexAspect/uAspect)+0.5; } vec4 c = texture2D(uBgTexture, clamp(bv, 0.001, 0.999)); c.a *= texture2D(uInteriorMap, vUv).a; gl_FragColor = c; }'\n"
            "      ].join('\\n'),\n"
            "    });\n"
            "    loader.load(" + json.dumps(rel) + ", function (t) { bgMat.uniforms.uTexAspect.value = t.image.width / t.image.height; });\n"
            "    var bgMesh = new THREE.Mesh(new THREE.PlaneGeometry(CARD_WIDTH, CARD_HEIGHT), bgMat);\n"
            "    bgMesh.position.z = -0.004; bgMesh.renderOrder = -2; flipGroup.add(bgMesh);\n"
            "    window.__BG_LAYER__ = bgMesh;\n"
            "  })();\n"
        )
    return (
        "  (function () {\n"
        "    var bgMat = new THREE.ShaderMaterial({\n"
        "      transparent: true, depthWrite: false, side: THREE.FrontSide, toneMapped: false,\n"
        "      uniforms: {\n"
        "        uColor: { value: new THREE.Color(0x0e0b09) },\n"
        "        uAspect: { value: CARD_WIDTH / CARD_HEIGHT },\n"
        "        uInteriorMap: { value: interiorTexture },\n"
        "      },\n"
        "      vertexShader: 'varying vec2 vUv; void main(){ vUv = uv; gl_Position = projectionMatrix * modelViewMatrix * vec4(position,1.0); }',\n"
        "      fragmentShader: [\n"
        "        'uniform vec3 uColor; uniform float uAspect; uniform sampler2D uInteriorMap; varying vec2 vUv;',\n"
        "        'float bgRounded(vec2 uv, float radius){ vec2 s = vec2((uv.x-0.5)*uAspect, uv.y-0.5); vec2 hs = vec2(0.5*uAspect, 0.5); vec2 e = abs(s) - (hs - vec2(radius)); return length(max(e,0.0)) + min(max(e.x,e.y),0.0) - radius; }',\n"
        "        'void main(){ if (bgRounded(vUv, 0.032) > 0.0) discard; vec4 c = vec4(uColor, 1.0); c.a *= texture2D(uInteriorMap, vUv).a; gl_FragColor = c; }'\n"
        "      ].join('\\n'),\n"
        "    });\n"
        "    var bgMesh = new THREE.Mesh(new THREE.PlaneGeometry(CARD_WIDTH, CARD_HEIGHT), bgMat);\n"
        "    bgMesh.position.z = -0.004; bgMesh.renderOrder = -2; flipGroup.add(bgMesh);\n"
        "    window.__BG_LAYER__ = bgMesh;\n"
        "  })();\n"
    )


def _face_mesh_js(face_scales: bool) -> str:
    """卡面/卡背双网格拆分 JS。

    face_scales（卡面为卡面素材随缩放）时：主网格只渲染背面（卡背整卡不缩放），
    另建卡面网格随 cardContent 缩放——缩放只作用卡面，卡背保持整卡尺寸。

    用 uniform uSide 区分网格角色（2=双面默认 / 1=仅卡背 / 0=仅卡面），
    不用 BackSide 拆面——Three.js 渲染 BackSide 时会反转 frontFace 绕序，
    导致背面片段的 gl_FrontFacing 误报为 true，翻面后错误走正面分支显示卡面。
    """
    if not face_scales:
        return ""
    return (
        "  (function () {\n"
        "    cardMaterial.uniforms.uSide.value = 1.0;\n"
        "    var faceMat = cardMaterial.clone();\n"
        "    faceMat.side = THREE.FrontSide;\n"
        "    faceMat.uniforms = {};\n"
        "    for (var uk in cardMaterial.uniforms) faceMat.uniforms[uk] = cardMaterial.uniforms[uk];\n"
        "    faceMat.uniforms.uSide = { value: 0.0 };\n"
        "    var faceMesh = new THREE.Mesh(new THREE.PlaneGeometry(CARD_WIDTH, CARD_HEIGHT), faceMat);\n"
        "    cardContent.add(faceMesh);\n"
        "    hitMeshes.push(faceMesh);\n"
    "    window.__FACE_MAT__ = faceMat;\n"
    "  })();\n"
)


def _subject_offset_js(off_x: float, off_y: float) -> str:
    """主体偏移 JS：设置全局偏移量（卡宽/卡高比例），供视差目标与 bgFollow 同步使用。

    仅偏移主体图像位置（foregroundGroup 经视差目标带偏移，蒙版随 syncWorld 钉在卡片坐标），
    渲染范围（蒙版裁剪边界）不跟随偏移；整幅+抠图+文本型的背景跟随网格同步偏移保持对齐。
    """
    if not off_x and not off_y:
        return ""
    return (
        "  (function () {\n"
        "    window.__SUBJ_OFFSET_X_VAL__ = " + str(off_x) + ";\n"
        "    window.__SUBJ_OFFSET_Y_VAL__ = " + str(off_y) + ";\n"
        "    var b = window.__BG_FOLLOW__;\n"
        "    if (b) { b.position.x += " + str(off_x) + " * CARD_WIDTH; b.position.y += " + str(off_y) + " * CARD_HEIGHT; }\n"
        "  })();\n"
    )


def build_card_html(name: str, front_rel: str, foreground_rel: str, back_rel: str, effects=None,
                    frame_rel: str | None = None, seal_rel: str | None = None,
                    seal_name: str | None = None,
                    seal_frame_rel: str | None = None,
                    seal_frame_name: str | None = None,
                    description: str | None = None, text_type: str = "none",
                    text_pos: dict | None = None,
                    subject_over_frame: bool = False,
                    subject_offset_x: float = 0.0,
                    subject_offset_y: float = 0.0,
                    round_foreground: bool = False,
                    frame_fit_subject: bool = False,
                    outline_rel: str | None = None,
                    seal_strength_front: float = 0.945,
                    seal_strength_back: float = 0.5775,
                    seal_base: float = 0.55,
                    seal_amp: float = 0.45,
                    seal_tilt: float = 0.045,
                    seal_add: bool = False,
                    seal_tilt_lo: float = 0.12,
                    seal_tilt_hi: float = 0.6,
                    seal_sheen_base: float = 0.03,
                    seal_band_power: float = 4.0,
                    seal_extra: list | None = None,
                    detail_rel: str | None = None,
                    deco_under_rel: str | None = None,
                    deco_over_rel: str | None = None,
                    smoke_rel: str | None = None,
                    smoke_noise_rel: str | None = None,
                    smoke_amt: float = 0.0,
                    smoke_speed: float = 1.0,
                    smoke_strength: float = 1.0,
                    pattern_rel: str | None = None,
                    text_rel: str | None = None,
                    flow_mask_rel: str | None = None,
                    flow_noise_rel: str | None = None,
                    flow_amt: float = 0.0,
                    flow_speed: float = 1.0,
                    seal_frame_strength_front: float = 0.8,
                    seal_frame_strength_back: float = 0.5,
                    content_scale: float = 1.0,
                    face_scales: bool = False,
                    interior_rel: str | None = None,
                    background_rel: str | None = None,
                    mask_clip: bool = True,
                    glow_name: str | None = None,
                    glow_strength: float = 1.0,
                    glow_boost: float = 1.0,
                    follow_bg: bool = False,
                    bg_follow_rel: str | None = None,
                    card_width: float = 2.25) -> str:
    """生成自包含 3D 卡网页 HTML。

    effects: 特效实例列表 [{"name":..., "def": {...}, "pos": {...}}]，def 为完整特效参数。
    frame_rel/seal_rel: 可选的边框/卡封图片相对路径（叠加在卡面之上）。
    seal_name: 卡封素材原始文件名（如「拉丝闪光膜.png」）；命中内置冷裱膜时改用动态光效层。
    seal_frame_rel/seal_frame_name: 层2 边框卡封（仅边框非透明区域显示，边框作蒙版）。
    description/text_type/text_pos: 卡面描述文字（text_type: none/transparent/boxed）。
    subject_over_frame: 透明主体浮于边框之上（PVZ 式立体感）；默认边框盖住主体。
    subject_offset_x/y: 主体图像偏移（卡宽/卡高比例，如 ±0.25），仅偏移图像位置，
    渲染范围（蒙版裁剪边界）不跟随偏移。
    round_foreground: 整幅卡面时对前景主体做圆角遮罩（与卡面圆角一致）。
    frame_fit_subject: 未开启浮于边框时，边框按前景主体包围盒缩放（否则整卡）。
    outline_rel: 主体白色描边图（贴纸边，叠加在主体层之下、随前景视差移动）。
    seal_strength_front/back: 层1 卡牌卡封效果强度（正面/背面，默认 0.945/0.5775）。
    seal_frame_strength_front/back: 层2 边框卡封效果强度（正面/背面，默认 0.8/0.5）。
    face_scales: front 是否为卡面素材（True 时卡面网格随 content_scale 缩放；False 时
    front 为背景底图整卡不缩放，仅主体随缩放）。缩放只作用卡面，背景/层1卡封不跟随。
    interior_rel: 边框内区域蒙版图（白=边框环+中空，黑=边框外围），正面整卡内容按此裁剪，
    边框实际绘制形态（整卡或按主体包围盒）在浏览器端动态对齐。
    background_rel: 独立背景层素材（卡面单独缩放时背景整卡铺满，不随缩放；缺省用深色底）。
    mask_clip: False 时不应用边框内蒙版裁剪，主体可延伸出边框外沿（内边框效果，与 2D 一致）。
    glow_name: 辉光特效名（GLOW_EFFECTS 键）；"none"=关闭辉光；None=默认暖金描边。
    glow_strength: 辉光整体强度（0~2，默认 1.0；未知名预设时强制 1.0）。
    glow_boost: 异形边框辉光强度补偿系数（由 frame_glow_boost 计算，默认 1.0）。
    follow_bg: 整幅+抠图+文本型：背景跟随主体（bg_follow.png 与主体同几何），
    主体精确遮盖背景原主体；另垫虚化整卡底填充文本区留白（与 2D follow_bg 一致）。
    bg_follow_rel: 预适配的背景跟随图相对路径（与前景主体同尺寸同几何，任意源图比例下
    主体都能像素级遮盖背景原主体）。
    card_width: 3D 卡体的宽（卡高恒为 3），默认 2.25（3:4）。卡面比例不同
    （如 Rip It! 游戏的 1465×2048 ≈ 5:7 → 2.25）时传入对应值，避免卡面被拉伸。
    """
    three_js = THREE_JS.read_text(encoding="utf-8")
    fg_js = (
        # 浮于边框之上：前景组整体提升到边框层之上、卡封之下
        CARD_HTML_TEMPLATE_FG_FLOAT
        if subject_over_frame else
        CARD_HTML_TEMPLATE_FG
    )
    # 前景主体/描边/阴影按边框内蒙版裁剪（整卡内容不超出边框外围，与 2D 一致）：
    #   未浮于边框时用 interior 蒙版（主体可延伸到边框环下、被边框盖住）；
    #   浮于边框之上时用边框内沿蒙版（主体裁到边框内沿，不压边框装饰、不超出边框外沿）。
    # mask_clip=False 时不裁剪（内边框效果：主体可延伸出边框外沿，边框外留一圈图像）
    if frame_rel and mask_clip:
        if subject_over_frame:
            if interior_rel:
                fg_js += _fg_edge_mask_js(content_scale, frame_rel, interior_rel)
            # interior_rel 缺失时不裁剪（避免主体被空蒙版整卡消隐，保持原浮起行为）
        else:
            fg_js += _fg_mask_js(content_scale)
    # 层1 卡牌卡封：整卡尺寸、不随缩放（挂 flipGroup，z/order 介于主体与边框之间）；
    # 有边框时仅正面渲染（卡背统一用边框卡封），并按边框内区域蒙版裁剪
    seal_layer = ""
    effect = None
    seal1_mask = "interiorTexture" if frame_rel else None
    if seal_name:
        key = Path(str(seal_name)).stem
        effect = SEAL_EFFECTS.get(key)
        if effect:
            seal_layer = _seal_layer_js(effect, seal_strength_front, seal_strength_back,
                                        parent="flipGroup", z=0.008, order=3.5,
                                        front_only=bool(frame_rel), mask_canvas=seal1_mask,
                                        mask_clip=mask_clip)
    if not seal_layer:
        seal_layer = _custom_seal_layer_js(seal_rel, seal_strength_front, seal_strength_back,
                                           parent="flipGroup", z=0.008, order=3.5,
                                           front_only=bool(frame_rel), mask_canvas=seal1_mask,
                                           mask_clip=mask_clip,
                                           base=seal_base, amp=seal_amp,
                                           tilt_gain=seal_tilt, blend_add=seal_add,
                                           tilt_lo=seal_tilt_lo, tilt_hi=seal_tilt_hi,
                                           sheen_base=seal_sheen_base,
                                           band_power=seal_band_power)
    if seal_layer:
        # 亚光膜（matte）几乎无箔光高光（更哑光），其余卡封默认箔光弱化一半
        seal_u = 0.12 if (effect and effect.get("mode") == "matte") else 0.5
        seal_layer += "  (function () { cardMaterial.uniforms.uSeal.value = " + str(seal_u) + "; })();\n"
    # 主 shader 正面卡面按边框内蒙版裁剪的开关：mask_clip=False 时关闭裁剪（内边框效果）
    mask_js = ("  (function () { if (cardMaterial.uniforms.uMaskClip) "
               "cardMaterial.uniforms.uMaskClip.value = "
               + ("1.0" if mask_clip else "0.0") + "; })();\n")
    # 层2 边框卡封：仅边框非透明区域显示（边框 PNG 的 alpha 作蒙版），整卡尺寸、高于边框；
    # 无边框时无蒙版源，不绘制层2（保持现状仅层1）；双面渲染，卡背卡封与边框卡封统一
    seal_frame_layer = ""
    if frame_rel:
        effect2 = None
        if seal_frame_name:
            key2 = Path(str(seal_frame_name)).stem
            effect2 = SEAL_EFFECTS.get(key2)
            if effect2:
                seal_frame_layer = _seal_layer_js(
                    effect2, seal_frame_strength_front, seal_frame_strength_back,
                    parent="flipGroup", z=0.02, order=5.2, mask_rel=frame_rel)
        if not seal_frame_layer:
            seal_frame_layer = _custom_seal_layer_js(
                seal_frame_rel, seal_frame_strength_front, seal_frame_strength_back,
                parent="flipGroup", z=0.02, order=5.2, mask_rel=frame_rel)
    # 卡面渲染缩放：可选项（默认 1.0 不缩放），由界面填写；仅缩放卡面/主体，背景与层1卡封不跟随。
    # 卡面网格随缩放时（face_scales），主 shader 需按内容缩放反算蒙版采样坐标（uContentScale）
    scale_js = ""
    if content_scale != 1.0:
        scale_js += "  (function () { cardContent.scale.setScalar(" + str(content_scale) + "); })();\n"
    if face_scales and content_scale != 1.0:
        scale_js += ("  (function () { cardMaterial.uniforms.uContentScale.value = " + str(content_scale)
                     + "; if (window.__FACE_MAT__) window.__FACE_MAT__.uniforms.uContentScale.value = "
                     + str(content_scale) + "; })();\n")
    # 边框层：未开启浮于边框且 frame_fit_subject 时按主体包围盒缩放，否则整卡；均仅正面渲染。
    # boxed 文本框边框素材按整卡设计（文本框在底部），不随主体缩放；
    # 文本型卡图 cover 铺满卡图区，边框按整卡覆盖、不向内收缩
    adapt_limit = _text_adapt_limit(text_type, description, text_pos)
    frame_layer = _frame_layer_js(
        frame_rel, 0.012, 4,
        frame_fit_subject and not subject_over_frame and text_type != "boxed" and adapt_limit is None,
        None,
        interior_rel,
        bool(frame_rel and not subject_over_frame and face_scales and frame_fit_subject),
    )
    # 主体描边层：渲染在主体之上（盖住主体边缘锯齿），浮起时随前景组抬升到边框之上
    outline_layer = _outline_layer_js(
        outline_rel, 0.0145 if subject_over_frame else 0.0065,
        4.45 if subject_over_frame else 3.1,
    )
    bg_follow_load = (
        "var bgFollowTexture = loader.load(" + json.dumps(bg_follow_rel) + ");\n"
        "  bgFollowTexture.colorSpace = THREE.NoColorSpace;\n"
        "  bgFollowTexture.anisotropy = 8;\n"
    ) if bg_follow_rel else ""
    offset_js = _subject_offset_js(subject_offset_x, subject_offset_y)
    # 多层卡封叠加（seal_extra）：每层独立着色器，各有自己的倾角窗口/强度。
    # 用途：官方的卡面光是「边光 + 扫光 + 细闪」几层叠出来的，不是单层。
    # 流动背景：卡面 shader 采样 UV 时只在**纯背景**区域做位移（掩码来自 2D 背景图），
    # 所以立绘/文字/图标原地不动，同一张贴图被「揉动」——没有第二层，不会重影。
    flow_glsl = ""
    flow_warp = "vec2 flowUv = faceUv;\n"
    flow_uniform_values = ""
    if flow_mask_rel and flow_amt > 0.0:
        flow_glsl = (
            "      uniform sampler2D uFlowMask;\n"
            "      uniform sampler2D uFlowNoise;\n"
            "      uniform float uFlowAmt;\n"
            "      uniform float uFlowSpeed;\n"
            "      uniform float uFlowTime;\n"
        )
        flow_warp = (
            "          vec2 flowUv = faceUv;\n"
            "          {\n"
            "            float fm = smoothstep(0.02, 0.55, texture2D(uFlowMask, faceUv).a);\n"
            "            if (fm > 0.002) {\n"
            "              float ft = uFlowTime * uFlowSpeed;\n"
            "// 噪声当角速度旋转 90° → 纯旋涡，无整体平移（否则像整张纸在滑）\n"
            "              vec2 n1 = texture2D(uFlowNoise, faceUv * vec2(0.62, 0.82)\n"
            "                                   + vec2(ft * 0.052, -ft * 0.040)).rg - 0.5;\n"
            "              vec2 n2 = texture2D(uFlowNoise, faceUv * vec2(2.10, 2.70)\n"
            "                                   + vec2(-ft * 0.095, ft * 0.072)).rg - 0.5;\n"
            "              vec2 sw = vec2(-n1.y, n1.x) * 0.0215 + vec2(-n2.y, n2.x) * 0.0062;\n"
            "              float ef = smoothstep(0.0, 0.055, min(min(faceUv.x, 1.0 - faceUv.x), min(faceUv.y, 1.0 - faceUv.y)));\n"
            "              flowUv = faceUv + sw * fm * ef * uFlowAmt;\n"
            "            }\n"
            "          }\n"
        )
        flow_uniform_values = (
            "      uFlowMask: { value: loader.load(" + json.dumps(flow_mask_rel) + ") },\n"
            "      uFlowNoise: { value: (function () {\n"
            "        var nt = loader.load(" + json.dumps(flow_noise_rel or "") + ");\n"
            "        nt.wrapS = THREE.RepeatWrapping; nt.wrapT = THREE.RepeatWrapping;\n"
            "        nt.colorSpace = THREE.NoColorSpace;\n"
            "        return nt;\n"
            "      })() },\n"
            "      uFlowAmt: { value: " + str(flow_amt) + " },\n"
            "      uFlowSpeed: { value: " + str(flow_speed) + " },\n"
            "      uFlowTime: { value: 0 },\n"
        )

    # 图层叠放（从纸到人眼）：
    #   底图(z0) → 烟雾(z0.008) → 版本图案(z0.014) → 卡封1(z0.010~) → 主体(z0.030)
    #   → 卡封2 稀有度框(z0.034) → 文字(z0.036)
    smoke_layer = _smoke_layer_js(smoke_rel, smoke_noise_rel,
                                  strength=float(smoke_strength),
                                  speed=float(smoke_speed),
                                  amt=float(smoke_amt))
    pattern_layer = _pattern_layer_js(pattern_rel)
    detail_layer = _detail_layer_js(detail_rel)
    # 共享装饰层：每张卡只有立绘是独有的，稀有度底板/条纹（under）与
    # 徽章/边框/属性图标（over）按「版本+属性+稀有度」复用同一份文件。
    deco_under_layer = _detail_layer_js(deco_under_rel, z=0.026, order=5.8)
    deco_over_layer = _detail_layer_js(deco_over_rel, z=0.033, order=6.6)
    text_layer = _detail_layer_js(text_rel, z=0.036, order=7.5)

    seal_extra_layer = ""
    for _i, _ex in enumerate(seal_extra or []):
        if not _ex or not _ex.get("rel"):
            continue
        if _ex.get("kind") == "flow":
            seal_extra_layer += _flow_layer_js(
                _ex["rel"],
                float(_ex.get("strength", 0.62)),
                float(_ex.get("speed", 1.0)),
                float(_ex.get("warp", 1.0)),
                z=float(_ex.get("z", 0.013)),
                order=float(_ex.get("order", 5.05)),
                drift=float(_ex.get("drift", 1.0)),
            )
            continue
        seal_extra_layer += _custom_seal_layer_js(
            _ex["rel"],
            float(_ex.get("front_strength", 1.0)),
            float(_ex.get("back_strength", 0.0)),
            parent="flipGroup",
            z=float(_ex.get("z", 0.010 + 0.0015 * _i)),
            order=float(_ex.get("order", 4.0 + 0.2 * _i)),
            front_only=bool(_ex.get("front_only", True)),
            base=float(_ex.get("base", 0.1)),
            amp=float(_ex.get("amp", 1.0)),
            tilt_gain=float(_ex.get("tilt", 0.5)),
            blend_add=bool(_ex.get("add", True)),
            tilt_lo=float(_ex.get("tilt_lo", 0.004)),
            tilt_hi=float(_ex.get("tilt_hi", 0.06)),
            sheen_base=float(_ex.get("sheen_base", 0.02)),
            band_power=float(_ex.get("band_power", 2.0)),
            rainbow=float(_ex.get("rainbow", 0.7)),
        )
    return (
        CARD_HTML_TEMPLATE
        .replace("__THREE_JS__", three_js)
        .replace("__CARD_WIDTH__", ("%g" % round(card_width, 6)))
        .replace("__CARD_NAME__", name)
        .replace("__FRONT__", front_rel)
        .replace("__FOREGROUND__", foreground_rel)
        .replace("__BACK__", back_rel)
        .replace("__BG_FOLLOW_LOAD__", bg_follow_load)
        .replace("__MESH_PARENT__", "flipGroup")
        .replace("__FACE_MESH_JS__", _face_mesh_js(face_scales))
        .replace("__BG_LAYER__", _bg_layer_js(background_rel, face_scales))
        .replace("__FG_BLOCK__", fg_js)
        .replace("__FG_ADAPT_JS__", _fg_adapt_js(adapt_limit, content_scale, follow_bg, bg_follow_rel))
        .replace("__FG_ROUND_JS__", FG_ROUND_JS if round_foreground else "")
        .replace("__OUTLINE_LAYER__", outline_layer)
        .replace("__FRAME_LAYER__", frame_layer)
        .replace("__FLOW_GLSL__", flow_glsl)
        .replace("__FLOW_WARP__", flow_warp)
        .replace("__FLOW_UNIFORM_VALUES__", flow_uniform_values)
        .replace("__SEAL_LAYER__", seal_layer + seal_frame_layer + seal_extra_layer
                 + deco_under_layer + detail_layer + deco_over_layer
                 + text_layer + smoke_layer + pattern_layer
                 + scale_js + mask_js + offset_js)
        .replace("__SEAL_TICK__", _SEAL_TICK_JS)
        .replace("__GLOW_TICK__", _GLOW_TICK_JS)
        .replace("__GLOW_INIT__", _glow_init_js(glow_name, glow_strength, glow_boost))
        .replace("__TEXT_LAYER__", _text_layer_js(description, text_type, text_pos))
        .replace("__ENGINE_JS__", EFFECT_ENGINE_JS)
        .replace("__EFFECTS__", _json(effects or []))
        .replace("__SUBJ_OFFSET_X__", str(subject_offset_x))
        .replace("__SUBJ_OFFSET_Y__", str(subject_offset_y))
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
