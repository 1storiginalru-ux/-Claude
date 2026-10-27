"""Use individual API events when configured; never add them to the same CSV sales."""
from datetime import datetime, time, timedelta
import math
from django.db.models import Q
from django.utils import timezone
from desk.models import ProductMapping, SaleEvent, SalesDaily, SalesSyncState, SyncState


def sales_window(family, window='100', now=None, account_id=None):
    now = now or timezone.now()
    mappings = ProductMapping.objects.filter(product__in=family)
    if account_id is not None:
        mappings = mappings.filter(account_id=account_id)
    pairs = list(mappings.select_related('account'))
    api_match, daily_match = Q(pk__in=[]), Q(pk__in=[])
    api_accounts, all_accounts, manual_accounts = set(), set(), set()
    for mapping in pairs:
        all_accounts.add(mapping.account_id)
        match = Q(account_id=mapping.account_id, sku=mapping.sku)
        if mapping.account.platform == 'wb' and mapping.account.sales_enabled:
            api_match |= match
            api_accounts.add(mapping.account_id)
        else:
            daily_match |= match
            manual_accounts.add(mapping.account_id)
    exact = bool(api_accounts) and not manual_accounts
    sales_states = {state.account_id: state for state in SalesSyncState.objects.filter(account_id__in=api_accounts)}
    from desk.integrations.coverage import feedback_coverage
    feedback_states = feedback_coverage(all_accounts)
    daily = list(SalesDaily.objects.filter(daily_match).values('account_id', 'date', 'redemptions'))
    events = list(SaleEvent.objects.filter(api_match, kind='sale').order_by('-occurred_at', '-pk').values('occurred_at', 'id'))
    return calculate_window(window, now, api_accounts, all_accounts, manual_accounts, sales_states, feedback_states, daily, events)


def calculate_window(window, now, api_accounts, all_accounts, manual_accounts, sales_states, feedback_states, daily, events):
    """Shared calculation for a single card and the bulk, globally sorted queue."""
    sales_states = {key: value for key, value in sales_states.items() if key in api_accounts}
    feedback_states = {key: value for key, value in feedback_states.items() if key in all_accounts}
    exact = bool(api_accounts) and not manual_accounts
    until = now
    if (exact and len(sales_states) == len(api_accounts) and len(feedback_states) == len(all_accounts)
            and all(state.coverage_until for state in [*sales_states.values(), *feedback_states.values()])):
        until = min([now, *[state.coverage_until for state in sales_states.values()], *[state.coverage_until for state in feedback_states.values()]])
    daily = [row for row in daily if row['date'] <= timezone.localdate(until)]
    events = [row for row in events if row['occurred_at'] <= until]
    target = None if window.endswith('d') else int(window)
    if target is None:
        start_day = timezone.localdate(until) - timedelta(days=int(window[:-1]) - 1)
        start_at = timezone.make_aware(datetime.combine(start_day, time.min))
        total = sum(row['occurred_at'] >= start_at for row in events) + sum(row['redemptions'] for row in daily if row['date'] >= start_day)
    elif exact:
        selected = [row['occurred_at'] for row in events[:target]]
        total, start_at = len(selected), (selected[-1] if selected else None)
        start_day = timezone.localdate(start_at) if start_at else None
    else:
        by_day = {}
        for row in daily:
            by_day[row['date']] = by_day.get(row['date'], 0) + row['redemptions']
        for occurred in (row['occurred_at'] for row in events):
            day = timezone.localdate(occurred)
            by_day[day] = by_day.get(day, 0) + 1
        total, start_day = 0, None
        for day in sorted(by_day, reverse=True):
            if not by_day[day]:
                continue
            total += by_day[day]
            start_day = day
            if total >= target:
                break
        start_at = timezone.make_aware(datetime.combine(start_day, time.min)) if start_day else None
    reasons = []
    if target and total < target:
        reasons.append(f'Накоплено {total} из {target} выкупов')
    manual_present = {row['account_id'] for row in daily}
    if manual_accounts - manual_present:
        reasons.append('Не для всех кабинетов загружены продажи')
    for account_id in api_accounts:
        state = sales_states.get(account_id)
        if (not state or not state.coverage_since or not state.coverage_until
                or not start_at or state.coverage_since > start_at or state.coverage_until < now - timedelta(hours=3)):
            reasons.append('Не подтверждена полнота продаж WB за окно')
            break
    sales_reasons = list(reasons)
    feedback_reasons = []
    for account_id in all_accounts:
        state = feedback_states.get(account_id)
        if (not state or not state.coverage_since or not state.coverage_until or not start_at
                or state.coverage_since > start_at or state.coverage_until < now - timedelta(hours=3)):
            feedback_reasons.append('Не подтверждена полнота отзывов за окно')
            break
    reasons.extend(feedback_reasons)
    span_days = max(1, math.ceil((until-start_at).total_seconds()/86400)) if start_at else None
    return {'redemptions': total, 'start': start_day, 'start_at': start_at, 'until': until, 'span_days': span_days, 'exact': exact,
            'target': target, 'reasons': list(dict.fromkeys(reasons)), 'complete': bool(all_accounts) and not reasons,
            'has_api': bool(api_accounts), 'accounts': all_accounts,
            'sales_reasons': sales_reasons, 'feedback_reasons': feedback_reasons}
