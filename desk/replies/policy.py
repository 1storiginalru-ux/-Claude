"""Deterministic reply routing. Materials teach wording; they never become facts."""
import re
from django.db.models import Q
from desk.models import ReplyMaterial, ReplyRule
from desk.voc.rules import human_only, matches, grouped_signals
from desk.reviews import assessment_is_current
from desk.ai.classification import local_classify, review_sections
from desk.ai.classification import COMPILED, SUPPRESS, normalize


# Retrieval only: these expressions rank examples, never classify reviews or
# create analytics signals. Keep explicit negation and the actual complaint.
LONGEVITY_WORDING = [re.compile(pattern) for pattern in [
    r'\b(?:держ\w*|хвата\w*).{0,20}\bнедолго\b',
    r'\b(?:слаб\w*|низк\w*)\s+стойкост\w*\b',
    r'\bна\s+(?:пару|два|2)\s+час\w*\s+хвата\w*.{0,50}(?:выветр\w*|исчез\w*|пропа\w*)',
]]


def wording_concerns(text):
    text = normalize(text)
    for pattern in SUPPRESS:
        text = pattern.sub(lambda match: ' ' * len(match.group()), text)
    positions = {}
    for concern, patterns in COMPILED.items():
        for pattern in [*patterns, *(LONGEVITY_WORDING if concern == 'LONG' else [])]:
            match = pattern.search(text)
            if match: positions[concern] = min(positions.get(concern, len(text)), match.start())
    return sorted(positions, key=lambda concern: (positions[concern], concern))


def lines(value): return [x.strip() for x in value.splitlines() if x.strip()]
def normalized(value): return value.casefold().replace('ё', 'е')
def contains(text, phrase): return bool(re.search(r'(?<!\w)' + re.escape(normalized(phrase)) + r'(?!\w)', normalized(text)))


def decision_for(thread, model=None):
    local = local_classify(thread.text, thread.rating, sections=review_sections(thread), kind=thread.kind)
    data = dict(model or local)
    assessment = getattr(thread, 'assessment', None)
    if assessment_is_current(thread) and assessment.decision != 'pending':
        data.update(sentiment='negative' if assessment.decision == 'negative' else ('positive' if local['sentiment'] == 'positive' else 'neutral'),
                    signals=assessment.signals, severity=assessment.severity, requires_human=True)
    elif local['sentiment'] == 'negative':
        # A generic positive template cannot override a strong textual complaint.
        data['sentiment'] = 'negative'
        data['signals'] = sorted(set(data['signals']) | set(local['signals']))
    if thread.kind=='question':
        topics={'CONTENT':r'объ[её]м|\bмл\b|крышк|тестер|комплектац|слюд',
                'LONG_AUTH':r'оригинал|подлинн', 'SCENT':r'нот[аы]|аромат|запах', 'DELIVERY':r'достав|привез|получу'}
        data['signals']=sorted(set(data['signals'])|{topic for topic,pattern in topics.items() if re.search(pattern,thread.text,re.IGNORECASE)})
    return data


def library_state(thread):
    company = thread.account.company
    product=thread.product if thread.product_id else None
    materials = list(ReplyMaterial.objects.filter(company=company, approved=True, active=True)
                     .filter(Q(product_code='')|Q(product_code=product.code if product else ''))
                     .filter(Q(platform='')|Q(platform=thread.account.platform))
                     .filter(Q(thread_kind='')|Q(thread_kind=thread.kind))
                     .filter(Q(brand='')|Q(brand__iexact=product.brand if product else ''))
                     .order_by('pk').values('id', 'kind', 'title', 'customer_text', 'answer', 'explanation',
                     'topic', 'covered_topics', 'sentiment', 'thread_kind', 'platform', 'product_code', 'brand', 'excluded_words', 'version'))
    rules = []
    for rule in ReplyRule.objects.filter(company=company, active=True).prefetch_related('accounts').order_by('priority', 'pk'):
        rules.append({'id': rule.pk, 'name': rule.name, 'priority': rule.priority, 'conditions': rule.conditions,
                      'condition_mode': rule.condition_mode, 'action': rule.action, 'material_id': rule.material_id,
                      'accounts': sorted(a.pk for a in rule.accounts.all()), 'version': rule.version})
    return materials, rules


def material_mismatch_reasons(material, thread, data):
    product = thread.product if thread.product_id else None
    checks = [
        (material.get('approved', True), 'не подтверждён'),
        (material.get('active', True), 'выключен'),
        (not material['platform'] or material['platform'] == thread.account.platform, 'другая площадка'),
        (not material['thread_kind'] or material['thread_kind'] == thread.kind, 'другой тип обращения'),
        (not material['sentiment'] or material['sentiment'] == data['sentiment'], 'другая тональность'),
        (not material['topic'] or matches(data['signals'], material['topic']), 'тема не совпала'),
        (not material['product_code'] or product and material['product_code'] == product.code, 'другой ProductID'),
        (not material['brand'] or product and normalized(material['brand']) == normalized(product.brand), 'другой бренд'),
        (not any(contains(thread.text, phrase) for phrase in lines(material['excluded_words'])), 'найдена исключающая фраза'),
    ]
    return [reason for passed, reason in checks if not passed]


def material_matches(material, thread, data):
    return not material_mismatch_reasons(material, thread, data)


def rule_matches(rule, thread, data, materials):
    if rule['accounts'] and thread.account_id not in rule['accounts']: return False
    conditions = rule['conditions']; checks = []
    product = thread.product if thread.product_id else None
    if 'kind' in conditions: checks.append(thread.kind == conditions['kind'])
    if 'ratings' in conditions: checks.append(thread.rating in conditions['ratings'])
    if 'signals' in conditions: checks.append(any(matches(data['signals'], s) for s in conditions['signals']))
    if 'sentiment' in conditions: checks.append(data['sentiment'] == conditions['sentiment'])
    if 'words' in conditions: checks.append(any(contains(thread.text, s) for s in conditions['words']))
    if 'brands' in conditions: checks.append(bool(product and normalized(product.brand) in {normalized(s) for s in conditions['brands']}))
    if 'products' in conditions: checks.append(bool(product and product.code in conditions['products']))
    if 'has_text' in conditions: checks.append((thread.text.strip() not in {'','(Отзыв без текста)','(Вопрос без текста)'}) == conditions['has_text'])
    if 'has_photo' in conditions: checks.append(bool(materials['photos']) == conditions['has_photo'])
    if 'has_video' in conditions: checks.append(bool(materials['videos']) == conditions['has_video'])
    if not checks: return True
    return all(checks) if rule['condition_mode'] == 'all' else any(checks)


def plan(thread, *, data=None, library=None, available_materials=None):
    from desk.ai.context import words
    from desk.integrations.content import content_view
    data = decision_for(thread, data)
    materials, rules = library or library_state(thread)
    if available_materials is None:
        content = content_view(thread)
        available_materials = {'photos': len(content['photos']), 'videos': len(content['videos'])}
    eligible = [m for m in materials if material_matches(m, thread, data)]
    applicable = [r for r in rules if rule_matches(r, thread, data, available_materials)]
    strict = thread.account.company.reply_sensitive_templates and data['sentiment'] == 'negative'
    if strict: applicable = [r for r in applicable if r['action'] != 'ai']
    selected = applicable[0] if applicable else None
    action = selected['action'] if selected else 'ai'
    template = next((m for m in eligible if m['kind'] == 'template' and selected and m['id'] == selected['material_id']), None)
    notes = []
    if action == 'template' and not template:
        action = 'manual' if strict else 'ai'
        notes.append('Шаблон сценария недоступен или не подходит этому обращению. ' + ('Нужен ответ сотрудника; свободная генерация отключена.' if strict else 'Подготовлен AI-черновик для проверки.'))
    coverage = set(template.get('covered_topics', [])) | {template['topic']} if template else set()
    if action == 'template' and (strict or human_only(data)) and any(not any(matches([s], topic) for topic in coverage) for s in grouped_signals(data['signals'])):
        action='manual' if strict else 'ai';template=None
        notes.append('Шаблон не покрывает все темы жалобы. ' + ('Обращение передано сотруднику без генерации.' if strict else 'AI учитывает жалобу целиком; решение принимает сотрудник.'))
    if strict and (action == 'ai' or data['severity'] == 'P0' or not data['signals']):
        action='manual';template=None
        notes.append('Негатив: разрешён только подходящий утверждённый шаблон. Для этого случая нужен ответ сотрудника; AI-текст не создаётся.')
    query = words(thread.text)
    concerns = wording_concerns(thread.text)
    examples = [m for m in eligible if m['kind'] == 'example']
    def relevance(material):
        similar = set(wording_concerns(material['customer_text'] + '\n' + material['title']))
        semantic = len(set(concerns) & similar) * 4 + (6 if concerns and concerns[0] in similar else 0)
        return len(query & words(material['customer_text'])) + bool(material['topic']) * 5 + bool(material['product_code']) * 10 + semantic
    examples.sort(key=lambda m: (-relevance(m), m['id']))
    return {'action': action, 'rule': selected, 'template': template, 'examples': examples[:3], 'notes': notes,
            'requires_human': bool(strict or data.get('requires_human') or human_only(data) or data.get('confidence', 0) < .7),
            'situation': data['sentiment'], 'all_topics': data['signals']}


def public_plan(value):
    return {'action': value['action'], 'rule': ({k: value['rule'][k] for k in ['id', 'name', 'priority', 'version']} if value['rule'] else None),
            'template': ({k: value['template'][k] for k in ['id', 'title', 'version']} if value['template'] else None),
            'examples': [{k: m[k] for k in ['id', 'title', 'version']} for m in value['examples']],
            'notes': value['notes'], 'requires_human': value['requires_human'], 'situation': value['situation'], 'all_topics': value['all_topics']}


def template_text(material, thread):
    from .greetings import apply_greeting
    product = thread.product if thread.product_id else None
    text = material['answer']
    if thread.account.reply_signature:
        text = text.replace('{{утверждённая_подпись}}',thread.account.reply_signature)
    for key, value in {'товар': product.name if product else '', 'бренд': product.brand if product else '', 'объем': product.volume if product else ''}.items():
        if value: text = text.replace('{{' + key + '}}', value)
    return apply_greeting(text, thread)
