import uuid
import re
from datetime import timedelta
from django.db import transaction
from django.utils import timezone
from desk.integrations.base import IntegrationError, get_adapter
from desk.models import Audit, Outgoing, Thread

MAX_ATTEMPTS = 5


def require_completed_template(text):
    if re.search(r'\{\{[^{}]{1,100}\}\}|\[(?:имя[ _]сотрудника|имя оператора|ФИО)\]', text, re.IGNORECASE):
        raise ValueError('Замените заполнитель подписи или шаблона реальными данными перед отправкой.')


@transaction.atomic
def enqueue(thread, actor, text, request_key):
    text = text.strip()
    if not 1 <= len(text) <= 5000: raise ValueError('Ответ должен содержать от 1 до 5000 символов.')
    require_completed_template(text)
    if thread.account.platform == 'ozon' and not 2 <= len(text) <= 3000:
        raise ValueError('Ответ Ozon должен содержать от 2 до 3000 символов.')
    try: request_key = uuid.UUID(str(request_key))
    except (ValueError, TypeError, AttributeError) as e: raise ValueError('Некорректный ключ отправки.') from e
    previous = Outgoing.objects.filter(request_key=request_key).first()
    if previous:
        if previous.thread_id != thread.pk or previous.sender_id != actor.pk:
            raise ValueError('Этот ключ относится к другому ответу.')
        return previous
    if not thread.account.enabled or thread.account.company.is_demo: raise ValueError('Кабинет выключен или демонстрационный. Подключите реальную интеграцию перед отправкой.')
    if Outgoing.objects.filter(thread=thread, status__in=['Queued', 'Sending', 'Sent']).exists():
        raise ValueError('Предыдущий ответ ещё обрабатывается или ожидает подтверждения публикации.')
    if Outgoing.objects.filter(thread=thread, text=text, status='Confirmed').exists():
        raise ValueError('Такой ответ уже опубликован.')
    analysis = getattr(thread, 'analysis', None)
    item = Outgoing.objects.create(thread=thread, sender=actor, text=text, request_key=request_key, ai_draft=analysis.draft if analysis else '')
    Thread.objects.filter(pk=thread.pk).update(status='Ответ готов')
    Audit.objects.create(company=thread.account.company, actor=actor, action='outgoing.queued', object_id=str(item.pk), after={'text': text, 'ai_draft': item.ai_draft, 'sent_by': 'OPERATOR'})
    return item


def process_one():
    now = timezone.now()
    # A terminated worker may have sent the request. Never automatically resend it.
    stuck = Outgoing.objects.filter(status='Sending', updated_at__lt=now - timedelta(minutes=5))
    stuck.update(status='Failed', retry_safe=False, error='Worker прерван: проверьте публикацию в площадке перед повтором.')
    candidate = Outgoing.objects.filter(status='Queued', next_attempt__lte=now).order_by('created_at').first()
    if not candidate: return False
    claimed = Outgoing.objects.filter(pk=candidate.pk, status='Queued').update(status='Sending', attempts=candidate.attempts + 1, updated_at=now)
    if not claimed: return False
    item = Outgoing.objects.select_related('thread__account__company').get(pk=candidate.pk)
    try:
        try: require_completed_template(item.text)
        except ValueError as error:
            raise IntegrationError(str(error), retryable=False, request_sent=False) from error
        external_id = get_adapter(item.thread.account).send(item.thread, item.text)
    except IntegrationError as e:
        if not e.request_sent:
            item.attempts = max(0, item.attempts - 1)
        retry = e.retryable and not e.ambiguous and (e.rate_limited or item.attempts < MAX_ATTEMPTS)
        item.status = 'Queued' if retry else 'Failed'
        item.error, item.retry_safe = str(e)[:240], not e.ambiguous
        item.next_attempt = timezone.now() + timedelta(seconds=max(e.retry_after, min(3600, 30 * 2 ** max(0, min(item.attempts - 1, 7)))))
        item.save(update_fields=['status', 'attempts', 'error', 'retry_safe', 'next_attempt', 'updated_at'])
        if not retry: Thread.objects.filter(pk=item.thread_id).update(status='Ошибка отправки')
    except Exception:
        item.status, item.retry_safe = 'Failed', False
        item.error = 'Непредвиденная ошибка после начала отправки: результат требует сверки.'
        item.save(update_fields=['status', 'retry_safe', 'error', 'updated_at'])
        Thread.objects.filter(pk=item.thread_id).update(status='Ошибка отправки')
    else:
        with transaction.atomic():
            item.status, item.external_id, item.sent_at, item.error = 'Sent', external_id, timezone.now(), ''
            item.save(update_fields=['status', 'external_id', 'sent_at', 'error', 'updated_at'])
            Thread.objects.filter(pk=item.thread_id).update(status='Ответ отправлен')
    Audit.objects.create(company=item.thread.account.company, actor=item.sender, action='outgoing.' + item.status.lower(), object_id=str(item.pk), after={'status': item.status, 'attempts': item.attempts, 'error': item.error})
    return True


def retry(item, actor):
    if item.status != 'Failed' or not item.retry_safe:
        raise ValueError('Повтор недоступен. При неопределённом результате сначала нужна сверка с площадкой.')
    if Outgoing.objects.filter(thread=item.thread, status__in=['Queued', 'Sending', 'Sent']).exclude(pk=item.pk).exists():
        raise ValueError('В диалоге уже есть обрабатываемый ответ.')
    Outgoing.objects.filter(pk=item.pk, status='Failed', retry_safe=True).update(status='Queued', next_attempt=timezone.now(), attempts=0, error='')
    Audit.objects.create(company=item.thread.account.company, actor=actor, action='outgoing.retry', object_id=str(item.pk))


def reconcile(item, actor, verified_absent=False):
    if item.status != 'Failed' or item.retry_safe:
        raise ValueError('Сверка нужна только для неопределённого результата отправки.')
    replies = get_adapter(item.thread.account).replies(item.thread)
    matching = next((r for r in replies if r['direction'] == 'out' and r['text'] == item.text), None)
    if matching:
        item.external_id = matching['external_id']
        item.status = 'Confirmed' if matching['published'] else 'Sent'
        item.sent_at = item.sent_at or timezone.now()
        item.confirmed_at = timezone.now() if matching['published'] else None
        item.error = ''
        item.save()
        Thread.objects.filter(pk=item.thread_id).update(status='Ответ отправлен')
    elif verified_absent:
        item.retry_safe, item.error = True, 'Руководитель подтвердил отсутствие ответа после сверки с площадкой.'
        item.save(update_fields=['retry_safe', 'error', 'updated_at'])
    else:
        raise ValueError('Ответ не найден через API. Проверьте кабинет и подтвердите отсутствие ответа перед повтором.')
    Audit.objects.create(company=item.thread.account.company, actor=actor, action='outgoing.reconcile', object_id=str(item.pk), after={'status': item.status, 'retry_safe': item.retry_safe, 'verified_absent': verified_absent})
