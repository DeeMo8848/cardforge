# cardforge —— 本地卡牌制作工具

输入一张图像，自动完成抠图（rembg + BiRefNet，离线本地运行）并产出卡牌资产，可被脚本或其他工具调用。

## 功能划分

| 入口 | 负责 | 说明 |
|---|---|---|
| `cardforge.cmd`（命令行） | **卡牌生成** | 供脚本/其他工具调用：抠图 → 生成卡牌资产（front / foreground / card.png / JSON 定义） |
| `webui.cmd`（可视化界面） | 卡牌生成 + **特效 DIY / 卡牌拼装 / 素材管理 / 收集册（分组）/ 异兽牌（图鉴）** | http://127.0.0.1:8765，特效 DIY、卡牌拼装、素材管理等目前仅 Web 提供 |

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
| `--export-type` | `image` | 导出类型：`image`=2D 合成图（`card.png`）；`html`=自包含单文件（同目录图片 base64 内嵌，可离线双击打开） |
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
- `card.png` — 2D 合成预览卡图（背景+主体+边框+特效+文字）
- `cards/<slug>.json` — 卡牌定义（含全部 DIY 字段）

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

- **版式**：一页 3×3 = 一个主体的 9 个版本，左右两页共 18 格；`← →` 或「上一页 / 下一页」翻册页。
- **点亮 / 未解锁**：该版本真有成品卡就点亮；没有的留暗位（`？？？` / `未收录`），整本顺序不因为缺卡而错位。
- **预览开关**（右上角）：一键「点亮 / 熄灭」全部槽位，用来检查未解锁状态好不好看。
- 点已点亮的卡牌 = 打开 3D 卡面（与收集册同一个弹窗）。
- 目录存 `codex.json`：`subjects`（主体：slug / 名称 / 编号 / 属性）+ `edition_order`（版本顺序，9 格）。
  卡牌 id 按 `<slug>-<版本小写>` 推导，例如 `air-cat-01a-negative`。
  **加主体 = 加一条记录**，册页自动分页；删卡只会让对应格变暗，不会让整本重排。

离线双击 `codex.html` 也可看（图片走相对路径，链接自动隐藏）。

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
