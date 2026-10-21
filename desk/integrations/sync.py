import hashlib
import uuid
from datetime import timedelta
from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from desk.models import Account, Message, Outgoing, ProductMapping, SyncLog, SyncState, Thread
from .base import IntegrationError, get_adapter


@transaction.atomic
def ingest(account, incoming):
    mapping = ProductMapping.objects.filter(account=account, sku=incoming.sku).first()
    received = incoming.created_at or timezone.now()
    thread, created = Thread.objects.get_or_create(account=account, kind=incoming.kind, external_id=incoming.external_id, defaults={'text': incoming.text, 'sku': incoming.sku, 'customer': incoming.customer[:120], 'rating': incoming.rating, 'received_at': received, 'due_at': received + timedelta(minutes=account.company.sla_minutes), 'product': mapping.product if mapping else None})
    changed = thread.text != incoming.text or thread.rating != incoming.rating
    if changed:
        thread.text, thread.rating = incoming.text, incoming.rating
        if thread.status == 'Закрыто':
            thread.status, thread.closed_at = 'Новое', None
        thread.save(update_fields=['text', 'rating', 'status', 'closed_at'])
    revision = hashlib.sha256(incoming.text.encode()).hexdigest()[:24]
    Message.objects.get_or_create(thread=thread, external_id='source-' + revision, direction='in', defaults={'text': incoming.text, 'created_at': received})
    for reply in incoming.replies: ingest_reply(thread, reply)
    return thread, created or changed


@transaction.atomic
def ingest_reply(thread, reply):
    revision = hashlib.sha256(reply['text'].encode()).hexdigest()[:16]
    _, created = Message.objects.get_or_create(thread=thread, external_id=(reply['external_id'] + '-' + revision)[:200], direction=reply['direction'], defaults={'text': reply['text']})
    if reply['direction'] == 'out' and reply['published']:
        Outgoing.objects.filter(thread=thread, external_id=reply['external_id'], text=reply['text'], status='Sent').update(status='Confirmed', confirmed_at=timezone.now(), error='')
    if created and reply['direction'] == 'in' and thread.status in {'Закрыто', 'Ожидаем клиента', 'Ответ отправлен'}:
        thread.status, thread.closed_at = 'Новое', None
        thread.due_at = timezone.now() + timedelta(minutes=thread.account.company.sla_minutes)
        thread.save(update_fields=['status', 'closed_at', 'due_at'])


def sync_account(account):
    state, _ = SyncState.objects.get_or_create(account=account)
    now = timezone.now()
    if state.next_attempt > now:
        return 0
    lease = uuid.uuid4()
    if not SyncState.objects.filter(pk=state.pk).filter(Q(lease_until__isnull=True) | Q(lease_until__lte=now)).update(lease_token=lease, lease_until=now + timedelta(minutes=3)):
        return 0
    state.refresh_from_db()
    adapter = get_adapter(account)
    count, analysis_errors = 0, 0
    heartbeat_at = now

    def checkpoint(cursor):
        if not SyncState.objects.filter(pk=state.pk, lease_token=lease).update(cursor=cursor, lease_until=timezone.now() + timedelta(minutes=3)):
            raise IntegrationError('Синхронизация передана другому процессу.')

    def heartbeat():
        nonlocal heartbeat_at
        current = timezone.now()
        if current - heartbeat_at >= timedelta(seconds=20):
            if not SyncState.objects.filter(pk=state.pk, lease_token=lease).update(lease_until=current + timedelta(minutes=3)):
                raise IntegrationError('Синхронизация передана другому процессу.')
            Account.objects.filter(pk=account.pk).update(sync_status=f'Синхронизация: получено {count}; ошибок AI: {analysis_errors}')
            heartbeat_at = current

    adapter.sync_cursor, adapter.checkpoint = state.cursor, checkpoint
    try:
        from desk.ai.provider import analyze, AIProblem
        # Confirm published outgoing replies before spending the budget on history.
        pending = Outgoing.objects.filter(thread__account=account, status='Sent', next_attempt__lte=now).select_related('thread__account__company').order_by('next_attempt')[:20]
        for outgoing in pending:
            for reply in adapter.replies(outgoing.thread): ingest_reply(outgoing.thread, reply)
            Outgoing.objects.filter(pk=outgoing.pk, status='Sent').update(next_attempt=timezone.now() + timedelta(seconds=60))
            heartbeat()
        for incoming in adapter.fetch():
            thread, changed = ingest(account, incoming)
            count += 1
            if changed or not hasattr(thread, 'analysis'):
                try: analyze(thread)
                except AIProblem: analysis_errors += 1
            heartbeat()
        detail = f'Получено: {count}; ошибок AI: {analysis_errors}'
        Account.objects.filter(pk=account.pk).update(last_sync=timezone.now(), sync_status=detail)
        SyncState.objects.filter(pk=state.pk, lease_token=lease).update(next_attempt=timezone.now() + timedelta(seconds=60))
        SyncLog.objects.create(account=account, success=True, detail=detail)
        return count
    except (IntegrationError, KeyError, ValueError, TypeError) as e:
        detail = str(e)[:240] if isinstance(e, IntegrationError) else 'Неожиданный формат данных API; требуется проверка схемы.'
        retry_at = timezone.now() + timedelta(seconds=max(1, e.retry_after or 60) if isinstance(e, IntegrationError) else 60)
        SyncState.objects.filter(pk=state.pk, lease_token=lease).update(next_attempt=retry_at)
        if isinstance(e, IntegrationError) and e.rate_limited:
            channel = 'WB' if account.platform == 'wb' else 'Яндекс Маркет'
            detail = f'Получено: {count}. Пауза {channel}: {detail} Не ранее {timezone.localtime(retry_at):%d.%m %H:%M:%S}.'[:240]
        Account.objects.filter(pk=account.pk).update(sync_status=detail)
        SyncLog.objects.create(account=account, success=False, detail=detail, retry_at=retry_at)
        if isinstance(e, IntegrationError): raise
        raise IntegrationError(detail) from e
    finally:
        SyncState.objects.filter(pk=state.pk, lease_token=lease).update(lease_token=None, lease_until=None)
