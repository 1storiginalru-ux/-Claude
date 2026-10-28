"""Explicit provider contracts; errors never include response bodies or keys."""
import json
import os
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import requests
from .provider import AIProblem


class ProviderProblem(AIProblem):
    def __init__(self, message, *, retryable=False, retry_after=0, rejected=False):
        super().__init__(message)
        self.retryable, self.retry_after, self.rejected = retryable, retry_after, rejected


def headers(snapshot):
    key = os.environ.get(snapshot['key_env'], '').strip()
    if not key: raise ProviderProblem('Не задан API-ключ выбранной компании.', rejected=True)
    provider = snapshot['provider']
    if provider == 'yandex':
        return {'Authorization': f'Api-Key {key}', 'OpenAI-Project': snapshot['folder']}
    if provider == 'openai': return {'Authorization': f'Bearer {key}'}
    if provider == 'anthropic': return {'x-api-key': key, 'anthropic-version': '2023-06-01'}
    raise ProviderProblem('Этот AI-провайдер не подключён.', rejected=True)


def validate_settings(snapshot):
    headers(snapshot)
    if not snapshot['model']: raise ProviderProblem('Укажите точный ID AI-модели.', rejected=True)
    if snapshot['provider'] == 'yandex':
        folder = snapshot['folder']
        if not folder or not snapshot['model'].startswith(f'gpt://{folder}/'):
            raise ProviderProblem('Нужны каталог Яндекса и полный gpt:// URI модели из этого каталога.', rejected=True)


def retry_delay(value):
    try: return max(1, min(86400, int(value)))
    except (ValueError, TypeError):
        try:
            return max(1, min(86400, int((parsedate_to_datetime(value) - datetime.now(timezone.utc)).total_seconds())))
        except (ValueError, TypeError, OverflowError): return 60


def check_http(response):
    if response.status_code == 200: return
    code = response.status_code
    message = f'AI API: HTTP {code}.'
    if code in {401, 403}: message += ' Проверьте ключ, права и доступ к модели.'
    if code in {400, 404, 422}: message += ' Проверьте URI модели и поддерживаемый формат запроса.'
    raise ProviderProblem(message, retryable=code in {408, 409, 429} or code >= 500,
                          retry_after=retry_delay(response.headers.get('Retry-After')),
                          rejected=code in {400, 401, 403, 404, 413, 422, 429})


def complete(snapshot, system, context, max_tokens):
    validate_settings(snapshot)
    provider = snapshot['provider']
    message = json.dumps(context, ensure_ascii=False)
    body = {'model': snapshot['model'], 'messages': [{'role': 'system', 'content': system}, {'role': 'user', 'content': message}],
            'max_tokens': max_tokens, 'temperature': 0.2, 'response_format': {'type': 'json_object'}}
    endpoint = {'yandex': 'https://llm.api.cloud.yandex.net/v1/chat/completions',
                'openai': 'https://api.openai.com/v1/chat/completions',
                'anthropic': 'https://api.anthropic.com/v1/messages'}[provider]
    if provider == 'anthropic':
        body.pop('response_format'); body['system'] = system
        body['messages'] = [{'role': 'user', 'content': message}]
    try:
        response = requests.post(endpoint, headers=headers(snapshot), json=body, timeout=(5, 60), allow_redirects=False)
    except requests.RequestException as error:
        raise ProviderProblem('AI не ответил вовремя или соединение прервано.', retryable=True) from error
    check_http(response)
    try:
        payload = response.json()
        if provider == 'anthropic':
            raw = ''.join(p['text'] for p in payload['content'] if p.get('type') == 'text')
            usage = payload.get('usage', {})
            counts = (usage.get('input_tokens'), usage.get('output_tokens'))
            stopped = payload.get('stop_reason') == 'end_turn'
        else:
            choice = payload['choices'][0]
            raw = choice['message']['content']
            usage = payload.get('usage', {})
            counts = (usage.get('prompt_tokens'), usage.get('completion_tokens'))
            stopped = choice.get('finish_reason') == 'stop'
        if not all(type(n) is int and 0 <= n <= 100000000 for n in counts): counts = None
        return raw, counts, stopped
    except (ValueError, KeyError, IndexError, TypeError) as error:
        raise ProviderProblem('AI вернул неожиданный формат ответа.', retryable=True) from error


def list_yandex_models(folder, key_env):
    snapshot = {'provider': 'yandex', 'folder': folder, 'key_env': key_env}
    if not folder: raise ProviderProblem('Укажите ID каталога Яндекса.', rejected=True)
    try:
        response = requests.get('https://llm.api.cloud.yandex.net/v1/models', headers=headers(snapshot), timeout=(5, 30), allow_redirects=False)
        check_http(response)
        values = response.json()['data']
        if not isinstance(values, list): raise ValueError
        return [row['id'] for row in values if isinstance(row, dict) and isinstance(row.get('id'), str) and row['id'].startswith(f'gpt://{folder}/')]
    except ProviderProblem: raise
    except (requests.RequestException, KeyError, TypeError, ValueError) as error:
        raise ProviderProblem('Не удалось получить список моделей Яндекса.') from error
