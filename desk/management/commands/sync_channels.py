import time
import signal
from django.core.management.base import BaseCommand
from django.db import OperationalError
from django.db.models import Q
from django.utils import timezone
from desk.db import is_database_busy
from desk.integrations.base import IntegrationError
from desk.integrations.sync import sync_account
from desk.models import Account


class Command(BaseCommand):
    help = 'Polling включённых кабинетов. Демо-кабинеты никогда не опрашиваются.'

    def add_arguments(self, parser):
        parser.add_argument('--loop', action='store_true')
        parser.add_argument('--interval', type=int, default=60)

    def handle(self, *args, **options):
        def stop(signum, frame): raise KeyboardInterrupt
        previous = signal.signal(signal.SIGTERM, stop)
        try:
            while True:
                try:
                    # Materialize the result before writes: don't keep a read
                    # cursor open while processing a whole marketplace archive.
                    accounts = list(Account.objects.filter(enabled=True, company__is_demo=False).filter(Q(sync_state__isnull=True) | Q(sync_state__next_attempt__lte=timezone.now())).select_related('company'))
                    for account in accounts:
                        try: self.stdout.write(f'{account.name}: {sync_account(account)} объектов')
                        except IntegrationError as e: self.stderr.write(f'{account.name}: {e}')
                except OperationalError as error:
                    if not is_database_busy(error): raise
                    self.stderr.write('База временно занята. Синхронизация продолжится в следующем цикле с сохранённой страницы.')
                if not options['loop']: break
                time.sleep(max(30, options['interval']))
        except KeyboardInterrupt: pass
        finally:
            signal.signal(signal.SIGTERM, previous)
