from datetime import datetime, timedelta, timezone as dt_timezone
from unittest.mock import Mock, patch
import uuid
from django.test import SimpleTestCase
from django.utils import timezone
from desk.integrations.details import enrich_account
from desk.integrations.sync import sync_account
from desk.integrations.wildberries import Wildberries
from desk.integrations.yandex import YandexMarket
from desk.integrations.window import sync_window, within_window
from desk.models import Outgoing, SyncState, Thread
from .base import DeskCase

NOW = datetime(2026, 10, 23, 12, 34, 56, tzinfo=dt_timezone.utc)
SINCE = NOW - timedelta(days=7)


def wb_row(identifier, created):
    row = {'id': identifier, 'text': identifier, 'productDetails': {'supplierArticle': 'sku1'}, 'productValuation': 5}
    if created is not None: row['createdDate'] = created.isoformat()
    return row


def ym_review(identifier, created):
    row = {'feedbackId': identifier, 'identifiers': {'offerId': 'sku1'}, 'description': {'comment': 'Отзыв'}, 'statistics': {'rating': 5}}
    if created is not None: row['createdAt'] = created.isoformat()
    return row


def ym_question(identifier, created):
    row = {'questionIdentifiers': {'id': identifier, 'offerId': 'sku1'}, 'text': 'Вопрос', 'author': {'name': 'Покупатель'}}
    if created is not None: row['createdAt'] = created.isoformat()
    return row


class WindowBoundaryTests(SimpleTestCase):
    def test_window_is_168_hours_and_boundary_is_included(self):
        self.assertEqual(sync_window(NOW), (SINCE, NOW))
        for date in [SINCE, SINCE + timedelta(seconds=1), NOW]:
            self.assertTrue(within_window(date, SINCE, NOW))
        for date in [SINCE - timedelta(microseconds=1), NOW + timedelta(microseconds=1), None, '', 'invalid', '2026-02-31T00:00:00Z']:
            with self.subTest(date=date): self.assertFalse(within_window(date, SINCE, NOW))

    def test_timezone_offsets_and_naive_local_dates_are_compared_correctly(self):
        self.assertTrue(within_window('2026-10-16T15:34:56+03:00', SINCE, NOW))
        self.assertFalse(within_window('2026-10-16T15:34:55+03:00', SINCE, NOW))
        self.assertTrue(within_window('2026-10-16T15:34:56', SINCE, NOW))


class AutomaticWindowTests(DeskCase):
    def setUp(self):
        self.clock = patch('desk.integrations.window.timezone.now', return_value=NOW)
        self.clock.start()
        self.addCleanup(self.clock.stop)
        super().setUp()
        Thread.objects.update(details_next_attempt=NOW - timedelta(seconds=1))

    @patch.object(Wildberries, 'request')
    def test_wb_requests_only_seven_days_and_rejects_outside_or_undated_rows(self, request):
        rows = [wb_row('boundary', SINCE), wb_row('recent', NOW - timedelta(hours=1)),
                wb_row('old', SINCE - timedelta(seconds=1)), wb_row('future', NOW + timedelta(seconds=1)), wb_row('unknown', None)]
        request.side_effect = lambda method, path, **kwargs: {'data': {path.rsplit('/', 1)[-1]: rows}}
        incoming = list(Wildberries(self.account).fetch())
        self.assertEqual(len(incoming), 8)
        self.assertEqual({row.external_id for row in incoming}, {'boundary', 'recent'})
        self.assertEqual(request.call_count, 4)
        for call in request.call_args_list:
            self.assertEqual(call.kwargs['params']['dateFrom'], int(SINCE.timestamp()))
            self.assertEqual(call.kwargs['params']['dateTo'], int(NOW.timestamp()))

    def test_wb_discards_legacy_archive_cursor_and_expired_window(self):
        adapter = Wildberries(self.account)
        for cursor in [
            {'until': int(NOW.timestamp()), 'since': 0, 'skip': 9000, 'stream': 2},
            {'lookback_days': 7, 'until': int((SINCE - timedelta(days=1)).timestamp()), 'since': 0, 'skip': 2000, 'stream': 3},
            {'watermark': int((NOW - timedelta(days=90)).timestamp())},
        ]:
            with self.subTest(cursor=cursor):
                prepared = adapter.window_cursor(cursor)
                self.assertEqual((prepared['skip'], prepared['stream']), (0, 0))
                self.assertEqual(prepared['since'], int(SINCE.timestamp()))
                self.assertEqual(prepared['until'], int(NOW.timestamp()))

    def test_wb_resume_keeps_page_and_cutoff_but_advances_lower_bound(self):
        until = NOW - timedelta(hours=1)
        cursor = {'lookback_days': 7, 'until': int(until.timestamp()), 'since': int((SINCE - timedelta(hours=1)).timestamp()), 'stream': 1, 'skip': 1000}
        prepared = Wildberries(self.account).window_cursor(cursor)
        self.assertEqual(prepared['since'], int(SINCE.timestamp()))
        self.assertEqual(prepared['until'], int(until.timestamp()))
        self.assertEqual((prepared['skip'], prepared['stream']), (1000, 1))

    @patch.object(Wildberries, 'request')
    def test_wb_rows_expiring_during_request_are_not_imported(self, request):
        current = [NOW]
        def response(method, path, **kwargs):
            current[0] += timedelta(minutes=2)
            return {'data': {path.rsplit('/', 1)[-1]: [wb_row('expired', SINCE + timedelta(seconds=30))]}}
        request.side_effect = response
        with patch('desk.integrations.window.timezone.now', side_effect=lambda: current[0]):
            incoming = list(Wildberries(self.account).fetch())
        self.assertEqual(incoming, [])
        self.assertGreater(request.call_args_list[1].kwargs['params']['dateFrom'], request.call_args_list[0].kwargs['params']['dateFrom'])

    @patch.object(YandexMarket, 'request')
    def test_market_review_dates_apply_to_every_page_and_filter_response(self, request):
        request.side_effect = [
            {'result': {'feedbacks': [ym_review(1, NOW - timedelta(hours=1)), ym_review(2, SINCE - timedelta(seconds=1))], 'paging': {'nextPageToken': 'page2'}}},
            {'result': {'feedbacks': [ym_review(3, NOW + timedelta(seconds=1)), ym_review(4, None)]}},
            {'result': {'questions': []}},
        ]
        incoming = list(YandexMarket(self.ym).fetch())
        self.assertEqual([row.external_id for row in incoming], ['1'])
        for call in request.call_args_list[:2]:
            self.assertEqual(call.kwargs['json'], {'dateTimeFrom': SINCE.isoformat(), 'dateTimeTo': NOW.isoformat()})
        self.assertEqual(request.call_args_list[1].kwargs['params']['page_token'], 'page2')

    @patch.object(YandexMarket, 'request')
    def test_market_question_day_filter_is_refined_to_exact_time(self, request):
        request.side_effect = [{'result': {'feedbacks': []}}, {'result': {'questions': [
            ym_question(1, SINCE), ym_question(2, SINCE.replace(hour=0)),
            ym_question(3, NOW - timedelta(hours=1)), ym_question(4, NOW + timedelta(hours=1)), ym_question(5, None),
        ]}}]
        incoming = list(YandexMarket(self.ym).fetch())
        self.assertEqual([row.external_id for row in incoming], ['1', '3'])
        self.assertEqual(request.call_args.kwargs['json'], {'dateFrom': '2026-10-16', 'dateTo': '2026-10-23'})

    def test_enrichment_never_requests_old_or_future_stored_records(self):
        Thread.objects.filter(pk=self.thread.pk).update(received_at=SINCE - timedelta(seconds=1))
        self.make_thread('future', received_at=NOW + timedelta(seconds=1), details_next_attempt=NOW - timedelta(seconds=1))
        adapter = Mock()
        self.assertEqual(enrich_account(self.account, adapter), 0)
        adapter.fetch_one.assert_not_called()
        adapter.history.assert_not_called()
        self.assertEqual(Thread.objects.count(), 2)

    def test_enrichment_accepts_boundary_and_keeps_archived_record_unchanged(self):
        Thread.objects.filter(pk=self.thread.pk).update(received_at=SINCE - timedelta(days=1))
        boundary = self.make_thread('boundary', received_at=SINCE, details_next_attempt=NOW - timedelta(seconds=1))
        adapter = Mock()
        adapter.fetch_one.return_value = Wildberries(self.account).parse(wb_row(boundary.external_id, SINCE), 'review')
        self.assertEqual(enrich_account(self.account, adapter), 1)
        adapter.fetch_one.assert_called_once()
        self.assertEqual(adapter.fetch_one.call_args.args[0].pk, boundary.pk)
        self.thread.refresh_from_db()
        self.assertIsNone(self.thread.source_synced_at)

    def test_details_response_cannot_import_outside_or_missing_creation_date(self):
        for date in [SINCE - timedelta(days=1), NOW + timedelta(seconds=1), None]:
            with self.subTest(date=date):
                Thread.objects.filter(pk=self.thread.pk).update(details_next_attempt=NOW - timedelta(seconds=1))
                adapter = Mock()
                adapter.fetch_one.return_value = Wildberries(self.account).parse(wb_row(self.thread.external_id, date), 'review')
                self.assertEqual(enrich_account(self.account, adapter), 0)
                self.thread.refresh_from_db()
                self.assertIsNone(self.thread.source_synced_at)
                self.assertEqual(self.thread.source_data, {})

    @patch.object(Wildberries, 'request')
    def test_outside_api_rows_do_not_overwrite_saved_history_or_trigger_ai(self, request):
        Thread.objects.filter(pk=self.thread.pk).update(received_at=SINCE - timedelta(days=1))
        SyncState.objects.create(account=self.account, next_attempt=NOW - timedelta(seconds=1), cursor={'since': 0, 'until': int(NOW.timestamp()), 'stream': 2, 'skip': 1000})
        request.side_effect = lambda method, path, **kwargs: {'data': {path.rsplit('/', 1)[-1]: [wb_row('must-not-import', SINCE - timedelta(days=1))]}}
        with patch('desk.ai.provider.analyze') as analyze:
            self.assertEqual(sync_account(self.account), 0)
        analyze.assert_not_called()
        self.assertEqual(Thread.objects.count(), 1)
        self.thread.refresh_from_db()
        self.assertEqual(self.thread.text, 'Аромат исчезает через час')
        self.assertEqual(request.call_args_list[0].kwargs['params']['skip'], 0)

    @patch('desk.integrations.sync.get_adapter')
    def test_publication_confirmation_for_explicit_reply_to_old_review_remains(self, factory):
        Thread.objects.filter(pk=self.thread.pk).update(received_at=SINCE - timedelta(days=1))
        SyncState.objects.create(account=self.account, next_attempt=NOW - timedelta(seconds=1))
        item = Outgoing.objects.create(thread=self.thread, sender=self.user, request_key=uuid.uuid4(), text='Ответ', status='Sent', external_id='published', next_attempt=NOW - timedelta(seconds=1))
        factory.return_value.fetch.return_value = []
        factory.return_value.replies.return_value = [{'external_id': 'published', 'text': item.text, 'direction': 'out', 'published': True}]
        sync_account(self.account)
        item.refresh_from_db()
        self.assertEqual(item.status, 'Confirmed')
        factory.return_value.fetch_one.assert_not_called()

    def test_dashboard_counts_recent_data_and_preserves_old_record_in_inbox(self):
        Thread.objects.filter(pk=self.thread.pk).update(received_at=SINCE - timedelta(days=1), text='Старый отзыв сохранён')
        self.make_thread('recent', source_synced_at=NOW, history_synced_at=NOW)
        self.client.force_login(self.user)
        response = self.client.get('/integrations/')
        row = next(row for row in response.context['rows'] if row['account'].pk == self.account.pk)
        self.assertEqual((row['account'].total_threads, row['account'].sync_threads, row['account'].full_threads), (2, 1, 1))
        self.assertContains(response, 'Последние 7 суток')
        self.assertContains(self.client.get('/'), 'Старый отзыв сохранён')
        self.assertContains(self.client.get(f'/threads/{self.thread.pk}/'), 'автоматическая дозагрузка старых данных отключена')
