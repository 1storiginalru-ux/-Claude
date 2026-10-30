from datetime import timedelta
from unittest.mock import patch
from unittest import skipUnless
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from django.contrib.auth import get_user_model
from django.db import connection
from django.test import Client
from django.test import TransactionTestCase
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from desk.forms import NegativeRuleForm
from desk.models import Analysis, Audit, Baseline, HistorySyncState, Incident, Membership, Product, ProductMapping, ReviewAssessment, SaleEvent, SalesDaily, SalesSyncState, SyncState
from desk.reviews import assess_review, effective_analysis, fingerprint
from desk.voc.dataset import ControlDataset
from desk.voc.rules import evaluate, review_summary
from desk.incidents.service import evaluate_incidents, escalate, recheck_products
from .base import DeskCase


class ReviewControlCase(DeskCase):
    def setUp(self):
        super().setUp()
        self.account.sales_enabled = True; self.account.save()
        ProductMapping.objects.filter(account=self.ym).delete()
        self.now = timezone.now()
        SalesSyncState.objects.create(account=self.account, coverage_since=self.now - timedelta(days=7), coverage_until=self.now)
        SyncState.objects.create(account=self.account, coverage_since=self.now - timedelta(days=7), coverage_until=self.now)
        HistorySyncState.objects.create(account=self.account, kind='reviews', coverage_since=self.now - timedelta(days=7), coverage_until=self.now)
        self.client.force_login(self.user)

    def events(self, count=100, product=None, sku='sku1'):
        product = product or self.product
        SaleEvent.objects.bulk_create([SaleEvent(account=self.account, external_id=f'S{product.pk}-{i}', sku=sku, kind='sale', occurred_at=self.now - timedelta(minutes=i + 5), changed_at=self.now) for i in range(count)])

    def decision(self, thread=None, decision='negative', **kwargs):
        thread = thread or self.thread
        old = ReviewAssessment.objects.filter(thread=thread).first()
        data = {'decision': decision, 'signals': ['LONG_AUTH'], 'severity': 'P2', 'objective_issue': False, 'comment': 'Проверено', 'source_fingerprint': fingerprint(thread), 'assessment_version': old.reviewed_at.isoformat() if old else ''}
        data.update(kwargs)
        return data

    def confirm(self, thread=None, **kwargs):
        thread = thread or self.thread
        result = assess_review(thread.pk, self.user, self.decision(thread, **kwargs))
        if thread.pk == self.thread.pk: self.thread.refresh_from_db()
        return result

    def rule(self, **kwargs):
        data = {'signal': 'NEGATIVE', 'low': 5, 'high': 10, 'min_redemptions': 100, 'min_signals': 3, 'cluster_count': 100, 'cluster_days': 3}
        data.update(kwargs)
        return self.baseline(**data)


class HumanReviewTests(ReviewControlCase):
    def test_unreviewed_pending_and_confirmed_states_are_distinct(self):
        url = f'/threads/{self.thread.pk}/'
        response = self.client.get(url)
        self.assertContains(response, 'Оператор ещё не проверял')
        self.assertContains(response, 'начальные значения формы')
        self.assertNotContains(response, 'Решение отложено')
        self.confirm(decision='pending')
        response = self.client.get(url)
        self.assertContains(response, 'Решение отложено')
        self.assertNotContains(response, 'Оператор ещё не проверял')
        self.confirm()
        self.assertContains(self.client.get(url), 'Подтверждено сотрудником')

    def test_human_confirmation_works_without_ai_and_preserves_original(self):
        self.events(); original = self.analysis(provider='rules', confidence=0, signals=['LONG', 'AUTH-UNKNOWN'])
        self.confirm()
        self.assertEqual(review_summary(self.product)['per100'], 1)
        self.assertEqual(evaluate(self.product, 'LONG_AUTH')['count'], 1)
        original.refresh_from_db()
        self.assertEqual((original.provider, original.signals), ('rules', ['LONG', 'AUTH-UNKNOWN']))
        self.assertEqual(Audit.objects.filter(action='review.assess', actor=self.user).count(), 1)

    def test_manual_non_negative_overrides_ai_negative_in_metrics_and_inbox(self):
        self.events(); self.analysis(); self.confirm(decision='non_negative')
        self.assertEqual(review_summary(self.product)['per100'], 0)
        self.assertEqual(evaluate(self.product, 'LONG_AUTH')['count'], 0)
        self.assertEqual(self.client.get('/?negative=1').context['page'].paginator.count, 0)
        self.assertContains(self.client.get(f'/threads/{self.thread.pk}/'), 'Не негатив')

    def test_pending_is_not_silently_replaced_by_high_confidence_ai(self):
        self.events(); self.analysis(); self.confirm(decision='pending')
        self.assertIsNone(review_summary(self.product)['per100'])
        self.assertEqual(self.client.get('/?needs_review=1').context['page'].paginator.count, 1)
        self.assertIsNone(effective_analysis(self.thread))

    def test_modified_text_rating_or_mapping_invalidates_confirmation(self):
        self.events(); self.analysis(); self.confirm()
        for field, value in [('text', 'Покупатель изменил отзыв'), ('rating', 1), ('sku', 'different')]:
            with self.subTest(field=field):
                setattr(self.thread, field, value); self.thread.save()
                self.assertIsNone(review_summary(self.product)['per100'])
                self.assertContains(self.client.get(f'/threads/{self.thread.pk}/'), 'Прошлое подтверждение не участвует')
                self.confirm()
                self.assertEqual(review_summary(self.product)['per100'], 1)

    def test_ai_reanalysis_does_not_override_operator(self):
        self.events(); a = self.analysis(); self.confirm(decision='non_negative', signals=[])
        Analysis.objects.filter(pk=a.pk).update(sentiment='negative', confidence=1)
        self.assertEqual(review_summary(self.product)['negative'], 0)

    def test_stale_page_and_concurrent_decision_are_rejected(self):
        data = self.decision()
        self.confirm()
        with self.assertRaisesRegex(ValueError, 'Другой сотрудник'):
            assess_review(self.thread.pk, self.user, data)
        current = self.decision()
        self.thread.text = 'Новая редакция'; self.thread.save()
        response = self.client.post(f'/threads/{self.thread.pk}/assessment/', current)
        self.assertEqual(response.status_code, 409)
        self.assertContains(response, 'Отзыв изменился', status_code=409)

    def test_roles_cross_company_csrf_and_get_are_protected(self):
        url = f'/threads/{self.thread.pk}/assessment/'
        self.assertEqual(self.client.get(url).status_code, 405)
        secured = Client(enforce_csrf_checks=True); secured.force_login(self.user)
        self.assertEqual(secured.post(url, self.decision()).status_code, 403)
        for role in ['viewer', 'quality', 'investigator', 'operator']:
            Membership.objects.filter(user=self.user).update(role=role)
            response = self.client.post(url, self.decision())
            self.assertEqual(response.status_code, 403 if role == 'viewer' else 302)
        other = self.make_thread('private', account=self.other_account, product=None)
        self.assertEqual(self.client.post(f'/threads/{other.pk}/assessment/', self.decision(other)).status_code, 404)

    def test_question_and_invalid_values_do_not_create_decisions(self):
        question = self.make_thread('q', kind='question')
        self.assertEqual(self.client.post(f'/threads/{question.pk}/assessment/', self.decision(question)).status_code, 404)
        data = self.decision(signals=['IGNORE_RULES'])
        self.assertEqual(self.client.post(f'/threads/{self.thread.pk}/assessment/', data).status_code, 400)
        self.assertFalse(ReviewAssessment.objects.exists())

    def test_combined_manual_group_counts_once_and_comment_is_escaped(self):
        self.events(); self.confirm(signals=['LONG_AUTH', 'PACK'], comment='<script>bad()</script>')
        self.assertEqual(evaluate(self.product, 'LONG_AUTH')['count'], 1)
        self.assertEqual(review_summary(self.product)['negative'], 1)
        self.assertEqual(self.client.get('/?signal=LONG_AUTH').context['page'].paginator.count, 1)
        response = self.client.get(f'/threads/{self.thread.pk}/')
        self.assertNotContains(response, '<script>bad()')
        self.assertContains(response, '&lt;script&gt;')


class ControlQueueTests(ReviewControlCase):
    def test_bulk_calculations_match_individual_cards(self):
        self.events(150); self.analysis()
        dataset = ControlDataset([self.product], self.company.__class__.objects.filter(pk=self.company.pk), now=self.now)
        for window in ['20', '100', '7d', '90d']:
            with self.subTest(window=window):
                a = review_summary(self.product, window, now=self.now)
                b = review_summary(self.product, window, dataset=dataset)
                self.assertEqual(a, b)
                self.assertEqual(evaluate(self.product, 'LONG_AUTH', window, now=self.now), evaluate(self.product, 'LONG_AUTH', window, dataset=dataset))

    def test_missing_analysis_is_visible_in_queue_and_pending_filter(self):
        self.events()
        response = self.client.get('/voc/?filter=pending')
        self.assertEqual(response.context['product_page'].paginator.count, 1)
        self.assertContains(response, 'Недостаточно данных')
        self.assertContains(response, 'Проверить отзывы: 1')

    def test_incident_is_ranked_before_pagination(self):
        self.events(); self.analysis()
        for i in range(23):
            product = Product.objects.create(company=self.company, code=f'A{i:02}', name=f'Товар {i}')
            self.make_thread(f'rank-review-{i}', product=product)
        high = Product.objects.create(company=self.company, code='ZZZ', name='Важный товар')
        thread = self.make_thread('critical', product=high)
        self.analysis(thread)
        escalate(thread, 'LONG_AUTH')
        response = self.client.get('/voc/')
        self.assertEqual(response.context['product_page'].object_list[0]['product'].code, 'ZZZ')
        self.assertEqual(self.client.get('/voc/?filter=incidents').context['product_page'].paginator.count, 1)

    def test_scope_never_leaks_another_company_counts_or_names(self):
        self.events(); self.analysis()
        other = Product.objects.create(company=self.other, code=self.product.code, name=self.product.name)
        t = self.make_thread('secret', account=self.other_account, product=other, text='SECRET_BODY')
        self.analysis(t, signals=['DEFECT'], objective_issue=True, severity='P0')
        response = self.client.get('/voc/')
        row = response.context['product_page'].object_list[0]
        self.assertEqual(row['summary']['negative'], 1)
        self.assertNotContains(response, 'SECRET_BODY')
        self.assertNotContains(response, 'Other')

    def test_query_count_does_not_grow_per_product(self):
        self.events(); self.analysis()
        with CaptureQueriesContext(connection) as first:
            self.client.get('/voc/')
        for i in range(8):
            p = Product.objects.create(company=self.company, code=f'B{i}', name=f'Товар B{i}')
            self.make_thread(f'b{i}', product=p)
        with CaptureQueriesContext(connection) as many:
            self.client.get('/voc/')
        self.assertLessEqual(len(many), len(first) + 3)


class NegativeRuleTests(ReviewControlCase):
    def negatives(self, count):
        for i in range(count):
            self.analysis(self.thread if i == 0 else self.make_thread(f'neg-{i}'))

    def test_exact_threshold_creates_one_case_with_all_evidence(self):
        self.events(); self.rule(); self.negatives(10)
        recheck_products()
        item = Incident.objects.get(signal='NEGATIVE')
        self.assertEqual(item.threads.count(), 10)
        self.assertEqual((item.opening_snapshot['count'], item.opening_snapshot['rate']), (10, 10))
        recheck_products()
        self.assertEqual(Incident.objects.filter(signal='NEGATIVE').count(), 1)
        self.assertEqual(Audit.objects.filter(action='incident.linked').count(), 9)

    def test_warning_and_insufficient_history_do_not_create_cases(self):
        self.events(); self.rule(); self.negatives(6)
        metric = evaluate(self.product, 'NEGATIVE')
        self.assertEqual((metric['status'], metric['reason']), ('WATCH', 'Достигнут порог предупреждения'))
        recheck_products(); self.assertFalse(Incident.objects.exists())
        SaleEvent.objects.all().delete(); self.events(10)
        self.assertIsNone(evaluate(self.product, 'NEGATIVE')['rate'])
        recheck_products(); self.assertFalse(Incident.objects.exists())

    def test_similar_cluster_can_trigger_with_small_sample_but_mixed_topics_cannot(self):
        self.events(20); self.rule(cluster_count=4)
        for i, theme in enumerate(['LONG', 'PACK', 'MARK', 'SCENT']):
            self.analysis(self.thread if i == 0 else self.make_thread(f'mix-{i}'), signals=[theme])
        self.assertFalse(evaluate(self.product, 'NEGATIVE')['cluster'])
        Analysis.objects.update(signals=['LONG', 'AUTH-UNKNOWN'])
        self.assertTrue(evaluate(self.product, 'NEGATIVE')['cluster'])
        recheck_products(); self.assertEqual(Incident.objects.get().signal, 'NEGATIVE')

    def test_confirmed_cluster_works_even_when_no_sales_have_arrived(self):
        self.rule(cluster_count=4); self.negatives(4)
        metric = evaluate(self.product, 'NEGATIVE')
        self.assertEqual((metric['status'], metric['cluster_count']), ('ANOMALY', 4))
        self.assertIsNone(metric['rate'])
        recheck_products()
        case = Incident.objects.get(signal='NEGATIVE')
        self.assertEqual(case.threads.count(), 4)
        self.assertEqual(case.opening_snapshot['cluster_count'], 4)
        self.assertContains(self.client.get('/voc/?filter=attention'), 'Серия похожих жалоб: 4')

    def test_incomplete_classification_or_coverage_blocks_percentage_trigger(self):
        self.events(); self.rule(); self.negatives(10)
        self.make_thread('not-classified')
        recheck_products(); self.assertFalse(Incident.objects.exists())
        SyncState.objects.update(coverage_since=self.now)
        self.assertIsNone(evaluate(self.product, 'NEGATIVE')['rate'])

    def test_close_is_preserved_and_new_evidence_can_open_new_case(self):
        self.events(); self.rule(); self.negatives(10)
        recheck_products(); item = Incident.objects.get()
        snapshot = dict(item.opening_snapshot)
        item.status = 'CLOSED'; item.save()
        recheck_products(); self.assertEqual(Incident.objects.count(), 1)
        new = self.make_thread('new-negative'); self.analysis(new)
        evaluate_incidents(new)
        self.assertEqual(Incident.objects.count(), 2)
        item.refresh_from_db(); self.assertEqual(item.opening_snapshot, snapshot)

    def test_rule_setup_requires_quality_role_and_valid_thresholds(self):
        data = {'warning': 5, 'threshold': 10, 'min_redemptions': 100, 'min_signals': 3, 'cluster_count': 4, 'cluster_days': 3}
        url = f'/products/{self.product.pk}/rules/'
        self.assertEqual(self.client.post(url, data).status_code, 403)
        Membership.objects.filter(user=self.user).update(role='quality')
        self.assertEqual(self.client.post(url, data).status_code, 302)
        self.assertEqual(Baseline.objects.get(signal='NEGATIVE').high, 10)
        self.assertFalse(NegativeRuleForm({**data, 'threshold': 2}).is_valid())
        self.assertFalse(NegativeRuleForm({**data, 'min_redemptions': 10}).is_valid())
        self.assertContains(self.client.get(f'/products/{self.product.pk}/'), 'Правило общего негатива')

    def test_account_problem_is_not_hidden_by_other_marketplace_sales(self):
        self.events(); self.rule(); self.negatives(10)
        ProductMapping.objects.create(account=self.ym, product=self.product, sku='ym-sku')
        SalesDaily.objects.create(account=self.ym, sku='ym-sku', date=timezone.localdate(self.now), redemptions=200)
        SyncState.objects.create(account=self.ym, coverage_since=self.now - timedelta(days=7), coverage_until=self.now)
        dataset = ControlDataset([self.product], self.company.__class__.objects.filter(pk=self.company.pk))
        self.assertEqual(evaluate(self.product, 'NEGATIVE', dataset=dataset)['status'], 'NORMAL')
        self.assertEqual(evaluate(self.product, 'NEGATIVE', account_id=self.account.pk, dataset=dataset)['status'], 'ANOMALY')
        recheck_products()
        item = Incident.objects.get(signal='NEGATIVE')
        self.assertEqual(item.opening_snapshot['account_id'], self.account.pk)
        self.assertEqual(item.opening_snapshot['rate'], 10)
        self.assertContains(self.client.get('/voc/'), 'WB1: 10 / 100')

    def test_old_long_and_auth_rules_do_not_create_two_incidents_for_combined_group(self):
        self.events(); self.negatives(10)
        self.baseline(signal='LONG', high=1)
        self.baseline(signal='AUTH', high=1)
        recheck_products()
        self.assertFalse(Incident.objects.exists())

    def test_get_queue_is_read_only_even_when_threshold_is_exceeded(self):
        self.events(); self.rule(); self.negatives(10)
        response = self.client.get('/voc/?filter=attention')
        self.assertEqual(response.context['product_page'].paginator.count, 1)
        self.assertFalse(Incident.objects.exists())

    def test_rate_uses_full_precision_at_threshold_boundary(self):
        self.account.sales_enabled = False; self.account.save()
        SalesDaily.objects.create(account=self.account, sku='sku1', date=timezone.localdate(self.now), redemptions=129)
        self.rule(low=0.7755, high=0.776, min_signals=1)
        self.analysis()
        # 100 / 129 = 0.77519..., displayed as 0.78; rounding must not trigger the 0.776 rule.
        metric = evaluate(self.product, 'NEGATIVE')
        self.assertEqual((metric['rate'], metric['status']), (0.78, 'NORMAL'))


class IncidentWorkflowTests(ReviewControlCase):
    def setUp(self):
        super().setUp()
        self.events(); self.analysis()
        self.case = escalate(self.thread, 'LONG_AUTH')
        Membership.objects.filter(user=self.user).update(role='quality')

    def payload(self, **kwargs):
        self.case.refresh_from_db()
        data = {'version': self.case.updated_at.isoformat(), 'status': 'CONTROL', 'owner': self.user.pk, 'due_at': '2026-11-01T12:00', 'hypothesis': 'Проверяем партию', 'actions': 'Сверить документы', 'result': 'Проверка продолжается'}
        data.update(kwargs)
        return data

    def test_owner_deadline_russian_status_and_snapshot_are_preserved(self):
        snapshot = dict(self.case.opening_snapshot)
        response = self.client.post(f'/incidents/{self.case.pk}/', self.payload())
        self.assertEqual(response.status_code, 302)
        self.case.refresh_from_db()
        self.assertEqual(self.case.owner_id, self.user.pk)
        self.assertEqual(timezone.localtime(self.case.due_at).hour, 12)
        self.assertEqual(self.case.opening_snapshot, snapshot)
        response = self.client.get(f'/incidents/{self.case.pk}/')
        self.assertContains(response, 'Контроль')
        self.assertContains(response, 'Почему открыто расследование')

    def test_viewer_cannot_edit_and_wrong_company_owner_is_rejected(self):
        stranger = get_user_model().objects.create_user('stranger')
        Membership.objects.create(user=stranger, company=self.other, role='quality')
        self.assertEqual(self.client.post(f'/incidents/{self.case.pk}/', self.payload(owner=stranger.pk)).status_code, 404)
        Membership.objects.filter(user=self.user).update(role='viewer')
        self.assertEqual(self.client.post(f'/incidents/{self.case.pk}/', self.payload()).status_code, 403)

    def test_close_requires_result_and_stale_update_is_rejected(self):
        url = f'/incidents/{self.case.pk}/'
        self.client.post(url, self.payload(status='CLOSED', result=''))
        self.case.refresh_from_db(); self.assertNotEqual(self.case.status, 'CLOSED')
        old = self.payload()
        self.client.post(url, old)
        self.client.post(url, {**old, 'result': 'STALE_RESULT'})
        self.case.refresh_from_db(); self.assertNotEqual(self.case.result, 'STALE_RESULT')
        self.client.post(url, self.payload(status='CLOSED', result='Документы и партия проверены'))
        self.case.refresh_from_db(); self.assertEqual(self.case.status, 'CLOSED')

    def test_partial_company_access_hides_global_snapshot(self):
        other = Product.objects.create(company=self.other, code=self.product.code, name=self.product.name)
        snapshot = dict(self.case.opening_snapshot)
        snapshot.update(company_ids=[self.company.pk, self.other.pk], reason='PRIVATE_AGGREGATE_REASON')
        self.case.opening_snapshot = snapshot; self.case.save()
        response = self.client.get(f'/incidents/{self.case.pk}/')
        self.assertContains(response, 'Полный снимок включает компании за пределами вашего доступа')
        self.assertNotContains(response, 'PRIVATE_AGGREGATE_REASON')
        data = {'warning': 5, 'threshold': 10, 'min_redemptions': 100, 'min_signals': 3, 'cluster_count': 4, 'cluster_days': 3}
        self.assertEqual(self.client.post(f'/products/{self.product.pk}/rules/', data).status_code, 403)

    def test_review_text_is_visible_even_without_imported_messages(self):
        self.thread.messages.all().delete()
        self.assertContains(self.client.get(f'/threads/{self.thread.pk}/'), self.thread.text)

    def test_legacy_snapshot_is_not_presented_as_the_opening_trigger_or_rewritten(self):
        snapshot = dict(self.case.opening_snapshot)
        snapshot.pop('trigger')
        snapshot.update(count=0, redemptions=93, rate=None,
                        reason='Не все отзывы разобраны; предварительная разметка не подтверждает отсутствие жалоб')
        self.case.opening_snapshot = snapshot; self.case.save()
        response = self.client.get(f'/incidents/{self.case.pk}/')
        self.assertContains(response, 'Историческое основание открытия отдельно не сохранялось')
        self.assertContains(response, 'Показатели на момент открытия')
        self.assertContains(response, 'Учтённых негативных отзывов по теме в расчётном окне')
        self.case.refresh_from_db(); self.assertEqual(self.case.opening_snapshot, snapshot)

    def test_saved_opening_reason_survives_later_reclassification(self):
        before = dict(self.case.opening_snapshot)
        self.confirm(decision='non_negative', signals=[], severity='P3')
        self.case.refresh_from_db()
        self.assertEqual(self.case.opening_snapshot, before)
        self.assertEqual(self.case.status, 'INVESTIGATION')


class SeriousIncidentEvidenceTests(ReviewControlCase):
    def test_preliminary_or_low_confidence_analysis_cannot_open_a_serious_case(self):
        a = self.analysis(provider='rules', confidence=0, objective_issue=True, severity='P1')
        evaluate_incidents(self.thread); self.assertFalse(Incident.objects.exists())
        for provider, confidence in [('yandex', 0.69), ('demo', 1)]:
            Analysis.objects.filter(pk=a.pk).update(provider=provider, confidence=confidence)
            self.thread.refresh_from_db(); evaluate_incidents(self.thread)
            self.assertFalse(Incident.objects.exists())
        # A deliberate human escalation remains available without AI.
        case = escalate(self.thread, 'LONG_AUTH', self.user)
        self.assertEqual(case.opening_snapshot['trigger']['type'], 'manual')

    def test_serious_trigger_is_distinct_from_a_zero_count_in_the_sales_window(self):
        self.events()
        self.thread.received_at = self.now - timedelta(minutes=1); self.thread.save()
        SyncState.objects.update(coverage_until=self.now - timedelta(minutes=3))
        HistorySyncState.objects.update(coverage_until=self.now - timedelta(minutes=3))
        self.analysis(provider='yandex', objective_issue=True, severity='P1', summary='Покупатель сообщил о проблеме')
        evaluate_incidents(self.thread)
        case = Incident.objects.get()
        snapshot = case.opening_snapshot
        self.assertEqual(snapshot['count'], 0)
        self.assertEqual(snapshot['trigger']['type'], 'serious')
        self.assertEqual(snapshot['trigger']['thread_id'], self.thread.pk)
        self.assertEqual(snapshot['trigger']['provider'], 'yandex')
        response = self.client.get(f'/incidents/{case.pk}/')
        self.assertContains(response, 'Серьёзная конкретная проблема по разметке отзыва')
        self.assertContains(response, 'не подтверждает дефект или подделку')
        self.assertContains(response, f'отзыв №{self.thread.pk}')
        self.assertNotContains(response, 'Историческое основание')


@skipUnless(connection.vendor == 'postgresql', 'Проверка блокировок PostgreSQL')
class ConcurrentHumanReviewTests(TransactionTestCase):
    def test_two_simultaneous_confirmations_do_not_deadlock_or_duplicate_cases(self):
        from django.db import close_old_connections, connections
        from desk.models import Account, Company, Thread
        now = timezone.now()
        company = Company.objects.create(name='Concurrent company')
        user = get_user_model().objects.create_user('concurrent-reviewer')
        Membership.objects.create(user=user, company=company, role='operator')
        account = Account.objects.create(company=company, name='Concurrent WB', platform='wb', token_env='UNUSED_CONCURRENT_TOKEN', sales_enabled=True)
        product = Product.objects.create(company=company, code='concurrent', name='Concurrent product')
        ProductMapping.objects.create(account=account, product=product, sku='123', external_product_id='123')
        SalesSyncState.objects.create(account=account, coverage_since=now-timedelta(days=7), coverage_until=now)
        SyncState.objects.create(account=account, coverage_since=now-timedelta(days=7), coverage_until=now)
        HistorySyncState.objects.create(account=account, kind='reviews', coverage_since=now-timedelta(days=7), coverage_until=now)
        SaleEvent.objects.bulk_create([SaleEvent(account=account, sku='123', external_id=f'S-concurrent-{i}', kind='sale', occurred_at=now-timedelta(minutes=i+5), changed_at=now) for i in range(100)])
        Baseline.objects.create(product=product, signal='NEGATIVE', low=5, high=10, min_redemptions=100, cluster_count=2, cluster_days=3)
        decisions = []
        for i in range(2):
            thread = Thread.objects.create(account=account, product=product, sku='123', external_id=f'concurrent-{i}', kind='review', text='Не держится аромат', due_at=now, received_at=now-timedelta(minutes=10))
            Analysis.objects.create(thread=thread, sentiment='negative', signals=['LONG'], severity='P2', confidence=.95, provider='openai', summary='Негатив')
            decisions.append((thread.pk, {'decision':'negative','signals':['LONG_AUTH'],'severity':'P2','objective_issue':False,'comment':'Проверено','source_fingerprint':fingerprint(thread),'assessment_version':''}))
        gate = Barrier(2)
        def confirm(item):
            close_old_connections()
            try:
                gate.wait(timeout=10)
                return assess_review(item[0], user, item[1]).pk
            finally:
                connections.close_all()
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(confirm, item) for item in decisions]
            for future in futures: future.result(timeout=20)
        self.assertEqual(ReviewAssessment.objects.count(), 2)
        self.assertEqual(Incident.objects.filter(signal='NEGATIVE').count(), 1)
        self.assertEqual(Incident.objects.get().threads.count(), 2)
