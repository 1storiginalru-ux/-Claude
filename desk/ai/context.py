"""Local, company-scoped retrieval. No provider-hosted memory or vector service."""
import hashlib
import json
import re
from django.db.models import Q
from desk.models import KnowledgeEntry
from .classification import local_classify, review_sections

PIPELINE_VERSION = 'voice-ai-4.1'


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, default=str).encode()).hexdigest()


def source_context(thread):
    product = thread.product if thread.product_id else None
    return {'text': thread.text, 'rating': thread.rating, 'kind': thread.kind,
            'platform': thread.account.platform, 'channel': thread.account.get_platform_display(),
            'is_public': thread.kind in {'review', 'question'},
            'review_sections': review_sections(thread), 'sku': thread.sku,
            'product': {'id': thread.product_id, 'code': product.code, 'name': product.name,
                        'brand': product.brand, 'volume': product.volume,
                        'concentration': product.concentration} if product else None}


def source_digest(thread): return digest(source_context(thread))


def words(text):
    return {word[:7] for word in re.findall(r'[а-яёa-z0-9]{4,}', text.lower())}


def platform_tag(text):
    marker = re.match(r'^\s*\[(WB|YM|OZON|ALL)\]\s*', text, re.IGNORECASE)
    return marker.group(1).lower() if marker else None


def platform_matches(text, platform):
    """Optional explicit paragraph scope, e.g. [WB], [YM], [OZON], [ALL]."""
    tag = platform_tag(text)
    return not tag or tag in {'all', platform}


def retrieval_query(source):
    """Expand short complaints using local topics, without a third AI call."""
    query = words(source['text'])
    local = local_classify(source['text'], source['rating'], sections=source['review_sections'], kind=source['kind'])
    signals = local['signals']
    needs_return = local['sentiment'] == 'negative' and any(s in {'LONG', 'LONG_AUTH', 'RETURN'} or s.startswith('AUTH') for s in signals)
    if needs_return:
        query |= words('сомнения качество оригинальность дополнительная гарантия возврат вернуть заказ заявка инструкция вскрытие распыление')
    if 'PACK' in signals or 'DEFECT' in signals:
        query |= words('повреждение протечка упаковка комплектация распылитель')
    if 'RETURN' in signals:
        query |= words('возврат заявка деньги сроки')
    return query, needs_return


def fact_score(text, title, query, platform, needs_return):
    score = len(query & words(title + ' ' + text))
    if platform_tag(text) == platform:
        if score: score += 2
        # Prefer the customer's actual route, rather than a paragraph of timers.
        if needs_return and re.search(r'профиль|выбирает заказ|«Вернуть заказ»', text, re.IGNORECASE): score += 12
    if needs_return and re.search(r'только оригинальн|безоговорочный возврат|допустимы вскрытие', text, re.IGNORECASE): score += 8
    return score


def prepare(thread, *, history_override=None, mode='guided', materials_override=None):
    company = thread.account.company
    source = source_context(thread)
    code = thread.product.code if thread.product_id else ''
    entries = list(KnowledgeEntry.objects.filter(company=company, approved=True, active=True)
                   .filter(Q(product_code='') | Q(product_code=code)).order_by('pk')
                   .values('id', 'title', 'content', 'source', 'product_code', 'updated_at'))
    query, needs_return = retrieval_query(source)
    query |= words(thread.product.name if thread.product_id else '')
    candidates = []
    for entry in entries:
        if not entry['source'].strip() or len(entry['content']) > 3000 or not platform_matches(entry['content'], thread.account.platform): continue
        score = fact_score(entry['content'], entry['title'], query, thread.account.platform, needs_return)
        # All approved facts for this product are eligible; global facts need a
        # lexical match. Nothing from another company or product enters context.
        if entry['product_code'] and code: score += 20
        if score:
            candidates.append((score, f"fact:{entry['id']}:{digest(entry['content'])[:12]}", entry['content'], entry['source']))
    # The existing field is explicitly maintained as verified knowledge. Keep it
    # backwards compatible; use whole paragraphs, not arbitrary text slices.
    for i, paragraph in enumerate(re.split(r'\n\s*\n', company.knowledge.strip())):
        paragraph = paragraph.strip()
        if not paragraph or len(paragraph) > 3000 or not platform_matches(paragraph, thread.account.platform): continue
        score = fact_score(paragraph, '', query, thread.account.platform, needs_return)
        if score: candidates.append((score, f'profile:{i}:{digest(paragraph)[:12]}', paragraph, 'Подтверждённые знания компании'))
    selected, size = [], 0
    for score, identifier, text, origin in sorted(candidates, key=lambda row: (-row[0], row[1])):
        if len(selected) >= 6: break
        if size + len(text) > 6000: continue
        selected.append({'id': identifier, 'text': text, 'source': origin})
        size += len(text)
    from desk.reviews import assessment_is_current
    assessment = getattr(thread, 'assessment', None)
    decision = ({'decision': assessment.decision, 'signals': assessment.signals,
                 'severity': assessment.severity, 'objective_issue': assessment.objective_issue}
                if assessment_is_current(thread) else None)
    history = (list(thread.messages.order_by('-created_at', '-pk').values('direction', 'text')[:6])[::-1]
               if history_override is None else history_override)
    # Classification never sees this history or the brand's style instructions.
    history = [{'direction': m['direction'], 'text': m['text'][:2000]} for m in history]
    from desk.integrations.content import content_view
    content = content_view(thread)
    materials = {'photos': len(content['photos']), 'videos': len(content['videos']),
                 'images_analyzed': False,
                 'order_reference_available': any(item['label'] == 'ID заказа' for item in content['info'])}
    if materials_override is not None: materials=materials_override
    from desk.replies.policy import library_state
    library = library_state(thread)
    style = (company.ai_working_style or company.tone_of_voice) if mode == 'guided' else company.tone_of_voice
    settings = {'provider': company.ai_provider, 'model': company.ai_model,
                'folder': company.ai_folder_id, 'key_env': company.ai_key_env,
                'tone_of_voice': company.tone_of_voice, 'working_style': company.ai_working_style, 'knowledge': company.knowledge,
                'entries': entries, 'operator_assessment': decision, 'history': history,
                'available_materials': materials, 'reply_library': library, 'mode': mode, 'version': PIPELINE_VERSION}
    snapshot = {'source': source, 'provider': company.ai_provider, 'model': company.ai_model,
                'folder': company.ai_folder_id, 'key_env': company.ai_key_env,
                'tone_of_voice': style, 'facts': selected, 'history': history,
                'operator_assessment': decision, 'available_materials': materials,
                'reply_library': library, 'mode': mode, 'version': PIPELINE_VERSION}
    return digest(source), digest(settings), snapshot


def draft_is_current(thread, analysis):
    """A previous profile may leave classification usable, but not its reply."""
    if not analysis: return False
    if analysis.source_fingerprint and analysis.source_fingerprint != source_digest(thread): return False
    evidence = analysis.evidence or {}
    if not evidence.get('config'): return not analysis.source_fingerprint
    return evidence['config'] == prepare(thread)[1]
