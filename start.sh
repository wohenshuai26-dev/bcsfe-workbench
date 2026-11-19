#!/usr/bin/env bash
# 貓咪存檔工作台：一鍵安裝並啟動
set -euo pipefail
cd "$(dirname "$0")"

find_python() {
  for c in python3.13 python3.12 python3.11 python3.10 python3; do
    if command -v "$c" >/dev/null 2>&1 && "$c" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)'; then
      echo "$c"; return 0
    fi
  done
  return 1
}

if [ ! -x .venv/bin/python ]; then
  PY="$(find_python)" || { echo "找不到 Python 3.9 以上的版本，請先安裝 Python。" >&2; exit 1; }
  echo "第一次執行：用 $PY 建立虛擬環境並安裝 BCSFE…"
  "$PY" -m venv .venv
  .venv/bin/pip install -q --upgrade pip
  .venv/bin/pip install -q -r requirements.txt
fi

exec .venv/bin/python server.py "$@"
