import sys
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.core.management.base import BaseCommand, CommandError
from desk.models import Company


class Command(BaseCommand):
    help = 'Первый локальный запуск: демо только для пустой БД и создание собственного пользователя.'

    def add_arguments(self, parser):
        parser.add_argument('--no-input', action='store_true')

    def handle(self, *args, **options):
        if not Company.objects.exists():
            call_command('seed_demo', stdout=self.stdout)
        if get_user_model().objects.filter(is_active=True).exists():
            return
        if options['no_input']:
            self.stdout.write('Пользователь ещё не создан. Для входа выполните: .venv/bin/python manage.py createsuperuser')
            return
        if not sys.stdin.isatty():
            raise CommandError('Для первого запуска откройте обычный Terminal и выполните bash start.sh, чтобы задать логин и пароль.')
        self.stdout.write('Создайте свой логин и пароль для входа. При вводе пароля символы не отображаются — это нормально.')
        call_command('createsuperuser', interactive=True)
