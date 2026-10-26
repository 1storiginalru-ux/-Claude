from datetime import timedelta
from django.utils import timezone
from desk.integrations.coverage import record_coverage
from desk.models import SyncState
from .base import DeskCase


class CoverageTests(DeskCase):
    def test_overlapping_completed_windows_accumulate_history(self):
        now = timezone.now()
        state = SyncState.objects.create(account=self.account)
        record_coverage(state, now - timedelta(days=7), now)
        record_coverage(state, now - timedelta(days=6), now + timedelta(days=1))
        state.refresh_from_db()
        self.assertEqual(state.coverage_since, now - timedelta(days=7))
        self.assertEqual(state.coverage_until, now + timedelta(days=1))

    def test_gap_does_not_claim_unfetched_history_is_complete(self):
        now = timezone.now()
        state = SyncState.objects.create(account=self.account, coverage_since=now - timedelta(days=7), coverage_until=now)
        record_coverage(state, now + timedelta(days=1), now + timedelta(days=8))
        state.refresh_from_db()
        self.assertEqual(state.coverage_since, now + timedelta(days=1))
