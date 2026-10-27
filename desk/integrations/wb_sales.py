"""WB operational sales. One source row is one sold or returned unit."""
import copy
import uuid
from datetime import timedelta
from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from desk.models import Account, SaleEvent, SalesSyncState
from desk.products.identifiers import wb_nm_id
from .base import Adapter, IntegrationError
from .coverage import record_coverage
from .rate_limit import acquire, observe
from .window import LOOKBACK_DAYS, sync_window


def source_time(value):
    try:
        parsed = parse_datetime(value) if isinstance(value, str) else None
    except (ValueError, OverflowError):
        return None
    if parsed and timezone.is_naive(parsed):
        parsed = timezone.make_aware(parsed)
    return parsed


class WBSales(Adapter):
    base_url = 'https://statistics-api.wildberries.ru'

    def __init__(self, account):
        account = copy.copy(account)
        account.token_env = account.sales_token_env or account.token_env
        super().__init__(account)

    def before_request(self, token):
        acquire(token, namespace='wb-sales', interval=60)

    def after_response(self, token, response):
        return observe(token, response, namespace='wb-sales', fallback=7200, maximum=86400)


def sales_page(rows, since, until, changed_since):
    if not isinstance(rows, list) or len(rows) > 80000:
        raise IntegrationError('Неожиданный формат отчёта продаж WB.')
    events, changes = {}, []
    for row in rows:
        if not isinstance(row, dict): raise IntegrationError('WB вернул некорректную строку продаж.')
        changed, occurred = source_time(row.get('lastChangeDate')), source_time(row.get('date'))
        identifier, sku = row.get('saleID'), wb_nm_id(row.get('nmId'))
        if not changed or not occurred or not sku or not isinstance(identifier, str) or not 1 < len(identifier) <= 160 or identifier[0] not in {'S', 'R'}:
            raise IntegrationError('В строке продаж WB отсутствуют достоверные saleID, nmId или даты. Период не отмечен полным.')
        changes.append(changed)
        if changed > until: raise IntegrationError('WB вернул будущую дату изменения продажи. Полнота периода не подтверждена.')
        if not since <= occurred <= until or changed < changed_since: continue
        order_id = str(row.get('srid') or '')
        if len(order_id) > 160: raise IntegrationError('WB вернул слишком длинный ID заказа.')
        events[identifier] = SaleEvent(external_id=identifier, sku=sku, kind='sale' if identifier[0] == 'S' else 'return',
            occurred_at=occurred, changed_at=changed, order_id=order_id, source_data=row)
    if changes and (changes[-1] != max(changes) or changes[-1] <= changed_since):
        raise IntegrationError('Страница продаж WB не продвигает lastChangeDate. Требуется проверка; полнота периода не подтверждена.')
    return events


@transaction.atomic
def store_sales(account, events):
    # Fresh and historical readers share sale IDs. Serialize their upserts so
    # an older response cannot overwrite a newer correction from another lane.
    Account.objects.select_for_update().get(pk=account.pk)
    identifiers = list(events)
    for offset in range(0, len(identifiers), 500):
        for identifier, stored_change in SaleEvent.objects.filter(account=account, external_id__in=identifiers[offset:offset+500]).values_list('external_id', 'changed_at'):
            if events[identifier].changed_at < stored_change: del events[identifier]
    for event in events.values(): event.account = account
    SaleEvent.objects.bulk_create(list(events.values()), update_conflicts=True, unique_fields=['account','external_id'],
        update_fields=['sku','kind','occurred_at','changed_at','order_id','source_data','synced_at'], batch_size=500)
    return len(events)


def sync_wb_sales(account):
    if account.platform != 'wb' or not account.sales_enabled or not account.enabled or account.company.is_demo:
        raise IntegrationError('Включите продажи в настройках реального кабинета WB.', request_sent=False)
    state, _ = SalesSyncState.objects.get_or_create(account=account)
    now = timezone.now()
    if state.next_attempt > now:
        return 0
    lease = uuid.uuid4()
    if not SalesSyncState.objects.filter(pk=state.pk).filter(Q(lease_until__isnull=True) | Q(lease_until__lte=now)).update(lease_token=lease, lease_until=now + timedelta(minutes=5)):
        return 0
    state.refresh_from_db()
    lower, upper = sync_window(now)
    cursor = state.cursor if isinstance(state.cursor, dict) else {}
    until = source_time(cursor.get('until'))
    since = source_time(cursor.get('since'))
    page_from = source_time(cursor.get('page_from'))
    if (cursor.get('lookback_days') != LOOKBACK_DAYS or not until or not lower <= until <= upper
            or not since or not page_from or page_from < lower):
        cursor = {'lookback_days': LOOKBACK_DAYS, 'since': lower.isoformat(), 'until': upper.isoformat(), 'page_from': lower.isoformat()}
        until, since, page_from = upper, lower, lower
    try:
        rows = WBSales(account).request('GET', '/api/v1/supplier/sales', params={'dateFrom': cursor['page_from'], 'flag': 0})
        if not isinstance(rows, list) or len(rows) > 80000:
            raise IntegrationError('Неожиданный формат отчёта продаж WB.')
        if not rows:
            record_coverage(state, max(since, lower), now)
            SalesSyncState.objects.filter(pk=state.pk, lease_token=lease).update(cursor={}, last_sync=now, next_attempt=now + timedelta(minutes=30), status='Продажи WB: период загружен полностью. Оперативный отчёт, не финансовая сверка.')
            return 0
        observed_until = timezone.now()
        events = sales_page(rows, max(lower, since), observed_until, page_from)
        cursor['page_from'] = rows[-1]['lastChangeDate']
        cursor['until'] = observed_until.isoformat()
        with transaction.atomic():
            Account.objects.select_for_update().get(pk=account.pk)
            if not SalesSyncState.objects.select_for_update().filter(pk=state.pk, lease_token=lease).first():
                raise IntegrationError('Загрузка продаж передана другому процессу.')
            store_sales(account, events)
            SalesSyncState.objects.filter(pk=state.pk, lease_token=lease).update(cursor=cursor, next_attempt=now + timedelta(seconds=61), status=f'Обработано событий: {len(events)}. Продолжается загрузка; полнота периода ещё не подтверждена.')
        return len(events)
    except IntegrationError as error:
        delay = error.retry_after or (3600 if not error.retryable else 120)
        message = str(error)
        if 'HTTP 401' in message or 'HTTP 403' in message:
            message = 'Нет доступа к продажам WB. Проверьте ключ и права категории «Статистика».'
        SalesSyncState.objects.filter(pk=state.pk, lease_token=lease).update(next_attempt=timezone.now() + timedelta(seconds=delay), status=message[:240])
        raise
    finally:
        SalesSyncState.objects.filter(pk=state.pk, lease_token=lease).update(lease_token=None, lease_until=None)
