# cardforge —— 本地卡牌制作工具

输入一张图像，自动完成抠图（rembg + BiRefNet，离线本地运行）并产出卡牌资产，可被脚本或其他工具调用。

## 功能划分

| 入口 | 负责 | 说明 |
|---|---|---|
| `cardforge.cmd`（命令行） | **卡牌生成** | 供脚本/其他工具调用：抠图 → 生成卡牌资产（front / foreground / card.webp / thumb.webp / JSON 定义） |
| `webui.cmd`（可视化界面） | 卡牌生成 + **特效 DIY / 卡牌拼装 / 素材管理 / 收集册（分组）/ 异兽牌（图鉴）/ 光栅卡** | http://127.0.0.1:8765，特效 DIY、卡牌拼装、素材管理、光栅卡等目前仅 Web 提供 |

> 其他工具对接本工具时只需调用命令行（卡牌生成部分），特效 DIY、卡牌拼装、素材管理不在 CLI 范围内。

## 卡牌效果在线预览

点击以下链接即可查看 3D 卡效果（拖动可旋转，由 raw.githack 在线渲染）：

- [🃏 预览](https://raw.githack.com/DeeMo8848/cardforge/main/preview/%E9%A2%84%E8%A7%88.html)


## 快速上手

### 可视化界面（日常使用推荐）

```bat
webui.cmd
```

启动后自动打开浏览器（http://127.0.0.1:8765），上传图片 → 设置名称/风格/模型/DIY 层 → 一键生成，结果可直接预览和保存。

### 命令行（供脚本/其他工具调用）

```bat
:: 基础用法：抠图并生成卡牌（transparent 风格：透明主体卡，类似 PVZ 植物卡）
cardforge.cmd 封面.png --name "莲之空双人组"

:: 整幅图卡面（类似影之诗/游戏王：卡面用一整幅图）
cardforge.cmd 封面.png --name "xxx" --style full-bleed

:: 机器可读输出（供其他工具解析）
cardforge.cmd 封面.png --name xxx --json

:: 抠图默认用动漫特化模型 isnet-anime-q8（int8，体积小 4 倍、发丝更好、内存低）
cardforge.cmd 封面.png --name "xxx"

:: 需要更高保真时可换回 fp32 版（体积/内存更大）
cardforge.cmd 封面.png --name "xxx" --model isnet-anime
```

## 命令行参数参考

> 完整命令：`cardforge.cmd <卡面图片路径> [可选参数...]`
> 也可只导出已有卡牌：`cardforge.cmd --export <卡id> [--export-type image|html] [--out 路径]`

### 必选参数

| 参数 | 说明 |
|---|---|
| `image`（位置参数） | **卡面**输入图像路径（`--export` 导出模式可省略） |

### 导出已有卡牌（不生成新卡）

| 参数 | 默认 | 说明 |
|---|---|---|
| `--export` | 无 | 导出已有卡牌（卡 id 即 `assets/output/` 下的目录名，可在收集册列表或列表接口看到） |
| `--export-type` | `image` | 导出类型：`image`=2D 合成图（`card.webp`）；`html`=自包含单文件（同目录图片 base64 内嵌，可离线双击打开） |
| `--out` | 当前目录 | 导出文件保存路径（默认 `<卡id>.png` / `<卡id>.html`） |

> 示例：`cardforge.cmd --export hasunosora-duo --export-type html --out 收藏/莲之空.html`

### 卡牌信息

| 参数 | 默认 | 说明 |
|---|---|---|
| `--name` | 取文件名 | 卡片名称（收集册/列表里显示的名字，**不印在卡面上**） |
| `--slug` | 由名称生成 | 卡片 id（小写连字符） |
| `--style` | `transparent` | `transparent`=透明主体卡（PVZ 风）；`full-bleed`=整幅图卡面（影之诗/游戏王风） |
| `--engine` | `local` | 抠图引擎（`local`=rembg+BiRefNet；配置了抠图 API 时选 `api` 自动切换） |
| `--model` | 自动 | 抠图模型。缺省时：`settings.json` 配置了抠图 API（`matting_api`）则用 `api`，否则用 `isnet-anime-q8`（动漫特化 int8 量化版，默认）；显式指定则用指定模型（`api`=阿里云分割抠图；质量优先 `birefnet-massive`；动漫素材推荐默认的 `isnet-anime-q8`） |

### 卡面文字（印在卡面上，可选）

| 参数 | 默认 | 说明 |
|---|---|---|
| `--title` | 无 | 卡面标题文字（印在卡面上的大标题，如"莲之空双人组"） |
| `--desc` | 无 | 卡牌效果/描述文字（印在卡面上，如"登场时：使我方全体攻击力 +2"） |
| `--text-type` | `none` | 卡牌类型：`none`=无文本型（默认）；`transparent`=特殊文本型（透明文本框）；`boxed`=文本型（带文本框边框） |
| `--text-pos` | 卡牌下方 | 描述文本区域 `x:y:w`（归一化 0~1），如 `0.5:0.82:0.84` |

### DIY 素材层（可选，均指向 `assets/` 下的素材名）

| 参数 | 说明 |
|---|---|
| `--background` | 卡牌背景（`assets/backgrounds/`） |
| `--face` | 卡面底图（`assets/faces/`；full-bleed 时即整幅卡面） |
| `--frame` | 卡牌边框（`assets/frames/`） |
| `--seal` | 卡牌卡封（`assets/seals/`，如镭射覆层） |
| `--seal-frame` | 边框卡封（`assets/seals/`）；传 `same` 表示与卡牌卡封同素材 |
| `--back` | 卡牌牌背（`assets/backs/`，默认三国卡背） |

### 卡牌特效与开关

| 参数 | 默认 | 说明 |
|---|---|---|
| `--effect` | `none` | 3D 网页卡动画特效，逗号分隔多个 `名称@位置`，如 `love@top,sparkle@右下` 或 `love@0.3:0.7` |
| `--glow` | `暖金描边` | 辉光/边框特效（`assets/effects-glow/` 自定义 JSON 或内置）：`暖金描边`/`冰蓝冷光`/`紫罗兰夜光`/`四角聚焦`/`上下双色`/`菲涅尔描边`/`双环辉光`/`RGB变色灯光`/`霓虹灯`/`发廊螺纹`/`樱花粉`；`none`=关闭 |
| `--glow-strength` | `1.0` | 辉光整体强度（0~2） |
| `--float-fg` | 关闭 | 主体浮于边框之上（PVZ 式立体感；默认边框盖住主体） |
| `--outline` | 关闭 | 主体描边（白色贴纸边，透明主体卡可选） |
| `--no-mask` | 裁剪开启 | **关闭**「裁剪到边框内」（默认开启：主体裁剪在边框内；加本参数后主体可延伸出边框外沿，边框外留一圈图像） |

### 裁剪与缩放

| 参数 | 默认 | 说明 |
|---|---|---|
| `--adaptive` | `1` | 自适应：`1`=方式1（整幅判定，按上传图与卡面比例缩放裁剪，恒铺满，推荐）；`2`=方式2（横图适配，不足留白，主体完整不裁切）；`0`=不使用自适应（直接拉伸填满） |
| `--scale` | `1.0` | 卡面缩放（0.5~1.5，默认 1 不缩放） |

### 输出

| 参数 | 默认 | 说明 |
|---|---|---|
| `--card-size` | `900x1200` | 2D 合成卡图尺寸，如 `900x1200` |
| `--no-compose` | 合成 | 只出资产、不合成 2D 预览卡图 |
| `--json` | 文本输出 | 以 JSON 输出结果（供其他工具解析） |

### 常用组合示例（其他工具调用）

```bat
:: 带边框+卡封+特效+浮起主体的完整卡
cardforge.cmd 封面.png --name "xxx" --frame sample-1.png --seal sample-1.png --effect sparkle --float-fg

:: 文本型卡（卡面标题 + 效果描述）
cardforge.cmd 封面.png --name "xxx" --title "莲之空双人组" --desc "登场时：使我方全体攻击力 +2" --text-type boxed

:: 主体不裁剪（延伸出边框外沿）+ 缩放 1.2
cardforge.cmd 封面.png --name "xxx" --frame sample-1.png --no-mask --scale 1.2

:: 供其他工具解析的 JSON 输出
cardforge.cmd 封面.png --name "xxx" --frame sample-1.png --json
```

> 提示：`--name`（卡片名称）是收集册里的名字，不印上卡；要往卡面上写字用 `--title`（标题）和 `--desc`（效果描述）。

## 部署

GitHub 仓库只包含本体程序（约几 MB），**虚拟环境和模型都不在仓库里**，部署时按需自动生成/下载：

1. 克隆或解压项目后，直接运行 `webui.cmd` 或 `cardforge.cmd`（首次使用会自动执行 `setup.cmd`，创建 `.venv` 虚拟环境并安装依赖）
2. 也可以手动执行 `setup.cmd` 单独完成环境初始化
3. 首次抠图时，会自动准备所选模型到 `models/` 目录（默认 `isnet-anime-q8`：先由 rembg 下载 fp32 的 `isnet-anime.onnx`，再本地量化成 int8 版，约 42MB），之后完全离线使用

> **模型复用**：多个副本/多台机器想共享已下载的模型时，设置环境变量 `CARDFORGE_MODELS_DIR` 指向模型所在目录即可，例如
> `set CARDFORGE_MODELS_DIR=D:\ai\shared-models`（再运行 webui.cmd）。

### 使用抠图 API（可选）

低配机器不想跑本地大模型时，可接入阿里云视觉智能开放平台「分割抠图」（约 0.002 元/次）：
在 `settings.json` 的 `matting_api` 填入 AccessKey 后，生成页下拉会自动出现「云端抠图 API」并作为默认；
CLI 缺省也会自动走 API（显式 `--model` 仍然优先）。

```json
"matting_api": { "provider": "aliyun", "access_key_id": "你的AK", "access_key_secret": "你的SK" }
```

> `settings.json` 已在 .gitignore 中，AccessKey 不会进入仓库。API 会把图片传到云端处理，素材不外传时请用本地模型。

## 产物

每次运行在 `assets/output/<slug>/` 下生成：

- `source.png` — 原图副本
- `front.png` — 底图层（3D 卡应用用）
- `foreground.png` — 透明前景层（抠图主体）
- `card.webp` — 2D 合成预览卡图（背景+主体+边框+特效+文字）
- `thumb.webp` — 收集册网格缩略图（宽 320）
- `cards/<slug>.json` — 卡牌定义（含全部 DIY 字段）

**素材库内的素材不再逐卡复制**：牌背、边框、卡封、背景、特效贴图这些如果直接取自
`assets/`、`packs/`、`fx2/`，`card.html` 里写的就是相对引用（`../../../assets/backs/sample-1.png`），
卡目录里不留副本 —— 一张 2.8 MB 的牌背在几十张卡里各存一份纯属浪费。
只有你自己上传、裁剪、缩放烘焙过的素材才会落到卡目录里（`front.png` / `interior.png` / `outline.png`
这类每卡独有的产物同样留在本地）。

要让单张卡脱离项目目录独立分发，用 `--export-type html` 导出：导出时会把所有引用（含 `../../../`）
现场 base64 内嵌，得到一个真正自包含的单文件。

## DIY 素材目录（目前均为示例，可替换）

| 目录 | 用途 | 示例 |
|---|---|---|
| `assets/backgrounds/` | 卡牌正面最底层的背景层 | `sample-1.png` 羊皮纸渐变 |
| `assets/faces/` | **卡面 / 主体**（同一类素材：不透明图铺满整卡当底图，抠好的透明图作前景层） | `sample-1.png` 夜空星点 |
| `assets/frames/` | 卡牌边框（透明 PNG） | `sample-1.png` 金色圆角框 |
| `assets/seals/` | 卡封（半透明覆层，如镭射箔光） | `sample-1.png` 斜向光泽 |
| `assets/backs/` | 卡背（默认沿用三国卡背，后续可 DIY） | `sample-1.png` |

> 早期版本把「卡面」和「主体」拆成 `faces/` 与 `subjects/` 两个素材类型，导致同一张图要在两个下拉里各选一次。
> 现已合并为一类（`subjects/` 在启动时自动并入 `faces/`，目录随之删除）；拼装页只剩一个「卡面 / 主体」下拉，
> 「🪟 这张图只作透明前景层」勾选框决定它是铺满整卡还是叠成前景。

> 区分：**卡封** = 静态覆层（镭射这类，叠在卡面最上层）；**特效** = 3D 网页卡里的动态动画（冒爱心、冒闪光，周期随机触发），由 `--effect` 或界面下拉选择。

## 光栅卡（lenticular）

模拟实体「光栅变画卡」：卡面是一排柱状透镜，下面压着 N 条图像条带，换视角时
透镜把不同条带折射进眼睛 —— 于是倾斜卡片会切换画面，同时伴随棱纹反光。

**主入口：卡牌拼装页**（勾选「🎞️ 光栅卡卡面」）—— 光栅卡面就是一个卡面层，
边框 / 卡封 / 辉光 / 特效 / 文字照常往上叠，预览仍是工具那套可拖拽缩放旋转的 3D 卡。

| 入口 | 用法 |
|---|---|
| 拼装页（推荐） | 🧩 卡牌拼装 → 勾选「🎞️ 光栅卡卡面」→ 加 2~6 张图（上传 / 素材库卡面）→ 调参数 → 保存为卡牌 |
| 拼装页（视频） | 同上，点「🎬 上传视频 / GIF」→ 自动抽 24 帧做图集 → 保存（见下节） |
| 单页工作台 | 主界面导航「🎞️ 光栅卡」→ 填路径或上传 → 生成预览 |
| 命令行 | `python lenticular.py --images a.png b.png c.png --name demo` |

拼装页参数：**换帧方式**（条纹推进 / 整幅柔和过渡 / 整幅直接切换）/ **条纹方向**
（0°=竖直 · 90°=水平 · 其余斜向）/ 透镜密度 / 棱纹 / 光泽 / 柔和（0=硬切变画，
1=全渐变）/ 错位 / 灵敏度（倾角 → 相位行程）。

命令行参数：

- `--mode flip`：N 张图直接交替（经典变画卡，**2~6 张**）
- `--mode 3d`：把每张图展开成视差帧（抠主体 + 按深度错位），张数 × 帧数 ≤ 6
- `--aspect 3:4|2:3|1:1|<数字>`：画幅，默认 3:4
- `--lpi 75`：额外导出一张 `print_interlaced.jpg`（按 600dpi 交错，可直接送印）

保存下来的光栅卡：卡目录 `lenti/f00.webp…`（帧图）+ `card.html`（同一套透镜 shader，
双击可离线看）+ `card.webp`（首帧当静态封面）；`cards/<id>.json` 的 `lenti`
记着**原始图源 spec 与参数**，重开这张卡会自动回填面板。

**实现要点**（改之前先读）：

- 透镜坐标：`axP`（条纹推进方向，垂直于透镜轴）/ `axL`（条纹走向）由 `uAngle` 旋转得到，
  `lx = dot(qc, axP) * (uLens/uAspect)`；扫光方向 `brush/cross` 也跟着这两根轴转。
- 相位用**视角在 `axP` 上的有符号投影**（`sv = dot(viewDirection, axPw)`），不写死 rotY ——
  否则条纹一转 90° 就变成「横条纹却在横向倾斜时变画」。`uSpan`（自动 =(N-1)/2）× 系数
  决定「满倾角走几张图」。
- 扫光包络必须用 **未归一化**的面内反射分量模长（∝ sin2θ）。用归一化方向做
  `dot(r2, brush)` 会得到 cos(夹角)，一点点倾斜就跳到 1 —— 退化成开关，卡面直接泛白。
- **过渡柔和**靠 `soft` 窗口 + 极小的 `jitter`：`jitter` 一旦接近窗口宽度，
  相邻透镜就会一半纯图 A 一半纯图 B，画面撕成竖条（早期 0.24/0.05 就是这个毛病）。
- 光栅卡面按 **cover** 铺满整卡、由边框内蒙版裁掉边框环（`face_cover` / 不 contain）：
  插画比例通常比边框窗口窄，contain 会在窗口里留一圈透明边。
- 帧数上限 = shader 里的采样器数量（6 个，`uT0..uT5`）。要加张数就同步扩
  `SLOT` 数组、`lentiTex()`/`frameAt()` 的 if 链，以及 `build_card_html` 的 `uLentiN` 上限。

**光栅卡 + foil 特效（一起用时的坑，已修）**

特效（`assets/effects/*.json`，`engine: foil`）不是叠在主卡面上，而是**另起一块不透明平面**
（`foilBuild`），自己贴着 `uMain`(= 卡面纹理) 画一遍再把特效加进去。所以：

- 光栅卡的卡面算在**卡面网格**上，特效层却拿 `uMain` 覆盖它 ⇒ 看起来「光栅完全不切换，
  永远停在第 1 张」，而且连棱纹都没有。
- 修法：给 `FOIL_VS` 补世界轴/世界坐标 varying，`FOIL_FS` 里注入同一段透镜合成
  （`_lenti_face_frag(target="col", uv_var="fuv")`，采样 `fuv` 而不是 `flowUv`），
  uniform 直接**共享** `cardMaterial.uniforms` 的那批对象。非光栅页注入空串。
- 注入位置必须在 `if (uBgOn > 0.5){ col = mix(bgc, mainTex.rgb, ...) }` **之后** ——
  那句会把 `col` 整个重置回第 1 帧，放前面等于没写。
- 主体层（`foilBuildSubject`，`uSubject=1`）要 `uGuard = uSubject < 0.5` 跳过透镜合成，
  否则抠图前景也会被套上透镜图。
- 视角很陡时（`setView`≈0.9）**光泽（`sheen`）会明显泛白**，尤其配白色/雾状特效的卡：
  光栅+特效建议 `sheen 0.3~0.4`，纯光栅卡 0.6~0.9 都行。

**视频 / GIF → 光栅卡（图集模式，帧数不受限）**

6 个采样器的上限只在「每帧一张贴图」时成立。把 N 帧**平铺成一张图集**后取帧变成
uv 偏移，**永远只占 1 个采样器**，帧数上限就变成图集尺寸（1440×1920 能装 25 格）。

```python
build_card_html(..., lenti_opts={
    "atlas": "lenti/atlas.webp", "atlasCols": 5, "atlasRows": 5, "frames": 24,
    "lens": 120, "stripe": 0.42, "sheen": 0.30, "soft": 0.18, "gloss": 0.6,
    "fade": "whole"})
```

- 每格必须与卡面同比例（**1465/2048 = 0.7153，不是 3:4**；按 3:4 做会横向挤 4.8%），
  横版素材先 cover 裁成卡面比例
- **`fade` 换帧形态**（视频卡必须换，静态光栅卡保持默认）：
  - `stripe`（默认）= 真光栅卡行为，相位逐条带递进，倾斜时一条条推过去
  - `whole` = 整张卡同时溶解换帧（视频/GIF 用这个）
  - `cut` = 同上但在半程硬切，整幅直接换图，**绝无双影**
  - 为什么视频卡不能用 `stripe`：视频本来就是「整幅图换掉」，一条条推进看着像画面被撕开
- ★★ **`whole`/`cut` 的相位必须全卡唯一**：取「卡面中心」的视角投影 `uLViewS`（JS 每帧
  用 `cardMesh.matrixWorld` 的基向量 + 相机位置算一次写进 uniform），**绝不能用
  `dot(viewDirection, axPw)`** —— 那是逐像素的，卡一倾斜，左右两端视线方向差得多，
  相位能差好几帧（`uLGyro` 24 帧时就是十几个），画面直接被切成几竖条不同内容
  （同一人物出现两三个姿势）。这个 bug 肉眼在缩略图上看不出来，必须靠「把 wf/i0
  编码成颜色再沿列取中位数」的探针测，见 `工作间/cardrip/_chk_band_uniform.py`
- `soft` 是 wf 的混合窗口宽度。`whole` 下 0.30 意味着 30% 的角度区间都在两帧叠影里
  ⇒ 看着像画面在融化；视频卡用 **0.12~0.18**，要绝对干净就 `fade: "cut"`
- `jitter`（条带错位）只有 `stripe` 用得上；整幅两种是全卡同步，拼装页里会自动禁用
- **在拼装页直接做**：光栅面板里「🎬 上传视频 / GIF」→ `POST /api/lenti/video`（裸字节，
  `X-File-Name` 头带文件名）→ 均匀抽 24 帧（cv2 解 mp4/webm，PIL 解 GIF/动画 WebP）→
  拼 5×5 图集存 `assets/lenticular/_video/<md5>/`（按内容哈希缓存，同文件秒回）→
  前端存成 `asmLenti.video`，导入时自动把 `fade` 切到 `whole`、`soft` 收到 0.18、
  `sheen` 压到 0.3（只在还停在默认值时才覆盖）。保存时图集与首帧拷进卡目录
  `lenti/atlas.webp` + `lenti/f00.webp`，`cards/<id>.json` 记 `atlas_src`（`lvid:<hash>`）
  供重开回填
- `_mk_video_cards.py`（`工作间/cardrip`）是批量流水线（直接出成品卡 + 五角度截图 +
  倾斜 GIF），与新加的 UI 入口共用同一套图集参数
- 收集册卡片定义里 `engine: "video-lenti"`，`_regen_cards.py` 会跳过（它只认普通 front 路径）

## 程序化背景（虚空）

`assets/backgrounds/虚空.png` + 同名 `.json`：

```json
{ "shader": "void", "version": 2 }
```

选它当**背景**时，3D 卡页的卡面颜色改由着色器实时生成：**五层星点 + 双倍频星云**，
深紫虚空里星点在漂。**0 个额外纹理单元**（只多四个 float uniform：`uVoidOn/uVoidTime/
uVoidPar/uVoidZoom`），所以能跟光栅卡、foil 特效同时挂。

> 旧素材名是「末地传送门」，`shader: "end_portal"` 仍作为别名兼容旧卡片；
> 真正的末地门（MC 那种贴图/扭曲门面）以后单独做一个 shader，别往这个上叠。

**立体感怎么做出来的（第一版做错了，别再犯）**

- 第一版只按时间滚动（`uv * sc + t * spd`），`viewDirection` 根本没用上 ⇒ 拖动卡牌时
  星空纹丝不动，看着就是一张平贴的星空图，完全没有「隔着平面看另一片空间」的感觉
- 现在把视线用世界轴拆到卡面平面内：`lk = -vec2(dot(V,X), dot(V,Y)) / max(dot(V,n),.34)`，
  再让每层用自己的深度系数乘它 —— **近层位移大、远层位移小（差 6 倍以上）**，倾斜时
  层与层互相错开；同时远层采样坐标被放大（`zk = 1 + dep*zoom`）⇒ 图案朝中心收缩
- 只做平移没有缩放也不行：远处必须**更小更密更暗**，否则全是同样大小的点，像噪点
- ★★ **`lk` 必须软饱和**：它逐像素算，倾斜时卡面两端的梯度被层系数放大，40~45° 就
  变成「星点被拉成斜条」的扭曲（用户实测）。原来只 `clamp(±1.25)` 等于没限。
  现在 `lk *= 1/(1 + 1.5*|lk|)`：40° 附近到 0.35，再倾斜几乎不再增长（渐近 0.67），
  观感是「视差到一定角度就停住」。验证 `工作间/cardrip/_chk_void_angle.py`
  （0~0.9 rad 六档截图 + 拉普拉斯方差）
- 星点半径要够小（`mix(0.0032,0.0090)` 卡高）：halo 一旦超过半格就会被格子边界硬切，
  星星直接变**方块**（第一版踩过，`r*4.6` 的 halo 就是元凶）
- 星云必须用 fbm（每级转 37° 再 ×2.07）：两层不同频率的噪声直接相加会出**斜向摩尔纹**
- 卡面 FS 的 `varying` 声明必须写在 GLSL 注入占位**之前**（该函数要用
  `vWorldNormal/vWorldPosition/vWorldXAxis/vWorldYAxis` 做透视）
- ★ 占位符名字不要写进**注释**：模板是全局字符串替换，注释里的 `__BG_SHADER_GLSL__`
  也会被换成整段函数，于是多出一份定义在 varying 之前的 `vpSpace`，整页编译失败
- 量化验证：`工作间/cardrip/_chk_epl3d.py`（多角度截图 + 相位相关测位移）
- 静态那张 PNG 负责：素材库下拉的缩略、2D 合成、`card.webp`、印刷 —— 全部定格
- 配合「主体」使用才有空间感：**背景 = 虚空，主体 = 抠图**（拼装页勾「卡面当主体」）
- 实现要点：`_void_space_glsl()` 同时注入**主卡面（CARD_FS）和特效层（FOIL_FS）**，
  两处都必须写；`FOIL_FS` 里那一段要带 `uSubject < 0.5` 守卫，否则抠图主体会被涂成
  一块紫色剪影（星点还压在脸上）
- `FOIL_FS` 的注入点在 `uAspect` 声明**之后**：GLSL 不允许先用后声明，放前面整层编译失败
- 卡片定义 `background_name` 指向它即可；`_regen_cards.py` 会跳过这类卡（脚本不传 `bg_shader`）

## 收集册（分组）

收集册页（顶栏「📖 收集册」，或 http://127.0.0.1:8765/album.html）除了浏览成品卡，还能给卡牌分组：

- **新建 / 重命名 / 删除收集册** —— 册条右侧「＋ 新建收集册」，把鼠标移到册名上出现 `✎` / `✕`。
- **删除册不会删卡** —— 册里的卡自动回到「未分类」，卡牌目录与 JSON 一个都不动。
- **移动卡牌** —— 鼠标移到卡片上点 `📁` 选目标册；或多选（顶栏「☑ 多选」）后底部操作条批量移动。
- 组数据存 `albums.json`（本地文件，已 gitignore），只记「册名 + 卡牌归属」；一张卡最多属于一个册。

离线双击 `album.html` 也能看分组结果（只读，不能改）。

## 异兽牌图鉴（codex）

顶栏「🐾 异兽牌」/ http://127.0.0.1:8765/codex.html —— 和收集册是两件事：
收集册**自由分组**，图鉴是**固定顺序**的展册，像游戏里的图鉴。

- **版式**：一页 3×3 = 一个主体的 9 个版本，左右两页共 18 格，像相册一样翻。
- **点亮 / 未解锁**：该版本真有成品卡就点亮；没有的留暗位（`？？？` / `未收录`），整本顺序不因为缺卡而错位。
- **预览开关**（右上角）：一键「点亮 / 熄灭」全部槽位，用来检查未解锁状态好不好看。
- 点已点亮的卡牌 = 打开 3D 卡面（与收集册同一个弹窗）。

### 翻页（四种方式）

| 方式 | 操作 |
|---|---|
| 按钮 / 键盘 | `‹ 上一页` `下一页 ›`；`←` `→`、`PgUp` `PgDn`；`Home` `End` 直接到首尾册页 |
| 拖动滑块 | 底部中间的长条滑块，拖动即跳，松手前逐帧合并渲染 |
| 输入页码 | 底部「第 [ ] / N 册页」的输入框，回车或失焦生效；超范围自动夹到首/末页 |
| 点边缘 | 书的左/右**视口边缘**有热区（约 100px 宽），悬停显箭头，点一下翻一册页 |

### 目录数据

- `codex_cards.json` —— 全部 209 张主体的元数据（卡号 / 中英名 / 元素 / 稀有度 / 动物 / 版本 / 描述），
  由 `工作间/cardrip/_gen_codex_cards.py` 从解包总表 + 稀有度表合并导出。
  图鉴因此能列出**还没渲染的卡**（显示为未收录），而不是只有做过的那几张。
- `codex.json` —— 只放展示配置：标题 / 副标题 / `edition_order`（9 格顺序）/ `include_promo`（是否列 No.201-209 特典）。
  想手工指定条目或顺序时，加 `subjects` 数组即可覆盖自动清单。
- 卡牌 id 按 `<slug>-<版本小写>` 推导（如 `air-cat-01a-negative`）；点亮判定 = 这个目录是否真的存在。

### 成品大图与缩略图统一用 WebP（`card.webp` + `thumb.webp`）

每张卡除了 `card.html`（3D 分层页）还写一份**平面成品图**，用于「导出图片」和制作页的 2D 预览，
外加一份网格用的**缩略图**。两者都存 WebP：

| | 单张 | 1800 版合计 |
|---|---|---|
| `card.png`（1465×2048） | 1.75 MB | ~3.1 GB |
| `card.webp`（q92，同分辨率） | **0.17 MB** | **~315 MB** |
| `thumb.png`（宽 320） | ~190 KB | ~340 MB |
| `thumb.webp`（q88，宽 320） | **~35 KB** | **~63 MB** |

肉眼看不出差别。**保存、重生成文字、CLI 合成、导出**四个入口全部走
`cardforge.save_preview(im, out_dir)`（`card.webp` q92 + `thumb.webp` q88，顺手清掉同名老 PNG），
口径只此一处 —— 以前分散在 `cardforge.py` / `webapp.py` 两处各写一遍 PNG，
改口径时漏一处就会出现「有的卡是 png 有的是 webp」。
`_export_card` 现在也直接给 `.webp`（磁盘已是 webp 就字节直搬，不做重编码）。
`list_cards()` 用 `_big_name()` 取文件名，`card.webp` 不存在时回落 `card.png`（兼容老产物）。

### 缩略图（**不要删** `thumb.webp`）

网格每格只有约 160 CSS px，早期直接用 1465×2048 的成品大图当缩略图，实测：

| | 一册页（18 格） | 单页解码 |
|---|---|---|
| 整卡大图 | **37 MB** | 0.8 – 2.0 s（翻页明显顿） |
| `thumb.webp`（宽 320，~35 KB） | **0.6 MB** | ~20 ms |

所以每张卡额外写一份 `thumb.webp`（`batch_editions.make_one` / `cardforge.save_preview` 生成，
`_backfill_thumb.py` 可回填历史卡）。
收集册的网格格子和侧栏小图都读它；**详情弹窗走 `card.html` 的 iframe**，
那里是 three.js 按层实时合成（front / under_rare / art / border / badges / text / smoke 各自一张贴图），
不是一张预合成的大图 —— 这才是「换掉 art.png 就换主体」的可 DIY 架构。

★ 两个坑：

1. `thumb.webp`（和老的 `thumb.png`）**必须**在 `cardrip/_sharedize.py` 的 `KEEP_LOCAL` 里。它是每卡专属的，
   一旦被当公共素材处理，就会被 prune 扫进 `packs/_trash`，图鉴立刻变成一片空位。
2. 图鉴页注入数据用的是 `re.sub(...)`，**替换串会被解释反斜杠**（`\"` → `"`）。
   直接把 `json.dumps` 的结果塞进替换串，卡牌描述里只要有一个引号，JSON 就被吃坏，
   浏览器报 `Invalid or unexpected token`，`window.__CODEX__` 没定义 → 整本图鉴白屏。
   必须用函数式替换（`lambda _m: payload`）原样插入，并顺手把 `</` 转成 `<\/`
   防止描述里出现 `</script>` 提前闭合。

离线双击 `codex.html` 也可看（图片走相对路径，链接自动隐藏）。

## 批量还原卡牌（从游戏解包图）

渲染脚本在 `工作间/cardrip/`（依赖 `render_ripit.py` 与解包素材 `F://卡牌解包`）。

```bash
cd "D://略夹//ai//ai工具//工作间//cardrip"

py batch_editions.py --cards CardInfo.Ice.Cat.01a            # 单卡九版
py batch_editions.py --rarity Epic --per-rarity 4 --jobs 3   # 每个指定稀有度各取前 4 张
py batch_editions.py --rarity Common,Uncommon --jobs 3       # 整档批量（默认跳过 Promo）
py batch_editions.py --cards CardInfo.Air.Frog.01a --dry-run # 只看将要生成什么
```

| 参数 | 说明 |
|---|---|
| `--cards a,b` | 逗号分隔的 card_id，批量 |
| `--rarity R` | 按稀有度抽：`Common,Uncommon,Rare,Epic,Mythic,Promo` |
| `--per-rarity N` | 配合 `--rarity`，每档只取前 N 张（试验批次） |
| `--no-promo` | 跳过 No.201-209 特典卡 |
| `--editions ...` | 版本子集，默认 8 个变体；九宫格需要 `Normal` 时显式带上 |
| `--jobs N` | 并行进程数，**按卡分片**（同一张卡的 9 版必须同进程，否则 art/文字层互相覆盖） |
| `--dry-run` | 只打印目标清单 |

产物落在 `cardforge/assets/output/<slug>/`，同时刷新 `cards/*.json` 与收集册。跑完在工具里刷新图鉴即可看到点亮。

> 批量渲染的中间层（边光 / 细闪 / 稀有度框 / 烟雾 / 漂移底图）写在 `cardforge/packs/_build/`，
> 被替换的旧产物搬到 `cardforge/packs/_trash/`——**渲染过程中不做任何逐文件删除**，
> 否则每 50 个文件就会撞上一次工作区的批量删除确认。批次结束用 `rm -rf` 清这两个目录即可。

## 素材包（asset pack）

素材变多后，仓库只保留少量示例素材，大素材以「素材包」分发：一个包 = 一个文件夹（或压缩包），
包根放**一份** `pack.json` 索引，界面「📦 素材包」页里可导入 / 启用 / 禁用 / 删除。

导入（压缩包或文件夹）会把内容**复制**进工具的 `packs/` 目录，源文件不动；删除也只删 `packs/` 里的这份，
不影响内置 `assets/` 与你自己另放的素材。启用的包会和内置素材合并出现在各页下拉里（标注来源包）。

### `pack.json` 格式

```json
{
  "id": "sakura-pack",
  "name": "樱色素材包",
  "author": "xxx",
  "description": "樱花主题背景 + 金边边框",
  "thumbnail": "cover.png",
  "assets": [
    { "type": "backgrounds", "dir": "bg" },
    { "type": "frames", "dir": "frames" }
  ]
}
```

| 字段 | 必填 | 说明 |
|---|---|---|
| `id` | ✅ | 包唯一 id（字母/数字/`._-`）；导入后文件夹名即此 id |
| `name` | ✅ | 显示名 |
| `author` / `description` / `thumbnail` | — | 可选；`thumbnail` 为相对包根的缩略图 |
| `assets` | — | 素材声明：`{"type": 类型, "dir": 相对包根的目录}`，目录会递归扫描（非图片跳过） |

- 素材类型只认 5 类：`backgrounds`（背景）/ `faces`（卡面）/ `frames`（边框）/ `seals`（卡封）/ `backs`（卡背）；未知类型跳过。
- **不写 `assets` 时**按标准目录名自动识别：包根下的 `backgrounds/` `faces/` `frames/` `seals/` `backs/`。
- 一个包只读**顶层**的 `pack.json`（嵌套的当普通文件忽略）；坏索引会在管理页标红并跳过，不影响工具运行。

### 包目录示例

```text
sakura-pack/
├── pack.json          ← 索引（必需）
├── cover.png          ← 缩略图（可选）
├── bg/                ← 声明为 backgrounds
│   ├── sakura-1.png
│   └── sakura-2.png
└── frames/            ← 声明为 frames
    └── gold.png
```

导入方式：把上面的文件夹打包成 `sakura-pack.zip`（`pack.json` 在压缩包顶层）后在「素材包」页导入，
或直接选文件夹导入。启用/禁用只是逻辑开关，不搬动文件；切换后在「素材管理」页可即时看到，各页下拉刷新页面后生效。

## 抠图引擎

- 默认模型：`isnet-anime-q8`（动漫/插画特化的 ISNet-anime 之 int8 量化版；离线免费）。
  实测（输入 1450×2051）：模型约 42MB，推理约 1s，进程峰值内存约 475MB，发丝边缘干净。
- 该模型由项目**自量化**生成：首次使用时以 rembg 官方 fp32 的 `isnet-anime.onnx` 为源，
  用 `assets/` 下的素材做校准，现场跑 QDQ int8 静态量化（约 20s），产物缓存在 `models/`。
  - 为什么不用现成的 fp32：峰值内存 916MB、耗时 2.3s，约是 q8 的两倍；量化后质量几乎不变
    （与 fp32 的互相 IoU ≈ 0.998）。
  - 量化需要 `onnx` 包（已列入 requirements.txt；仅首次生成时用到）。
- 需要更高保真时可用 `--model isnet-anime`（fp32）或 `--model birefnet-general-lite`
  （通用分割质量更高，但峰值内存约 5.4GB，小内存机器会被内存守卫自动降级）。
- 引擎抽象在 `engine.py`，未来可插拔火山引擎等 API 引擎（`create_engine()` 注册）
- 模型存放在 `models/` 目录（默认项目内；缺失时首次使用自动下载/生成，之后离线可用）
- 启动时会在控制台列出各模型已下载/未下载状态，并给出内存守卫结论

## 环境

- Python 虚拟环境：`.venv`（用系统 Python 3.10 创建，避免依赖冲突；`setup.cmd` 自动创建）
- 依赖：`requirements.txt`（rembg + onnxruntime + Pillow + onnx）
- 调用入口：`webui.cmd` / `cardforge.cmd`（自动检测环境，缺失时先部署再运行）

## 开发

```bat
:: 重新安装依赖
.venv\Scripts\python.exe -m pip install -r requirements.txt

:: 重新生成示例素材
.venv\Scripts\python.exe scripts\make_samples.py

:: 重新生成卡封预览图（assets/seals/*.png）
.venv\Scripts\python.exe _generate_seals.py

:: 按 cards/*.json 批量重建卡片到 assets/output/
.venv\Scripts\python.exe _regen_cards.py
```
