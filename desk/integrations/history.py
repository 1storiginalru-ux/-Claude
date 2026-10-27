"""Bounded, resumable historical lanes. All calls use the existing API budgets."""
import hashlib
import math
import uuid
from datetime import timedelta
from django.db import transaction
from django.db.models import Q, Min
from django.utils import timezone
from desk.models import (Account, Analysis, Audit, Company, HistorySyncState, Outgoing, Product,
                         ProductMapping, ReviewAssessment, SalesSyncState, SyncState, Thread)
from desk.ai.classification import RULES_VERSION, local_classify, review_sections
from .base import IntegrationError, get_adapter
from .coverage import record_coverage
from .sync import ingest
from .wb_sales import WBSales, sales_page, source_time, store_sales

STEPS = (7, 14, 30, 60, 90)
MAX_DAYS = 90
TARGET = 100
PAGE_SIZE = 1000
MAX_SKIP = 200000


def eligible(account, kind):
    return (account.enabled and not account.company.is_demo and account.history_enabled
            and (kind == 'reviews' or (account.platform == 'wb' and account.sales_enabled))
            and ProductMapping.objects.filter(account=account).exists())


def planning_dataset(account, now):
    from desk.voc.dataset import ControlDataset
    codes = ProductMapping.objects.filter(account=account).values('product__code')
    ids = Product.objects.filter(code__in=codes, company__is_demo=False).values('code').annotate(pk_first=Min('pk')).values_list('pk_first', flat=True)
    products = list(Product.objects.filter(pk__in=ids))
    return products, ControlDataset(products, Company.objects.filter(is_demo=False), now=now, include_reviews=False)


def history_need(account, kind, state, now):
    products, data = planning_dataset(account, now)
    periods = [data.period(p.code) for p in products]
    recent = SalesSyncState.objects.filter(account=account).first() if kind == 'sales' else SyncState.objects.filter(account=account).first()
    if kind == 'sales':
        # Complete the first current report before widening the shared statistics stream.
        if not recent or not recent.coverage_until:
            return None, 'Сначала ожидаем завершения текущей загрузки продаж.'
        needs = any(p['redemptions'] < TARGET or not p['start_at'] or not recent.coverage_since or recent.coverage_since > p['start_at'] for p in periods)
        if not needs: return None, 'Последние 100 выкупов доступны по всем сопоставленным товарам.'
        if state.days >= MAX_DAYS:
            # A genuine outage can leave a gap between saved history and current polling.
            if recent.coverage_since and state.coverage_until and recent.coverage_since > state.coverage_until:
                state.days = 7
            else:
                return None, 'Доступная история до 90 дней загружена. Для части товаров ещё нет 100 выкупов.'
        days = next(n for n in STEPS if n > state.days)
        return (now-timedelta(days=days), now, days), ''
    if not recent or not recent.coverage_until:
        return None, 'Сначала ожидаем завершения текущего опроса отзывов.'
    starts = [p['start_at'] for p in periods if p['start_at']]
    # If sales are still being filled, collect feedback as far back as their completed history.
    if any(p['redemptions'] < TARGET for p in periods):
        starts.extend(s.coverage_since for s in data.sales_states.values() if s.coverage_since)
    if not starts:
        if account.platform != 'wb' or not account.sales_enabled:
            return None, 'Нет периода продаж для исторической загрузки отзывов.'
        starts = [now-timedelta(days=7)]
    since = max(now-timedelta(days=MAX_DAYS), min(starts + [now-timedelta(days=7)]))
    verified=data.feedback_states.get(account.pk)
    if verified and verified.coverage_since and verified.coverage_until and verified.coverage_since<=since and verified.coverage_until>=now-timedelta(minutes=30):
        return None, 'Отзывы за период выкупов загружены.'
    return (since, now, min(MAX_DAYS, max(7, math.ceil((now-since).total_seconds()/86400)))), ''


def history_turn(account, kind, now=None):
    """Reserve an occasional shared-budget slot; outgoing responses always take priority."""
    now = now or timezone.now()
    state = HistorySyncState.objects.filter(account=account, kind=kind, next_attempt__lte=now).first()
    if not state or not eligible(account, kind): return False
    if state.last_request_at and state.last_request_at > now-timedelta(minutes=5): return False
    if kind == 'reviews' and Outgoing.objects.filter(thread__account=account, status__in=['Queued','Sending','Sent']).exists(): return False
    model = SalesSyncState if kind == 'sales' else SyncState
    current = model.objects.filter(account=account).first()
    return bool(current and current.coverage_until)


def merge_current_coverage(account, kind, since, until):
    model = SalesSyncState if kind == 'sales' else SyncState
    with transaction.atomic():
        state, _ = model.objects.get_or_create(account=account)
        state = model.objects.select_for_update().get(pk=state.pk)
        record_coverage(state, since, until)


def finish(account, state, cursor, until):
    since = max(source_time(cursor['since']), timezone.now()-timedelta(days=MAX_DAYS))
    with transaction.atomic():
        Account.objects.select_for_update().get(pk=account.pk)
        if not HistorySyncState.objects.select_for_update().filter(pk=state.pk,lease_token=state.lease_token).exists():
            raise IntegrationError('Историческая загрузка передана другому процессу.')
        record_coverage(state, since, until)
        if state.kind == 'sales': merge_current_coverage(account, state.kind, since, until)
        days = cursor['days']
        HistorySyncState.objects.filter(pk=state.pk, lease_token=state.lease_token).update(
            cursor={}, days=days, exhausted=state.kind=='sales' and days>=MAX_DAYS,
            next_attempt=timezone.now()+timedelta(seconds=60 if state.kind=='sales' else 1800),
            status=f'История {"продаж" if state.kind=="sales" else "отзывов"}: период {days} дней завершён.')
        if state.kind == 'sales':
            # Only shorten a normal completed-cycle wait, never an API retry deadline.
            HistorySyncState.objects.filter(account=account,kind='reviews',cursor={},status__startswith='История отзывов: период').update(next_attempt=timezone.now())


def historical_analysis(thread, changed):
    if ReviewAssessment.objects.filter(thread=thread).exists(): return
    old = Analysis.objects.filter(thread=thread).first()
    if old and not changed: return
    if old and old.provider not in {'rules','demo'}:
        # Preserve model output, but do not count a verdict for an edited review as verified.
        before={'confidence':old.confidence,'summary':old.summary}
        Analysis.objects.filter(pk=old.pk).update(confidence=0, summary=old.summary[:1800]+' Текст изменился; требуется повторная проверка.')
        Audit.objects.create(company=thread.account.company,action='analysis.history_stale',object_id=str(thread.pk),before=before,after={'confidence':0})
        return
    data=local_classify(thread.text, thread.rating, sections=review_sections(thread), kind=thread.kind)
    before={key:getattr(old,key) for key in data} if old else {}
    Analysis.objects.update_or_create(thread=thread, defaults={**data,'provider':'rules','model':'','prompt_version':RULES_VERSION})
    Audit.objects.create(company=thread.account.company,action='analysis.history',object_id=str(thread.pk),before=before,after={**data,'prompt_version':RULES_VERSION})


def save_reviews(account, rows, cursor, adapter, state):
    since, until=max(source_time(cursor['since']),timezone.now()-timedelta(days=MAX_DAYS)), source_time(cursor['until'])
    candidates=[]
    for row in rows:
        if not isinstance(row,dict): raise IntegrationError('Некорректная строка исторического отзыва.')
        date_key={'wb':'createdDate','ym':'createdAt','ozon':'published_at'}[account.platform]
        created=source_time(row.get(date_key))
        if not created or created>timezone.now(): raise IntegrationError('Нет достоверной даты исторического отзыва.')
        incoming=adapter.parse(row,'review')
        if not incoming.external_id or incoming.external_id=='None' or not incoming.sku:
            raise IntegrationError('Нет достоверного ID или артикула исторического отзыва.')
        if since<=created<=until: candidates.append(incoming)
    with transaction.atomic():
        # Human decisions lock the physical product before their review/account.
        # Use the same order before a batch can insert FK references or edit reviews.
        codes=ProductMapping.objects.filter(account=account,sku__in=[i.sku for i in candidates]).values('product__code')
        list(Product.objects.select_for_update().filter(code__in=codes).order_by('pk').values_list('pk',flat=True))
        Account.objects.select_for_update().get(pk=account.pk)
        if not HistorySyncState.objects.select_for_update().filter(pk=state.pk,lease_token=state.lease_token).exists():
            raise IntegrationError('Историческая загрузка передана другому процессу.')
        for incoming in candidates:
            existing=Thread.objects.filter(account=account,kind='review',external_id=incoming.external_id).first()
            if existing and existing.source_synced_at and existing.source_synced_at>until:
                continue  # A fresh poll has already stored a newer version.
            thread,changed=ingest(account,incoming,historical=True)
            historical_analysis(thread,changed)
    return len(candidates)


def reviews_page(account, state, cursor):
    adapter=get_adapter(account)
    since,until=source_time(cursor['since']),source_time(cursor['until'])
    if account.platform=='ozon':
        rows,continuation=adapter.page('review',since,until,cursor)
        complete=not continuation
        cursor.pop('last_id',None)
        cursor.update(continuation)
    elif account.platform=='ym':
        body={'dateTimeFrom':(since-timedelta(microseconds=1)).isoformat(),'dateTimeTo':(until+timedelta(microseconds=1)).isoformat()}
        params={'limit':50}
        if cursor.get('page_token'): params['page_token']=cursor['page_token']
        payload=adapter.request('POST',adapter.path('goods-feedback'),json=body,params=params)
        result=payload.get('result') if isinstance(payload,dict) else None
        rows=result.get('feedbacks') if isinstance(result,dict) else None
        paging=result.get('paging') if isinstance(result,dict) else None
        if paging is not None and not isinstance(paging,dict):
            raise IntegrationError('Неожиданный формат страниц исторических отзывов Маркета.')
        token=(paging or {}).get('nextPageToken')
        if token is not None and not isinstance(token,str):
            raise IntegrationError('Некорректный токен страницы истории Маркета.')
        if token and token==cursor.get('page_token'): raise IntegrationError('API повторил токен страницы истории.')
        complete=not token
        if token: cursor['page_token']=token
    else:
        stream,skip=cursor.get('stream',0),cursor.get('skip',0)
        if stream not in {0,1,2} or not isinstance(skip,int) or not 0<=skip<MAX_SKIP:
            raise IntegrationError('Достигнут предел страниц архива WB; полнота не подтверждена.')
        params={'take':PAGE_SIZE,'skip':skip,'order':'dateDesc'}
        if stream<2:
            params.update(isAnswered='false' if stream==0 else 'true', dateFrom=math.floor(since.timestamp()),dateTo=math.ceil(until.timestamp()))
        path='/api/v1/feedbacks' if stream<2 else '/api/v1/feedbacks/archive'
        payload=adapter.request('GET',path,params=params)
        body=payload.get('data') if isinstance(payload,dict) else None
        rows=body.get('feedbacks') if isinstance(body,dict) else None
        if not isinstance(rows,list) or len(rows)>PAGE_SIZE: raise IntegrationError('Неожиданный формат истории отзывов WB.')
        dates=[source_time(row.get('createdDate')) if isinstance(row,dict) else None for row in rows]
        if any(d is None for d in dates): raise IntegrationError('В архиве WB отсутствуют даты; полнота не подтверждена.')
        if stream==2 and dates!=sorted(dates,reverse=True): raise IntegrationError('Нарушен порядок дат архива WB; полнота не подтверждена.')
        signature=hashlib.sha256(str([row.get('id') for row in rows]).encode()).hexdigest()
        if rows and cursor.get('signature')==signature: raise IntegrationError('API повторил страницу архива WB.')
        done=len(rows)<PAGE_SIZE or (stream==2 and dates and dates[-1]<=since)
        if done:
            cursor.update(stream=stream+1,skip=0);cursor.pop('signature',None)
        else:
            cursor.update(skip=skip+PAGE_SIZE,signature=signature)
        complete=cursor['stream']==3
    if not isinstance(rows,list): raise IntegrationError('Неожиданный формат исторических отзывов.')
    count=save_reviews(account,rows,cursor,adapter,state)
    if complete: finish(account,state,cursor,until)
    else:
        HistorySyncState.objects.filter(pk=state.pk,lease_token=state.lease_token).update(cursor=cursor,next_attempt=timezone.now()+timedelta(seconds=61),status=f'История отзывов {cursor["days"]} дней: загружено ещё {count}; обход продолжается.')
    return count


def sales_history_page(account,state,cursor):
    since=source_time(cursor['since'])
    page_from=source_time(cursor.get('page_from',cursor['since']))
    rows=WBSales(account).request('GET','/api/v1/supplier/sales',params={'dateFrom':cursor.get('page_from',cursor['since']),'flag':0})
    if not isinstance(rows,list) or len(rows)>80000: raise IntegrationError('Неожиданный формат истории продаж WB.')
    if not rows:
        finish(account,state,cursor,timezone.now())
        return 0
    observed=timezone.now()
    events=sales_page(rows,max(since,observed-timedelta(days=MAX_DAYS)),observed,page_from)
    with transaction.atomic():
        Account.objects.select_for_update().get(pk=account.pk)
        if not HistorySyncState.objects.select_for_update().filter(pk=state.pk,lease_token=state.lease_token).exists():
            raise IntegrationError('Историческая загрузка передана другому процессу.')
        count=store_sales(account,events)
        cursor.update(page_from=rows[-1]['lastChangeDate'])
        HistorySyncState.objects.filter(pk=state.pk,lease_token=state.lease_token).update(cursor=cursor,next_attempt=observed+timedelta(seconds=61),status=f'История продаж {cursor["days"]} дней: загружено ещё {count}; обход продолжается.')
    return count


def sync_history(account,kind):
    if kind not in {'sales','reviews'} or not eligible(account,kind): return 0
    state,_=HistorySyncState.objects.get_or_create(account=account,kind=kind)
    now=timezone.now()
    if state.next_attempt>now: return 0
    lease=uuid.uuid4()
    if not HistorySyncState.objects.filter(pk=state.pk).filter(Q(lease_until__isnull=True)|Q(lease_until__lte=now)).update(lease_token=lease,lease_until=now+timedelta(minutes=10)): return 0
    state.refresh_from_db()
    try:
        cursor=state.cursor if isinstance(state.cursor,dict) else {}
        since,until=source_time(cursor.get('since')),source_time(cursor.get('until'))
        days=cursor.get('days')
        if (not since or not until or not isinstance(days,int) or not 7<=days<=MAX_DAYS
                or not 0<(until-since).total_seconds()<=MAX_DAYS*86400+1
                or until>now or since<now-timedelta(days=MAX_DAYS+1)):
            plan,message=history_need(account,kind,state,now)
            if plan is None:
                HistorySyncState.objects.filter(pk=state.pk,lease_token=lease).update(cursor={},next_attempt=now+timedelta(minutes=30),status=message,exhausted=kind=='sales' and state.days>=MAX_DAYS)
                return 0
            since,until,days=plan
            cursor={'since':since.isoformat(),'until':until.isoformat(),'days':days}
            HistorySyncState.objects.filter(pk=state.pk,lease_token=lease).update(cursor=cursor)
        try:
            count=sales_history_page(account,state,cursor) if kind=='sales' else reviews_page(account,state,cursor)
        except IntegrationError as error:
            if error.request_sent: HistorySyncState.objects.filter(pk=state.pk,lease_token=lease).update(last_request_at=timezone.now())
            raise
        HistorySyncState.objects.filter(pk=state.pk,lease_token=lease).update(last_request_at=timezone.now())
        return count
    except (IntegrationError,ValueError,TypeError,KeyError) as error:
        delay=error.retry_after or (120 if error.retryable else 3600) if isinstance(error,IntegrationError) else 3600
        message=str(error) if isinstance(error,IntegrationError) else 'Неожиданный формат исторических данных; полнота не подтверждена.'
        if kind=='sales' and ('HTTP 401' in message or 'HTTP 403' in message):
            message='Нет доступа к истории продаж WB. Проверьте ключ с правами «Статистика».'
        HistorySyncState.objects.filter(pk=state.pk,lease_token=lease).update(next_attempt=timezone.now()+timedelta(seconds=delay),status=message[:240])
        if isinstance(error,IntegrationError): raise
        raise IntegrationError(message) from error
    finally:
        HistorySyncState.objects.filter(pk=state.pk,lease_token=lease).update(lease_token=None,lease_until=None)
