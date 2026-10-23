import math
from .base import Adapter, Incoming, IntegrationError, timestamp
from .window import LOOKBACK_DAYS, sync_window, within_window


class Wildberries(Adapter):
    base_url = 'https://feedbacks-api.wildberries.ru'
    page_size = 1000
    streams = [('review', 'feedbacks', 'false'), ('question', 'questions', 'false'), ('review', 'feedbacks', 'true'), ('question', 'questions', 'true')]

    def before_request(self, token):
        from .rate_limit import acquire
        acquire(token)

    def after_response(self, token, response):
        from .rate_limit import observe
        return observe(token, response)

    def parse(self, row, kind):
        if not isinstance(row, dict): raise IntegrationError('Wildberries не вернул данные обращения.')
        product = row.get('productDetails') or {}
        text = '\n'.join(str(row.get(k) or '') for k in ['text', 'pros', 'cons']).strip()
        answer = row.get('answer') or {}
        replies = []
        if answer.get('text'):
            state = answer.get('state') if kind == 'review' else row.get('state')
            replies.append({'external_id': 'answer-' + str(row['id']), 'text': answer['text'], 'direction': 'out', 'published': state == 'wbRu', 'source_data': answer})
        return Incoming(str(row['id']), kind, text or '(Отзыв без текста)', str(product.get('supplierArticle') or ''), row.get('userName') or 'Покупатель', row.get('productValuation') if kind == 'review' else None, timestamp(row.get('createdDate')), replies, source_data=row, history_complete=True)

    def window_cursor(self, cursor=None, *, now=None):
        since, until = sync_window(now)
        lower, upper = math.ceil(since.timestamp()), int(until.timestamp())
        cursor = dict(self.sync_cursor if cursor is None else cursor)
        if (cursor.get('lookback_days') != LOOKBACK_DAYS or not isinstance(cursor.get('until'), int)
                or not lower <= cursor['until'] <= upper):
            return {'lookback_days': LOOKBACK_DAYS, 'since': lower, 'until': upper, 'stream': 0, 'skip': 0}
        # dateDesc pagination stays stable when only the oldest tail expires.
        cursor['since'] = lower
        return cursor

    def fetch(self):
        cursor = self.window_cursor()
        self.checkpoint(dict(cursor))
        streams = self.streams
        while cursor['stream'] < len(streams):
            cursor = self.window_cursor(cursor)
            kind, key, answered = streams[cursor['stream']]
            if cursor['skip'] >= 200000:
                raise IntegrationError('Достигнут предел истории WB; нужен отдельный backfill по периодам.')
            params = {'isAnswered': answered, 'take': self.page_size, 'skip': cursor['skip'], 'order': 'dateDesc', 'dateTo': cursor['until']}
            params['dateFrom'] = cursor['since']
            data = self.request('GET', f'/api/v1/{key}', params=params)
            body = data.get('data')
            rows = body.get(key) if isinstance(body, dict) else None
            if not isinstance(rows, list): raise IntegrationError('Неожиданная структура ответа Wildberries.')
            since, until = sync_window()
            for row in rows:
                if within_window(row.get('createdDate'), since, until): yield self.parse(row, kind)
            # Save only after the whole page was consumed and persisted by the caller.
            if len(rows) < self.page_size:
                cursor.update(stream=cursor['stream'] + 1, skip=0)
            else:
                cursor['skip'] += len(rows)
            self.checkpoint(dict(cursor))
        self.checkpoint({'watermark': cursor['until'], 'lookback_days': LOOKBACK_DAYS})

    def send(self, thread, text):
        if thread.kind == 'review':
            self.request('POST', '/api/v1/feedbacks/answer', writing=True, json={'id': thread.external_id, 'text': text})
        else:
            self.request('PATCH', '/api/v1/questions', writing=True, json={'id': thread.external_id, 'answer': {'text': text}, 'state': 'wbRu'})
        return 'answer-' + thread.external_id

    def replies(self, thread):
        return self.fetch_one(thread).replies

    def fetch_one(self, thread):
        endpoint = 'feedback' if thread.kind == 'review' else 'question'
        data = self.request('GET', f'/api/v1/{endpoint}', params={'id': thread.external_id})
        return self.parse(data['data'], thread.kind)
