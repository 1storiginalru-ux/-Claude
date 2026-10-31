"""Track a contiguous interval actually completed by the API, not inferred from rows."""
from datetime import datetime
from django.db import transaction
from types import SimpleNamespace


@transaction.atomic
def record_coverage(state, since, until):
    if not isinstance(since, datetime) or not isinstance(until, datetime) or since > until:
        return
    current = type(state).objects.select_for_update().get(pk=state.pk)
    if current.lease_token != state.lease_token: return
    from desk.models import SalesSyncState, Audit
    if isinstance(current, SalesSyncState):
        Audit.objects.create(company_id=current.account.company_id, action='sales.coverage', object_id=str(current.account_id),
            before={'since': current.coverage_since.isoformat() if current.coverage_since else None,
                    'until': current.coverage_until.isoformat() if current.coverage_until else None},
            after={'since': since.isoformat(), 'until': until.isoformat(), 'basis': 'completed_api_interval'})
    previous_since, previous_until = current.coverage_since, current.coverage_until
    if previous_since and previous_until:
        if until < previous_since:
            return
        if since <= previous_until:
            since, until = min(since, previous_since), max(until, previous_until)
    type(state).objects.filter(pk=state.pk, lease_token=state.lease_token).update(coverage_since=since, coverage_until=until)
    state.coverage_since, state.coverage_until = since, until


def feedback_coverage(account_ids):
    """WB's active lists alone do not certify its separate archived reviews."""
    from desk.models import Account, HistorySyncState, SyncState
    states = {s.account_id: s for s in SyncState.objects.filter(account_id__in=account_ids)}
    historical = {s.account_id: s for s in HistorySyncState.objects.filter(account_id__in=account_ids, kind='reviews')}
    required = Account.objects.filter(pk__in=account_ids).values('pk','platform','sales_enabled')
    for account in required:
        pk = account['pk']
        fresh, archive = states.get(pk), historical.get(pk)
        if account['platform']=='wb' and (account['sales_enabled'] or archive):
            # Only the three-stream scan includes archived WB reviews.
            states[pk] = archive or SimpleNamespace(account_id=pk,coverage_since=None,coverage_until=None)
        elif archive and archive.coverage_since and archive.coverage_until:
            if fresh and fresh.coverage_since and fresh.coverage_until and archive.coverage_since<=fresh.coverage_until and fresh.coverage_since<=archive.coverage_until:
                states[pk] = SimpleNamespace(account_id=pk,coverage_since=min(fresh.coverage_since,archive.coverage_since),coverage_until=max(fresh.coverage_until,archive.coverage_until))
            elif not fresh or not fresh.coverage_until:
                states[pk] = archive
    return states
