"""Shared rolling window for automatic marketplace retrieval."""
from datetime import datetime, timedelta
from django.utils import timezone
from django.utils.dateparse import parse_datetime

LOOKBACK_DAYS = 7


def sync_window(now=None):
    until = now or timezone.now()
    return until - timedelta(days=LOOKBACK_DAYS), until


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
