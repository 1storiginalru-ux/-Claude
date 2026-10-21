from .base import Adapter, Incoming, IntegrationError, timestamp


class YandexMarket(Adapter):
    base_url = 'https://api.partner.market.yandex.ru'

    def path(self, suffix, version=2):
        if not self.account.business_id.isdigit(): raise IntegrationError('Нужен числовой Business ID Яндекс Маркета.')
        return f'/v{version}/businesses/{self.account.business_id}/{suffix}'

    def pages(self, suffix, key, body, version=2):
        token = None
        for _ in range(1000):
            params = {'limit': 50}
            if token: params['page_token'] = token
            payload = self.request('POST', self.path(suffix, version), params=params, json=body)
            result = payload.get('result', {})
            rows = result.get(key)
            if not isinstance(rows, list): raise IntegrationError('Неожиданная структура ответа Яндекс Маркета.')
            yield from rows
            next_token = result.get('paging', {}).get('nextPageToken')
            if not next_token: return
            if next_token == token: raise IntegrationError('API повторил токен страницы.')
            token = next_token
        raise IntegrationError('Достигнут предел страниц Яндекс Маркета; требуется backfill.')

    def fetch(self):
        for row in self.pages('goods-feedback', 'feedbacks', {}):
            description = row.get('description') or {}
            text = '\n'.join(description.get(k) or '' for k in ['advantages', 'disadvantages', 'comment']).strip()
            yield Incoming(str(row['feedbackId']), 'review', text or '(Отзыв без текста)', str(row.get('identifiers', {}).get('offerId') or ''), row.get('author') or 'Покупатель', row.get('statistics', {}).get('rating'), timestamp(row.get('createdAt')))
        for row in self.pages('goods-questions', 'questions', {}, 1):
            ids = row['questionIdentifiers']
            yield Incoming(str(ids['id']), 'question', row['text'], str(ids['offerId']), row.get('author', {}).get('name') or 'Покупатель', None, timestamp(row.get('createdAt')))

    def send(self, thread, text):
        if thread.kind == 'review':
            response = self.request('POST', self.path('goods-feedback/comments/update'), writing=True, json={'feedbackId': int(thread.external_id), 'comment': {'text': text}})
            external_id = response.get('result', {}).get('id')
        else:
            response = self.request('POST', self.path('goods-questions/update', 1), writing=True, json={'operationType': 'CREATE', 'parentEntityId': {'id': int(thread.external_id), 'type': 'QUESTION'}, 'text': text})
            external_id = response.get('result', {}).get('entity', {}).get('id')
        if external_id is None:
            raise IntegrationError('API принял запрос без ID ответа; нужна проверка публикации.', ambiguous=True)
        return str(external_id)

    def replies(self, thread):
        if thread.kind == 'review':
            rows = self.pages('goods-feedback/comments', 'comments', {'feedbackId': int(thread.external_id)})
        else:
            rows = self.pages('goods-questions/answers', 'answers', {'questionId': int(thread.external_id)}, 1)
        return [{'external_id': str(r['id']), 'text': r['text'], 'direction': 'out' if r.get('canModify') else 'in', 'published': r.get('status') == 'PUBLISHED'} for r in rows]
