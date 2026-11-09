#!/usr/bin/env bash
# =====================================================================
#  setup.sh — cardforge Linux/macOS 一键部署（创建 venv + 装依赖）
# ---------------------------------------------------------------------
#  对应 Windows 版 setup.cmd，职责一致。
#
#  venv 位置来源优先级：
#    1. 环境变量 CARDFORGE_VENV     （bot 调用时传入，最明确）
#    2. settings.json 的 "venv" 字段（配套 Python 脚本解析，无需 jq）
#    3. 项目自带 .venv              （cardforge 独立使用时的兜底）
#
#  ★ bot 集成说明：bot 的项目级共享 venv 在 <bot>/tools/.venv，
#    它会通过 CARDFORGE_VENV 或 settings.json 的 venv 字段指过来，
#    这样两个项目共用同一份依赖，不必各装一遍（rembg+onnxruntime 很大）。
#
#  用法：
#    bash setup.sh                # 自动解析 venv 位置
#    bash setup.sh --venv /path   # 显式指定 venv 目录
# =====================================================================
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT" || exit 1

BLUE='\033[36m'; GREEN='\033[32m'; YELLOW='\033[33m'; RED='\033[31m'; NC='\033[0m'
info() { echo -e "${BLUE}[cardforge]${NC} $*"; }
ok()   { echo -e "${GREEN}[cardforge]${NC} $*"; }
warn() { echo -e "${YELLOW}[cardforge]${NC} $*"; }
err()  { echo -e "${RED}[cardforge]${NC} $*"; }

VENV_DIR=""
PIP_INDEX="${CARDFORGE_PIP_INDEX:-}"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --venv)  VENV_DIR="$2"; shift 2 ;;
    --index) PIP_INDEX="$2"; shift 2 ;;
    *) warn "未知参数：$1"; shift ;;
  esac
done

# ---------- 1. 解析 venv 位置 ----------
# 优先级 1：命令行 / 环境变量
if [[ -z "$VENV_DIR" && -n "${CARDFORGE_VENV:-}" ]]; then
  VENV_DIR="$CARDFORGE_VENV"
fi

# 优先级 2：settings.json 的 venv 字段（用 python 解析，不依赖 jq）
if [[ -z "$VENV_DIR" && -f "$ROOT/settings.json" ]]; then
  PY_FOR_JSON=""
  for c in python3 python; do
    command -v "$c" >/dev/null 2>&1 && { PY_FOR_JSON="$c"; break; }
  done
  if [[ -n "$PY_FOR_JSON" ]]; then
    VENV_DIR="$("$PY_FOR_JSON" -c "
import json, sys
try:
    with open(r'$ROOT/settings.json', encoding='utf-8') as f:
        v = (json.load(f).get('venv') or '').strip()
    print(v)
except Exception:
    print('')
" 2>/dev/null)"
  fi
fi

# 优先级 3：项目自带 .venv
if [[ -z "$VENV_DIR" ]]; then
  VENV_DIR="$ROOT/.venv"
fi

# 已有可用 venv 则直接用（不校验它属于谁 —— 共享是设计意图）
if [[ -x "$VENV_DIR/bin/python" || -x "$VENV_DIR/Scripts/python.exe" ]]; then
  ok "venv 已存在：$VENV_DIR，跳过创建"
else
  # ---------- 2. 找基础 Python ----------
  BASE_PY=""
  for c in python3.12 python3.11 python3.10 python3 python; do
    if command -v "$c" >/dev/null 2>&1; then
      v="$("$c" -c 'import sys;print("%d.%d"%sys.version_info[:2])' 2>/dev/null)"
      if [[ -n "$v" ]]; then
        major="${v%%.*}"; minor="${v##*.}"
        if [[ "$major" -eq 3 && "$minor" -ge 10 ]]; then BASE_PY="$c"; break; fi
      fi
    fi
  done
  if [[ -z "$BASE_PY" ]]; then
    err "未找到 Python 3.10+，请先安装（Debian/Ubuntu: apt install python3 python3-venv）"
    exit 1
  fi

  info "创建虚拟环境 $VENV_DIR（使用 $BASE_PY）..."
  mkdir -p "$(dirname "$VENV_DIR")"
  if ! "$BASE_PY" -m venv "$VENV_DIR"; then
    err "创建 venv 失败。Debian/Ubuntu 可能缺少 python3-venv：apt install python3-venv"
    exit 1
  fi
fi

# ---------- 3. 定位解释器 ----------
VENV_PY=""
for rel in "bin/python" "Scripts/python.exe" "bin/python3"; do
  if [[ -x "$VENV_DIR/$rel" ]]; then VENV_PY="$VENV_DIR/$rel"; break; fi
done
if [[ -z "$VENV_PY" ]]; then
  err "在 $VENV_DIR 里找不到 python 解释器"
  exit 1
fi
ok "使用解释器：$VENV_PY ($("$VENV_PY" --version 2>&1))"

# ---------- 4. 装依赖 ----------
info "安装依赖 ..."
PIP_ARGS=(install -r "$ROOT/requirements.txt")
if [[ -n "$PIP_INDEX" ]]; then
  PIP_ARGS=(install -i "$PIP_INDEX" -r "$ROOT/requirements.txt")
fi

# ★ 不要接管道再判退出码 —— $? 会变成管道的退出码，装失败了也会误报成功
"$VENV_PY" -m pip install --upgrade pip -q 2>/dev/null || true
if "$VENV_PY" -m pip "${PIP_ARGS[@]}"; then
  ok "依赖安装完成"
else
  err "依赖安装失败，请检查网络后重试"
  err "（国内服务器可加 --index：bash setup.sh --index https://pypi.tuna.tsinghua.edu.cn/simple）"
  exit 1
fi

# ---------- 5. 记录 venv 位置到 settings.json ----------
"$VENV_PY" - "$ROOT" "$VENV_DIR" <<'PYEOF' || true
import json, os, sys
root, venv = sys.argv[1], sys.argv[2]
path = os.path.join(root, "settings.json")
cfg = {}
if os.path.isfile(path):
    try:
        with open(path, encoding="utf-8") as f:
            cfg = json.load(f)
    except Exception:
        cfg = {}
if cfg.get("venv") != venv:
    cfg["venv"] = venv
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
        print("[cardforge] 已把 venv 路径写回 settings.json")
    except Exception as e:
        print("[cardforge] 写入 settings.json 失败：%s" % e)
PYEOF

echo ""
ok "部署完成！"
echo "    启动 Web UI：bash webui.sh"
echo "    CLI 使用   ：bash cardforge.sh --help"
echo "    （抠图模型不在仓库里：默认 isnet-anime-q8 首次使用时自动下载 fp32 源模型并本地量化为 int8，约 42MB）"
