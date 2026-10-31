"""Explicit, scoped repair of rating-only analyses; no provider or reply work."""
import json
import os
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone
from desk.models import Account, Analysis, Thread
from desk.ai.backfill_period import period_start
from desk.ai.ratings import rating_result, classify_rating
from desk.reviews import effective_analysis


class Command(BaseCommand):
    help = 'Разобрать отзывы без текста по оценке, бесплатно. Без --apply только просмотр.'

    def add_arguments(self, parser):
        parser.add_argument('--account', action='append', required=True)
        parser.add_argument('--months', type=int, default=6)
        parser.add_argument('--apply', action='store_true')

    @transaction.atomic
    def handle(self, *args, **options):
        if not 1 <= options['months'] <= 120: raise CommandError('Период: от 1 до 120 месяцев.')
        accounts = list(Account.objects.filter(name__in=options['account'], company__is_demo=False,
                                               enabled=True, platform__in=['wb', 'ym']).order_by('pk'))
        if {a.name for a in accounts} != set(options['account']):
            raise CommandError('Не все выбранные кабинеты WB/ЯМ найдены или включены.')
        now = timezone.now()
        rows = Thread.objects.filter(account__in=accounts, kind='review', received_at__gte=period_start(options['months'], now),
                                     received_at__lte=now, assessment__isnull=True)
        if options['apply']: rows = rows.select_for_update(of=('self',), no_key=True)
        candidates = []
        for thread in rows.select_related('account__company', 'product', 'analysis', 'assessment').order_by('pk').iterator():
            if rating_result(thread) is None: continue
            previous = effective_analysis(thread)
            if previous and previous.provider == 'rating' and previous.confidence == 1: continue
            candidates.append(thread)
        for account in accounts:
            self.stdout.write(f'{account.name}: отзывов без текста для разбора по оценке: '
                              f'{sum(t.account_id == account.pk for t in candidates)}; период {options["months"]} мес.')
        if not options['apply']:
            self.stdout.write('Только просмотр. Для бесплатного разбора добавьте --apply.'); return
        if not candidates:
            self.stdout.write('Изменения не требуются.'); return
        backup = settings.STATE_DIR / 'backups' / ('rating-analysis-before-' + now.strftime('%Y%m%dT%H%M%S%fZ') + '.json')
        try:
            backup.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            with open(backup, 'x', encoding='utf-8', opener=lambda path, flags: os.open(path, flags, 0o600)) as stream:
                json.dump({'thread_ids': [t.pk for t in candidates],
                           'analyses': list(Analysis.objects.filter(thread__in=candidates).values())},
                          stream, ensure_ascii=False, default=str)
        except OSError as error:
            raise CommandError('Не удалось сохранить резервную копию. Изменений нет.') from error
        for thread in candidates: classify_rating(thread)
        self.stdout.write(f'Разбор завершён. Резервная копия: {backup}')
        self.stdout.write('AI не вызывался. Лимиты, решения сотрудников, черновики и отправки не менялись.')
