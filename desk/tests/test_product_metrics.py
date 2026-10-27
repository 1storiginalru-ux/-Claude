from datetime import timedelta
from django.utils import timezone
from desk.models import HistorySyncState, ProductMapping, SaleEvent, SalesDaily, SalesSyncState, SyncState
from desk.voc.rules import evaluate, grouped_signals, human_only, review_summary
from .base import DeskCase


class ProductMetricTests(DeskCase):
    def setUp(self):
        super().setUp()
        self.account.sales_enabled = True; self.account.save()
        ProductMapping.objects.filter(account=self.account).update(sku='123', external_product_id='123')
        ProductMapping.objects.filter(account=self.ym).delete()
        self.thread.sku = '123'; self.thread.save()
        self.now = timezone.now()
        since = self.now - timedelta(days=7)
        SalesSyncState.objects.create(account=self.account, coverage_since=since, coverage_until=self.now)
        SyncState.objects.create(account=self.account, coverage_since=since, coverage_until=self.now)
        HistorySyncState.objects.create(account=self.account, kind='reviews', coverage_since=since, coverage_until=self.now)

    def sales_events(self, count=100):
        SaleEvent.objects.bulk_create([SaleEvent(account=self.account, external_id=f'S{n}', sku='123', kind='sale', occurred_at=self.now - timedelta(minutes=n + 5), changed_at=self.now) for n in range(count)])

    def reviews(self, count=20, negative=5):
        for n in range(count):
            thread = self.thread if n == 0 else self.make_thread(f'negative-review-{n}', sku='123', received_at=self.now - timedelta(minutes=10))
            self.analysis(thread, sentiment='negative' if n < negative else 'positive', hidden_negative=n < negative,
                          signals=['LONG', 'AUTH-UNKNOWN'] if n < negative else [])

    def test_exact_last_100_and_two_different_negative_rates(self):
        self.sales_events(110); self.reviews()
        summary = review_summary(self.product, now=self.now)
        self.assertEqual(summary['period']['redemptions'], 100)
        self.assertTrue(summary['period']['exact'])
        self.assertEqual((summary['negative'], summary['total'], summary['review_rate'], summary['per100']), (5, 20, 25, 5))
        self.assertEqual(evaluate(self.product, 'LONG_AUTH', now=self.now)['count'], 5)

    def test_group_counts_both_topics_once_and_retains_human_only_rule(self):
        self.sales_events()
        raw = ['LONG', 'AUTH', 'AUTH-SCENT', 'AUTH-PACK']
        self.analysis(signals=raw)
        metric = evaluate(self.product, 'LONG_AUTH', now=self.now)
        self.assertEqual(metric['count'], 1)
        self.assertEqual(grouped_signals(raw), ['LONG_AUTH'])
        self.assertTrue(human_only({'signals': raw, 'severity': 'P2'}))
        self.thread.analysis.refresh_from_db()
        self.assertEqual(self.thread.analysis.signals, raw)

    def test_questions_and_seller_replies_do_not_inflate_negative_reviews(self):
        self.sales_events(); self.reviews(1, 1)
        question = self.make_thread('question', kind='question')
        self.analysis(question, signals=['LONG', 'AUTH-UNKNOWN'])
        from desk.models import Message
        Message.objects.create(thread=self.thread, external_id='seller-answer', direction='out', text='Ответ')
        summary = review_summary(self.product, now=self.now)
        self.assertEqual((summary['total'], summary['negative']), (1, 1))
        self.assertEqual(evaluate(self.product, 'LONG_AUTH', now=self.now)['count'], 1)

    def test_same_csv_sales_are_not_added_again_to_api_sales(self):
        self.sales_events()
        SalesDaily.objects.create(account=self.account, sku='123', date=self.now.date(), redemptions=100)
        self.reviews(1, 1)
        self.assertEqual(review_summary(self.product, now=self.now)['period']['redemptions'], 100)

    def test_returns_are_separate_and_do_not_erase_original_sale(self):
        self.sales_events(); self.reviews(1, 1)
        SaleEvent.objects.create(account=self.account, external_id='R1', sku='123', kind='return', occurred_at=self.now, changed_at=self.now)
        self.assertEqual(review_summary(self.product, now=self.now)['period']['redemptions'], 100)

    def test_fewer_than_100_never_claims_a_complete_100_sale_window(self):
        self.sales_events(20); self.reviews(1, 1)
        summary = review_summary(self.product, now=self.now)
        self.assertIsNone(summary['per100'])
        self.assertIsNone(summary['review_rate'])
        self.assertIn('Накоплено 20 из 100 выкупов', summary['period']['reasons'])

    def test_missing_feedback_history_does_not_report_a_false_zero(self):
        self.sales_events(); self.reviews(1, 0)
        SyncState.objects.filter(account=self.account).update(coverage_since=self.now - timedelta(minutes=1))
        HistorySyncState.objects.filter(account=self.account,kind='reviews').update(coverage_since=self.now - timedelta(minutes=1))
        summary = review_summary(self.product, now=self.now)
        self.assertIsNone(summary['review_rate'])
        self.assertIsNone(summary['per100'])

    def test_keyword_classification_is_not_a_confirmed_negative_or_positive(self):
        self.sales_events()
        self.analysis(provider='rules', confidence=0, signals=['PACK'])
        summary = review_summary(self.product, now=self.now)
        self.assertEqual((summary['classified'], summary['unclassified'], summary['preliminary']), (0, 1, 1))
        self.assertIsNone(summary['per100'])
        metric = evaluate(self.product, 'PACK', now=self.now)
        self.assertEqual(metric['count'], 0)
        self.assertIsNone(metric['rate'])

    def test_low_confidence_ai_result_is_pending_review(self):
        self.sales_events()
        self.analysis(provider='openai', confidence=0.4)
        self.assertEqual(review_summary(self.product, now=self.now)['unclassified'], 1)
        self.assertIsNone(evaluate(self.product, 'LONG_AUTH', now=self.now)['rate'])

    def test_combined_confirmed_cluster_still_alerts_without_a_full_sales_window(self):
        self.sales_events(20); self.reviews(4, 4)
        self.baseline(signal='LONG_AUTH', cluster_count=4)
        metric = evaluate(self.product, 'LONG_AUTH', now=self.now)
        self.assertTrue(metric['cluster'])
        self.assertEqual(metric['status'], 'ANOMALY')
        self.assertIsNone(metric['rate'])
        from desk.incidents.service import evaluate_incidents
        from desk.models import Incident
        evaluate_incidents(self.thread)
        self.assertEqual(list(Incident.objects.values_list('signal', flat=True)), ['LONG_AUTH'])

    def test_sales_and_reviews_use_the_same_completed_snapshot_during_refresh(self):
        self.sales_events(160)
        self.thread.received_at = self.now - timedelta(minutes=40); self.thread.save()
        self.analysis()
        newer = self.make_thread('not-in-snapshot', received_at=self.now - timedelta(minutes=10))
        self.analysis(newer)
        cutoff = self.now - timedelta(minutes=30)
        SalesSyncState.objects.filter(account=self.account).update(coverage_until=cutoff, cursor={'updating': True})
        summary = review_summary(self.product, now=self.now)
        self.assertEqual(summary['period']['until'], cutoff)
        self.assertEqual((summary['period']['redemptions'], summary['total'], summary['negative'], summary['per100']), (100, 1, 1, 1))

    def test_no_reviews_with_complete_coverage_has_zero_signals_not_a_review_percentage(self):
        self.sales_events()
        self.thread.delete()
        summary = review_summary(self.product, now=self.now)
        self.assertEqual(summary['per100'], 0)
        self.assertIsNone(summary['review_rate'])

    def test_seven_day_review_percentage_does_not_require_sales(self):
        self.reviews()
        summary = review_summary(self.product, window='7d', now=self.now)
        self.assertEqual(summary['review_rate'], 25)
        self.assertIsNone(summary['per100'])

    def test_missing_other_marketplace_is_visible_and_can_be_scoped_to_wb(self):
        self.sales_events(); self.reviews(1, 1)
        ProductMapping.objects.create(account=self.ym, product=self.product, sku='ym-sku')
        self.assertIsNone(review_summary(self.product, now=self.now)['per100'])
        self.assertEqual(review_summary(self.product, now=self.now, account_id=self.account.pk)['per100'], 1)

    def test_old_component_baselines_are_not_summed_for_new_group(self):
        self.sales_events(); self.reviews(1, 1)
        self.baseline(signal='LONG', high=3)
        self.baseline(signal='AUTH', high=4)
        metric = evaluate(self.product, 'LONG_AUTH', now=self.now)
        self.assertIsNone(metric['baseline'])
        self.assertEqual(metric['status'], 'WATCH')

    def test_product_page_and_filter_show_single_group(self):
        self.sales_events(); self.reviews(1, 1)
        self.client.force_login(self.user)
        response = self.client.get(f'/products/{self.product.pk}/')
        self.assertContains(response, 'Стойкость и оригинальность')
        self.assertContains(response, 'Последние 100 выкупов WB')
        self.assertContains(response, 'Негатив среди отзывов')
        filtered = self.client.get('/?signal=LONG_AUTH')
        self.assertEqual(filtered.context['page'].paginator.count, 1)
