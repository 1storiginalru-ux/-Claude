"""Read-only, account-scoped causes of incomplete control data; never calls an API."""
from collections import Counter, defaultdict
from django.db.models import Count, Max, Min, Q
from django.utils import timezone
from desk.ai.backfill_period import period_start
from desk.integrations.ym_scan import progress as market_sales_progress
from desk.models import Company, HistorySyncState, Product, ProductMapping, SaleEvent, SalesSyncState, SyncState, Thread, YandexOrderState
from .dataset import ControlDataset
from .rules import is_negative, review_summary, trusted_analysis


CAUSES = {
    'mapping': ('Нет связи товара с артикулом кабинета', 'Проверить Product Mapping и артикулы отзывов.'),
    'few_sales': ('Меньше 100 выкупов', 'Продолжать накопление; проверить глубину истории продаж.'),
    'window_missing': ('Окно выкупов ещё не определено', 'Проверить артикул и наличие выкупов за загруженный период. Полноту окна можно оценить после определения его начала.'),
    'manual_sales': ('Нет загруженных продаж', 'Проверить источник продаж кабинета.'),
    'sales_coverage': ('Не подтверждён период продаж', 'Проверить текущую загрузку и историю продаж кабинета.'),
    'sales_stale': ('Продажи не обновлялись более 3 часов', 'Проверить срок следующей попытки, лимиты API и службу voice-poll.'),
    'sale_date_quantity': ('Неизвестны дата или количество выкупов Маркета', 'Восстановить исходные данные заказа; не подставлять дату возврата вместо выкупа.'),
    'reviews_coverage': ('Не подтверждён период отзывов', 'Проверить загрузку истории; для WB требуется также архив отзывов.'),
    'reviews_stale': ('Отзывы не обновлялись более 3 часов', 'Проверить загрузку отзывов и ограничения API.'),
    'classification': ('Не завершён разбор отзывов в окне', 'Проверить очередь AI и спорные результаты; решения сотрудников имеют приоритет.'),
}


def state_snapshot(state):
    if not state:
        return None
    return {key: getattr(state, key, None) for key in ['coverage_since', 'coverage_until', 'next_attempt', 'status']} | {
        'continuation': bool(getattr(state, 'cursor', None)),
        'lease_until': getattr(state, 'lease_until', None),
        'progress': market_sales_progress(getattr(state, 'cursor', {}) or {}),
    }


def control_data_report(accounts, now=None):
    """All mapped catalogue products, with review-bearing problems ranked first.

    Counts are explicit: product totals are distinct codes; row totals are
    product/cabinet pairs; overlapping causes are not added into one total.
    """
    now = now or timezone.now()
    since = period_start(6, now)
    accounts = list(accounts)
    account_ids = {a.pk for a in accounts}
    companies = Company.objects.filter(pk__in={a.company_id for a in accounts})
    linked = Thread.objects.filter(account_id__in=account_ids, kind='review', received_at__lte=now)
    mapped = ProductMapping.objects.filter(account_id__in=account_ids, product__company__in=companies)
    candidates = Product.objects.filter(company__in=companies).filter(
        Q(code__in=mapped.values('product__code')) | Q(code__in=linked.values('product__code')))
    ids = candidates.values('code').annotate(first_pk=Min('pk')).values('first_pk')
    products = list(Product.objects.filter(pk__in=ids).order_by('code'))
    dataset = ControlDataset(products, companies, now=now, account_ids=account_ids)
    histories = {(s.account_id, s.kind): s for s in HistorySyncState.objects.filter(account_id__in=account_ids)}
    sales_states = {s.account_id: s for s in SalesSyncState.objects.filter(account_id__in=account_ids)}
    current_reviews = {s.account_id: s for s in SyncState.objects.filter(account_id__in=account_ids)}
    storage = {s['account_id']: s for s in SaleEvent.objects.filter(account_id__in=account_ids, kind='sale', occurred_at__lte=now)
               .values('account_id').annotate(count=Count('pk'), first=Min('occurred_at'), last=Max('occurred_at'), saved_at=Max('synced_at'))}
    order_issues = defaultdict(Counter)
    order_counts = Counter()
    for order in YandexOrderState.objects.filter(account_id__in=account_ids).exclude(issues=[]).values('account_id', 'issues'):
        order_counts[order['account_id']] += 1
        for issue in order['issues']:
            order_issues[order['account_id']][issue.get('reason', 'Причина не указана')] += 1
    unmapped_rows = linked.filter(product__isnull=True).values('account_id').annotate(
        total=Count('pk'), recent=Count('pk', filter=Q(received_at__gte=since)))
    unmapped, unmapped_recent = Counter(), Counter()
    for row in unmapped_rows:
        unmapped[row['account_id']] = row['total']
        unmapped_recent[row['account_id']] = row['recent']
    rows, reasons = [], Counter()
    for product in products:
        code = product.code
        account_pairs = defaultdict(list)
        for mapping in dataset.mappings[code]:
            account_pairs[mapping.account_id].append(mapping.sku)
        reviews = defaultdict(list)
        for thread in dataset.threads[code]:
            reviews[thread.account_id].append(thread)
        for account in accounts:
            if account.pk not in account_pairs and account.pk not in reviews:
                continue
            summary = review_summary(product, dataset=dataset, account_id=account.pk)
            period = summary['period']
            recent = [t for t in reviews[account.pk] if since <= t.received_at <= now]
            negative = sum(trusted_analysis(a := dataset.analysis(t)) and is_negative(a) for t in recent)
            pending = [t for t in recent if not trusted_analysis(dataset.analysis(t))]
            suspected = sum((t.rating is not None and t.rating <= 3) or is_negative(dataset.analysis(t)) for t in pending)
            causes = {issue['code'] for issue in period['issues']}
            if not period['start_at']:
                # No sale window is different from a known window with a hole.
                # Keep the calculation blocked, but don't count an unassessable
                # interval as two independent API coverage failures.
                causes.difference_update({'sales_coverage', 'reviews_coverage'})
                causes.add('window_missing')
            if summary['unclassified']:
                causes.add('classification')
            if recent and any(t.sku not in account_pairs[account.pk] for t in recent):
                causes.add('mapping')
            # A catalogue row with no saved activity is not a failed review check.
            activity = bool(reviews[account.pk] or any(e['account_id'] == account.pk for e in dataset.events[code])
                            or any(d['account_id'] == account.pk and d['redemptions'] for d in dataset.daily[code])
                            or any(i['account_id'] == account.pk for i in dataset.uncertainties[code]))
            ready = summary['complete'] and 'mapping' not in causes
            priority = 0 if negative or summary['negative'] else 1 if suspected else 2 if recent else 3
            if activity and not ready:
                reasons.update(causes)
            rows.append({'product_id': product.pk, 'code': code, 'name': product.name,
                         'account_id': account.pk, 'account': account.name, 'priority': priority,
                         'reviews': len(recent), 'negative': negative, 'suspected': suspected, 'pending': len(pending),
                         'window_negative': summary['negative'], 'window_pending': summary['unclassified'],
                         'redemptions': period['redemptions'], 'start_at': period['start_at'], 'until': period['until'],
                         'per100': summary['per100'] if ready else None, 'ready': ready, 'activity': activity,
                         'causes': sorted(causes), 'issues': period['issues']})
    rows.sort(key=lambda r: (r['priority'], r['ready'], -r['negative'], -r['suspected'], r['code'], r['account']))
    account_rows = []
    for account in accounts:
        own = [r for r in rows if r['account_id'] == account.pk]
        account_rows.append({'id': account.pk, 'name': account.name, 'enabled': account.enabled,
            'sales_enabled': account.sales_enabled, 'history_enabled': account.history_enabled,
            'storage': storage.get(account.pk, {'count': 0, 'first': None, 'last': None, 'saved_at': None}),
            'sales': state_snapshot(sales_states.get(account.pk)),
            'reviews': state_snapshot(dataset.feedback_states.get(account.pk)),
            'reviews_polling': state_snapshot(current_reviews.get(account.pk)),
            'sales_history': state_snapshot(histories.get((account.pk, 'sales'))),
            'reviews_history': state_snapshot(histories.get((account.pk, 'reviews'))),
            'history_days': getattr(histories.get((account.pk, 'sales')), 'days', 0),
            'history_exhausted': bool(getattr(histories.get((account.pk, 'sales')), 'exhausted', False)),
            'uncertain_orders': order_counts[account.pk], 'order_issue_reasons': dict(order_issues[account.pk]),
            'unmapped_reviews': unmapped[account.pk], 'unmapped_recent_reviews': unmapped_recent[account.pk], 'pairs': len(own),
            'negative_pairs': sum(r['priority'] == 0 for r in own),
            'ready_pairs': sum(r['ready'] for r in own)})
    return {'format': 'control-data-1', 'generated_at': now, 'since': since, 'accounts': account_rows,
            'summary': {'catalogue_products': len(products), 'products_with_reviews': len({r['code'] for r in rows if r['reviews']}),
                'negative_products': len({r['code'] for r in rows if r['priority'] == 0}),
                'pairs': len(rows), 'ready_pairs': sum(r['ready'] for r in rows),
                'no_activity_pairs': sum(not r['activity'] for r in rows),
                'incomplete_active_pairs': sum(r['activity'] and not r['ready'] for r in rows),
                'negative_incomplete_pairs': sum(r['priority'] == 0 and not r['ready'] for r in rows),
                'unmapped_reviews': sum(unmapped.values()), 'unmapped_recent_reviews': sum(unmapped_recent.values())},
            'causes': [{'code': code, 'label': CAUSES[code][0], 'action': CAUSES[code][1], 'count': reasons[code]}
                       for code in CAUSES], 'items': rows}
