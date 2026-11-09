#!/usr/bin/env bash
# =====================================================================
#  webui.sh — 启动 cardforge Web UI（Linux/macOS）
# ---------------------------------------------------------------------
#  对应 Windows 版 webui.cmd。venv 解析规则同 cardforge.sh。
# =====================================================================
set -uo pipefail

SOURCE="${BASH_SOURCE[0]}"
while [[ -L "$SOURCE" ]]; do
  DIR="$(cd -P "$(dirname "$SOURCE")" && pwd)"
  SOURCE="$(readlink "$SOURCE")"
  [[ "$SOURCE" != /* ]] && SOURCE="$DIR/$SOURCE"
done
ROOT="$(cd -P "$(dirname "$SOURCE")" && pwd)"
cd "$ROOT" || exit 1

VENV_DIR=""
[[ -n "${CARDFORGE_VENV:-}" ]] && VENV_DIR="$CARDFORGE_VENV"

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
[[ -z "$VENV_DIR" ]] && VENV_DIR="$ROOT/.venv"

VENV_PY=""
for rel in "bin/python" "Scripts/python.exe" "bin/python3"; do
  [[ -x "$VENV_DIR/$rel" ]] && { VENV_PY="$VENV_DIR/$rel"; break; }
done

if [[ -z "$VENV_PY" ]]; then
  echo "[cardforge] 首次运行，正在执行 setup.sh 部署..."
  bash "$ROOT/setup.sh" --venv "$VENV_DIR" || exit 1
  for rel in "bin/python" "Scripts/python.exe" "bin/python3"; do
    [[ -x "$VENV_DIR/$rel" ]] && { VENV_PY="$VENV_DIR/$rel"; break; }
  done
fi

[[ -z "$VENV_PY" ]] && { echo "[cardforge] 找不到解释器：$VENV_DIR" >&2; exit 1; }

exec "$VENV_PY" "$ROOT/webapp.py" "$@"
