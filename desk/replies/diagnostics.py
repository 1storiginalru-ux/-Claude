"""Read-only explanation of recorded routing. Never regenerates a draft."""
from .policy import decision_for, material_mismatch_reasons, plan, public_plan
from desk.models import ReplyMaterial


def saved_context(draft):
    snapshot = draft.job.snapshot if draft.job_id else {}
    explanation = draft.explanation or {}
    library = snapshot.get('reply_library') or [[], []]
    by_id = {row['id']: row for row in library[0]}
    examples = []
    for selected in explanation.get('examples', []):
        # Use the version frozen for this generation, never today's edited text.
        original = by_id.get(selected['id'])
        examples.append({**selected, 'answer': original['answer'] if original else ''})
    template = explanation.get('template')
    original_template = by_id.get(template['id']) if template else None
    return {'mode': explanation.get('mode', 'guided'), 'pipeline': explanation.get('version') or snapshot.get('version', ''),
            'action': explanation.get('action', draft.method), 'method': draft.get_method_display(),
            'rule': explanation.get('rule'), 'template': template,
            'template_answer': original_template['answer'] if original_template else '',
            'examples': examples, 'style_chars': len(snapshot.get('tone_of_voice', '')),
            'facts': explanation.get('facts', snapshot.get('facts', [])), 'notes': explanation.get('notes', []),
            'greeting': snapshot.get('customer_greeting',''), 'strict_templates': snapshot.get('strict_templates',False)}


def current_selection(subject):
    data = decision_for(subject)
    routing = plan(subject, data=data)
    selected_ids = {m['id'] for m in routing['examples']}
    template_id = routing['template']['id'] if routing['template'] else None
    candidates = []
    for material in ReplyMaterial.objects.filter(company=subject.account.company).order_by('pk').values():
        reasons = material_mismatch_reasons(material, subject, data)
        if material['id'] in selected_ids:
            status = 'выбран как пример для AI' if routing['action'] == 'ai' else 'подходит как пример, но сценарий не вызывает AI'
        elif material['id'] == template_id:
            status = 'выбран для подстановки'
        elif reasons:
            status = '; '.join(reasons)
        elif material['kind'] == 'template':
            status = 'подходит по полям, но не выбран действием сценария'
        else:
            status = 'подходит, но вне трёх первых по сходству'
        candidates.append({'id': material['id'], 'title': material['title'], 'kind': material['kind'],
                           'version': material['version'], 'status': status})
    return {'plan': public_plan(routing), 'candidates': candidates}
