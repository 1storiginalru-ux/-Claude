"""Persist WB budgets across the polling, outgoing and reconciliation processes."""
import hashlib
import math
import time
from datetime import timedelta
from email.utils import parsedate_to_datetime
from django.db.models import F
from django.utils import timezone
from desk.models import ApiThrottle


def seconds(value):
    try:
        number = float(value)
        return number if math.isfinite(number) and number >= 0 else None
    except (ValueError, TypeError):
        return None


def retry_delay(headers, *, now=None, fallback=60):
    now = now or timezone.now()
    waits = []
    for name in ['Retry-After', 'X-Ratelimit-Retry', 'X-Ratelimit-Reset']:
        value = headers.get(name)
        number = seconds(value)
        if number is None and name == 'Retry-After' and isinstance(value, str):
            try:
                date = parsedate_to_datetime(value)
                if timezone.is_naive(date): date = timezone.make_aware(date)
                number = max(0, (date - now).total_seconds())
            except (ValueError, TypeError, OverflowError):
                pass
        if number is not None: waits.append(number)
    # A small margin avoids retrying precisely on the server's boundary.
    return math.ceil(max(waits)) + 1 if waits else fallback


def scope_for(token):
    return hashlib.sha256(('wb-feedbacks\0' + token).encode()).hexdigest()


def acquire(token):
    from .base import IntegrationError
    scope = scope_for(token)
    for _ in range(10):
        state, _ = ApiThrottle.objects.get_or_create(scope=scope)
        now = timezone.now()
        wait = (state.next_request_at - now).total_seconds()
        if wait > 2:
            raise IntegrationError(
                f'Пауза WB по лимиту запросов: ещё {math.ceil(wait)} сек.',
                retryable=True, retry_after=math.ceil(wait), rate_limited=True, request_sent=False,
            )
        if wait > 0:
            time.sleep(wait)
            continue
        # Compare-and-swap also works with SQLite, whose select_for_update is a no-op.
        if ApiThrottle.objects.filter(pk=state.pk, next_request_at=state.next_request_at).update(next_request_at=now + timedelta(seconds=1)):
            return
    raise IntegrationError('Канал WB занят другим запросом; следующая попытка отложена.', retryable=True, retry_after=2, rate_limited=True, request_sent=False)


def observe(token, response):
    state, _ = ApiThrottle.objects.get_or_create(scope=scope_for(token))
    now = timezone.now()
    if response.status_code in {420, 429}:
        wait = retry_delay(response.headers, now=now, fallback=min(3600, 60 * 2 ** min(state.strikes, 6)))
        ApiThrottle.objects.filter(pk=state.pk).update(strikes=F('strikes') + 1)
    elif 200 <= response.status_code < 300:
        ApiThrottle.objects.filter(pk=state.pk).update(strikes=0)
        if seconds(response.headers.get('X-Ratelimit-Remaining')) != 0:
            return None
        wait = retry_delay(response.headers, now=now)
    else:
        return None
    until = now + timedelta(seconds=wait)
    ApiThrottle.objects.filter(pk=state.pk, next_request_at__lt=until).update(next_request_at=until)
    return wait
