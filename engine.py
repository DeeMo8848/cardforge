"""抠图引擎：本地 rembg + BiRefNet，预留 API 引擎扩展点。

扩展新引擎：子类化 MattingEngine 并实现 remove_background()，
然后在 create_engine() 中按名称注册即可。
"""
from __future__ import annotations

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

DEFAULT_MODEL = "birefnet-general"

# 常用模型名 → 本地文件名（用于缺失提示，缺失时 rembg 自动下载）
_MODEL_FILES = {
    "birefnet-massive": ("birefnet-massive.onnx", "~970MB"),
    "birefnet-portrait": ("birefnet-portrait.onnx", "~970MB"),
    "birefnet-general": ("birefnet-general.onnx", "~970MB"),
    "birefnet-general-lite": ("birefnet-general-lite.onnx", "~130MB"),
    "isnet-anime": ("isnet-anime.onnx", "~176MB"),
    "isnet-general-use": ("isnet-general-use.onnx", "~176MB"),
    "u2net": ("u2net.onnx", "~176MB"),
    "u2netp": ("u2netp.onnx", "~4MB"),
    "u2net_human_seg": ("u2net_human_seg.onnx", "~176MB"),
    "silueta": ("silueta.onnx", "~43MB"),
    "bria-rmbg": ("rmbg-2.0.onnx", "~176MB"),
}


def model_status() -> list[str]:
    """已下载/缺失的模型清单（用于启动提示）。"""
    lines = []
    for name, (fname, size) in _MODEL_FILES.items():
        if (MODELS_DIR / fname).exists():
            lines.append(f"  ✅ {name}（{size}）")
        else:
            lines.append(f"  ⏳ {name}（{size}）未下载，首次使用时自动下载")
    return lines


def _warn_if_model_missing(model: str) -> None:
    info = _MODEL_FILES.get(model)
    if info is None:
        return
    fname, size = info
    if not (MODELS_DIR / fname).exists():
        print(
            f"[cardforge] 模型「{model}」未找到（{fname}，{size}），"
            f"正在自动下载到 {MODELS_DIR} ..."
        )

SUPPORTED_MODELS = {
    "none": "不使用抠图（整图直接作为卡面素材）",
    "birefnet-massive": "多数据集通用分割，质量最高（~970MB）",
    "birefnet-general": "通用分割（默认，速度与质量平衡）",
    "birefnet-general-lite": "通用分割轻量版",
    "birefnet-portrait": "人像特化分割",
    "isnet-anime": "动漫插画特化，体积小质量好（适合二次元封面）",
    "isnet-general-use": "通用分割，速度快",
    "bria-rmbg": "RMBG-2.0 通用分割（~1GB）",
    "u2net": "经典通用分割",
    "u2netp": "u2net 轻量版",
    "u2net_human_seg": "人像专用",
    "silueta": "u2net 变体",
    "sam": "SAM 交互式分割",
}


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
    """本地引擎：rembg 库 + BiRefNet ONNX 模型，离线运行。"""

    name = "local"

    def __init__(self, model: str = DEFAULT_MODEL) -> None:
        super().__init__(model)
        self._session = None

    def _get_session(self):
        """会话惰性加载并缓存（同一进程内多次抠图只加载一次模型）。"""
        if self._session is None:
            from rembg import new_session

            _warn_if_model_missing(self.model)
            self._session = new_session(self.model)
        return self._session

    def remove_background(self, image: Image.Image) -> Image.Image:
        from rembg import remove

        result = remove(image.convert("RGB"), session=self._get_session())
        result = result.convert("RGBA")
        if result.size != image.size:
            result = result.resize(image.size, Image.LANCZOS)
        return result


def create_engine(engine_name: str = "local", model: str = DEFAULT_MODEL) -> MattingEngine:
    """引擎工厂。engine_name: local/rembg（本地），后续可注册火山引擎等 API 引擎。"""
    if engine_name in ("local", "rembg", "auto"):
        return RembgEngine(model)
    raise ValueError(f"未知抠图引擎: {engine_name}（当前支持: local）")
