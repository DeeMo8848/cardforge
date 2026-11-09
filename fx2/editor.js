/* ============================================================
 * 特效DIY2 · 影之诗官方闪卡（foil）效果编辑器
 * 单一实现，供 cardforge 原生分页与 fx2/index.html 独立页共用。
 *   - 结构：单预览画布 + 底卡切换（雪华 进化前/后、疾风之箭）
 *   - 官方 Wizard/VariantCardShader 重建：Front1/Front2/Middle/Back1/Back2 + Distortion
 *   - 保存为预设 → cardforge 共用特效库（engine:"foil"），拼装页可直接选用
 * ============================================================ */
(function () {
  "use strict";

  /* ---------------- 页面模板 ---------------- */
  const FX2_HTML = `
<div class="fx2-root">
  <div class="fx2-layout">
    <!-- 预览 -->
    <div class="fx2-card">
      <h2>效果预览 <span class="fx2-sub" id="f2PanelTitle"></span></h2>
      <div class="fx2-basebar">
        <label for="f2Base">底卡</label>
        <select id="f2Base">
          <optgroup label="官方预览卡">
            <option value="sekka11">雪华 · 进化后</option>
            <option value="sekka10">雪华 · 进化前</option>
            <option value="arrow10">疾风之箭</option>
          </optgroup>
          <optgroup label="收集册成品卡" id="f2BaseCards"></optgroup>
        </select>
        <span class="fx2-tagchip" id="f2GName"></span>
      </div>
      <div class="fx2-toolbar">
        <button class="fx2-btn ghost" id="f2MaskViz" type="button">👁 遮罩视图</button>
        <button class="fx2-btn ghost" id="f2ResetT" type="button">↺ 重置时间</button>
        <button class="fx2-btn ghost" id="f2Play" type="button">⏸ 暂停</button>
      </div>
      <div class="fx2-cvwrap" id="f2CvWrap">
        <canvas id="f2Canvas" width="512" height="512"></canvas>
        <span class="fx2-cvtag" id="f2CardTag"></span>
      </div>
      <div class="fx2-cvtitle" id="f2CardTitle"></div>
      <div class="fx2-diag" id="f2Diag">初始化中…</div>
      <div class="fx2-note">
        每槽贴图按 <b>MoveType</b> 运动（1=固定 · 2=滚动 · 3=旋转 · 4=极坐标），按 <b>RenderType</b> 混合
        （1=加法 · 2=减法 · 3=透明）；<b>Distortion</b> 层以 RG 位移图折射全卡。
      </div>
    </div>

    <!-- 控制 -->
    <div class="fx2-card fx2-controls">
      <h2>特效参数</h2>

      <div class="fx2-sec">
        <div class="fx2-sechead"><b>💾 保存为预设</b><span class="fx2-sub" id="f2SaveCount"></span></div>
        <div class="fx2-secbody">
          <div class="fx2-savebar">
            <input type="text" id="f2Name" placeholder="预设名称，如：雪华狐火">
            <button class="fx2-btn" id="f2Save" type="button">保存</button>
          </div>
          <div class="fx2-savebar">
            <select id="f2List"></select>
            <button class="fx2-btn ghost" id="f2Load" type="button">载入</button>
            <button class="fx2-btn danger" id="f2Del" type="button">删除</button>
          </div>
          <div class="fx2-row">
            <span class="fx2-lab">蒙版（可选）</span>
            <button class="fx2-btn ghost" id="f2MaskBtn" type="button">上传 PNG</button>
            <span class="fx2-hint" id="f2UploadMaskName">无</span>
            <button class="fx2-btn ghost" id="f2MaskClear" type="button">清除</button>
            <input type="file" id="f2MaskFile" accept="image/png" hidden>
          </div>
          <div class="fx2-status" id="f2Status">保存后进入 cardforge 特效库，在「卡牌拼装」的特效下拉里选用；3D 卡牌生效时整面接管卡面。</div>
        </div>
      </div>

      <div class="fx2-sec">
        <div class="fx2-sechead"><b>全局</b><span class="fx2-sub">速度 · 强度 · 色调映射</span></div>
        <div class="fx2-secbody">
          <div class="fx2-row"><span class="fx2-lab">播放速度</span>
            <input type="range" id="f2Speed" min="0" max="3" step="0.05" value="1"><span class="fx2-val" id="f2SpeedV">1.00×</span></div>
          <div class="fx2-row"><span class="fx2-lab">扭曲强度</span>
            <input type="range" id="f2DistPow" min="0" max="3" step="0.05" value="1"><span class="fx2-val" id="f2DistPowV">1.00×</span></div>
          <div class="fx2-row"><span class="fx2-lab">特效强度</span>
            <input type="range" id="f2FxPow" min="0" max="2" step="0.02" value="0.6"><span class="fx2-val" id="f2FxPowV">0.60×</span></div>
          <div class="fx2-row"><span class="fx2-lab">过曝抑制</span>
            <input type="range" id="f2ToneKnee" min="0.4" max="0.99" step="0.01" value="0.85"><span class="fx2-val" id="f2ToneKneeV">0.85</span></div>
          <div class="fx2-row"><span class="fx2-lab">高光柔光</span>
            <input type="range" id="f2Bloom" min="0" max="1" step="0.01" value="0.10"><span class="fx2-val" id="f2BloomV">0.10</span></div>
          <div class="fx2-row"><span class="fx2-lab">整体曝光</span>
            <input type="range" id="f2Exposure" min="0.3" max="2" step="0.01" value="1"><span class="fx2-val" id="f2ExposureV">1.00</span></div>
          <div class="fx2-row"><span class="fx2-lab">色调映射</span>
            <button class="fx2-btn ghost on" id="f2ToneOn" type="button">● 开</button></div>
          <div class="fx2-row"><span class="fx2-lab">运动方向</span>
            <button class="fx2-btn ghost" id="f2MotionDir" type="button">⟲ 反向</button></div>
          <div class="fx2-hint warn">实机把卡面渲进浮点缓冲再合成：过曝抑制=膝点（越小压得越狠）、高光柔光=泛光强度、整体曝光=统一明暗。</div>
        </div>
      </div>

      <div class="fx2-sec">
        <div class="fx2-sechead"><b>虹光 Sheen</b><span class="fx2-sub">扫光覆盖层</span></div>
        <div class="fx2-secbody">
          <div class="fx2-row"><span class="fx2-lab">虹光</span>
            <select id="f2Sheen">
              <option value="off">关闭</option>
              <option value="rainbow">彩虹（官方闪卡）</option>
              <option value="gold">暖金</option>
              <option value="ice">冷蓝</option>
            </select></div>
          <div class="fx2-row"><span class="fx2-lab">亮度</span>
            <input type="range" id="f2SheenPow" min="0" max="0.6" step="0.02" value="0.25"><span class="fx2-val" id="f2SheenPowV">0.25</span></div>
          <div class="fx2-row"><span class="fx2-lab">周期 (秒)</span>
            <input type="range" id="f2SheenSpd" min="1" max="12" step="0.5" value="4.5"><span class="fx2-val" id="f2SheenSpdV">4.5</span></div>
          <div class="fx2-row"><span class="fx2-lab">角度 (°)</span>
            <input type="range" id="f2SheenAng" min="0" max="180" step="5" value="115"><span class="fx2-val" id="f2SheenAngV">115</span></div>
        </div>
      </div>

      <div class="fx2-sec">
        <div class="fx2-sechead"><b>卡面与遮罩</b><span class="fx2-sub">MainTex / MaskTex</span></div>
        <div class="fx2-secbody">
          <div class="fx2-row"><span class="fx2-lab">本体立绘</span><span class="fx2-hint" id="f2ArtName"></span></div>
          <div class="fx2-row"><span class="fx2-lab">闪卡遮罩</span><span class="fx2-hint" id="f2CardMaskName"></span></div>
          <div class="fx2-row"><span class="fx2-lab">DIY 导入</span>
            <button class="fx2-btn ghost" id="f2ImportArt" type="button">导入立绘</button>
            <button class="fx2-btn ghost" id="f2ImportMask" type="button">导入遮罩</button></div>
          <div class="fx2-row"><span class="fx2-lab">遮罩强度</span>
            <input type="range" id="f2MaskPow" min="0" max="2" step="0.05" value="1"><span class="fx2-val" id="f2MaskPowV">1.00</span></div>
          <div class="fx2-hint">遮罩控制特效在卡面的分布（白=出特效，黑=不出）。导入仅本次会话生效，刷新后恢复官方素材。</div>
        </div>
      </div>

      <div class="fx2-sec">
        <div class="fx2-sechead"><b>背景</b><span class="fx2-sub">卡面四周衬托</span></div>
        <div class="fx2-secbody">
          <div class="fx2-row"><span class="fx2-lab">背景</span>
            <select id="f2BgMode">
              <option value="none">深色（满卡）</option>
              <option value="checker">棋盘格</option>
              <option value="solid">纯色</option>
              <option value="art">当前卡面（暗）</option>
            </select>
            <input type="color" id="f2BgColor" value="#20242e"></div>
          <div class="fx2-hint">选非「满卡」时卡面四周留 5% 边距，露出背景，方便判断边缘光/透明区域。不影响卡内渲染。</div>
        </div>
      </div>

      <div class="fx2-savebar" style="margin-top:12px">
        <button class="fx2-btn ghost" id="f2AddSlot" type="button">＋ 添加特效槽</button>
        <button class="fx2-btn ghost" id="f2OrderReset" type="button">↺ 恢复官方顺序</button>
      </div>
      <div id="f2Slots"></div>

      <div class="fx2-sec">
        <div class="fx2-sechead"><b>扭曲层 Distortion</b><span class="fx2-sub">RG 位移折射</span></div>
        <div class="fx2-secbody" id="f2Dist"></div>
      </div>

      <div class="fx2-hint">每槽参数全部可调；贴图选择器列出全部官方 foil 贴图。</div>
    </div>
  </div>

  <!-- 贴图选择浮层 -->
  <div class="fx2-pick" id="f2TexGrid">
    <div class="fx2-pickinner">
      <div class="fx2-pickhead">
        <h3>选择特效贴图 <span class="fx2-sub" id="f2TexFor"></span></h3>
        <button class="fx2-btn ghost fx2-close" id="f2TexClose" type="button" title="关闭（Esc）">✕ 关闭</button>
      </div>
      <input type="text" id="f2TexSearch" placeholder="搜索贴图名…">
      <div id="f2TexItems" class="fx2-texitems"></div>
    </div>
  </div>
</div>`;

  /* ---------------- 官方 foil 贴图总库 ---------------- */
  const FOIL_TEX = ['tx_foil_arrow','tx_foil_aura','tx_foil_aura_short','tx_foil_aura_turbulence','tx_foil_aura_turbulence_clearly','tx_foil_aura_turbulence_clearly_Transparent','tx_foil_aura_turbulence_Transparent','tx_foil_bard','tx_foil_bomb','tx_foil_bubble','tx_foil_bubble_big','tx_foil_bubble_large','tx_foil_bubble_simple','tx_foil_bubble_soap','tx_foil_bubble_soap_few','tx_foil_character','tx_foil_circle','tx_foil_cloth_piece','tx_foil_cloud','tx_foil_cloud_strong','tx_foil_confetti','tx_foil_digital_line','tx_foil_digital_line_few','tx_foil_distortion','tx_foil_distortion_add','tx_foil_distortion_line_2','tx_foil_distortion_ring','tx_foil_distortion_ring_constant','tx_foil_distortion_ring_thick','tx_foil_distortion_ring_thick_shift','tx_foil_distortion_swell','tx_foil_dust','tx_foil_dust_light','tx_foil_dust_light_colorful','tx_foil_dust_light_Oblong','tx_foil_dust_little','tx_foil_dust_money','tx_foil_dust_more','tx_foil_dust_paper','tx_foil_dust_paper_few','tx_foil_dust_paper_more','tx_foil_dust_thin','tx_foil_dust_wind','tx_foil_dust_wind_Transparent','tx_foil_explosion','tx_foil_feather','tx_foil_feather_clearly','tx_foil_feather_few','tx_foil_feather_few_Transparent','tx_foil_feather_real','tx_foil_feather_real_few','tx_foil_fire','tx_foil_fireball','tx_foil_fireboll_few','tx_foil_fire_steam','tx_foil_fire_toon','tx_foil_fire_toon_blur','tx_foil_fire_toon_blur_Transparent','tx_foil_fire_toon_Transparent','tx_foil_fire_turbulence','tx_foil_flare','tx_foil_flare_dark','tx_foil_flare_flat','tx_foil_flare_flat_Transparent','tx_foil_flare_glow','tx_foil_flare_glow_Transparent','tx_foil_flare_random','tx_foil_flare_rotate','tx_foil_flare_small','tx_foil_flare_small_dark','tx_foil_flare_small_dark_Transparent','tx_foil_flare_small_Transparent','tx_foil_flare_Transparent','tx_foil_flower_black','tx_foil_flower_colorful','tx_foil_flower_grow','tx_foil_flower_little','tx_foil_flower_orange','tx_foil_flower_strong','tx_foil_flower_toon_Transparent','tx_foil_fluff','tx_foil_fog','tx_foil_fog_deep','tx_foil_fog_detailed','tx_foil_fog_detailed_Transparent','tx_foil_fog_strong','tx_foil_fog_strong_Transparent','tx_foil_fog_toon_Transparent','tx_foil_fractal_noise','tx_foil_fractal_noise_Transparent','tx_foil_glass_alpha','tx_foil_glass_alpha_few','tx_foil_glitter','tx_foil_glitter_few','tx_foil_heart','tx_foil_heart_line','tx_foil_heart_little','tx_foil_impact','tx_foil_impact_Transparent','tx_foil_leaves','tx_foil_leaves_bamboo','tx_foil_leaves_bamboo_few_Transparent','tx_foil_leaves_bamboo_Transparent','tx_foil_leaves_ginkgo_Transparent','tx_foil_leaves_little','tx_foil_leaves_little_few','tx_foil_leaves_maple_Transparent','tx_foil_leaves_simple','tx_foil_leaves_simple_Transparent','tx_foil_leaves_simple_Transparent_few','tx_foil_light','tx_foil_light_smoke','tx_foil_light_smoke_Transparent','tx_foil_light_strong','tx_foil_line','tx_foil_line_long','tx_foil_line_long_rainbow','tx_foil_line_more','tx_foil_line_rainbow','tx_foil_Luminescence','tx_foil_Luminescence_harf','tx_foil_magicball','tx_foil_particle','tx_foil_particle_rainbow','tx_foil_particle_square','tx_foil_particle_square_glow','tx_foil_rainbow','tx_foil_rainbow_dark','tx_foil_rainbow_light','tx_foil_ring','tx_foil_ring_beside','tx_foil_ring_beside_02','tx_foil_ring_beside_03','tx_foil_ring_one','tx_foil_ring_radiation','tx_foil_ring_rainbow','tx_foil_ring_rainbow_thin','tx_foil_ring_rainbow_thin_two','tx_foil_ring_side','tx_foil_ring_thin','tx_foil_roar','tx_foil_rock_brick','tx_foil_rock_Brick_small','tx_foil_rock_Brick_small_2','tx_foil_rock_ice','tx_foil_rock_ice_Detailed','tx_foil_rock_large','tx_foil_rock_large_few','tx_foil_rock_light','tx_foil_rock_medium','tx_foil_rock_small','tx_foil_sandstorm','tx_foil_shockWave','tx_foil_shockWave_big','tx_foil_shockWave_big_Transparent','tx_foil_shockwave_rainbow','tx_foil_shockWave_soft','tx_foil_shockWave_soft_Transparent','tx_foil_smoke','tx_foil_smoke_bulge','tx_foil_smoke_Transparent','tx_foil_snow','tx_foil_snow_transparent','tx_foil_soul','tx_foil_soul_long','tx_foil_soul_thick','tx_foil_spark','tx_foil_spark_dust','tx_foil_spark_more','tx_foil_spark_random','tx_foil_spark_random_transparent','tx_foil_spark_strong','tx_foil_spark_strong_glow','tx_foil_spark_strong_more','tx_foil_spark_triangle','tx_foil_spark_weak','tx_foil_spiral','tx_foil_spiral_line','tx_foil_spiral_line_thick','tx_foil_spiral_Transparent','tx_foil_star','tx_foil_star_handwriting','tx_foil_star_line','tx_foil_star_pop','tx_foil_star_pop_Transparent','tx_foil_star_small','tx_foil_stream','tx_foil_stream_soul','tx_foil_sugarplum_transparent','tx_foil_sugarplum_transparent_small','tx_foil_sweat','tx_foil_thunder','tx_foil_thunder_center','tx_foil_thunder_detailed','tx_foil_thunder_detailed_Transparent','tx_foil_thunder_detailed_Transparent_white','tx_foil_thunder_thick','tx_foil_thunder_thick_clearly','tx_foil_thunder_thick_Transparent','tx_foil_twinkle','tx_foil_twinkle_clearly','tx_foil_wave','tx_foil_white','tx_foil_white_tile','tx_foil_wind','tx_foil_windstorm','tx_foil_wind_polar','tx_foil_wind_thin','tx_foil_wing','tx_foil_wing_focus','tx_foil_wood'];

  const TEX_DIR = "/fx2/assets/foil/";
  const CARD_DIR = "/fx2/assets/card/";
  const slotTexPath = s => s.tex ? (s.tex.indexOf("tx_face") === 0 ? CARD_DIR : TEX_DIR) + s.tex + ".png" : "";
  const distTexPath = d => TEX_DIR + d.tex + ".png";

  /* 统一槽位字段 */
  function normSlot(s) {
    if (s.tx == null) { s.tx = (s.uvScale != null ? s.uvScale : 1); s.ty = s.tx; }
    if (s.ox == null) { s.ox = s.px || 0; s.oy = s.py || 0; }
    if (!s.tint) s.tint = [1, 1, 1];
    if (s.contrast == null) s.contrast = 1;
    if (s.spin == null) s.spin = 0;
    if (s.foff == null) s.foff = 0;
    if (s.lut == null) s.lut = "";
    if (s.area == null) s.area = 0;
    if (s.blink == null) s.blink = 0;
    if (s.bper == null) s.bper = 3.2;
    if (s.bdur == null) s.bdur = 0.12;
    if (s.bpeak == null) s.bpeak = 1;
    if (s.bphase == null) s.bphase = 0;
    return s;
  }
  function normCfg(cfg) {
    if (!cfg) return cfg;
    cfg.slots.forEach(normSlot);
    const d = cfg.dist;
    if (d.tx == null) { d.tx = 1; d.ty = 1; d.ox = 0; d.oy = 0; }
    return cfg;
  }

  /* ---------------- 官方原值预设 ---------------- */
  const PRESETS = {
    sekka10: {
      id: "120141020 · 宿命狐火·雪华", card: "雪华 · 进化前",
      art: CARD_DIR + "1201410200.png", mask: CARD_DIR + "1201410210.png",
      dist: { tex: "tx_foil_distortion", su: -0.20, sv: 0.40, sa: 0, iu: 0.03, iv: 0.03, mt: 2, maskCh: 2 },
      slots: [
        { slot: "Front1", tex: "tx_foil_fog_deep",    color: [0.336,0.28,0.32,0.51], su: -0.42, sv: 0, sa: 0,  rot: 0.3,  spiral: 0,  fixrot: 0,  mt: 2, rt: 3, flash: 0, fspd: 1, fmin: 0, fmax: 1, maskCh: 0, maskStr: 1, uvScale: 1, px: 0, py: 0, en: 1 },
        { slot: "Front2", tex: "tx_foil_fog_strong",  color: [0.288,0.262,0.294,1.0], su: -0.20, sv: 0, sa: 0,  rot: -0.15,spiral: 0,  fixrot: 0,  mt: 2, rt: 1, flash: 0, fspd: 1, fmin: 0, fmax: 1, maskCh: 0, maskStr: 1, uvScale: 1, px: 0, py: 0, en: 1 },
        { slot: "Middle", tex: "tx_face_1201410210",  color: [0.5,0.5,0.5,1], su: 0, sv: 0, sa: 0, rot: 0, spiral: 0, fixrot: 0, mt: 1, rt: 3, flash: 0, fspd: 2, fmin: 0.001, fmax: 0.0015, maskCh: 4, maskStr: 1, tx: 6.4, ty: 6.4, ox: -3.1, oy: -3.788, en: 1,
          blink: 1, bper: 3.14, bdur: 0.12, bpeak: 1 },
        { slot: "Back1", tex: "tx_foil_flower_little",color: [0.651,0.12,0.217,1.0], su: -0.40, sv: 0, sa: 0,  rot: 0.25, spiral: 0, fixrot: 9.7, mt: 3, rt: 3, flash: 0, fspd: 1, fmin: 0, fmax: 1, maskCh: 1, maskStr: 1, uvScale: 1, px: 0, py: 0, en: 1 },
        { slot: "Back2", tex: "tx_foil_flower_little",color: [0.604,0.071,0.102,1.0],su: -0.20, sv: 0, sa: 0,  rot: -0.10,spiral: 0, fixrot: 0,  mt: 3, rt: 3, flash: 0, fspd: 1, fmin: 0, fmax: 1, maskCh: 1, maskStr: 1, uvScale: 1, px: 0, py: 0, en: 1 },
      ]
    },
    sekka11: {
      id: "120141020 · 宿命狐火·雪华", card: "雪华 · 进化后",
      art: CARD_DIR + "1201410201.png", mask: CARD_DIR + "1201410211.png",
      dist: { tex: "tx_foil_distortion", su: 0.23, sv: 0.43, sa: 0, iu: 0.03, iv: 0.03, mt: 2, maskCh: 2 },
      slots: [
        { slot: "Front1", tex: "tx_foil_flower_little", color: [0.231,0.202,0.679,1.0], su: 0.88, sv: -0.38, sa: -88, rot: -0.32, spiral: 0,  fixrot: 0,  mt: 3, rt: 3, flash: 0, fspd: 1, fmin: 0, fmax: 1, maskCh: 0, maskStr: 1, uvScale: 1, px: 0, py: 0, en: 1 },
        { slot: "Front2", tex: "tx_foil_flower_little", color: [0.071,0.1,0.604,1.0],   su: 0.58, sv: -0.28, sa: -78, rot: 0.17,  spiral: 0,  fixrot: 0,  mt: 3, rt: 3, flash: 0, fspd: 1, fmin: 0, fmax: 1, maskCh: 0, maskStr: 1, uvScale: 1, px: 0, py: 0, en: 1 },
        { slot: "Middle", tex: "",                      color: [0.5,0.5,0.5,1.0],      su: 0,    sv: 0,    sa: 0,   rot: 0,    spiral: 0,  fixrot: 0,  mt: 1, rt: 1, flash: 0, fspd: 1, fmin: 0, fmax: 1, maskCh: 4, maskStr: 1, uvScale: 1, px: 0, py: 0, en: 0 },
        { slot: "Back1", tex: "tx_foil_Luminescence",   color: [0.261,0.413,0.455,1.0],su: 0,    sv: 0.30, sa: 0,   rot: 0,    spiral: 0,  fixrot: 0,  mt: 4, rt: 1, flash: 0, fspd: 1, fmin: 0, fmax: 1, maskCh: 1, maskStr: 1, uvScale: 1, px: 0, py: 0, en: 1 },
        { slot: "Back2", tex: "tx_foil_Luminescence",   color: [0.221,0.242,0.329,1.0],su: 0,    sv: 0.30, sa: 0,   rot: 0,    spiral: 0,  fixrot: 0,  mt: 4, rt: 1, flash: 0, fspd: 1, fmin: 0, fmax: 1, maskCh: 1, maskStr: 1, uvScale: 1, px: 0, py: 0, en: 1 },
      ]
    },
    arrow10: {
      id: "900144090 · 疾风之箭", card: "疾风之箭",
      art: CARD_DIR + "9001440900.png", mask: CARD_DIR + "9001440910.png",
      dist: { tex: "tx_foil_distortion", su: 0.02, sv: -0.37, sa: -75, iu: 0.10, iv: 0.10, mt: 2, maskCh: 2 },
      slots: [
        { slot: "Front1", tex: "tx_foil_fire_toon_Transparent", color: [0.141,0.368,0.089,1.0], su: -0.02, sv: 0.42, sa: 0,  rot: 0,   spiral: -0.30, fixrot: 0, mt: 4, rt: 3, flash: 0, fspd: 1, fmin: 0, fmax: 1, maskCh: 0, maskStr: 1, uvScale: 1, px: 0, py: 0, en: 1 },
        { slot: "Front2", tex: "tx_foil_aura_turbulence_clearly",color: [0.504,0.818,0.191,1.0], su: 0.02,  sv: 0.60, sa: 0,  rot: 0,   spiral: 0,     fixrot: 0, mt: 4, rt: 1, flash: 0, fspd: 1, fmin: 0, fmax: 1, maskCh: 0, maskStr: 1, uvScale: 1, px: 0, py: 0, en: 1 },
        { slot: "Middle", tex: "",                               color: [0.5,0.5,0.5,1.0],       su: 0,     sv: 0,    sa: 0,  rot: 0,   spiral: 0,     fixrot: 0, mt: 1, rt: 1, flash: 0, fspd: 1, fmin: 0, fmax: 1, maskCh: 4, maskStr: 1, uvScale: 1, px: 0, py: 0, en: 0 },
        { slot: "Back1", tex: "tx_foil_smoke",                   color: [0.309,0.293,0.357,1.0], su: 0.15,  sv: -0.03,sa: -35, rot: 0,   spiral: 0,     fixrot: 0, mt: 2, rt: 2, flash: 0, fspd: 1, fmin: 0, fmax: 1, maskCh: 1, maskStr: 1, uvScale: 1, px: 0, py: 0, en: 1 },
        { slot: "Back2", tex: "tx_foil_smoke",                   color: [0.65,0.585,0.364,1.0],  su: 0.12,  sv: 0.02, sa: 45,  rot: 0,   spiral: 0,     fixrot: 0, mt: 2, rt: 1, flash: 0, fspd: 1, fmin: 0, fmax: 1, maskCh: 1, maskStr: 1, uvScale: 1, px: 0, py: 0, en: 1 },
      ]
    }
  };

  /* 官方 Tiling/Offset（RenderDoc 抓帧真值） */
  (function applyOfficialST() {
    const T = {
      arrow10: { Front1: [1.2,1.44,0.54,0.6936], Front2: [0.9,1.08,0.405,0.5202],
                 Back1: [1.5,1.5,0,0], Back2: [0.8,0.8,0,0] },
      sekka10: { Front1: [1.1,1.5,0,-4.62617], Front2: [0.7316,1.27,0,-2.94532],
                 Back1: [1.65675,1.88,-0.81966,-2.162] },
      sekka11: { Front1: [1.41,1.6,0.69758,-1.84], Front2: [1.28,1.28,-1.0368,3.2],
                 Back1: [-4.41664,-5.3,-1.56,1.79647], Back2: [3.74998,4.5,1.87,-1.54409] }
    };
    const D = { arrow10: [0.6,0.6,0,0], sekka11: [0.8,0.8,0,0] };
    Object.keys(T).forEach(k => {
      PRESETS[k].slots.forEach(s => {
        const t = T[k][s.slot];
        if (t) { s.tx = t[0]; s.ty = t[1]; s.ox = t[2]; s.oy = t[3]; }
      });
    });
    Object.keys(D).forEach(k => {
      const d = PRESETS[k].dist, t = D[k];
      d.tx = t[0]; d.ty = t[1]; d.ox = t[2]; d.oy = t[3];
    });
  })();

  /* ---------------- WebGL 渲染核心 ---------------- */
  const SLOT_KEYS = ["Front1","Front2","Middle","Back1","Back2"];
  const SLOT_TAGS = { Front1:"上层①", Front2:"上层②", Middle:"面部", Back1:"底层①", Back2:"底层②" };
  const SLOT_ORDER = { Back1:0, Back2:1, Middle:2, Front1:3, Front2:4 };
  function layerOf(s) { return s._layer != null ? s._layer : (SLOT_ORDER[s.slot] != null ? SLOT_ORDER[s.slot] : 9); }
  function orderedIdx(cfg) {
    return cfg.slots.map((_, i) => i)
      .sort((a, b) => layerOf(cfg.slots[a]) - layerOf(cfg.slots[b]) || a - b);
  }

  const VS = `#version 300 es
layout(location=0) in vec2 aPos;
out vec2 vUv;
void main(){
  vUv = aPos * 0.5 + 0.5;
  gl_Position = vec4(aPos, 0.0, 1.0);
}
`;

  const VS_POST = `#version 300 es
layout(location=0) in vec2 aPos;
out vec2 vUv;
void main(){
  vUv = aPos * 0.5 + 0.5;
  gl_Position = vec4(aPos, 0.0, 1.0);
}
`;

  const FS = `#version 300 es
precision highp float;
uniform float uTime;
uniform sampler2D uMain, uMask, uDist;
uniform vec4 uDistCtl;
uniform vec4 uDistCtl2;
uniform vec4 uGlob;
uniform vec4 uGlob2;
uniform vec4 uDistTile;
uniform sampler2D uTex0, uTex1, uTex2, uTex3, uTex4;
uniform vec4 uSlot0, uSlot1, uSlot2, uSlot3, uSlot4;
uniform vec4 uTile0, uTile1, uTile2, uTile3, uTile4;
uniform vec4 uMove0, uMove1, uMove2, uMove3, uMove4;
uniform vec4 uFx0, uFx1, uFx2, uFx3, uFx4;
uniform vec4 uCtl0, uCtl1, uCtl2, uCtl3, uCtl4;
uniform vec4 uF0, uF1, uF2, uF3, uF4;
uniform vec4 uPos0, uPos1, uPos2, uPos3, uPos4;
uniform vec4 uTint0, uTint1, uTint2, uTint3, uTint4;
uniform sampler2D uLut0, uLut1, uLut2, uLut3, uLut4;
uniform vec4 uLx0, uLx1, uLx2, uLx3, uLx4;
uniform vec4 uBlink0, uBlink1, uBlink2, uBlink3, uBlink4;
in vec2 vUv;
out vec4 fragColor;

const float PI = 3.14159265359;
const float INV2PI = 0.15915494309;
mat2 rotM(float a){ float c=cos(a), s=sin(a); return mat2(c,s,-s,c); }

float maskChVal(vec2 uv, float ch){
  vec4 m = texture(uMask, uv);
  float v;
  if (ch < 0.5) v = m.r;
  else if (ch < 1.5) v = m.g;
  else if (ch < 2.5) v = m.b;
  else if (ch < 3.5) v = m.a;
  else if (ch < 4.5) v = 1.0;
  else v = 1.0 - m.r;
  return clamp(v, 0.0, 1.0);
}

float areaVal(vec2 uv, float ty){
  if (ty < 0.5) return 1.0;
  if (ty < 1.5){
    float d = 1.0 - uv.y;
    if (d < 0.18) return mix(1.0, 0.5, d / 0.18);
    if (d < 0.44) return mix(0.5, 0.12, (d - 0.18) / 0.26);
    if (d < 0.70) return mix(0.12, 0.0, (d - 0.44) / 0.26);
    return 0.0;
  }
  if (ty < 2.5){
    float d = uv.y;
    if (d < 0.16) return mix(1.0, 0.92, d / 0.16);
    if (d < 0.40) return mix(0.92, 0.55, (d - 0.16) / 0.24);
    if (d < 0.64) return mix(0.55, 0.16, (d - 0.40) / 0.24);
    if (d < 0.86) return mix(0.16, 0.0, (d - 0.64) / 0.22);
    return 0.0;
  }
  vec2 e = vec2((uv.x - 0.5) / 0.64, (uv.y - 0.44) / 0.54);
  float r = length(e);
  if (r < 0.42) return mix(1.0, 0.7, r / 0.42);
  if (r < 0.70) return mix(0.7, 0.22, (r - 0.42) / 0.28);
  if (r < 0.92) return mix(0.22, 0.0, (r - 0.70) / 0.22);
  return 0.0;
}

vec4 slotUV(vec4 mv, vec4 fx, float mt, vec4 tile, vec2 uv, float te){
  vec2 T = vec2(tile.x == 0.0 ? 1e-4 : tile.x, tile.y == 0.0 ? 1e-4 : tile.y);
  vec2 O = tile.zw;
  vec2 tuv = uv * T + O;
  if (mt > 3.5) return vec4(tuv, fract(-mv.y * te), fract(-mv.x * te));
  if (mt > 2.5) { vec2 c = tuv - T * 0.5; return vec4(rotM(mv.w * te) * c + T * 0.5, 0.0, 0.0); }
  if (mt > 1.5) {
    vec2 r = rotM(-mv.z * PI / 180.0) * (tuv / T) * T;
    return vec4(r + vec2(fract(-mv.x * te), fract(-mv.y * te)), 0.0, 0.0);
  }
  return vec4(rotM(-fx.y) * tuv, 0.0, 0.0);
}

vec2 polarUV(vec4 muv, float spiral, vec4 tile){
  vec2 T = tile.xy;
  vec2 d = T * 0.5 - muv.xy;
  float r = length(d);
  float ang = atan(d.y, d.x) - r * spiral + PI;
  return vec2(r * INV2PI + muv.z, ang * INV2PI + muv.w);
}

vec3 blend(sampler2D tex, sampler2D lutT, vec4 col, vec4 mv, vec4 fx, vec4 ctl, vec4 f,
           vec4 tile, vec4 pos, vec4 tint, vec4 lx, vec4 bl, vec2 uv, float te, vec3 acc){
  if (ctl.w < 0.5) return acc;
  vec4 muv = slotUV(mv, fx, ctl.x, tile, uv, te);
  vec2 suv = (ctl.x > 3.5) ? polarUV(muv, fx.x, tile) : muv.xy;
  vec4 tx = texture(tex, suv);

  tx.rgb = (tx.rgb - 0.5) * tint.w + 0.5;
  tx.rgb *= tint.rgb;
  if (lx.x > 0.5){
    float l = clamp(dot(tx.rgb, vec3(0.213, 0.715, 0.072)), 0.0, 1.0);
    tx.rgb = texture(lutT, vec2(l, 0.5)).rgb;
  }

  float g = maskChVal(uv, f.y) * f.z * uDistCtl2.w * areaVal(uv, lx.y);
  float a = tx.a * col.a * g;
  float st = uGlob.x;

  if (ctl.z > 0.5){
    float w = 0.5 + 0.5 * sin(2.0 * PI * (f.x * te + pos.w));
    float lo = fx.z, hi = fx.w;
    if (pos.z > 0.5){ lo *= uGlob2.x; hi *= uGlob2.x; }
    a *= lo + (hi - lo) * w;
  }
  if (bl.x > 0.5){
    if (any(lessThan(suv, vec2(0.0))) || any(greaterThan(suv, vec2(1.0)))) return acc;
    float per = max(bl.y, 0.05);
    float halfD = clamp(bl.z * 0.5 / per, 0.001, 0.3);
    float ph = fract((te + bl.w) / per);
    float pulse = smoothstep(0.0, halfD, ph) * (1.0 - smoothstep(halfD, halfD * 2.0, ph));
    float blinkA = smoothstep(0.10, 0.80, tx.a) * clamp(f.w, 0.0, 1.0) * pulse;
    return mix(acc, tx.rgb, blinkA);
  }
  vec3 cMask  = tx.rgb * (col.rgb * g) * 2.0 * st;
  vec3 cPlain = tx.rgb * col.rgb * 2.0 * st;
  if (ctl.y > 2.5) return mix(acc, cPlain, a);
  if (ctl.y > 1.5) return acc - cMask;
  return acc + cMask;
}

void main(){
  float t = uTime;
  float te = t * uGlob2.y;
  vec2 uv = vUv;
  vec2 disp = vec2(0.0);
  if (uDistCtl2.y > 0.5){
    float dmt = uDistCtl2.y;
    vec4 muv = slotUV(vec4(uDistCtl.xy, uDistCtl.z, 0.0), vec4(0.0), dmt, uDistTile, uv, te);
    vec2 duv = (dmt > 3.5) ? polarUV(muv, 0.0, uDistTile) : muv.xy;
    vec2 d = texture(uDist, duv).rg - 0.5;
    disp = clamp(d * vec2(uDistCtl.w, uDistCtl2.x), -0.06, 0.06);
    disp *= maskChVal(uv, uGlob2.w);
  }
  vec2 fuv = uv + disp;

  vec3 col = texture(uMain, fuv).rgb;
  col = blend(uTex0, uLut0, uSlot0, uMove0, uFx0, uCtl0, uF0, uTile0, uPos0, uTint0, uLx0, uBlink0, fuv, te, col);
  col = blend(uTex1, uLut1, uSlot1, uMove1, uFx1, uCtl1, uF1, uTile1, uPos1, uTint1, uLx1, uBlink1, fuv, te, col);
  col = blend(uTex2, uLut2, uSlot2, uMove2, uFx2, uCtl2, uF2, uTile2, uPos2, uTint2, uLx2, uBlink2, fuv, te, col);
  col = blend(uTex3, uLut3, uSlot3, uMove3, uFx3, uCtl3, uF3, uTile3, uPos3, uTint3, uLx3, uBlink3, fuv, te, col);
  col = blend(uTex4, uLut4, uSlot4, uMove4, uFx4, uCtl4, uF4, uTile4, uPos4, uTint4, uLx4, uBlink4, fuv, te, col);

  if (uDistCtl2.z > 0.5){
    vec4 m = texture(uMask, uv);
    col = mix(col, m.rgb * 1.4, 0.85);
  }
  fragColor = vec4(col, 1.0);
}
`;

  const TM_FS = `#version 300 es
precision highp float;
uniform sampler2D uHdr;
uniform vec4 uTone;
uniform sampler2D uBloom;
uniform vec2 uTexel;
uniform vec4 uSheen;
uniform vec4 uSheenA, uSheenB, uSheenC, uSheenD;
in vec2 vUv;
out vec4 fragColor;
void main(){
  vec3 c = texture(uHdr, vUv).rgb * uTone.z;
  float knee = clamp(uTone.x, 0.05, 0.999);
  if (uTone.y > 0.5){
    vec3 over = max(c - knee, 0.0) / (1.0 - knee);
    c = min(c, vec3(knee)) + (1.0 - knee) * (over / (over + 1.0));
  }
  if (uTone.w > 0.0){
    vec3 bl = texture(uBloom, vUv).rgb;
    c += bl * uTone.w;
  }
  if (uSheen.w > 0.5 && uSheen.y > 0.0){
    vec2 dir = vec2(cos(uSheen.z), sin(uSheen.z));
    float q = dot(vUv - 0.5, dir) / 0.85 + 0.5;
    float band = smoothstep(0.34, 0.0, abs(q - uSheen.x));
    float k = clamp((q - (uSheen.x - 0.34)) / 0.68, 0.0, 1.0);
    vec3 sc;
    if (k < 0.25)      sc = mix(uSheenA.rgb, uSheenB.rgb, k / 0.25);
    else if (k < 0.5)  sc = mix(uSheenB.rgb, uSheenC.rgb, (k - 0.25) / 0.25);
    else if (k < 0.75) sc = mix(uSheenC.rgb, uSheenD.rgb, (k - 0.5) / 0.25);
    else               sc = mix(uSheenD.rgb, uSheenA.rgb, (k - 0.75) / 0.25);
    c += sc * band * uSheen.y;
  }
  fragColor = vec4(clamp(c, 0.0, 1.0), 1.0);
}
`;

  const BL_FS = `#version 300 es
precision highp float;
uniform sampler2D uSrc;
uniform vec2 uTexel;
uniform vec2 uDir;
uniform float uPass;
in vec2 vUv;
out vec4 fragColor;
void main(){
  if (uPass < 0.5){
    vec3 c = texture(uSrc, vUv).rgb;
    float l = max(max(c.r, c.g), c.b);
    c = c * clamp((l - 0.75) / 0.75, 0.0, 1.0);
    fragColor = vec4(c, 1.0);
    return;
  }
  vec3 s = texture(uSrc, vUv).rgb * 0.227027;
  s += (texture(uSrc, vUv + uDir * uTexel * 1.384615).rgb + texture(uSrc, vUv - uDir * uTexel * 1.384615).rgb) * 0.316216;
  s += (texture(uSrc, vUv + uDir * uTexel * 3.230769).rgb + texture(uSrc, vUv - uDir * uTexel * 3.230769).rgb) * 0.070270;
  fragColor = vec4(s, 1.0);
}
`;

  const LUT_TABLE = {
    fire:    ["0 0.35 0.85 1 1", "0 0.02 0.25 0.65 0.95", "0 0 0.02 0.1 0.7"],
    ice:     ["0 0.1 0.3 0.6 1", "0 0.4 0.7 0.9 1", "0 0.7 0.9 1 1"],
    gold:    ["0 0.3 0.7 1 1", "0 0.15 0.45 0.85 1", "0 0 0.05 0.3 0.85"],
    magic:   ["0 0.2 0.6 0.9 1", "0 0 0.1 0.5 0.95", "0 0.3 0.8 1 1"],
    official:["0.031 0.078 0.165 0.251 0.325", "0.027 0.082 0.184 0.329 0.490", "0.165 0.235 0.369 0.557 0.769"]
  };
  const LUT_CN = { "":"无", fire:"火焰", ice:"寒冰", gold:"黄金", magic:"魔法", official:"官方蓝" };
  const AREA_CN = { 0:"全卡", 1:"上部", 2:"下部", 3:"中部" };
  const SHEEN_PALETTE = {
    rainbow:[[1,0.31,0.31],[1,0.90,0.35],[0.35,1,0.55],[0.31,0.71,1]],
    gold:   [[1,0.75,0.35],[1,0.93,0.75],[1,0.59,0.24],[1,0.86,0.59]],
    ice:    [[0.47,0.78,1],[0.82,0.96,1],[0.35,0.63,1],[0.88,0.98,1]]
  };

  class CardGL {
    constructor(canvas) {
      this.gl = canvas.getContext("webgl2", { antialias:true, alpha:false, premultipliedAlpha:false });
      if (!this.gl) throw new Error("需要 WebGL2 支持");
      this.gl.getExtension("EXT_color_buffer_float");
      this.texCache = {};
      this.lutCache = {};
      this.artTex = null; this.maskTex = null; this.distTex = null;
      this.compile();
      this.time = 0;
      this.config = null;
    }
    compile() {
      const gl = this.gl;
      const sh = (type, src) => {
        const s = gl.createShader(type); gl.shaderSource(s, src); gl.compileShader(s);
        if (!gl.getShaderParameter(s, gl.COMPILE_STATUS)) throw new Error(gl.getShaderInfoLog(s));
        return s;
      };
      const mk = (fsSrc, vsSrc) => {
        const p = gl.createProgram();
        gl.attachShader(p, sh(gl.VERTEX_SHADER, vsSrc || VS));
        gl.attachShader(p, sh(gl.FRAGMENT_SHADER, fsSrc));
        gl.linkProgram(p);
        if (!gl.getProgramParameter(p, gl.LINK_STATUS)) throw new Error(gl.getProgramInfoLog(p));
        return p;
      };
      this.prog   = mk(FS);
      this.blProg = mk(BL_FS, VS_POST);
      this.tmProg = mk(TM_FS, VS_POST);
      gl.useProgram(this.prog);
      this.vbo = gl.createBuffer();
      gl.bindBuffer(gl.ARRAY_BUFFER, this.vbo);
      gl.bufferData(gl.ARRAY_BUFFER, new Float32Array([-1,-1, 1,-1, -1,1, 1,1]), gl.STATIC_DRAW);
      gl.enableVertexAttribArray(0);
      gl.vertexAttribPointer(0, 2, gl.FLOAT, false, 0, 0);
      this.u = {
        time: gl.getUniformLocation(this.prog, "uTime"),
        uDistCtl: gl.getUniformLocation(this.prog, "uDistCtl"),
        uDistCtl2: gl.getUniformLocation(this.prog, "uDistCtl2"),
        uGlob: gl.getUniformLocation(this.prog, "uGlob"),
        uGlob2: gl.getUniformLocation(this.prog, "uGlob2"),
        uDistTile: gl.getUniformLocation(this.prog, "uDistTile"),
        slot: [0,1,2,3,4].map(i => gl.getUniformLocation(this.prog, "uSlot" + i)),
        tile: [0,1,2,3,4].map(i => gl.getUniformLocation(this.prog, "uTile" + i)),
        move: [0,1,2,3,4].map(i => gl.getUniformLocation(this.prog, "uMove" + i)),
        fx:   [0,1,2,3,4].map(i => gl.getUniformLocation(this.prog, "uFx" + i)),
        ctl:  [0,1,2,3,4].map(i => gl.getUniformLocation(this.prog, "uCtl" + i)),
        f:    [0,1,2,3,4].map(i => gl.getUniformLocation(this.prog, "uF" + i)),
        pos:  [0,1,2,3,4].map(i => gl.getUniformLocation(this.prog, "uPos" + i)),
        tint: [0,1,2,3,4].map(i => gl.getUniformLocation(this.prog, "uTint" + i)),
        lut:  [0,1,2,3,4].map(i => gl.getUniformLocation(this.prog, "uLut" + i)),
        lx:   [0,1,2,3,4].map(i => gl.getUniformLocation(this.prog, "uLx" + i)),
        blink:[0,1,2,3,4].map(i => gl.getUniformLocation(this.prog, "uBlink" + i))
      };
      gl.uniform1i(gl.getUniformLocation(this.prog, "uMain"), 0);
      gl.uniform1i(gl.getUniformLocation(this.prog, "uMask"), 1);
      gl.uniform1i(gl.getUniformLocation(this.prog, "uDist"), 2);
      [0,1,2,3,4].forEach(i => gl.uniform1i(gl.getUniformLocation(this.prog, "uTex" + i), 3 + i));
      [0,1,2,3,4].forEach(i => gl.uniform1i(gl.getUniformLocation(this.prog, "uLut" + i), 8 + i));
      gl.useProgram(this.blProg);
      this.blU = { src: gl.getUniformLocation(this.blProg,"uSrc"), texel: gl.getUniformLocation(this.blProg,"uTexel"),
                   dir: gl.getUniformLocation(this.blProg,"uDir"), pass: gl.getUniformLocation(this.blProg,"uPass") };
      gl.uniform1i(this.blU.src, 0);
      gl.useProgram(this.tmProg);
      this.tmU = { hdr: gl.getUniformLocation(this.tmProg,"uHdr"), bloom: gl.getUniformLocation(this.tmProg,"uBloom"),
                   texel: gl.getUniformLocation(this.tmProg,"uTexel"), tone: gl.getUniformLocation(this.tmProg,"uTone"),
                   sheen: gl.getUniformLocation(this.tmProg,"uSheen"),
                   sheenA: gl.getUniformLocation(this.tmProg,"uSheenA"), sheenB: gl.getUniformLocation(this.tmProg,"uSheenB"),
                   sheenC: gl.getUniformLocation(this.tmProg,"uSheenC"), sheenD: gl.getUniformLocation(this.tmProg,"uSheenD") };
      gl.uniform1i(this.tmU.hdr, 0);
      gl.uniform1i(this.tmU.bloom, 1);
      this.hdrReady = false;
    }
    initHDR(w, h) {
      const gl = this.gl;
      if (this.hdrW === w && this.hdrH === h) return;
      const bw = Math.max(1, w >> 1), bh = Math.max(1, h >> 1);
      const mkTex = (tw, th) => {
        const t = gl.createTexture();
        gl.bindTexture(gl.TEXTURE_2D, t);
        gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA16F, tw, th, 0, gl.RGBA, gl.FLOAT, null);
        gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.LINEAR);
        gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.LINEAR);
        gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE);
        gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);
        return t;
      };
      const mkFbo = tex => {
        const f = gl.createFramebuffer();
        gl.bindFramebuffer(gl.FRAMEBUFFER, f);
        gl.framebufferTexture2D(gl.FRAMEBUFFER, gl.COLOR_ATTACHMENT0, gl.TEXTURE_2D, tex, 0);
        const ok = gl.checkFramebufferStatus(gl.FRAMEBUFFER) === gl.FRAMEBUFFER_COMPLETE;
        gl.bindFramebuffer(gl.FRAMEBUFFER, null);
        return ok ? f : null;
      };
      if (this.hdrTex) {
        [this.hdrTex, this.blA, this.blB].forEach(t => t && gl.deleteTexture(t));
        [this.fboHDR, this.fboA, this.fboB].forEach(f => f && gl.deleteFramebuffer(f));
      }
      this.hdrW = w; this.hdrH = h; this.blW = bw; this.blH = bh;
      this.hdrTex = mkTex(w, h); this.blA = mkTex(bw, bh); this.blB = mkTex(bw, bh);
      this.fboHDR = mkFbo(this.hdrTex); this.fboA = mkFbo(this.blA); this.fboB = mkFbo(this.blB);
      this.hdrReady = !!(this.fboHDR && this.fboA && this.fboB);
    }
    loadTex(name, unitIdx, wrapRepeat, fallback) {
      const gl = this.gl;
      if (name === "") {
        gl.activeTexture(gl.TEXTURE0 + unitIdx); gl.bindTexture(gl.TEXTURE_2D, null); return null;
      }
      if (this.texCache[name]) { this.bind(name, unitIdx, wrapRepeat); return this.texCache[name]; }
      const img = new Image();
      const t = gl.createTexture();
      this.texCache[name] = t;
      const fillSolid = px => {
        gl.bindTexture(gl.TEXTURE_2D, t);
        gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA, 1, 1, 0, gl.RGBA, gl.UNSIGNED_BYTE, new Uint8Array(px));
        gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, wrapRepeat ? gl.REPEAT : gl.CLAMP_TO_EDGE);
        gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, wrapRepeat ? gl.REPEAT : gl.CLAMP_TO_EDGE);
        gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.LINEAR);
        gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.LINEAR);
        gl.bindTexture(gl.TEXTURE_2D, null);
      };
      img.onload = () => {
        gl.bindTexture(gl.TEXTURE_2D, t);
        gl.pixelStorei(gl.UNPACK_FLIP_Y_WEBGL, true);
        gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA, gl.RGBA, gl.UNSIGNED_BYTE, img);
        gl.pixelStorei(gl.UNPACK_FLIP_Y_WEBGL, false);
        gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, wrapRepeat ? gl.REPEAT : gl.CLAMP_TO_EDGE);
        gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, wrapRepeat ? gl.REPEAT : gl.CLAMP_TO_EDGE);
        gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.LINEAR_MIPMAP_LINEAR);
        gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.LINEAR);
        gl.generateMipmap(gl.TEXTURE_2D);
        gl.bindTexture(gl.TEXTURE_2D, null);
      };
      img.onerror = () => { if (fallback) fillSolid(fallback); };
      img.src = name;
      return t;
    }
    lutTex(name) {
      const gl = this.gl;
      if (name === "") return null;
      if (this.lutCache[name]) return this.lutCache[name];
      const tab = (LUT_TABLE[name] || []).map(s => s.trim().split(/\s+/).map(Number));
      if (tab.length < 3) return null;
      const n = 256, data = new Uint8Array(n * 4);
      const interp = (stops, x) => {
        const f = Math.min(0.99999, Math.max(0, x)) * (stops.length - 1);
        const i = Math.floor(f), k = f - i;
        return (stops[i] + (stops[Math.min(i + 1, stops.length - 1)] - stops[i]) * k) * 255;
      };
      for (let x = 0; x < n; x++) {
        data[x*4] = Math.round(interp(tab[0], x / (n - 1)));
        data[x*4+1] = Math.round(interp(tab[1], x / (n - 1)));
        data[x*4+2] = Math.round(interp(tab[2], x / (n - 1)));
        data[x*4+3] = 255;
      }
      const t = gl.createTexture();
      gl.bindTexture(gl.TEXTURE_2D, t);
      gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA, n, 1, 0, gl.RGBA, gl.UNSIGNED_BYTE, data);
      gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.LINEAR);
      gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.LINEAR);
      gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE);
      gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);
      gl.bindTexture(gl.TEXTURE_2D, null);
      this.lutCache[name] = t;
      return t;
    }
    bind(name, unitIdx) {
      const gl = this.gl;
      gl.activeTexture(gl.TEXTURE0 + unitIdx);
      gl.bindTexture(gl.TEXTURE_2D, this.texCache[name]);
    }
    setConfig(cfg) {
      normCfg(cfg);
      this.config = cfg;
      this.loadTex(cfg.art, 0, false, [18, 22, 32, 255]);
      this.loadTex(cfg.mask, 1, false, [255, 255, 255, 255]);
      this.loadTex(distTexPath(cfg.dist), 2, true, [128, 128, 128, 255]);
      cfg.slots.forEach((s, i) => this.loadTex(slotTexPath(s), 3 + i, s.slot !== "Middle", [0, 0, 0, 0]));
    }
    render(t, globals) {
      const gl = this.gl, cfg = this.config;
      if (!cfg) return;
      const W = gl.drawingBufferWidth, H = gl.drawingBufferHeight;
      this.initHDR(W, H);
      const maskViz = globals.maskViz ? 1 : 0;
      const distPow = globals.distPow, fxPow = globals.fxPow, maskPow = globals.maskPow;
      const ord = orderedIdx(cfg);

      gl.bindFramebuffer(gl.FRAMEBUFFER, this.hdrReady ? this.fboHDR : null);
      gl.viewport(0, 0, W, H);
      gl.useProgram(this.prog);
      this.bind(cfg.art, 0);
      this.bind(cfg.mask, 1);
      this.bind(distTexPath(cfg.dist), 2);
      ord.forEach((src, i) => {
        const s = cfg.slots[src];
        const p = slotTexPath(s);
        if (p) this.bind(p, 3 + i);
        else { gl.activeTexture(gl.TEXTURE0 + 3 + i); gl.bindTexture(gl.TEXTURE_2D, null); }
        const lut = s.lut ? this.lutTex(s.lut) : null;
        gl.activeTexture(gl.TEXTURE0 + 8 + i);
        gl.bindTexture(gl.TEXTURE_2D, lut);
        gl.uniform4f(this.u.lx[i], lut ? 1 : 0, s.area || 0, 0, 0);
      });
      for (let i = ord.length; i < 5; i++) {
        gl.activeTexture(gl.TEXTURE0 + 3 + i);
        gl.bindTexture(gl.TEXTURE_2D, null);
        gl.activeTexture(gl.TEXTURE0 + 8 + i);
        gl.bindTexture(gl.TEXTURE_2D, null);
        gl.uniform4f(this.u.lx[i], 0, 0, 0, 0);
        gl.uniform4f(this.u.blink[i], 0, 0, 0, 0);
        gl.uniform4f(this.u.ctl[i], 1, 1, 0, 0);
        gl.uniform4f(this.u.slot[i], 0, 0, 0, 0);
        gl.uniform4f(this.u.tile[i], 1, 1, 0, 0);
        gl.uniform4f(this.u.move[i], 0, 0, 0, 0);
        gl.uniform4f(this.u.fx[i], 0, 0, 0, 1);
        gl.uniform4f(this.u.f[i], 1, 4, 0, 0);
        gl.uniform4f(this.u.pos[i], 0, 0, 0, 0);
        gl.uniform4f(this.u.tint[i], 1, 1, 1, 1);
      }
      gl.uniform1f(this.u.time, t);
      gl.uniform4f(this.u.uDistCtl, cfg.dist.su, cfg.dist.sv, cfg.dist.sa, cfg.dist.iu * distPow);
      gl.uniform4f(this.u.uDistCtl2, cfg.dist.iv * distPow, cfg.dist.mt || 0, maskViz, maskPow);
      gl.uniform4f(this.u.uDistTile, cfg.dist.tx != null ? cfg.dist.tx : 1, cfg.dist.ty != null ? cfg.dist.ty : 1, cfg.dist.ox || 0, cfg.dist.oy || 0);
      gl.uniform4f(this.u.uGlob, fxPow, 1, 0, 0);
      gl.uniform4f(this.u.uGlob2, globals.blink, globals.motionDir, 0, cfg.dist.maskCh);
      ord.forEach((src, i) => {
        const s = cfg.slots[src];
        gl.uniform4f(this.u.slot[i], s.color[0], s.color[1], s.color[2], s.color[3]);
        gl.uniform4f(this.u.tile[i], s.tx != null ? s.tx : 1, s.ty != null ? s.ty : 1, s.ox || 0, s.oy || 0);
        gl.uniform4f(this.u.move[i], s.su, s.sv, s.sa, (s.rot || 0) + (s.spin || 0) * Math.PI / 180);
        gl.uniform4f(this.u.fx[i], s.spiral, s.fixrot, s.fmin, s.fmax);
        gl.uniform4f(this.u.ctl[i], s.mt, s.rt, s.flash ? 1 : 0, s.en ? 1 : 0);
        gl.uniform4f(this.u.f[i], s.fspd, s.maskCh, s.maskStr, s.blink ? (s.bpeak != null ? s.bpeak : 1) : 0);
        gl.uniform4f(this.u.pos[i], 0, 0, (s.slot === "Middle") ? 1 : 0, s.foff || 0);
        gl.uniform4f(this.u.blink[i], s.blink ? 1 : 0, s.bper != null ? s.bper : 3.2, s.bdur != null ? s.bdur : 0.12, s.bphase || 0);
        const tn = s.tint || [1, 1, 1];
        gl.uniform4f(this.u.tint[i], tn[0], tn[1], tn[2], s.contrast == null ? 1 : s.contrast);
      });
      gl.drawArrays(gl.TRIANGLE_STRIP, 0, 4);

      if (!this.hdrReady) return;

      const BW = this.blW, BH = this.blH;
      gl.useProgram(this.blProg);
      gl.uniform2f(this.blU.texel, 1 / BW, 1 / BH);
      gl.bindFramebuffer(gl.FRAMEBUFFER, this.fboA);
      gl.viewport(0, 0, BW, BH);
      gl.activeTexture(gl.TEXTURE0); gl.bindTexture(gl.TEXTURE_2D, this.hdrTex);
      gl.uniform1f(this.blU.pass, 0);
      gl.drawArrays(gl.TRIANGLE_STRIP, 0, 4);
      gl.bindFramebuffer(gl.FRAMEBUFFER, this.fboB);
      gl.activeTexture(gl.TEXTURE0); gl.bindTexture(gl.TEXTURE_2D, this.blA);
      gl.uniform1f(this.blU.pass, 1); gl.uniform2f(this.blU.dir, 1, 0);
      gl.drawArrays(gl.TRIANGLE_STRIP, 0, 4);
      gl.bindFramebuffer(gl.FRAMEBUFFER, this.fboA);
      gl.activeTexture(gl.TEXTURE0); gl.bindTexture(gl.TEXTURE_2D, this.blB);
      gl.uniform1f(this.blU.pass, 2); gl.uniform2f(this.blU.dir, 0, 1);
      gl.drawArrays(gl.TRIANGLE_STRIP, 0, 4);

      gl.bindFramebuffer(gl.FRAMEBUFFER, null);
      gl.viewport(0, 0, W, H);
      gl.useProgram(this.tmProg);
      gl.activeTexture(gl.TEXTURE0); gl.bindTexture(gl.TEXTURE_2D, this.hdrTex);
      gl.activeTexture(gl.TEXTURE1); gl.bindTexture(gl.TEXTURE_2D, this.blA);
      gl.uniform2f(this.tmU.texel, 1 / W, 1 / H);
      gl.uniform4f(this.tmU.tone, globals.toneKnee, globals.toneOn ? 1 : 0, globals.exposure, globals.bloom || 0);
      const pal = SHEEN_PALETTE[globals.sheen] || SHEEN_PALETTE.rainbow;
      gl.uniform4f(this.tmU.sheen, globals.sheenPos != null ? globals.sheenPos : 1.2,
                   globals.sheen !== "off" ? globals.sheenPow : 0,
                   (globals.sheenAng || 0) * Math.PI / 180, globals.sheen !== "off" ? 1 : 0);
      gl.uniform4f(this.tmU.sheenA, pal[0][0], pal[0][1], pal[0][2], 1);
      gl.uniform4f(this.tmU.sheenB, pal[1][0], pal[1][1], pal[1][2], 1);
      gl.uniform4f(this.tmU.sheenC, pal[2][0], pal[2][1], pal[2][2], 1);
      gl.uniform4f(this.tmU.sheenD, pal[3][0], pal[3][1], pal[3][2], 1);
      gl.drawArrays(gl.TRIANGLE_STRIP, 0, 4);
    }
  }

  /* ---------------- 状态 ---------------- */
  let mounted = false;
  let card = null;
  let playing = true;
  let maskViz = false;
  let sheenT = 0;
  let lastProbe = 0;
  let tPrev = 0;
  let pickTarget = null, pickIsDist = false;
  let savedMaskName = "";
  const globals = { speed:1, distPow:1, fxPow:0.6, flashPow:1, maskPow:1, blink:1, motionDir:1,
                    toneOn:1, toneKnee:0.85, exposure:1.0, bloom:0.10, probe:0,
                    sheen:"off", sheenPow:0.25, sheenSpd:4.5, sheenAng:115, sheenPos:1.2 };
  const bgState = { mode:"none", color:"#20242e" };

  const $ = id => document.getElementById(id);

  /* ---------------- 面板组件 ---------------- */
  function fieldRow(label, control, valEl) {
    const row = document.createElement("div");
    row.className = "fx2-row";
    const l = document.createElement("span"); l.className = "fx2-lab"; l.textContent = label;
    row.appendChild(l); row.appendChild(control);
    if (valEl) row.appendChild(valEl);
    return row;
  }
  function rng(min, max, step, val, cb) {
    const input = document.createElement("input");
    input.type = "range"; input.min = min; input.max = max; input.step = step; input.value = val;
    const valEl = document.createElement("span"); valEl.className = "fx2-val";
    const fmt = step < 0.01 ? 3 : step < 0.1 ? 2 : 1;
    valEl.textContent = (+val).toFixed(fmt);
    input.addEventListener("input", () => { valEl.textContent = (+input.value).toFixed(fmt); cb(+input.value); });
    return { input, val: valEl };
  }
  function selCtrl(options, val, cb) {
    const s = document.createElement("select");
    Object.keys(options).forEach(k => {
      const o = document.createElement("option"); o.value = k; o.textContent = options[k]; s.appendChild(o);
    });
    s.value = val;
    s.addEventListener("change", () => cb(s.value));
    return s;
  }
  function addSection(title, sub, open) {
    const sec = document.createElement("div"); sec.className = "fx2-sec";
    const head = document.createElement("div"); head.className = "fx2-sechead";
    const b = document.createElement("b"); b.textContent = title;
    const t = document.createElement("span"); t.className = "fx2-sub"; t.textContent = sub || "";
    head.appendChild(b); head.appendChild(t);
    const body = document.createElement("div"); body.className = "fx2-secbody";
    body.style.display = open ? "flex" : "none";
    head.addEventListener("click", () => {
      body.style.display = body.style.display === "none" ? "flex" : "none";
    });
    sec.appendChild(head); sec.appendChild(body);
    return { sec, head, body };
  }

  /* ---------------- 面板构建 ---------------- */
  function rebuildPanel() {
    if (!card) return;
    const cfg = card.cfg;
    $("f2PanelTitle").textContent = cfg.card;
    $("f2GName").textContent = cfg.id;
    $("f2CardTitle").textContent = cfg.id;
    $("f2ArtName").textContent = cfg.artName || cfg.art;
    $("f2CardMaskName").textContent = cfg.maskName || cfg.mask;

    const slotsBox = $("f2Slots");
    slotsBox.innerHTML = "";
    cfg.slots.slice().sort((a, b) => layerOf(b) - layerOf(a))
      .forEach(s => slotsBox.appendChild(slotSection(s)));
    rebuildDist(cfg.dist);
    applyBg();
  }

  function slotSection(s) {
    const sec = document.createElement("div"); sec.className = "fx2-sec";
    const head = document.createElement("div"); head.className = "fx2-sechead";
    head.innerHTML = '<span class="fx2-slotname">' + s.slot + '</span>' +
      '<b>' + (SLOT_TAGS[s.slot] || s.slot) + ' · ' + (s.tex || "(空)") + '</b>' +
      '<span class="fx2-sub fx2-mode">' + (["加法","减法","透明"][s.rt-1] || "?") + ' / ' + (["固定","滚动","旋转","极坐标"][s.mt-1] || "?") + '</span>';
    const body = document.createElement("div"); body.className = "fx2-secbody";
    body.style.display = s._open ? "flex" : "none";
    head.addEventListener("click", () => {
      s._open = body.style.display === "none";
      body.style.display = s._open ? "flex" : "none";
    });

    const enChk = document.createElement("label");
    enChk.className = "fx2-enhead"; enChk.title = s.en ? "已启用（点击停用）" : "已停用（点击启用）";
    enChk.innerHTML = '<input type="checkbox"> 启用';
    const enBox = enChk.querySelector("input");
    enBox.checked = !!s.en;
    enBox.onchange = e => { s.en = e.target.checked ? 1 : 0; };
    enChk.addEventListener("click", e => e.stopPropagation());
    head.appendChild(enChk);

    const MASK_NAMES = ["R","G","B","A","全开","1−R"];
    const chChip = document.createElement("button");
    chChip.className = "fx2-btn ghost tiny";
    chChip.title = "蒙版通道（点击轮换：R→G→B→A→全开→1−R）";
    chChip.textContent = "M:" + (MASK_NAMES[s.maskCh] || "?");
    chChip.onclick = e => {
      e.stopPropagation();
      s.maskCh = (s.maskCh + 1) % 6;
      chChip.textContent = "M:" + MASK_NAMES[s.maskCh];
      const sel = body.querySelector("[data-maskch]");
      if (sel) sel.value = String(s.maskCh);
    };
    head.appendChild(chChip);

    const tools = document.createElement("div"); tools.className = "fx2-slotTools";
    const mkTool = (txt, title, fn) => {
      const b = document.createElement("button");
      b.className = "fx2-btn ghost tiny"; b.textContent = txt; b.title = title;
      b.onclick = e => { e.stopPropagation(); fn(); };
      return b;
    };
    tools.appendChild(mkTool("↑", "上移一层（更晚绘制=更靠上）", () => moveSlot(s, +1)));
    tools.appendChild(mkTool("↓", "下移一层（更早绘制=更靠下）", () => moveSlot(s, -1)));
    tools.appendChild(mkTool("✕", "移除本槽（载入预设可恢复）", () => removeSlot(s)));
    head.appendChild(tools);
    sec.appendChild(head); sec.appendChild(body);

    const sync = () => {
      const b = head.querySelector("b");
      b.textContent = (SLOT_TAGS[s.slot] || s.slot) + " · " + (s.tex || "(空)");
    };

    // 贴图
    const texRow = document.createElement("div"); texRow.className = "fx2-row";
    const texL = document.createElement("span"); texL.className = "fx2-lab"; texL.textContent = "特效贴图";
    const texBtn = document.createElement("button"); texBtn.className = "fx2-btn ghost";
    texBtn.textContent = s.tex || "选择贴图…";
    texBtn.onclick = () => openTexPicker(s, texBtn, false);
    texRow.appendChild(texL); texRow.appendChild(texBtn);
    body.appendChild(texRow);

    // 颜色
    const colRow = document.createElement("div"); colRow.className = "fx2-row";
    const colL = document.createElement("span"); colL.className = "fx2-lab"; colL.textContent = "颜色 _Color";
    const colDot = document.createElement("span"); colDot.className = "fx2-coldot";
    const colInp = document.createElement("input"); colInp.type = "color";
    const alphaR = rng(0, 1, 0.01, s.color[3], v => { s.color[3] = v; });
    const setColor = hex => {
      colDot.style.background = hex;
      const n = parseInt(hex.slice(1), 16);
      s.color[0] = ((n>>16)&255)/255; s.color[1] = ((n>>8)&255)/255; s.color[2] = (n&255)/255;
    };
    const hexOf = () => "#" + [0,1,2].map(i => Math.round(s.color[i]*255).toString(16).padStart(2,"0")).join("");
    colInp.value = hexOf(); setColor(hexOf());
    colInp.addEventListener("input", () => setColor(colInp.value));
    colRow.appendChild(colL); colRow.appendChild(colInp); colRow.appendChild(colDot);
    colRow.appendChild(alphaR.input); colRow.appendChild(alphaR.val);
    body.appendChild(colRow);

    // 运动与混合
    const mtS = selCtrl({ "1":"1 固定 Fixed", "2":"2 滚动 Scroll", "3":"3 旋转 Rotate", "4":"4 极坐标 Polar" },
      String(s.mt), v => { s.mt = +v; updateMode(); });
    const rtS = selCtrl({ "1":"1 加法 Additive", "2":"2 减法 Subtract", "3":"3 透明 Transparent" },
      String(s.rt), v => { s.rt = +v; updateMode(); });
    const updateMode = () => {
      const tag = head.querySelector(".fx2-mode");
      if (tag) tag.textContent = (["加法","减法","透明"][s.rt-1] || "?") + " / " + (["固定","滚动","旋转","极坐标"][s.mt-1] || "?");
      sync();
    };
    const rowMT = document.createElement("div"); rowMT.className = "fx2-row";
    rowMT.appendChild(Object.assign(document.createElement("span"), { className:"fx2-lab", textContent:"运动 MoveType" }));
    rowMT.appendChild(mtS);
    body.appendChild(rowMT);
    const rowRT = document.createElement("div"); rowRT.className = "fx2-row";
    rowRT.appendChild(Object.assign(document.createElement("span"), { className:"fx2-lab", textContent:"混合 RenderType" }));
    rowRT.appendChild(rtS);
    body.appendChild(rowRT);

    // 参数
    const refs = {};
    const mk = (label, key, min, max, step) => {
      const r = rng(min, max, step, s[key], v => { s[key] = v; });
      refs[key] = r;
      body.appendChild(fieldRow(label, r.input, r.val));
    };
    mk("ScrollU (UV/s)", "su", -2, 2, 0.01);
    mk("ScrollV (UV/s)", "sv", -2, 2, 0.01);
    mk("ScrollAngle (°)", "sa", -180, 180, 1);
    mk("Rotate (弧度/秒)", "rot", -2, 2, 0.01);
    mk("额外自旋 (°/秒)", "spin", -180, 180, 1);
    mk("Spiral 螺旋", "spiral", -3, 3, 0.01);
    mk("FixedRotate (弧度)", "fixrot", -6.3, 6.3, 0.01);
    mk("Tiling X", "tx", 0.1, 16, 0.01);
    mk("Tiling Y", "ty", 0.1, 16, 0.01);
    mk("Offset X", "ox", -4, 4, 0.01);
    mk("Offset Y", "oy", -4, 4, 0.01);

    const tintRow = document.createElement("div"); tintRow.className = "fx2-row";
    tintRow.appendChild(Object.assign(document.createElement("span"), { className:"fx2-lab", textContent:"额外染色" }));
    const tintInp = document.createElement("input"); tintInp.type = "color";
    const toHex = t => "#" + t.map(v => Math.round(Math.max(0,Math.min(1,v))*255).toString(16).padStart(2,"0")).join("");
    tintInp.value = toHex(s.tint || [1,1,1]);
    tintInp.addEventListener("input", () => {
      const n = parseInt(tintInp.value.slice(1), 16);
      s.tint = [((n>>16)&255)/255, ((n>>8)&255)/255, (n&255)/255];
    });
    tintRow.appendChild(tintInp);
    body.appendChild(tintRow);
    mk("对比度", "contrast", 0.5, 4, 0.05);

    const lutRow = document.createElement("div"); lutRow.className = "fx2-row";
    lutRow.appendChild(Object.assign(document.createElement("span"), { className:"fx2-lab", textContent:"LUT 调色" }));
    lutRow.appendChild(selCtrl(LUT_CN, s.lut || "", v => { s.lut = v; }));
    body.appendChild(lutRow);
    const areaRow = document.createElement("div"); areaRow.className = "fx2-row";
    areaRow.appendChild(Object.assign(document.createElement("span"), { className:"fx2-lab", textContent:"区域预设" }));
    areaRow.appendChild(selCtrl(AREA_CN, String(s.area || 0), v => { s.area = +v; }));
    body.appendChild(areaRow);

    if (/^tx_face/.test(s.tex || "") || s.blink) {
      const blkChk = document.createElement("label");
      blkChk.className = "fx2-check"; blkChk.style.color = "var(--fx2-gold)";
      blkChk.innerHTML = '<input type="checkbox"> 眨眼 Blink（脸部差分帧）';
      blkChk.querySelector("input").checked = !!s.blink;
      blkChk.querySelector("input").onchange = e => { s.blink = e.target.checked ? 1 : 0; };
      body.appendChild(blkChk);
      const mkB = (label, key, min, max, step) => {
        const r = rng(min, max, step, s[key], v => { s[key] = v; });
        body.appendChild(fieldRow(label, r.input, r.val));
      };
      mkB("眨眼间隔 (秒)", "bper", 1.5, 10, 0.1);
      mkB("眨眼时长 (秒)", "bdur", 0.05, 0.6, 0.01);
      mkB("闭眼浓度", "bpeak", 0, 1, 0.02);
      mkB("相位 (秒)", "bphase", 0, 10, 0.05);
      const hb = document.createElement("div"); hb.className = "fx2-hint";
      hb.textContent = "闭眼差分帧平时隐藏，每「间隔」秒整帧盖上去「时长」秒。间隔默认按官方 FlashSpeed≈2 换算（约 3.1s），可手动调到与实机手感一致。";
      body.appendChild(hb);
    }

    if (s.off && s.tuned) {
      const applyOfficial = () => {
        s.tx = s.off.st[0]; s.ty = s.off.st[1]; s.ox = s.off.ofs[0]; s.oy = s.off.ofs[1];
        s._offOn = true;
        const set = (k, f, v) => { refs[k].input.value = v; refs[k].val.textContent = (+v).toFixed(f); };
        set("tx", 1, s.tx); set("ty", 1, s.ty); set("ox", 2, s.ox); set("oy", 2, s.oy);
      };
      if (s._offOn) applyOfficial();
      const offChk = document.createElement("label");
      offChk.className = "fx2-check"; offChk.style.color = "var(--fx2-gold)";
      offChk.innerHTML = '<input type="checkbox"> 官方坐标（材质 ST/OFF）';
      offChk.querySelector("input").checked = !!s._offOn;
      offChk.querySelector("input").onchange = e => {
        if (e.target.checked) applyOfficial();
        else {
          s._offOn = false;
          const src = s.tuned;
          s.tx = src.uvScale; s.ty = src.uvScale; s.ox = src.px; s.oy = src.py;
          const set = (k, f) => { refs[k].input.value = src[k]; refs[k].val.textContent = (+src[k]).toFixed(f); };
          set("tx", 1); set("ty", 1); set("ox", 2); set("oy", 2);
        }
      };
      body.appendChild(offChk);
    }

    const flChk = document.createElement("label");
    flChk.className = "fx2-check";
    flChk.innerHTML = '<input type="checkbox"> 呼吸闪烁 Flash';
    flChk.querySelector("input").checked = !!s.flash;
    flChk.querySelector("input").onchange = e => { s.flash = e.target.checked ? 1 : 0; };
    body.appendChild(flChk);
    mk("FlashSpeed (转/秒)", "fspd", 0, 5, 0.01);
    mk("FlashMin", "fmin", 0, 1, 0.001);
    mk("FlashMax", "fmax", 0, 1, 0.001);
    mk("呼吸相位", "foff", 0, 6.28, 0.01);

    const maskS = selCtrl({ "0":"R 流光", "1":"G 细节", "2":"B 细节", "3":"A 透明", "4":"无(全开)", "5":"1−R 互补" },
      String(s.maskCh), v => { s.maskCh = +v; chChip.textContent = "M:" + (MASK_NAMES[+v] || "?"); });
    maskS.setAttribute("data-maskch", "1");
    const rowM = document.createElement("div"); rowM.className = "fx2-row";
    rowM.appendChild(Object.assign(document.createElement("span"), { className:"fx2-lab", textContent:"遮罩通道" }));
    rowM.appendChild(maskS);
    body.appendChild(rowM);
    mk("遮罩强度", "maskStr", 0, 2, 0.05);

    if (s.slot === "Middle") {
      const blk = rng(1, 600, 1, globals.blink, v => { globals.blink = v; });
      body.appendChild(fieldRow("眨眼幅度 ×", blk.input, blk.val));
      const hl = document.createElement("div"); hl.className = "fx2-hint";
      hl.textContent = "仅供面部层把官方极微幅闭眼闪烁放大到肉眼可见；不影响其它各层。";
      body.appendChild(hl);
    }

    sec.dataset.slot = s.slot;
    return sec;
  }

  function moveSlot(s, dir) {
    const cfg = card.cfg;
    const list = cfg.slots.slice().sort((a, b) => layerOf(a) - layerOf(b));
    const i = list.indexOf(s);
    const tgt = list[i + dir];
    if (!tgt) return;
    if (!cfg.orderCustom) {
      cfg.slots.forEach(x => { x._layer = layerOf(x); });
      cfg.orderCustom = 1;
    }
    const tmp = s._layer; s._layer = tgt._layer; tgt._layer = tmp;
    rebuildPanel();
  }
  function removeSlot(s) {
    const cfg = card.cfg;
    const i = cfg.slots.indexOf(s);
    if (i < 0) return;
    cfg.slots.splice(i, 1);
    rebuildPanel();
  }
  function addSlot() {
    const cfg = card.cfg;
    if (cfg.slots.length >= 5) {
      alert("最多 5 个特效槽（官方着色器固定 5 个贴图单元）。先移除一个再添加。");
      return;
    }
    const used = new Set(cfg.slots.map(x => x.slot));
    const key = SLOT_KEYS.find(k => !used.has(k)) || SLOT_KEYS[0];
    const s = normSlot({
      slot: key, tex: "", color: [0.5, 0.5, 0.5, 1],
      su: 0, sv: 0, sa: 0, rot: 0, spiral: 0, fixrot: 0,
      mt: 1, rt: 1, flash: 0, fspd: 1, fmin: 0, fmax: 1,
      maskCh: key === "Middle" ? 4 : (/^Back/.test(key) ? 1 : 0),
      maskStr: 1, tx: 1, ty: 1, ox: 0, oy: 0,
      tint: [1,1,1], contrast: 1, spin: 0, foff: 0, en: 1, _open: true
    });
    if (cfg.orderCustom) s._layer = Math.max(0, ...cfg.slots.map(layerOf)) + 1;
    cfg.slots.push(s);
    rebuildPanel();
  }
  function resetSlotOrder() {
    const cfg = card.cfg;
    cfg.orderCustom = 0;
    cfg.slots.forEach(x => { delete x._layer; });
    rebuildPanel();
  }

  function applyBg() {
    if (!card) return;
    const w = $("f2CvWrap");
    w.classList.toggle("bgOn", bgState.mode !== "none");
    w.style.backgroundImage = "";
    w.style.backgroundSize = "";
    w.style.backgroundPosition = "";
    w.style.backgroundColor = "";
    if (bgState.mode === "checker") {
      w.style.backgroundImage = "repeating-conic-gradient(#2b303c 0% 25%, #171b24 0% 50%)";
      w.style.backgroundSize = "16px 16px";
      w.style.backgroundColor = "#171b24";
    } else if (bgState.mode === "solid") {
      w.style.backgroundColor = bgState.color;
    } else if (bgState.mode === "art") {
      w.style.backgroundImage = 'linear-gradient(rgba(0,0,0,.45), rgba(0,0,0,.45)), url("' + card.cfg.art + '")';
      w.style.backgroundSize = "cover, cover";
      w.style.backgroundPosition = "center, center";
    }
  }

  function rebuildDist(d) {
    const body = $("f2Dist");
    body.innerHTML = "";
    const mk = (label, key, min, max, step) => {
      const r = rng(min, max, step, d[key], v => { d[key] = v; });
      body.appendChild(fieldRow(label, r.input, r.val));
    };
    mk("ScrollU (UV/s)", "su", -2, 2, 0.01);
    mk("ScrollV (UV/s)", "sv", -2, 2, 0.01);
    mk("ScrollAngle (°)", "sa", -180, 180, 1);
    mk("IntensityU", "iu", 0, 0.3, 0.005);
    mk("IntensityV", "iv", 0, 0.3, 0.005);
    mk("Tiling X", "tx", 0.1, 16, 0.01);
    mk("Tiling Y", "ty", 0.1, 16, 0.01);
    mk("Offset X", "ox", -4, 4, 0.01);
    mk("Offset Y", "oy", -4, 4, 0.01);
    mk("运动类型 MoveType (0=关)", "mt", 0, 4, 1);

    const rowC = document.createElement("div"); rowC.className = "fx2-row";
    rowC.appendChild(Object.assign(document.createElement("span"), { className:"fx2-lab", textContent:"扭曲通道" }));
    rowC.appendChild(selCtrl({ "0":"R", "1":"G", "2":"B 头发", "3":"A", "4":"无(全卡)" }, String(d.maskCh), v => { d.maskCh = +v; }));
    body.appendChild(rowC);

    const row = document.createElement("div"); row.className = "fx2-row";
    row.appendChild(Object.assign(document.createElement("span"), { className:"fx2-lab", textContent:"贴图" }));
    const btn = document.createElement("button"); btn.className = "fx2-btn ghost";
    btn.textContent = d.tex;
    btn.onclick = () => openTexPicker(d, btn, true);
    row.appendChild(btn);
    body.appendChild(row);
  }

  /* ---------------- 贴图选择器 ---------------- */
  function closePicker() { $("f2TexGrid").classList.remove("on"); }
  function openTexPicker(target, btnEl, isDist) {
    pickTarget = target; pickIsDist = !!isDist;
    $("f2TexFor").textContent = isDist ? "（扭曲层）" : "（当前槽）";
    const grid = $("f2TexItems");
    grid.innerHTML = "";
    const cur = target.tex;
    const items = FOIL_TEX.filter(n => !isDist || /distortion/.test(n));
    items.forEach(n => {
      const it = document.createElement("div");
      it.className = "fx2-texitem" + (n === cur ? " cur" : "");
      const img = document.createElement("img");
      img.src = TEX_DIR + n + ".png"; img.loading = "lazy";
      const lab = document.createElement("div"); lab.textContent = n;
      it.appendChild(img); it.appendChild(lab);
      it.onclick = () => {
        target.tex = n;
        btnEl.textContent = n;
        closePicker();
        if (isDist) {
          rebuildDist(card.cfg.dist);
        } else {
          const idx = card.cfg.slots.indexOf(target);
          card.gl.loadTex(TEX_DIR + n + ".png", 3 + (idx < 0 ? 0 : idx), target.slot !== "Middle");
          rebuildPanel();
        }
      };
      grid.appendChild(it);
    });
    $("f2TexSearch").value = "";
    $("f2TexGrid").classList.add("on");
    setTimeout(() => { const el = $("f2TexSearch"); if (el) el.focus(); }, 30);
  }

  /* ---------------- 底卡切换 ---------------- */
  function setBase(key) {
    if (!PRESETS[key]) return;
    card.presetKey = key;
    card.cfg = PRESETS[key];
    card.gl.setConfig(card.cfg);
    $("f2CardTag").textContent = PRESETS[key].card;
    $("f2Base").value = key;
    savedMaskName = "";
    $("f2UploadMaskName").textContent = "无";
    applyBg();
    rebuildPanel();
    startTicker();
  }
  function startTicker() { if (card) card.gl.time = 0; }

  /* ---------------- 收集册成品卡（沿用卡牌已有特效参数） ---------------- */
  let collectedCards = [];
  let whiteMaskUrl = "";
  function whiteMask() {
    if (whiteMaskUrl) return whiteMaskUrl;
    const cv = document.createElement("canvas");
    cv.width = cv.height = 32;
    const ctx = cv.getContext("2d");
    ctx.fillStyle = "#fff";
    ctx.fillRect(0, 0, 32, 32);
    whiteMaskUrl = cv.toDataURL("image/png");
    return whiteMaskUrl;
  }
  async function setCollectionCard(id) {
    const item = collectedCards.find(c => c.id === id);
    if (!item) return;
    let def = null;
    try {
      const list = await fetchEffects();
      for (const it of (item.effects || [])) {
        const e = list.find(x => x.name === it.name);
        if (e && (e.def || {}).engine === "foil") { def = e.def; break; }
      }
    } catch (e) { /* 特效库读取失败时按无特效展示 */ }
    const maskName = def && def.mask ? String(def.mask).split(/[\\/]/).pop() : "";
    card.cfg = {
      id: item.id,
      card: item.name || item.id,
      art: "/preview/" + encodeURIComponent(item.id) + "/front.png",
      mask: maskName ? "/assets_abs/effects-images/" + encodeURIComponent(maskName) : whiteMask(),
      dist: def && def.dist ? JSON.parse(JSON.stringify(def.dist))
            : { tex: "tx_foil_distortion", su: 0, sv: 0, sa: 0, iu: 0, iv: 0, mt: 1, maskCh: 2 },
      slots: def && def.slots ? JSON.parse(JSON.stringify(def.slots)) : []
    };
    card.gl.setConfig(card.cfg);
    savedMaskName = maskName;
    $("f2UploadMaskName").textContent = savedMaskName || "无";
    $("f2CardTag").textContent = card.cfg.card;
    $("f2Base").value = "card:" + id;
    if (def && def.globals) syncUI(def.globals);
    applyBg();
    rebuildPanel();
    startTicker();
    setStatus(def ? "已载入「" + card.cfg.card + "」的特效参数"
                  : "已载入「" + card.cfg.card + "」（该卡暂无闪卡特效）");
  }
  async function refreshBaseCards() {
    try {
      const r = await fetch("/api/cards");
      const d = await r.json();
      collectedCards = (d.cards || []).filter(c => c.id && c.name);
      const g = $("f2BaseCards");
      if (!g) return;
      const keep = $("f2Base").value;
      g.innerHTML = "";
      collectedCards.forEach(c => {
        const op = document.createElement("option");
        op.value = "card:" + c.id;
        op.textContent = c.name + "（" + c.id + "）";
        g.appendChild(op);
      });
      if (!g.children.length) {
        const op = document.createElement("option");
        op.disabled = true;
        op.textContent = "（暂无成品卡）";
        g.appendChild(op);
      }
      if (keep && [...$("f2Base").options].some(o => o.value === keep)) $("f2Base").value = keep;
    } catch (e) { /* 读取成品卡失败时仅保留官方预览卡 */ }
  }

  /* ---------------- 保存预设 ---------------- */
  function setStatus(msg, err) {
    const el = $("f2Status");
    el.textContent = msg;
    el.classList.toggle("err", !!err);
  }
  function snapshot() {
    if (!card) return null;
    const cfg = JSON.parse(JSON.stringify(card.cfg));
    return {
      engine: "foil",
      slots: cfg.slots || [],
      dist: cfg.dist || {},
      globals: {
        speed: globals.speed, distPow: globals.distPow, fxPow: globals.fxPow,
        flashPow: globals.flashPow, maskPow: globals.maskPow,
        blink: globals.blink, motionDir: globals.motionDir,
        toneOn: globals.toneOn, toneKnee: globals.toneKnee, exposure: globals.exposure,
        sheen: globals.sheen, sheenPow: globals.sheenPow, sheenSpd: globals.sheenSpd,
        sheenAng: globals.sheenAng
      },
      mask: savedMaskName || ""
    };
  }
  function syncUI(g) {
    if (g) Object.assign(globals, g);
    const set = (id, v) => {
      const el = $(id);
      if (!el || v == null) return;
      el.value = v;
      el.dispatchEvent(new Event("input"));
    };
    set("f2Speed", globals.speed); set("f2DistPow", globals.distPow); set("f2FxPow", globals.fxPow);
    set("f2MaskPow", globals.maskPow); set("f2ToneKnee", globals.toneKnee); set("f2Exposure", globals.exposure);
    set("f2SheenPow", globals.sheenPow); set("f2SheenSpd", globals.sheenSpd); set("f2SheenAng", globals.sheenAng);
    const sh = $("f2Sheen");
    if (sh) sh.value = globals.sheen || "off";
  }
  async function fetchEffects() {
    const r = await fetch("/api/effects");
    const d = await r.json();
    return d.custom || [];
  }
  let presetList = [];
  async function refreshPresets() {
    try {
      presetList = (await fetchEffects()).filter(x => (x.def || {}).engine === "foil");
      const sel = $("f2List");
      const keep = sel.value;
      sel.innerHTML = "";
      presetList.forEach(x => {
        const op = document.createElement("option");
        op.value = x.name;
        op.textContent = x.name + (x.def.mask ? "（含蒙版）" : "");
        sel.appendChild(op);
      });
      if (keep && presetList.some(x => x.name === keep)) sel.value = keep;
      const cnt = $("f2SaveCount");
      if (cnt) cnt.textContent = presetList.length + " 个";
    } catch (e) {
      setStatus("读取特效库失败：" + e.message, true);
    }
  }

  /* ---------------- 启动 ---------------- */
  function boot(container, opts) {
    container.innerHTML = FX2_HTML;

    card = { gl: null, cfg: null, presetKey: "sekka11" };
    try {
      card.gl = new CardGL($("f2Canvas"));
    } catch (e) {
      $("f2Diag").textContent = "⚠ WebGL 初始化失败：" + e.message;
      return;
    }
    card.cfg = PRESETS.sekka11;
    card.gl.setConfig(card.cfg);
    $("f2CardTag").textContent = card.cfg.card;
    $("f2Base").value = "sekka11";
    rebuildPanel();
    $("f2Diag").textContent = "OK · 就绪";

    /* 底卡 */
    $("f2Base").addEventListener("change", e => {
      const v = e.target.value;
      if (v.indexOf("card:") === 0) setCollectionCard(v.slice(5));
      else setBase(v);
    });

    /* 顶部工具 */
    $("f2MaskViz").addEventListener("click", e => {
      maskViz = !maskViz;
      e.currentTarget.classList.toggle("on", maskViz);
    });
    $("f2ResetT").addEventListener("click", () => { card.gl.time = 0; });
    $("f2Play").addEventListener("click", e => {
      playing = !playing;
      e.currentTarget.textContent = playing ? "⏸ 暂停" : "▶ 播放";
    });

    /* 全局滑杆 */
    const bindRange = (id, vid, fmt) => {
      const el = $(id);
      el.addEventListener("input", () => {
        $(vid).textContent = (+el.value).toFixed(fmt) + (id === "f2Speed" ? "×" : id === "f2DistPow" || id === "f2FxPow" || id === "f2MaskPow" ? "×" : "");
      });
    };
    bindRange("f2Speed", "f2SpeedV", 2);
    bindRange("f2DistPow", "f2DistPowV", 2);
    bindRange("f2FxPow", "f2FxPowV", 2);
    bindRange("f2MaskPow", "f2MaskPowV", 2);
    bindRange("f2ToneKnee", "f2ToneKneeV", 2);
    bindRange("f2Bloom", "f2BloomV", 2);
    bindRange("f2Exposure", "f2ExposureV", 2);
    bindRange("f2SheenPow", "f2SheenPowV", 2);
    bindRange("f2SheenSpd", "f2SheenSpdV", 1);
    bindRange("f2SheenAng", "f2SheenAngV", 0);

    $("f2Speed").addEventListener("input", e => globals.speed = +e.target.value);
    $("f2DistPow").addEventListener("input", e => globals.distPow = +e.target.value);
    $("f2FxPow").addEventListener("input", e => globals.fxPow = +e.target.value);
    $("f2MaskPow").addEventListener("input", e => globals.maskPow = +e.target.value);
    $("f2ToneKnee").addEventListener("input", e => globals.toneKnee = +e.target.value);
    $("f2Bloom").addEventListener("input", e => globals.bloom = +e.target.value);
    $("f2Exposure").addEventListener("input", e => globals.exposure = +e.target.value);
    $("f2SheenPow").addEventListener("input", e => globals.sheenPow = +e.target.value);
    $("f2SheenSpd").addEventListener("input", e => globals.sheenSpd = +e.target.value);
    $("f2SheenAng").addEventListener("input", e => globals.sheenAng = +e.target.value);
    $("f2Sheen").addEventListener("change", e => globals.sheen = e.target.value);

    $("f2ToneOn").addEventListener("click", e => {
      globals.toneOn = globals.toneOn ? 0 : 1;
      e.currentTarget.classList.toggle("on", !!globals.toneOn);
      e.currentTarget.textContent = globals.toneOn ? "● 开" : "○ 关";
    });
    $("f2MotionDir").addEventListener("click", e => {
      globals.motionDir *= -1;
      e.currentTarget.textContent = globals.motionDir === 1 ? "⟲ 反向" : "现行";
      e.currentTarget.classList.toggle("on", globals.motionDir !== 1);
    });

    $("f2BgMode").addEventListener("change", e => { bgState.mode = e.target.value; applyBg(); });
    $("f2BgColor").addEventListener("input", e => {
      bgState.color = e.target.value;
      if (bgState.mode === "solid") applyBg();
    });

    $("f2AddSlot").addEventListener("click", addSlot);
    $("f2OrderReset").addEventListener("click", resetSlotOrder);

    /* DIY 导入立绘 / 遮罩（本次会话生效） */
    function importOwnImage(unit, apply) {
      const input = document.createElement("input");
      input.type = "file"; input.accept = "image/*";
      input.onchange = () => {
        const f = input.files && input.files[0];
        if (!f) return;
        const url = URL.createObjectURL(f);
        apply(card, url, f.name);
        card.gl.loadTex(url, unit, false, unit === 0 ? [18, 22, 32, 255] : [255, 255, 255, 255]);
        rebuildPanel();
      };
      input.click();
    }
    $("f2ImportArt").addEventListener("click", () => importOwnImage(0, (c, url, name) => {
      c.cfg.art = url; c.cfg.artName = name + " · DIY";
    }));
    $("f2ImportMask").addEventListener("click", () => importOwnImage(1, (c, url, name) => {
      c.cfg.mask = url; c.cfg.maskName = name + " · DIY";
    }));

    /* 贴图浮层关闭 */
    $("f2TexClose").addEventListener("click", closePicker);
    $("f2TexGrid").addEventListener("click", e => { if (e.target.id === "f2TexGrid") closePicker(); });
    document.addEventListener("keydown", e => {
      if (e.key === "Escape" && $("f2TexGrid").classList.contains("on")) closePicker();
    });
    $("f2TexSearch").addEventListener("input", e => {
      const q = e.target.value.toLowerCase();
      document.querySelectorAll("#f2TexItems .fx2-texitem").forEach(it => {
        it.style.display = it.textContent.toLowerCase().includes(q) ? "" : "none";
      });
    });

    /* 保存预设 */
    $("f2Save").addEventListener("click", async () => {
      const name = ($("f2Name").value || "").trim();
      if (!name) { setStatus("请先填写预设名称", true); return; }
      const def = snapshot();
      if (!def) { setStatus("没有可保存的特效", true); return; }
      try {
        const r = await fetch("/api/effect/save", {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ name, def })
        });
        const d = await r.json();
        if (!d.ok) throw new Error(d.error || "保存失败");
        setStatus("✅ 已保存「" + name + "」，可在「卡牌拼装」的特效下拉选用");
        await refreshPresets();
        if (typeof window.refreshEffects === "function") window.refreshEffects();
      } catch (e) { setStatus("保存失败：" + e.message, true); }
    });
    $("f2MaskBtn").addEventListener("click", () => $("f2MaskFile").click());
    $("f2MaskFile").addEventListener("change", async e => {
      const f = e.target.files && e.target.files[0];
      e.target.value = "";
      if (!f) return;
      try {
        const dataUrl = await new Promise((res, rej) => {
          const fr = new FileReader();
          fr.onload = () => res(String(fr.result));
          fr.onerror = rej;
          fr.readAsDataURL(f);
        });
        const name = "foilmask_" + Date.now() + ".png";
        const r = await fetch("/api/assets/upload", {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ type: "effects-images", b64: dataUrl.split(",")[1], name })
        });
        const d = await r.json();
        if (!d.ok) throw new Error(d.error || "上传失败");
        savedMaskName = d.name;
        $("f2UploadMaskName").textContent = d.name;
        setStatus("✅ 蒙版已上传，保存预设时一并记录");
      } catch (err) { setStatus("蒙版上传失败：" + err.message, true); }
    });
    $("f2MaskClear").addEventListener("click", () => {
      savedMaskName = "";
      $("f2UploadMaskName").textContent = "无";
    });
    $("f2Load").addEventListener("click", async () => {
      const key = $("f2List").value;
      if (!key) { setStatus("先选一个已保存的预设", true); return; }
      try {
        const all = await fetchEffects();
        const item = all.find(x => x.name === key);
        if (!item) throw new Error("预设不存在");
        const def = item.def || {};
        const cfg = JSON.parse(JSON.stringify(card.cfg));
        cfg.slots = (def.slots || []).map(s => Object.assign({}, s));
        cfg.dist = Object.assign({}, def.dist || {});
        card.cfg = cfg;
        card.gl.setConfig(cfg);
        savedMaskName = def.mask || "";
        $("f2UploadMaskName").textContent = savedMaskName || "无";
        $("f2Name").value = key;
        syncUI(def.globals);
        rebuildPanel();
        setStatus("已载入「" + key + "」，可继续调整后再次保存");
      } catch (e) { setStatus("载入失败：" + e.message, true); }
    });
    $("f2Del").addEventListener("click", async () => {
      const key = $("f2List").value;
      if (!key) { setStatus("先选一个要删除的预设", true); return; }
      if (!confirm("删除预设「" + key + "」？")) return;
      try {
        const r = await fetch("/api/effect/delete", {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ name: key })
        });
        const d = await r.json();
        if (!d.ok) throw new Error(d.error || "删除失败");
        setStatus("🗑️ 已删除「" + key + "」");
        await refreshPresets();
        if (typeof window.refreshEffects === "function") window.refreshEffects();
      } catch (e) { setStatus("删除失败：" + e.message, true); }
    });

    refreshPresets();
    refreshBaseCards();

    /* 支持的 ?p= 初始底卡（仅独立页用；cardforge 分页无副作用） */
    if (opts && opts.preset && PRESETS[opts.preset]) setBase(opts.preset);

    /* 渲染循环 */
    const tick = now => {
      const dt = Math.min((now - tPrev) / 1000, 0.05);
      tPrev = now;
      if (playing) {
        card.gl.time += dt * globals.speed;
        sheenT += dt;
      }
      if (globals.sheen !== "off") {
        const per = Math.max(1, globals.sheenSpd);
        const ph = (sheenT % per) / per;
        const f = ph < 0.55 ? ph / 0.55 : 1;
        const e = f * f * (3 - 2 * f);
        globals.sheenPos = 1.2 - 1.4 * e;
      }
      card.gl.render(card.gl.time, {
        distPow: globals.distPow, fxPow: globals.fxPow, maskPow: globals.maskPow,
        blink: globals.blink, motionDir: globals.motionDir,
        toneOn: globals.toneOn, toneKnee: globals.toneKnee, exposure: globals.exposure, bloom: globals.bloom,
        sheen: globals.sheen, sheenPow: globals.sheenPow, sheenAng: globals.sheenAng, sheenPos: globals.sheenPos,
        maskViz: maskViz
      });
      const d = $("f2Diag");
      if (globals.probe && now - lastProbe > 800) {
        lastProbe = now;
        d.textContent = "HDR 浮点合成 · t=" + card.gl.time.toFixed(1) + "s · bloom=" + globals.bloom;
      } else if (d && d.textContent.indexOf("OK") === 0) {
        d.textContent = "OK · " + (playing ? "运行中" : "已暂停") + " · t=" + card.gl.time.toFixed(1) + "s";
      }
      requestAnimationFrame(tick);
    };
    tPrev = performance.now();
    requestAnimationFrame(tick);
  }

  /* ---------------- 对外接口 ---------------- */
  window.Fx2Editor = {
    mount(container, opts) {
      if (!container) return;
      if (mounted || container.dataset.fx2Mounted === "1") return;
      container.dataset.fx2Mounted = "1";
      mounted = true;
      boot(container, opts || {});
    }
  };

  /* 独立页自动挂载：/fx2/index.html 只有 #fx2Host */
  document.addEventListener("DOMContentLoaded", () => {
    const host = document.getElementById("fx2Host");
    if (!host) return;
    const qp = new URLSearchParams(location.search);
    window.Fx2Editor.mount(host, { preset: qp.get("p") });
  });
})();
