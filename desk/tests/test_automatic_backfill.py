import tempfile
from datetime import timedelta
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
from .test_ai_queue import AIConfiguredCase, POSITIVE


class AutomaticBackfillTests(AIConfiguredCase):
    def enabled(self, account=None):
        return AIBackfillState.objects.create(account=account or self.account, enabled=True)

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
