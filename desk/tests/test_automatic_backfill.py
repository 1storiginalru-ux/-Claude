import tempfile
from datetime import datetime, timedelta
from io import StringIO
from pathlib import Path
from unittest.mock import patch
from django.core.management import call_command
from django.test import override_settings
from django.utils import timezone
from desk.ai.backfill import refill, QUEUE_LIMIT
from desk.ai.queue import enqueue, process_one
from desk.models import AIBackfillState, AIJob, AIRequest, Analysis, ReplyDraft, Outgoing, ReviewAssessment
from desk.reviews import fingerprint
from desk.ai.backfill_period import period_start, OUTSIDE_PERIOD
from .test_ai_queue import AIConfiguredCase, POSITIVE


class AutomaticBackfillTests(AIConfiguredCase):
    def enabled(self, account=None, months=24):
        return AIBackfillState.objects.create(account=account or self.account, enabled=True, months=months)

    def test_calendar_months_include_clamped_boundary_day_in_moscow(self):
        from zoneinfo import ZoneInfo
        moscow = ZoneInfo('Europe/Moscow')
        self.assertEqual(period_start(6, datetime(2026, 10, 31, 15, tzinfo=moscow)),
                         datetime(2026, 4, 30, 0, tzinfo=moscow))
        self.assertEqual(period_start(6, datetime(2024, 8, 31, 15, tzinfo=moscow)),
                         datetime(2024, 2, 29, 0, tzinfo=moscow))

    def test_only_reviews_inside_six_months_are_queued_including_boundary(self):
        now = timezone.now()
        cutoff = period_start(6, now)
        self.enabled(months=6)
        old = self.make_thread('too-old', received_at=cutoff-timedelta(microseconds=1))
        edge = self.make_thread('boundary', received_at=cutoff)
        future = self.make_thread('future', received_at=now+timedelta(days=1))
        refill()
        self.assertEqual(set(AIJob.objects.values_list('thread_id', flat=True)), {self.thread.pk, edge.pk})
        self.assertFalse(AIJob.objects.filter(thread__in=[old, future]).exists())

    @patch('desk.ai.queue.complete')
    def test_already_queued_out_of_period_job_is_cancelled_before_any_paid_call(self, post):
        state = self.enabled(months=24)
        self.thread.received_at = timezone.now()-timedelta(days=400); self.thread.save()
        refill(); job = AIJob.objects.get()
        state.months=6; state.save()
        process_one(); job.refresh_from_db()
        self.assertEqual(job.status, 'cancelled'); self.assertEqual(job.error, OUTSIDE_PERIOD)
        post.assert_not_called(); self.assertFalse(AIRequest.objects.exists())

    def test_recent_review_is_scanned_before_later_imported_old_review_and_resume_is_stable(self):
        state = self.enabled(months=6)
        old = self.make_thread('later-imported-old', received_at=timezone.now()-timedelta(days=90))
        with patch('desk.ai.backfill.QUEUE_LIMIT', 1):
            refill()
            self.assertEqual(AIJob.objects.get().thread_id, self.thread.pk)
            state.refresh_from_db(); self.assertEqual(state.before_received_at, self.thread.received_at)
            AIJob.objects.update(status='done')
            AIBackfillState.objects.update(next_attempt=timezone.now())
            refill()
        self.assertTrue(AIJob.objects.filter(thread=old).exists())

    def test_month_setting_cancels_old_background_jobs_and_retains_done_results_and_manual_jobs(self):
        state = self.enabled(months=24)
        old = self.make_thread('old-background', received_at=timezone.now()-timedelta(days=400))
        done = self.make_thread('old-done', received_at=old.received_at)
        self.analysis(done)
        refill()
        done_job = enqueue(done, classification_only=True, backfill=True)
        AIJob.objects.filter(pk=done_job.pk).update(status='done')
        manual = self.make_thread('old-manual', received_at=old.received_at)
        manual_job = enqueue(manual, classification_only=True)
        before = Analysis.objects.values().get(thread=done)
        with tempfile.TemporaryDirectory() as folder, override_settings(STATE_DIR=Path(folder)):
            call_command('analysis_backlog', '--account', self.account.name, months=6, enable=True, apply=True, stdout=StringIO())
        state.refresh_from_db(); self.assertEqual(state.months, 6)
        self.assertEqual(state.before_pk, 0); self.assertIsNone(state.before_received_at)
        self.assertEqual(AIJob.objects.get(thread=old).status, 'cancelled')
        done_job.refresh_from_db(); self.assertEqual(done_job.status, 'done')
        manual_job.refresh_from_db(); self.assertEqual(manual_job.status, 'queued')
        self.assertEqual(Analysis.objects.values().get(thread=done), before)

    def test_preview_months_does_not_change_scope_or_cancel_jobs(self):
        self.enabled(months=24)
        self.thread.received_at=timezone.now()-timedelta(days=400); self.thread.save()
        refill()
        out=StringIO()
        call_command('analysis_backlog','--account',self.account.name,months=6,stdout=out)
        self.assertIn('всего 0',out.getvalue())
        self.assertEqual(AIBackfillState.objects.get().months,24)
        self.assertEqual(AIJob.objects.get().status,'queued')

    def test_apply_cleans_legacy_queue_even_when_six_month_setting_already_matches(self):
        state=self.enabled(months=24)
        old=self.make_thread('legacy-old',received_at=timezone.now()-timedelta(days=400))
        enqueue(old,classification_only=True,backfill=True)
        state.months=6;state.save()
        with tempfile.TemporaryDirectory() as folder,override_settings(STATE_DIR=Path(folder)):
            call_command('analysis_backlog','--account',self.account.name,months=6,enable=True,apply=True,stdout=StringIO())
        self.assertEqual(AIJob.objects.get().status,'cancelled')
        self.assertEqual(AIBackfillState.objects.get().months,6)

    @patch('desk.ai.queue.complete')
    def test_already_queued_archive_jobs_are_processed_by_publication_date(self,post):
        self.enabled(months=6)
        old=self.make_thread('older-high-pk',received_at=timezone.now()-timedelta(days=90))
        enqueue(old,classification_only=True,backfill=True)
        recent=enqueue(self.thread,classification_only=True,backfill=True)
        post.return_value=self.answer(POSITIVE)
        process_one();recent.refresh_from_db()
        self.assertEqual(recent.status,'done')
        self.assertEqual(AIJob.objects.get(thread=old).status,'queued')

    @patch('desk.ai.queue.complete')
    def test_opt_in_scans_saved_reviews_without_inline_calls_and_never_rewrites_reply(self, post):
        self.enabled()
        draft = ReplyDraft.objects.create(thread=self.thread, text='Текст оператора', original_text='Текст оператора',
            method='manual', source_fingerprint='kept', config_fingerprint='kept')
        before = ReplyDraft.objects.values().get(pk=draft.pk)
        self.assertEqual(refill(), 1); post.assert_not_called()
        job = AIJob.objects.get(); self.assertTrue(job.snapshot['classification_only']); self.assertTrue(job.snapshot['backfill'])
        post.return_value = self.answer(POSITIVE)
        process_one(); self.assertEqual(post.call_count, 1)
        self.assertEqual(ReplyDraft.objects.values().get(pk=draft.pk), before)
        self.assertFalse(Outgoing.objects.exists())

    def test_default_off_and_unselected_cabinet_are_not_queued(self):
        AIBackfillState.objects.create(account=self.account)
        self.assertEqual(refill(), 0)
        self.assertFalse(AIJob.objects.exists())

    def test_current_and_deferred_human_assessments_and_trusted_ai_are_skipped(self):
        self.enabled(); self.analysis()
        for decision in ['pending', 'negative', 'non_negative']:
            t = self.make_thread(f'human-{decision}')
            ReviewAssessment.objects.create(thread=t, decision=decision, reviewed_by=self.user, source_fingerprint=fingerprint(t))
        self.assertEqual(refill(), 0); self.assertFalse(AIJob.objects.exists())

    def test_queue_is_bounded_and_resume_does_not_duplicate_jobs(self):
        state = self.enabled()
        for i in range(QUEUE_LIMIT + 3): self.make_thread(f'archive-{i}')
        self.assertEqual(refill(), QUEUE_LIMIT)
        state.refresh_from_db(); cursor = state.before_pk
        AIBackfillState.objects.update(next_attempt=timezone.now())
        self.assertEqual(refill(), 0); self.assertEqual(AIJob.objects.count(), QUEUE_LIMIT)
        state.refresh_from_db(); self.assertEqual(state.before_pk, cursor)
        AIJob.objects.update(status='done')
        AIBackfillState.objects.update(next_attempt=timezone.now())
        self.assertEqual(refill(), 4); self.assertEqual(AIJob.objects.count(), QUEUE_LIMIT + 4)

    @patch('desk.ai.queue.complete')
    def test_current_review_has_priority_over_older_background_jobs(self, post):
        self.enabled(); refill()
        current = self.make_thread('live-new', text='Спасибо')
        job = enqueue(current, classification_only=True)
        post.return_value = self.answer(POSITIVE)
        process_one(); job.refresh_from_db()
        self.assertEqual(job.status, 'done')
        self.assertEqual(AIJob.objects.get(thread=self.thread).status, 'queued')

    @patch('desk.ai.queue.complete')
    def test_terminal_error_is_not_retried_on_every_background_scan(self, post):
        self.enabled(); refill()
        AIJob.objects.update(status='failed', error='Нужна проверка подключения')
        AIBackfillState.objects.update(next_attempt=timezone.now(), before_pk=0)
        refill(); post.assert_not_called()
        self.assertEqual(AIJob.objects.get().status, 'failed')

    def test_authorization_failure_stops_new_archive_batches_until_access_is_restored(self):
        self.enabled(); refill()
        AIJob.objects.update(status='failed', error='AI API: HTTP 403.')
        self.make_thread('another-archive')
        AIBackfillState.objects.update(next_attempt=timezone.now(), before_pk=0)
        self.assertEqual(refill(), 0)
        self.assertEqual(AIJob.objects.count(), 1)
        AIRequest.objects.create(company=self.company, job=AIJob.objects.get(),
            month=timezone.localdate().replace(day=1), stage='classification', provider='yandex',
            model=self.company.ai_model, state='confirmed', budget_tokens=100)
        AIBackfillState.objects.update(next_attempt=timezone.now())
        self.assertEqual(refill(), 1)

    @patch('desk.ai.queue.complete')
    def test_existing_budget_blocks_history_without_creating_provider_calls(self, post):
        self.company.ai_token_limit = 1; self.company.save()
        self.enabled(); refill(); process_one()
        self.assertEqual(AIJob.objects.get().status, 'blocked')
        post.assert_not_called(); self.assertFalse(AIRequest.objects.exists())

    def test_completed_scan_wraps_to_find_an_edited_old_review(self):
        self.enabled(); self.analysis()
        self.assertEqual(refill(), 0)
        self.thread.text = 'Другой отзыв'; self.thread.save()
        Analysis.objects.filter(thread=self.thread).update(provider='rules', confidence=0)
        AIBackfillState.objects.update(next_attempt=timezone.now())
        self.assertEqual(refill(), 1)

    @patch('desk.ai.queue.complete')
    def test_old_archive_complaint_does_not_open_a_new_serious_case_outside_current_window(self, post):
        from desk.models import Incident
        self.thread.text = 'Не оригинал'; self.thread.rating = 1
        self.thread.received_at = timezone.now() - timedelta(days=400); self.thread.save()
        self.enabled(); refill()
        post.return_value = self.answer({**POSITIVE, 'sentiment': 'negative', 'signals': ['AUTH-UNKNOWN'],
            'severity': 'P1', 'objective_issue': True, 'requires_human': True})
        process_one()
        self.assertEqual(Analysis.objects.get().sentiment, 'negative')
        self.assertFalse(Incident.objects.exists())

    @patch('desk.ai.queue.complete')
    def test_saved_valid_checkpoint_repairs_a_legacy_confidence_reset_without_another_paid_call(self, post):
        self.enabled(); refill()
        post.return_value = self.answer(POSITIVE)
        process_one()
        Analysis.objects.update(confidence=0)
        AIBackfillState.objects.update(next_attempt=timezone.now(), before_pk=0)
        self.assertEqual(refill(), 1)
        process_one()
        self.assertEqual(post.call_count, 1)
        self.assertEqual(Analysis.objects.get().confidence, POSITIVE['confidence'])

    def test_enable_preview_apply_status_and_disable_preserve_budget_and_normal_jobs(self):
        args = ['--account', self.account.name]
        with tempfile.TemporaryDirectory() as folder, override_settings(STATE_DIR=Path(folder)):
            call_command('analysis_backlog', *args, enable=True, stdout=StringIO())
            self.assertFalse(AIBackfillState.objects.exists()); self.assertFalse(AIJob.objects.exists())
            call_command('analysis_backlog', *args, enable=True, apply=True, stdout=StringIO())
            refill()
            live = self.make_thread('new-live')
            normal = enqueue(live, classification_only=True)
            call_command('analysis_backlog', *args, disable=True, apply=True, stdout=StringIO())
            self.assertEqual(AIJob.objects.get(thread=self.thread).status, 'cancelled')
            normal.refresh_from_db(); self.assertEqual(normal.status, 'queued')
            call_command('analysis_backlog', *args, enable=True, apply=True, stdout=StringIO())
            self.assertEqual(AIJob.objects.get(thread=self.thread).status, 'queued')
        self.company.refresh_from_db(); self.assertEqual(self.company.ai_token_limit, 1000000)
