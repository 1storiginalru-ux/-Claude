import json
import tempfile
from datetime import timedelta
from io import StringIO
from pathlib import Path
from unittest.mock import patch
from django.core.management import call_command
from django.test import override_settings
from django.utils import timezone
from desk.ai.backfill import refill
from desk.ai.backfill_period import period_start
from desk.ai.context import source_digest
from desk.ai.queue import enqueue, process_one
from desk.ai.ratings import classify_rating, rating_result, EMPTY_TEXT
from desk.models import AIBackfillState, AIJob, AIRequest, Analysis, Audit, Outgoing, ReplyDraft, ReviewAssessment
from desk.reviews import effective_analysis, fingerprint
from desk.voc.rules import evaluate, trusted_analysis
from .test_ai_queue import AIConfiguredCase, POSITIVE


class RatingAnalysisTests(AIConfiguredCase):
    def blank(self, **kwargs):
        self.thread.kind = 'review'
        self.thread.account = self.account
        self.thread.text = EMPTY_TEXT
        self.thread.rating = 5
        self.thread.source_data = {'text': '', 'pros': '', 'cons': '', 'productValuation': 5}
        for key, value in kwargs.items(): setattr(self.thread, key, value)
        self.thread.save()
        return self.thread

    def test_all_ratings_are_classified_without_inventing_a_topic_or_defect(self):
        for rating in range(1, 6):
            with self.subTest(rating=rating):
                thread = self.blank(rating=rating, source_data={})
                result = classify_rating(thread)
                self.assertEqual(result.provider, 'rating')
                self.assertEqual(result.sentiment, 'negative' if rating <= 3 else 'positive')
                self.assertEqual(result.signals, ['OTHER'] if rating <= 3 else [])
                self.assertFalse(result.hidden_negative or result.objective_issue)
                self.assertIn('Причина оценки', result.summary)
                self.assertTrue(trusted_analysis(result))
        self.assertFalse(AIRequest.objects.exists() or Outgoing.objects.exists() or AIJob.objects.exists())

    def test_structured_yandex_empty_media_container_is_supported(self):
        thread = self.blank(account=self.ym, source_data={'description': {'comment': '', 'advantages': None},
            'statistics': {'rating': 5}, 'media': {'photos': [], 'videos': []}})
        self.assertIsNotNone(rating_result(thread))

    def test_text_and_ambiguous_or_richer_payloads_never_use_rating_shortcut(self):
        variants = [
            {'text': 'Нет стойкости'}, {'text': 'Спасибо'}, {'kind': 'question'}, {'rating': None},
            {'source_data': {'cons': 'Не оригинал'}}, {'source_data': {'pros': ['text']}},
            {'source_data': {'photoLinks': [{'fullSize': 'bad-url'}]}},
            {'source_data': {'video': {'id': 7}}}, {'source_data': {'bables': ['Слабый аромат']}},
            {'source_data': {'productValuation': 2}},
            {'account': self.ym, 'source_data': {'description': {'disadvantages': 'нет запаха'}}},
            {'account': self.ym, 'source_data': {'media': {'photos': ['https://example.com/photo']}}},
            {'account': self.ym, 'source_data': {'statistics': {'rating': 5, 'recommended': False}}},
            {'account': self.ym, 'source_data': {'description': 'bad'}},
        ]
        for variant in variants:
            with self.subTest(variant=variant):
                self.thread.account = self.account
                self.blank(**variant)
                self.assertIsNone(rating_result(self.thread))

    def test_correction_preserves_existing_drafts_and_is_audited_and_idempotent(self):
        thread = self.blank()
        old = self.analysis(thread, provider='yandex', confidence=.1, sentiment='neutral', signals=['OTHER'],
                            draft='Сохранённый текст ответа', evidence={'config': 'kept', 'facts': ['kept']})
        draft = ReplyDraft.objects.create(thread=thread, method='manual', text='Текст оператора',
            original_text='Текст оператора', source_fingerprint='kept', config_fingerprint='kept')
        draft_before = ReplyDraft.objects.values().get(pk=draft.pk)
        result = classify_rating(thread)
        self.assertEqual(result.pk, old.pk)
        self.assertEqual(result.draft, 'Сохранённый текст ответа')
        self.assertEqual(result.evidence['config'], 'kept')
        self.assertEqual(result.evidence['classification_basis']['kind'], 'rating_only')
        self.assertEqual(ReplyDraft.objects.values().get(pk=draft.pk), draft_before)
        self.assertEqual(Audit.objects.get(action='analysis.rating').before['confidence'], .1)
        classify_rating(thread)
        self.assertEqual(Audit.objects.filter(action='analysis.rating').count(), 1)

    def test_every_human_assessment_including_stale_or_pending_is_preserved(self):
        for decision in ['negative', 'non_negative', 'pending']:
            thread = self.blank()
            old = self.analysis(thread, provider='yandex', confidence=.1)
            assessment = ReviewAssessment.objects.create(thread=thread, decision=decision,
                reviewed_by=self.user, source_fingerprint='stale')
            before = Analysis.objects.values().get(pk=old.pk)
            self.assertIsNone(classify_rating(thread))
            self.assertEqual(Analysis.objects.values().get(pk=old.pk), before)
            assessment.delete()
            old.delete()

    def test_new_text_or_raw_media_invalidates_rating_verdict(self):
        thread = self.blank()
        classify_rating(thread)
        thread = type(thread).objects.select_related('analysis', 'account').get(pk=thread.pk)
        self.assertIsNotNone(effective_analysis(thread))
        thread.source_data['cons'] = 'Не оригинал'
        self.assertIsNone(effective_analysis(thread))
        thread.source_data = {'photoLinks': [{'fullSize': 'photo'}]}
        self.assertIsNone(effective_analysis(thread))
        thread.source_data = {}; thread.text = 'Нет стойкости'
        self.assertIsNone(effective_analysis(thread))

    def test_low_rating_counts_in_general_negative_but_not_authenticity(self):
        thread = self.blank(rating=2, source_data={})
        classify_rating(thread)
        self.assertEqual(evaluate(self.product, 'NEGATIVE', window='7d')['count'], 1)
        self.assertEqual(evaluate(self.product, 'LONG_AUTH', window='7d')['count'], 0)
        self.assertEqual(evaluate(self.product, 'DEFECT', window='7d')['count'], 0)

    @patch('desk.ai.queue.complete')
    def test_already_queued_classification_is_completed_without_provider_or_budget(self, post):
        thread = self.blank()
        old = self.analysis(thread, provider='yandex', confidence=.1, draft='Не стирать',
            source_fingerprint=source_digest(thread), evidence={'config': 'old-reply'})
        job = enqueue(thread, classification_only=True)
        AIJob.objects.filter(pk=job.pk).update(classification={**POSITIVE, 'draft': '', 'confidence': .1})
        process_one(); job.refresh_from_db(); old.refresh_from_db()
        self.assertEqual(job.status, 'done'); self.assertEqual(job.classification['confidence'], 1)
        self.assertEqual(old.provider, 'rating'); self.assertEqual(old.draft, 'Не стирать')
        self.assertEqual(old.evidence['config'], 'old-reply')
        post.assert_not_called(); self.assertFalse(AIRequest.objects.exists())

    @patch('desk.ai.queue.complete')
    def test_reply_still_uses_drafter_but_not_paid_classification(self, post):
        thread = self.blank()
        post.return_value = self.answer({'draft': 'Спасибо за оценку!', 'used_fact_ids': []})
        enqueue(thread); process_one()
        self.assertEqual(post.call_count, 1)
        self.assertEqual(AIRequest.objects.get().stage, 'draft')
        result = Analysis.objects.get(thread=thread)
        self.assertEqual(result.provider, 'rating'); self.assertEqual(result.draft, 'Спасибо за оценку!')
        self.assertFalse(Outgoing.objects.exists())

    @patch('desk.ai.queue.complete')
    def test_backfill_repairs_done_low_confidence_without_rebilling_or_consuming_slots(self, post):
        thread = self.blank()
        AIBackfillState.objects.create(account=self.account, enabled=True)
        job = enqueue(thread, classification_only=True, backfill=True)
        AIJob.objects.filter(pk=job.pk).update(status='done', classification={**POSITIVE, 'draft': '', 'confidence': .1})
        self.analysis(thread, provider='yandex', confidence=.1, source_fingerprint=source_digest(thread))
        self.assertEqual(refill(), 0)
        self.assertEqual(Analysis.objects.get(thread=thread).provider, 'rating')
        self.assertEqual(AIJob.objects.count(), 1)
        self.assertEqual(enqueue(thread, classification_only=True).status, 'done')
        post.assert_not_called(); self.assertFalse(AIRequest.objects.exists())

    def test_command_preview_apply_period_scope_backup_and_idempotence(self):
        thread = self.blank()
        self.analysis(thread, provider='yandex', confidence=.1)
        self.make_thread('outside', text=EMPTY_TEXT, rating=5, received_at=period_start(6)-timedelta(days=1))
        self.make_thread('future', text=EMPTY_TEXT, rating=5, received_at=timezone.now()+timedelta(days=1))
        self.make_thread('other-account', text=EMPTY_TEXT, rating=5, account=self.ym)
        pending = self.make_thread('manual', text=EMPTY_TEXT, rating=5)
        ReviewAssessment.objects.create(thread=pending, decision='pending', reviewed_by=self.user, source_fingerprint=fingerprint(pending))
        with tempfile.TemporaryDirectory() as folder, override_settings(STATE_DIR=Path(folder)):
            out = StringIO()
            call_command('classify_ratings', '--account', self.account.name, stdout=out)
            self.assertIn('для разбора по оценке: 1', out.getvalue())
            self.assertFalse(list(Path(folder).rglob('*')))
            self.assertEqual(Analysis.objects.get(thread=thread).provider, 'yandex')
            call_command('classify_ratings', '--account', self.account.name, apply=True, stdout=StringIO())
            backup = next((Path(folder)/'backups').glob('*.json'))
            self.assertEqual(backup.stat().st_mode & 0o777, 0o600)
            self.assertEqual(json.loads(backup.read_text())['analyses'][0]['confidence'], .1)
            self.assertEqual(Analysis.objects.filter(provider='rating').count(), 1)
            out = StringIO()
            call_command('classify_ratings', '--account', self.account.name, apply=True, stdout=out)
            self.assertIn('Изменения не требуются', out.getvalue())
            self.assertEqual(len(list((Path(folder)/'backups').glob('*.json'))), 1)
        self.assertFalse(AIJob.objects.exists() or AIRequest.objects.exists() or Outgoing.objects.exists())

    @patch('desk.ai.queue.complete')
    def test_real_longevity_disagreement_stays_below_trust_threshold(self, post):
        self.thread.text = 'Быстро выветриваем болеем'; self.thread.rating = 2; self.thread.save()
        post.return_value = self.answer({**POSITIVE, 'sentiment': 'negative', 'signals': ['SCENT']})
        enqueue(self.thread, classification_only=True); process_one()
        result = Analysis.objects.get(thread=self.thread)
        self.assertEqual(result.provider, 'yandex')
        self.assertEqual(result.confidence, .69)
        self.assertFalse(trusted_analysis(result))

    def test_rating_source_is_visible_in_card_without_ai_confidence_claim(self):
        from django.urls import reverse
        self.blank(); classify_rating(self.thread)
        self.client.force_login(self.user)
        response = self.client.get(reverse('thread', args=[self.thread.pk]))
        self.assertContains(response, 'По оценке, без AI')
        self.assertNotContains(response, 'confidence 1,00')

    @patch('desk.ai.queue.complete')
    def test_new_raw_attachment_cannot_reuse_completed_rating_checkpoint(self, post):
        thread = self.blank()
        first = enqueue(thread, classification_only=True); process_one()
        thread.source_data['photoLinks'] = [{'fullSize': 'https://example.com/photo'}]
        thread.save()
        post.return_value = self.answer({**POSITIVE, 'confidence': .5})
        second = enqueue(thread, classification_only=True)
        self.assertNotEqual(first.pk, second.pk)
        process_one()
        self.assertEqual(post.call_count, 1)
        result = Analysis.objects.get(thread=thread)
        self.assertEqual(result.provider, 'yandex')
        self.assertEqual(result.confidence, .5)
