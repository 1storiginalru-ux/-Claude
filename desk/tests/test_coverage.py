from datetime import timedelta
import uuid
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

    def test_stale_instance_does_not_replace_concurrent_wider_coverage(self):
        now=timezone.now()
        state=SyncState.objects.create(account=self.account)
        stale=SyncState.objects.get(pk=state.pk)
        record_coverage(state,now-timedelta(days=30),now)
        record_coverage(stale,now-timedelta(days=7),now+timedelta(minutes=1))
        state.refresh_from_db()
        self.assertEqual(state.coverage_since,now-timedelta(days=30))
        self.assertEqual(state.coverage_until,now+timedelta(minutes=1))

    def test_expired_worker_cannot_record_coverage_under_a_new_lease(self):
        now=timezone.now()
        state=SyncState.objects.create(account=self.account,lease_token=uuid.uuid4())
        SyncState.objects.filter(pk=state.pk).update(lease_token=uuid.uuid4())
        record_coverage(state,now-timedelta(days=7),now)
        state.refresh_from_db()
        self.assertIsNone(state.coverage_since)
