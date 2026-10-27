from django.core.management.base import BaseCommand, CommandError
from django.db.models import Q
from desk.models import Account
from desk.integrations.base import IntegrationError
from desk.integrations.history import sync_history


class Command(BaseCommand):
    help='Одна страница истории с соблюдением лимитов; постоянная работа встроена в voice-poll.'

    def add_arguments(self, parser):
        parser.add_argument('--account',required=True)
        parser.add_argument('--kind',choices=['sales','reviews'],required=True)

    def handle(self,*args,**options):
        value=str(options['account']);match=Q(name=value)
        if value.isdecimal():match |= Q(pk=int(value))
        account=Account.objects.filter(match).select_related('company').first()
        if not account:raise CommandError('Кабинет не найден.')
        try: count=sync_history(account,options['kind'])
        except IntegrationError as error:raise CommandError(str(error)) from error
        self.stdout.write(f'Обработано: {count}. Проверка: voice-manage history_status --account "{account.name}"')
