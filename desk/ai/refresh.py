"""Versioned local repair; never calls an AI/marketplace or changes a human decision."""
from django.db import transaction
from desk.models import Analysis, Audit, ReviewAssessment, Thread
from .classification import local_classify, review_sections, RULES_VERSION

FIELDS = ['sentiment', 'hidden_negative', 'signals', 'severity', 'objective_issue', 'requires_human',
          'confidence', 'summary', 'draft', 'provider', 'model', 'prompt_version']


@transaction.atomic
def refresh_rules(thread_id, *, apply=False):
    thread = Thread.objects.select_for_update().select_related('account__company').get(pk=thread_id)
    if ReviewAssessment.objects.filter(thread=thread).exists(): return None
    analysis = Analysis.objects.select_for_update().filter(thread=thread, provider='rules').first()
    if not analysis or analysis.prompt_version == RULES_VERSION: return None
    before = {field: getattr(analysis, field) for field in FIELDS}
    data = local_classify(thread.text, thread.rating, sections=review_sections(thread), kind=thread.kind)
    after = {**data, 'provider': 'rules', 'model': '', 'prompt_version': RULES_VERSION}
    if apply:
        for field, value in after.items(): setattr(analysis, field, value)
        analysis.save(update_fields=FIELDS)
        Audit.objects.create(company=thread.account.company, action='analysis.rules_refresh', object_id=str(thread.pk), before=before, after=after)
    return before, after
