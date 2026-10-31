"""Read-only Market order snapshots and versioned, per-unit sale events."""
import copy
import uuid
from collections import defaultdict
from datetime import timedelta
from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from desk.models import Account, Audit, SaleEvent, SalesSyncState, YandexOrderState
from .base import IntegrationError
from .coverage import record_coverage
from .rate_limit import acquire, observe
from .wb_sales import source_time, store_sales
from .yandex import YandexMarket
from .window import sales_window_start

VERSION = 1
FINAL = {'DELIVERED', 'PARTIALLY_RETURNED', 'RETURNED'}
KNOWN = FINAL | {'PLACING','RESERVED','UNPAID','PROCESSING','DELIVERY','PICKUP','CANCELLED','PENDING'}
UNIT_STATUSES = {'CREATED','SHIPPED','CANCELLED','DELIVERED_TO_BUYER','LOST','REJECTED','RETURNED'}


class YandexSales(YandexMarket):
    def __init__(self, account):
        account = copy.copy(account)
        account.token_env = account.sales_token_env or account.token_env
        super().__init__(account)

    def before_request(self, token):
        acquire(token, namespace='ym-sales', label='Яндекс Маркет')

    def after_response(self, token, response):
        return observe(token, response, namespace='ym-sales')

    def access_error(self, status):
        if status in {401,403}:
            return f'Маркет: HTTP {status}. Для продаж нужны права чтения заказов и статистики; ключ отзывов может их не иметь.'
        return super().access_error(status)


def identifier(value):
    if type(value) is int: value=str(value)
    if not isinstance(value,str) or not value.isascii() or not value.isdecimal() or not 0<int(value)<2**63:
        raise IntegrationError('Нет достоверного идентификатора заказа, магазина или позиции Маркета.')
    return str(int(value))


def quantity(value):
    if type(value) is not int or not 0<=value<=20000:
        raise IntegrationError('Некорректное количество единиц товара Маркета.')
    return value


def order_header(row, now):
    if not isinstance(row,dict): raise IntegrationError('Некорректная строка заказа Маркета.')
    campaign,order=identifier(row.get('campaignId')),identifier(row.get('orderId'))
    created,changed=source_time(row.get('creationDate')),source_time(row.get('updateDate'))
    status=row.get('status')
    if not created or not changed or not created<=changed<=now or not isinstance(status,str) or type(row.get('fake')) is not bool or not isinstance(row.get('items'),list):
        raise IntegrationError('В заказе Маркета нет достоверной версии, даты, статуса или списка товаров.')
    if len(row['items'])>1000: raise IntegrationError('Слишком много позиций в заказе Маркета.')
    if status in FINAL and not row['items']: raise IntegrationError('У завершённого заказа Маркета отсутствуют позиции товаров.')
    return campaign,order,created,changed,status


def units(item, status):
    """Unit statuses are authoritative; pickup and rejected units are not sales."""
    count=quantity(item.get('count'))
    if status not in KNOWN: return None,None
    if status=='CANCELLED': return 0,0
    statuses=item.get('itemStatuses')
    if statuses is not None:
        if not isinstance(statuses,list) or not statuses: raise IntegrationError('Некорректные статусы единиц товара Маркета.')
        counts={}
        for entry in statuses:
            if not isinstance(entry,dict) or entry.get('status') not in UNIT_STATUSES or entry['status'] in counts:
                raise IntegrationError('Неизвестный или повторный статус единицы товара Маркета.')
            counts[entry['status']]=quantity(entry.get('count'))
        gross=counts.get('DELIVERED_TO_BUYER',0)+counts.get('RETURNED',0)
        if gross>20000: raise IntegrationError('Слишком много выкупленных единиц Маркета.')
        return gross,counts.get('RETURNED',0)
    if status=='DELIVERED': return count,0
    if status=='RETURNED': return count,count
    if status=='PARTIALLY_RETURNED': return None,None
    return 0,0


def plan_order(row, stats, now):
    campaign,order,created,changed,status=order_header(row,now)
    if row['fake']: return {'campaign':campaign,'order':order,'created':created,'changed':changed,'items':[],'source':{'fake':True}}
    items=[];seen=set();total=0
    for item in row['items']:
        if not isinstance(item,dict): raise IntegrationError('Некорректная позиция заказа Маркета.')
        item_id=identifier(item.get('id'));sku=item.get('offerId')
        if item_id in seen or not isinstance(sku,str) or not 1<=len(sku)<=120:
            raise IntegrationError('Повторный ID позиции или некорректный offerId Маркета.')
        seen.add(item_id)
        gross,returned=units(item,status)
        total+=gross or 0
        if total>50000: raise IntegrationError('Превышен предел единиц в странице заказа Маркета.')
        items.append({'id':item_id,'sku':sku,'gross':gross,'returned':returned,'count':item['count'],
                      'itemStatuses':item.get('itemStatuses')})
    sold_at=None;returned_at=None
    if status in KNOWN and any(i['gross'] is None or i['gross']>0 for i in items):
        if not isinstance(stats,dict) or identifier(stats.get('id'))!=order or stats.get('fake') is True:
            raise IntegrationError('Статистика Маркета ещё не содержит заказ. Повторим после обновления отчёта.',retryable=True,retry_after=600)
        stats_status=stats.get('status');stamp=source_time(stats.get('statusUpdateDate'))
        if not stamp or not created<=stamp<=now: raise IntegrationError('Нет достоверной даты изменения статуса в статистике Маркета.')
        if stamp>changed:
            raise IntegrationError('Версии заказа и статистики Маркета ещё не согласованы. Повторим позже.',retryable=True,retry_after=600)
        if status in FINAL and stats_status not in {'DELIVERED','PARTIALLY_DELIVERED','PARTIALLY_RETURNED','RETURNED'}:
            raise IntegrationError('Статистика Маркета отстаёт от статуса заказа. Повторим позже.',retryable=True,retry_after=600)
        if stats_status=='DELIVERED': sold_at=stamp
        if stats_status in {'PARTIALLY_RETURNED','RETURNED'}: returned_at=stamp
    source={'campaignId':campaign,'orderId':order,'creationDate':created.isoformat(),'updateDate':changed.isoformat(),
            'status':status,'items':items,'saleDate':sold_at.isoformat() if sold_at else None,
            'returnStatusDate':returned_at.isoformat() if returned_at else None}
    return {'campaign':campaign,'order':order,'created':created,'changed':changed,'items':items,'sold_at':sold_at,'returned_at':returned_at,'source':source}


@transaction.atomic
def save_order(account, plan):
    # Caller holds Account before the state lease. Serialize current/history
    # replacements and reject snapshots older than the saved order version.
    Account.objects.select_for_update().get(pk=account.pk)
    prior=YandexOrderState.objects.filter(account=account,campaign_id=plan['campaign'],order_id=plan['order']).first()
    if prior and prior.changed_at>plan['changed']: return 0
    if prior and prior.source_data==plan['source']: return 0
    order_key=plan['campaign']+'/'+plan['order']
    previous={e.external_id:e for e in SaleEvent.objects.filter(account=account,order_id=order_key,external_id__startswith='YM:')}
    expected={};issues=[]
    for item in plan['items']:
        prefix=f'YM:S:{plan["campaign"]}:{plan["order"]}:{item["id"]}:'
        return_prefix=prefix.replace('YM:S:','YM:R:',1)
        if item['gross'] is None:
            # Preserve observed facts, but never derive gross sales from an
            # ambiguous remaining quantity in a partially returned order.
            expected.update({key:value for key,value in previous.items() if key.startswith((prefix,return_prefix))})
            issues.append({'sku':item['sku'],'count':None,'since':plan['created'].isoformat(),'until':plan['changed'].isoformat(),'reason':'Не подтверждено количество до частичного возврата'})
            continue
        missing=0
        for ordinal in range(item['gross']):
            key=prefix+str(ordinal);old=previous.get(key)
            occurred=old.occurred_at if old else plan.get('sold_at')
            if occurred is None:
                missing+=1;continue
            expected[key]=SaleEvent(external_id=key,order_id=order_key,sku=item['sku'],kind='sale',occurred_at=occurred,changed_at=plan['changed'],
                                    source_data={'source':'ym-orders+stats','campaignId':plan['campaign'],'orderId':plan['order'],'itemId':item['id'],'date_basis':'delivered_status'})
        if missing: issues.append({'sku':item['sku'],'count':missing,'since':plan['created'].isoformat(),'until':plan['changed'].isoformat(),'reason':'Нет подтверждённой даты исходного выкупа'})
        for ordinal in range(item['returned']):
            key=return_prefix+str(ordinal);old=previous.get(key)
            occurred=old.occurred_at if old else plan.get('returned_at') or plan['changed']
            expected[key]=SaleEvent(external_id=key,order_id=order_key,sku=item['sku'],kind='return',occurred_at=occurred,changed_at=plan['changed'],
                                    source_data={'source':'ym-orders+stats','campaignId':plan['campaign'],'orderId':plan['order'],'itemId':item['id'],'date_basis':'return_status_confirmation'})
    obsolete=set(previous)-set(expected)
    if obsolete: SaleEvent.objects.filter(account=account,external_id__in=obsolete).delete()
    stored=store_sales(account,expected)
    YandexOrderState.objects.update_or_create(account=account,campaign_id=plan['campaign'],order_id=plan['order'],defaults={
        'changed_at':plan['changed'],'issues':issues,'source_data':plan['source']})
    Audit.objects.create(company=account.company,action='ym.sales.snapshot',object_id=order_key,
                         before={'events':len(previous)},after={'events':len(expected),'issues':len(issues)})
    return stored


def load_page(account, state, cursor):
    """One paged updated-order slice (at most 30 days), enriched by order IDs."""
    adapter=YandexSales(account)
    since,until=source_time(cursor['since']),source_time(cursor['until'])
    start=source_time(cursor.get('slice_from')) or since
    end=min(start+timedelta(days=30),until)
    if not since<=start<end<=until: raise IntegrationError('Некорректный интервал страницы продаж Маркета.')
    body={'fake':False,'dates':{'updateDateFrom':start.isoformat(),'updateDateTo':end.isoformat()}}
    params={'limit':50}
    if cursor.get('page_token'): params['page_token']=cursor['page_token']
    def heartbeat():
        if not type(state).objects.filter(pk=state.pk,lease_token=state.lease_token).update(lease_until=timezone.now()+timedelta(minutes=5)):
            raise IntegrationError('Загрузка продаж Маркета передана другому процессу.',request_sent=False)
    heartbeat()
    payload=adapter.request('POST',adapter.path('orders',1),json=body,params=params)
    rows=payload.get('orders') if isinstance(payload,dict) else None
    paging=payload.get('paging') if isinstance(payload,dict) else None
    if not isinstance(rows,list) or len(rows)>50 or (paging is not None and not isinstance(paging,dict)):
        raise IntegrationError('Неожиданный формат страницы заказов Маркета.')
    token=(paging or {}).get('nextPageToken')
    if token and (not isinstance(token,str) or token==cursor.get('page_token') or not rows):
        raise IntegrationError('Маркет повторил позицию страницы заказов.')
    now=timezone.now();groups=defaultdict(list);selected=[];seen=set()
    for row in rows:
        campaign,order,created,changed,status=order_header(row,now)
        if (campaign,order) in seen: raise IntegrationError('Повторный заказ в странице Маркета.')
        seen.add((campaign,order))
        if not start<=changed<=end: continue
        selected.append(row)
        if not row['fake'] and (status in FINAL or any(isinstance(i,dict) and any(isinstance(s,dict) and s.get('status') in {'DELIVERED_TO_BUYER','RETURNED'} for s in (i.get('itemStatuses') or [])) for i in row['items'])):
            groups[campaign].append(order)
    stats={}
    for campaign,orders in groups.items():
        heartbeat()
        data=adapter.request('POST',f'/v2/campaigns/{campaign}/stats/orders',json={'orders':[int(i) for i in orders]},params={'limit':200})
        result=data.get('result') if isinstance(data,dict) else None
        entries=result.get('orders') if isinstance(result,dict) else None
        if not isinstance(entries,list): raise IntegrationError('Неожиданный формат статистики заказов Маркета.')
        stats_paging=result.get('paging')
        if stats_paging is not None and not isinstance(stats_paging,dict): raise IntegrationError('Некорректная позиция страницы статистики Маркета.')
        if (stats_paging or {}).get('nextPageToken'):
            raise IntegrationError('Статистика вернула неполный набор выбранных заказов; полнота не подтверждена.')
        for entry in entries:
            if not isinstance(entry,dict): raise IntegrationError('Некорректная статистика заказа Маркета.')
            oid=identifier(entry.get('id'))
            if oid not in orders or (campaign,oid) in stats: raise IntegrationError('Статистика вернула посторонний или повторный заказ.')
            stats[campaign,oid]=entry
    plans=[plan_order(row,stats.get((identifier(row['campaignId']),identifier(row['orderId']))),now) for row in selected]
    next_cursor=dict(cursor)
    if token: next_cursor['page_token']=token
    else:
        next_cursor.pop('page_token',None)
        next_cursor['slice_from']=end.isoformat()
    complete=not token and end==until
    with transaction.atomic():
        Account.objects.select_for_update().get(pk=account.pk)
        if not type(state).objects.select_for_update().filter(pk=state.pk,lease_token=state.lease_token).exists():
            raise IntegrationError('Загрузка продаж Маркета передана другому процессу.')
        count=sum(save_order(account,plan) for plan in plans)
    return count,next_cursor,complete


def sync_ym_sales(account):
    if account.platform!='ym' or not account.enabled or not account.sales_enabled or account.company.is_demo:
        raise IntegrationError('Включите продажи реального кабинета Маркета.',request_sent=False)
    if Account.objects.filter(platform='ym',sales_enabled=True,business_id=account.business_id).exclude(pk=account.pk).exists():
        raise IntegrationError('Продажи этого Business ID подключены повторно. Проверка конфигурации обязательна.',request_sent=False)
    state,_=SalesSyncState.objects.get_or_create(account=account);now=timezone.now()
    if state.next_attempt>now:return 0
    lease=uuid.uuid4()
    if not SalesSyncState.objects.filter(pk=state.pk).filter(Q(lease_until__isnull=True)|Q(lease_until__lte=now)).update(lease_token=lease,lease_until=now+timedelta(minutes=5)):return 0
    state.refresh_from_db()
    try:
        cursor=state.cursor if isinstance(state.cursor,dict) else {}
        since,until=source_time(cursor.get('since')),source_time(cursor.get('until'))
        if cursor.get('ym_version')!=VERSION or not since or not until or not now-timedelta(days=8)<=since<until<=now:
            cursor={'ym_version':VERSION,'since':sales_window_start(state, now).isoformat(),'until':now.isoformat()}
        count,next_cursor,complete=load_page(account,state,cursor)
        if complete:
            record_coverage(state,source_time(cursor['since']),source_time(cursor['until']))
            pending=YandexOrderState.objects.filter(account=account).exclude(issues=[]).count()
            status=f'Маркет: опрос периода завершён. Заказов с неподтверждёнными данными: {pending}.'
            SalesSyncState.objects.filter(pk=state.pk,lease_token=lease).update(cursor={},last_sync=timezone.now(),next_attempt=timezone.now()+timedelta(minutes=30),status=status)
        else:
            SalesSyncState.objects.filter(pk=state.pk,lease_token=lease).update(cursor=next_cursor,next_attempt=timezone.now()+timedelta(seconds=61),status=f'Маркет: обработано событий {count}; загрузка продолжается.')
        return count
    except IntegrationError as error:
        SalesSyncState.objects.filter(pk=state.pk,lease_token=lease).update(cursor=cursor,next_attempt=timezone.now()+timedelta(seconds=error.retry_after or (120 if error.retryable else 3600)),status=str(error)[:240])
        raise
    finally:
        SalesSyncState.objects.filter(pk=state.pk,lease_token=lease).update(lease_token=None,lease_until=None)
