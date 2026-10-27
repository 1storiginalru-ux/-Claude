"""Presentation of marketplace payloads; original fields remain in source_data."""
import ipaddress
import json
from urllib.parse import urlsplit


def mapping(value):
    return value if isinstance(value, dict) else {}


def media_url(value):
    if not isinstance(value, str) or len(value) > 16384 or any(char.isspace() or not char.isprintable() for char in value): return ''
    try:
        parts = urlsplit(value)
        host = parts.hostname or ''
        if parts.scheme != 'https' or not host or parts.username or parts.password: return ''
        if host == 'localhost' or host.endswith(('.localhost', '.local', '.internal')): return ''
        try:
            if not ipaddress.ip_address(host).is_global: return ''
        except ValueError:
            if '.' not in host: return ''
        if parts.port is not None and not 1 <= parts.port <= 65535: return ''
    except ValueError:
        return ''
    return value


def content_view(thread):
    data = mapping(thread.source_data)
    photos, videos, sections, info = [], [], [], []
    if thread.account.platform == 'wb':
        parts = [('Комментарий', data.get('text')), ('Достоинства', data.get('pros')), ('Недостатки', data.get('cons'))]
        for photo in data.get('photoLinks') or []:
            if not isinstance(photo, dict): continue
            full = media_url(photo.get('fullSize'))
            if full: photos.append({'url': full, 'preview': media_url(photo.get('miniSize')) or full})
        video = data.get('video')
        if isinstance(video, dict) and media_url(video.get('link')):
            videos.append({'url': media_url(video['link']), 'preview': media_url(video.get('previewImage')), 'duration': video.get('durationSec')})
        product = mapping(data.get('productDetails'))
        pairs = [
            ('Название товара', product.get('productName')), ('Бренд', product.get('brandName')),
            ('Артикул WB', product.get('nmId')), ('ID карточки WB', product.get('imtId')),
            ('Артикул продавца', product.get('supplierArticle')),
            ('Размер', product.get('size')), ('Цвет', data.get('color')),
            ('Статус заказа', {'buyout': 'Выкуплен', 'rejected': 'Отказ', 'returned': 'Возврат', 'notSpecified': 'Не указан'}.get(data.get('orderStatus'), data.get('orderStatus'))),
            ('Дата покупки', data.get('lastOrderCreatedAt')), ('Категория', data.get('subjectName')),
            ('Соответствие размера', data.get('matchingSize')), ('Теги покупателя', data.get('bables')),
            ('Исходный отзыв', data.get('parentFeedbackId')), ('Дополнение к отзыву', data.get('childFeedbackId')),
        ]
    elif thread.account.platform == 'ozon':
        parts = [('Комментарий', data.get('text'))]
        for photo in data.get('photos') or []:
            full = media_url(mapping(photo).get('url'))
            if full: photos.append({'url': full, 'preview': full})
        for video in data.get('videos') or []:
            value = mapping(video)
            url = media_url(value.get('url'))
            if url: videos.append({'url': url, 'preview': media_url(value.get('preview_url')), 'duration': None})
        pairs = [('SKU Ozon', data.get('sku')), ('Статус на Ozon', data.get('status')),
                 ('Статус заказа', data.get('order_status')), ('Учитывается в рейтинге', {True:'Да',False:'Нет'}.get(data.get('is_rating_participant'))),
                 ('Комментариев', data.get('comments_amount')), ('Ответов', data.get('answers_count')),
                 ('Голосов за', data.get('likes_amount')), ('Голосов против', data.get('dislikes_amount'))]
    else:
        description = mapping(data.get('description'))
        parts = [('Комментарий', description.get('comment')), ('Достоинства', description.get('advantages')), ('Недостатки', description.get('disadvantages'))]
        media = mapping(data.get('media'))
        for url in media.get('photos') or []:
            if media_url(url): photos.append({'url': url, 'preview': url})
        for url in media.get('videos') or []:
            if media_url(url): videos.append({'url': url, 'preview': '', 'duration': None})
        ids = mapping(data.get('identifiers') or data.get('questionIdentifiers'))
        statistics = mapping(data.get('statistics'))
        pairs = [('Артикул продавца', ids.get('offerId')), ('ID заказа', ids.get('orderId')),
                 ('Нужна реакция', {True: 'Да', False: 'Нет'}.get(data.get('needReaction'))),
                 ('Комментариев', statistics.get('commentsCount')), ('Баллов за отзыв', statistics.get('paidAmount')),
                 ('Рекомендует товар', {True: 'Да', False: 'Нет'}.get(statistics.get('recommended')))]
        votes = mapping(data.get('votes'))
        pairs.extend([('Голосов за', votes.get('positive')), ('Голосов против', votes.get('negative'))])
    for label, value in parts:
        if value: sections.append({'label': label, 'text': value})
    for label, value in pairs:
        if value is not None and value != '':
            info.append({'label': label, 'value': ', '.join(map(str, value)) if isinstance(value, list) else value})
    photos = list({photo['url']: photo for photo in photos}.values())
    videos = list({video['url']: video for video in videos}.values())
    return {'photos': photos, 'videos': videos, 'sections': sections, 'info': info, 'raw': json.dumps(data, ensure_ascii=False, indent=2)}
