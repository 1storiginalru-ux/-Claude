import sqlite3
import os
import subprocess
from datetime import datetime, timezone
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = 'Согласованная резервная копия SQLite или PostgreSQL (custom pg_dump), без остановки сервиса.'

    def handle(self, *args, **options):
        directory = settings.STATE_DIR / 'backups'
        directory.mkdir(mode=0o700, exist_ok=True)
        database = settings.DATABASES['default']
        engine = database['ENGINE']
        if engine not in {'django.db.backends.sqlite3', 'django.db.backends.postgresql'}:
            raise CommandError('Резервное копирование этой базы не поддерживается.')
        extension = '.sqlite3' if engine.endswith('sqlite3') else '.dump'
        output = directory / ('voice-' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ') + extension)
        output.touch(mode=0o600, exist_ok=False)
        try:
            if engine.endswith('sqlite3'):
                with sqlite3.connect(database['NAME']) as source, sqlite3.connect(output) as destination:
                    source.backup(destination)
            else:
                environment = os.environ.copy()
                environment.update(PGDATABASE=str(database['NAME']), PGUSER=database.get('USER', ''),
                                   PGHOST=database.get('HOST', ''), PGPORT=str(database.get('PORT') or '5432'),
                                   PGPASSWORD=database.get('PASSWORD', ''), PGCONNECT_TIMEOUT='5')
                subprocess.run(['pg_dump', '--no-password', '--format=custom', '--file', str(output)],
                               env=environment, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        except (OSError, sqlite3.Error, subprocess.CalledProcessError) as error:
            output.unlink(missing_ok=True)
            raise CommandError('Резервная копия не создана. Проверьте доступ к базе, место на диске и наличие pg_dump для версии сервера.') from error
        self.stdout.write(f'Резервная копия: {output}')
