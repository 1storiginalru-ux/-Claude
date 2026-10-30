"""Versioned editable drafts and isolated evaluation cases. Never publishes."""
from types import SimpleNamespace
from django.db import transaction
from django.utils import timezone
from desk.models import AIJob, Analysis, Audit, ReplyDraft, ReplyMaterial, Thread
from desk.ai.context import prepare, digest
from desk.ai.classification import local_classify
from desk.ai.provider import AIProblem
from desk.ai.client import validate_settings
from desk.ai.drafting import review_notes
from .policy import plan, public_plan, template_text


def case_sections(thread):
    from desk.ai.classification import review_sections
    from desk.integrations.content import content_view
    content=content_view(thread)
    return {**(review_sections(thread) or {}),'__materials':{'photos':len(content['photos']),'videos':len(content['videos']),
            'images_analyzed':False,'order_reference_available':any(v['label']=='ID заказа' for v in content['info'])}}


def case_subject(case):
    sections = case.review_sections or {}
    source = {'description': sections} if case.account.platform == 'ym' else ({'text':sections.get('comment'), 'pros':sections.get('advantages'), 'cons':sections.get('disadvantages')} if case.account.platform == 'wb' else {})
    return SimpleNamespace(pk=None, account=case.account, account_id=case.account_id, product=case.product,
        product_id=case.product_id, text=case.text, rating=case.rating, kind=case.kind,
        sku=case.product.code if case.product_id else '', source_data=source, customer=case.customer)


def prepare_case(case, mode, model=''):
    subject = case_subject(case)
    source, config, snapshot = prepare(subject, history_override=[], mode=mode,materials_override=(case.review_sections or {}).get('__materials'))
    if len(model)>100: raise AIProblem('ID модели длиннее 100 символов.')
    if model: snapshot['model'] = model
    config = digest({'config':config, 'model':snapshot['model'], 'case':case.pk})
    snapshot['test_mode'] = mode
    return source, config, snapshot


@transaction.atomic
def enqueue_test(case, actor, *, mode='guided', model='', force=False):
    from desk.models import ReplyTestCase
    case = ReplyTestCase.objects.select_for_update(of=('self',),no_key=True).select_related('account__company','product').get(pk=case.pk)
    if mode not in {'guided','baseline'} or not case.account.enabled or case.account.company.is_demo: raise AIProblem('Выберите подключённый реальный кабинет и режим испытания.')
    if case.product_id and case.product.company_id != case.account.company_id: raise AIProblem('Товар относится к другой компании.')
    source, config, snapshot = prepare_case(case,mode,model)
    routing = plan(case_subject(case),library=snapshot['reply_library'],available_materials=snapshot['available_materials'])
    if mode == 'baseline' or routing['action'] == 'ai': validate_settings(snapshot)
    AIJob.objects.filter(test_case=case,status__in=['queued','running','retry','blocked']).exclude(source_fingerprint=source).update(status='cancelled',lease_token=None,lease_until=None,error='Испытание изменилось.')
    job,created = AIJob.objects.get_or_create(test_case=case,source_fingerprint=source,config_fingerprint=config, defaults={'actor':actor,'snapshot':snapshot})
    if not created and (force and job.status=='done' or job.status in {'failed','cancelled','blocked'}):
        job.status='queued';job.next_attempt=timezone.now();job.error='';job.attempts=0;job.lease_token=None;job.lease_until=None;job.save()
    Audit.objects.create(company=case.account.company,actor=actor,action='reply.test.queued',object_id=str(case.pk),after={'job':job.pk,'mode':mode,'model':snapshot['model']})
    return job


@transaction.atomic
def store_result(job, data, notes, routing, *, method='ai'):
    notes = review_notes(data,[*routing['notes'],*notes])
    explanation = {**public_plan(routing), 'version':job.snapshot['version'], 'mode':job.snapshot.get('test_mode','guided'),
                   'model':job.snapshot['model'], 'facts':job.snapshot['facts']}
    draft = ReplyDraft.objects.create(thread_id=job.thread_id,test_case_id=job.test_case_id,job=job,
        text=data['draft'],original_text=data['draft'],notes=notes,explanation=explanation,
        method='ai_example' if method=='ai' and routing['examples'] else method,
        source_fingerprint=job.source_fingerprint,config_fingerprint=job.config_fingerprint)
    Audit.objects.create(company=job.company,actor=job.actor,action='reply.draft.created',object_id=str(draft.pk),after={'job':job.pk,'thread':job.thread_id,'method':draft.method,'scenario':explanation})
    return draft


@transaction.atomic
def prepare_reply(thread, actor, *, another=False):
    thread = Thread.objects.select_for_update(of=('self',),no_key=True).select_related('account__company','product','assessment').get(pk=thread.pk)
    if not thread.account.enabled or thread.account.company.is_demo: raise AIProblem('Кабинет выключен или демонстрационный.')
    source, config, snapshot = prepare(thread)
    routing = plan(thread,library=snapshot['reply_library'],available_materials=snapshot['available_materials'])
    if routing['action'] != 'ai':
        method = 'template' if routing['action']=='template' else 'manual'
        if not another:
            existing = ReplyDraft.objects.filter(thread=thread,source_fingerprint=source,config_fingerprint=config,method=method).order_by('-pk').first()
            if existing: return existing
        text = template_text(routing['template'],thread) if routing['action']=='template' else ''
        notes = [*routing['notes']]
        if not text: notes.append('Сценарий предусматривает ответ сотрудника. AI не вызывается.')
        notes = review_notes({'requires_human':True,'confidence':0,'draft':text},notes)
        draft=ReplyDraft.objects.create(thread=thread,text=text,original_text=text,method=method,notes=notes,
            explanation=public_plan(routing),source_fingerprint=source,config_fingerprint=config)
        Audit.objects.create(company=thread.account.company,actor=actor,action='reply.draft.created',object_id=str(draft.pk),after={'method':method,'scenario':draft.explanation})
        return draft
    from desk.ai.queue import enqueue
    return enqueue(thread,actor,force=another,retry_failed=True,draft_only=another)


def draft_current(draft):
    if draft.thread_id:
        thread=Thread.objects.select_related('account__company','product','assessment').get(pk=draft.thread_id)
        source,config,_=prepare(thread)
    else:
        from desk.models import ReplyTestCase
        case=ReplyTestCase.objects.select_related('account__company','product').get(pk=draft.test_case_id)
        source,config,_=prepare_case(case,draft.explanation.get('mode','guided'),draft.explanation.get('model',''))
    return source==draft.source_fingerprint and config==draft.config_fingerprint


@transaction.atomic
def save_draft(thread, actor, text, *, draft_id='', version='', feedback='unreviewed', comment=''):
    text = text.strip()
    if len(text)>5000 or feedback not in dict(ReplyDraft._meta.get_field('feedback').choices): raise ValueError('Проверьте длину текста и результат проверки.')
    thread=Thread.objects.select_for_update(of=('self',),no_key=True).select_related('account__company','product','assessment').get(pk=thread.pk)
    if thread.account.company.is_demo: raise ValueError('Демонстрационные обращения не сохраняются в рабочую очередь черновиков.')
    source,config,_=prepare(thread)
    if draft_id:
        draft=ReplyDraft.objects.select_for_update().filter(pk=draft_id,thread=thread).first()
        if not draft: raise ValueError('Черновик не найден в этом обращении.')
        if str(draft.version)!=str(version): raise ValueError('Черновик изменён другим сотрудником. Обновите страницу.')
        if draft.outgoing_id: raise ValueError('Этот вариант уже использован для отправки. Сохраните новый черновик.')
        if not draft_current(draft): raise ValueError('Обращение или настройки изменились. Подготовьте актуальный черновик.')
        draft.version+=1
    else:
        draft=ReplyDraft(thread=thread,method='manual',source_fingerprint=source,config_fingerprint=config)
    before={'text':draft.text,'feedback':draft.feedback}
    if draft.original_text and draft.original_text!=text and feedback=='accepted': feedback='edited'
    draft.text=text;draft.edited_by=actor;draft.feedback=feedback;draft.feedback_comment=comment[:2000];draft.save()
    Audit.objects.create(company=thread.account.company,actor=actor,action='reply.draft.saved',object_id=str(draft.pk),before=before,after={'text':text,'feedback':feedback,'version':draft.version})
    return draft


@transaction.atomic
def propose_example(thread, actor, text):
    if thread.account.company.is_demo: raise ValueError('Используйте реальное обращение для рабочей библиотеки.')
    if not text.strip(): raise ValueError('Сначала подготовьте текст ответа.')
    if len(text.strip())>3000 or len(thread.text)>4000: raise ValueError('Для примера нужен ответ до 3000 символов и обращение до 4000. Подготовьте сокращённый пример в библиотеке.')
    data=local_classify(thread.text,thread.rating,kind=thread.kind)
    material=ReplyMaterial.objects.create(company=thread.account.company,title=f'Из обращения №{thread.pk}',customer_text=thread.text[:4000],answer=text.strip()[:3000],source_thread=thread,thread_kind=thread.kind,platform=thread.account.platform,sentiment=data['sentiment'],explanation='Кандидат из рабочей переписки. Проверьте факты, условия и стиль перед подтверждением.')
    Audit.objects.create(company=thread.account.company,actor=actor,action='reply.example.proposed',object_id=str(material.pk),after={'thread':thread.pk,'approved':False})
    return material
