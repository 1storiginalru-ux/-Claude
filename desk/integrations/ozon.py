"""Ozon Seller feedback API. Credentials and budgets stay scoped to the seller."""
from datetime import timedelta
from django.utils import timezone
from desk.models import Outgoing
from desk.products.identifiers import wb_nm_id
from .base import Adapter, Incoming, IntegrationError, timestamp
from .rate_limit import acquire, observe
from .wb_sales import source_time
from .window import LOOKBACK_DAYS, sync_window, within_window


class Ozon(Adapter):
    base_url = 'https://api-seller.ozon.ru'
    page_size = 100

    def request_headers(self, token):
        if not self.account.ozon_client_id.isdigit():
            raise IntegrationError('Для Ozon нужен числовой Client-Id.', request_sent=False)
        return {'Api-Key': token, 'Client-Id': self.account.ozon_client_id}

    def before_request(self, token):
        acquire(token, namespace='ozon-feedbacks:'+self.account.ozon_client_id, label='Ozon')

    def after_response(self, token, response):
        return observe(token, response, namespace='ozon-feedbacks:'+self.account.ozon_client_id)

    def access_error(self, status):
        if status in {401, 403}:
            return f'Ozon: HTTP {status}. Проверьте Client-Id, права ключа и доступ к отзывам/вопросам по подписке Premium Plus.'
        return super().access_error(status)

    def parse(self, row, kind):
        if not isinstance(row, dict): raise IntegrationError('Ozon вернул некорректное обращение.')
        identifier, sku = row.get('id'), wb_nm_id(row.get('sku'))
        created = source_time(row.get('published_at'))
        if not isinstance(identifier, str) or not 1 <= len(identifier) <= 160 or not sku or not created:
            raise IntegrationError('В обращении Ozon нет достоверного ID, SKU или даты публикации.')
        text = row.get('text') or ''
        if not isinstance(text, str): raise IntegrationError('Ozon вернул некорректный текст обращения.')
        rating = row.get('rating') if kind == 'review' else None
        if kind == 'review' and (type(rating) is not int or not 1 <= rating <= 5):
            raise IntegrationError('Ozon вернул некорректную оценку отзыва.')
        return Incoming(identifier, kind, text or '(Отзыв без текста)', sku,
                        row.get('author_name') or 'Покупатель Ozon', rating, created,
                        source_data=row, history_complete=False)

    def page(self, kind, since, until, cursor=None):
        """A fixed date interval and one resumable page, also used by history."""
        token = (cursor or {}).get('last_id')
        if token is not None and not isinstance(token, str): raise IntegrationError('Некорректная позиция страницы Ozon.')
        body = {'limit': self.page_size, 'sort_dir': 'DESC'}
        if token: body['last_id'] = token
        if kind == 'review':
            body['filters'] = {'status': 'ALL', 'order_status': 'ALL',
                               'published_from': (since-timedelta(microseconds=1)).isoformat(),
                               'published_to': (until+timedelta(microseconds=1)).isoformat()}
            path, key = '/v2/review/list', 'reviews'
        else:
            body['filter'] = {'status': 'ALL', 'date_from': since.isoformat(), 'date_to': until.isoformat()}
            path, key = '/v1/question/list', 'questions'
        response = self.request('POST', path, json=body)
        rows = response.get(key) if isinstance(response, dict) else None
        if not isinstance(rows, list) or len(rows) > self.page_size:
            raise IntegrationError('Неожиданный формат списка обращений Ozon.')
        # Validate a complete page before yielding any part of it.
        for row in rows: self.parse(row, kind)
        more, next_id = response.get('has_next'), response.get('last_id')
        if kind == 'review' and type(more) is not bool:
            raise IntegrationError('Ozon не подтвердил завершение списка отзывов.')
        if more is not None and type(more) is not bool: raise IntegrationError('Некорректная пагинация Ozon.')
        more = more if more is not None else bool(rows and next_id)
        if more and (not rows or not isinstance(next_id, str) or not next_id or next_id == token):
            raise IntegrationError('Страница Ozon не продвигает позицию загрузки.')
        selected = [row for row in rows if within_window(row['published_at'], since, until)]
        detailed = [self.with_media(row) if kind == 'review' else row for row in selected]
        return detailed, ({'last_id': next_id} if more else {})

    def with_media(self, row):
        # The list omits media URLs. Fetch the info endpoint when attachments
        # exist; absent counters are also insufficient proof of an empty list.
        if any(row.get(key) != 0 for key in ['photos_amount', 'videos_amount']):
            info = self.request('POST', '/v2/review/info', json={'review_id': row['id']})
            self.parse(info, 'review')
            if info['id'] != row['id'] or str(info['sku']) != str(row['sku']):
                raise IntegrationError('Ozon вернул другой отзыв при дозагрузке медиа.')
            return {**row, **info}
        return row

    def fetch(self):
        since, now = sync_window()
        cursor = dict(self.sync_cursor)
        until = source_time(cursor.get('until'))
        if (cursor.get('ozon_version') != 2 or cursor.get('lookback_days') != LOOKBACK_DAYS
                or not until or not since < until <= now):
            cursor = {'ozon_version': 2, 'lookback_days': LOOKBACK_DAYS, 'until': now.isoformat(), 'stream': 0}
            until = now
        if cursor.get('stream') not in {0, 1, 2}: raise IntegrationError('Некорректная позиция потока Ozon.')
        self.checkpoint(dict(cursor))
        for _ in range(1000):
            if cursor['stream'] == 2:
                self.completed_window = (max(since, sync_window()[0]), until)
                self.checkpoint({})
                return
            kind = ['review', 'question'][cursor['stream']]
            rows, continuation = self.page(kind, max(since, sync_window()[0]), until, cursor)
            for row in rows: yield self.parse(row, kind)
            cursor.pop('last_id', None)
            if continuation: cursor.update(continuation)
            else: cursor['stream'] += 1
            self.checkpoint(dict(cursor))
        raise IntegrationError('Достигнут предел страниц Ozon; полнота не подтверждена.')

    def fetch_one(self, thread):
        path, key = ('/v2/review/info', 'review_id') if thread.kind == 'review' else ('/v1/question/info', 'question_id')
        row = self.request('POST', path, json={key: thread.external_id})
        result = self.parse(row, thread.kind)
        if result.external_id != thread.external_id or result.sku != thread.sku:
            raise IntegrationError('Ozon вернул другое обращение при запросе по ID.')
        return result

    def send(self, thread, text):
        if not 2 <= len(text.strip()) <= 3000:
            raise IntegrationError('Ответ Ozon должен содержать от 2 до 3000 символов.', request_sent=False)
        if thread.kind == 'review':
            path = '/v1/review/comment/create'
            body = {'review_id': thread.external_id, 'text': text, 'mark_review_as_processed': False}
            key = 'comment_id'
        else:
            sku = wb_nm_id(thread.sku)
            if not sku: raise IntegrationError('Для ответа нужен числовой SKU Ozon.', request_sent=False)
            path = '/v1/question/answer/create'
            body = {'question_id': thread.external_id, 'sku': int(sku), 'text': text}
            key = 'answer_id'
        response = self.request('POST', path, writing=True, json=body)
        identifier = response.get(key) if isinstance(response, dict) else None
        if not isinstance(identifier, str) or not 1 <= len(identifier) <= 160:
            raise IntegrationError('Ozon принял запрос без ID ответа; нужна сверка публикации.', ambiguous=True)
        return identifier

    def history(self, thread, *, cursor=None, checkpoint=None):
        cursor = dict(cursor or {})
        if cursor.get('complete'): return
        for _ in range(1000):
            if thread.kind == 'review':
                offset = cursor.get('offset', 0)
                if type(offset) is not int or not 0 <= offset < 100000:
                    raise IntegrationError('Достигнут предел истории комментариев Ozon.')
                response = self.request('POST', '/v1/review/comment/list', json={'review_id': thread.external_id, 'limit': 100, 'offset': offset, 'sort_dir': 'ASC'})
                rows = response.get('comments') if isinstance(response, dict) else None
                continuation = {'offset': offset+100} if isinstance(rows, list) and len(rows) == 100 else {}
            else:
                sku = wb_nm_id(thread.sku)
                if not sku: raise IntegrationError('Для истории ответов нужен числовой SKU Ozon.')
                body = {'question_id': thread.external_id, 'sku': int(sku)}
                if cursor.get('last_id'): body['last_id'] = cursor['last_id']
                response = self.request('POST', '/v1/question/answer/list', json=body)
                rows = response.get('answers') if isinstance(response, dict) else None
                token = response.get('last_id') if isinstance(response, dict) else None
                if rows and token and (not isinstance(token, str) or token == cursor.get('last_id')):
                    raise IntegrationError('Ozon повторил позицию истории ответов.')
                continuation = {'last_id': token} if rows and token else {}
            if not isinstance(rows, list) or len(rows) > 100: raise IntegrationError('Неожиданный формат истории ответов Ozon.')
            ids = [row.get('id') if isinstance(row, dict) else None for row in rows]
            if rows and cursor.get('ids') == ids: raise IntegrationError('Ozon повторил страницу комментариев.')
            parsed = [self.parse_reply(row, thread) for row in rows]
            yield from parsed
            if continuation: continuation['ids'] = ids
            if checkpoint: checkpoint(continuation or {'complete': True})
            if not continuation: return
            cursor = continuation
        raise IntegrationError('Достигнут предел страниц истории Ozon.')

    def parse_reply(self, row, thread):
        if not isinstance(row, dict) or not isinstance(row.get('id'), str) or not row['id'] or not isinstance(row.get('text'), str):
            raise IntegrationError('Ozon вернул некорректный ответ.')
        if thread.kind == 'review':
            owned = row.get('is_owner') is True
            rejected = row.get('is_rejected') is True
            published = row.get('is_published') is True if 'is_published' in row else bool(source_time(row.get('published_at')))
        else:
            # Author names do not prove ownership. Only our recorded answer ID
            # can confirm a question reply; other people's answers stay distinct.
            owned = Outgoing.objects.filter(thread=thread, external_id=row['id']).exists()
            rejected = row.get('status_publication') == 'MODERATION_FAILED'
            published = row.get('status_publication') == 'PUBLISHED' if 'status_publication' in row else bool(source_time(row.get('published_at')))
        return {'external_id': row['id'], 'text': row['text'], 'direction': 'out' if owned else 'in',
                'published': published and not rejected, 'rejected': rejected and owned,
                'owned': owned, 'allow_reopen': thread.kind == 'review' and not owned,
                'created_at': timestamp(row.get('published_at')), 'source_data': row}

    def replies(self, thread):
        return list(self.history(thread))
