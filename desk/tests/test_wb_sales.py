import os
from datetime import timedelta
from io import StringIO
from unittest.mock import Mock, patch
from django.core.management import call_command
from django.utils import timezone
from desk.integrations.base import IntegrationError
from desk.integrations.rate_limit import scope_for
from desk.integrations.wb_sales import WBSales, source_time, sync_wb_sales
from desk.models import ApiThrottle, Audit, SaleEvent, SalesSyncState, SyncState
from .base import DeskCase


class WBSalesTests(DeskCase):
    def setUp(self):
        super().setUp()
        self.account.sales_enabled = True
        self.account.save()

    def row(self, number=1, **values):
        moment = timezone.now() - timedelta(hours=1)
        return {'saleID': f'S{number}', 'nmId': 123, 'srid': f'order-{number}',
                'supplierArticle': 'not-the-card-id', 'date': moment.isoformat(),
                'lastChangeDate': moment.isoformat(), 'forPay': 0, **values}

    def due(self, **values):
        SalesSyncState.objects.filter(account=self.account).update(next_attempt=timezone.now() - timedelta(seconds=1), **values)

    @patch.object(WBSales, 'request')
    def test_sales_returns_pagination_and_replay_are_idempotent(self, request):
        sale, returned = self.row(), self.row(2, saleID='R2')
        request.return_value = [sale, returned]
        self.assertEqual(sync_wb_sales(self.account), 2)
        self.assertEqual(SaleEvent.objects.filter(kind='sale').count(), 1)
        self.assertEqual(SaleEvent.objects.filter(kind='return').count(), 1)
        self.assertEqual(SaleEvent.objects.get(external_id='S1').sku, '123')
        self.assertEqual(SaleEvent.objects.get(external_id='S1').source_data['forPay'], 0)
        state = SalesSyncState.objects.get(account=self.account)
        self.assertIsNotNone(state.coverage_since)
        self.assertEqual(state.coverage_until, source_time(returned['lastChangeDate']) - timedelta(microseconds=1))
        self.assertIsNone(state.last_sync)  # Prefix confirmed; the full current scan is still running.
        self.assertEqual(state.cursor['page_from'], returned['lastChangeDate'])
        self.due(); request.return_value = []
        sync_wb_sales(self.account)
        state.refresh_from_db()
        self.assertEqual(state.cursor, {})
        self.assertIsNotNone(state.coverage_since)
        self.assertIsNotNone(state.last_sync)
        self.due(); request.return_value = [sale, returned]
        sync_wb_sales(self.account)
        self.assertEqual(SaleEvent.objects.count(), 2)

    @patch.object(WBSales, 'request')
    def test_correction_updates_original_event_without_adding_another_sale(self, request):
        row = self.row(); request.return_value = [row]
        sync_wb_sales(self.account)
        row = {**row, 'forPay': 700, 'lastChangeDate': timezone.now().isoformat()}
        self.due(cursor={}); request.return_value = [row]
        sync_wb_sales(self.account)
        self.assertEqual(SaleEvent.objects.count(), 1)
        self.assertEqual(SaleEvent.objects.get().source_data['forPay'], 700)

    @patch.object(WBSales, 'request')
    def test_stale_replay_does_not_overwrite_a_newer_correction(self, request):
        old = self.row()
        latest = {**old, 'lastChangeDate': timezone.now().isoformat(), 'forPay': 700}
        request.return_value = [latest]
        sync_wb_sales(self.account)
        self.due(cursor={}); request.return_value = [old]
        sync_wb_sales(self.account)
        self.assertEqual(SaleEvent.objects.get().source_data['forPay'], 700)

    @patch.object(WBSales, 'request')
    def test_rows_arriving_during_pagination_are_included_before_coverage_is_completed(self, request):
        request.return_value = [self.row()]
        sync_wb_sales(self.account)
        row = self.row(2, date=(timezone.now() - timedelta(seconds=1)).isoformat(), lastChangeDate=timezone.now().isoformat())
        self.due(); request.return_value = [row]
        sync_wb_sales(self.account)
        self.due(); request.return_value = []
        sync_wb_sales(self.account)
        self.assertEqual(SaleEvent.objects.count(), 2)
        self.assertGreater(SalesSyncState.objects.get().coverage_until, source_time(row['date']))

    @patch.object(WBSales, 'request')
    def test_recent_changes_to_older_events_are_retained_and_future_event_dates_are_excluded(self, request):
        request.return_value = [self.row(1, date=(timezone.now() - timedelta(days=8)).isoformat()),
                                self.row(2, date=(timezone.now() + timedelta(days=1)).isoformat()),
                                self.row(3)]
        sync_wb_sales(self.account)
        self.assertEqual(list(SaleEvent.objects.values_list('external_id', flat=True)), ['S1', 'S3'])
        self.assertEqual(SaleEvent.objects.get(external_id='S3').source_data['supplierArticle'], 'not-the-card-id')

    @patch.object(WBSales, 'request', return_value=[])
    def test_incremental_refresh_overlaps_six_hours_and_preserves_long_term_coverage(self, request):
        now = timezone.now()
        beginning = now - timedelta(days=90)
        prior = now - timedelta(minutes=30)
        SalesSyncState.objects.create(account=self.account, coverage_since=beginning, coverage_until=prior)
        sync_wb_sales(self.account)
        self.assertEqual(source_time(request.call_args.kwargs['params']['dateFrom']), prior - timedelta(hours=6))
        state = SalesSyncState.objects.get()
        self.assertEqual(state.coverage_since, beginning)
        self.assertGreater(state.coverage_until, prior)
        audit = Audit.objects.get(action='sales.coverage')
        self.assertEqual(source_time(audit.after['since']), prior - timedelta(hours=6))

    @patch.object(WBSales, 'request')
    def test_confirmed_prefix_excludes_the_unread_boundary_timestamp(self, request):
        stamp = timezone.now() - timedelta(minutes=30)
        request.return_value = [self.row(1, lastChangeDate=stamp.isoformat()), self.row(2, lastChangeDate=stamp.isoformat())]
        sync_wb_sales(self.account)
        state = SalesSyncState.objects.get()
        self.assertEqual(state.coverage_until, stamp - timedelta(microseconds=1))
        self.assertTrue(state.cursor)
        self.assertEqual(Audit.objects.filter(action='sales.coverage').count(), 1)

    @patch.object(WBSales, 'request')
    def test_bad_row_never_marks_period_complete_or_partially_commits_page(self, request):
        request.return_value = [self.row(), self.row(2, nmId=None)]
        with self.assertRaises(IntegrationError): sync_wb_sales(self.account)
        self.assertFalse(SaleEvent.objects.exists())
        self.assertIsNone(SalesSyncState.objects.get().coverage_since)

    @patch.object(WBSales, 'request')
    def test_nonadvancing_page_is_reported_without_claiming_full_coverage(self, request):
        row = self.row()
        SalesSyncState.objects.create(account=self.account, cursor={'lookback_days': 7,
            'since': (timezone.now() - timedelta(days=7)).isoformat(), 'until': timezone.now().isoformat(), 'page_from': row['lastChangeDate']})
        request.return_value = [row]
        with self.assertRaisesRegex(IntegrationError, 'не продвигает'): sync_wb_sales(self.account)
        self.assertIsNone(SalesSyncState.objects.get().coverage_since)

    @patch.object(WBSales, 'request')
    def test_rate_limit_preserves_cursor_rows_and_retry_deadline(self, request):
        request.return_value = [self.row()]
        sync_wb_sales(self.account)
        state = SalesSyncState.objects.get(); cursor = state.cursor
        self.due(); request.side_effect = IntegrationError('HTTP 429', retryable=True, rate_limited=True, retry_after=7200)
        with self.assertRaises(IntegrationError): sync_wb_sales(self.account)
        state.refresh_from_db()
        self.assertEqual(state.cursor, cursor)
        self.assertGreater(state.next_attempt, timezone.now() + timedelta(seconds=7190))
        self.assertEqual(SaleEvent.objects.count(), 1)
        self.assertIsNone(state.lease_token)
        request.reset_mock()
        self.assertEqual(sync_wb_sales(self.account), 0)
        request.assert_not_called()

    @patch.object(WBSales, 'request')
    def test_other_process_lease_prevents_duplicate_fetch(self, request):
        import uuid
        SalesSyncState.objects.create(account=self.account, lease_token=uuid.uuid4(), lease_until=timezone.now() + timedelta(minutes=1))
        self.assertEqual(sync_wb_sales(self.account), 0)
        request.assert_not_called()

    @patch.object(WBSales, 'request')
    def test_expired_cursor_resets_to_seven_days(self, request):
        old = (timezone.now() - timedelta(days=8)).isoformat()
        SalesSyncState.objects.create(account=self.account, cursor={'lookback_days': 7, 'since': old, 'until': old, 'page_from': old})
        request.return_value = []
        sync_wb_sales(self.account)
        from desk.integrations.wb_sales import source_time
        self.assertGreaterEqual(source_time(request.call_args.kwargs['params']['dateFrom']), timezone.now() - timedelta(days=7, seconds=2))

    @patch('desk.integrations.base.requests.request')
    @patch.dict(os.environ, {'TEST_WB_STATS': 'test-statistics-only'})
    def test_real_adapter_accepts_array_and_uses_separate_key_and_budget(self, request):
        self.account.sales_token_env = 'TEST_WB_STATS'; self.account.save()
        ApiThrottle.objects.create(scope=scope_for('test-statistics-only'), next_request_at=timezone.now() + timedelta(hours=1))
        request.return_value = Mock(status_code=200, headers={}, content=b'[]')
        request.return_value.json.return_value = []
        self.assertEqual(sync_wb_sales(self.account), 0)
        self.assertEqual(request.call_args.args[:2], ('GET', 'https://statistics-api.wildberries.ru/api/v1/supplier/sales'))
        self.assertEqual(request.call_args.kwargs['headers'], {'Authorization': 'test-statistics-only'})
        self.assertEqual(request.call_args.kwargs['params']['flag'], 0)
        self.assertTrue(ApiThrottle.objects.filter(scope=scope_for('test-statistics-only', 'wb-sales')).exists())

    @patch.object(WBSales, 'request')
    def test_access_error_reports_required_permission(self, request):
        request.side_effect = IntegrationError('API: HTTP 403. Проверьте доступ и параметры.')
        with self.assertRaises(IntegrationError): sync_wb_sales(self.account)
        self.assertIn('Статистика', SalesSyncState.objects.get().status)

    @patch.object(WBSales, 'request')
    def test_disabled_and_demo_accounts_cannot_fetch(self, request):
        self.account.enabled = False
        with self.assertRaises(IntegrationError): sync_wb_sales(self.account)
        self.account.enabled = True; self.company.is_demo = True
        with self.assertRaises(IntegrationError): sync_wb_sales(self.account)
        request.assert_not_called()

    @patch('desk.management.commands.sync_channels.sync_account')
    @patch('desk.integrations.wb_sales.sync_wb_sales')
    def test_feedback_pause_does_not_block_sales_polling(self, sales, reviews):
        self.ym.enabled = False; self.ym.save()
        SyncState.objects.create(account=self.account, next_attempt=timezone.now() + timedelta(hours=1))
        call_command('sync_channels', stdout=StringIO(), stderr=StringIO())
        reviews.assert_not_called()
        sales.assert_called_once()
