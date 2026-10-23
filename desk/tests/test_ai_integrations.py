import copy
import os
from unittest.mock import Mock, patch
from datetime import timedelta
from django.utils import timezone
from desk.ai.provider import AIProblem, analyze, local_classify, validate_result
from desk.integrations.base import Incoming, IntegrationError, get_adapter
from desk.integrations.sync import ingest
from desk.models import Message, Thread
from .base import DeskCase

VALID = {'sentiment': 'negative', 'hidden_negative': True, 'signals': ['LONG'], 'severity': 'P2', 'objective_issue': False, 'requires_human': True, 'confidence': 0.94, 'summary': 'Жалоба на стойкость', 'draft': 'Здравствуйте! Уточните, пожалуйста, условия использования.'}


class AITests(DeskCase):
    def test_hidden_negative_five_star_review_is_detected(self):
        data = local_classify('Всё отлично, но аромат не держится даже час', 5)
        self.assertTrue(data['hidden_negative']); self.assertIn('LONG', data['signals'])
        self.assertTrue(data['requires_human']); self.assertEqual(data['draft'], '')

    def test_human_only_suppresses_even_model_generated_draft(self):
        data = copy.deepcopy(VALID); data['signals'] = ['AUTH-UNKNOWN']; data['requires_human'] = False
        result = validate_result(data)
        self.assertTrue(result['requires_human']); self.assertEqual(result['draft'], '')

    def test_non_boolean_flags_unknown_signals_and_nan_are_rejected(self):
        for key, value in [('hidden_negative', 'false'), ('signals', ['INVENTED']), ('confidence', float('nan')), ('confidence', True), ('summary', None)]:
            with self.subTest(key=key), self.assertRaises(AIProblem): validate_result({**VALID, key: value})

    @patch('desk.ai.provider.requests.post')
    @patch.dict(os.environ, {'TEST_AI_KEY': 'test-only-placeholder'})
    def test_prompt_is_scoped_to_thread_company(self, post):
        self.company.ai_provider, self.company.ai_key_env, self.company.ai_model = 'openai', 'TEST_AI_KEY', 'configured-model'
        self.company.save()
        post.return_value = Mock(status_code=200)
        import json
        post.return_value.json.return_value = {'choices': [{'message': {'content': json.dumps(VALID)}}]}
        result = analyze(self.thread, self.user)
        self.assertEqual(result.provider, 'openai')
        system = post.call_args.kwargs['json']['messages'][0]['content']
        self.assertIn('PROFILE_A_ONLY', system); self.assertNotIn('PROFILE_B_PRIVATE', system)
        self.assertEqual(post.call_args.kwargs['timeout'], (3, 7))

    def test_unconfigured_ai_is_explicit_rules_mode_without_draft(self):
        result = analyze(self.thread)
        self.assertEqual(result.provider, 'rules'); self.assertEqual(result.draft, '')
        self.assertEqual(result.confidence, 0)


class IntegrationTests(DeskCase):
    def test_duplicate_incoming_is_idempotent_and_edits_keep_history(self):
        incoming = Incoming('new-review', 'review', 'Первый текст', 'sku1', rating=4)
        first, changed = ingest(self.account, incoming)
        second, changed = ingest(self.account, incoming)
        self.assertEqual(first.pk, second.pk); self.assertFalse(changed)
        self.assertEqual(Message.objects.filter(thread=first).count(), 1)
        incoming.text = 'Изменённый текст'
        item, changed = ingest(self.account, incoming)
        self.assertTrue(changed); self.assertEqual(item.messages.count(), 2)

    @patch('desk.integrations.yandex.YandexMarket.request')
    def test_yandex_review_and_question_contracts_and_pagination(self, request):
        created = (timezone.now() - timedelta(hours=1)).isoformat()
        request.side_effect = [
            {'result': {'feedbacks': [{'feedbackId': 10, 'createdAt': created, 'identifiers': {'offerId': 'sku1'}, 'statistics': {'rating': 5}, 'description': {'disadvantages': 'Не держится'}, 'author': 'Анна'}], 'paging': {'nextPageToken': 'page2'}}},
            {'result': {'feedbacks': []}},
            {'result': {'questions': [{'questionIdentifiers': {'id': 20, 'offerId': 'sku1'}, 'createdAt': created, 'text': 'Какой объём?', 'author': {'name': 'Дима'}}]}},
        ]
        rows = list(get_adapter(self.ym).fetch())
        self.assertEqual([(r.external_id, r.kind, r.sku) for r in rows], [('10', 'review', 'sku1'), ('20', 'question', 'sku1')])
        self.assertEqual(request.call_args_list[1].kwargs['params']['page_token'], 'page2')
        self.assertIn('/v1/businesses/123/goods-questions', request.call_args_list[2].args)

    @patch('desk.integrations.yandex.YandexMarket.request')
    def test_yandex_question_answer_uses_typed_parent(self, request):
        thread = self.make_thread('20', account=self.ym, kind='question')
        request.return_value = {'result': {'entity': {'id': 999, 'type': 'ANSWER'}}}
        self.assertEqual(get_adapter(self.ym).send(thread, '100 мл'), '999')
        payload = request.call_args.kwargs['json']
        self.assertEqual(payload['parentEntityId'], {'id': 20, 'type': 'QUESTION'})

    @patch('desk.integrations.wildberries.Wildberries.request')
    def test_wildberries_review_reply_contract(self, request):
        get_adapter(self.account).send(self.thread, 'Ответ')
        self.assertEqual(request.call_args.args, ('POST', '/api/v1/feedbacks/answer'))
        self.assertEqual(request.call_args.kwargs['json'], {'id': self.thread.external_id, 'text': 'Ответ'})

    @patch('desk.integrations.wildberries.Wildberries.request')
    def test_wildberries_question_state_and_publication(self, request):
        thread = self.make_thread('question-id', kind='question')
        adapter = get_adapter(self.account)
        adapter.send(thread, 'Ответ')
        self.assertEqual(request.call_args.kwargs['json']['state'], 'wbRu')
        incoming = adapter.parse({'id': 'question-id', 'text': 'Вопрос', 'state': 'wbRu', 'answer': {'text': 'Ответ'}}, 'question')
        self.assertTrue(incoming.replies[0]['published'])

    @patch('desk.integrations.base.requests.request')
    @patch.dict(os.environ, {'TEST_WB_TOKEN': 'test-only-placeholder'})
    def test_auth_denial_does_not_expose_token_or_response_body(self, request):
        request.return_value = Mock(status_code=401, text='secret response')
        with self.assertRaises(IntegrationError) as error: get_adapter(self.account).request('GET', '/test')
        self.assertIn('401', str(error.exception)); self.assertNotIn('test-only', str(error.exception))
        self.assertFalse(request.call_args.kwargs['allow_redirects'])

    @patch('desk.integrations.base.requests.request')
    @patch.dict(os.environ, {'TEST_WB_TOKEN': 'test-only-placeholder'})
    def test_post_timeout_is_ambiguous_and_not_retryable(self, request):
        import requests
        request.side_effect = requests.ReadTimeout()
        with self.assertRaises(IntegrationError) as error: get_adapter(self.account).send(self.thread, 'Ответ')
        self.assertTrue(error.exception.ambiguous); self.assertFalse(error.exception.retryable)
