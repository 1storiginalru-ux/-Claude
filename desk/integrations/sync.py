import hashlib
from datetime import timedelta
from django.db import transaction
from django.utils import timezone
from desk.models import Account, Message, Outgoing, ProductMapping, SyncLog, Thread
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
    adapter = get_adapter(account)
    count, analysis_errors = 0, 0
    try:
        from desk.ai.provider import analyze, AIProblem
        for incoming in adapter.fetch():
            thread, changed = ingest(account, incoming)
            count += 1
            if changed or not hasattr(thread, 'analysis'):
                try: analyze(thread)
                except AIProblem: analysis_errors += 1
        for thread in Thread.objects.filter(account=account, outgoing__status='Sent').distinct():
            for reply in adapter.replies(thread): ingest_reply(thread, reply)
        detail = f'Получено: {count}; ошибок AI: {analysis_errors}'
        Account.objects.filter(pk=account.pk).update(last_sync=timezone.now(), sync_status=detail)
        SyncLog.objects.create(account=account, success=True, detail=detail)
        return count
    except (IntegrationError, KeyError, ValueError, TypeError) as e:
        detail = str(e)[:240] if isinstance(e, IntegrationError) else 'Неожиданный формат данных API; требуется проверка схемы.'
        Account.objects.filter(pk=account.pk).update(sync_status=detail)
        SyncLog.objects.create(account=account, success=False, detail=detail)
        if isinstance(e, IntegrationError): raise
        raise IntegrationError(detail) from e
