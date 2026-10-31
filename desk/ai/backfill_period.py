"""Calendar-month bounds in the service timezone, enforced at enqueue and execution."""
from calendar import monthrange
from django.db.models import Q
from django.utils import timezone
from desk.models import AIJob

OUTSIDE_PERIOD = 'Отзыв вне выбранного периода авторазбора.'


def period_start(months=6, now=None):
    local = timezone.localtime(now or timezone.now(), timezone.get_default_timezone())
    number = local.year * 12 + local.month - 1 - months
    year, month = divmod(number, 12)
    month += 1
    return local.replace(year=year, month=month, day=min(local.day, monthrange(year, month)[1]),
                         hour=0, minute=0, second=0, microsecond=0)


def in_period(received_at, state, now=None):
    now = now or timezone.now()
    return bool(state and state.enabled and period_start(state.months, now) <= received_at <= now)


def outside_jobs(state, now=None):
    now = now or timezone.now()
    return AIJob.objects.filter(thread__account_id=state.account_id, snapshot__backfill=True,
        status__in=['queued', 'retry', 'blocked', 'running']).filter(
            Q(thread__received_at__lt=period_start(state.months, now)) | Q(thread__received_at__gt=now)
        )


def cancel_outside_period(state, now=None):
    return outside_jobs(state, now).update(status='cancelled', error=OUTSIDE_PERIOD, lease_token=None, lease_until=None)
