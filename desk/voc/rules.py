from datetime import timedelta
from django.db.models import Q
from django.utils import timezone
from desk.models import Baseline, SalesDaily, Thread

SIGNALS = {'AUTH', 'AUTH-SCENT', 'AUTH-LONGEVITY', 'AUTH-MARKING', 'AUTH-PACK', 'AUTH-UNKNOWN', 'LONG', 'SCENT', 'PACK', 'DEFECT', 'MARK', 'DELIVERY', 'SERVICE', 'RETURN', 'CONTENT', 'PRICE', 'OTHER'}


def matches(signals, signal):
    return any(x == signal or (signal == 'AUTH' and x.startswith('AUTH-')) for x in signals)


def human_only(result):
    return result['severity'] == 'P0' or any(s.startswith('AUTH') or s in {'RETURN', 'MARK'} or (s == 'DEFECT' and result['severity'] in {'P0', 'P1'}) for s in result['signals'])


def product_family(product, scope=None):
    from desk.models import Product
    products = Product.objects.filter(code=product.code)
    if scope is not None: products = products.filter(company__in=scope)
    return products


def evaluate(product, signal, window='100', now=None, scope=None):
    now = now or timezone.now()
    from desk.models import ProductMapping
    family = product_family(product, scope)
    mappings = list(ProductMapping.objects.filter(product__in=family).values_list('account_id', 'sku'))
    match = Q(pk__in=[])
    for account_id, sku in mappings: match |= Q(account_id=account_id, sku=sku)
    sales = SalesDaily.objects.filter(match, date__lte=now.date()).order_by('-date')
    if window.endswith('d'):
        days = int(window[:-1])
        start = now.date() - timedelta(days=days - 1)
        sales = sales.filter(date__gte=start)
        total = sum(sales.values_list('redemptions', flat=True))
    else:
        target = int(window)
        total, start = 0, None
        by_day = {}
        for row in sales:
            by_day[row.date] = by_day.get(row.date, 0) + row.redemptions
        for day, amount in sorted(by_day.items(), reverse=True):
            if amount == 0: continue
            total += amount
            start = day
            if total >= target: break
    threads = Thread.objects.filter(product__in=family, received_at__lte=now).select_related('analysis')
    if start: threads = threads.filter(received_at__date__gte=start)
    else: threads = threads.none()
    selected = [t for t in threads if hasattr(t, 'analysis') and matches(t.analysis.signals, signal)]
    count = len(selected)
    rate = round(count / total * 100, 2) if total else None
    baseline = Baseline.objects.filter(product__code=product.code, signal=signal).order_by('product_id').first()
    status, reason = 'WATCH', 'Baseline не настроен'
    cluster = False
    if baseline:
        cluster = sum(t.received_at >= now - timedelta(days=baseline.cluster_days) for t in selected) >= baseline.cluster_count
        if total < baseline.min_redemptions:
            reason = 'Недостаточная выборка'
        elif rate is not None and rate > baseline.high and count >= baseline.min_signals:
            status, reason = 'ANOMALY', 'Частота выше baseline'
        elif rate is not None and rate > baseline.high:
            reason = 'Недостаточно сигналов для аномалии'
        else:
            status, reason = 'NORMAL', 'В пределах baseline'
        if cluster: status, reason = 'ANOMALY', 'Кластер похожих сигналов'
    return {'signal': signal, 'count': count, 'redemptions': total, 'rate': rate, 'start': start, 'status': status, 'reason': reason, 'baseline': baseline, 'cluster': cluster}
