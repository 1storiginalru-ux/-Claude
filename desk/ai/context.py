"""Local, company-scoped retrieval. No provider-hosted memory or vector service."""
import hashlib
import json
import re
from django.db.models import Q
from desk.models import KnowledgeEntry
from .classification import review_sections

PIPELINE_VERSION = 'voice-ai-2.1'


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


def prepare(thread):
    company = thread.account.company
    source = source_context(thread)
    code = thread.product.code if thread.product_id else ''
    entries = list(KnowledgeEntry.objects.filter(company=company, approved=True, active=True)
                   .filter(Q(product_code='') | Q(product_code=code)).order_by('pk')
                   .values('id', 'title', 'content', 'source', 'product_code', 'updated_at'))
    query = words(source['text'] + ' ' + (thread.product.name if thread.product_id else ''))
    candidates = []
    for entry in entries:
        if not entry['source'].strip() or len(entry['content']) > 3000 or not platform_matches(entry['content'], thread.account.platform): continue
        score = len(query & words(entry['title'] + ' ' + entry['content']))
        if score and platform_tag(entry['content']) == thread.account.platform: score += 2
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
        score = len(query & words(paragraph))
        if score and platform_tag(paragraph) == thread.account.platform: score += 2
        candidates.append((score, f'profile:{i}:{digest(paragraph)[:12]}', paragraph, 'Подтверждённые знания компании'))
    selected, size = [], 0
    for score, identifier, text, origin in sorted(candidates, key=lambda row: (-row[0], row[1])):
        if len(selected) >= 4: break
        if size + len(text) > 6000: continue
        selected.append({'id': identifier, 'text': text, 'source': origin})
        size += len(text)
    settings = {'provider': company.ai_provider, 'model': company.ai_model,
                'folder': company.ai_folder_id, 'key_env': company.ai_key_env,
                'tone_of_voice': company.tone_of_voice, 'knowledge': company.knowledge,
                'entries': entries, 'version': PIPELINE_VERSION}
    history = list(thread.messages.order_by('-created_at', '-pk').values('direction', 'text')[:6])[::-1]
    # Classification never sees this history or the brand's style instructions.
    history = [{'direction': m['direction'], 'text': m['text'][:2000]} for m in history]
    snapshot = {'source': source, 'provider': company.ai_provider, 'model': company.ai_model,
                'folder': company.ai_folder_id, 'key_env': company.ai_key_env,
                'tone_of_voice': company.tone_of_voice, 'facts': selected, 'history': history,
                'version': PIPELINE_VERSION}
    return digest(source), digest(settings), snapshot
