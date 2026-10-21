import os
from dataclasses import dataclass, field
from datetime import datetime
import requests
from django.utils import timezone
from django.utils.dateparse import parse_datetime


class IntegrationError(Exception):
    def __init__(self, message, retryable=False, ambiguous=False, retry_after=0, rate_limited=False, request_sent=True):
        super().__init__(message)
        self.retryable, self.ambiguous, self.retry_after = retryable, ambiguous, retry_after
        self.rate_limited, self.request_sent = rate_limited, request_sent


@dataclass
class Incoming:
    external_id: str
    kind: str
    text: str
    sku: str
    customer: str = 'Покупатель'
    rating: int | None = None
    created_at: datetime | None = None
    replies: list = field(default_factory=list)


def timestamp(value):
    parsed = parse_datetime(value) if isinstance(value, str) else None
    if parsed and timezone.is_naive(parsed): parsed = timezone.make_aware(parsed)
    return parsed or timezone.now()


class Adapter:
    capabilities = {'fetch_reviews': True, 'reply_reviews': True, 'fetch_questions': True, 'reply_questions': True, 'chats': False, 'attachments': False, 'sales_sync': False}
    base_url = ''

    def __init__(self, account):
        self.account = account
        self.sync_cursor = {}
        self.checkpoint = lambda cursor: None

    def before_request(self, token):
        pass

    def after_response(self, token, response):
        return None

    def request(self, method, path, *, writing=False, **kwargs):
        if not self.account.enabled or self.account.company.is_demo: raise IntegrationError('Кабинет выключен или относится к демо-компании.')
        token = os.environ.get(self.account.token_env)
        if not token: raise IntegrationError('Не задана переменная с API-ключом кабинета.')
        header = 'Authorization' if self.account.platform == 'wb' else 'Api-Key'
        self.before_request(token)
        try:
            response = requests.request(method, self.base_url + path, headers={header: token}, timeout=(5, 20), allow_redirects=False, **kwargs)
        except requests.ConnectTimeout as e:
            raise IntegrationError('Тайм-аут подключения.', retryable=True) from e
        except requests.RequestException as e:
            raise IntegrationError('Сетевая ошибка; результат отправки требует проверки.' if writing else 'Ошибка сети.', retryable=not writing, ambiguous=writing) from e
        observed_delay = self.after_response(token, response)
        if response.status_code in {420, 429}:
            from .rate_limit import retry_delay
            delay = observed_delay or retry_delay(response.headers)
            raise IntegrationError(f'API ограничил частоту: HTTP {response.status_code}. Пауза {delay} сек.', retryable=True, retry_after=delay, rate_limited=True)
        if response.status_code >= 500:
            raise IntegrationError(f'API: HTTP {response.status_code}.', retryable=not writing, ambiguous=writing)
        if not 200 <= response.status_code < 300:
            raise IntegrationError(f'API: HTTP {response.status_code}. Проверьте доступ и параметры.')
        try: payload = response.json() if response.content else {}
        except ValueError as e:
            raise IntegrationError('API вернул некорректный JSON.', ambiguous=writing) from e
        if payload.get('error') or payload.get('status') == 'ERROR':
            raise IntegrationError('API сообщил об ошибке. Проверьте права и параметры кабинета.')
        return payload


def get_adapter(account):
    from .wildberries import Wildberries
    from .yandex import YandexMarket
    if account.platform == 'wb': return Wildberries(account)
    if account.platform == 'ym': return YandexMarket(account)
    raise IntegrationError('Канал не поддерживается.')
