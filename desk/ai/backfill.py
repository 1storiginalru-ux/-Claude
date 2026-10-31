"""Bounded, resumable scans of saved reviews. No generation or inline provider calls."""
from datetime import timedelta
from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from desk.models import AIBackfillState, AIJob, AIRequest, Company, Thread
from desk.reviews import effective_analysis
from desk.voc.rules import trusted_analysis
from .provider import AIProblem
from .queue import enqueue

SCAN_SIZE = 200
QUEUE_LIMIT = 20


def refill():
    now = timezone.now()
    ids = list(AIBackfillState.objects.filter(enabled=True, next_attempt__lte=now,
        account__enabled=True, account__company__is_demo=False).exclude(account__company__ai_provider='none')
        .order_by('next_attempt', 'pk').values_list('pk', 'account__company_id'))
    queued = 0
    for pk, company_id in ids:
        with transaction.atomic():
            Company.objects.select_for_update(no_key=True).get(pk=company_id)
            state = AIBackfillState.objects.select_for_update(of=('self',), no_key=True).select_related('account__company').get(pk=pk)
            if not state.enabled or state.next_attempt > now: continue
            company = state.account.company
            failure = AIJob.objects.filter(thread__account__company=company, snapshot__backfill=True,
                snapshot__provider=company.ai_provider, snapshot__model=company.ai_model,
                snapshot__key_env=company.ai_key_env, status='failed').filter(
                    Q(error__contains='HTTP 401') | Q(error__contains='HTTP 403')).order_by('-updated_at').first()
            if failure and not AIRequest.objects.filter(company=company, state='confirmed',
                    provider=company.ai_provider, model=company.ai_model, created_at__gt=failure.updated_at).exists():
                state.status = 'Нет доступа к AI. Новые партии истории приостановлены до исправления ключа и успешного запроса.'
                state.next_attempt = now + timedelta(minutes=30); state.save(); continue
            pending = AIJob.objects.filter(Q(thread__account__company=company) | Q(test_case__account__company=company),
                status__in=['queued', 'retry', 'blocked', 'running']).count()
            slots = max(0, QUEUE_LIMIT - pending)
            if not slots:
                state.next_attempt = now + timedelta(seconds=60)
                state.status = 'Ожидает задач компании или месячного лимита; новая платная партия не добавляется.'
                state.save(); continue
            rows = Thread.objects.filter(account=state.account, kind='review')
            if state.before_pk: rows = rows.filter(pk__lt=state.before_pk)
            rows = list(rows.select_related('account__company', 'product', 'analysis', 'assessment').order_by('-pk')[:SCAN_SIZE])
            added, scanned = 0, 0
            for thread in rows:
                scanned += 1
                # Human decisions (also deferred/stale ones) are never overruled.
                if not getattr(thread, 'assessment', None) and not trusted_analysis(effective_analysis(thread)):
                    try:
                        job = enqueue(thread, classification_only=True, backfill=True)
                    except AIProblem as error:
                        state.status = str(error)[:240]; state.next_attempt = now + timedelta(minutes=30)
                        break
                    # Completed low-confidence and terminal failed jobs are not
                    # repeatedly billed. Their exceptions remain visible.
                    if job.status in {'queued', 'retry', 'blocked', 'running'}:
                        slots -= 1; added += 1
                state.before_pk = thread.pk
                if not slots: break
            else:
                if len(rows) < SCAN_SIZE:
                    state.before_pk = 0
                    state.next_attempt = now + timedelta(minutes=30)
                    state.status = 'Обход сохранённых отзывов завершён. Повторная проверка изменений — через 30 минут.'
                    state.save(); queued += added; continue
            if state.next_attempt <= now:
                state.next_attempt = now + timedelta(seconds=60)
                state.status = f'Просмотрено в партии: {scanned}; ожидают классификации: {added}. Продолжение сохранено.'
            state.save(); queued += added
    return queued
