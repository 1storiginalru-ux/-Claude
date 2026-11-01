"""Validated rule persistence and a read-only simulation of the starter profile."""
from django.db import transaction
from django.utils import timezone
from desk.models import Account, Audit, Baseline, Company, ControlDefaults, ControlTopicRule, Incident, Product
from .defaults import FIELDS, TOPIC_FIELDS
from .topics import CONTROL_SIGNALS, PROFILE_VERSION, starter_profile


def rule_model(signal):
    if signal == 'NEGATIVE': return ControlDefaults
    if signal not in CONTROL_SIGNALS: raise ValueError('Неизвестная тема контроля.')
    return ControlTopicRule


def rule_values(signal, values):
    return {key: values[key] for key in (FIELDS if signal == 'NEGATIVE' else TOPIC_FIELDS)}


def validate_rule(account, signal, values):
    model = rule_model(signal)
    row = model(account=account, **rule_values(signal, values), **({} if signal == 'NEGATIVE' else {'signal': signal}))
    row.full_clean(exclude=['id', 'account'])
    return row


@transaction.atomic
def save_rule(account, signal, values, actor=None, expected_version=None):
    account = Account.objects.select_for_update(of=('self',), no_key=True).select_related('company').get(pk=account.pk)
    validate_rule(account, signal, values)
    model = rule_model(signal)
    lookup = {'account': account, **({} if signal == 'NEGATIVE' else {'signal': signal})}
    before = model.objects.filter(**lookup).values().first()
    current_version = before['updated_at'].isoformat() if before else ''
    if expected_version is not None and expected_version != current_version:
        raise ValueError('Другой сотрудник изменил правило. Обновите страницу.')
    fields = rule_values(signal, values)
    if before and all(before[key] == value for key, value in fields.items()): return False
    row, _ = model.objects.update_or_create(**lookup, defaults=fields)
    Audit.objects.create(company=account.company, actor=actor,
        action='control.defaults' if signal == 'NEGATIVE' else 'control.topic_rule', object_id=str(row.pk),
        before={key: before[key] for key in fields} if before else {},
        after={**fields, 'account': account.pk, 'signal': signal})
    return True


def policy_state(accounts):
    return {'overall': list(ControlDefaults.objects.filter(account__in=accounts).order_by('account_id').values()),
            'topics': list(ControlTopicRule.objects.filter(account__in=accounts).order_by('account_id', 'signal').values())}


def preview_profile(accounts, now=None):
    """No writes, incident evaluation, model calls, or marketplace requests."""
    from django.db.models import Min
    from .dataset import ControlDataset
    from .queue import product_queue
    now = now or timezone.now()
    companies = Company.objects.filter(pk__in={a.company_id for a in accounts})
    ids = Product.objects.filter(company__in=companies, mappings__account__in=accounts).values('code').annotate(first=Min('pk')).values('first')
    products = list(Product.objects.filter(pk__in=ids))
    dataset = ControlDataset(products, companies, now=now)
    incidents = list(Incident.objects.filter(product__code__in=[p.code for p in products], product__company__in=companies)
                     .exclude(status='CLOSED').select_related('product', 'owner'))
    before = {r['product'].code: r for r in product_queue(products, dataset, incidents)}
    profile = starter_profile()
    for account in accounts:
        dataset.default_rules[account.pk] = validate_rule(account, 'NEGATIVE', profile['NEGATIVE'])
        for signal in CONTROL_SIGNALS:
            dataset.topic_rules[account.pk, signal] = validate_rule(account, signal, profile[signal])
    rows = product_queue(products, dataset, incidents)
    items = []
    for row in rows:
        alerts = []
        for metric in row['metrics']:
            if metric['status'] != 'ANOMALY' and not metric['warning']: continue
            rule = metric['baseline']
            alerts.append({key: metric[key] for key in ['signal', 'label', 'control_kind', 'account_id', 'count',
                'redemptions', 'rate', 'reason', 'cluster', 'cluster_count', 'warning']} | {
                'account': metric.get('name', 'Сводно'), 'threshold': rule.high if rule else None,
                'review_ids': [t.pk for t in metric['selected']]})
        items.append({'product_id': row['product'].pk, 'code': row['product'].code, 'name': row['product'].name,
            'before': before[row['product'].code]['label'], 'after': row['label'],
            'attention_before': before[row['product'].code]['attention'], 'attention_after': row['attention'],
            'reason': row['reason'], 'incomplete': row['incomplete'], 'alerts': alerts,
            'open_incidents': [i.pk for i in row['incidents']]})
    overrides = list(Baseline.objects.filter(product__company__in=companies,
        product__code__in=[p.code for p in products]).order_by('product__code', 'signal')
        .values('product__code', 'signal', 'low', 'high', 'source', 'rule_kind', 'cluster_enabled', 'min_redemptions', 'min_signals', 'cluster_count', 'cluster_days'))
    return {'format': PROFILE_VERSION, 'created_at': now.isoformat(), 'accounts': [a.name for a in accounts],
            'profile': profile, 'summary': {'products': len(items),
                'attention_before': sum(r['attention_before'] for r in items),
                'attention_after': sum(r['attention_after'] for r in items),
                'new_attention': sum(r['attention_after'] and not r['attention_before'] for r in items),
                'incomplete': sum(r['incomplete'] for r in items)}, 'items': items, 'preserved_overrides': overrides}
