"""Revoke certificates produced with implicit 30-day order creation filters.

Keep every saved event/order and API retry deadline. Prime the existing,
opted-in history lane for a bounded 90-day reconciliation, without AI jobs.
"""
from datetime import timedelta
from django.db import migrations
from django.utils import timezone


def revoke_coverage(apps, schema_editor):
    alias = schema_editor.connection.alias
    Account = apps.get_model('desk', 'Account')
    Sales = apps.get_model('desk', 'SalesSyncState')
    History = apps.get_model('desk', 'HistorySyncState')
    Audit = apps.get_model('desk', 'Audit')
    now = timezone.now()
    for account in Account.objects.using(alias).filter(platform='ym', company__is_demo=False, sales_enabled=True):
        current, _ = Sales.objects.using(alias).get_or_create(account_id=account.pk)
        history, _ = History.objects.using(alias).get_or_create(account_id=account.pk, kind='sales')
        before = {'current': {'since': str(current.coverage_since), 'until': str(current.coverage_until),
                             'cursor': current.cursor, 'status': current.status},
                  'history': {'since': str(history.coverage_since), 'until': str(history.coverage_until),
                              'cursor': history.cursor, 'days': history.days, 'exhausted': history.exhausted, 'status': history.status}}
        message = 'Полнота продаж Маркета отозвана: повторная проверка по статистике изменений и явным датам создания.'
        Sales.objects.using(alias).filter(pk=current.pk).update(coverage_since=None, coverage_until=None,
            cursor={}, lease_token=None, lease_until=None, next_attempt=max(now, current.next_attempt), status=message)
        cursor = {'since': (now-timedelta(days=90)).isoformat(),
                  'until': (now-timedelta(minutes=45)).isoformat(), 'days': 90, 'ym_version': 2}
        History.objects.using(alias).filter(pk=history.pk).update(coverage_since=None, coverage_until=None,
            days=0, exhausted=False, cursor=cursor if account.history_enabled else {}, lease_token=None, lease_until=None,
            next_attempt=max(now, current.next_attempt, history.next_attempt), status=message)
        Audit.objects.using(alias).create(company_id=account.company_id, action='ym.sales.recheck_coverage',
            object_id=str(account.pk), before=before, after={'coverage': 'unverified', 'history_days': 90,
                'history_enabled': account.history_enabled, 'events_preserved': True})


class Migration(migrations.Migration):
    dependencies = [('desk', '0019_topic_control_thresholds')]
    operations = [migrations.RunPython(revoke_coverage, reverse_code=migrations.RunPython.noop)]
