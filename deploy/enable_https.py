#!/usr/bin/python3
"""Publish the existing loopback service through a dedicated Nginx HTTPS site.

Run as root on the Ubuntu server. Certbot prompts for the contact address and
agreement when registering an account. Existing sites and application data are
not rewritten. A failed activation restores this site's configuration and env.
"""
import argparse
import fcntl
import http.client
import ipaddress
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import signal
import socket
import ssl
import subprocess
import tempfile
import time
from datetime import datetime, timezone

if __package__:
    from .environment import read_environment
else:
    from environment import read_environment

ENV = Path('/etc/voice/voice.env')
SITE = Path('/etc/nginx/sites-available/voice-https.conf')
LINK = Path('/etc/nginx/sites-enabled/voice-https.conf')
WEBROOT = Path('/var/www/voice-acme')
LE = Path('/etc/letsencrypt')
MARKER = '# Managed by Voice HTTPS v1'


def say(message):
    try:
        print(message, flush=True)
    except OSError:
        pass  # A disconnected SSH terminal must not prevent rollback.


def run(*command, capture=False):
    return subprocess.run(command, check=True, text=True, capture_output=capture,
                          umask=0o022 if command[0] == 'certbot' else -1)


def domain_name(value):
    value = value.lower()
    if len(value) > 253 or '.' not in value or not all(
            re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?', label)
            for label in value.split('.')):
        raise argparse.ArgumentTypeError('Укажите обычный DNS-домен без https:// и пути.')
    return value


def atomic_write(path, data, mode):
    fd, name = tempfile.mkstemp(prefix='.voice-https-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            os.fchmod(stream.fileno(), mode)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def environment_bytes(domain):
    values = read_environment(ENV)
    if values.get('DJANGO_SETTINGS_MODULE') != 'config.production':
        raise ValueError('Ожидаются серверные настройки config.production.')
    hosts = [v.strip() for v in values.get('DJANGO_ALLOWED_HOSTS', '').split(',') if v.strip()]
    if '*' in hosts:
        raise ValueError('В DJANGO_ALLOWED_HOSTS должны быть конкретные адреса.')
    origins = [v.strip() for v in values.get('DJANGO_CSRF_TRUSTED_ORIGINS', '').split(',') if v.strip()]
    replacements = {
        'DJANGO_ALLOWED_HOSTS': ','.join(dict.fromkeys([*hosts, 'localhost', '127.0.0.1', domain])),
        'DJANGO_CSRF_TRUSTED_ORIGINS': ','.join(dict.fromkeys([*origins, 'https://' + domain])),
        'VOC_HTTPS': '1',
    }
    # Preserve API keys, database settings and comments literally. Never source
    # this file as shell code and never print its values or their backups.
    lines = [line for line in ENV.read_text().splitlines()
             if line.partition('=')[0].strip() not in replacements]
    lines.extend(key + '=' + json.dumps(value) for key, value in replacements.items())
    return ('\n'.join(lines) + '\n').encode()


def nginx_site(domain, *, tls):
    challenge = '''    location ^~ /.well-known/acme-challenge/ {
        root @WEBROOT@;
        default_type text/plain;
        try_files $uri =404;
    }
'''.replace('@WEBROOT@', str(WEBROOT))
    header = MARKER + '; domain=' + domain + '\n'
    if not tls:
        return (header + '''server {
    listen 80;
    server_name @DOMAIN@;
@CHALLENGE@
    location / { return 503; }
}
'''.replace('@DOMAIN@', domain).replace('@CHALLENGE@', challenge)).encode()
    config = '''map $request_method $voice_https_login_key {
    default "";
    POST $binary_remote_addr;
}
limit_req_zone $voice_https_login_key zone=voice_https_login:10m rate=10r/m;

server {
    listen 80;
    server_name @DOMAIN@;
@CHALLENGE@
    location / { return 301 https://@DOMAIN@$request_uri; }
}

server {
    listen 443 ssl;
    server_name @DOMAIN@;
    ssl_certificate @CERT@/fullchain.pem;
    ssl_certificate_key @CERT@/privkey.pem;
    ssl_protocols TLSv1.2 TLSv1.3;
    ssl_session_cache shared:VoiceHTTPS:10m;
    ssl_session_timeout 1d;
    ssl_session_tickets off;
    client_max_body_size 6m;
    proxy_connect_timeout 10s;
    proxy_read_timeout 180s;
    proxy_set_header Host @DOMAIN@;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_set_header X-Forwarded-For $remote_addr;
    proxy_set_header X-Real-IP $remote_addr;

    location ~ ^/(login|admin/login)/$ {
        limit_req zone=voice_https_login burst=10 nodelay;
        limit_req_status 429;
        proxy_pass http://127.0.0.1:8000;
    }
    location / {
        proxy_pass http://127.0.0.1:8000;
    }
}
'''
    return (header + config.replace('@DOMAIN@', domain).replace('@CHALLENGE@', challenge)
            .replace('@CERT@', str(LE / 'live' / domain))).encode()


def conflicting_site(configuration, domain):
    for block in re.split(r'(?m)^# configuration file ', configuration)[1:]:
        filename, separator, content = block.partition(':\n')
        if not separator or Path(filename).resolve() == SITE.resolve():
            continue
        content = '\n'.join(line.partition('#')[0] for line in content.splitlines())
        for names in re.findall(r'\bserver_name\s+([^;]+);', content):
            if domain in [v.strip('"\'').lower() for v in names.split()]:
                return filename
    return None


def local_request(domain, port, path, *, tls=False, forwarded=False):
    connection = http.client.HTTPConnection('127.0.0.1', port, timeout=5)
    if tls:
        connection.sock = ssl.create_default_context().wrap_socket(
            socket.create_connection(('127.0.0.1', port), timeout=5), server_hostname=domain)
    try:
        headers = {'Host': domain}
        if forwarded:
            headers['X-Forwarded-Proto'] = 'https'
        connection.request('GET', path, headers=headers)
        response = connection.getresponse()
        result = response.status, response.getheaders()
        response.read(1024)
        return result
    finally:
        connection.close()


def wait_for_response(domain, port, path, **options):
    for attempt in range(40):
        try:
            response = local_request(domain, port, path, **options)
            if response[0] == 200:
                return response
        except (OSError, http.client.HTTPException):
            pass
        time.sleep(.25)
    raise ValueError(f'Не прошла проверка {domain}:{port}{path}.')


def wait_for_web(domain):
    wait_for_response(domain, 8000, '/health/', forwarded=True)


def wait_for_redirect(domain):
    # Nginx starts the new TLS listener before old HTTP workers have finished
    # their graceful shutdown. An immediate HTTP probe can still see bootstrap
    # 503 even though HTTPS is already ready. Wait for the new HTTP route too.
    status, locations = None, []
    for attempt in range(40):
        try:
            status, headers = local_request(domain, 80, '/')
            locations = [value for key, value in headers if key.lower() == 'location']
            if status in {301, 308} and locations == ['https://' + domain + '/']:
                return
        except (OSError, http.client.HTTPException):
            status, locations = None, []
        time.sleep(.25)
    observed = repr(locations)[:240]
    raise ValueError(f'Не подтверждён переход HTTP → HTTPS: HTTP {status}, Location={observed}.')


def preflight(domain, server_ip, hook):
    if os.geteuid() != 0:
        raise ValueError('Запустите настройку на сервере от root.')
    for tool in ['nginx', 'certbot', 'systemctl', 'openssl', 'voice-manage']:
        if not shutil.which(tool):
            raise ValueError('Не найден компонент: ' + tool)
    if not ENV.is_file() or ENV.is_symlink():
        raise ValueError('Не найден обычный файл /etc/voice/voice.env.')
    if ENV.stat().st_uid != 0 or ENV.stat().st_mode & 0o077:
        raise ValueError('Настройки должны принадлежать root и иметь права 0600.')
    addresses = {row[4][0] for row in socket.getaddrinfo(domain, 443)}
    if addresses != {str(ipaddress.ip_address(server_ip))}:
        raise ValueError('DNS должен указывать только на выбранный IP сервера. Проверьте A/AAAA-записи.')
    for service in ['voice-web', 'nginx']:
        run('systemctl', 'is-active', '--quiet', service)
    for path in [SITE, hook]:
        prefix = ('#!/bin/sh\n' if path == hook else '') + MARKER + '; domain=' + domain + '\n'
        if path.is_symlink() or path.exists() and not path.read_text().startswith(prefix):
            raise ValueError('Существующий файл не принадлежит этой настройке: ' + str(path))
    if LINK.is_symlink():
        if LINK.resolve() != SITE.resolve():
            raise ValueError('Ссылка Nginx указывает на другую конфигурацию.')
    elif LINK.exists():
        raise ValueError('Вместо ссылки Nginx существует обычный файл: ' + str(LINK))
    configuration = run('nginx', '-T', capture=True).stdout
    conflict = conflicting_site(configuration, domain)
    if conflict:
        raise ValueError('Домен уже настроен в другом файле Nginx: ' + conflict)
    certificate = LE / 'live' / domain / 'fullchain.pem'
    if certificate.exists():
        names = run('openssl', 'x509', '-in', str(certificate), '-noout', '-ext', 'subjectAltName', capture=True).stdout
        if set(re.findall(r'DNS:([^,\s]+)', names)) != {domain}:
            raise ValueError('Существующий сертификат содержит другие домены; его настройки сохранены.')
    environment_bytes(domain)  # Validate without writing anything.
    wait_for_web('localhost')


def activate(domain, server_ip):
    hook = LE / 'renewal-hooks' / 'deploy' / ('voice-' + domain)
    preflight(domain, server_ip, hook)
    backup = ENV.parent / 'backups' / ('https-before-' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ'))
    backup.mkdir(mode=0o700, parents=True)
    originals = {path: path.read_bytes() if path.exists() else None for path in [ENV, SITE, hook]}
    linked = LINK.is_symlink()
    for index, (path, data) in enumerate(originals.items()):
        if data is not None:
            atomic_write(backup / str(index), data, 0o600)
    atomic_write(backup / 'manifest.json', json.dumps({
        'domain': domain, 'files': {str(i): str(p) for i, p in enumerate(originals)},
        'existed': {str(p): data is not None for p, data in originals.items()}, 'site_enabled': linked,
    }, indent=2).encode(), 0o600)
    say('Резервная копия настроек: ' + str(backup))
    committed = False
    env_changed = False
    try:
        challenge = WEBROOT / '.well-known' / 'acme-challenge'
        for directory in [WEBROOT, WEBROOT / '.well-known', challenge]:
            if directory.is_symlink():
                raise ValueError('Каталог ACME не должен быть символической ссылкой.')
            directory.mkdir(mode=0o755, parents=True, exist_ok=True)
            if directory.stat().st_uid != os.geteuid() or directory.stat().st_mode & 0o022:
                raise ValueError('Каталог ACME должен принадлежать root и быть закрыт для чужой записи.')
            directory.chmod(0o755)
        # Keep an already active HTTPS site available on repeated runs.
        if not linked or not (LE / 'live' / domain / 'fullchain.pem').exists():
            atomic_write(SITE, nginx_site(domain, tls=False), 0o644)
        if not linked:
            LINK.symlink_to(SITE)
        run('nginx', '-t')
        run('systemctl', 'reload', 'nginx')
        probe = challenge / ('voice-setup-' + secrets.token_hex(12))
        try:
            atomic_write(probe, b'voice-acme-ready', 0o644)
            wait_for_response(domain, 80, '/.well-known/acme-challenge/' + probe.name)
        finally:
            probe.unlink(missing_ok=True)
        say('Выпускаем сертификат. Certbot может запросить email и согласие с условиями.')
        run('certbot', 'certonly', '--webroot', '--webroot-path', str(WEBROOT),
            '--cert-name', domain, '--domain', domain, '--preferred-challenges', 'http', '--keep-until-expiring')
        if not all((LE / 'live' / domain / name).is_file() for name in ['fullchain.pem', 'privkey.pem']):
            raise ValueError('Certbot не создал ожидаемый сертификат.')
        atomic_write(ENV, environment_bytes(domain), 0o600)
        env_changed = True
        run('voice-manage', 'check')
        run('systemctl', 'restart', 'voice-web')
        wait_for_web(domain)
        atomic_write(SITE, nginx_site(domain, tls=True), 0o644)
        run('nginx', '-t')
        run('systemctl', 'reload', 'nginx')
        for path in ['/health/', '/login/']:
            status, headers = wait_for_response(domain, 443, path, tls=True)
            if status != 200 or not any(k.lower() == 'strict-transport-security' for k, v in headers):
                raise ValueError('Не прошла проверка HTTPS: ' + path)
            if path == '/login/' and not any(k.lower() == 'set-cookie' and v.startswith('csrftoken=')
                                           and re.search(r'(?:^|;)\s*Secure(?:;|$)', v, re.I) for k, v in headers):
                raise ValueError('Не подтверждена защищённая cookie формы входа.')
        wait_for_redirect(domain)
        hook.parent.mkdir(parents=True, exist_ok=True)
        hook_text = ('#!/bin/sh\n' + MARKER + '; domain=' + domain + '\nset -eu\n'
                     'if [ "${RENEWED_LINEAGE:-}" = "' + str(LE / 'live' / domain) + '" ]; then\n'
                     '    /usr/sbin/nginx -t\n    /bin/systemctl reload nginx\nfi\n')
        atomic_write(hook, hook_text.encode(), 0o755)
        run('systemctl', 'enable', '--now', 'certbot.timer')
        committed = True
        say('HTTPS и вход проверены. Адрес: https://' + domain)
        say('Проверяем автоматическое продление сертификата...')
        run('certbot', 'renew', '--cert-name', domain, '--dry-run', '--run-deploy-hooks')
        say('Проверка продления: OK. Входите под своей существующей учётной записью.')
    except (Exception, KeyboardInterrupt):
        if committed:
            say('HTTPS уже работает. Не завершена проверка продления; пришлите текст ошибки.')
        else:
            say('Активация не завершена. Восстанавливаем прежние настройки.')
            for path, data in originals.items():
                if data is None:
                    path.unlink(missing_ok=True)
                else:
                    atomic_write(path, data, 0o600 if path == ENV else 0o755 if path == hook else 0o644)
            if not linked:
                LINK.unlink(missing_ok=True)
            if env_changed:
                run('systemctl', 'restart', 'voice-web')
            run('nginx', '-t')
            run('systemctl', 'reload', 'nginx')
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('domain', type=domain_name)
    parser.add_argument('--server-ip', required=True, type=ipaddress.IPv4Address)
    args = parser.parse_args()
    def interrupted(signum, frame):
        raise InterruptedError('Установка прервана сигналом ' + str(signum) + '.')
    signal.signal(signal.SIGHUP, interrupted)
    signal.signal(signal.SIGTERM, interrupted)
    try:
        if os.geteuid() != 0:
            raise ValueError('Запустите настройку на сервере от root.')
        parent = ENV.parent.stat()
        if parent.st_uid != 0 or parent.st_mode & 0o022:
            raise ValueError('Каталог /etc/voice должен принадлежать root и быть закрыт для чужой записи.')
        descriptor = os.open(ENV.parent / '.https-setup.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        with os.fdopen(descriptor, 'w') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            activate(args.domain, args.server_ip)
    except (OSError, ValueError, subprocess.CalledProcessError) as error:
        if isinstance(error, subprocess.CalledProcessError):
            parser.exit(1, 'Команда не завершена: ' + ' '.join(error.cmd[:3]) + '. Настройка требует проверки.\n')
        parser.exit(1, str(error) + '\n')
    except KeyboardInterrupt:
        parser.exit(130, 'Установка прервана пользователем.\n')


if __name__ == '__main__':
    main()
