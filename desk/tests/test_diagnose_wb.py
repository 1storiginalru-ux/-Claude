import os
from datetime import timedelta
from io import StringIO
from unittest.mock import Mock, patch
from django.core.management import call_command
from django.core.management.base import CommandError
from django.utils import timezone
from desk.integrations.rate_limit import scope_for
from desk.models import Account, ApiThrottle, SyncState, Thread
from .base import DeskCase


class DiagnoseWBTests(DeskCase):
    token = 'diagnostic-test-key-never-a-real-credential'

    def run_command(self, *args):
        output = StringIO()
        with patch.dict(os.environ, {'TEST_WB_TOKEN': self.token}):
            call_command('diagnose_wb', *args, stdout=output)
        return output.getvalue()

    @patch('desk.integrations.base.requests.request')
    def test_default_does_not_send_requests_or_create_state(self, request):
        output = self.run_command()
        request.assert_not_called()
        self.assertFalse(SyncState.objects.exists())
        self.assertFalse(ApiThrottle.objects.exists())
        self.assertIn('Обращений в базе: 1', output)
        self.assertNotIn(self.token, output)

    @patch('desk.integrations.base.requests.request')
    def test_probe_respects_saved_server_pause(self, request):
        ApiThrottle.objects.create(scope=scope_for(self.token), next_request_at=timezone.now() + timedelta(minutes=10))
        self.assertIn('Запрос не отправлен', self.run_command('--probe'))
        request.assert_not_called()

    @patch('desk.integrations.base.requests.request')
    def test_probe_respects_account_retry_and_active_sync(self, request):
        state = SyncState.objects.create(account=self.account, next_attempt=timezone.now() + timedelta(minutes=1))
        self.assertIn('Запрос не отправлен', self.run_command('--probe'))
        state.next_attempt = timezone.now() - timedelta(seconds=1)
        state.lease_until = timezone.now() + timedelta(minutes=1)
        state.save()
        self.assertIn('Синхронизация ещё работает', self.run_command('--probe'))
        request.assert_not_called()

    @patch('desk.integrations.base.requests.request')
    def test_one_small_read_reports_limit_but_does_not_move_cursor(self, request):
        cursor = {'until': int(timezone.now().timestamp()), 'since': 100, 'stream': 1, 'skip': 1000}
        state = SyncState.objects.create(account=self.account, cursor=cursor)
        request.return_value = Mock(status_code=200, content=b'{}', headers={'X-Ratelimit-Remaining': '2'})
        request.return_value.json.return_value = {'data': {'questions': [{'id': 'not-ingested'}]}}
        output = self.run_command('--probe')
        request.assert_called_once()
        self.assertEqual(request.call_args.args, ('GET', 'https://feedbacks-api.wildberries.ru/api/v1/questions'))
        self.assertEqual(request.call_args.kwargs['params'], {'isAnswered': 'false', 'take': 1, 'skip': 1000, 'order': 'dateDesc', 'dateTo': cursor['until'], 'dateFrom': 100})
        state.refresh_from_db()
        self.assertEqual(state.cursor, cursor)
        self.assertEqual(Thread.objects.count(), 1)
        self.assertIn('X-Ratelimit-Remaining: 2', output)
        self.assertIn('Объектов в проверочном ответе: 1', output)

    @patch('desk.integrations.base.requests.request')
    def test_rejection_reports_server_reason_without_key_or_body_dump(self, request):
        request.return_value = Mock(status_code=429, content=b'{}', headers={'X-Ratelimit-Retry': '660', 'Authorization': self.token})
        request.return_value.json.return_value = {'title': 'too many requests', 'detail': f'limit by seller\n{self.token}', 'origin': 'gateway', 'requestId': 'support-id', 'unlisted': 'PRIVATE'}
        output = self.run_command('--probe')
        self.assertIn('Ответ WB: HTTP 429', output)
        self.assertIn('X-Ratelimit-Retry: 660', output)
        self.assertIn('detail: limit by seller [ключ скрыт]', output)
        self.assertIn('requestId: support-id', output)
        self.assertNotIn(self.token, output)
        self.assertNotIn('PRIVATE', output)
        self.assertGreater(ApiThrottle.objects.get(scope=scope_for(self.token)).next_request_at, timezone.now() + timedelta(seconds=650))

    @patch('desk.integrations.base.requests.request')
    def test_probe_requires_explicit_choice_if_multiple_accounts(self, request):
        Account.objects.create(company=self.company, name='WB2', platform='wb', token_env='SECOND_KEY', enabled=True)
        with self.assertRaisesMessage(CommandError, 'Выберите один кабинет'):
            self.run_command('--probe')
        request.assert_not_called()
