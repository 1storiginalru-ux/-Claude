import math
from datetime import timedelta
from django.db.models import Q, Sum
from django.utils import timezone
from desk.models import Audit, Baseline, SalesDaily, SaleEvent, SalesSyncState, SyncState, Thread
from .rules import evaluate, matches, product_family, MIN_CLASSIFICATION_CONFIDENCE


def refresh_baselines(product, actor=None, days=90, minimum=100):
    product = product_family(product).order_by('pk').first()
    # Exclude the entire current rolling window so an incident cannot raise its own baseline.
    start = evaluate(product, 'LONG')['start']
    if not start: return 0
    earliest = start - timedelta(days=days)
    query, api_query = Q(pk__in=[]), Q(pk__in=[])
    from desk.models import ProductMapping
    mappings = list(ProductMapping.objects.filter(product__in=product_family(product)).select_related('account'))
    api_accounts, all_accounts = set(), {mapping.account_id for mapping in mappings}
    for mapping in mappings:
        match = Q(account_id=mapping.account_id, sku=mapping.sku)
        if mapping.account.platform == 'wb' and mapping.account.sales_enabled:
            api_query |= match
            api_accounts.add(mapping.account_id)
        else:
            query |= match
    if api_accounts:
        coverages = list(SalesSyncState.objects.filter(account_id__in=api_accounts).values_list('coverage_since', 'coverage_until'))
        feedback = list(SyncState.objects.filter(account_id__in=all_accounts).values_list('coverage_since', 'coverage_until'))
        if len(coverages) != len(api_accounts) or len(feedback) != len(all_accounts) or any(not low or not high for low, high in coverages + feedback):
            return 0
        from django.utils import timezone
        earliest = max(earliest, max(timezone.localdate(low) + timedelta(days=1) for low, _ in coverages + feedback))
        if earliest >= start or any(timezone.localdate(high) < start for _, high in coverages + feedback):
            return 0
        known_manual = set(SalesDaily.objects.filter(query, date__gte=earliest, date__lt=start).values_list('account_id', flat=True))
        if all_accounts - api_accounts - known_manual:
            return 0
    count = ((SalesDaily.objects.filter(query, date__gte=earliest, date__lt=start).aggregate(total=Sum('redemptions'))['total'] or 0)
             + SaleEvent.objects.filter(api_query, kind='sale', occurred_at__date__gte=earliest, occurred_at__date__lt=start).count())
    if count < minimum: return 0
    history = Thread.objects.filter(product__in=product_family(product), kind='review', received_at__date__gte=earliest, received_at__date__lt=start)
    if history.filter(Q(analysis__isnull=True) | Q(analysis__provider__in=['rules', 'demo']) | Q(analysis__confidence__lt=MIN_CLASSIFICATION_CONFIDENCE)).exists():
        return 0
    analyses = list(history.values_list('analysis__signals', flat=True))
    changed = 0
    for signal in ['AUTH', 'LONG', 'SCENT', 'PACK', 'DEFECT', 'MARK', 'RETURN', 'LONG_AUTH']:
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
