import uuid
from datetime import timedelta
from unittest.mock import Mock, patch
from django.utils import timezone
from desk.integrations.base import IntegrationError
from desk.integrations.sync import ingest_reply
from desk.messaging.service import enqueue, process_one, retry, reconcile
from desk.models import Audit, Outgoing
from .base import DeskCase


class MessagingTests(DeskCase):
    def enqueue(self): return enqueue(self.thread, self.user, 'Ответ оператора', uuid.uuid4())

    def test_queue_is_persistent_and_double_submit_is_idempotent(self):
        analysis = self.analysis()
        key = uuid.uuid4()
        first = enqueue(self.thread, self.user, 'Финальный ответ', key)
        second = enqueue(self.thread, self.user, 'Другой текст', key)
        self.assertEqual(first.pk, second.pk)
        self.assertEqual(Outgoing.objects.count(), 1)
        self.assertEqual(first.ai_draft, analysis.draft)
        self.assertEqual(first.status, 'Queued')

    @patch('desk.messaging.service.get_adapter')
    def test_full_send_then_publication_confirmation(self, factory):
        factory.return_value.send.return_value = 'reply-321'
        item = self.enqueue()
        self.assertTrue(process_one())
        item.refresh_from_db()
        self.assertEqual(item.status, 'Sent')
        self.assertIsNone(item.confirmed_at)
        factory.return_value.send.assert_called_once()
        ingest_reply(self.thread, {'external_id': 'reply-321', 'text': item.text, 'direction': 'out', 'published': False})
        item.refresh_from_db(); self.assertEqual(item.status, 'Sent')
        ingest_reply(self.thread, {'external_id': 'reply-321', 'text': item.text, 'direction': 'out', 'published': True})
        item.refresh_from_db(); self.assertEqual(item.status, 'Confirmed')
        self.assertIsNotNone(item.confirmed_at)
        self.assertFalse(process_one())

    @patch('desk.messaging.service.get_adapter')
    def test_rate_limit_schedules_exponential_backoff(self, factory):
        factory.return_value.send.side_effect = IntegrationError('Rate limit', retryable=True, retry_after=120)
        item = self.enqueue(); process_one(); item.refresh_from_db()
        self.assertEqual((item.status, item.attempts), ('Queued', 1))
        self.assertGreater(item.next_attempt, timezone.now() + timedelta(seconds=110))
        self.assertFalse(process_one())

    @patch('desk.messaging.service.get_adapter')
    def test_retry_exhaustion_keeps_message_for_manual_retry(self, factory):
        factory.return_value.send.side_effect = IntegrationError('Rate limit', retryable=True)
        item = self.enqueue()
        for _ in range(5):
            Outgoing.objects.filter(pk=item.pk).update(next_attempt=timezone.now() - timedelta(seconds=1))
            process_one()
        item.refresh_from_db(); self.assertEqual(item.status, 'Failed')
        self.assertEqual(item.text, 'Ответ оператора')
        retry(item, self.user); item.refresh_from_db()
        self.assertEqual((item.status, item.attempts), ('Queued', 0))

    @patch('desk.messaging.service.get_adapter')
    def test_ambiguous_result_cannot_be_automatically_resent(self, factory):
        factory.return_value.send.side_effect = IntegrationError('Read timeout', ambiguous=True)
        item = self.enqueue(); process_one(); item.refresh_from_db()
        self.assertEqual(item.status, 'Failed'); self.assertFalse(item.retry_safe)
        with self.assertRaises(ValueError): retry(item, self.user)
        self.assertFalse(process_one())

    def test_stale_sending_is_retained_without_blind_retry(self):
        item = self.enqueue()
        Outgoing.objects.filter(pk=item.pk).update(status='Sending', updated_at=timezone.now() - timedelta(minutes=10))
        self.assertFalse(process_one()); item.refresh_from_db()
        self.assertEqual(item.status, 'Failed'); self.assertFalse(item.retry_safe)

    def test_second_pending_message_is_blocked(self):
        self.enqueue()
        with self.assertRaises(ValueError): self.enqueue()

    def test_disabled_and_demo_accounts_cannot_send(self):
        self.account.enabled = False; self.account.save()
        with self.assertRaises(ValueError): self.enqueue()
        self.account.enabled = True; self.account.save()
        self.company.is_demo = True; self.company.save()
        with self.assertRaises(ValueError): self.enqueue()

    def test_every_enqueue_has_operator_and_final_text_in_audit(self):
        item = self.enqueue()
        audit = Audit.objects.get(action='outgoing.queued')
        self.assertEqual(audit.actor, self.user)
        self.assertEqual(audit.after['text'], item.text)

    @patch('desk.messaging.service.get_adapter')
    def test_reconciliation_finds_published_response_without_resending(self, factory):
        item = self.enqueue(); item.status, item.retry_safe = 'Failed', False; item.save()
        factory.return_value.replies.return_value = [{'external_id': 'existing', 'text': item.text, 'direction': 'out', 'published': True}]
        reconcile(item, self.user); item.refresh_from_db()
        self.assertEqual(item.status, 'Confirmed')
        factory.return_value.send.assert_not_called()

    @patch('desk.messaging.service.get_adapter')
    def test_reconciliation_requires_confirmation_for_absent_message(self, factory):
        item = self.enqueue(); item.status, item.retry_safe = 'Failed', False; item.save()
        factory.return_value.replies.return_value = []
        with self.assertRaises(ValueError): reconcile(item, self.user)
        reconcile(item, self.user, verified_absent=True); item.refresh_from_db()
        self.assertTrue(item.retry_safe)
