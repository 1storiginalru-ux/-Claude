import time
from django.core.management.base import BaseCommand
from desk.integrations.base import IntegrationError
from desk.integrations.sync import sync_account
from desk.models import Account


class Command(BaseCommand):
    help = 'Polling включённых кабинетов. Демо-кабинеты никогда не опрашиваются.'

    def add_arguments(self, parser):
        parser.add_argument('--loop', action='store_true')
        parser.add_argument('--interval', type=int, default=60)

    def handle(self, *args, **options):
        try:
            while True:
                for account in Account.objects.filter(enabled=True, company__is_demo=False).select_related('company'):
                    try: self.stdout.write(f'{account.name}: {sync_account(account)} объектов')
                    except IntegrationError as e: self.stderr.write(f'{account.name}: {e}')
                if not options['loop']: break
                time.sleep(max(30, options['interval']))
        except KeyboardInterrupt: pass
