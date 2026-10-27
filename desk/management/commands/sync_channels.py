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
                    from desk.integrations.history import history_turn, sync_history
                    history_accounts = list(Account.objects.filter(enabled=True, history_enabled=True, company__is_demo=False).select_related('company'))
                    review_turns = {a.pk for a in history_accounts if history_turn(a, 'reviews')}
                    sales_turns = {a.pk for a in history_accounts if history_turn(a, 'sales')}
                    # Materialize the result before writes: don't keep a read
                    # cursor open while processing a whole marketplace archive.
                    accounts = list(Account.objects.filter(enabled=True, company__is_demo=False).filter(Q(sync_state__isnull=True) | Q(sync_state__next_attempt__lte=timezone.now())).select_related('company'))
                    for account in accounts:
                        if account.pk in review_turns: continue
                        try: self.stdout.write(f'{account.name}: {sync_account(account)} объектов')
                        except IntegrationError as e: self.stderr.write(f'{account.name}: {e}')
                    from desk.integrations.wb_sales import sync_wb_sales
                    sales_accounts = list(Account.objects.filter(enabled=True, sales_enabled=True, platform='wb', company__is_demo=False).filter(Q(sales_state__isnull=True) | Q(sales_state__next_attempt__lte=timezone.now())).select_related('company'))
                    for account in sales_accounts:
                        if account.pk in sales_turns: continue
                        try: self.stdout.write(f'{account.name}, продажи WB: {sync_wb_sales(account)} событий')
                        except IntegrationError as e: self.stderr.write(f'{account.name}, продажи WB: {e}')
                    from desk.models import Outgoing
                    for account in history_accounts:
                        for kind in ['sales', 'reviews']:
                            if kind == 'reviews' and Outgoing.objects.filter(thread__account=account,status__in=['Queued','Sending','Sent']).exists(): continue
                            try:
                                count = sync_history(account, kind)
                                if count: self.stdout.write(f'{account.name}, история {kind}: {count} записей')
                            except IntegrationError as error:
                                self.stderr.write(f'{account.name}, история {kind}: {error}')
                    from desk.incidents.service import recheck_products
                    recheck_products()
                except OperationalError as error:
                    if not is_database_busy(error): raise
                    self.stderr.write('База временно занята. Синхронизация продолжится в следующем цикле с сохранённой страницы.')
                if not options['loop']: break
                time.sleep(max(30, options['interval']))
        except KeyboardInterrupt: pass
        finally:
            signal.signal(signal.SIGTERM, previous)
