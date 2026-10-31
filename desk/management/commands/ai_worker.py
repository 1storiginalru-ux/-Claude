import signal
import time
from django.core.management.base import BaseCommand
from django.db import OperationalError
from desk.db import is_database_busy
from desk.ai.queue import process_one


class Command(BaseCommand):
    help = 'Отдельная очередь AI: классификация и черновики, без публикации.'

    def add_arguments(self, parser): parser.add_argument('--once', action='store_true')

    def handle(self, *args, **options):
        def stop(signum, frame): raise KeyboardInterrupt
        previous = signal.signal(signal.SIGTERM, stop)
        count = 0
        next_scan = 0
        try:
            while True:
                try:
                    if not options['once'] and time.monotonic() >= next_scan:
                        from desk.ai.backfill import refill
                        refill(); next_scan = time.monotonic() + 30
                    handled = process_one()
                except OperationalError as error:
                    if not is_database_busy(error): raise
                    handled = False
                if handled: count += 1
                elif options['once']: break
                else: time.sleep(2)
                if options['once'] and count >= 30: break
        except KeyboardInterrupt: pass
        finally: signal.signal(signal.SIGTERM, previous)
        self.stdout.write(f'Обработано AI-задач: {count}')
