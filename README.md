# cardforge —— 本地卡牌制作工具

输入一张图像，自动完成抠图（rembg + BiRefNet，离线本地运行）并产出卡牌资产，可被脚本或其他工具调用。

## 快速上手

### 可视化界面（日常使用推荐）

```bat
webui.cmd
```

启动后自动打开浏览器（http://127.0.0.1:8765），上传图片 → 设置名称/风格/模型/DIY 层 → 一键生成，结果可直接预览和保存。

### 命令行（供脚本/其他工具调用）

```bat
:: 抠图并生成卡牌（transparent 风格：透明主体卡，类似 PVZ 植物卡）
cardforge.cmd 封面.png --name "莲之空双人组"

:: 整幅图卡面（类似影之诗/游戏王：卡面用一整幅图）
cardforge.cmd 封面.png --name "xxx" --style full-bleed

:: 机器可读输出（供其他工具解析）
cardforge.cmd 封面.png --name xxx --json

:: 动漫/插画封面推荐用动漫特化模型（体积小 5 倍、发丝更好）
cardforge.cmd 封面.png --name "xxx" --model isnet-anime
```

## 部署

GitHub 仓库只包含本体程序（约几 MB），**虚拟环境和模型都不在仓库里**，部署时按需自动生成/下载：

1. 克隆或解压项目后，直接运行 `webui.cmd` 或 `cardforge.cmd`（首次使用会自动执行 `setup.cmd`，创建 `.venv` 虚拟环境并安装依赖）
2. 也可以手动执行 `setup.cmd` 单独完成环境初始化
3. 首次抠图时，rembg 会自动把所选模型下载到 `models/` 目录（默认 `birefnet-massive`，约 970MB），之后完全离线使用

> **模型复用**：多个副本/多台机器想共享已下载的模型时，设置环境变量 `CARDFORGE_MODELS_DIR` 指向模型所在目录即可，例如
> `set CARDFORGE_MODELS_DIR=D:\ai\shared-models`（再运行 webui.cmd）。

## 常用参数

| 参数 | 说明 |
|---|---|
| `--style` | `transparent`（透明主体卡，默认）/ `full-bleed`（整幅卡面） |
| `--name` / `--slug` | 卡片显示名 / 卡片 id |
| `--model` | 本地抠图模型，默认 `birefnet-massive`（通用、体积小质量高） |
| `--title` / `--desc` | 卡面标题 / 描述文字（可选） |
| `--background` / `--face` / `--frame` / `--seal` | DIY 素材层（可选，见下） |
| `--effect` | 3D 网页卡动画特效：`none`（默认）/ `love` 冒爱心 / `sparkle` 冒闪光 |
| `--no-compose` | 只出资产、不合成 2D 预览卡图 |
| `--json` | 输出 JSON 结果 |

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

- 默认本地引擎：`rembg + birefnet-massive`（通用显著性分割，人像/动物/物体通吃，离线免费；模型约 970MB）
- **动漫/插画素材推荐**：加 `--model isnet-anime`（动漫特化，发丝保留更好、边缘更细，模型仅约 176MB，体积小 5 倍）
- 引擎抽象在 `engine.py`，未来可插拔火山引擎等 API 引擎（`create_engine()` 注册）
- 模型存放在 `models/` 目录（默认项目内；缺失时首次抠图自动下载，之后离线可用）
- 启动时会在控制台列出各模型已下载/未下载状态，未下载的模型首次使用自动下载

## 环境

- Python 虚拟环境：`.venv`（用系统 Python 3.10 创建，避免依赖冲突；`setup.cmd` 自动创建）
- 依赖：`requirements.txt`（rembg + Pillow）
- 调用入口：`webui.cmd` / `cardforge.cmd`（自动检测环境，缺失时先部署再运行）

## 卡牌效果在线预览

GitHub 不直接渲染 HTML，仓库内 `preview/` 目录存放自包含的卡牌预览页（图片已内嵌，不依赖本地资源），点击以下链接即可查看 3D 卡效果（拖动可旋转，由 raw.githack 在线渲染）：

- [🃏 莲之空双人组（hasunosora-duo）](https://raw.githack.com/DeeMo8848/cardforge/main/preview/hasunosora-duo.html)

> 重新生成预览页：`.venv\Scripts\python.exe scripts\make_gh_preview.py <卡牌id>`

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
