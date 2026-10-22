import os
import uuid
from datetime import timedelta
from email.utils import format_datetime
from unittest.mock import Mock, patch
from django.test import SimpleTestCase
from django.utils import timezone
from desk.integrations.base import IntegrationError, get_adapter
from desk.integrations.rate_limit import acquire, observe, retry_delay, scope_for
from desk.integrations.sync import sync_account
from desk.integrations.wildberries import Wildberries
from desk.messaging.service import enqueue, process_one
from desk.models import Account, ApiThrottle, Outgoing, SyncLog, SyncState, Thread
from .base import DeskCase


class RetryHeaderTests(SimpleTestCase):
    def test_wb_headers_fractional_seconds_and_http_date(self):
        now = timezone.now()
        cases = [
            ({'X-Ratelimit-Retry': '720'}, 721),
            ({'X-Ratelimit-Retry': '0.34'}, 2),
            ({'Retry-After': '15', 'X-Ratelimit-Reset': '720'}, 721),
            ({'Retry-After': format_datetime(now.replace(microsecond=0) + timedelta(seconds=120), usegmt=True)}, 121),
            ({'Retry-After': 'invalid', 'X-Ratelimit-Retry': '-1', 'X-Ratelimit-Reset': 'NaN'}, 60),
            ({}, 60),
        ]
        for headers, expected in cases:
            with self.subTest(headers=headers):
                self.assertEqual(retry_delay(headers, now=now.replace(microsecond=0)), expected)


class SharedThrottleTests(DeskCase):
    token = 'unit-test-credential-not-a-real-key'

    def response(self, status=200, headers=None):
        return Mock(status_code=status, headers=headers or {})

    def test_wb_cooldown_is_persisted_and_prevents_another_request(self):
        before = timezone.now()
        wait = observe(self.token, self.response(429, {'X-Ratelimit-Retry': '720'}))
        state = ApiThrottle.objects.get(scope=scope_for(self.token))
        self.assertEqual(wait, 721)
        self.assertGreaterEqual(state.next_request_at, before + timedelta(seconds=721))
        with self.assertRaises(IntegrationError) as error: acquire(self.token)
        self.assertTrue(error.exception.rate_limited)
        self.assertFalse(error.exception.request_sent)
        self.assertNotIn(self.token, str(error.exception))
        self.assertEqual(len(state.scope), 64)

    def test_empty_budget_on_success_also_defers_next_request(self):
        self.assertEqual(observe(self.token, self.response(200, {'X-Ratelimit-Remaining': '0', 'X-Ratelimit-Reset': '720'})), 721)
        with self.assertRaises(IntegrationError): acquire(self.token)

    def test_missing_headers_use_increasing_backoff(self):
        self.assertEqual(observe(self.token, self.response(429)), 60)
        self.assertEqual(observe(self.token, self.response(429)), 120)
        self.assertEqual(observe(self.token, self.response(429)), 240)

    def test_request_spacing_is_shared_and_waits_outside_database_lock(self):
        clock = [timezone.now()]
        ApiThrottle.objects.create(scope=scope_for(self.token), next_request_at=clock[0])
        def advance(delay): clock[0] += timedelta(seconds=delay)
        with patch('desk.integrations.rate_limit.timezone.now', side_effect=lambda: clock[0]), patch('desk.integrations.rate_limit.time.sleep', side_effect=advance) as sleep:
            acquire(self.token)
            acquire(self.token)
        sleep.assert_called_once_with(1.0)

    @patch('desk.integrations.base.requests.request')
    def test_another_adapter_and_account_using_same_key_cannot_bypass_pause(self, request):
        request.return_value = Mock(status_code=429, headers={'X-Ratelimit-Retry': '720'})
        other = Account.objects.create(company=self.company, name='WB2', platform='wb', token_env='ANOTHER_WB_KEY', enabled=True)
        with patch.dict(os.environ, {'TEST_WB_TOKEN': self.token, 'ANOTHER_WB_KEY': self.token}):
            with self.assertRaises(IntegrationError): get_adapter(self.account).request('GET', '/api/v1/feedbacks')
            with self.assertRaises(IntegrationError) as error: get_adapter(other).request('GET', '/api/v1/questions')
        self.assertEqual(request.call_count, 1)
        self.assertFalse(error.exception.request_sent)

    @patch('desk.messaging.service.get_adapter')
    def test_local_budget_deferrals_do_not_exhaust_outgoing_attempts(self, factory):
        factory.return_value.send.side_effect = IntegrationError('Пауза WB', retryable=True, retry_after=720, rate_limited=True, request_sent=False)
        item = enqueue(self.thread, self.user, 'Ответ', uuid.uuid4())
        for _ in range(6):
            Outgoing.objects.filter(pk=item.pk).update(next_attempt=timezone.now() - timedelta(seconds=1))
            process_one()
        item.refresh_from_db()
        self.assertEqual((item.status, item.attempts), ('Queued', 0))
        self.assertGreater(item.next_attempt, timezone.now() + timedelta(seconds=700))

    @patch('desk.messaging.service.get_adapter')
    def test_actual_http_rate_limits_keep_reply_queued_past_five_attempts(self, factory):
        factory.return_value.send.side_effect = IntegrationError('HTTP 429', retryable=True, retry_after=720, rate_limited=True)
        item = enqueue(self.thread, self.user, 'Ответ', uuid.uuid4())
        for _ in range(6):
            Outgoing.objects.filter(pk=item.pk).update(next_attempt=timezone.now() - timedelta(seconds=1))
            process_one()
        item.refresh_from_db()
        self.assertEqual((item.status, item.attempts), ('Queued', 6))


@patch.object(Wildberries, 'page_size', 2)
@patch('desk.integrations.details.enrich_account', new=lambda *args, **kwargs: 0)
class ResumableSyncTests(DeskCase):
    def row(self, number):
        return {'id': f'wb-{number}', 'text': 'Отзыв', 'productDetails': {'supplierArticle': 'sku1'}, 'productValuation': 5}

    def empty_pages(self):
        return [{'data': {'feedbacks': []}}, {'data': {'questions': []}}, {'data': {'feedbacks': []}}, {'data': {'questions': []}}]

    @patch('desk.ai.provider.analyze')
    @patch.object(Wildberries, 'request')
    def test_rate_limit_resumes_saved_page_and_honors_retry_deadline(self, request, analyze):
        request.side_effect = [{'data': {'feedbacks': [self.row(1), self.row(2)]}}, IntegrationError('HTTP 429', retryable=True, retry_after=720, rate_limited=True)]
        with self.assertRaises(IntegrationError): sync_account(self.account)
        state = SyncState.objects.get(account=self.account)
        self.assertEqual((state.cursor['stream'], state.cursor['skip']), (0, 2))
        self.assertIsNone(state.lease_token)
        self.assertGreater(state.next_attempt, timezone.now() + timedelta(seconds=700))
        self.assertEqual(Thread.objects.filter(external_id__startswith='wb-').count(), 2)
        self.assertIn('Получено: 2', SyncLog.objects.latest('pk').detail)
        self.assertIsNotNone(SyncLog.objects.latest('pk').retry_at)
        self.assertEqual(sync_account(self.account), 0)
        self.assertEqual(request.call_count, 2)
        cutoff = state.cursor['until']
        SyncState.objects.filter(pk=state.pk).update(next_attempt=timezone.now() - timedelta(seconds=1))
        request.reset_mock()
        request.side_effect = self.empty_pages()
        sync_account(self.account)
        self.assertEqual(request.call_args_list[0].kwargs['params']['skip'], 2)
        self.assertEqual(request.call_args_list[0].kwargs['params']['dateTo'], cutoff)
        state.refresh_from_db()
        self.assertEqual(state.cursor, {'watermark': cutoff})
        self.assertEqual(Thread.objects.filter(external_id__startswith='wb-').count(), 2)

    @patch('desk.ai.provider.analyze')
    @patch.object(Wildberries, 'request')
    def test_interrupted_page_can_replay_without_duplicate_messages(self, request, analyze):
        request.return_value = {'data': {'feedbacks': [self.row(1), self.row(2)]}}
        analyze.side_effect = [None, KeyboardInterrupt()]
        with self.assertRaises(KeyboardInterrupt): sync_account(self.account)
        state = SyncState.objects.get(account=self.account)
        self.assertEqual(state.cursor['skip'], 0)
        self.assertIsNone(state.lease_token)
        analyze.side_effect = None
        request.side_effect = [{'data': {'feedbacks': [self.row(1), self.row(2)]}}, *self.empty_pages()]
        sync_account(self.account)
        threads = Thread.objects.filter(external_id__startswith='wb-')
        self.assertEqual(threads.count(), 2)
        self.assertTrue(all(t.messages.count() == 1 for t in threads))

    @patch.object(Wildberries, 'request')
    def test_completed_history_is_followed_by_incremental_window(self, request):
        watermark = int(timezone.now().timestamp()) - 3600
        SyncState.objects.create(account=self.account, cursor={'watermark': watermark})
        request.side_effect = self.empty_pages()
        sync_account(self.account)
        params = request.call_args_list[0].kwargs['params']
        self.assertEqual(params['dateFrom'], watermark - 86400)
        self.assertEqual(params['order'], 'dateDesc')
        self.assertEqual(request.call_args_list[1].args[1], '/api/v1/questions')

    @patch.object(Wildberries, 'request')
    def test_live_sync_lease_blocks_duplicate_pollers(self, request):
        lease = uuid.uuid4()
        state = SyncState.objects.create(account=self.account, lease_token=lease, lease_until=timezone.now() + timedelta(minutes=1))
        self.assertEqual(sync_account(self.account), 0)
        request.assert_not_called()
        state.refresh_from_db()
        self.assertEqual(state.lease_token, lease)

    @patch('desk.integrations.sync.get_adapter')
    def test_pending_publication_confirmation_runs_before_history_fetch(self, factory):
        item = Outgoing.objects.create(thread=self.thread, request_key=uuid.uuid4(), sender=self.user, text='Ответ', status='Sent')
        adapter = factory.return_value
        adapter.replies.return_value = []
        adapter.fetch.return_value = []
        sync_account(self.account)
        self.assertEqual([call[0] for call in adapter.method_calls], ['replies', 'fetch'])
        item.refresh_from_db()
        self.assertGreater(item.next_attempt, timezone.now())

    @patch('desk.integrations.sync.get_adapter')
    def test_first_confirmation_does_not_reopen_for_old_customer_history(self, factory):
        self.thread.status = 'Ответ отправлен'
        self.thread.save(update_fields=['status'])
        item = Outgoing.objects.create(thread=self.thread, request_key=uuid.uuid4(), sender=self.user, text='Ответ', status='Sent', external_id='published-reply')
        factory.return_value.replies.return_value = [
            {'external_id': 'old-customer-comment', 'text': 'Ранний комментарий', 'direction': 'in', 'published': True},
            {'external_id': item.external_id, 'text': item.text, 'direction': 'out', 'published': True},
        ]
        factory.return_value.fetch.return_value = []
        sync_account(self.account)
        self.thread.refresh_from_db()
        item.refresh_from_db()
        self.assertEqual(self.thread.status, 'Ответ отправлен')
        self.assertEqual(item.status, 'Confirmed')

    def test_ui_shows_retry_time_as_deferred_instead_of_success(self):
        deadline = timezone.now() + timedelta(minutes=12)
        SyncState.objects.create(account=self.account, next_attempt=deadline)
        self.account.sync_status = 'Получено: 2. Пауза WB: HTTP 429'
        self.account.save(update_fields=['sync_status'])
        SyncLog.objects.create(account=self.account, success=False, detail=self.account.sync_status, retry_at=deadline)
        self.client.force_login(self.user)
        response = self.client.get('/integrations/')
        self.assertContains(response, 'Следующая попытка не ранее')
        self.assertContains(response, 'Отложено')
