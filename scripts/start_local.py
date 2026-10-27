"""Portable local bootstrap; requires only the standard library until the venv exists."""
import argparse
import hashlib
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.request
import venv
import webbrowser


ROOT = Path(__file__).resolve().parent.parent


def run(arguments):
    subprocess.run([str(x) for x in arguments], cwd=ROOT, check=True)


def local_python():
    environment = ROOT / '.venv'
    executable = environment / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python')
    if not environment.exists():
        print('Первый запуск: создаём окружение программы…', flush=True)
        venv.EnvBuilder(with_pip=True).create(environment)
    if not executable.is_file():
        raise RuntimeError('Папка .venv уже существует, но в ней нет Python. Переименуйте её и повторите запуск.')
    compatible = subprocess.run([str(executable), '-c', 'import sys; sys.exit(0 if sys.version_info >= (3, 12) else 1)'], capture_output=True)
    if compatible.returncode:
        raise RuntimeError('В .venv находится несовместимый Python. Переименуйте .venv и повторите запуск с Python 3.12+.')
    return executable


def install_dependencies(executable):
    requirements = ROOT / 'requirements.txt'
    digest = hashlib.sha256(requirements.read_bytes()).hexdigest()
    stamp = ROOT / '.venv' / '.voice-requirements.sha256'
    installed = stamp.exists() and stamp.read_text().strip() == digest
    if installed:
        installed = subprocess.run([str(executable), '-c', 'import django, openpyxl, pypdf, requests'], capture_output=True).returncode == 0
    if not installed:
        print('Устанавливаем необходимые библиотеки. В первый раз это может занять несколько минут…', flush=True)
        run([executable, '-m', 'pip', '--disable-pip-version-check', 'install', '-r', requirements])
        stamp.write_text(digest + '\n')


def open_when_ready(port, process):
    # Local browser requests must never be routed through a configured corporate proxy.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    for _ in range(60):
        if process.poll() is not None:
            return
        try:
            with opener.open(f'http://127.0.0.1:{port}/health/', timeout=1) as response:
                if response.status == 200:
                    webbrowser.open(f'http://127.0.0.1:{port}/login/')
                    return
        except OSError:
            pass
        time.sleep(0.5)


def check_port_available(port):
    try:
        with socket.socket() as probe:
            # Match runserver's socket policy: recently closed HTTP connections
            # may remain in TIME_WAIT after the old listener has stopped.
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            probe.bind(('127.0.0.1', port))
    except OSError as error:
        raise RuntimeError(f'Порт {port} занят. Остановите прежний запуск или используйте bash start.sh --port {port + 1 if port < 65535 else 8000}.') from error


def main():
    parser = argparse.ArgumentParser(description='Запустить Voice на вашем компьютере.')
    parser.add_argument('--port', type=int, default=8000, help='Порт программы (по умолчанию 8000).')
    parser.add_argument('--setup-only', action='store_true', help='Только подготовить зависимости и базу, без запуска.')
    parser.add_argument('--no-input', action='store_true', help='Не запрашивать создание пользователя (для автоматизации).')
    parser.add_argument('--no-browser', action='store_true', help='Не открывать браузер автоматически.')
    options = parser.parse_args()
    if sys.version_info < (3, 12):
        raise RuntimeError('Нужен Python 3.12 или новее.')
    if not 1024 <= options.port <= 65535:
        raise RuntimeError('Выберите порт от 1024 до 65535.')
    if not options.setup_only:
        check_port_available(options.port)
    executable = local_python()
    install_dependencies(executable)
    run([executable, ROOT / 'manage.py', 'migrate', '--noinput'])
    run([executable, ROOT / 'manage.py', 'refresh_rules', '--apply'])
    prepare = [executable, ROOT / 'manage.py', 'prepare_local']
    if options.no_input:
        prepare.append('--no-input')
    run(prepare)
    run([executable, ROOT / 'manage.py', 'check'])
    if options.setup_only:
        print('Библиотеки и база подготовлены. Для запуска выполните: bash start.sh', flush=True)
        return 0
    print(f'Запускаем Voice на порту {options.port}. Для остановки нажмите Ctrl+C.', flush=True)
    process = subprocess.Popen([str(executable), str(ROOT / 'manage.py'), 'run_dev', '--port', str(options.port)], cwd=ROOT, start_new_session=True)
    def stop(signum, frame):
        raise KeyboardInterrupt
    previous = signal.signal(signal.SIGTERM, stop)
    if not options.no_browser:
        threading.Thread(target=open_when_ready, args=(options.port, process), daemon=True).start()
    try:
        return process.wait()
    except KeyboardInterrupt:
        return 0
    finally:
        signal.signal(signal.SIGTERM, previous)
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=20)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()


if __name__ == '__main__':
    try:
        sys.exit(main())
    except (RuntimeError, subprocess.CalledProcessError, OSError) as error:
        print(f'Не удалось запустить Voice: {error}', file=sys.stderr)
        sys.exit(1)
