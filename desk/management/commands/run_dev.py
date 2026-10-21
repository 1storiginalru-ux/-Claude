import signal
import subprocess
import sys
import time
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = 'Локальный запуск HTTP, очереди и polling с общим жизненным циклом. Для production нужен process manager.'

    def add_arguments(self, parser): parser.add_argument('--port', type=int, default=8000)

    def handle(self, *args, **options):
        port = options['port']
        if not 1024 <= port <= 65535: raise CommandError('Порт должен быть от 1024 до 65535.')
        prefix = [sys.executable, str(settings.BASE_DIR / 'manage.py')]
        children = []
        def stop(signum, frame): raise KeyboardInterrupt
        previous = signal.signal(signal.SIGTERM, stop)
        try:
            for command in [['runserver', '--insecure', '--noreload', f'127.0.0.1:{port}'], ['worker'], ['sync_channels', '--loop']]:
                children.append(subprocess.Popen(prefix + command, cwd=settings.BASE_DIR))
            self.stdout.write('HTTP, worker и polling запущены. Ctrl+C завершает только эти процессы.')
            while True:
                for process in children:
                    if process.poll() is not None: raise CommandError('Один из процессов завершился; остальные остановлены.')
                time.sleep(1)
        except KeyboardInterrupt: pass
        finally:
            signal.signal(signal.SIGTERM, previous)
            for process in children:
                if process.poll() is None: process.terminate()
            for process in children:
                try: process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill(); process.wait()
