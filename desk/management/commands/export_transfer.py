from datetime import datetime, timezone
import os
from pathlib import Path
from django.conf import settings
from django.core.management import call_command
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction


class Command(BaseCommand):
    help = 'Экспорт рабочей базы для переноса на сервер. Перед запуском остановите локальное приложение.'

    def add_arguments(self, parser):
        parser.add_argument('--output', type=Path)

    def handle(self, *args, **options):
        output = options['output'] or settings.STATE_DIR / ('voice-transfer-' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ') + '.json')
        try:
            with open(output, 'x', encoding='utf-8', opener=lambda p, flags: os.open(p, flags, 0o600)) as stream:
                try:
                    # With the local processes stopped, keep all tables at one
                    # consistent revision. Database-generated permission IDs may
                    # differ between SQLite and PostgreSQL; use natural keys.
                    with transaction.atomic():
                        call_command('dumpdata', natural_foreign=True, natural_primary=True,
                                     exclude=['contenttypes', 'auth.permission', 'sessions'],
                                     stdout=stream)
                except Exception:
                    output.unlink(missing_ok=True)
                    raise
        except OSError as error:
            raise CommandError(f'Не удалось создать новый файл экспорта: {error}') from error
        self.stdout.write(f'Экспорт готов: {output}')
        self.stdout.write('Файл содержит рабочие данные и хеши паролей. Передавайте его по SSH и не добавляйте в Git.')
