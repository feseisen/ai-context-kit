#!/bin/sh
# ai-context-kit: tools/setup.py を、この端末の Python で走らせる。
#   sh setup.sh            実行
#   sh setup.sh --dry-run  何も書かずに、やることだけ見せる
cd "$(dirname "$0")" || exit 1
if command -v python3 >/dev/null 2>&1; then
  PY=python3
elif command -v python >/dev/null 2>&1; then
  PY=python
else
  echo "Python 3 が見つかりません。入れてから、もう一度走らせてください" >&2
  exit 1
fi
exec "$PY" tools/setup.py "$@"
