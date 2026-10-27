"""Human decisions override AI output without modifying its original result."""
import hashlib
import json
from types import SimpleNamespace
from django.db import transaction
from desk.models import Audit, ReviewAssessment, Thread


def fingerprint(thread):
    source = [thread.text, thread.rating, thread.sku, thread.product_id]
    return hashlib.sha256(json.dumps(source, ensure_ascii=False).encode()).hexdigest()


def assessment_is_current(thread):
    assessment = getattr(thread, 'assessment', None)
    return bool(assessment and assessment.source_fingerprint == fingerprint(thread))


def effective_analysis(thread):
    assessment = getattr(thread, 'assessment', None)
    if assessment:
        if not assessment_is_current(thread) or assessment.decision == 'pending':
            return None
        return SimpleNamespace(
            sentiment='negative' if assessment.decision == 'negative' else 'neutral',
            hidden_negative=False, signals=assessment.signals,
            severity=assessment.severity, objective_issue=assessment.objective_issue,
            provider='human', confidence=1, requires_human=True,
        )
    return getattr(thread, 'analysis', None)


@transaction.atomic
def assess_review(thread_id, actor, cleaned_data):
    from desk.access import OPERATORS, QUALITY, require_role
    from desk.incidents.service import evaluate_incidents
    from desk.models import Product
    from desk.voc.rules import product_family
    thread = Thread.objects.select_related('account', 'product').get(pk=thread_id)
    require_role(actor, thread.account.company_id, list(set(OPERATORS + QUALITY)))
    # All review/incident writers acquire the family lock before locking a review.
    # Otherwise two confirmations can each hold a review the other incident needs.
    if thread.product_id:
        canonical = product_family(thread.product).order_by('pk').first()
        Product.objects.select_for_update().get(pk=canonical.pk)
    thread = Thread.objects.select_for_update().select_related('account').get(pk=thread_id)
    if thread.kind != 'review':
        raise ValueError('Подтверждение негатива доступно для отзывов.')
    if cleaned_data['source_fingerprint'] != fingerprint(thread):
        raise ValueError('Отзыв изменился. Обновите страницу и проверьте новую версию.')
    previous = ReviewAssessment.objects.filter(thread=thread).values().first()
    version = previous['reviewed_at'].isoformat() if previous else ''
    if cleaned_data['assessment_version'] != version:
        raise ValueError('Другой сотрудник уже изменил разметку. Обновите страницу.')
    fields = {key: cleaned_data[key] for key in ['decision', 'signals', 'severity', 'objective_issue', 'comment', 'source_fingerprint']}
    if fields['decision'] == 'negative' and not fields['signals']:
        fields['signals'] = ['OTHER']
    assessment, _ = ReviewAssessment.objects.update_or_create(thread=thread, defaults={**fields, 'reviewed_by': actor})
    def serializable(data):
        return {key: value.isoformat() if hasattr(value, 'isoformat') else value for key, value in (data or {}).items()}
    Audit.objects.create(company=thread.account.company, actor=actor, action='review.assess', object_id=str(thread.pk),
                         before=serializable(previous), after={**fields, 'reviewed_by': actor.pk})
    # Use a fresh relation cache: earlier AI runs and concurrent views cannot mask the decision.
    thread = Thread.objects.select_related('assessment', 'analysis', 'account', 'product').get(pk=thread.pk)
    if effective_analysis(thread) is not None:
        evaluate_incidents(thread, actor=actor)
    return assessment
