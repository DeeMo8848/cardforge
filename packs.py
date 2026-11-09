"""cardforge 素材包（asset pack）系统。

一个素材包 = `packs/<文件夹>/`，包根放**一份** `pack.json` 索引（嵌套的 pack.json 忽略）：

    {
      "id": "sakura-pack",        // 必填：包唯一 id（字母/数字/._-）
      "name": "樱色素材包",        // 必填：显示名
      "author": "xxx",            // 可选
      "description": "樱花背景 + 金边边框",  // 可选
      "thumbnail": "cover.png",   // 可选：相对包根的缩略图
      "format": 1,                // 可选：格式版本（保留字段）
      "assets": [                 // 可选：素材声明（目录，递归扫描）
        {"type": "backgrounds", "dir": "bg"},
        {"type": "frames", "dir": "frames"}
      ]
    }

- 不写 `assets` 时按标准目录名自动识别：backgrounds / faces / frames / seals / backs。
- `type` 只认这 5 类，未知类型跳过；`dir` 相对包根，`.` 表示包根。
- 导入（zip / 文件夹）都【复制】进 `packs/<id>/`，源文件不动；删除只删这里面的。
- 启用/禁用是逻辑开关（记录在 picks.json），不搬动文件。
- 素材「键」：内置素材 = 文件名；包内素材 = `包id/相对路径`（两者天然不重名）。
"""
from __future__ import annotations

import json
import re
import shutil
import tempfile
import zipfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
ASSETS_DIR = PROJECT_ROOT / "assets"
PACKS_DIR = PROJECT_ROOT / "packs"
STATE_FILE = PROJECT_ROOT / "packs.json"      # 本地启用/禁用状态（不入库）
INDEX_NAME = "pack.json"

CORE_TYPES = ("backgrounds", "faces", "frames", "seals", "backs")
_IMG_EXTS = (".png", ".jpg", ".jpeg", ".webp", ".gif")
_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
_MAX_EXTRACT_BYTES = 512 * 1024 * 1024
_MAX_EXTRACT_FILES = 5000


# ---------- 基础 ----------

def _valid_id(pid: str) -> bool:
    return bool(pid) and pid not in (".", "..") and bool(_ID_RE.match(pid))


def _is_img(p: Path) -> bool:
    return p.is_file() and p.suffix.lower() in _IMG_EXTS


def _iter_images(base: Path):
    """递归列举目录下的图片文件（按路径排序）。"""
    if not base.is_dir():
        return
    for p in sorted(base.rglob("*")):
        if _is_img(p):
            yield p


def _slug_id(text: str) -> str:
    s = re.sub(r"[^A-Za-z0-9._-]+", "-", (text or "").strip()).strip("-._")
    return s or "pack"


# ---------- 状态（启用/禁用） ----------

def _load_state() -> dict:
    try:
        d = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        if isinstance(d, dict):
            return d
    except (OSError, ValueError):
        pass
    return {}


def _save_state(state: dict) -> None:
    try:
        STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        pass


def _disabled_ids() -> set[str]:
    d = _load_state().get("disabled")
    return set(d) if isinstance(d, list) else set()


def set_enabled(pid: str, enabled: bool) -> None:
    state = _load_state()
    dis = set(state.get("disabled") or [])
    if enabled:
        dis.discard(pid)
    else:
        dis.add(pid)
    state["disabled"] = sorted(dis)
    _save_state(state)


# ---------- 扫描 / 索引解析 ----------

def _declared_assets(idx: dict, pack_dir: Path) -> list[tuple[str, str]]:
    """返回 (type, dir) 列表。未写 assets 时按标准目录名自动识别。"""
    out: list[tuple[str, str]] = []
    raw = idx.get("assets")
    if isinstance(raw, list) and raw:
        for item in raw:
            if not isinstance(item, dict):
                continue
            t = str(item.get("type") or "").strip()
            sub = str(item.get("dir") or ".").strip() or "."
            if t in CORE_TYPES:
                out.append((t, sub))
    else:
        for t in CORE_TYPES:
            if (pack_dir / t).is_dir():
                out.append((t, t))
    # 去重
    seen = set()
    uniq = []
    for item in out:
        if item not in seen:
            seen.add(item)
            uniq.append(item)
    return uniq


def _load_pack(pack_dir: Path, taken_ids: set[str], disabled: set[str]) -> dict:
    info = {
        "id": "", "name": pack_dir.name, "author": "", "description": "",
        "thumbnail": None, "format": None, "dir": pack_dir.name,
        "path": pack_dir, "declared": [], "counts": {}, "total": 0,
        "enabled": True, "error": None,
    }
    idx_path = pack_dir / INDEX_NAME
    if not idx_path.is_file():
        info["error"] = f"缺少索引文件 {INDEX_NAME}"
        return info
    try:
        idx = json.loads(idx_path.read_text(encoding="utf-8"))
        if not isinstance(idx, dict):
            raise ValueError("索引必须是 JSON 对象")
    except (OSError, ValueError) as e:
        info["error"] = f"索引无法解析：{e}"
        return info

    pid = str(idx.get("id") or "").strip()
    if not pid:
        pid = _slug_id(pack_dir.name)
    name = str(idx.get("name") or "").strip()
    if not _valid_id(pid):
        info["error"] = f"id 非法：{pid!r}（只允许字母/数字/._-）"
        return info
    if not name:
        info["error"] = "索引缺少必填字段 name"
        return info
    if pid in taken_ids:
        info["error"] = f"id 冲突：{pid} 已被其它素材包占用"
        return info

    info["id"] = pid
    info["name"] = name
    info["author"] = str(idx.get("author") or "").strip()
    info["description"] = str(idx.get("description") or "").strip()
    fmt = idx.get("format")
    info["format"] = fmt if isinstance(fmt, (int, str)) else None
    th = str(idx.get("thumbnail") or "").strip()
    if th and _is_img(pack_dir / th):
        info["thumbnail"] = th.replace("\\", "/")
    info["declared"] = _declared_assets(idx, pack_dir)
    taken_ids.add(pid)
    for t, sub in info["declared"]:
        base = pack_dir if sub in ("", ".") else (pack_dir / sub)
        info["counts"][t] = info["counts"].get(t, 0) + sum(1 for _ in _iter_images(base))
    info["total"] = sum(info["counts"].values())
    info["enabled"] = pid not in disabled
    return info


def scan() -> list[dict]:
    """扫描 packs/ 下所有素材包，返回元数据（含 error 标记；坏索引标红用）。"""
    if not PACKS_DIR.is_dir():
        return []
    disabled = _disabled_ids()
    taken: set[str] = set()
    out = []
    for d in sorted(PACKS_DIR.iterdir()):
        if d.is_dir():
            out.append(_load_pack(d, taken, disabled))
    return out


def get_pack(pid: str) -> dict | None:
    for p in scan():
        if p["id"] == pid:
            return p
    return None


# ---------- 合并素材列表 / 路径解析 ----------

def list_assets(atype: str) -> list[dict]:
    """该类型的合并素材列表（内置 + 已启用素材包）。

    每项：{key, name, pack, pack_name, builtin}
    内置：key = 文件名；包内：key = `包id/相对路径`。effect 等非 5 类只回内置。
    """
    out: list[dict] = []
    seen: set[str] = set()
    d = ASSETS_DIR / atype
    if d.is_dir():
        for p in sorted(d.iterdir()):
            if _is_img(p):
                out.append({"key": p.name, "name": p.name, "pack": None,
                            "pack_name": "内置", "builtin": True})
                seen.add(p.name)
    if atype not in CORE_TYPES:
        return out
    for pk in scan():
        if pk["error"] or not pk["enabled"]:
            continue
        for t, sub in pk["declared"]:
            if t != atype:
                continue
            base = pk["path"] if sub in ("", ".") else (pk["path"] / sub)
            for f in _iter_images(base):
                rel = f.relative_to(pk["path"]).as_posix()
                key = f"{pk['id']}/{rel}"
                if key in seen:
                    continue
                seen.add(key)
                out.append({"key": key, "name": f.name, "pack": pk["id"],
                            "pack_name": pk["name"], "builtin": False})
    return out


def pack_file(pid: str, rel: str) -> Path | None:
    """安全解析素材包内的图片文件（防越界）。"""
    if not _valid_id(pid) or not rel:
        return None
    rel = str(rel).replace("\\", "/").strip()
    if not rel or rel.startswith("/") or ".." in rel.split("/"):
        return None
    root = PACKS_DIR / pid
    try:
        root = root.resolve()
    except OSError:
        return None
    if not root.is_dir():
        return None
    try:
        rp = (root / rel).resolve()
    except OSError:
        return None
    if rp != root and root not in rp.parents:
        return None
    return rp if _is_img(rp) else None


def resolve_asset(atype: str, name: str | None) -> Path | None:
    """把素材名解析为绝对路径：内置 assets/<type>/<文件名> 或素材包 packs/<id>/<相对路径>。"""
    if not name:
        return None
    name = str(name).replace("\\", "/").strip()
    if not name:
        return None
    if "/" not in name:
        if name in (".", ".."):
            return None
        p = ASSETS_DIR / atype / name
        return p if _is_img(p) else None
    if atype not in CORE_TYPES:
        return None
    pid, rel = name.split("/", 1)
    return pack_file(pid, rel)


# ---------- 导入 ----------

def _safe_extract(zf: zipfile.ZipFile, dest: Path) -> None:
    """解压（防 zip-slip / 大小与数量限制）。"""
    dest = dest.resolve()
    total = 0
    members = zf.infolist()
    if len(members) > _MAX_EXTRACT_FILES:
        raise ValueError(f"压缩包条目过多（>{_MAX_EXTRACT_FILES}）")
    for m in members:
        name = m.filename.replace("\\", "/")
        if not name or name.startswith("/") or ".." in name.split("/"):
            raise ValueError(f"压缩包含非法路径：{m.filename}")
        total += int(m.file_size)
        if total > _MAX_EXTRACT_BYTES:
            raise ValueError("压缩包解压后过大（>512MB）")
        target = (dest / name).resolve()
        if target != dest and dest not in target.parents:
            raise ValueError(f"压缩包含非法路径：{m.filename}")
        if m.is_dir():
            target.mkdir(parents=True, exist_ok=True)
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        with zf.open(m) as src, open(target, "wb") as out:
            shutil.copyfileobj(src, out)


def _find_pack_root(d: Path, depth: int = 0) -> Path | None:
    """定位包根：顶层有 pack.json 即用它；否则仅在「只有唯一子目录」时下探。"""
    if (d / INDEX_NAME).is_file():
        return d
    if depth >= 3:
        return None
    subs = [x for x in d.iterdir() if x.is_dir()]
    if len(subs) == 1:
        return _find_pack_root(subs[0], depth + 1)
    return None


def _install(root: Path, workspace: Path) -> dict:
    """把已解压的包根复制进 packs/<id>/。"""
    idx_path = root / INDEX_NAME
    try:
        idx = json.loads(idx_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise ValueError(f"索引 pack.json 无法解析：{e}")
    if not isinstance(idx, dict):
        raise ValueError("索引 pack.json 必须是 JSON 对象")
    pid = str(idx.get("id") or "").strip() or _slug_id(root.name)
    if not _valid_id(pid):
        raise ValueError(f"索引 id 非法：{pid!r}（只允许字母/数字/._-）")
    if not str(idx.get("name") or "").strip():
        raise ValueError("索引缺少必填字段 name")
    dest = PACKS_DIR / pid
    if dest.exists():
        raise ValueError(f"已存在同名素材包 id「{pid}」，请先删除或改 id")
    PACKS_DIR.mkdir(parents=True, exist_ok=True)
    shutil.copytree(root, dest)
    set_enabled(pid, True)
    return {"ok": True, "id": pid, "name": str(idx.get("name")).strip()}


def import_zip(filename: str, data: bytes) -> dict:
    """导入 zip 压缩包（复制进 packs/）。"""
    workspace = Path(tempfile.mkdtemp(prefix="cf_pack_"))
    try:
        zpath = workspace / "pack.zip"
        zpath.write_bytes(data)
        extract_dir = workspace / "x"
        extract_dir.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(zpath) as zf:
            _safe_extract(zf, extract_dir)
        root = _find_pack_root(extract_dir)
        if root is None:
            raise ValueError("压缩包里没找到 pack.json（请把索引放在包的顶层）")
        return _install(root, workspace)
    except zipfile.BadZipFile:
        raise ValueError("不是有效的 zip 压缩包")
    finally:
        shutil.rmtree(workspace, ignore_errors=True)


def import_folder(files: list[dict]) -> dict:
    """导入文件夹（浏览器 webkitdirectory 逐文件上传）。

    files：每项 {path: 相对路径, b64: base64 内容}。
    """
    import base64

    if not files:
        raise ValueError("没有收到文件")
    if len(files) > _MAX_EXTRACT_FILES:
        raise ValueError(f"文件过多（>{_MAX_EXTRACT_FILES}）")
    workspace = Path(tempfile.mkdtemp(prefix="cf_pack_"))
    try:
        extract_dir = workspace / "x"
        total = 0
        for f in files:
            rel = str(f.get("path") or "").replace("\\", "/").lstrip("/")
            if not rel or ".." in rel.split("/"):
                continue
            raw = base64.b64decode(f.get("b64") or "")
            total += len(raw)
            if total > _MAX_EXTRACT_BYTES:
                raise ValueError("文件夹过大（>512MB）")
            target = extract_dir / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(raw)
        root = _find_pack_root(extract_dir)
        if root is None:
            raise ValueError("所选文件夹里没找到 pack.json（请把索引放在文件夹顶层）")
        return _install(root, workspace)
    finally:
        shutil.rmtree(workspace, ignore_errors=True)


def delete_pack(pid: str) -> bool:
    """删除素材包（只删 packs/<id>/，不动内置 assets/ 与独立素材）。"""
    if not _valid_id(pid):
        raise ValueError("素材包 id 非法")
    d = PACKS_DIR / pid
    if not d.is_dir():
        return False
    shutil.rmtree(d)
    state = _load_state()
    dis = set(state.get("disabled") or [])
    dis.discard(pid)
    state["disabled"] = sorted(dis)
    _save_state(state)
    return True