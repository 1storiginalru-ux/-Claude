from datetime import timedelta
from django.utils import timezone
from desk.models import Baseline
from desk.voc.baselines import refresh_baselines
from .base import DeskCase


class HistoricalBaselineTests(DeskCase):
    def test_current_window_is_excluded_from_history(self):
        self.sales(100); self.sales(100, days_ago=20)
        self.analysis()
        old = self.make_thread('historical', received_at=timezone.now() - timedelta(days=20))
        self.analysis(old)
        self.assertEqual(refresh_baselines(self.product), 8)
        baseline = Baseline.objects.get(product=self.product, signal='LONG')
        self.assertEqual(baseline.sample_redemptions, 100)
        self.assertAlmostEqual(baseline.high, 2.96)
        self.assertEqual(baseline.source, 'historical')

    def test_manual_baseline_is_preserved(self):
        manual = self.baseline(high=7)
        self.sales(100); self.sales(100, days_ago=20)
        refresh_baselines(self.product)
        manual.refresh_from_db(); self.assertEqual(manual.high, 7)

    def test_insufficient_history_does_not_invent_baseline(self):
        self.sales(100)
        self.assertEqual(refresh_baselines(self.product), 0)
        self.assertFalse(Baseline.objects.exists())
