"""Exact rating-only reviews: a known score, not evidence about product quality."""
import json
from django.db import transaction
from desk.models import Analysis, Audit, Thread

VERSION = 'rating-2'
EMPTY_TEXT = '(Отзыв без текста)'
POSITIVE_TAGS = {'цвет', 'качество', 'текстура', 'хороший эффект', 'хорошо пахнет', 'удобно пользоваться'}
NEGATIVE_TAGS = {'плохое качество': 'OTHER', 'плохо пахнет': 'SCENT', 'нет эффекта': 'OTHER'}


def rating_basis(thread):
    source = thread.source_data if isinstance(thread.source_data, dict) else {}
    tags = source.get('bables') if thread.account.platform == 'wb' else None
    return {'kind': 'rating_and_tags' if tags else 'rating_only', 'rating': thread.rating,
            'tags': tags or [], 'version': VERSION}


def rating_result(thread):
    if thread.kind != 'review' or type(thread.rating) is not int or not 1 <= thread.rating <= 5:
        return None
    if thread.account.platform not in {'wb', 'ym'} or thread.text.strip() not in {'', EMPTY_TEXT}:
        return None
    source = thread.source_data
    if not isinstance(source, dict): return None
    tags, negative_signals = [], set()
    # Inspect raw fields as well: stale/edited combined text must never hide a
    # complaint, attachment or marketplace feedback tag.
    if thread.account.platform == 'wb':
        fields = [source.get(key) for key in ['text', 'pros', 'cons']]
        if any(source.get(key) for key in ['photoLinks', 'video', 'matchingSize']): return None
        raw_tags = source.get('bables')
        if raw_tags is not None:
            if not isinstance(raw_tags, list) or any(not isinstance(t, str) for t in raw_tags): return None
            tags = [t.strip().casefold().replace('ё', 'е') for t in raw_tags]
            if any(t not in POSITIVE_TAGS and t not in NEGATIVE_TAGS for t in tags): return None
            negative_signals = {NEGATIVE_TAGS[t] for t in tags if t in NEGATIVE_TAGS}
        score = source.get('productValuation', thread.rating)
    else:
        description = source.get('description')
        if description is not None and not isinstance(description, dict): return None
        fields = [(description or {}).get(key) for key in ['comment', 'advantages', 'disadvantages']]
        media = source.get('media')
        if media is not None and not isinstance(media, dict): return None
        if any((media or {}).values()): return None
        statistics = source.get('statistics')
        if statistics is not None and not isinstance(statistics, dict): return None
        if (statistics or {}).get('recommended') is False: return None
        score = (statistics or {}).get('rating', thread.rating)
    if type(score) is not int or score != thread.rating or any(value is not None and (not isinstance(value, str) or value.strip()) for value in fields):
        return None
    negative = thread.rating <= 3 or bool(negative_signals)
    summary = (f'Отзыв без текста: {thread.rating} ★. Разобрана только оценка, без AI. '
               'Причина оценки и свойства товара неизвестны.')
    if tags:
        summary = (f'Отзыв без текста: {thread.rating} ★; теги покупателя: {", ".join(tags)}. '
                   'Разобраны оценка и выбранные теги, без AI. Свойства товара и наличие дефекта не проверялись.')
    return {
        'sentiment': 'negative' if negative else 'positive',
        'hidden_negative': bool(thread.rating >= 4 and negative_signals),
        'signals': sorted(negative_signals) if negative_signals else ['OTHER'] if negative else [],
        'severity': 'P2' if negative else 'P3',
        'objective_issue': False, 'requires_human': negative, 'confidence': 1.0,
        'summary': summary,
        'draft': '',
    }


@transaction.atomic
def classify_rating(thread, actor=None):
    """Preserve every human decision and reply; never create paid/outgoing work."""
    thread = Thread.objects.select_for_update(of=('self',), no_key=True).select_related(
        'account__company', 'product', 'assessment').get(pk=thread.pk)
    if thread.account.company.is_demo or not thread.account.enabled or getattr(thread, 'assessment', None):
        return None
    data = rating_result(thread)
    if data is None: return None
    from .context import source_digest
    source = source_digest(thread)
    old = Analysis.objects.filter(thread=thread).first()
    fields = {**data, 'provider': 'rating', 'model': '', 'prompt_version': VERSION, 'source_fingerprint': source}
    fields.pop('draft')
    if old and all(getattr(old, key) == value for key, value in fields.items()): return old
    before = Analysis.objects.filter(thread=thread).values().first() or {}
    evidence = dict(old.evidence or {}) if old else {}
    evidence['classification_basis'] = rating_basis(thread)
    result, _ = Analysis.objects.update_or_create(thread=thread, defaults={**fields, 'evidence': evidence})
    Audit.objects.create(company=thread.account.company, actor=actor, action='analysis.rating', object_id=str(thread.pk),
                         before=json.loads(json.dumps(before, default=str)), after={**fields, 'evidence': evidence})
    return result
