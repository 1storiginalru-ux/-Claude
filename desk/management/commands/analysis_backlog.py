"""Read-only progress, or explicit opt-in to bounded paid historical classification."""
import json
import os
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.db.models import Count, Sum
from django.utils import timezone
from desk.models import Account, AIBackfillState, AIJob, AIRequest, Audit, Thread
from desk.ai.client import validate_settings
from desk.ai.provider import AIProblem
from desk.reviews import effective_analysis
from desk.voc.rules import trusted_analysis
from desk.ai.backfill_period import period_start, cancel_outside_period, outside_jobs, OUTSIDE_PERIOD

STOPPED = 'Авторазбор истории отключён сотрудником.'


class Command(BaseCommand):
    help = 'Прогресс разбора сохранённых отзывов; --enable --apply включает платную классификацию в существующем лимите.'

    def add_arguments(self, parser):
        parser.add_argument('--account', action='append', required=True)
        mode = parser.add_mutually_exclusive_group()
        mode.add_argument('--enable', action='store_true'); mode.add_argument('--disable', action='store_true')
        parser.add_argument('--apply', action='store_true')
        parser.add_argument('--months', type=int, help='Сколько последних календарных месяцев разбирать, от 1 до 120; по умолчанию 6.')

    @transaction.atomic
    def handle(self, *args, **options):
        if options['months'] is not None and not 1 <= options['months'] <= 120:
            raise CommandError('Период: от 1 до 120 календарных месяцев.')
        accounts = list(Account.objects.select_for_update(of=('self',), no_key=True).select_related('company')
                        .filter(name__in=options['account'], company__is_demo=False).order_by('pk'))
        if {a.name for a in accounts} != set(options['account']): raise CommandError('Не все реальные кабинеты найдены.')
        now = timezone.now()
        before = list(AIBackfillState.objects.filter(account__in=accounts).values())
        existing = {row['account_id']: row for row in before}
        wanted = {}
        for account in accounts:
            previous = existing.get(account.pk, {})
            enabled = True if options['enable'] else False if options['disable'] else previous.get('enabled', False)
            months = options['months'] if options['months'] is not None else previous.get('months', 6)
            wanted[account.pk] = enabled, months
        if options['enable']:
            for account in accounts:
                if not account.enabled or account.platform not in {'wb', 'ym'}:
                    raise CommandError('Для авторазбора выберите включённые кабинеты WB и Маркета.')
                c = account.company
                try: validate_settings({'provider': c.ai_provider, 'model': c.ai_model, 'folder': c.ai_folder_id, 'key_env': c.ai_key_env})
                except AIProblem as error: raise CommandError(str(error)) from error
        for account in accounts:
            months = wanted[account.pk][1]
            cutoff = period_start(months, now)
            counts = dict(total=0, trusted=0, human_pending=0, uncertain=0, needs_ai=0, rating=0)
            for thread in Thread.objects.filter(account=account, kind='review', received_at__gte=cutoff, received_at__lte=now).select_related('analysis', 'assessment', 'account__company', 'product').iterator():
                counts['total'] += 1
                analysis = effective_analysis(thread)
                if trusted_analysis(analysis):
                    counts['trusted'] += 1
                    if analysis.provider == 'rating': counts['rating'] += 1
                elif getattr(thread, 'assessment', None): counts['human_pending'] += 1
                elif analysis and analysis.provider not in {'rules', 'demo'}: counts['uncertain'] += 1
                else: counts['needs_ai'] += 1
            state = AIBackfillState.objects.filter(account=account).first()
            self.stdout.write(f'{account.name}: выборка за {months} мес., с {cutoff:%d.%m.%Y %H:%M} МСК по текущий момент.')
            self.stdout.write(f'{account.name}: всего {counts["total"]}, разобрано {counts["trusted"]}, '
                f'для AI {counts["needs_ai"]}, низкая уверенность AI {counts["uncertain"]}, отложено сотрудником {counts["human_pending"]}. Авторазбор истории: {bool(state and state.enabled)}.')
            if counts['rating']: self.stdout.write(f'  Из разобранных по оценке без AI: {counts["rating"]}. Причина оценки не определялась.')
            if state: self.stdout.write(f'  {state.status} Следующий обход: {state.next_attempt.isoformat()}')
            jobs = AIJob.objects.filter(thread__account=account, thread__received_at__gte=cutoff, thread__received_at__lte=now)
            self.stdout.write('  Очередь: ' + str(list(jobs.values('status').annotate(count=Count('pk')).order_by('status'))))
        for company in {a.company_id: a.company for a in accounts}.values():
            used = AIRequest.objects.filter(company=company, month=timezone.localdate().replace(day=1)).aggregate(n=Sum('budget_tokens'))['n'] or 0
            self.stdout.write(f'{company.name}: в месячном лимите учтено {used} / {company.ai_token_limit} токенов. Лимит не увеличивается.')
        if not options['enable'] and not options['disable'] and options['months'] is None: return
        if not options['apply']:
            self.stdout.write('Просмотр. Для изменения добавьте --apply. Включение разрешает оплачиваемый разбор сохранённых отзывов без генерации и отправки ответов.'); return
        changed = [a for a in accounts if a.pk not in existing or
                   (existing[a.pk]['enabled'], existing[a.pk]['months']) != wanted[a.pk]]
        cleanup = sum(outside_jobs(AIBackfillState(account=a, months=wanted[a.pk][1]), now).count() for a in accounts)
        if not changed and not cleanup:
            self.stdout.write('Настройки уже совпадают. Продолжение не сброшено.'); return
        backup = settings.STATE_DIR / 'backups' / ('ai-backfill-before-' + timezone.now().strftime('%Y%m%dT%H%M%S%fZ') + '.json')
        try:
            backup.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            with open(backup, 'x', encoding='utf-8', opener=lambda path, flags: os.open(path, flags, 0o600)) as stream:
                json.dump({'accounts': [a.pk for a in accounts], 'before': before, 'settings': wanted}, stream, ensure_ascii=False, default=str)
        except OSError as error: raise CommandError('Не удалось сохранить прежние настройки. Изменений нет.') from error
        for account in changed:
            state, _ = AIBackfillState.objects.get_or_create(account=account)
            desired, months = wanted[account.pk]
            if state.months != months:
                state.before_pk = 0; state.before_received_at = None
            state.months = months
            state.enabled = desired; state.next_attempt = timezone.now()
            state.status = f'Ожидает фонового разбора за {months} мес.' if desired else STOPPED
            state.save()
            Audit.objects.create(company=account.company, action='ai.backfill.configure', object_id=str(state.pk),
                before={key: existing.get(account.pk, {}).get(key) for key in ['enabled', 'months']},
                after={'enabled': desired, 'months': months, 'backup': str(backup)})
        for account in accounts:
            desired, months = wanted[account.pk]
            state = AIBackfillState.objects.get(account=account)
            background = AIJob.objects.filter(thread__account=account, snapshot__backfill=True)
            if desired:
                cancelled = cancel_outside_period(state, now)
                background.filter(status='cancelled', error__in=[STOPPED, OUTSIDE_PERIOD],
                    thread__received_at__gte=period_start(months, now), thread__received_at__lte=now
                ).update(status='queued', error='', next_attempt=now)
            else:
                cancelled = background.filter(status__in=['queued', 'running', 'retry', 'blocked']).update(
                    status='cancelled', error=STOPPED, lease_token=None, lease_until=None)
            if cancelled:
                Audit.objects.create(company=account.company, action='ai.backfill.range', object_id=str(state.pk),
                                     after={'months': months, 'cancelled': cancelled, 'backup': str(backup)})
            self.stdout.write(f'{account.name}: авторазбор {"ВКЛ" if desired else "ВЫКЛ"}, период {months} мес.; отменено фоновых задач: {cancelled}.')
        self.stdout.write(f'Настройки авторазбора сохранены. Резервная копия: {backup}')
        self.stdout.write('Профили, лимиты, черновики и решения сотрудников сохранены. Обработку выполняет voice-ai.')
