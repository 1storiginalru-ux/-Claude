"""Inspect local progress and optionally make one rate-limited, read-only request."""
import os
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone
from desk.integrations.base import IntegrationError
from desk.integrations.rate_limit import scope_for
from desk.integrations.wildberries import Wildberries
from desk.models import Account, ApiThrottle, SyncState, Thread


RATE_HEADERS = ('Retry-After', 'X-Ratelimit-Limit', 'X-Ratelimit-Remaining', 'X-Ratelimit-Retry', 'X-Ratelimit-Reset')


def safe_text(value, token):
    text = str(value)
    if token:
        text = text.replace(token, '[ключ скрыт]')
    return ''.join(char if char.isprintable() else ' ' for char in text)[:240]


class DiagnosticAdapter(Wildberries):
    def after_response(self, token, response):
        self.report = [f'Ответ WB: HTTP {response.status_code}']
        for name in RATE_HEADERS:
            if name in response.headers:
                self.report.append(f'{name}: {safe_text(response.headers[name], token)}')
        if response.status_code >= 400:
            try:
                payload = response.json()
            except ValueError:
                payload = None
            if isinstance(payload, dict):
                for name in ('title', 'detail', 'origin', 'requestId'):
                    if isinstance(payload.get(name), (str, int)):
                        self.report.append(f'{name}: {safe_text(payload[name], token)}')
        return super().after_response(token, response)


class Command(BaseCommand):
    help = 'Диагностика WB без вывода API-ключа; --probe делает один GET с соблюдением паузы.'

    def add_arguments(self, parser):
        parser.add_argument('--account', type=int, help='ID кабинета, если их несколько.')
        parser.add_argument('--probe', action='store_true', help='Один запрос чтения после окончания паузы. Сначала остановите программу через Ctrl+C.')

    def handle(self, *args, **options):
        accounts = Account.objects.filter(platform='wb', enabled=True, company__is_demo=False).select_related('company')
        if options['account'] is not None:
            accounts = accounts.filter(pk=options['account'])
        accounts = list(accounts)
        if not accounts:
            raise CommandError('Нет подходящего включённого кабинета WB реальной компании.')
        if options['probe'] and len(accounts) != 1:
            for account in accounts:
                self.stdout.write(f'ID {account.pk}: {account.name}')
            raise CommandError('Выберите один кабинет: добавьте --account ID.')
        for account in accounts:
            token = os.environ.get(account.token_env, '')
            self.stdout.write(f'Кабинет ID {account.pk}: {safe_text(account.name, token)}')
            self.stdout.write(f'API-ключ в этом Terminal: {"задан" if token else "не задан"}')
            self.stdout.write(f'Обращений в базе: {Thread.objects.filter(account=account).count()}')
            state = SyncState.objects.filter(account=account).first()
            cursor = state.cursor if state else {}
            stream = cursor.get('stream', 0)
            if not isinstance(stream, int) or not 0 <= stream <= len(Wildberries.streams):
                raise CommandError('Неожиданная позиция загрузки; требуется проверка состояния.')
            if stream == len(Wildberries.streams):
                self.stdout.write('Все страницы текущего окна получены; осталось завершить цикл.')
                stream = 0
            kind, endpoint, answered = Wildberries.streams[stream]
            label = 'Отзывы' if kind == 'review' else 'Вопросы'
            self.stdout.write(f'Этап: {label}, {"с ответом" if answered == "true" else "без ответа"}; смещение {cursor.get("skip", 0)}')
            now = timezone.now()
            deadlines = []
            if state:
                deadlines.append(state.next_attempt)
            throttle = ApiThrottle.objects.filter(scope=scope_for(token)).first() if token else None
            if throttle:
                deadlines.append(throttle.next_request_at)
            deadline = max(deadlines) if deadlines else now
            if deadline > now:
                self.stdout.write(f'Пауза до {timezone.localtime(deadline):%d.%m.%Y %H:%M:%S %Z}. Запрос не отправлен.')
                continue
            if not options['probe']:
                self.stdout.write('Сетевые запросы не выполнялись. Для проверки: diagnose_wb --probe.')
                continue
            if state and state.lease_until and state.lease_until > now:
                self.stdout.write('Синхронизация ещё работает. Остановите программу через Ctrl+C; запрос не отправлен.')
                continue
            # Probe the same stream with one item, without ingesting or moving its cursor.
            params = {'isAnswered': answered, 'take': 1, 'skip': cursor.get('skip', 0), 'order': 'dateDesc'}
            if cursor.get('until'): params['dateTo'] = cursor['until']
            if cursor.get('since'): params['dateFrom'] = cursor['since']
            self.stdout.write(f'Один GET /api/v1/{endpoint}, isAnswered={answered}, take=1.')
            adapter = DiagnosticAdapter(account)
            adapter.report = []
            try:
                payload = adapter.request('GET', f'/api/v1/{endpoint}', params=params)
            except IntegrationError as error:
                for line in adapter.report: self.stdout.write(line)
                self.stdout.write(safe_text(error, token))
            else:
                for line in adapter.report: self.stdout.write(line)
                data = payload.get('data')
                rows = data.get(endpoint) if isinstance(data, dict) else None
                self.stdout.write(f'Объектов в проверочном ответе: {len(rows)}' if isinstance(rows, list) else 'Неожиданная структура ответа WB.')
            self.stdout.write('Позиция загрузки и обращения в базе не изменены. Повторять проверку подряд не нужно.')
