"""抠图引擎：本地 rembg + BiRefNet，预留 API 引擎扩展点。

扩展新引擎：子类化 MattingEngine 并实现 remove_background()，
然后在 create_engine() 中按名称注册即可。
"""
from __future__ import annotations

import json
import os
from abc import ABC, abstractmethod
from pathlib import Path

from PIL import Image

# 模型目录：默认项目 models/，可用环境变量 CARDFORGE_MODELS_DIR 指向共享位置
# （GitHub 仓库不含模型，缺失时 rembg 首次使用会自动下载到该目录）
MODELS_DIR = Path(
    os.environ.get("CARDFORGE_MODELS_DIR") or (Path(__file__).resolve().parent / "models")
)
os.environ.setdefault("U2NET_HOME", str(MODELS_DIR))
MODELS_DIR.mkdir(parents=True, exist_ok=True)

# ★ 默认模型：动漫/插画特化的 isnet-anime 之 int8 量化版。
#   相比原来的 birefnet-general-lite，实测（输入 1450×2051）：
#     加载 3.60s → 0.21s，推理 15.71s → 1.01s，峰值内存 5356MB → 475MB。
#   质量上（与人工抠图对比 IoU）0.9876，肉眼边缘与 fp32 无异 ——
#   这正是 2GB 小内存服务器原来会 OOM 的根因所在，换掉后即可安全本地抠图。
DEFAULT_MODEL = "isnet-anime-q8"

# isnet-anime 的 int8 量化（QDQ）版本自量化参数
_Q8_SOURCE = "isnet-anime"       # 量化来源：rembg 官方 fp32 模型（缺失时自动下载）
_Q8_TARGET = "isnet-anime-q8"    # 量化产物：本项目新增的模型名
_Q8_FILENAME = "isnet-anime-q8.onnx"

# 常用模型名 → 本地文件名（用于缺失提示，缺失时 rembg 自动下载）
_MODEL_FILES = {
    "birefnet-massive": ("birefnet-massive.onnx", "~970MB"),
    "birefnet-portrait": ("birefnet-portrait.onnx", "~970MB"),
    "birefnet-general": ("birefnet-general.onnx", "~970MB"),
    "birefnet-general-lite": ("birefnet-general-lite.onnx", "~130MB"),
    "isnet-anime": ("isnet-anime.onnx", "~176MB"),
    "isnet-anime-q8": (_Q8_FILENAME, "~42MB"),
    "isnet-general-use": ("isnet-general-use.onnx", "~176MB"),
    "u2net": ("u2net.onnx", "~176MB"),
    "u2netp": ("u2netp.onnx", "~4MB"),
    "u2net_human_seg": ("u2net_human_seg.onnx", "~176MB"),
    "silueta": ("silueta.onnx", "~43MB"),
    "bria-rmbg": ("rmbg-2.0.onnx", "~176MB"),
}

# ★ 每个模型的「推理峰值内存需求」（MB）—— 注意是【峰值】不是权重体积。
#   载入权重只是起点，onnxruntime 推理时还要按 输入尺寸 × 通道 × 网络中间层数
#   分配大量临时张量，实测峰值远大于权重。
#   ★ 2026-10-12 实测校正（Windows，输入 1450×2051，1024² 推理，2 线程，
#     关闭 CPU 内存池）—— 旧估值偏差很大，这里改为以实测峰值为准并向上取整：
#       isnet-anime-q8        475MB（旧估无）
#       isnet-anime (fp32)    916MB（旧估 1800）
#       birefnet-general-lite 5356MB（旧估 2000 —— 严重低估，正是 OOM 元凶）
#   为什么必须有这道守卫（事故记录 2026-09-21）：
#   在 1.6GB 内存的 ECS 上跑 BiRefNet-lite，机器被 OOM 打死 ——
#   期间 TCP 端口全部能连上，但 SSH/HTTP 一律零响应。
#   ★ 关键：这种僵死【不会自行恢复】，等待无任何效果，
#     最终只能登录云控制台强制重启整台服务器才恢复。
#   也就是说代价不是「慢一下」，而是「服务彻底中断 + 必须人工救场」，
#   所以宁可不抠图（降级/改走 API），也绝不能硬跑。
_MODEL_MEM_MB = {
    "birefnet-massive": 12000,
    "birefnet-portrait": 12000,
    "birefnet-general": 12000,
    # 实测峰值 5356MB（事故现场就是它：权重仅 130MB，却把 1.6GB 机器打死）
    "birefnet-general-lite": 5400,
    "bria-rmbg": 2400,
    "isnet-anime": 950,
    # int8 量化版：实测峰值仅 475MB，比 silueta 还轻，适合小内存服务器
    "isnet-anime-q8": 600,
    "isnet-general-use": 1600,
    "u2net": 1800,
    "u2net_human_seg": 1800,
    "silueta": 800,
    "u2netp": 350,
}

# 内存不足时的降级链：必须【由重到轻】单调递减，取第一个「装得下」的。
_FALLBACK_CHAIN = (
    "birefnet-general-lite",  # 5400MB
    "isnet-anime",            # 950MB
    "silueta",                # 800MB
    "isnet-anime-q8",         # 600MB
    "u2netp",                 # 350MB
)

# 低于这个可用内存就完全不跑本地抠图（连最轻的模型也撑不住）
_MIN_AVAILABLE_MB = 350

# ★ 给「抠图之外的东西」预留的内存（MB）。
#   bot 进程本身、PIL/Skia 渲染、系统缓存都要吃内存，
#   拿 MemAvailable 全给抠图模型必然翻车。留 400MB 兜底。
_RESERVE_MB = 400


def _available_mem_mb() -> float | None:
    """当前可用内存（MB）。取不到返回 None。

    Linux 用 /proc/meminfo 的 MemAvailable（已扣除 cache/buffer 的真实可用）；
    其它平台退回 psutil，再不行返回 None（此时不做守卫，保持原有行为）。
    """
    try:
        with open("/proc/meminfo", "r") as f:
            info = {}
            for line in f:
                k, _, v = line.partition(":")
                info[k.strip()] = v.strip()
        for key in ("MemAvailable",):
            if key in info:
                return int(info[key].split()[0]) / 1024.0
    except Exception:
        pass
    try:
        import psutil  # type: ignore
        return psutil.virtual_memory().available / (1024 * 1024)
    except Exception:
        return None


def _mem_guard(model: str) -> tuple[str, str | None]:
    """内存守卫：返回 (实际可用的模型名, 提示信息或 None)。

    可用内存充足 → 原样返回；不足 → 沿 _FALLBACK_CHAIN 降级；
    连最轻的也装不下 → 返回 ("none", 原因)，调用方应改用不抠图模式。

    ★ 判定用「可用内存 - _RESERVE_MB」而不是全部可用内存：
      抠图不是唯一吃内存的东西，留出余量才不会把机器推向 OOM 边缘。
    """
    avail = _available_mem_mb()
    if avail is None:
        return model, None
    usable = avail - _RESERVE_MB
    need = _MODEL_MEM_MB.get(model)
    if need is None:
        return model, None
    if usable >= need:
        return model, None
    for cand in _FALLBACK_CHAIN:
        cneed = _MODEL_MEM_MB.get(cand)
        if cneed and usable >= cneed:
            return cand, (
                f"可用内存 {avail:.0f}MB（扣除预留 {_RESERVE_MB}MB 后约 {usable:.0f}MB），"
                f"不足以加载「{model}」（约需 {need}MB），"
                f"已自动降级为「{cand}」（约需 {cneed}MB）"
            )
    return "none", (
        f"可用内存仅 {avail:.0f}MB，连最轻的抠图模型也放不下，"
        f"本次改为「不抠图」模式（整图作为卡面素材）。"
        f"建议在 settings.json 配置抠图 API（matting_api）或迁到更大内存的机器"
    )


def model_status() -> list[str]:
    """已下载/缺失的模型清单（用于启动提示）。"""
    lines = []
    for name, (fname, size) in _MODEL_FILES.items():
        if (MODELS_DIR / fname).exists():
            lines.append(f"  ✅ {name}（{size}）")
        else:
            lines.append(f"  ⏳ {name}（{size}）未下载，首次使用时自动下载")
    # ★ 顺带报一次内存守卫结论：小内存机器上会提示默认模型将被降级
    avail = _available_mem_mb()
    if avail is not None:
        picked, note = _mem_guard(DEFAULT_MODEL)
        head = f"  🧠 可用内存 {avail:.0f}MB"
        if note is None:
            lines.append(f"{head}，足够运行默认模型「{DEFAULT_MODEL}」")
        else:
            lines.append(f"{head} → {note}")
    return lines


def _warn_if_model_missing(model: str) -> None:
    info = _MODEL_FILES.get(model)
    if info is None:
        return
    fname, size = info
    if (MODELS_DIR / fname).exists():
        return
    if model == _Q8_TARGET:
        print(
            f"[cardforge] 模型「{model}」未找到（{fname}，{size}），"
            f"首次使用时将由 isnet-anime 自动量化生成 ..."
        )
    else:
        print(
            f"[cardforge] 模型「{model}」未找到（{fname}，{size}），"
            f"正在自动下载到 {MODELS_DIR} ..."
        )


def _calibration_feeds(limit: int = 6) -> list:
    """为量化收集校准输入（模型输入格式：1×3×1024×1024 float32）。

    QDQ int8 量化需要校准数据来确定激活值的量化范围，用不用真图差别很大：
    实测用项目自带素材校准，抠图 IoU 0.9878（与 fp32 几乎相同）；
    改用合成噪声则掉到 0.8630 —— 所以优先用 assets/ 下的真实素材
    （它们正是本模型要处理的二次元封面这类图），实在没有才退回合成图。
    """
    import numpy as np
    from PIL import Image

    exts = {".jpg", ".jpeg", ".png", ".webp", ".bmp"}
    root = Path(__file__).resolve().parent / "assets"
    picks: list[Path] = []
    for sub, quota in (("backgrounds", 3), ("faces", 3)):
        d = root / sub
        if not d.is_dir():
            continue
        n = 0
        for f in sorted(d.iterdir()):
            if f.is_file() and f.suffix.lower() in exts:
                picks.append(f)
                n += 1
                if n >= quota:
                    break
    if not picks and root.is_dir():
        picks = [f for f in sorted(root.rglob("*"))
                 if f.is_file() and f.suffix.lower() in exts][:limit]

    mean, std, size = (0.485, 0.456, 0.406), (1.0, 1.0, 1.0), (1024, 1024)

    def prep(a: "np.ndarray") -> "np.ndarray":
        a = a.astype(np.float64)
        a = a / max(float(a.max()), 1e-6)
        t = np.zeros_like(a)
        for i, m in enumerate(mean):
            t[:, :, i] = (a[:, :, i] - m) / std[i]
        return t.transpose(2, 0, 1)[None].astype(np.float32)

    feeds = []
    for f in picks:
        try:
            img = Image.open(f).convert("RGB").resize(size, Image.Resampling.LANCZOS)
        except Exception:
            continue
        feeds.append(prep(np.array(img)))
    if not feeds:
        print("[cardforge] 未找到可用于校准的素材图，改用合成图量化（抠图质量会下降）")
        rng = np.random.default_rng(0)
        for _ in range(limit):
            a = rng.integers(0, 256, (1024, 1024, 3), dtype=np.uint8)
            a = np.array(Image.fromarray(a).resize((32, 32)).resize(size, Image.Resampling.BILINEAR))
            feeds.append(prep(a))
    return feeds


def _ensure_anime_q8() -> Path:
    """确保 isnet-anime 的 int8 量化模型存在，返回其路径。

    本项目不依赖第三方个人仓库，改为「自己量化」：
    以 rembg 官方 fp32 的 isnet-anime.onnx（缺失时会自动下载）为源，
    用项目自带素材做校准，现场做 QDQ int8 静态量化生成 isnet-anime-q8.onnx。
    实测 168MB → 42MB、推理峰值 916MB → 475MB、耗时 2.21s → 1.01s，
    而与 fp32 的互相 IoU 达 0.9982（量化几乎零损失）。

    量化属一次性开销（约 20s），产物缓存于 models/，后续启动直接加载。
    需要 onnx 包（见 requirements.txt）：pip install onnx
    """
    target = MODELS_DIR / _Q8_FILENAME
    if target.exists():
        return target

    source = MODELS_DIR / f"{_Q8_SOURCE}.onnx"
    if not source.exists():
        # 复用 rembg 官方下载逻辑取 fp32 源模型（无需用户手动准备）
        from rembg.sessions.dis_anime import DisSession

        _warn_if_model_missing(_Q8_SOURCE)
        source = Path(DisSession.download_models())

    try:
        import onnx
        from onnxruntime.quantization import (
            CalibrationDataReader,
            QuantFormat,
            QuantType,
            quantize_static,
        )
    except ImportError as e:
        raise RuntimeError(
            "生成 isnet-anime-q8 需要量化支持，请先安装：pip install onnx；"
            f"或直接提供量化好的模型文件 {target}"
        ) from e

    class _Reader(CalibrationDataReader):
        def __init__(self, items, input_name):
            self._it = iter(items)
            self._name = input_name

        def get_next(self):
            item = next(self._it, None)
            return None if item is None else {self._name: item}

    print(f"[cardforge] 首次生成「{_Q8_TARGET}」（int8 量化，约需数十秒）...")
    tmp = MODELS_DIR / (_Q8_FILENAME + ".part")
    try:
        # ★ 传 ModelProto 而不是文件路径：onnxruntime 量化时会在【模型同级目录】
        #   用 onnx 的 C++ shape-inference 写一个 -inferred.onnx，中文/空格路径下
        #   该步骤会静默失败（FileNotFoundError）。传 ModelProto 后中间文件落在
        #   系统临时目录，可绕开非 ASCII 路径问题。
        model = onnx.load(str(source))
        input_name = model.graph.input[0].name
        # 用 QDQ 静态量化（weight/activation 均 int8）：
        # quantize_dynamic 会生成 ConvInteger，而本机 onnxruntime 的 CPU EP
        # 没有该算子实现（NOT_IMPLEMENTED），故必须走 QDQ 格式。
        quantize_static(
            model,
            str(tmp),
            _Reader(_calibration_feeds(), input_name),
            quant_format=QuantFormat.QDQ,
            per_channel=False,
            activation_type=QuantType.QUInt8,
            weight_type=QuantType.QInt8,
        )
        os.replace(tmp, target)
    finally:
        if tmp.exists():
            tmp.unlink(missing_ok=True)
    print(f"[cardforge] 已生成 {target}（{target.stat().st_size / 1024 / 1024:.1f}MB）")
    return target

SUPPORTED_MODELS = {
    "none": "不使用抠图（整图直接作为卡面素材）",
    "birefnet-massive": "多数据集通用分割，质量最高（~970MB）",
    "birefnet-general": "通用分割（速度与质量平衡）",
    "birefnet-general-lite": "通用分割轻量版（质量高但内存峰值大，约需 5.4GB）",
    "birefnet-portrait": "人像特化分割",
    "isnet-anime-q8": "动漫插画特化 int8 量化版（默认，体积小/内存低/速度快）",
    "isnet-anime": "动漫插画特化，体积小质量好（适合二次元封面）",
    "isnet-general-use": "通用分割，速度快",
    "bria-rmbg": "RMBG-2.0 通用分割（~1GB）",
    "u2net": "经典通用分割",
    "u2netp": "u2net 轻量版",
    "u2net_human_seg": "人像专用",
    "silueta": "u2net 变体",
    "sam": "SAM 交互式分割",
}


def get_api_config() -> dict:
    """读取 settings.json 中的抠图 API 配置；AccessKey 未填写时返回 {}（不使用 API）。"""
    try:
        s = json.loads((Path(__file__).resolve().parent / "settings.json").read_text(encoding="utf-8"))
    except Exception:
        return {}
    cfg = s.get("matting_api") or {}
    if isinstance(cfg, dict) and cfg.get("access_key_id") and cfg.get("access_key_secret"):
        return cfg
    return {}


class MattingEngine(ABC):
    """抠图引擎抽象：输入 PIL Image，输出同尺寸 RGBA（透明背景）。"""

    name = "base"

    def __init__(self, model: str = DEFAULT_MODEL) -> None:
        self.model = model

    @abstractmethod
    def remove_background(self, image: Image.Image) -> Image.Image:
        """返回同尺寸 RGBA 图：背景透明、前景完整保留。"""

    def describe(self) -> str:
        return f"{self.name}({self.model})"


class RembgEngine(MattingEngine):
    """本地引擎：rembg 库 + BiRefNet ONNX 模型，离线运行。

    ★ 加载前会做内存守卫（见 _mem_guard）：内存不足时自动降级到更小的模型，
      避免在小内存机器（如 1.6GB 的 ECS）上把系统拖入 OOM 僵死。
      降级结果可用 self.degraded_note 读取；若降级为 "none"，
      则 remove_background() 直接原图返回（即不抠图）。
    """

    name = "local"

    def __init__(self, model: str = DEFAULT_MODEL) -> None:
        super().__init__(model)
        self._session = None
        self.degraded_note: str | None = None
        # 只有真正要跑本地模型时才做守卫；"none" 代表用户主动选的不抠图
        if model not in ("none", "api"):
            picked, note = _mem_guard(model)
            self.model = picked
            self.degraded_note = note
            if note:
                print(f"[cardforge] {note}")

    def _get_session(self):
        """会话惰性加载并缓存（同一进程内多次抠图只加载一次模型）。"""
        if self._session is None:
            if self.model == "none":
                # 内存守卫判定连最轻的模型都放不下 → 不加载模型，走原图直通
                return None
            _warn_if_model_missing(self.model)
            import os

            import onnxruntime as ort
            from rembg import sessions

            if self.model == _Q8_TARGET:
                # 本项目自量化的 isnet-anime int8 版：子类化官方 DisSession，
                # 仅把模型路径改指到量化产物，预处理/后处理完全复用。
                from rembg.sessions.dis_anime import DisSession

                q8_path = _ensure_anime_q8()

                class _DisSessionQ8(DisSession):
                    @classmethod
                    def name(cls, *args, **kwargs):
                        return _Q8_TARGET

                    @classmethod
                    def download_models(cls, *args, **kwargs):
                        return str(q8_path)

                cls = _DisSessionQ8
            else:
                cls = next((sc for sc in sessions.sessions_class if sc.name() == self.model), None)
            if cls is None:
                raise ValueError(f"未知抠图模型: {self.model}")
            opts = ort.SessionOptions()
            # 关闭 onnxruntime CPU 内存池：推理中间内存用完即归还系统。
            # 实测 BiRefNet 系列开启时进程常驻 ~6.5GB（分配后不归还），
            # 关闭后常驻 ~0.5GB，速度损失 <1s（大图 3072x4096 约 8s→10s）
            opts.enable_cpu_mem_arena = False
            if "OMP_NUM_THREADS" in os.environ:
                threads = int(os.environ["OMP_NUM_THREADS"])
                opts.inter_op_num_threads = threads
                opts.intra_op_num_threads = threads
            else:
                # ★ 小内存机器上限制线程数：onnxruntime 默认按核数开线程，
                #   每线程都占栈与临时缓冲，在 1.6GB 机器上是压垮内存的帮凶。
                threads = min(2, os.cpu_count() or 1)
                opts.inter_op_num_threads = threads
                opts.intra_op_num_threads = threads
            self._session = cls(self.model, opts)
        return self._session

    def remove_background(self, image: Image.Image) -> Image.Image:
        session = self._get_session()
        if session is None:
            # 内存不足走到这里：不抠图，整图转 RGBA 直接当素材
            return image.convert("RGBA")
        from rembg import remove

        result = remove(image.convert("RGB"), session=session)
        result = result.convert("RGBA")
        if result.size != image.size:
            result = result.resize(image.size, Image.LANCZOS)
        return result


class ApiMattingEngine(MattingEngine):
    """云端引擎：阿里云视觉智能开放平台·分割抠图（SegmentCommonImage）。

    本地图片以文件流形式传给官方 SDK（Advance 请求自动上传临时 OSS），
    返回主体透明 PNG 后下载回本地。SDK 仅在真正调用时惰性加载。
    """

    name = "api"

    def __init__(self, model: str = "api") -> None:
        super().__init__(model)
        self._client = None

    def _get_client(self):
        if self._client is None:
            cfg = get_api_config()
            if not cfg:
                raise RuntimeError(
                    "未配置抠图 API：请在 settings.json 的 matting_api 填写阿里云 AccessKey"
                )
            try:
                from alibabacloud_imageseg20191230.client import Client
                from alibabacloud_tea_openapi.models import Config
            except ImportError as e:
                raise RuntimeError(
                    "缺少阿里云 SDK，请先安装："
                    "pip install alibabacloud_imageseg20191230 alibabacloud_tea_openapi alibabacloud_tea_util"
                ) from e
            self._client = Client(Config(
                access_key_id=cfg["access_key_id"],
                access_key_secret=cfg["access_key_secret"],
                endpoint="imageseg.cn-shanghai.aliyuncs.com",
                region_id="cn-shanghai",
            ))
        return self._client

    def remove_background(self, image: Image.Image) -> Image.Image:
        import io
        import urllib.request

        client = self._get_client()
        try:
            from alibabacloud_imageseg20191230.models import SegmentCommonImageAdvanceRequest
            from alibabacloud_tea_util.models import RuntimeOptions
        except ImportError as e:
            raise RuntimeError(
                "缺少阿里云 SDK，请先安装："
                "pip install alibabacloud_imageseg20191230 alibabacloud_tea_openapi alibabacloud_tea_util"
            ) from e

        buf = io.BytesIO()
        image.convert("RGBA").save(buf, format="PNG")
        request = SegmentCommonImageAdvanceRequest()
        request.image_urlobject = io.BytesIO(buf.getvalue())
        try:
            response = client.segment_common_image_advance(request, RuntimeOptions())
        except Exception as e:
            raise RuntimeError(f"阿里云分割抠图调用失败: {e}") from e
        result_url = response.body.data.image_url
        with urllib.request.urlopen(result_url, timeout=60) as r:
            raw = r.read()
        result = Image.open(io.BytesIO(raw)).convert("RGBA")
        if result.size != image.size:
            result = result.resize(image.size, Image.LANCZOS)
        return result


def create_engine(engine_name: str = "local", model: str = DEFAULT_MODEL) -> MattingEngine:
    """引擎工厂。engine_name: local/rembg（本地）或 api（阿里云分割抠图）。"""
    if engine_name in ("local", "rembg", "auto"):
        return RembgEngine(model)
    if engine_name == "api":
        return ApiMattingEngine(model)
    raise ValueError(f"未知抠图引擎: {engine_name}（当前支持: local、api）")
