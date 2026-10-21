from datetime import timedelta
from django.utils import timezone
from desk.voc.rules import evaluate
from desk.incidents.service import evaluate_incidents, escalate
from desk.models import Incident, Product, ProductMapping
from .base import DeskCase


class RulesTests(DeskCase):
    def test_normal_rate_merges_accounts(self):
        self.sales(40); self.sales(60, self.ym); self.baseline()
        for n in range(3): self.analysis(self.thread if n == 0 else self.make_thread(str(n)))
        result = evaluate(self.product, 'LONG')
        self.assertEqual((result['count'], result['redemptions'], result['rate'], result['status']), (3, 100, 3.0, 'NORMAL'))

    def test_rate_above_baseline_is_anomaly(self):
        self.sales(); self.baseline()
        for n in range(9): self.analysis(self.thread if n == 0 else self.make_thread(str(n)))
        self.assertEqual(evaluate(self.product, 'LONG')['status'], 'ANOMALY')

    def test_one_signal_one_sale_is_not_mass_problem(self):
        self.sales(1); self.baseline(); self.analysis()
        result = evaluate(self.product, 'LONG')
        self.assertEqual(result['rate'], 100)
        self.assertEqual(result['status'], 'WATCH')

    def test_no_denominator_does_not_invent_a_rate(self):
        self.analysis(); self.baseline()
        self.assertIsNone(evaluate(self.product, 'LONG')['rate'])

    def test_boundary_day_includes_all_accounts(self):
        self.sales(70); self.sales(60, self.ym); self.analysis(); self.baseline()
        self.assertEqual(evaluate(self.product, 'LONG')['redemptions'], 130)

    def test_closed_window_excludes_old_and_future_signals(self):
        self.sales(); self.analysis(); self.baseline()
        self.analysis(self.make_thread('old', received_at=timezone.now() - timedelta(days=2)))
        self.analysis(self.make_thread('future', received_at=timezone.now() + timedelta(days=2)))
        self.assertEqual(evaluate(self.product, 'LONG')['count'], 1)

    def test_cluster_can_trigger_even_with_small_sample(self):
        self.sales(10); self.baseline(cluster_count=4)
        for n in range(4): self.analysis(self.thread if n == 0 else self.make_thread(str(n)))
        self.assertTrue(evaluate(self.product, 'LONG')['cluster'])
        self.assertEqual(evaluate(self.product, 'LONG')['status'], 'ANOMALY')

    def test_auth_subtypes_count_thread_once(self):
        self.sales(); self.analysis(signals=['AUTH', 'AUTH-SCENT', 'AUTH-PACK'])
        self.assertEqual(evaluate(self.product, 'AUTH')['count'], 1)

    def test_serious_objective_issue_escalates_without_sales(self):
        self.analysis(signals=['DEFECT'], severity='P1', objective_issue=True)
        evaluate_incidents(self.thread)
        self.assertEqual(Incident.objects.count(), 1)

    def test_incident_dedup_and_reopening(self):
        self.analysis()
        first = escalate(self.thread, 'LONG', self.user)
        other = self.make_thread('second'); self.analysis(other)
        self.assertEqual(escalate(other, 'LONG').pk, first.pk)
        self.assertEqual(first.threads.count(), 2)
        first.status = 'CLOSED'; first.save()
        self.assertNotEqual(escalate(other, 'LONG').pk, first.pk)

    def test_same_physical_product_merges_legal_entities_with_scoped_visibility(self):
        other_product = Product.objects.create(company=self.other, code=self.product.code, name=self.product.name, brand=self.product.brand, volume=self.product.volume, concentration=self.product.concentration)
        ProductMapping.objects.create(product=other_product, account=self.other_account, sku='sku1')
        self.sales(50); self.sales(50, self.other_account)
        other_thread = self.make_thread('other-company', account=self.other_account, product=other_product)
        self.analysis(); self.analysis(other_thread)
        self.assertEqual(evaluate(self.product, 'LONG')['redemptions'], 100)
        self.assertEqual(evaluate(self.product, 'LONG')['count'], 2)
        from desk.access import companies_for
        scoped = evaluate(self.product, 'LONG', scope=companies_for(self.user))
        self.assertEqual((scoped['redemptions'], scoped['count']), (50, 1))
        first = escalate(self.thread, 'LONG')
        self.assertEqual(escalate(other_thread, 'LONG').pk, first.pk)
