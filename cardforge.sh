#!/usr/bin/env bash
# =====================================================================
#  cardforge.sh — cardforge CLI 入口（Linux/macOS）
# ---------------------------------------------------------------------
#  对应 Windows 版 cardforge.cmd，职责一致：
#    解析 venv（CARDFORGE_VENV > settings.json "venv" > 项目 .venv）
#    → 缺则自动跑 setup.sh → 用该 venv 执行 cardforge.py
#
#  可从任意目录调用。
# =====================================================================
set -uo pipefail

# 解析脚本自身真实路径（兼容被 symlink 引用的情况）
SOURCE="${BASH_SOURCE[0]}"
while [[ -L "$SOURCE" ]]; do
  DIR="$(cd -P "$(dirname "$SOURCE")" && pwd)"
  SOURCE="$(readlink "$SOURCE")"
  [[ "$SOURCE" != /* ]] && SOURCE="$DIR/$SOURCE"
done
ROOT="$(cd -P "$(dirname "$SOURCE")" && pwd)"
cd "$ROOT" || exit 1

# ---------- 1. 解析 venv ----------
VENV_DIR=""

# 优先级 1：环境变量（bot 调用时传入）
if [[ -n "${CARDFORGE_VENV:-}" ]]; then
  VENV_DIR="$CARDFORGE_VENV"
fi

# 优先级 2：settings.json 的 venv 字段
if [[ -z "$VENV_DIR" && -f "$ROOT/settings.json" ]]; then
  for c in python3 python; do
    if command -v "$c" >/dev/null 2>&1; then
      VENV_DIR="$("$c" -c "
import json
try:
    with open(r'$ROOT/settings.json', encoding='utf-8') as f:
        print((json.load(f).get('venv') or '').strip())
except Exception:
    print('')
" 2>/dev/null)"
      break
    fi
  done
fi

# 优先级 3：项目自带 .venv
[[ -z "$VENV_DIR" ]] && VENV_DIR="$ROOT/.venv"

# ---------- 2. 找解释器 ----------
VENV_PY=""
for rel in "bin/python" "Scripts/python.exe" "bin/python3"; do
  [[ -x "$VENV_DIR/$rel" ]] && { VENV_PY="$VENV_DIR/$rel"; break; }
done

# ---------- 3. 缺失则先部署 ----------
if [[ -z "$VENV_PY" ]]; then
  echo "[cardforge] 首次运行，正在执行 setup.sh 部署..."
  if ! bash "$ROOT/setup.sh" --venv "$VENV_DIR"; then
    echo "[cardforge] 部署失败，已中止" >&2
    exit 1
  fi
  for rel in "bin/python" "Scripts/python.exe" "bin/python3"; do
    [[ -x "$VENV_DIR/$rel" ]] && { VENV_PY="$VENV_DIR/$rel"; break; }
  done
fi

if [[ -z "$VENV_PY" ]]; then
  echo "[cardforge] 仍找不到解释器：$VENV_DIR" >&2
  exit 1
fi

# ---------- 4. 执行 ----------
exec "$VENV_PY" "$ROOT/cardforge.py" "$@"
