import json
import os
import uuid
from datetime import timedelta
from io import StringIO
from unittest.mock import Mock, patch
import requests
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from unittest import skipUnless
from django.db import connection, connections, close_old_connections
from django.test import TransactionTestCase
from django.core.management import call_command
from django.core.management.base import CommandError
from django.urls import reverse
from django.utils import timezone
from desk.models import AIJob, AIRequest, Analysis, Company, KnowledgeEntry, Message, Outgoing, ReviewAssessment
from desk.ai.client import complete, list_yandex_models, ProviderProblem
from desk.ai.context import prepare, source_digest
from desk.ai.queue import enqueue, process_one
from desk.reviews import effective_analysis, fingerprint
from .base import DeskCase

POSITIVE = {'sentiment': 'positive', 'hidden_negative': False, 'signals': [], 'severity': 'P3',
            'objective_issue': False, 'requires_human': False, 'confidence': 0.94, 'summary': 'Покупатель доволен'}
NEGATIVE = {**POSITIVE, 'sentiment': 'negative', 'signals': ['LONG'], 'severity': 'P2', 'summary': 'Жалоба на стойкость'}


class AIConfiguredCase(DeskCase):
    def setUp(self):
        super().setUp()
        self.key = patch.dict(os.environ, {'TEST_AI_KEY': 'test-only-placeholder'})
        self.key.start(); self.addCleanup(self.key.stop)
        self.company.ai_provider = 'yandex'; self.company.ai_key_env = 'TEST_AI_KEY'
        self.company.ai_folder_id = 'folder-test'; self.company.ai_model = 'gpt://folder-test/alice-test/v1'
        self.company.save()
        self.thread.text = 'Отличный аромат, всё понравилось'; self.thread.save()

    def answer(self, data): return json.dumps(data, ensure_ascii=False), (120, 30), True

    def responses(self, post, classification=None, draft=None):
        post.side_effect = [self.answer(classification or POSITIVE), self.answer(draft or {'draft': 'Спасибо за отзыв! Рады, что аромат понравился.', 'used_fact_ids': []})]

    def assess(self, decision='non_negative', signals=None):
        return ReviewAssessment.objects.create(thread=self.thread, decision=decision, signals=signals or [],
            source_fingerprint=fingerprint(self.thread), reviewed_by=self.user)


class AIQueueTests(AIConfiguredCase):
    @patch('desk.ai.queue.complete')
    def test_two_stages_keep_style_out_of_classification_and_never_publish(self, post):
        self.company.knowledge = 'Подтверждённые правила упаковки.'; self.company.save()
        self.responses(post)
        job = enqueue(self.thread, self.user)
        self.assertTrue(process_one())
        job.refresh_from_db(); self.assertEqual(job.status, 'done')
        analysis = Analysis.objects.get(thread=self.thread)
        self.assertEqual(analysis.provider, 'yandex'); self.assertTrue(analysis.draft)
        self.assertEqual(analysis.source_fingerprint, source_digest(self.thread))
        self.assertEqual(post.call_count, 2)
        self.assertNotIn('PROFILE_A_ONLY', post.call_args_list[0].args[1])
        self.assertNotIn('tone_of_voice', post.call_args_list[0].args[2])
        self.assertEqual(post.call_args_list[1].args[2]['tone_of_voice'], 'PROFILE_A_ONLY')
        self.assertFalse(Outgoing.objects.exists())
        self.thread.refresh_from_db(); self.assertEqual(self.thread.status, 'Новое')
        self.assertEqual(list(AIRequest.objects.values_list('budget_tokens', flat=True)), [150, 150])

    @patch('desk.ai.queue.complete')
    def test_duplicate_click_reuses_result_without_another_paid_call(self, post):
        self.responses(post)
        first = enqueue(self.thread); self.assertEqual(enqueue(self.thread).pk, first.pk)
        process_one(); self.assertEqual(enqueue(self.thread).pk, first.pk)
        self.assertFalse(process_one()); self.assertEqual(post.call_count, 2)

    @patch('desk.ai.queue.complete')
    def test_long_and_auth_get_reviewable_drafts_without_publishing(self, post):
        for signal in ['LONG', 'AUTH-UNKNOWN', 'RETURN', 'MARK']:
            with self.subTest(signal=signal):
                self.thread.text = 'Не стойкий'; self.thread.save()
                self.company.ai_model += 'x'; self.company.save()
                post.reset_mock(); self.responses(post, classification={**NEGATIVE, 'signals': [signal]})
                enqueue(self.thread); process_one()
                self.assertEqual(post.call_count, 2)
                result = Analysis.objects.get(thread=self.thread)
                self.assertTrue(result.draft); self.assertTrue(result.requires_human)
                self.assertTrue(result.evidence['operator_notes']); self.assertFalse(Outgoing.objects.exists())

    @patch('desk.ai.queue.complete')
    def test_model_disagreement_with_strong_local_issue_requires_review(self, post):
        self.thread.text = 'Это подделка'; self.thread.save()
        self.responses(post)
        enqueue(self.thread); process_one()
        result = Analysis.objects.get(thread=self.thread)
        self.assertLess(result.confidence, 0.7); self.assertTrue(result.draft)
        self.assertTrue(result.requires_human); self.assertEqual(post.call_count, 2)
        self.assertFalse(Outgoing.objects.exists())

    @patch('desk.ai.queue.complete')
    def test_high_rating_complaint_is_hidden_negative(self, post):
        self.responses(post, classification=NEGATIVE)
        enqueue(self.thread); process_one()
        self.assertTrue(Analysis.objects.get(thread=self.thread).hidden_negative)

    @patch('desk.ai.queue.complete')
    def test_positive_no_disadvantages_stays_positive(self, post):
        self.thread.account = self.ym
        self.thread.text = 'Аромат, стойкость\nнет'
        self.thread.source_data = {'description': {'advantages': 'Аромат, стойкость', 'disadvantages': 'нет'}}
        self.thread.save(); self.responses(post)
        enqueue(self.thread); process_one()
        result = Analysis.objects.get(thread=self.thread)
        self.assertFalse(result.hidden_negative); self.assertEqual(result.sentiment, 'positive')
        self.assertEqual(post.call_args_list[0].args[2]['review_sections']['disadvantages'], 'нет')

    def test_retrieval_only_approved_matching_company_and_product_with_sources(self):
        def fact(**kw):
            return KnowledgeEntry.objects.create(**{'company': self.company, 'product_code': 'P1', 'title': 'Аромат',
                'content': 'TARGET_FACT', 'source': 'Документ поставщика', 'approved': True, **kw})
        target = fact()
        fact(company=self.other, content='OTHER_COMPANY_PRIVATE')
        fact(product_code='P2', content='WRONG_PRODUCT_PRIVATE')
        fact(approved=False, content='UNAPPROVED_PRIVATE')
        fact(active=False, content='INACTIVE_PRIVATE')
        fact(source='', content='UNSOURCED_PRIVATE')
        _, _, snapshot = prepare(self.thread)
        text = json.dumps(snapshot, ensure_ascii=False)
        self.assertIn('TARGET_FACT', text)
        for value in ['OTHER_COMPANY_PRIVATE', 'WRONG_PRODUCT_PRIVATE', 'UNAPPROVED_PRIVATE', 'INACTIVE_PRIVATE', 'UNSOURCED_PRIVATE', 'PROFILE_B_PRIVATE']:
            self.assertNotIn(value, text)
        self.assertTrue(snapshot['facts'][0]['id'].startswith(f'fact:{target.pk}:'))

    @patch('desk.ai.queue.complete')
    def test_unknown_citation_retries_without_saving_fabricated_draft(self, post):
        self.responses(post, draft={'draft': 'Выдуманный факт', 'used_fact_ids': ['not-supplied']})
        job = enqueue(self.thread); process_one(); job.refresh_from_db()
        self.assertEqual(job.status, 'retry'); self.assertTrue(job.classification)
        self.assertEqual(Analysis.objects.get(thread=self.thread).draft, '')

    @patch('desk.ai.queue.complete')
    def test_retry_draft_does_not_repeat_successful_classification(self, post):
        post.side_effect = [self.answer(POSITIVE), ProviderProblem('temporary', retryable=True, retry_after=120)]
        job = enqueue(self.thread); process_one(); job.refresh_from_db()
        self.assertEqual(job.status, 'retry'); self.assertTrue(job.classification)
        self.assertGreater(job.next_attempt, timezone.now() + timedelta(seconds=110))
        AIJob.objects.filter(pk=job.pk).update(next_attempt=timezone.now())
        post.side_effect = [self.answer({'draft': 'Спасибо!', 'used_fact_ids': []})]
        process_one(); job.refresh_from_db(); self.assertEqual(job.status, 'done')
        self.assertEqual(post.call_count, 3)
        self.assertEqual(AIRequest.objects.filter(state='uncertain').count(), 1)

    @patch('desk.ai.queue.complete')
    def test_permanent_auth_failure_does_not_loop(self, post):
        post.side_effect = ProviderProblem('HTTP 403', rejected=True)
        job = enqueue(self.thread); process_one(); job.refresh_from_db()
        self.assertEqual(job.status, 'failed'); self.assertFalse(process_one())
        self.assertEqual(AIRequest.objects.get().budget_tokens, 0)

    @patch('desk.ai.queue.complete')
    def test_token_limit_blocks_before_http(self, post):
        self.company.ai_token_limit = 1; self.company.save()
        job = enqueue(self.thread); process_one(); job.refresh_from_db()
        self.assertEqual(job.status, 'blocked'); post.assert_not_called()
        self.assertFalse(AIRequest.objects.exists()); self.assertEqual(job.attempts, 0)

    @patch('desk.ai.queue.complete')
    def test_actual_usage_replaces_conservative_reservation(self, post):
        self.responses(post)
        enqueue(self.thread); process_one()
        self.assertEqual(sum(AIRequest.objects.values_list('budget_tokens', flat=True)), 300)

    @patch('desk.ai.queue.complete')
    def test_missing_usage_keeps_a_conservative_budget_reservation(self, post):
        post.side_effect = [(json.dumps(NEGATIVE), None, True), (json.dumps({'draft': 'Здравствуйте! Поможем разобраться.', 'used_fact_ids': []}), None, True)]
        enqueue(self.thread); process_one()
        self.assertEqual(AIRequest.objects.count(), 2)
        for call in AIRequest.objects.all():
            self.assertEqual(call.state, 'uncertain'); self.assertGreater(call.budget_tokens, 1000)

    @patch('desk.ai.queue.complete')
    def test_interrupted_worker_is_recovered_without_losing_reservations(self, post):
        self.responses(post, classification=NEGATIVE)
        job = enqueue(self.thread)
        AIJob.objects.filter(pk=job.pk).update(status='running', lease_until=timezone.now() - timedelta(seconds=1), attempts=1)
        AIRequest.objects.create(company=self.company, job=job, month=timezone.localdate().replace(day=1),
            stage='classification', provider='yandex', model=self.company.ai_model, budget_tokens=1000)
        process_one(); job.refresh_from_db(); self.assertEqual(job.status, 'done')
        self.assertEqual(AIRequest.objects.filter(state='reserved', budget_tokens=1000).count(), 1)

    @patch('desk.ai.queue.complete')
    def test_changed_review_cancels_old_job_before_http(self, post):
        job = enqueue(self.thread)
        self.thread.text = 'Другой текст'; self.thread.save()
        process_one(); job.refresh_from_db(); self.assertEqual(job.status, 'cancelled'); post.assert_not_called()

    @patch('desk.ai.queue.complete')
    def test_change_during_http_cannot_commit_stale_verdict(self, post):
        def change(*args):
            self.thread.text = 'Новый текст'; self.thread.save()
            return self.answer(NEGATIVE)
        post.side_effect = change
        job = enqueue(self.thread); process_one(); job.refresh_from_db()
        self.assertEqual(job.status, 'cancelled'); self.assertFalse(Analysis.objects.exists())
        self.assertEqual(AIRequest.objects.get().state, 'confirmed')

    @patch('desk.ai.queue.complete')
    def test_revoked_knowledge_cancels_queued_context(self, post):
        fact = KnowledgeEntry.objects.create(company=self.company, product_code='P1', title='Факт', content='Аромат', source='Документ', approved=True)
        job = enqueue(self.thread); fact.approved = False; fact.save()
        process_one(); job.refresh_from_db(); self.assertEqual(job.status, 'cancelled'); post.assert_not_called()

    @patch('desk.ai.queue.complete')
    def test_human_verdict_has_priority_over_completed_ai_job(self, post):
        human = self.assess()
        self.responses(post, classification=NEGATIVE)
        enqueue(self.thread); process_one()
        self.thread.refresh_from_db()
        self.assertEqual(effective_analysis(self.thread).provider, 'human')
        self.assertEqual(effective_analysis(self.thread).sentiment, 'neutral')
        human.refresh_from_db(); self.assertEqual(human.decision, 'non_negative')

    @patch('desk.ai.queue.complete')
    def test_pending_human_assessment_remains_pending(self, post):
        self.assess('pending'); self.responses(post)
        enqueue(self.thread); process_one(); self.thread.refresh_from_db()
        self.assertIsNone(effective_analysis(self.thread))

    @patch('desk.ai.queue.complete')
    def test_text_change_invalidates_previous_model_analysis(self, post):
        self.responses(post, classification=NEGATIVE)
        enqueue(self.thread); process_one()
        self.thread.text = 'Новая версия'; self.thread.save(); self.thread.refresh_from_db()
        self.assertIsNone(effective_analysis(self.thread))

    @patch('desk.ai.queue.complete')
    def test_stale_draft_is_not_offered_to_operator(self, post):
        self.responses(post, draft={'draft': 'OLD_DRAFT_MUST_NOT_BE_SENT', 'used_fact_ids': []})
        enqueue(self.thread); process_one()
        self.thread.text = 'Новая версия отзыва'; self.thread.save()
        self.client.force_login(self.user)
        response = self.client.get(reverse('thread', args=[self.thread.pk]))
        self.assertContains(response, 'Предыдущий анализ не участвует')
        self.assertNotContains(response, 'OLD_DRAFT_MUST_NOT_BE_SENT')

    @patch('desk.ai.queue.complete')
    def test_web_click_queues_and_page_is_scoped_without_network(self, post):
        self.client.force_login(self.user)
        response = self.client.post(reverse('thread_action', args=[self.thread.pk]), {'action': 'analyze'})
        self.assertEqual(response.status_code, 302); post.assert_not_called()
        self.assertEqual(AIJob.objects.count(), 1)
        self.assertContains(self.client.get(reverse('thread', args=[self.thread.pk])), 'В очереди')
        self.assertContains(self.client.get(reverse('ai_queue')), self.account.name)
        self.assertNotContains(self.client.get(reverse('ai_queue')), self.other.name)

    def test_demo_never_enters_external_queue(self):
        self.company.is_demo = True; self.company.save()
        with self.assertRaises(ValueError): enqueue(self.thread)

    def test_configuration_dry_run_and_apply_touch_only_selected_entity(self):
        self.company.legal_entity = 'АМ'; self.company.save()
        args = dict(legal_entity='АМ', folder_id='folder-test', model=self.company.ai_model, key_env='TEST_AI_KEY', stdout=StringIO())
        call_command('configure_ai', **args)
        call_command('configure_ai', apply=True, **args)
        self.company.refresh_from_db(); self.other.refresh_from_db()
        self.assertFalse(self.company.ai_auto_enabled); self.assertEqual(self.other.ai_provider, 'none')
        self.assertFalse(AIJob.objects.exists())

    def test_batch_preview_is_free_and_human_confirmed_reviews_are_excluded(self):
        self.assess()
        call_command('enqueue_ai', account=self.account.name, stdout=StringIO())
        call_command('enqueue_ai', account=self.account.name, apply=True, stdout=StringIO())
        self.assertFalse(AIJob.objects.exists())
        ReviewAssessment.objects.all().delete()
        call_command('enqueue_ai', account=self.account.name, stdout=StringIO())
        self.assertFalse(AIJob.objects.exists())
        call_command('enqueue_ai', account=self.account.name, apply=True, stdout=StringIO())
        self.assertEqual(AIJob.objects.count(), 1)

    def test_rejected_cross_folder_model_cannot_be_configured(self):
        with self.assertRaises(CommandError):
            call_command('configure_ai', company=self.company.name, folder_id='other-folder', model=self.company.ai_model,
                         key_env='TEST_AI_KEY', apply=True, stdout=StringIO())

    @patch('desk.ai.queue.complete')
    def test_status_contains_usage_and_errors_without_keys(self, post):
        post.side_effect = ProviderProblem('HTTP 403', rejected=True)
        enqueue(self.thread); process_one()
        out = StringIO(); call_command('ai_status', company=self.company.name, stdout=out)
        self.assertIn('HTTP 403', out.getvalue()); self.assertNotIn('test-only-placeholder', out.getvalue())

    @patch('desk.integrations.details.enrich_account', return_value=0)
    @patch('desk.integrations.sync.get_adapter')
    @patch('desk.ai.queue.complete')
    def test_polling_opt_in_never_calls_ai_inline_or_restarts_a_failed_job(self, post, adapter, enrich):
        from desk.integrations.base import Incoming
        from desk.integrations.sync import sync_account
        from desk.models import SyncState, Thread
        source = Incoming('fresh-ai-review', 'review', 'Приятный аромат', 'sku1', rating=5)
        fake = Mock(completed_window=None); fake.fetch.return_value = [source]; adapter.return_value = fake
        # With auto disabled, a new review is saved with preliminary local rules.
        sync_account(self.account)
        self.assertFalse(AIJob.objects.exists()); post.assert_not_called()
        self.company.ai_auto_enabled = True; self.company.save()
        SyncState.objects.filter(account=self.account).update(next_attempt=timezone.now())
        sync_account(self.account)
        self.assertFalse(AIJob.objects.exists())  # No mass re-analysis on opt-in.
        source.text = 'Очень приятный аромат'
        SyncState.objects.filter(account=self.account).update(next_attempt=timezone.now())
        sync_account(self.account)
        self.assertEqual(AIJob.objects.count(), 2); post.assert_not_called()
        post.side_effect = ProviderProblem('HTTP 403', rejected=True)
        process_one()
        SyncState.objects.filter(account=self.account).update(next_attempt=timezone.now())
        sync_account(self.account)
        classification = AIJob.objects.get(snapshot__classification_only=True)
        self.assertEqual(classification.status, 'failed')
        self.assertEqual(classification.attempts, 1)
        process_one()  # Dependent reply fails without a second paid classification.
        self.assertFalse(AIJob.objects.exclude(status='failed').exists())
        self.assertEqual(post.call_count, 1)

    @patch('desk.ai.queue.complete')
    def test_terminal_failure_requires_explicit_manual_retry(self, post):
        post.side_effect = ProviderProblem('HTTP 403', rejected=True)
        job = enqueue(self.thread); process_one()
        self.assertEqual(enqueue(self.thread).status, 'failed')
        self.assertEqual(enqueue(self.thread, retry_failed=True).status, 'queued')


class YandexClientTests(AIConfiguredCase):
    @patch('desk.ai.client.requests.post')
    def test_yandex_contract_has_json_model_uri_key_project_and_real_usage(self, post):
        _, _, snap = prepare(self.thread)
        post.return_value = Mock(status_code=200)
        post.return_value.json.return_value = {'choices': [{'message': {'content': '{"ok":true}'}, 'finish_reason': 'stop'}], 'usage': {'prompt_tokens': 10, 'completion_tokens': 5}}
        raw, usage, stopped = complete(snap, 'Return JSON', {'text': 'Проверка'}, 32)
        self.assertEqual(usage, (10, 5)); self.assertTrue(stopped)
        self.assertEqual(post.call_args.args[0], 'https://llm.api.cloud.yandex.net/v1/chat/completions')
        self.assertEqual(post.call_args.kwargs['headers']['OpenAI-Project'], 'folder-test')
        self.assertEqual(post.call_args.kwargs['headers']['Authorization'], 'Api-Key test-only-placeholder')
        self.assertEqual(post.call_args.kwargs['json']['model'], self.company.ai_model)
        self.assertEqual(post.call_args.kwargs['json']['response_format'], {'type': 'json_object'})
        self.assertFalse(post.call_args.kwargs['allow_redirects'])

    @patch('desk.ai.client.requests.post')
    def test_http_errors_are_sanitized_and_429_respects_retry_after(self, post):
        _, _, snap = prepare(self.thread)
        post.return_value = Mock(status_code=429, headers={'Retry-After': '181'}, text='private-key-should-not-appear')
        with self.assertRaises(ProviderProblem) as caught: complete(snap, 'JSON', {}, 32)
        self.assertTrue(caught.exception.retryable); self.assertEqual(caught.exception.retry_after, 181)
        self.assertNotIn('private-key', str(caught.exception))

    @patch('desk.ai.client.requests.post')
    def test_timeouts_are_retryable_without_logging_request(self, post):
        _, _, snap = prepare(self.thread)
        post.side_effect = requests.ReadTimeout('private request data')
        with self.assertRaises(ProviderProblem) as caught: complete(snap, 'JSON', {}, 32)
        self.assertTrue(caught.exception.retryable); self.assertNotIn('private request', str(caught.exception))

    @patch('desk.ai.client.requests.get')
    def test_models_list_filters_to_selected_folder(self, get):
        get.return_value = Mock(status_code=200)
        get.return_value.json.return_value = {'data': [{'id': self.company.ai_model}, {'id': 'gpt://other/private/latest'}]}
        self.assertEqual(list_yandex_models('folder-test', 'TEST_AI_KEY'), [self.company.ai_model])

    @patch('desk.ai.client.requests.post')
    def test_probe_is_synthetic_and_does_not_change_reviews_or_enqueue(self, post):
        post.return_value = Mock(status_code=200)
        post.return_value.json.return_value = {'choices': [{'message': {'content': '{"ok":true}'}, 'finish_reason': 'stop'}], 'usage': {'prompt_tokens': 10, 'completion_tokens': 5}}
        out = StringIO(); call_command('diagnose_ai', company=self.company.name, probe=True, stdout=out)
        self.assertIn('API доступен', out.getvalue())
        self.assertFalse(AIJob.objects.exists()); self.assertFalse(Analysis.objects.exists())
        self.assertNotIn(self.thread.text, str(post.call_args))

    @patch('desk.ai.client.requests.get')
    def test_models_probe_cannot_send_another_provider_key_to_yandex(self, get):
        self.company.ai_provider = 'openai'; self.company.save()
        with self.assertRaises(CommandError):
            call_command('diagnose_ai', company=self.company.name, list_models=True, stdout=StringIO())
        get.assert_not_called()


@skipUnless(connection.vendor == 'postgresql', 'PostgreSQL concurrency')
class AIBudgetConcurrencyTests(TransactionTestCase):
    def test_two_workers_cannot_reserve_the_same_remaining_budget(self):
        from desk.models import Account, Thread
        from desk.ai.queue import CLASSIFIER
        company = Company.objects.create(name='Concurrent company', ai_provider='yandex', ai_model='gpt://test/model/v1',
            ai_folder_id='test', ai_key_env='TEST_CONCURRENT_AI_KEY')
        account = Account.objects.create(company=company, name='Concurrent WB', platform='wb', token_env='TEST_WB', enabled=True)
        fields = dict(account=account, kind='review', text='Спасибо', rating=5, received_at=timezone.now(), due_at=timezone.now())
        a = Thread.objects.create(external_id='budget-1', **fields)
        b = Thread.objects.create(external_id='budget-2', **fields)
        entered, release = Event(), Event()
        def respond(*args):
            entered.set()
            if not release.wait(timeout=10): raise AssertionError('Other worker stalled')
            return json.dumps(NEGATIVE), (100, 20), True
        def worker():
            close_old_connections()
            try: return process_one()
            finally: connections.close_all()
        with patch.dict(os.environ, {'TEST_CONCURRENT_AI_KEY': 'test-placeholder'}):
            first, second = enqueue(a), enqueue(b)
            source = first.snapshot['source']
            company.ai_token_limit = len((CLASSIFIER + json.dumps(source, ensure_ascii=False)).encode()) + 768 + 2048
            company.save(update_fields=['ai_token_limit'])
            with patch('desk.ai.queue.complete', side_effect=respond) as post, ThreadPoolExecutor(max_workers=2) as pool:
                result = pool.submit(worker)
                try:
                    self.assertTrue(entered.wait(timeout=10))
                    self.assertTrue(pool.submit(worker).result(timeout=10))
                    second.refresh_from_db(); self.assertEqual(second.status, 'blocked')
                finally: release.set()
                self.assertTrue(result.result(timeout=10)); self.assertEqual(post.call_count, 1)
        first.refresh_from_db(); self.assertEqual(first.status, 'blocked'); self.assertTrue(first.classification)
