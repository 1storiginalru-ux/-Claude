from django.core.management.base import BaseCommand, CommandError
from desk.models import Thread
from desk.ai.provider import AIProblem
from desk.ai.queue import enqueue


class Command(BaseCommand):
    help = 'Выбранная партия AI-анализа. По умолчанию просмотр, без HTTP и постановки задач.'

    def add_arguments(self, parser):
        scope = parser.add_mutually_exclusive_group(required=True)
        scope.add_argument('--account'); scope.add_argument('--legal-entity')
        parser.add_argument('--product')
        parser.add_argument('--limit', type=int, default=30)
        parser.add_argument('--force', action='store_true')
        parser.add_argument('--apply', action='store_true')

    def handle(self, *args, **o):
        if not 1 <= o['limit'] <= 100: raise CommandError('Размер партии: от 1 до 100.')
        rows = Thread.objects.filter(account__company__is_demo=False, account__enabled=True, kind='review').exclude(account__company__ai_provider='none')
        rows = rows.filter(account__name=o['account']) if o['account'] else rows.filter(account__company__legal_entity=o['legal_entity'])
        if o['product']: rows = rows.filter(product__code=o['product'])
        rows = rows.exclude(assessment__decision__in=['negative', 'non_negative']).select_related('account__company', 'product').order_by('-received_at', '-pk')[:o['limit']]
        count = 0
        for thread in rows:
            self.stdout.write(f'{thread.pk}: {thread.account.name}, ProductID {thread.product.code if thread.product_id else "не сопоставлен"}, {thread.rating or "без оценки"} ★')
            if o['apply']:
                try: job = enqueue(thread, force=o['force'], retry_failed=True)
                except AIProblem as error: raise CommandError(str(error)) from error
                self.stdout.write(f'  AI-задача {job.pk}: {job.get_status_display()}')
            count += 1
        self.stdout.write(f'Выбрано отзывов: {count}.' + ('' if o['apply'] else ' Для постановки в очередь добавьте --apply.'))
