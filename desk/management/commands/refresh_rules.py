from django.core.management.base import BaseCommand, CommandError
from django.db.models import Q
from desk.ai.classification import RULES_VERSION
from desk.ai.refresh import refresh_rules
from desk.models import Account, Thread


class Command(BaseCommand):
    help = 'Пересчитать прежнюю разметку rules без API. По умолчанию — только проверка; --apply сохраняет с аудитом.'

    def add_arguments(self, parser):
        parser.add_argument('--apply', action='store_true')
        parser.add_argument('--account', help='Кабинет: имя или ID; без параметра — все кабинеты.')

    def handle(self, *args, **options):
        query = Thread.objects.filter(analysis__provider='rules', assessment__isnull=True).exclude(analysis__prompt_version=RULES_VERSION)
        if options['account']:
            value = options['account']
            match = Q(name=value)
            if value.isdecimal(): match |= Q(pk=int(value))
            account = Account.objects.filter(match).first()
            if not account: raise CommandError('Кабинет не найден.')
            query = query.filter(account=account)
        last, processed, cleared = 0, 0, 0
        # Keyset batches keep memory bounded and remain stable as repaired rows leave the query.
        while ids := list(query.filter(pk__gt=last).order_by('pk').values_list('pk', flat=True)[:200]):
            for pk in ids:
                result = refresh_rules(pk, apply=options['apply'])
                if result is None: continue
                before, after = result
                processed += 1
                cleared += bool(before['hidden_negative'] and not after['hidden_negative'])
            last = ids[-1]
            if options['apply'] and processed and processed % 1000 == 0:
                self.stdout.write(f'Обработано: {processed}.')
        self.stdout.write(f'{"Сохранено" if options["apply"] else "Проверено без сохранения"}: {processed}. {"Снято" if options["apply"] else "Будет снято"} меток скрытого негатива: {cleared}.')
        self.stdout.write('Ручные решения, AI-анализ, отзывы, ответы, статусы и расследования не изменялись.')
