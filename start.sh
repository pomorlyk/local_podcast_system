#!/usr/bin/env bash
# 启动「听间」本地播客书架（macOS / Linux）
#
#   ./start.sh              # 默认 8765 端口
#   ./start.sh 9000         # 指定端口
#   NO_BROWSER=1 ./start.sh # 不自动打开浏览器
#
# 只用 Python 标准库，不需要 pip install。
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
port="${1:-${PODCAST_PORT:-8765}}"

python="${PODCAST_PYTHON:-}"
if [ -z "$python" ]; then
  for candidate in python3 python; do
    if command -v "$candidate" >/dev/null 2>&1; then python="$candidate"; break; fi
  done
fi
if [ -z "$python" ]; then
  echo "没有找到 Python。请安装 Python 3.10 或更新版本，或设置 PODCAST_PYTHON。" >&2
  exit 1
fi

echo "启动中，稍后会自动打开 http://127.0.0.1:${port}"
if [ -n "${NO_BROWSER:-}" ]; then
  exec "$python" "$root/run.py" --port "$port" --no-browser
fi
exec "$python" "$root/run.py" --port "$port"
