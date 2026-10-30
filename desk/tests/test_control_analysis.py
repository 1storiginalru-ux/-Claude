"""Independent VoC classification must never rewrite an operator's reply."""
from datetime import timedelta
from io import StringIO
from unittest.mock import patch
from django.core.management import call_command
from django.utils import timezone
from desk.ai.client import ProviderProblem
from desk.ai.context import prepare_classification
from desk.ai.queue import enqueue, process_one
from desk.models import AIJob, AIRequest, Analysis, Incident, ReplyDraft, ReplyMaterial, ReviewAssessment, Outgoing
from desk.replies.service import prepare_reply
from desk.reviews import effective_analysis, fingerprint
from .test_ai_queue import AIConfiguredCase, NEGATIVE, POSITIVE


class IndependentControlTests(AIConfiguredCase):
    def strict_draft(self):
        self.company.reply_sensitive_templates = True; self.company.save()
        self.thread.text = 'Не стойкий'; self.thread.rating = 2; self.thread.save()
        ReplyMaterial.objects.create(company=self.company, title='Строгий регламент', kind='template',
            approved=True, topic='LONG_AUTH', sentiment='negative', answer='Здравствуйте! Утверждённый текст.')
        return prepare_reply(self.thread, self.user)

    @patch('desk.ai.queue.complete')
    def test_one_classification_preserves_the_entire_edited_template(self, post):
        draft = self.strict_draft()
        draft.text += ' Правка оператора.'; draft.feedback = 'edited'; draft.edited_by = self.user; draft.save()
        before = ReplyDraft.objects.values().get(pk=draft.pk)
        post.return_value = self.answer(NEGATIVE)
        job = enqueue(self.thread, classification_only=True)
        self.assertEqual(enqueue(self.thread, classification_only=True).pk, job.pk)
        process_one(); job.refresh_from_db()
        self.assertEqual(job.status, 'done'); self.assertEqual(post.call_count, 1)
        self.assertEqual(ReplyDraft.objects.values().get(pk=draft.pk), before)
        self.assertEqual(ReplyDraft.objects.count(), 1)
        self.assertEqual(Analysis.objects.get(thread=self.thread).provider, 'yandex')
        self.assertEqual(Analysis.objects.get(thread=self.thread).draft, '')
        self.assertEqual(list(AIRequest.objects.values_list('stage', flat=True)), ['classification'])
        for private in ['tone_of_voice', 'facts', 'history', 'reply_library', 'operator_assessment']:
            self.assertNotIn(private, post.call_args.args[2])
        self.assertFalse(Outgoing.objects.exists()); self.assertFalse(process_one())

    @patch('desk.ai.queue.complete')
    def test_style_signature_and_library_changes_do_not_cancel_control(self, post):
        self.company.knowledge = 'PRIVATE_COMPANY_FACT'; self.company.save()
        job = enqueue(self.thread, classification_only=True)
        self.company.tone_of_voice = 'Другой стиль'; self.company.save()
        self.account.reply_signature = 'Новая подпись'; self.account.save()
        post.return_value = self.answer(POSITIVE)
        process_one(); job.refresh_from_db(); self.assertEqual(job.status, 'done')
        self.assertNotIn('PRIVATE_COMPANY_FACT', str(post.call_args))
        self.assertNotIn('tone_of_voice', job.snapshot)

    @patch('desk.ai.queue.complete')
    def test_changed_source_and_disabled_account_cancel_without_http(self, post):
        job = enqueue(self.thread, classification_only=True)
        self.thread.text = 'Отзыв изменён'; self.thread.save()
        process_one(); job.refresh_from_db(); self.assertEqual(job.status, 'cancelled')
        job = enqueue(self.thread, classification_only=True)
        self.account.enabled = False; self.account.save()
        process_one(); job.refresh_from_db(); self.assertEqual(job.status, 'cancelled')
        post.assert_not_called(); self.assertFalse(Analysis.objects.exists())

    @patch('desk.ai.queue.complete')
    def test_model_change_cancels_independent_context(self, post):
        job = enqueue(self.thread, classification_only=True)
        self.company.ai_model += '-new'; self.company.save()
        process_one(); job.refresh_from_db(); self.assertEqual(job.status, 'cancelled')
        post.assert_not_called()

    @patch('desk.ai.queue.complete')
    def test_budget_block_and_rate_limit_use_the_existing_queue(self, post):
        self.company.ai_token_limit = 1; self.company.save()
        job = enqueue(self.thread, classification_only=True)
        process_one(); job.refresh_from_db(); self.assertEqual(job.status, 'blocked')
        post.assert_not_called(); self.assertFalse(AIRequest.objects.exists())
        self.company.ai_token_limit = 1000000; self.company.save()
        enqueue(self.thread, classification_only=True, retry_failed=True)
        post.side_effect = ProviderProblem('HTTP 429', rejected=True, retryable=True, retry_after=120)
        process_one(); job.refresh_from_db(); self.assertEqual(job.status, 'retry')
        self.assertGreater(job.next_attempt, timezone.now() + timedelta(seconds=110))
        self.assertEqual(AIRequest.objects.get().budget_tokens, 0)
        AIJob.objects.filter(pk=job.pk).update(next_attempt=timezone.now())
        post.side_effect = None; post.return_value = self.answer(POSITIVE)
        process_one(); job.refresh_from_db(); self.assertEqual(job.status, 'done')
        self.assertFalse(ReplyDraft.objects.exists())

    @patch('desk.ai.queue.complete')
    def test_human_decision_during_http_wins_and_does_not_open_a_case(self, post):
        def respond(*args):
            self.assess('non_negative')
            return self.answer({**NEGATIVE, 'severity': 'P1', 'objective_issue': True})
        post.side_effect = respond
        job = enqueue(self.thread, classification_only=True)
        process_one(); job.refresh_from_db(); self.assertEqual(job.status, 'done')
        self.thread.refresh_from_db()
        self.assertEqual(effective_analysis(self.thread).provider, 'human')
        self.assertEqual(effective_analysis(self.thread).sentiment, 'neutral')
        self.assertFalse(Incident.objects.exists())

    @patch('desk.ai.queue.complete')
    def test_pending_human_decision_stays_excluded(self, post):
        self.assess('pending')
        post.return_value = self.answer(NEGATIVE)
        enqueue(self.thread, classification_only=True); process_one()
        self.thread.refresh_from_db(); self.assertIsNone(effective_analysis(self.thread))
        self.assertEqual(ReviewAssessment.objects.get().decision, 'pending')

    @patch('desk.ai.queue.complete')
    def test_independent_request_reuses_a_reply_job(self, post):
        self.responses(post)
        reply = enqueue(self.thread)
        self.assertEqual(enqueue(self.thread, classification_only=True).pk, reply.pk)
        process_one()
        self.assertEqual(enqueue(self.thread, classification_only=True).pk, reply.pk)
        self.assertEqual(AIJob.objects.count(), 1); self.assertEqual(post.call_count, 2)

    @patch('desk.ai.queue.complete')
    def test_reply_waits_for_control_and_reuses_its_classification(self, post):
        control = enqueue(self.thread, classification_only=True)
        AIJob.objects.filter(pk=control.pk).update(next_attempt=timezone.now() + timedelta(minutes=1))
        reply = enqueue(self.thread)
        self.assertEqual(reply.snapshot['classification_job'], control.pk)
        process_one(); reply.refresh_from_db()
        self.assertEqual(reply.status, 'retry'); self.assertEqual(reply.attempts, 0)
        post.assert_not_called()
        AIJob.objects.filter(pk=control.pk).update(next_attempt=timezone.now())
        self.responses(post)
        process_one(); control.refresh_from_db(); self.assertEqual(control.status, 'done')
        AIJob.objects.filter(pk=reply.pk).update(next_attempt=timezone.now())
        process_one(); reply.refresh_from_db(); self.assertEqual(reply.status, 'done')
        self.assertEqual(post.call_count, 2)
        self.assertEqual(list(AIRequest.objects.values_list('stage', flat=True)), ['classification', 'draft'])
        self.assertEqual(ReplyDraft.objects.count(), 1)

    @patch('desk.ai.queue.complete')
    def test_completed_control_can_be_used_by_a_later_reply_request(self, post):
        post.return_value = self.answer(POSITIVE)
        control = enqueue(self.thread, classification_only=True); process_one()
        post.return_value = self.answer({'draft': 'Спасибо!', 'used_fact_ids': []})
        reply = enqueue(self.thread); process_one(); reply.refresh_from_db()
        self.assertEqual(reply.status, 'done'); self.assertEqual(post.call_count, 2)
        control.refresh_from_db(); self.assertEqual(control.status, 'done')

    @patch('desk.ai.queue.complete')
    def test_lost_lease_does_not_save_a_billable_stale_response(self, post):
        job = enqueue(self.thread, classification_only=True)
        def respond(*args):
            AIJob.objects.filter(pk=job.pk).update(status='cancelled', lease_token=None, lease_until=None)
            return self.answer(POSITIVE)
        post.side_effect = respond
        process_one()
        self.assertFalse(Analysis.objects.exists()); self.assertFalse(ReplyDraft.objects.exists())
        self.assertEqual(AIRequest.objects.get().state, 'confirmed')

    def test_history_preview_selects_only_unclassified_reviews_and_never_enqueues(self):
        self.analysis()
        pending = self.make_thread('human-pending')
        ReviewAssessment.objects.create(thread=pending, decision='pending', reviewed_by=self.user,
                                       source_fingerprint=fingerprint(pending))
        target = self.make_thread('unclassified')
        self.make_thread('another-product', product=None)
        out = StringIO()
        options = dict(account=self.account.name, product=self.product.code, classification_only=True, stdout=out)
        call_command('enqueue_ai', **options)
        self.assertIn(f'{target.pk}:', out.getvalue())
        self.assertIn('Выбрано отзывов: 1', out.getvalue())
        self.assertFalse(AIJob.objects.exists()); self.assertFalse(AIRequest.objects.exists())
        call_command('enqueue_ai', apply=True, **options)
        self.assertEqual(AIJob.objects.get().thread_id, target.pk)
        self.assertTrue(AIJob.objects.get().snapshot['classification_only'])

    @patch('desk.ai.queue.complete')
    def test_return_to_a_previous_review_revision_restores_cached_classification_without_billing(self, post):
        post.return_value = self.answer(POSITIVE)
        job = enqueue(self.thread, classification_only=True); process_one()
        # A new revision may temporarily replace Analysis with local rules. If
        # the buyer restores a previously analysed text, reuse that checkpoint.
        Analysis.objects.filter(thread=self.thread).update(provider='rules', source_fingerprint='', confidence=0)
        restored = enqueue(self.thread, classification_only=True)
        self.assertEqual(restored.pk, job.pk); self.assertEqual(restored.status, 'queued')
        process_one()
        self.assertEqual(post.call_count, 1)
        self.assertEqual(Analysis.objects.get(thread=self.thread).provider, 'yandex')

    @patch('desk.ai.queue.complete')
    def test_explicit_forced_control_reanalysis_does_not_reuse_a_completed_reply(self, post):
        self.responses(post)
        enqueue(self.thread); process_one()
        before = list(ReplyDraft.objects.values())
        post.side_effect = [self.answer(POSITIVE)]
        control = enqueue(self.thread, classification_only=True, force=True)
        process_one(); control.refresh_from_db()
        self.assertEqual(control.status, 'done'); self.assertEqual(post.call_count, 3)
        self.assertEqual(list(ReplyDraft.objects.values()), before)

    @patch('desk.ai.queue.complete')
    def test_generation_failure_keeps_completed_product_classification(self, post):
        control = enqueue(self.thread, classification_only=True)
        reply = enqueue(self.thread)
        post.side_effect = [self.answer(POSITIVE), ProviderProblem('Draft API unavailable', retryable=True)]
        process_one(); process_one()
        control.refresh_from_db(); reply.refresh_from_db()
        self.assertEqual(control.status, 'done'); self.assertEqual(reply.status, 'retry')
        analysis = Analysis.objects.get(thread=self.thread)
        self.assertEqual(analysis.provider, 'yandex'); self.assertEqual(analysis.sentiment, 'positive')
        self.assertTrue(analysis.evidence['classification_only'])
        self.assertFalse(ReplyDraft.objects.exists()); self.assertFalse(Outgoing.objects.exists())

    @patch('desk.ai.queue.complete')
    def test_profile_change_cancels_reply_but_not_product_classification(self, post):
        control = enqueue(self.thread, classification_only=True)
        reply = enqueue(self.thread)
        self.company.knowledge = 'Новые знания компании'; self.company.save()
        post.return_value = self.answer(POSITIVE)
        process_one(); process_one()
        control.refresh_from_db(); reply.refresh_from_db()
        self.assertEqual(control.status, 'done'); self.assertEqual(reply.status, 'cancelled')
        self.assertEqual(post.call_count, 1)
        self.assertEqual(Analysis.objects.get(thread=self.thread).provider, 'yandex')
