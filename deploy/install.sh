#!/usr/bin/env bash
# Run on the dedicated Ubuntu server, after cloning into /opt/voice.
set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then
  echo 'Запустите установщик через sudo.' >&2
  exit 1
fi
cd /opt/voice
if [ ! -f requirements-server.txt ] || [ ! -f deploy/voice-web.service ]; then
  echo 'Сначала загрузите репозиторий в /opt/voice.' >&2
  exit 1
fi
for tool in python3 runuser systemctl pg_isready git ss; do
  if ! command -v "$tool" >/dev/null; then
    echo "Не найден компонент: $tool. Установите системные зависимости из docs/SERVER.md." >&2
    exit 1
  fi
done
if [ -n "$(ss -H -ltn 'sport = :8000')" ]; then
  echo 'Порт 8000 уже занят. Остановите прежний процесс перед установкой Voice.' >&2
  exit 1
fi
for service in voice-web voice-worker voice-poll; do
  if systemctl is-active --quiet "$service"; then
    echo 'Сначала остановите службы: systemctl stop voice-web voice-worker voice-poll' >&2
    exit 1
  fi
done

bash deploy/create-service-user.sh
install -d -o voice -g voice-app -m 0700 /var/lib/voice
install -d -o root -g root -m 0700 /etc/voice
python3 - <<'PY'
from pathlib import Path
import os
import secrets
path = Path('/etc/voice/voice.env')
if not path.exists():
    lines = [
        'DJANGO_SETTINGS_MODULE=config.production',
        'DJANGO_SECRET_KEY=' + secrets.token_urlsafe(64),
        'VOC_STATE_DIR=/var/lib/voice',
        'DJANGO_ALLOWED_HOSTS=localhost,127.0.0.1',
        'VOC_HTTPS=0',
        'PGDATABASE=voice', 'PGUSER=voice', 'PGHOST=/var/run/postgresql', 'PGPORT=5432',
    ]
    with open(path, 'x', opener=lambda p, flags: os.open(p, flags, 0o600)) as stream:
        stream.write('\n'.join(lines) + '\n')
    print('Созданы настройки /etc/voice/voice.env. Секреты не выводятся.')
else:
    print('Существующие настройки сохранены.')
os.chmod(path, 0o600)
from deploy.environment import read_environment
configuration = read_environment(path)
if any(configuration.get(key) != value for key, value in {
    'DJANGO_SETTINGS_MODULE': 'config.production', 'VOC_STATE_DIR': '/var/lib/voice',
    'PGDATABASE': 'voice', 'PGUSER': 'voice', 'PGHOST': '/var/run/postgresql', 'PGPORT': '5432',
}.items()):
    raise SystemExit('Установщик рассчитан на локальную PostgreSQL и стандартные пути. Существующий файл не изменён; проверьте настройки.')
PY

systemctl start postgresql
runuser -u postgres -- pg_isready -q
if [ "$(runuser -u postgres -- psql -X -tAc "SELECT count(*) FROM pg_roles WHERE rolname='voice'")" = '0' ]; then
  runuser -u postgres -- createuser --no-superuser --no-createdb --no-createrole voice
fi
if [ "$(runuser -u postgres -- psql -X -tAc "SELECT rolsuper OR rolcreatedb OR rolcreaterole FROM pg_roles WHERE rolname='voice'")" != 'f' ]; then
  echo 'У роли voice обнаружены лишние права PostgreSQL; проверьте её перед продолжением.' >&2
  exit 1
fi
if [ "$(runuser -u postgres -- psql -X -tAc "SELECT count(*) FROM pg_database WHERE datname='voice'")" = '0' ]; then
  runuser -u postgres -- createdb --owner=voice voice
fi
if [ "$(runuser -u postgres -- psql -X -tAc "SELECT pg_get_userbyid(datdba) FROM pg_database WHERE datname='voice'")" != 'voice' ]; then
  echo 'База voice уже существует с другим владельцем. Проверьте её; установщик данные не удаляет.' >&2
  exit 1
fi

python3 -m venv /opt/voice/.venv
/opt/voice/.venv/bin/python -m pip --disable-pip-version-check install -r requirements-server.txt
install -o root -g root -m 0755 deploy/voice-manage /usr/local/sbin/voice-manage
install -o root -g root -m 0755 deploy/voice-set-key /usr/local/sbin/voice-set-key
voice-manage migrate --noinput
voice-manage refresh_rules --apply
voice-manage collectstatic --noinput
voice-manage check

for unit in voice-web.service voice-worker.service voice-poll.service voice-backup.service voice-backup.timer; do
  install -o root -g root -m 0644 "deploy/$unit" "/etc/systemd/system/$unit"
done
systemctl daemon-reload
systemctl enable --now voice-web.service voice-backup.timer
python3 - <<'PY'
import time
import urllib.request
from deploy.environment import read_environment
configuration = read_environment('/etc/voice/voice.env')
headers = {'X-Forwarded-Proto': 'https'} if configuration.get('VOC_HTTPS') == '1' else {}
opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
for attempt in range(40):
    try:
        request = urllib.request.Request('http://127.0.0.1:8000/health/', headers=headers)
        with opener.open(request, timeout=2) as response:
            if response.status == 200:
                print('Проверка HTTP и базы: OK.')
                break
    except OSError:
        pass
    time.sleep(0.5)
else:
    raise SystemExit('Веб-служба не прошла проверку. Выполните journalctl -u voice-web -n 40 --no-pager.')
PY
echo 'Voice запущен только на 127.0.0.1:8000. Проверка — через SSH-туннель.'
echo 'Демо и пользователь не создавались. Polling и очередь пока не запущены.'
echo 'Следующий шаг: перенос рабочей базы с Mac или создание собственного пользователя.'
