import math
from datetime import timedelta
from django.db.models import Q, Sum
from django.utils import timezone
from desk.models import Audit, Baseline, SalesDaily, Thread
from .rules import evaluate, matches, product_family


def refresh_baselines(product, actor=None, days=90, minimum=100):
    product = product_family(product).order_by('pk').first()
    # Exclude the entire current rolling window so an incident cannot raise its own baseline.
    start = evaluate(product, 'LONG')['start']
    if not start: return 0
    earliest = start - timedelta(days=days)
    query = Q(pk__in=[])
    from desk.models import ProductMapping
    for account_id, sku in ProductMapping.objects.filter(product__in=product_family(product)).values_list('account_id', 'sku'):
        query |= Q(account_id=account_id, sku=sku)
    count = SalesDaily.objects.filter(query, date__gte=earliest, date__lt=start).aggregate(total=Sum('redemptions'))['total'] or 0
    if count < minimum: return 0
    analyses = list(Thread.objects.filter(product__in=product_family(product), received_at__date__gte=earliest, received_at__date__lt=start, analysis__isnull=False).values_list('analysis__signals', flat=True))
    changed = 0
    for signal in ['AUTH', 'LONG', 'SCENT', 'PACK', 'DEFECT', 'MARK', 'RETURN']:
        baseline = Baseline.objects.filter(product=product, signal=signal).first()
        if baseline and baseline.source == 'manual': continue
        n = sum(matches(values, signal) for values in analyses)
        low = max(0, n - 1.96 * math.sqrt(n)) * 100 / count
        high = (n + 1.96 * math.sqrt(n)) * 100 / count if n else 3 * 100 / count
        values = {'low': round(low, 3), 'high': round(high, 3), 'source': 'historical', 'sample_redemptions': count}
        baseline, _ = Baseline.objects.update_or_create(product=product, signal=signal, defaults=values)
        Audit.objects.create(company=product.company, actor=actor, action='baseline.calculate', object_id=str(baseline.pk), after={**values, 'signal': signal, 'start': str(earliest), 'end_exclusive': str(start)})
        changed += 1
    return changed
