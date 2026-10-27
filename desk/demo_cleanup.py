"""Remove only proven seed_demo records, rejecting mixed or ambiguous data."""
from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from desk.demo_data import PRODUCTS, EXAMPLES
from desk.models import (Account, Analysis, Audit, Baseline, Company, HistorySyncState, Incident, Message,
                         Outgoing, Product, ProductMapping, ReviewAssessment, SaleEvent,
                         SalesDaily, SalesSyncState, SyncLog, SyncState, Thread)
from desk.models import YandexOrderState


class DemoCleanupProblem(ValueError):
    pass


def reject(condition, explanation):
    if condition:
        raise DemoCleanupProblem(explanation + ' Очистка отменена; данные не удалены.')


@transaction.atomic
def purge_demo(*, apply=False):
    companies = Company.objects.filter(is_demo=True).order_by('pk')
    if apply: companies = companies.select_for_update()
    company_ids = list(companies.values_list('pk', flat=True))
    accounts = Account.objects.filter(company_id__in=company_ids)
    products = Product.objects.filter(company_id__in=company_ids)
    if apply:
        accounts = accounts.select_for_update()
        products = products.select_for_update()
    account_ids = list(accounts.values_list('pk', flat=True))
    product_ids = list(products.values_list('pk', flat=True))
    threads = Thread.objects.filter(account_id__in=account_ids)
    if apply: threads = threads.select_for_update()
    thread_rows = list(threads.values('pk', 'external_id', 'kind', 'product_id', 'source_data'))
    thread_ids = [row['pk'] for row in thread_rows]
    cases = Incident.objects.filter(Q(product_id__in=product_ids) | Q(threads__pk__in=thread_ids)).distinct()
    case_ids = list(cases.values_list('pk', flat=True))
    cases = Incident.objects.filter(pk__in=case_ids)
    if apply: cases = cases.select_for_update()
    case_rows = list(cases.values('pk', 'product_id', 'bitrix_id', 'opening_snapshot'))

    codes = {row[0] for row in PRODUCTS}
    expected_threads = {f'demo-{index}': row[4] for index, row in enumerate(EXAMPLES)}
    reject(accounts.filter(Q(enabled=True) | Q(sales_enabled=True)).exists(), 'Демо-кабинет включён для внешней интеграции.')
    reject(YandexOrderState.objects.filter(account_id__in=account_ids).exists(), 'В демо-кабинете есть рабочие данные заказов Маркета.')
    reject(products.exclude(code__in=codes).exists(), 'В демо-компании есть товары вне стандартного набора.')
    reject(Product.objects.filter(code__in=products.values_list('code', flat=True), company__is_demo=False).exists(), 'Демонстрационный ProductID используется рабочей компанией.')
    reject(any(row['external_id'] not in expected_threads or expected_threads[row['external_id']] != row['kind'] for row in thread_rows), 'В демо-кабинете есть обращения без стандартной демо-метки.')
    reject(any(row['source_data'] for row in thread_rows), 'У демо-обращений есть исходные данные маркетплейса.')
    reject(any(row['product_id'] is not None and row['product_id'] not in product_ids for row in thread_rows), 'Демо-обращение связано с рабочим товаром.')
    reject(Thread.objects.filter(product_id__in=product_ids).exclude(pk__in=thread_ids).exists(), 'Демонстрационный товар связан с обращением вне очистки.')
    reject(ProductMapping.objects.filter(product_id__in=product_ids).exclude(account_id__in=account_ids).exists(), 'Демонстрационный товар связан с рабочим кабинетом.')
    reject(ProductMapping.objects.filter(account_id__in=account_ids).exclude(product_id__in=product_ids).exists(), 'Демо-кабинет связан с рабочим товаром.')
    reject(ProductMapping.objects.filter(account_id__in=account_ids).exclude(sku__in=codes, external_product_id__in=codes).exists(), 'В демо-кабинете есть сопоставление вне стандартного набора.')
    reject(SaleEvent.objects.filter(account_id__in=account_ids).exists(), 'У демо-кабинета есть отдельные события продаж API.')
    daily = SalesDaily.objects.filter(account_id__in=account_ids)
    reject(daily.exclude(sku__in=codes, redemptions=25, orders=28, delivered=26, returns=1).exists(), 'В демо-компании есть продажи вне стандартного набора.')
    reject(Outgoing.objects.filter(thread_id__in=thread_ids).filter(Q(status__in=['Sending', 'Sent', 'Confirmed']) | ~Q(external_id='')).exists(), 'Демо-обращение связано с начатой или подтверждённой внешней отправкой.')
    reject(any(row['product_id'] not in product_ids or row['bitrix_id'] for row in case_rows), 'Расследование связано с рабочим товаром или внешней задачей.')
    reject(Incident.threads.through.objects.filter(incident_id__in=case_ids).exclude(thread_id__in=thread_ids).exists(), 'Есть смешанное расследование с обращениями вне очистки.')
    for row in case_rows:
        snapshot = row['opening_snapshot']
        reject(not isinstance(snapshot, dict), 'Неверный формат снимка расследования.')
        reject(not isinstance(snapshot.get('company_ids', []), list), 'Неверный формат компаний в снимке расследования.')
        reject(not set(map(str, snapshot.get('company_ids', []))) <= set(map(str, company_ids)), 'Снимок расследования включает рабочую компанию.')
        reject(snapshot.get('account_id') is not None and snapshot['account_id'] not in account_ids, 'Снимок расследования включает рабочий кабинет.')
    reject(SyncState.objects.filter(account_id__in=account_ids, lease_until__gt=timezone.now()).exists()
           or SalesSyncState.objects.filter(account_id__in=account_ids, lease_until__gt=timezone.now()).exists()
           or HistorySyncState.objects.filter(account_id__in=account_ids, lease_until__gt=timezone.now()).exists(), 'Для демо-кабинета ещё действует блокировка синхронизации.')

    counts = {
        'reviews': sum(row['kind'] == 'review' for row in thread_rows),
        'questions': sum(row['kind'] == 'question' for row in thread_rows),
        'messages': Message.objects.filter(thread_id__in=thread_ids).count(),
        'analyses': Analysis.objects.filter(thread_id__in=thread_ids).count(),
        'assessments': ReviewAssessment.objects.filter(thread_id__in=thread_ids).count(),
        'outgoing': Outgoing.objects.filter(thread_id__in=thread_ids).count(),
        'incidents': len(case_ids), 'products': len(product_ids), 'accounts': len(account_ids),
        'mappings': ProductMapping.objects.filter(account_id__in=account_ids).count(),
        'daily_sales': daily.count(), 'baselines': Baseline.objects.filter(product_id__in=product_ids).count(),
        'sync_logs': SyncLog.objects.filter(account_id__in=account_ids).count(),
        'sync_states': SyncState.objects.filter(account_id__in=account_ids).count() + SalesSyncState.objects.filter(account_id__in=account_ids).count() + HistorySyncState.objects.filter(account_id__in=account_ids).count(),
    }
    if not apply or not any(counts.values()): return counts
    Outgoing.objects.filter(thread_id__in=thread_ids).delete()
    Message.objects.filter(thread_id__in=thread_ids).delete()
    ReviewAssessment.objects.filter(thread_id__in=thread_ids).delete()
    Analysis.objects.filter(thread_id__in=thread_ids).delete()
    Incident.objects.filter(pk__in=case_ids).delete()
    Thread.objects.filter(pk__in=thread_ids).delete()
    Baseline.objects.filter(product_id__in=product_ids).delete()
    SalesDaily.objects.filter(account_id__in=account_ids).delete()
    ProductMapping.objects.filter(account_id__in=account_ids).delete()
    Account.objects.filter(pk__in=account_ids).delete()  # Cascades only its local sync state/logs.
    Product.objects.filter(pk__in=product_ids).delete()
    # Keep the empty demo company, users, memberships and audit. Its existence also
    # prevents prepare_local from automatically seeding the examples again.
    Audit.objects.create(action='demo.purge', object_id='seed_demo',
                         before={'company_ids': company_ids, 'counts': counts}, after={'demo_threads_remaining': 0})
    return counts
