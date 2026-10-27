from datetime import datetime, timezone
from pathlib import Path
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from desk.products.imports import ImportProblem
from desk.products.prepare import prepare_mapping


class Command(BaseCommand):
    help = 'Подготовить из широкой таблицы ID/ВБ/ЯМ небольшие XLSX для Product Mapping. База не изменяется.'

    def add_arguments(self, parser):
        parser.add_argument('file', type=Path)
        parser.add_argument('--wb', action='append', default=[], metavar='СТОЛБЕЦ=КАБИНЕТ')
        parser.add_argument('--ozon', action='append', default=[], metavar='СТОЛБЕЦ=КАБИНЕТ')
        parser.add_argument('--ym', action='append', default=[], metavar='СТОЛБЕЦ=КАБИНЕТ')
        parser.add_argument('--legal-entity', required=True)
        parser.add_argument('--output', type=Path)

    def handle(self, *args, **options):
        bindings = []
        for platform, key in [('WB', 'wb'), ('YM', 'ym'), ('OZON', 'ozon')]:
            for value in options[key]:
                column, separator, account = value.partition('=')
                if not separator:
                    raise CommandError('Привязка задаётся как СТОЛБЕЦ=КАБИНЕТ, например ВБ1=ВБ_1.')
                bindings.append((platform, column.strip(), account.strip()))
        output = options['output'] or settings.STATE_DIR / 'mapping-import' / datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
        try:
            with options['file'].open('rb') as upload:
                result = prepare_mapping(upload, bindings, options['legal_entity'], output)
        except (OSError, ImportProblem) as error:
            raise CommandError(str(error)) from error
        for item in result:
            self.stdout.write(f'{item["account"]}: {item["rows"]} строк → {item["path"]}')
        self.stdout.write('Готово. База не изменена. Загрузите файлы в Product Mapping, выбрав компанию соответствующего кабинета.')
