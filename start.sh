#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

voice_python=""
for voice_candidate in python3.12 python3.13 python3.14 python3; do
  if command -v "$voice_candidate" >/dev/null 2>&1 && "$voice_candidate" -c 'import sys, ensurepip, venv; sys.exit(0 if sys.version_info >= (3, 12) else 1)' >/dev/null 2>&1; then
    voice_python="$voice_candidate"
    break
  fi
done

if [ -z "$voice_python" ]; then
  echo "Для запуска нужен Python 3.12 или новее."
  echo "Установите его с https://www.python.org/downloads/macos/ и повторите: bash start.sh"
  exit 1
fi

exec "$voice_python" scripts/start_local.py "$@"
