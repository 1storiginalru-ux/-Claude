"""Track a contiguous interval actually completed by the API, not inferred from rows."""
from datetime import datetime


def record_coverage(state, since, until):
    if not isinstance(since, datetime) or not isinstance(until, datetime) or since > until:
        return
    previous_since, previous_until = state.coverage_since, state.coverage_until
    if previous_since and previous_until:
        if until < previous_since:
            return
        if since <= previous_until:
            since, until = min(since, previous_since), max(until, previous_until)
    type(state).objects.filter(pk=state.pk, lease_token=state.lease_token).update(coverage_since=since, coverage_until=until)
    state.coverage_since, state.coverage_until = since, until
