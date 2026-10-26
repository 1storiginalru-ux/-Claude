from .base import Adapter, Incoming, IntegrationError, timestamp
from django.utils import timezone
from .window import sync_window, within_window


class YandexMarket(Adapter):
    base_url = 'https://api.partner.market.yandex.ru'

    def path(self, suffix, version=2):
        if not self.account.business_id.isdigit(): raise IntegrationError('Нужен числовой Business ID Яндекс Маркета.')
        return f'/v{version}/businesses/{self.account.business_id}/{suffix}'

    def pages(self, suffix, key, body, version=2, *, cursor=None, checkpoint=None):
        if cursor and cursor.get('complete'): return
        token = (cursor or {}).get('page_token')
        for _ in range(1000):
            params = {'limit': 50}
            if token: params['page_token'] = token
            payload = self.request('POST', self.path(suffix, version), params=params, json=body)
            result = payload.get('result') or {}
            if not isinstance(result, dict): raise IntegrationError('Неожиданная структура ответа Яндекс Маркета.')
            rows = result.get(key)
            if not isinstance(rows, list): raise IntegrationError('Неожиданная структура ответа Яндекс Маркета.')
            yield from rows
            next_token = result.get('paging', {}).get('nextPageToken')
            if next_token and next_token == token: raise IntegrationError('API повторил токен страницы.')
            if checkpoint: checkpoint({'page_token': next_token} if next_token else {'complete': True})
            if not next_token: return
            token = next_token
        raise IntegrationError('Достигнут предел страниц Яндекс Маркета; требуется backfill.')

    def parse(self, row, kind):
        if not isinstance(row, dict): raise IntegrationError('Яндекс Маркет не вернул данные обращения.')
        if kind == 'review':
            description = row.get('description') or {}
            text = '\n'.join(description.get(k) or '' for k in ['advantages', 'disadvantages', 'comment']).strip()
            statistics = row.get('statistics') or {}
            return Incoming(str(row['feedbackId']), 'review', text or '(Отзыв без текста)', str(row.get('identifiers', {}).get('offerId') or ''), row.get('author') or 'Покупатель', statistics.get('rating'), timestamp(row.get('createdAt')), source_data=row, history_complete=statistics.get('commentsCount') == 0)
        ids = row['questionIdentifiers']
        return Incoming(str(ids['id']), 'question', row['text'], str(ids['offerId']), row.get('author', {}).get('name') or 'Покупатель', None, timestamp(row.get('createdAt')), source_data=row)

    def fetch(self):
        since, until = sync_window()
        review_filter = {'dateTimeFrom': since.isoformat(), 'dateTimeTo': until.isoformat()}
        for row in self.pages('goods-feedback', 'feedbacks', review_filter):
            if within_window(row.get('createdAt'), max(since, sync_window()[0]), until):
                yield self.parse(row, 'review')
        # The questions endpoint accepts dates, rather than timestamps. Filter
        # its response again to enforce the exact rolling seven-day boundary.
        question_filter = {'dateFrom': timezone.localdate(since).isoformat(), 'dateTo': timezone.localdate(until).isoformat()}
        for row in self.pages('goods-questions', 'questions', question_filter, 1):
            if within_window(row.get('createdAt'), max(since, sync_window()[0]), until):
                yield self.parse(row, 'question')
        self.completed_window = (max(since, sync_window()[0]), until)

    def fetch_one(self, thread):
        if thread.kind == 'review':
            rows = self.pages('goods-feedback', 'feedbacks', {'feedbackIds': [int(thread.external_id)]})
        else:
            rows = self.pages('goods-questions', 'questions', {'questionIds': [int(thread.external_id)]}, 1)
        for row in rows:
            incoming = self.parse(row, thread.kind)
            if incoming.external_id == thread.external_id: return incoming
        raise IntegrationError('Обращение больше не возвращается API маркетплейса.')

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
        return list(self.history(thread))

    def history(self, thread, *, cursor=None, checkpoint=None):
        if thread.kind == 'review':
            rows = self.pages('goods-feedback/comments', 'comments', {'feedbackId': int(thread.external_id)}, cursor=cursor, checkpoint=checkpoint)
        else:
            rows = self.pages('goods-questions/answers', 'answers', {'questionId': int(thread.external_id)}, 1, cursor=cursor, checkpoint=checkpoint)
        for row in rows:
            yield self.parse_reply(row)
            for comment in row.get('comments') or []:
                yield self.parse_reply(comment, prefix='comment-')

    def parse_reply(self, row, prefix=''):
        author = row.get('author') or {}
        outgoing = row.get('canModify') or author.get('type') == 'BUSINESS'
        return {'external_id': prefix + str(row['id']), 'text': row['text'], 'direction': 'out' if outgoing else 'in', 'published': row.get('status') == 'PUBLISHED', 'source_data': row, 'created_at': timestamp(row.get('createdAt'))}
