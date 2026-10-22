"""Bounded enrichment of existing records, sharing the normal API budget."""
from datetime import timedelta
from django.utils import timezone
from desk.models import Thread
from .base import IntegrationError
from .sync import ingest, ingest_reply


def enrich_account(account, adapter, *, heartbeat=lambda: None, on_change=lambda thread, changed: None, limit=10):
    now = timezone.now()
    candidates = list(Thread.objects.filter(account=account, details_next_attempt__lte=now).order_by('details_next_attempt', 'pk')[:limit])
    completed = 0
    for thread in candidates:
        thread.account = account
        try:
            # List polling refreshes recent source data. Older records and WB's
            # single-answer history are retrieved explicitly by ID as needed.
            if account.platform == 'wb' or thread.source_synced_at is None or (not thread.history_cursor and thread.source_synced_at < now - timedelta(hours=1)):
                incoming = adapter.fetch_one(thread)
                if incoming.external_id != thread.external_id or incoming.kind != thread.kind:
                    raise IntegrationError('API вернул другое обращение при запросе по ID.')
                thread, changed = ingest(account, incoming)
                thread.account = account
                on_change(thread, changed)
            if account.platform == 'ym':
                def checkpoint(cursor):
                    Thread.objects.filter(pk=thread.pk).update(history_cursor=cursor)
                    heartbeat()
                for reply in adapter.history(thread, cursor=thread.history_cursor, checkpoint=checkpoint):
                    ingest_reply(thread, reply, allow_reopen=thread.history_synced_at is not None)
                    heartbeat()
            checked = timezone.now()
            interval = timedelta(minutes=15) if thread.status != 'Закрыто' and thread.received_at > checked - timedelta(days=7) else timedelta(days=1)
            Thread.objects.filter(pk=thread.pk).update(history_synced_at=checked, details_next_attempt=checked + interval, details_error='', history_cursor={})
            completed += 1
        except IntegrationError as error:
            delay = max(1, error.retry_after or (60 if error.retryable else 86400))
            Thread.objects.filter(pk=thread.pk).update(details_next_attempt=timezone.now() + timedelta(seconds=delay), details_error=str(error)[:240])
            if error.retryable or error.rate_limited: raise
        heartbeat()
    return completed
