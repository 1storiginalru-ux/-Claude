import sqlite3
from datetime import datetime, timezone
from django.conf import settings
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = 'Согласованная резервная копия локальной SQLite через SQLite Backup API (без остановки сервиса).'

    def handle(self, *args, **options):
        directory = settings.STATE_DIR / 'backups'
        directory.mkdir(mode=0o700, exist_ok=True)
        output = directory / ('voice-' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ') + '.sqlite3')
        output.touch(mode=0o600, exist_ok=False)
        with sqlite3.connect(settings.DATABASES['default']['NAME']) as source, sqlite3.connect(output) as destination:
            source.backup(destination)
        self.stdout.write(f'Резервная копия: {output}')
