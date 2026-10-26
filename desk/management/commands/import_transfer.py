from pathlib import Path
from django.apps import apps
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.core.management import call_command
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction


class Command(BaseCommand):
    help = 'Перенос экспорта в пустую рабочую базу; очереди и polling должны быть остановлены.'

    def add_arguments(self, parser):
        parser.add_argument('file', type=Path)

    @transaction.atomic
    def handle(self, *args, **options):
        if any(model.objects.exists() for model in [get_user_model(), Group, *apps.get_app_config('desk').get_models()]):
            raise CommandError('База уже содержит рабочие данные или пользователей. Импорт отменён; существующие данные не изменены.')
        if not options['file'].is_file():
            raise CommandError('Файл экспорта не найден.')
        call_command('loaddata', str(options['file']), stdout=self.stdout)
        self.stdout.write('Данные и логины перенесены. Отдельно задайте API-ключи; перед запуском проверьте очередь исходящих ответов.')
