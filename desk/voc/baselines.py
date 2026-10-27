import math
from datetime import timedelta
from django.db.models import Q, Sum
from django.utils import timezone
from desk.models import Audit, Baseline, SalesDaily, SaleEvent, SalesSyncState, SyncState, Thread
from .rules import evaluate, matches, product_family, MIN_CLASSIFICATION_CONFIDENCE
from .rules import is_negative, trusted_analysis
from desk.reviews import effective_analysis


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
        if mapping.account.platform in {'wb','ym'} and mapping.account.sales_enabled:
            api_query |= match
            api_accounts.add(mapping.account_id)
        else:
            query |= match
    if api_accounts:
        coverages = list(SalesSyncState.objects.filter(account_id__in=api_accounts).values_list('coverage_since', 'coverage_until'))
        from desk.integrations.coverage import feedback_coverage
        feedback = [(s.coverage_since, s.coverage_until) for s in feedback_coverage(all_accounts).values()]
        if len(coverages) != len(api_accounts) or len(feedback) != len(all_accounts) or any(not low or not high for low, high in coverages + feedback):
            return 0
        from django.utils import timezone
        earliest = max(earliest, max(timezone.localdate(low) + timedelta(days=1) for low, _ in coverages + feedback))
        if earliest >= start or any(timezone.localdate(high) < start for _, high in coverages + feedback):
            return 0
        known_manual = set(SalesDaily.objects.filter(query, date__gte=earliest, date__lt=start).values_list('account_id', flat=True))
        if all_accounts - api_accounts - known_manual:
            return 0
        from .uncertainty import sale_issues,overlaps
        from datetime import datetime,time
        pairs={(m.account_id,m.sku) for m in mappings}
        low=timezone.make_aware(datetime.combine(earliest,time.min));high=timezone.make_aware(datetime.combine(start,time.min))
        if any(overlaps(i,low,high) for i in sale_issues(api_accounts) if (i['account_id'],i['sku']) in pairs): return 0
    count = ((SalesDaily.objects.filter(query, date__gte=earliest, date__lt=start).aggregate(total=Sum('redemptions'))['total'] or 0)
             + SaleEvent.objects.filter(api_query, kind='sale', occurred_at__date__gte=earliest, occurred_at__date__lt=start).count())
    if count < minimum: return 0
    history = Thread.objects.filter(product__in=product_family(product), kind='review', received_at__date__gte=earliest, received_at__date__lt=start)
    analyses = [effective_analysis(t) for t in history.select_related('analysis', 'assessment')]
    if any(not trusted_analysis(a) for a in analyses):
        return 0
    analyses = [a.signals if is_negative(a) else [] for a in analyses]
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
