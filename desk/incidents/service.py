import os
import re
import requests
from django.db import transaction
from desk.models import Audit, Incident
from desk.voc.rules import evaluate
from desk.voc.rules import product_family, grouped_signals
from desk.voc.rules import is_negative, trusted_analysis
from desk.reviews import effective_analysis


@transaction.atomic
def escalate(thread, signal, actor=None, metric=None):
    if not thread.product_id: raise ValueError('Сначала сопоставьте SKU с ProductID.')
    analysis = effective_analysis(thread)
    severity = analysis.severity if analysis else 'P2'
    canonical = product_family(thread.product).order_by('pk').first()
    # Lock the family representative so parallel reviews cannot create two open incidents.
    from desk.models import Product
    Product.objects.select_for_update().get(pk=canonical.pk)
    incident, created = Incident.objects.get_or_create(product=canonical, signal=signal, status__in=['INVESTIGATION', 'CAPA', 'CONTROL'], defaults={'status': 'INVESTIGATION', 'severity': severity})
    linked = incident.threads.filter(pk=thread.pk).exists()
    if created:
        metric = metric or evaluate(thread.product, signal)
        period = metric['period']
        baseline = metric['baseline']
        incident.opening_snapshot = {
            'signal': signal, 'count': metric['count'], 'redemptions': metric['redemptions'], 'rate': metric['rate'],
            'reason': metric['reason'], 'complete': period['complete'], 'warnings': period['reasons'],
            'start': period['start_at'].isoformat() if period['start_at'] else None, 'until': period['until'].isoformat(),
            'company_ids': sorted(set(product_family(thread.product).values_list('company_id', flat=True))),
            'threshold': baseline.high if baseline else None,
            'cluster_count': metric.get('cluster_count', 0),
            'account_id': metric.get('account_id'),
            'account_name': thread.account.name if metric.get('account_id') == thread.account_id else None,
        }
        incident.save(update_fields=['opening_snapshot'])
    incident.threads.add(thread)
    if severity < incident.severity:
        incident.severity = severity
        incident.save(update_fields=['severity', 'updated_at'])
    if not linked:
        thread.status = 'Эскалировано'
        thread.save(update_fields=['status'])
        Audit.objects.create(company=thread.account.company, actor=actor, action='incident.created' if created else 'incident.linked', object_id=str(incident.pk), after={'thread': thread.pk, 'signal': signal})
    return incident


def evaluate_incidents(thread, actor=None):
    if not thread.product_id: return
    analysis = effective_analysis(thread)
    if not analysis: return
    signals = grouped_signals(analysis.signals)
    if is_negative(analysis): signals.append('NEGATIVE')
    for signal in signals:
        stats = evaluate(thread.product, signal)
        if signal == 'NEGATIVE' and stats['status'] != 'ANOMALY':
            scoped = evaluate(thread.product, signal, account_id=thread.account_id)
            if scoped['status'] == 'ANOMALY': stats = scoped
        if stats['status'] == 'ANOMALY':
            attach_metric_incident(thread.product, stats, actor)
        elif signal != 'NEGATIVE' and analysis.objective_issue and analysis.severity in {'P0', 'P1'}:
            escalate(thread, signal, actor, stats)
        elif is_negative(analysis) and trusted_analysis(analysis):
            canonical = product_family(thread.product).order_by('pk').first()
            if Incident.objects.filter(product=canonical, signal=signal).exclude(status='CLOSED').exists():
                escalate(thread, signal, actor, stats)


@transaction.atomic
def attach_metric_incident(product, metric, actor=None):
    selected = metric['selected']
    if not selected:
        return
    canonical = product_family(product).order_by('pk').first()
    from desk.models import Product
    Product.objects.select_for_update().get(pk=canonical.pk)
    cases = list(Incident.objects.select_for_update().filter(product=canonical, signal=metric['signal']))
    open_exists = any(case.status != 'CLOSED' for case in cases)
    if not open_exists:
        closed_ids = set(Incident.objects.filter(product=canonical, signal=metric['signal'], status='CLOSED').values_list('threads__pk', flat=True))
        if all(t.pk in closed_ids for t in selected):
            return  # Closing a case is deliberate; old evidence must not reopen it every minute.
    for thread in selected:
        escalate(thread, metric['signal'], actor, metric)


def recheck_products():
    """Re-evaluate after sales, coverage and time changes, not only after AI calls."""
    from datetime import timedelta
    from django.db.models import Q, Min
    from django.utils import timezone
    from desk.models import Company, Product
    from desk.voc.dataset import ControlDataset
    companies = Company.objects.filter(is_demo=False)
    candidates = Product.objects.filter(company__in=companies).filter(Q(thread__kind='review', thread__received_at__gte=timezone.now() - timedelta(days=7)) | Q(incident__status__in=['INVESTIGATION', 'CAPA', 'CONTROL']))
    ids = candidates.values('code').annotate(first=Min('pk')).values_list('first', flat=True)
    products = list(Product.objects.filter(pk__in=ids))
    if not products: return
    dataset = ControlDataset(products, companies)
    for product in products:
        from desk.voc.rules import PRODUCT_SIGNALS
        for signal in ['NEGATIVE', *PRODUCT_SIGNALS]:
            if (product.code, signal) not in dataset.baselines: continue
            metric = evaluate(product, signal, dataset=dataset)
            if metric['status'] == 'ANOMALY':
                attach_metric_incident(product, metric)
            if signal == 'NEGATIVE':
                for account_id in {m.account_id for m in dataset.mappings[product.code]}:
                    scoped = evaluate(product, signal, dataset=dataset, account_id=account_id)
                    if scoped['status'] == 'ANOMALY': attach_metric_incident(product, scoped)


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
