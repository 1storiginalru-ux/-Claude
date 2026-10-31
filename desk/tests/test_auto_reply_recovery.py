"""Classification completing first must not lose an enabled incoming reply."""
from datetime import timedelta
from unittest.mock import Mock, patch
from django.urls import reverse
from django.utils import timezone
from desk.ai.client import ProviderProblem
from desk.ai.context import prepare, source_digest
from desk.ai.queue import process_one
from desk.ai.ratings import classify_rating, EMPTY_TEXT
from desk.integrations.base import Incoming
from desk.integrations.sync import sync_account
from desk.models import AIJob, AIRequest, Analysis, Outgoing, ReplyDraft, ReplyMaterial, ReplyRule, SyncState
from desk.replies.service import draft_current
from .test_ai_queue import AIConfiguredCase


class AutoReplyRecoveryTests(AIConfiguredCase):
    def setUp(self):
        super().setUp()
        self.company.ai_auto_enabled = True; self.company.save()
        self.thread.text = EMPTY_TEXT; self.thread.rating = 5
        self.thread.source_data = {'text': '', 'pros': '', 'cons': '', 'productValuation': 5}
        self.thread.save()
        self.original = classify_rating(self.thread)
        self.incoming = Incoming(self.thread.external_id, 'review', EMPTY_TEXT, self.thread.sku,
            rating=5, created_at=self.thread.received_at, source_data=self.thread.source_data)
        adapter = Mock(completed_window=None, fetch=Mock(return_value=[self.incoming]))
        self.adapter = patch('desk.integrations.sync.get_adapter', return_value=adapter)
        self.adapter.start(); self.addCleanup(self.adapter.stop)
        self.enrich = patch('desk.integrations.details.enrich_account', return_value=0)
        self.enrich.start(); self.addCleanup(self.enrich.stop)

    def poll(self):
        SyncState.objects.filter(account=self.account).update(next_attempt=timezone.now())
        sync_account(self.account)

    @patch('desk.ai.queue.complete')
    def test_rating_classified_first_still_gets_exactly_one_reply_job_and_one_draft(self, complete):
        before = Analysis.objects.values().get(thread=self.thread)
        self.assertFalse(AIJob.objects.exists())
        self.client.force_login(self.user)
        response = self.client.get(reverse('thread', args=[self.thread.pk]))
        composer = response.content.decode().split('<section class="panel composer">')[1].split('</section>')[0]
        self.assertIn('Черновик не подготовлен', composer)
        self.assertNotIn('По оценке, без AI', composer)
        self.poll(); self.poll()
        complete.assert_not_called()
        job = AIJob.objects.get()
        self.assertFalse(job.snapshot.get('classification_only'))
        self.assertEqual(Analysis.objects.values().get(thread=self.thread), before)
        complete.return_value = self.answer({'draft': 'Здравствуйте! Благодарим за оценку.', 'used_fact_ids': []})
        process_one()
        self.assertEqual(complete.call_count, 1)
        self.assertEqual(AIRequest.objects.get().stage, 'draft')
        draft = ReplyDraft.objects.get()
        self.assertTrue(draft_current(draft))
        self.assertIn('Благодарим', draft.text)
        response = self.client.get(reverse('thread', args=[self.thread.pk]))
        composer = response.content.decode().split('<section class="panel composer">')[1].split('</section>')[0]
        self.assertIn('<span class="pill neutral">AI</span>', composer)
        self.assertNotIn('По оценке, без AI', composer)
        self.poll()
        self.assertEqual(AIJob.objects.count(), 1)
        self.assertEqual(ReplyDraft.objects.count(), 1)
        self.assertFalse(Outgoing.objects.exists())

    @patch('desk.ai.queue.complete')
    def test_failure_does_not_get_automatic_paid_retries_on_next_polls(self, complete):
        self.poll()
        complete.side_effect = ProviderProblem('HTTP 403', rejected=True)
        process_one()
        job = AIJob.objects.get(); self.assertEqual(job.status, 'failed')
        self.poll(); self.poll()
        job.refresh_from_db()
        self.assertEqual(job.status, 'failed'); self.assertEqual(job.attempts, 1)
        self.assertEqual(complete.call_count, 1)
        self.assertEqual(AIRequest.objects.count(), 1)
        self.assertFalse(Outgoing.objects.exists())

    @patch('desk.ai.queue.complete')
    def test_saved_employee_draft_is_preserved(self, complete):
        source, config, _ = prepare(self.thread)
        draft = ReplyDraft.objects.create(thread=self.thread, method='manual', text='Ответ сотрудника',
            original_text='Ответ сотрудника', source_fingerprint=source, config_fingerprint=config)
        before = ReplyDraft.objects.values().get(pk=draft.pk)
        self.poll()
        self.assertEqual(ReplyDraft.objects.values().get(pk=draft.pk), before)
        self.assertFalse(AIJob.objects.exists()); complete.assert_not_called()

    def test_legacy_analysis_reply_is_preserved_without_a_new_job(self):
        Analysis.objects.filter(thread=self.thread).update(draft='Сохранённый старый ответ')
        self.poll()
        self.assertFalse(AIJob.objects.exists())
        self.assertEqual(Analysis.objects.get(thread=self.thread).draft, 'Сохранённый старый ответ')

    def test_disabled_account_or_company_does_not_start_recovery(self):
        self.account.ai_auto_enabled = False; self.account.save()
        self.poll(); self.assertFalse(AIJob.objects.exists())
        self.account.ai_auto_enabled = True; self.account.save()
        self.company.ai_auto_enabled = False; self.company.save()
        self.poll(); self.assertFalse(AIJob.objects.exists())

    def test_historical_classification_does_not_start_bulk_reply_generation(self):
        old = timezone.now() - timedelta(days=8)
        type(self.thread).objects.filter(pk=self.thread.pk).update(received_at=old)
        self.incoming.created_at = old
        self.poll()
        self.assertFalse(AIJob.objects.exists() or ReplyDraft.objects.exists())

    def test_current_text_classification_is_not_overwritten_or_requeued_for_analysis(self):
        self.incoming.text = self.thread.text = 'Прекрасный аромат'
        self.incoming.source_data = self.thread.source_data = {'text': 'Прекрасный аромат', 'pros': '', 'cons': '', 'productValuation': 5}
        self.thread.save()
        Analysis.objects.filter(thread=self.thread).update(provider='yandex', model=self.company.ai_model,
            confidence=.95, source_fingerprint=source_digest(self.thread))
        before = Analysis.objects.values().get(thread=self.thread)
        self.poll()
        self.assertEqual(Analysis.objects.values().get(thread=self.thread), before)
        self.assertFalse(AIJob.objects.get().snapshot.get('classification_only'))

    @patch('desk.ai.queue.complete')
    def test_classified_negative_uses_approved_template_without_a_paid_job(self, complete):
        self.company.reply_sensitive_templates = True; self.company.save()
        self.incoming.text = self.thread.text = 'Аромат совсем не держится'
        self.incoming.rating = self.thread.rating = 2
        self.incoming.source_data = self.thread.source_data = {'text': self.thread.text, 'productValuation': 2}
        self.thread.save()
        Analysis.objects.filter(thread=self.thread).update(provider='yandex', model=self.company.ai_model,
            sentiment='negative', signals=['LONG'], confidence=.95,
            source_fingerprint=source_digest(self.thread))
        material = ReplyMaterial.objects.create(company=self.company, title='Стойкость', kind='template',
            topic='LONG_AUTH', sentiment='negative', thread_kind='review', approved=True,
            answer='Здравствуйте! Поможем разобраться со стойкостью.')
        ReplyRule.objects.create(company=self.company, name='Стойкость', action='template', material=material,
            conditions={'kind': 'review', 'sentiment': 'negative', 'signals': ['LONG_AUTH']})
        self.poll(); self.poll()
        draft = ReplyDraft.objects.get()
        self.assertEqual(draft.method, 'template'); self.assertTrue(draft_current(draft))
        self.assertIn('Поможем разобраться', draft.text)
        complete.assert_not_called()
        self.assertFalse(AIJob.objects.exists() or AIRequest.objects.exists() or Outgoing.objects.exists())
