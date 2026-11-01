"""Discover changed orders from statistics, then fetch exact creation-date batches.

The business orders API defaults creationDateFrom to 30 days ago even when
update dates are supplied. It must never certify an older update interval.
Contract: yandex-market/yandex-market-partner-api, 96f7b9b85919e38ad8d8bcbeeae958953e47e7f6.
"""
from collections import defaultdict
from datetime import date, timedelta
from django.db import transaction
from django.utils import timezone
from desk.models import Account, YandexOrderState
from .base import IntegrationError
from .wb_sales import source_time

VERSION = 2
STATS_LAG = timedelta(minutes=45)


def progress(cursor):
    if cursor.get('ym_version') != VERSION: return ''
    if not cursor.get('campaigns_complete'): return 'получение списка магазинов'
    campaigns, index = cursor.get('campaigns', []), cursor.get('campaign_index', 0)
    if type(index) is not int or not 0 <= index < len(campaigns): return ''
    return f'магазин {campaigns[index]} ({index+1} из {len(campaigns)}); начало проверяемого интервала {cursor.get("slice_from", cursor.get("since", ""))}'


def paging_token(payload, previous=None):
    paging = payload.get('paging')
    if paging is not None and not isinstance(paging, dict):
        raise IntegrationError('Некорректная пагинация продаж Маркета.')
    token = (paging or {}).get('nextPageToken')
    if token is not None and (not isinstance(token, str) or not token or token == previous):
        raise IntegrationError('Маркет повторил или вернул некорректный токен страницы.')
    return token


def scan_page(account, state, cursor):
    from .ym_sales import YandexSales, identifier, order_header, plan_order, save_order
    adapter = YandexSales(account)
    next_cursor = dict(cursor)
    since, until = source_time(cursor['since']), source_time(cursor['until'])
    if not since or not until or not since < until:
        raise IntegrationError('Некорректный период продаж Маркета.')

    def request(method, path, **kwargs):
        if not type(state).objects.filter(pk=state.pk, lease_token=state.lease_token).update(
                lease_until=timezone.now()+timedelta(minutes=5)):
            raise IntegrationError('Загрузка продаж Маркета передана другому процессу.', request_sent=False)
        return adapter.request(method, path, **kwargs)

    if cursor.get('ym_version') != VERSION:
        next_cursor = {key: value for key, value in cursor.items() if key in {'since', 'until', 'days'}}
        next_cursor['ym_version'] = VERSION
        until = min(until, timezone.now()-STATS_LAG)
        next_cursor['until'] = until.isoformat()
    if not next_cursor.get('campaigns_complete'):
        params = {'limit': 100}
        previous = next_cursor.get('campaign_token')
        if previous: params['page_token'] = previous
        payload = request('GET', '/v2/campaigns', params=params)
        rows = payload.get('campaigns') if isinstance(payload, dict) else None
        if not isinstance(rows, list) or len(rows) > 100:
            raise IntegrationError('Неожиданный список магазинов Маркета.')
        campaigns = list(next_cursor.get('campaigns', []))
        availability = dict(next_cursor.get('campaign_availability', {}))
        for row in rows:
            if not isinstance(row, dict) or not isinstance(row.get('business'), dict):
                raise IntegrationError('Не подтверждена принадлежность магазина кабинету Маркета.')
            if identifier(row['business'].get('id')) == identifier(account.business_id):
                pk = identifier(row.get('id'))
                if pk in campaigns: raise IntegrationError('Маркет повторил магазин при обходе страниц.')
                campaigns.append(pk)
                if isinstance(row.get('apiAvailability'), str): availability[pk] = row['apiAvailability']
        token = paging_token(payload, previous)
        pager = payload.get('pager') or {}
        if not isinstance(pager, dict): raise IntegrationError('Некорректный список страниц магазинов.')
        pages, current_page = pager.get('pagesCount', 1), pager.get('currentPage', 1)
        if type(pages) is not int or type(current_page) is not int or pages < 1 or current_page < 1:
            raise IntegrationError('Некорректные номера страниц магазинов Маркета.')
        if not token and pages > current_page:
            raise IntegrationError('Не подтверждён полный список магазинов Маркета: нет токена следующей страницы.')
        next_cursor['campaigns'] = campaigns
        next_cursor['campaign_availability'] = availability
        if token:
            next_cursor['campaign_token'] = token
            return 0, next_cursor, False
        if not campaigns: raise IntegrationError('В API не найдены магазины настроенного Business ID.')
        # Restore previously accessible shops before diagnosing an archived shop
        # without rights. An inaccessible shop still prevents full certification.
        known = set(YandexOrderState.objects.filter(account=account).values_list('campaign_id', flat=True))
        next_cursor['campaigns'] = sorted(campaigns, key=lambda pk: (
            availability.get(pk) not in {None, 'AVAILABLE'}, pk not in known, int(pk)))
        next_cursor.update(campaigns_complete=True, campaign_index=0, slice_from=since.isoformat())
        next_cursor.pop('campaign_token', None)

    campaigns = next_cursor['campaigns']
    index = next_cursor.get('campaign_index', 0)
    if type(index) is not int or not 0 <= index < len(campaigns):
        raise IntegrationError('Некорректная позиция обхода магазинов Маркета.')
    campaign = identifier(campaigns[index])
    availability = (next_cursor.get('campaign_availability') or {}).get(campaign)
    if availability and availability != 'AVAILABLE':
        raise IntegrationError(f'Магазин Маркета {campaign}: API недоступен ({availability}); полнота кабинета не подтверждена.', request_sent=False)
    start = source_time(next_cursor.get('slice_from')) or since
    end = min(start + timedelta(days=29), until)
    if not since <= start < end <= until:
        raise IntegrationError('Некорректный интервал статистики Маркета.')
    params = {'limit': 50}
    previous = next_cursor.get('page_token')
    page_count = next_cursor.get('page_count', 0)
    if type(page_count) is not int or not 0 <= page_count < 2000:
        raise IntegrationError('Достигнут предел страниц статистики Маркета; полнота не подтверждена.')
    if previous: params['page_token'] = previous
    try:
        payload = request('POST', f'/v2/campaigns/{campaign}/stats/orders', params=params,
            json={'updateFrom': timezone.localdate(start).isoformat(), 'updateTo': timezone.localdate(end).isoformat()})
    except IntegrationError as error:
        raise IntegrationError(f'Магазин Маркета {campaign}: {error}', retryable=error.retryable,
            retry_after=error.retry_after, rate_limited=error.rate_limited, request_sent=error.request_sent) from error
    result = payload.get('result') if isinstance(payload, dict) else None
    entries = result.get('orders') if isinstance(result, dict) else None
    if not isinstance(entries, list) or len(entries) > 50:
        raise IntegrationError('Неожиданный формат страницы статистики заказов Маркета.')
    token = paging_token(result, previous)
    if token and not entries: raise IntegrationError('Пустая страница статистики с продолжением.')
    stats, batches = {}, defaultdict(list)
    for entry in entries:
        if not isinstance(entry, dict): raise IntegrationError('Некорректный заказ в статистике Маркета.')
        oid = identifier(entry.get('id'))
        if oid in stats: raise IntegrationError('Статистика Маркета повторила заказ.')
        if entry.get('fake') is True: continue
        try: created = date.fromisoformat(entry.get('creationDate'))
        except (ValueError, TypeError): raise IntegrationError('В статистике отсутствует дата создания заказа Маркета.')
        if created > timezone.localdate(): raise IntegrationError('Статистика содержит будущую дату заказа.')
        stats[oid] = entry
        batches[created].append(oid)
    plans = []
    # At most 50 statistics rows per turn. Explicit creation dates + IDs avoid implicit
    # defaults for old orders and works for a delivery months after creation.
    pending_days = sorted(batches)
    while pending_days:
        created = pending_days[0]
        selected_days = [d for d in pending_days if d < created+timedelta(days=30)]
        ids = [oid for d in selected_days for oid in batches[d]]
        creation_end = selected_days[-1]+timedelta(days=1)
        pending_days = [d for d in pending_days if d not in selected_days]
        payload = request('POST', adapter.path('orders', 1), params={'limit': 50}, json={
            'fake': False, 'campaignIds': [int(campaign)], 'orderIds': [int(oid) for oid in ids],
            'dates': {'creationDateFrom': created.isoformat(), 'creationDateTo': creation_end.isoformat()}})
        rows = payload.get('orders') if isinstance(payload, dict) else None
        if not isinstance(rows, list) or len(rows) > len(ids) or paging_token(payload):
            raise IntegrationError('Маркет вернул неполную или некорректную страницу выбранных заказов.')
        seen = set()
        for row in rows:
            cid, oid, actual_created, changed, status = order_header(row, timezone.now())
            if (cid != campaign or oid not in ids or oid in seen or row['fake']
                    or timezone.localdate(actual_created).isoformat() != stats[oid]['creationDate']):
                raise IntegrationError('Выбранный заказ Маркета не совпадает с магазином, ID или датой.')
            seen.add(oid)
            plans.append(plan_order(row, stats[oid], timezone.now()))
        if seen != set(ids):
            raise IntegrationError('Не все заказы статистики доступны в API заказов; полнота не подтверждена.', retryable=True, retry_after=600)
    with transaction.atomic():
        Account.objects.select_for_update().get(pk=account.pk)
        if not type(state).objects.select_for_update().filter(pk=state.pk, lease_token=state.lease_token).exists():
            raise IntegrationError('Загрузка продаж Маркета передана другому процессу.')
        count = sum(save_order(account, plan) for plan in plans)
    if token:
        next_cursor['page_token'] = token
        next_cursor['page_count'] = page_count+1
        return count, next_cursor, False
    next_cursor.pop('page_token', None)
    next_cursor.pop('page_count', None)
    if end < until:
        next_cursor['slice_from'] = end.isoformat()
        return count, next_cursor, False
    if index + 1 < len(campaigns):
        next_cursor.update(campaign_index=index+1, slice_from=since.isoformat())
        return count, next_cursor, False
    return count, next_cursor, True
