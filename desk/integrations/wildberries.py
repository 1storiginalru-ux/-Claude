from .base import Adapter, Incoming, IntegrationError, timestamp
from django.utils import timezone


class Wildberries(Adapter):
    base_url = 'https://feedbacks-api.wildberries.ru'
    page_size = 1000

    def before_request(self, token):
        from .rate_limit import acquire
        acquire(token)

    def after_response(self, token, response):
        from .rate_limit import observe
        return observe(token, response)

    def parse(self, row, kind):
        product = row.get('productDetails') or {}
        text = '\n'.join(str(row.get(k) or '') for k in ['text', 'pros', 'cons']).strip()
        answer = row.get('answer') or {}
        replies = []
        if answer.get('text'):
            state = answer.get('state') if kind == 'review' else row.get('state')
            replies.append({'external_id': 'answer-' + str(row['id']), 'text': answer['text'], 'direction': 'out', 'published': state == 'wbRu'})
        return Incoming(str(row['id']), kind, text or '(Отзыв без текста)', str(product.get('supplierArticle') or ''), row.get('userName') or 'Покупатель', row.get('productValuation') if kind == 'review' else None, timestamp(row.get('createdDate')), replies)

    def fetch(self):
        cursor = dict(self.sync_cursor)
        if 'until' not in cursor:
            cursor.update(until=int(timezone.now().timestamp()), since=max(0, cursor.get('watermark', 0) - 86400), stream=0, skip=0)
            self.checkpoint(dict(cursor))
        streams = [('review', 'feedbacks', 'false'), ('question', 'questions', 'false'), ('review', 'feedbacks', 'true'), ('question', 'questions', 'true')]
        while cursor['stream'] < len(streams):
            kind, key, answered = streams[cursor['stream']]
            if cursor['skip'] >= 200000:
                raise IntegrationError('Достигнут предел истории WB; нужен отдельный backfill по периодам.')
            params = {'isAnswered': answered, 'take': self.page_size, 'skip': cursor['skip'], 'order': 'dateDesc', 'dateTo': cursor['until']}
            if cursor['since']: params['dateFrom'] = cursor['since']
            data = self.request('GET', f'/api/v1/{key}', params=params)
            rows = data.get('data', {}).get(key)
            if not isinstance(rows, list): raise IntegrationError('Неожиданная структура ответа Wildberries.')
            for row in rows: yield self.parse(row, kind)
            # Save only after the whole page was consumed and persisted by the caller.
            if len(rows) < self.page_size:
                cursor.update(stream=cursor['stream'] + 1, skip=0)
            else:
                cursor['skip'] += len(rows)
            self.checkpoint(dict(cursor))
        self.checkpoint({'watermark': cursor['until']})

    def send(self, thread, text):
        if thread.kind == 'review':
            self.request('POST', '/api/v1/feedbacks/answer', writing=True, json={'id': thread.external_id, 'text': text})
        else:
            self.request('PATCH', '/api/v1/questions', writing=True, json={'id': thread.external_id, 'answer': {'text': text}, 'state': 'wbRu'})
        return 'answer-' + thread.external_id

    def replies(self, thread):
        endpoint = 'feedback' if thread.kind == 'review' else 'question'
        data = self.request('GET', f'/api/v1/{endpoint}', params={'id': thread.external_id})
        return self.parse(data['data'], thread.kind).replies
