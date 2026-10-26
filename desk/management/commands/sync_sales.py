from django.core.management.base import BaseCommand, CommandError
from django.db.models import Q
from desk.integrations.base import IntegrationError
from desk.integrations.wb_sales import sync_wb_sales
from desk.models import Account


class Command(BaseCommand):
    help = 'Одна страница продаж WB с соблюдением пауз. Фоновая загрузка работает в voice-poll.'

    def add_arguments(self, parser):
        parser.add_argument('--account', required=True, help='Имя или ID кабинета WB.')

    def handle(self, *args, **options):
        value = str(options['account'])
        match = Q(name=value)
        if value.isdigit():
            match |= Q(pk=int(value))
        account = Account.objects.filter(match, platform='wb').select_related('company').first()
        if not account:
            raise CommandError('Кабинет WB не найден.')
        try:
            count = sync_wb_sales(account)
        except IntegrationError as error:
            raise CommandError(str(error)) from error
        self.stdout.write(f'Обработано событий: {count}. Завершение периода и счётчики: voice-manage sales_status --account "{account.name}"')
