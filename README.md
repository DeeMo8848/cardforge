# cardforge —— 本地卡牌制作工具

输入一张图像，自动完成抠图（rembg + BiRefNet，离线本地运行）并产出卡牌资产，可被脚本或其他工具调用。

## 功能划分

| 入口 | 负责 | 说明 |
|---|---|---|
| `cardforge.cmd`（命令行） | **卡牌生成** | 供脚本/其他工具调用：抠图 → 生成卡牌资产（front / foreground / card.png / JSON 定义） |
| `webui.cmd`（可视化界面） | 卡牌生成 + **特效 DIY / 卡牌拼装 / 素材管理 / 收集册** | http://127.0.0.1:8765，特效 DIY、卡牌拼装、素材管理等目前仅 Web 提供 |

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

:: 动漫/插画封面推荐用动漫特化模型（体积小 5 倍、发丝更好）
cardforge.cmd 封面.png --name "xxx" --model isnet-anime
```

## 命令行参数参考（卡牌生成）

> 完整命令：`cardforge.cmd <卡面图片路径> [可选参数...]`

### 必选参数

| 参数 | 说明 |
|---|---|
| `image`（位置参数） | **卡面**输入图像路径。卡面是唯一必选项 |

### 卡牌信息

| 参数 | 默认 | 说明 |
|---|---|---|
| `--name` | 取文件名 | 卡片名称（收集册/列表里显示的名字，**不印在卡面上**） |
| `--slug` | 由名称生成 | 卡片 id（小写连字符） |
| `--style` | `transparent` | `transparent`=透明主体卡（PVZ 风）；`full-bleed`=整幅图卡面（影之诗/游戏王风） |
| `--engine` | `local` | 抠图引擎（当前支持 `local`=rembg+BiRefNet） |
| `--model` | `birefnet-general` | 本地抠图模型（质量优先可选 `birefnet-massive`；动漫素材推荐 `isnet-anime`） |

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
| `--adaptive` | `1` | 自适应开关：`1`=按上传图与卡面比例缩放裁剪；`0`=直接拉伸填满 |
| `--adaptive-mode` | `1` | 自适应方式：`1`=整幅判定（恒铺满，推荐）；`2`=透明主体判定（不足留白） |
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
3. 首次抠图时，rembg 会自动把所选模型下载到 `models/` 目录（默认 `birefnet-general`，约 970MB），之后完全离线使用

> **模型复用**：多个副本/多台机器想共享已下载的模型时，设置环境变量 `CARDFORGE_MODELS_DIR` 指向模型所在目录即可，例如
> `set CARDFORGE_MODELS_DIR=D:\ai\shared-models`（再运行 webui.cmd）。

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
| `assets/faces/` | 卡面底图（full-bleed 时即整幅卡面） | `sample-1.png` 夜空星点 |
| `assets/frames/` | 卡牌边框（透明 PNG） | `sample-1.png` 金色圆角框 |
| `assets/seals/` | 卡封（半透明覆层，如镭射箔光） | `sample-1.png` 斜向光泽 |
| `assets/backs/` | 卡背（默认沿用三国卡背，后续可 DIY） | `three-kingdoms-back.png` |

> 区分：**卡封** = 静态覆层（镭射这类，叠在卡面最上层）；**特效** = 3D 网页卡里的动态动画（冒爱心、冒闪光，周期随机触发），由 `--effect` 或界面下拉选择。

## 抠图引擎

- 默认本地引擎：`rembg + birefnet-general`（通用显著性分割，人像/动物/物体通吃，速度与质量平衡；离线免费；模型约 970MB）
- **动漫/插画素材推荐**：加 `--model isnet-anime`（动漫特化，发丝保留更好、边缘更细，模型仅约 176MB，体积小 5 倍）
- 引擎抽象在 `engine.py`，未来可插拔火山引擎等 API 引擎（`create_engine()` 注册）
- 模型存放在 `models/` 目录（默认项目内；缺失时首次抠图自动下载，之后离线可用）
- 启动时会在控制台列出各模型已下载/未下载状态，未下载的模型首次使用自动下载

## 环境

- Python 虚拟环境：`.venv`（用系统 Python 3.10 创建，避免依赖冲突；`setup.cmd` 自动创建）
- 依赖：`requirements.txt`（rembg + Pillow）
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
