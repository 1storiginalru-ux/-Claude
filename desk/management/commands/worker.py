import time
from django.core.management.base import BaseCommand
from desk.messaging.service import process_one


class Command(BaseCommand):
    help = 'Обработка надёжной исходящей очереди. В dev используется один worker.'

    def add_arguments(self, parser): parser.add_argument('--once', action='store_true')

    def handle(self, *args, **options):
        processed = 0
        try:
            while True:
                if process_one():
                    processed += 1
                elif options['once']: break
                else: time.sleep(1)
                if options['once'] and processed >= 100: break
        except KeyboardInterrupt: pass
        self.stdout.write(f'Обработано исходящих: {processed}')
