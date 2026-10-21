#!/usr/bin/env bash
set -euo pipefail
cd /workspace/-Claude
python3 -c 'import sys; assert sys.version_info >= (3, 12), "Python 3.12+ is required"'
if [ ! -x /workspace/.venvs/customer-voice/bin/python ]; then
  python3 -m venv /workspace/.venvs/customer-voice
fi
/workspace/.venvs/customer-voice/bin/python -m pip install --cache-dir /workspace/.cache/pip -r requirements.txt
/workspace/.venvs/customer-voice/bin/python manage.py migrate --noinput
/workspace/.venvs/customer-voice/bin/python manage.py check
