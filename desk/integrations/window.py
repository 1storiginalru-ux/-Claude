"""Shared rolling window for automatic marketplace retrieval."""
from datetime import datetime, timedelta
from django.utils import timezone
from django.utils.dateparse import parse_datetime

LOOKBACK_DAYS = 7
SALES_OVERLAP_HOURS = 6


def sync_window(now=None):
    until = now or timezone.now()
    return until - timedelta(days=LOOKBACK_DAYS), until


def sales_window_start(state, now):
    """Refresh changes with overlap; historical gaps remain the archive lane's job."""
    lower = now - timedelta(days=LOOKBACK_DAYS)
    if state.coverage_until and state.coverage_until <= now:
        lower = max(lower, state.coverage_until - timedelta(hours=SALES_OVERLAP_HOURS))
    return lower


def within_window(value, since=None, until=None):
    if since is None or until is None:
        since, until = sync_window()
    try:
        parsed = parse_datetime(value) if isinstance(value, str) else value
        if not isinstance(parsed, datetime): return False
        if timezone.is_naive(parsed): parsed = timezone.make_aware(parsed)
        return since <= parsed <= until
    except (ValueError, TypeError, OverflowError):
        return False
