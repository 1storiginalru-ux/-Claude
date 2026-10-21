from .base import Adapter, Incoming, IntegrationError, timestamp


class Wildberries(Adapter):
    base_url = 'https://feedbacks-api.wildberries.ru'

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
        for kind, key in [('review', 'feedbacks'), ('question', 'questions')]:
            for answered in ['false', 'true']:
                for page in range(2000):
                    data = self.request('GET', f'/api/v1/{key}', params={'isAnswered': answered, 'take': 100, 'skip': page * 100, 'order': 'dateAsc'})
                    rows = data.get('data', {}).get(key)
                    if not isinstance(rows, list): raise IntegrationError('Неожиданная структура ответа Wildberries.')
                    for row in rows: yield self.parse(row, kind)
                    if len(rows) < 100: break
                else: raise IntegrationError('Достигнут предел страниц WB; требуется backfill.')

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
