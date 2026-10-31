"""Exact rating-only reviews: a known score, not evidence about product quality."""
import json
from django.db import transaction
from desk.models import Analysis, Audit, Thread

VERSION = 'rating-1'
EMPTY_TEXT = '(Отзыв без текста)'


def rating_result(thread):
    if thread.kind != 'review' or type(thread.rating) is not int or not 1 <= thread.rating <= 5:
        return None
    if thread.account.platform not in {'wb', 'ym'} or thread.text.strip() not in {'', EMPTY_TEXT}:
        return None
    source = thread.source_data
    if not isinstance(source, dict): return None
    # Inspect raw fields as well: stale/edited combined text must never hide a
    # complaint, attachment or marketplace feedback tag.
    if thread.account.platform == 'wb':
        fields = [source.get(key) for key in ['text', 'pros', 'cons']]
        if any(source.get(key) for key in ['photoLinks', 'video', 'bables', 'matchingSize']): return None
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
    negative = thread.rating <= 3
    return {
        'sentiment': 'negative' if negative else 'positive', 'hidden_negative': False,
        'signals': ['OTHER'] if negative else [], 'severity': 'P2' if negative else 'P3',
        'objective_issue': False, 'requires_human': negative, 'confidence': 1.0,
        'summary': f'Отзыв без текста: {thread.rating} ★. Разобрана только оценка, без AI. '
                   'Причина оценки и свойства товара неизвестны.',
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
    evidence['classification_basis'] = {'kind': 'rating_only', 'rating': thread.rating, 'version': VERSION}
    result, _ = Analysis.objects.update_or_create(thread=thread, defaults={**fields, 'evidence': evidence})
    Audit.objects.create(company=thread.account.company, actor=actor, action='analysis.rating', object_id=str(thread.pk),
                         before=json.loads(json.dumps(before, default=str)), after={**fields, 'evidence': evidence})
    return result
