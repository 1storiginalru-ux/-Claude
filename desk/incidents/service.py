import os
import re
import requests
from django.db import transaction
from desk.models import Audit, Incident
from desk.voc.rules import evaluate
from desk.voc.rules import product_family, grouped_signals


@transaction.atomic
def escalate(thread, signal, actor=None):
    if not thread.product_id: raise ValueError('Сначала сопоставьте SKU с ProductID.')
    severity = thread.analysis.severity if hasattr(thread, 'analysis') else 'P2'
    canonical = product_family(thread.product).order_by('pk').first()
    incident, created = Incident.objects.get_or_create(product=canonical, signal=signal, status__in=['INVESTIGATION', 'CAPA', 'CONTROL'], defaults={'status': 'INVESTIGATION', 'severity': severity})
    incident.threads.add(thread)
    if severity < incident.severity:
        incident.severity = severity
        incident.save(update_fields=['severity', 'updated_at'])
    thread.status = 'Эскалировано'
    thread.save(update_fields=['status'])
    Audit.objects.create(company=thread.account.company, actor=actor, action='incident.created' if created else 'incident.linked', object_id=str(incident.pk), after={'thread': thread.pk, 'signal': signal})
    return incident


def evaluate_incidents(thread, actor=None):
    if not thread.product_id: return
    analysis = thread.analysis
    for signal in grouped_signals(analysis.signals):
        stats = evaluate(thread.product, signal)
        if stats['status'] == 'ANOMALY' or (analysis.objective_issue and analysis.severity in {'P0', 'P1'}):
            escalate(thread, signal, actor)


def sync_bitrix(incident):
    domain = os.environ.get('VOC_BITRIX_DOMAIN', '')
    key = os.environ.get('VOC_BITRIX_TOKEN', '')
    owner = os.environ.get('VOC_BITRIX_OWNER_ID', '')
    namespace = os.environ.get('VOC_BITRIX_NAMESPACE', 'voice')
    if not re.fullmatch(r'[a-zA-Z0-9-]+\.bitrix24\.(ru|com|by|kz)', domain) or not key or not owner.isdigit():
        raise ValueError('Настройте VOC_BITRIX_DOMAIN, VOC_BITRIX_TOKEN и VOC_BITRIX_OWNER_ID в окружении.')
    metric = evaluate(incident.product, incident.signal)
    description = f'Incident #{incident.pk}\nProductID: {incident.product.code}\nСигнал: {incident.signal}\nSeverity: {incident.severity}\nСтатус: {incident.status}\nСигналов: {incident.threads.count()}\nCurrent rate: {metric["rate"]} / 100\nВыкупов в окне: {metric["redemptions"]}\nГипотеза: {incident.hypothesis}\nCAPA: {incident.actions}\nРезультат: {incident.result}'
    marker = f'{namespace}:company:{incident.product.company_id}:incident:{incident.pk}'
    fields = {'TITLE': f'[VoC #{incident.pk}] {incident.product.code} · {incident.signal}', 'DESCRIPTION': description, 'RESPONSIBLE_ID': int(owner), 'XML_ID': marker, 'STATUS': 5 if incident.status == 'CLOSED' else 2}
    try:
        if not incident.bitrix_id:
            lookup = requests.post(f'https://{domain}/rest/tasks.task.list.json', json={'auth': key, 'filter': {'=XML_ID': marker}, 'select': ['ID', 'XML_ID']}, timeout=(5, 15), allow_redirects=False)
            if lookup.status_code != 200: raise ValueError(f'Проверка Bitrix24: HTTP {lookup.status_code}.')
            lookup_data = lookup.json()
            if lookup_data.get('error'): raise ValueError('Не удалось проверить наличие задачи в Bitrix24.')
            tasks = lookup_data.get('result', {}).get('tasks')
            if not isinstance(tasks, list): raise ValueError('Некорректный ответ проверки Bitrix24.')
            if tasks: incident.bitrix_id = str(tasks[0]['id'])
            elif incident.bitrix_status.startswith('Неопределённый'):
                raise ValueError('Предыдущий запрос имеет неопределённый результат. Нужна ручная сверка Bitrix24; автоматическое создание заблокировано.')
        method = 'tasks.task.update' if incident.bitrix_id else 'tasks.task.add'
        body = {'auth': key, 'fields': fields}
        if incident.bitrix_id: body['taskId'] = incident.bitrix_id
        else:
            incident.bitrix_status = 'Неопределённый результат: создание начато; требуется проверка до повторной попытки.'
            incident.save(update_fields=['bitrix_status'])
        response = requests.post(f'https://{domain}/rest/{method}.json', json=body, timeout=(5, 15), allow_redirects=False)
        if response.status_code != 200:
            if 400 <= response.status_code < 500:
                incident.bitrix_status = f'Запрос отклонён: HTTP {response.status_code}.'
                incident.save(update_fields=['bitrix_status'])
            raise ValueError(f'Bitrix24: HTTP {response.status_code}.')
        result = response.json()
        if result.get('error'): raise ValueError('Bitrix24 отклонил запрос; проверьте scope tasks и срок OAuth-токена.')
        if not incident.bitrix_id: incident.bitrix_id = str(result['result']['task']['id'])
    except requests.RequestException as e:
        # Creation is not idempotent in Bitrix: a timeout cannot safely be retried automatically.
        incident.bitrix_status = 'Неопределённый результат: проверьте Bitrix24 перед повтором.'
        incident.save(update_fields=['bitrix_status'])
        raise ValueError(incident.bitrix_status) from e
    except (KeyError, TypeError) as e:
        incident.bitrix_status = 'Неопределённый результат: проверьте наличие задачи в Bitrix24.'
        incident.save(update_fields=['bitrix_status'])
        raise ValueError('Неожиданный ответ Bitrix24: проверьте наличие задачи перед повтором.') from e
    incident.bitrix_status = 'Синхронизировано'
    incident.save(update_fields=['bitrix_id', 'bitrix_status', 'updated_at'])
