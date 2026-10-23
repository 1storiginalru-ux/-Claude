import copy
import importlib
from datetime import timedelta
from unittest.mock import Mock, patch
from django.apps import apps
from django.test import SimpleTestCase
from django.utils import timezone
from desk.integrations.base import Incoming, IntegrationError
from desk.integrations.content import content_view, media_url
from desk.integrations.details import enrich_account
from desk.integrations.sync import ingest, sync_account
from desk.integrations.wildberries import Wildberries
from desk.integrations.yandex import YandexMarket
from desk.models import Message, SyncState, Thread
from .base import DeskCase


WB_ROW = {
    'id': 'full-wb', 'text': 'Комментарий', 'pros': 'Удобно', 'cons': 'Коробка помята',
    'productValuation': 4, 'createdDate': (timezone.now() - timedelta(days=3)).isoformat(), 'userName': 'Анна',
    'productDetails': {'supplierArticle': 'sku1', 'nmId': 123, 'productName': 'Парфюм', 'brandName': 'Бренд'},
    'photoLinks': [{'fullSize': 'https://cdn.example.com/full.jpg', 'miniSize': 'https://cdn.example.com/small.jpg'}],
    'video': {'link': 'https://cdn.example.com/video.m3u8', 'previewImage': 'https://cdn.example.com/poster.jpg', 'durationSec': 25},
    'answer': {'text': 'Спасибо', 'state': 'wbRu', 'editable': False},
    'orderStatus': 'buyout', 'color': 'Красный', 'lastOrderCreatedAt': '2026-10-15',
    'futureField': {'nested': ['keep', 1, False]},
}
YM_ROW = {
    'feedbackId': 10, 'createdAt': (timezone.now() - timedelta(days=3)).isoformat(), 'author': 'Мария',
    'identifiers': {'offerId': 'sku1', 'orderId': 654321},
    'description': {'advantages': 'Удобно', 'disadvantages': 'Течёт', 'comment': 'Комментарий'},
    'statistics': {'rating': 4, 'commentsCount': 2, 'recommended': False, 'paidAmount': 100},
    'media': {'photos': ['https://cdn.example.com/market-photo.jpg'], 'videos': ['https://cdn.example.com/market-video.mp4']},
    'needReaction': False, 'futureField': {'keep': 'all fields'},
}


class MediaURLTests(SimpleTestCase):
    def test_media_links_are_https_without_credentials_or_local_targets(self):
        for value in ['javascript:alert(1)', 'data:text/html,test', '/relative.jpg', '//example.com/x',
                      'http://example.com/x', 'https://user:password@example.com/x', 'https://127.0.0.1/x',
                      'https://[::1]/x', 'https://localhost/x', 'https://computer.local/x',
                      'https://example.com:99999/x', 'https://example.com/x\ny', None]:
            with self.subTest(value=value): self.assertEqual(media_url(value), '')
        self.assertEqual(media_url('https://cdn.example.com/video.mp4?signature=abc'), 'https://cdn.example.com/video.mp4?signature=abc')


class FullPayloadTests(DeskCase):
    def test_wb_preserves_every_field_media_and_existing_published_answer(self):
        incoming = Wildberries(self.account).parse(copy.deepcopy(WB_ROW), 'review')
        thread, _ = ingest(self.account, incoming)
        thread.refresh_from_db()
        self.assertEqual(thread.source_data, WB_ROW)
        self.assertIsNotNone(thread.source_synced_at)
        self.assertIsNotNone(thread.history_synced_at)
        self.assertEqual(thread.messages.filter(direction='in').get().source_data, WB_ROW)
        self.assertEqual(thread.messages.filter(direction='out').get().source_data, WB_ROW['answer'])
        view = content_view(thread)
        self.assertEqual(view['photos'][0]['preview'], WB_ROW['photoLinks'][0]['miniSize'])
        self.assertEqual(view['videos'][0]['duration'], 25)
        self.assertIn({'label': 'Статус заказа', 'value': 'Выкуплен'}, view['info'])

    def test_yandex_preserves_media_order_statistics_and_unknown_fields(self):
        incoming = YandexMarket(self.ym).parse(copy.deepcopy(YM_ROW), 'review')
        thread, _ = ingest(self.ym, incoming)
        thread.refresh_from_db()
        self.assertEqual(thread.source_data, YM_ROW)
        self.assertIsNone(thread.history_synced_at)
        view = content_view(thread)
        self.assertEqual(view['photos'][0]['url'], YM_ROW['media']['photos'][0])
        self.assertEqual(view['videos'][0]['url'], YM_ROW['media']['videos'][0])
        self.assertIn({'label': 'ID заказа', 'value': 654321}, view['info'])
        self.assertIn({'label': 'Рекомендует товар', 'value': 'Нет'}, view['info'])

    def test_media_only_edit_updates_snapshot_without_duplicate_or_reopening(self):
        adapter = Wildberries(self.account)
        thread, _ = ingest(self.account, adapter.parse(copy.deepcopy(WB_ROW), 'review'))
        thread.status = 'Закрыто'
        thread.save(update_fields=['status'])
        row = copy.deepcopy(WB_ROW)
        row['photoLinks'].append({'fullSize': 'https://cdn.example.com/second.jpg'})
        updated, changed = ingest(self.account, adapter.parse(row, 'review'))
        self.assertFalse(changed)
        self.assertEqual(updated.status, 'Закрыто')
        self.assertEqual(updated.messages.count(), 2)
        self.assertEqual(len(updated.source_data['photoLinks']), 2)

    def test_legacy_incoming_without_payload_does_not_erase_full_data(self):
        thread, _ = ingest(self.account, Wildberries(self.account).parse(copy.deepcopy(WB_ROW), 'review'))
        ingest(self.account, Incoming(thread.external_id, thread.kind, thread.text, thread.sku, rating=thread.rating))
        thread.refresh_from_db()
        self.assertEqual(thread.source_data, WB_ROW)

    def test_new_comment_count_schedules_history_without_reanalyzing_text(self):
        row = copy.deepcopy(YM_ROW)
        thread, _ = ingest(self.ym, YandexMarket(self.ym).parse(row, 'review'))
        Thread.objects.filter(pk=thread.pk).update(history_synced_at=timezone.now(), details_next_attempt=timezone.now() + timedelta(days=1))
        row['statistics']['commentsCount'] = 3
        updated, changed = ingest(self.ym, YandexMarket(self.ym).parse(row, 'review'))
        self.assertFalse(changed)
        self.assertIsNotNone(updated.history_synced_at)
        self.assertLessEqual(updated.details_next_attempt, timezone.now())

    def test_zero_comments_avoids_unnecessary_history_request(self):
        row = copy.deepcopy(YM_ROW)
        row['statistics']['commentsCount'] = 0
        thread, _ = ingest(self.ym, YandexMarket(self.ym).parse(row, 'review'))
        self.assertIsNotNone(thread.history_synced_at)
        adapter = Mock()
        self.assertEqual(enrich_account(self.ym, adapter), 0)
        adapter.history.assert_not_called()

    def test_question_payload_keeps_votes_author_and_all_identifiers(self):
        row = {'questionIdentifiers': {'id': 20, 'offerId': 'sku1', 'extra': 'kept'}, 'text': 'Вопрос',
               'author': {'name': 'Иван', 'type': 'USER'}, 'votes': {'positive': 4, 'negative': 0}}
        thread, _ = ingest(self.ym, YandexMarket(self.ym).parse(row, 'question'))
        self.assertEqual(thread.source_data, row)
        self.assertEqual(thread.messages.get().source_author, 'Иван')

    def test_review_without_text_still_preserves_attachments(self):
        row = copy.deepcopy(YM_ROW)
        row['description'] = {}
        thread, _ = ingest(self.ym, YandexMarket(self.ym).parse(row, 'review'))
        self.assertEqual(thread.text, '(Отзыв без текста)')
        self.assertEqual(len(content_view(thread)['photos']), 1)

    def test_unavailable_record_is_reported_without_attribute_error(self):
        for adapter in [Wildberries(self.account), YandexMarket(self.ym)]:
            with self.subTest(platform=adapter.account.platform), self.assertRaises(IntegrationError):
                adapter.parse(None, 'review')


class HistoryEnrichmentTests(DeskCase):
    def yandex_thread(self):
        return ingest(self.ym, YandexMarket(self.ym).parse(copy.deepcopy(YM_ROW), 'review'))[0]

    @patch.object(YandexMarket, 'request')
    def test_history_resumes_after_rate_limit_and_preserves_all_reply_fields(self, request):
        thread = self.yandex_thread()
        request.side_effect = [
            {'result': {'comments': [{'id': 1, 'text': 'Первый', 'canModify': False, 'status': 'PUBLISHED', 'author': {'type': 'USER', 'name': 'Покупатель'}, 'parentId': 99}], 'paging': {'nextPageToken': 'page-2'}}},
            IntegrationError('HTTP 429', retryable=True, rate_limited=True, retry_after=720),
        ]
        with self.assertRaises(IntegrationError): enrich_account(self.ym, YandexMarket(self.ym))
        thread.refresh_from_db()
        self.assertEqual(thread.history_cursor, {'page_token': 'page-2'})
        self.assertIsNone(thread.history_synced_at)
        self.assertEqual(thread.messages.count(), 2)
        self.assertEqual(thread.messages.exclude(external_id__startswith='source-').get().source_data['parentId'], 99)
        self.assertGreater(thread.details_next_attempt, timezone.now() + timedelta(seconds=700))
        self.assertEqual(enrich_account(self.ym, YandexMarket(self.ym)), 0)
        Thread.objects.filter(pk=thread.pk).update(details_next_attempt=timezone.now() - timedelta(seconds=1))
        request.reset_mock()
        request.side_effect = [{'result': {'comments': [{'id': 2, 'text': 'Ответ магазина', 'canModify': False, 'status': 'PUBLISHED', 'author': {'type': 'BUSINESS', 'name': 'Магазин'}}]}}]
        self.assertEqual(enrich_account(self.ym, YandexMarket(self.ym)), 1)
        self.assertEqual(request.call_args.kwargs['params']['page_token'], 'page-2')
        thread.refresh_from_db()
        self.assertEqual(thread.history_cursor, {})
        self.assertIsNotNone(thread.history_synced_at)
        self.assertEqual(thread.messages.count(), 3)
        self.assertEqual(thread.messages.get(direction='out').source_author, 'Магазин')

    @patch.object(YandexMarket, 'request')
    def test_question_answers_include_nested_comments_and_original_dates(self, request):
        thread = self.make_thread('20', account=self.ym, kind='question')
        request.return_value = {'result': {'answers': [{'id': 7, 'text': 'Ответ', 'canModify': True, 'status': 'PUBLISHED',
             'createdAt': '2026-10-21T12:00:00Z', 'author': {'name': 'Компания', 'type': 'BUSINESS'},
             'comments': [{'id': 7, 'answerId': 7, 'text': 'Уточнение', 'status': 'PUBLISHED', 'author': {'name': 'Другой покупатель', 'type': 'USER'}, 'createdAt': '2026-10-21T13:00:00Z'}]}]}}
        adapter = YandexMarket(self.ym)
        replies = list(adapter.history(thread))
        self.assertEqual([row['external_id'] for row in replies], ['7', 'comment-7'])
        self.assertEqual([row['direction'] for row in replies], ['out', 'in'])
        self.assertEqual(replies[1]['source_data']['answerId'], 7)
        self.assertEqual(replies[1]['created_at'].hour, 13)

    @patch.object(YandexMarket, 'request')
    def test_older_review_can_be_requested_by_id_without_date_window(self, request):
        thread = self.make_thread('10', account=self.ym)
        request.return_value = {'result': {'feedbacks': [copy.deepcopy(YM_ROW)]}}
        incoming = YandexMarket(self.ym).fetch_one(thread)
        self.assertEqual(incoming.source_data, YM_ROW)
        self.assertEqual(request.call_args.kwargs['json'], {'feedbackIds': [10]})

    def test_backfill_is_bounded_and_preserves_old_threads(self):
        self.make_thread('old-two')
        third = self.make_thread('old-three')
        adapter = Mock()
        def incoming(thread):
            row = copy.deepcopy(WB_ROW)
            row['id'], row['text'], row['pros'], row['cons'], row['answer'] = thread.external_id, thread.text, '', '', None
            return Wildberries(self.account).parse(row, 'review')
        adapter.fetch_one.side_effect = incoming
        self.assertEqual(enrich_account(self.account, adapter, limit=2), 2)
        self.assertEqual(adapter.fetch_one.call_count, 2)
        self.assertEqual(Thread.objects.filter(source_synced_at__isnull=False).count(), 2)
        third.refresh_from_db()
        self.assertIsNone(third.source_synced_at)
        self.assertEqual(Thread.objects.count(), 3)

    def test_missing_record_does_not_block_other_old_reviews(self):
        other = self.make_thread('old-two')
        row = copy.deepcopy(WB_ROW)
        row['id'] = other.external_id
        adapter = Mock()
        adapter.fetch_one.side_effect = [IntegrationError('API: HTTP 404'), Wildberries(self.account).parse(row, 'review')]
        self.assertEqual(enrich_account(self.account, adapter), 1)
        self.thread.refresh_from_db()
        other.refresh_from_db()
        self.assertIn('404', self.thread.details_error)
        self.assertGreater(self.thread.details_next_attempt, timezone.now() + timedelta(hours=23))
        self.assertIsNotNone(other.source_synced_at)

    @patch.object(YandexMarket, 'request')
    def test_recent_history_refresh_uses_due_time_instead_of_waiting_a_day(self, request):
        thread = self.yandex_thread()
        Thread.objects.filter(pk=thread.pk).update(history_synced_at=timezone.now() - timedelta(minutes=16), details_next_attempt=timezone.now() - timedelta(seconds=1))
        request.return_value = {'result': {'comments': []}}
        self.assertEqual(enrich_account(self.ym, YandexMarket(self.ym)), 1)
        self.assertEqual(request.call_count, 1)

    @patch.object(YandexMarket, 'request')
    def test_initial_historical_comments_do_not_reopen_closed_dialogue(self, request):
        thread = self.yandex_thread()
        Thread.objects.filter(pk=thread.pk).update(status='Закрыто', closed_at=timezone.now())
        request.return_value = {'result': {'comments': [{'id': 7, 'text': 'Старый комментарий', 'status': 'PUBLISHED', 'author': {'type': 'USER', 'name': 'Покупатель'}}]}}
        enrich_account(self.ym, YandexMarket(self.ym))
        thread.refresh_from_db()
        self.assertEqual(thread.status, 'Закрыто')
        self.assertEqual(thread.messages.count(), 2)

    @patch.object(YandexMarket, 'request')
    def test_new_customer_comment_after_completed_history_reopens_dialogue(self, request):
        thread = self.yandex_thread()
        Thread.objects.filter(pk=thread.pk).update(status='Закрыто', closed_at=timezone.now() - timedelta(minutes=5), history_synced_at=timezone.now() - timedelta(minutes=10))
        request.return_value = {'result': {'comments': [{'id': 7, 'text': 'Новый комментарий', 'status': 'PUBLISHED', 'createdAt': timezone.now().isoformat(), 'author': {'type': 'USER', 'name': 'Покупатель'}}]}}
        enrich_account(self.ym, YandexMarket(self.ym))
        thread.refresh_from_db()
        self.assertEqual(thread.status, 'Новое')

    def test_partial_history_can_finish_after_restart_without_extra_api_call(self):
        thread = self.yandex_thread()
        Thread.objects.filter(pk=thread.pk).update(history_cursor={'complete': True})
        with patch.object(YandexMarket, 'request') as request:
            self.assertEqual(enrich_account(self.ym, YandexMarket(self.ym)), 1)
        request.assert_not_called()
        thread.refresh_from_db()
        self.assertIsNotNone(thread.history_synced_at)

    def test_backfill_migration_preserves_deadlines_and_other_platform_cursor(self):
        deadline = timezone.now() + timedelta(minutes=12)
        wb = SyncState.objects.create(account=self.account, cursor={'watermark': 123}, next_attempt=deadline)
        ym = SyncState.objects.create(account=self.ym, cursor={'keep': 'unchanged'}, next_attempt=deadline)
        migration = importlib.import_module('desk.migrations.0004_full_marketplace_content')
        migration.rewind_wb_for_full_content(apps, Mock(connection=Mock(alias='default')))
        wb.refresh_from_db()
        ym.refresh_from_db()
        self.assertEqual(wb.cursor, {})
        self.assertEqual(wb.next_attempt, deadline)
        self.assertEqual(ym.cursor, {'keep': 'unchanged'})
        self.assertTrue(Thread.objects.filter(pk=self.thread.pk).exists())


class FullContentWebTests(DeskCase):
    def setUp(self):
        super().setUp()
        self.client.force_login(self.user)

    def test_media_and_source_export_render_without_external_requests(self):
        thread, _ = ingest(self.account, Wildberries(self.account).parse(copy.deepcopy(WB_ROW), 'review'))
        response = self.client.get(f'/threads/{thread.pk}/')
        self.assertContains(response, 'Фото покупателя 1')
        self.assertContains(response, '<video controls', html=False)
        self.assertContains(response, 'Достоинства')
        download = self.client.get(f'/threads/{thread.pk}/source/')
        self.assertEqual(download.json()['source'], WB_ROW)
        self.assertEqual(download['Content-Disposition'], f'attachment; filename="review-{thread.pk}.json"')
        self.assertEqual(download['Cache-Control'], 'private, no-store')
        self.assertEqual(len(download.json()['messages']), 2)

    def test_untrusted_html_and_unsafe_media_are_not_executable(self):
        row = copy.deepcopy(WB_ROW)
        row['pros'] = '<script>alert(1)</script>'
        row['photoLinks'] = [{'fullSize': 'javascript:alert(1)'}]
        thread, _ = ingest(self.account, Wildberries(self.account).parse(row, 'review'))
        response = self.client.get(f'/threads/{thread.pk}/')
        self.assertNotContains(response, '<script>alert(1)</script>')
        self.assertNotContains(response, 'href="javascript:')
        self.assertNotContains(response, 'src="javascript:')
        self.assertContains(response, '&lt;script&gt;')

    def test_source_download_has_same_company_scope_as_thread(self):
        other = self.make_thread('private', account=self.other_account)
        self.assertEqual(self.client.get(f'/threads/{other.pk}/source/').status_code, 404)
        self.client.logout()
        self.assertEqual(self.client.get(f'/threads/{self.thread.pk}/source/').status_code, 302)

    def test_legacy_thread_explains_pending_enrichment(self):
        self.assertContains(self.client.get(f'/threads/{self.thread.pk}/'), 'Полные данные ещё не загружены')


class FullContentPollingTests(DeskCase):
    @patch.object(YandexMarket, 'request')
    def test_poll_imports_raw_data_and_history_without_reanalyzing_media_edits(self, request):
        from desk.ai.provider import analyze
        request.side_effect = [
            {'result': {'feedbacks': [copy.deepcopy(YM_ROW)]}}, {'result': {'questions': []}},
            {'result': {'comments': [{'id': 1, 'text': 'Ответ магазина', 'status': 'PUBLISHED', 'author': {'name': 'Магазин', 'type': 'BUSINESS'}},
                                     {'id': 2, 'text': 'Комментарий покупателя', 'status': 'PUBLISHED', 'author': {'name': 'Мария', 'type': 'USER'}}]}},
        ]
        with patch('desk.ai.provider.analyze', wraps=analyze) as ai:
            self.assertEqual(sync_account(self.ym), 1)
            thread = Thread.objects.get(account=self.ym, external_id='10')
            self.assertEqual(thread.source_data, YM_ROW)
            self.assertEqual(thread.messages.count(), 3)
            self.assertIsNotNone(thread.history_synced_at)
            SyncState.objects.filter(account=self.ym).update(next_attempt=timezone.now() - timedelta(seconds=1))
            edited = copy.deepcopy(YM_ROW)
            edited['media']['photos'].append('https://cdn.example.com/additional-photo.jpg')
            request.side_effect = [{'result': {'feedbacks': [edited]}}, {'result': {'questions': []}}]
            self.assertEqual(sync_account(self.ym), 1)
            ai.assert_called_once()
            thread.refresh_from_db()
            self.assertEqual(len(thread.source_data['media']['photos']), 2)
            self.assertEqual(thread.messages.count(), 3)

    @patch.object(Wildberries, 'request')
    def test_wb_archive_reread_fills_existing_record_without_duplicates(self, request):
        Thread.objects.filter(pk=self.thread.pk).update(external_id=WB_ROW['id'])
        request.side_effect = [{'data': {'feedbacks': [copy.deepcopy(WB_ROW)]}}, {'data': {'questions': []}},
                               {'data': {'feedbacks': []}}, {'data': {'questions': []}}]
        self.assertEqual(sync_account(self.account), 1)
        self.thread.refresh_from_db()
        self.assertEqual(Thread.objects.count(), 1)
        self.assertEqual(self.thread.source_data, WB_ROW)
        self.assertEqual(self.thread.messages.count(), 2)
        self.assertEqual(request.call_count, 4)
