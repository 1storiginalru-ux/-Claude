import time
from django.core.management.base import BaseCommand
from django.db import OperationalError
from desk.db import is_database_busy
from desk.messaging.service import process_one


class Command(BaseCommand):
    help = 'Обработка надёжной исходящей очереди. В dev используется один worker.'

    def add_arguments(self, parser): parser.add_argument('--once', action='store_true')

    def handle(self, *args, **options):
        processed = 0
        try:
            while True:
                try:
                    handled = process_one()
                except OperationalError as error:
                    if not is_database_busy(error): raise
                    # process_one never reclaims Sending automatically. If a
                    # publication happened before this error, preserve that
                    # uncertain state instead of repeating the external send.
                    self.stderr.write('База временно занята. Очередь проверится повторно; начатые отправки не дублируются.')
                    handled = False
                if handled:
                    processed += 1
                elif options['once']: break
                else: time.sleep(1)
                if options['once'] and processed >= 100: break
        except KeyboardInterrupt: pass
        self.stdout.write(f'Обработано исходящих: {processed}')
