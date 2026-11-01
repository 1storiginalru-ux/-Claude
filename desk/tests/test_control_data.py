"""Cause separation, read-only diagnostics and account-scoped history planning."""
from datetime import timedelta
from io import StringIO
import json
from pathlib import Path
import tempfile
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import connection
from django.test import override_settings
from django.test.utils import CaptureQueriesContext
from desk.models import AIJob, Analysis, Audit, Outgoing, Product, ProductMapping, ReviewAssessment, SaleEvent, SalesSyncState, SyncState, YandexOrderState
from desk.voc.data_quality import control_data_report
from .test_review_control import ReviewControlCase


class ControlDataTests(ReviewControlCase):
    def report(self, accounts=None):
        return control_data_report(accounts or [self.account], now=self.now)

    def test_complete_data_retains_rate_and_report_never_writes(self):
        self.events(); self.analysis()
        models = [Analysis, Audit, Outgoing, AIJob, ReviewAssessment, SaleEvent]
        before = [list(m.objects.values()) for m in models]
        report = self.report()
        row = report['items'][0]
        self.assertTrue(row['ready']); self.assertEqual(row['per100'], 1)
        self.assertEqual(row['causes'], [])
        self.assertEqual(report['summary']['negative_incomplete_pairs'], 0)
        self.assertEqual([list(m.objects.values()) for m in models], before)

    def test_catalogue_without_activity_is_separate_from_broken_review_checks(self):
        self.analysis(); self.events(20)
        unused = Product.objects.create(company=self.company, code='NO-REVIEWS', name='Без активности')
        ProductMapping.objects.create(account=self.account, product=unused, sku='unused')
        report = self.report()
        self.assertEqual(report['summary']['catalogue_products'], 2)
        self.assertEqual(report['summary']['products_with_reviews'], 1)
        self.assertEqual(report['summary']['no_activity_pairs'], 1)
        self.assertEqual(report['summary']['incomplete_active_pairs'], 1)
        self.assertEqual(report['summary']['negative_products'], 1)
        self.assertEqual(report['items'][0]['code'], 'P1')
        self.assertEqual(next(c['count'] for c in report['causes'] if c['code']=='few_sales'), 1)

    def test_few_sales_does_not_become_a_reliable_percentage(self):
        self.events(67); self.analysis()
        row = self.report()['items'][0]
        self.assertIn('few_sales', row['causes']); self.assertFalse(row['ready'])
        self.assertIsNone(row['per100']); self.assertEqual(row['negative'], 1)

    def test_stale_sales_coverage_and_classification_are_separate_causes(self):
        self.events(); self.analysis(confidence=.2)
        self.thread.received_at=self.now-timedelta(hours=5); self.thread.save()
        SaleEvent.objects.update(occurred_at=self.now-timedelta(hours=6))
        SalesSyncState.objects.filter(account=self.account).update(coverage_since=self.now-timedelta(hours=5),
            coverage_until=self.now-timedelta(hours=4), status='Ожидаем лимита API', cursor={'private':'not exported'})
        row = self.report()['items'][0]
        self.assertTrue({'sales_coverage','sales_stale','classification'} <= set(row['causes']))
        self.assertFalse(row['ready']); self.assertEqual(row['negative'], 0)
        sales = self.report()['accounts'][0]['sales']
        self.assertTrue(sales['continuation']); self.assertNotIn('cursor', sales)

    def test_market_unknown_date_only_blocks_the_affected_product_and_window(self):
        self.account.platform='ym'; self.account.save()
        self.events(); self.analysis()
        YandexOrderState.objects.create(account=self.account, campaign_id='1', order_id='2', changed_at=self.now,
            issues=[{'sku':'sku1','reason':'Нет даты выкупа','since':(self.now-timedelta(days=1)).isoformat(),'until':self.now.isoformat()}])
        report = self.report()
        self.assertIn('sale_date_quantity', report['items'][0]['causes'])
        self.assertEqual(report['accounts'][0]['uncertain_orders'], 1)
        YandexOrderState.objects.update(issues=[{'sku':'different', 'reason':'Нет даты выкупа'}])
        self.assertNotIn('sale_date_quantity', self.report()['items'][0]['causes'])

    def test_high_rating_hidden_negative_has_priority_but_human_non_negative_wins(self):
        self.events(30); self.analysis(sentiment='positive', hidden_negative=True)
        self.assertEqual(self.report()['items'][0]['priority'], 0)
        self.confirm(decision='non_negative', signals=[])
        row = self.report()['items'][0]
        self.assertEqual(row['negative'], 0); self.assertEqual(row['priority'], 2)

    def test_untrusted_low_rating_is_a_candidate_not_a_counted_complaint(self):
        self.thread.rating=2; self.thread.save()
        self.analysis(confidence=.4)
        row = self.report()['items'][0]
        self.assertEqual((row['negative'], row['suspected'], row['priority']), (0,1,1))

    def test_nonmatching_sku_and_unlinked_reviews_are_visible(self):
        self.events(); self.analysis()
        self.thread.sku='unmapped'; self.thread.save()
        self.make_thread('without-product', product=None)
        report = self.report()
        self.assertIn('mapping', report['items'][0]['causes'])
        self.assertFalse(report['items'][0]['ready'])
        self.assertEqual(report['summary']['unmapped_reviews'], 1)

    def test_period_and_storage_are_independent_for_each_cabinet(self):
        self.events(); self.analysis()
        self.ym.sales_enabled=True; self.ym.save()
        ProductMapping.objects.create(account=self.ym, product=self.product, sku='ym-sku')
        for model in [SalesSyncState, SyncState]:
            model.objects.create(account=self.ym, coverage_since=self.now-timedelta(days=7), coverage_until=self.now)
        t=self.make_thread('ym-negative', account=self.ym, sku='ym-sku'); self.analysis(t)
        report = self.report([self.account, self.ym])
        rows = {r['account_id']:r for r in report['items']}
        self.assertTrue(rows[self.account.pk]['ready'])
        self.assertIn('few_sales', rows[self.ym.pk]['causes'])
        self.assertEqual(rows[self.ym.pk]['redemptions'], 0)
        self.assertEqual(report['summary']['catalogue_products'], 1)
        self.assertEqual(report['summary']['pairs'], 2)

    def test_no_mapping_still_shows_saved_account_sales_state(self):
        self.events(); ProductMapping.objects.filter(account=self.account).delete()
        report = self.report()
        self.assertEqual(report['accounts'][0]['storage']['count'], 100)
        self.assertIsNotNone(report['accounts'][0]['sales'])
        self.assertIn('mapping', report['items'][0]['causes'])

    def test_six_month_priority_excludes_future_and_old_reviews(self):
        self.thread.received_at=self.now-timedelta(days=210); self.thread.save()
        self.analysis()
        t=self.make_thread('future', received_at=self.now+timedelta(days=1)); self.analysis(t)
        report = self.report()
        self.assertEqual(report['summary']['products_with_reviews'], 0)
        self.assertEqual(report['summary']['negative_products'], 0)

    def test_query_count_does_not_grow_per_catalogue_product(self):
        self.analysis(); self.events()
        with CaptureQueriesContext(connection) as small: self.report()
        for n in range(10):
            product=Product.objects.create(company=self.company,code=f'Q-{n}',name='Другой товар')
            ProductMapping.objects.create(account=self.account,product=product,sku=f'q-{n}')
        with CaptureQueriesContext(connection) as large: self.report()
        self.assertLessEqual(len(large),len(small)+1)


class ControlDataAccessTests(ReviewControlCase):
    def test_page_is_read_only_and_only_shows_accessible_accounts(self):
        self.analysis()
        response=self.client.get('/voc/data/')
        self.assertContains(response,'Полнота данных и продажи')
        self.assertNotContains(response,self.other_account.name)
        self.assertEqual(self.client.post('/voc/data/',{}).status_code,405)
        self.assertEqual(self.client.get(f'/voc/data/?account={self.other_account.pk}').status_code,404)
        self.assertEqual(self.client.get('/voc/data/?account=bad').status_code,404)
        self.assertEqual(self.client.get('/voc/data/?cause=bad').status_code,404)

    def test_account_and_cause_filters_work_without_marking_unknown_negatives_confirmed(self):
        self.thread.rating=2; self.thread.save(); self.analysis(confidence=.4)
        response=self.client.get(f'/voc/data/?account={self.account.pk}&cause=few_sales')
        self.assertContains(response,self.product.name)
        self.assertEqual(response.context['data_page'].paginator.count,1)
        self.assertContains(response,'кандидатов на негатив: 1')
        self.assertEqual(self.client.get('/voc/data/?filter=ready').context['data_page'].paginator.count,0)

    def test_command_exports_private_report_without_text_keys_or_cursors(self):
        self.analysis(); self.events(20)
        output=StringIO()
        with tempfile.TemporaryDirectory() as directory, override_settings(STATE_DIR=Path(directory)):
            call_command('control_data_status','--account',self.account.name,'--report',stdout=output)
            path=next((Path(directory)/'exports').glob('control-data-*.json'))
            report=json.loads(path.read_text())
            self.assertEqual(path.stat().st_mode & 0o777,0o600)
            self.assertEqual(report['summary']['negative_incomplete_pairs'],1)
            self.assertNotIn(self.thread.text,path.read_text())
            self.assertNotIn('TEST_WB_TOKEN',path.read_text())
            self.assertNotIn('cursor',path.read_text())
        self.assertIn('API и AI не вызываются',output.getvalue())
        self.assertFalse(Audit.objects.exists() or Outgoing.objects.exists() or AIJob.objects.exists())

    def test_command_rejects_unknown_accounts_before_any_export(self):
        with self.assertRaises(CommandError):
            call_command('control_data_status','--account','does-not-exist',stdout=StringIO())
