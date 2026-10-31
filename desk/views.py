import uuid
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.db.models import Count, Q, Sum, Max, Min
from django.db import transaction
from django.core.paginator import Paginator
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST
from django.http import JsonResponse
from .access import OPERATORS, QUALITY, companies_for, require_role
from .models import Account, Audit, Baseline, Company, Incident, Membership, Outgoing, Product, ProductMapping, SaleEvent, SalesDaily, SyncLog, Thread
from .voc.rules import SIGNALS, PRODUCT_SIGNALS, evaluate, human_only, product_family, grouped_signals, matches, signal_label, review_summary
from .integrations.base import IntegrationError
from .forms import NegativeRuleForm, ReviewAssessmentForm
from .reviews import assess_review, assessment_is_current, effective_analysis, fingerprint
from .voc.rules import trusted_analysis


def health(request):
    from django.db import connection
    with connection.cursor() as cursor: cursor.execute('SELECT 1')
    return JsonResponse({'status': 'ok'})


def scoped_threads(user):
    return Thread.objects.filter(account__company__in=companies_for(user)).select_related('account__company', 'product', 'analysis', 'assessment__reviewed_by', 'assigned_to')


def context(request, **kwargs):
    return {'companies': companies_for(request.user), 'has_demo': companies_for(request.user).filter(is_demo=True, account__thread__isnull=False).exists(), 'now': timezone.now(), **kwargs}


@login_required
def inbox(request):
    base = scoped_threads(request.user)
    threads = base
    from .replies.views import filter_dates
    threads = filter_dates(request,threads,'received_at')
    q = request.GET.get('q', '').strip()
    if q: threads = threads.filter(Q(text__icontains=q) | Q(product__name__icontains=q) | Q(customer__icontains=q) | Q(product__code__icontains=q))
    if request.GET.get('product_code'): threads = threads.filter(product__code=request.GET['product_code'])
    if request.GET.get('recent') == '7':
        from datetime import timedelta
        threads = threads.filter(received_at__gte=timezone.now() - timedelta(days=7), received_at__lte=timezone.now())
    filters = {'platform': 'account__platform', 'account': 'account__name', 'company': 'account__company__name', 'status': 'status', 'kind': 'kind', 'severity': 'analysis__severity', 'rating': 'rating', 'operator': 'assigned_to__username'}
    for key, field in filters.items():
        value = request.GET.get(key, '')
        if value and (key != 'rating' or value.isdigit()): threads = threads.filter(**{field: value})
    if request.GET.get('negative'):
        from .voc.rules import is_negative
        threads = threads.filter(pk__in=[t.pk for t in threads if is_negative(effective_analysis(t))])
    if request.GET.get('needs_review'):
        threads = threads.filter(pk__in=[t.pk for t in threads.filter(kind='review') if not trusted_analysis(effective_analysis(t))])
    if request.GET.get('unmapped'): threads = threads.filter(product__isnull=True)
    if request.GET.get('overdue'): threads = threads.filter(due_at__lt=timezone.now()).exclude(status__in=['Закрыто', 'Ответ отправлен', 'Ожидаем клиента'])
    signal = request.GET.get('signal')
    if signal in SIGNALS or signal == 'LONG_AUTH':
        ids = [t.pk for t in threads if (a := effective_analysis(t)) and matches(a.signals, signal)]
        threads = threads.filter(pk__in=ids)
    from django.core.paginator import Paginator
    page = Paginator(threads, 30).get_page(request.GET.get('page'))
    for thread in page:
        a = effective_analysis(thread)
        thread.display_signals = [signal_label(s) for s in grouped_signals(a.signals)] if a else []
        thread.assessment_current = assessment_is_current(thread)
        thread.classification_trusted = trusted_analysis(a)
    return render(request, 'desk/inbox.html', context(request, page=page, statuses=Thread.STATUSES, signals=[{'code': s, 'label': signal_label(s)} for s in grouped_signals(SIGNALS)], accounts=Account.objects.filter(company__in=companies_for(request.user)), total=base.count(), fresh=base.filter(status='Новое').count(), overdue=base.filter(due_at__lt=timezone.now()).exclude(status__in=['Закрыто', 'Ответ отправлен', 'Ожидаем клиента']).count(), failed=base.filter(status='Ошибка отправки').count()))


@login_required
def thread_detail(request, pk, assessment_form=None, form_status=200):
    from .integrations.content import content_view
    from .integrations.window import within_window
    thread = get_object_or_404(scoped_threads(request.user), pk=pk)
    analysis = getattr(thread, 'analysis', None)
    from .ai.context import source_digest, draft_is_current
    analysis_current = not analysis or not analysis.source_fingerprint or analysis.source_fingerprint == source_digest(thread)
    draft_current = draft_is_current(thread, analysis)
    reply_draft = (get_object_or_404(thread.reply_drafts,pk=request.GET['draft']) if request.GET.get('draft') else thread.reply_drafts.order_by('-pk').first())
    if reply_draft:
        from .replies.service import draft_current as current_reply
        draft_current = current_reply(reply_draft)
    composer_text = (reply_draft.text if reply_draft else analysis.draft if analysis else '') if draft_current else ''
    draft_notes = reply_draft.notes if reply_draft else (analysis.evidence.get('operator_notes',[]) if analysis else [])
    effective = effective_analysis(thread)
    metrics = [evaluate(thread.product, s, scope=companies_for(request.user)) for s in grouped_signals(effective.signals)] if thread.product_id and effective else []
    can_reply = request.user.is_superuser or Membership.objects.filter(user=request.user, company=thread.account.company, role__in=OPERATORS).exists()
    can_assess = request.user.is_superuser or Membership.objects.filter(user=request.user, company=thread.account.company, role__in=set(OPERATORS + QUALITY)).exists()
    assessment = getattr(thread, 'assessment', None)
    initial = {'decision': assessment.decision if assessment else 'pending', 'signals': assessment.signals if assessment else grouped_signals(analysis.signals) if analysis else [],
               'severity': assessment.severity if assessment else analysis.severity if analysis else 'P2', 'objective_issue': assessment.objective_issue if assessment else False,
               'comment': assessment.comment if assessment else '', 'source_fingerprint': fingerprint(thread), 'assessment_version': assessment.reviewed_at.isoformat() if assessment else ''}
    restricted = any(human_only({'signals': a.signals, 'severity': a.severity}) for a in [analysis, effective] if a)
    ai_job = thread.ai_jobs.order_by('-created_at', '-pk').first()
    return render(request, 'desk/thread.html', context(request, thread=thread, source_content=content_view(thread), within_sync_window=within_window(thread.received_at), analysis=analysis, metrics=metrics, request_key=str(uuid.uuid4()), can_reply=can_reply, can_assess=can_assess,
        assessment_form=assessment_form if assessment_form is not None else ReviewAssessmentForm(initial=initial), assessment=assessment, assessment_current=assessment_is_current(thread),
        ai_job=ai_job, analysis_current=analysis_current, draft_current=draft_current, composer_text=composer_text, reply_draft=reply_draft,
        draft_notes=draft_notes, draft_variants=thread.reply_drafts.order_by('-pk')[:10],
        draft_history=Audit.objects.filter(company=thread.account.company,action='reply.draft.saved',object_id=str(reply_draft.pk)).order_by('-created_at')[:10] if reply_draft else [],
        human_only=restricted, statuses=Thread.STATUSES, operators=Membership.objects.filter(company=thread.account.company, role__in=OPERATORS).select_related('user'), active_outgoing=thread.outgoing.filter(status__in=['Queued', 'Sending', 'Sent']).exists()), status=form_status)


@login_required
@require_POST
def review_assessment(request, pk):
    thread = get_object_or_404(scoped_threads(request.user), pk=pk, kind='review')
    require_role(request.user, thread.account.company_id, list(set(OPERATORS + QUALITY)))
    form = ReviewAssessmentForm(request.POST)
    status = 400
    if form.is_valid():
        try:
            assess_review(pk, request.user, form.cleaned_data)
            messages.success(request, 'Решение сохранено. Показатели учитывают разметку оператора; исходный AI-анализ сохранён.')
            return redirect('thread', pk=pk)
        except ValueError as error:
            form.add_error(None, str(error)); status = 409
    return thread_detail(request, pk, form, status)


@login_required
def thread_source(request, pk):
    thread = get_object_or_404(scoped_threads(request.user), pk=pk)
    payload = {'platform': thread.account.platform, 'external_id': thread.external_id,
               'fetched_at': thread.source_synced_at, 'source': thread.source_data,
               'history_fetched_at': thread.history_synced_at,
               'messages': list(thread.messages.values('external_id', 'direction', 'text', 'created_at', 'source_data'))}
    response = JsonResponse(payload, json_dumps_params={'ensure_ascii': False, 'indent': 2})
    response['Content-Disposition'] = f'attachment; filename="review-{thread.pk}.json"'
    response['Cache-Control'] = 'private, no-store'
    return response


@login_required
@require_POST
def thread_action(request, pk):
    thread = get_object_or_404(scoped_threads(request.user), pk=pk)
    require_role(request.user, thread.account.company_id, OPERATORS)
    action = request.POST.get('action')
    try:
        if action in {'prepare_reply','another_reply'}:
            from .replies.service import prepare_reply
            prepare_reply(thread,request.user,another=action=='another_reply')
            messages.success(request,'Подготовка запущена. Готовый шаблон появится сразу, AI-черновик — после обработки очереди.')
        elif action == 'save_draft':
            from .replies.service import save_draft
            save_draft(thread,request.user,request.POST.get('text',''),draft_id=request.POST.get('reply_draft',''),version=request.POST.get('draft_version',''),feedback=request.POST.get('feedback','unreviewed'),comment=request.POST.get('feedback_comment',''))
            messages.success(request,'Черновик сохранён. Ответ клиенту не отправлен.')
        elif action == 'propose_example':
            from .replies.service import propose_example
            propose_example(thread,request.user,request.POST.get('text',''))
            messages.success(request,'Пример передан на проверку руководителю. До подтверждения AI его не использует.')
        elif action == 'add_test':
            from .models import ReplyTestCase
            from .replies.service import case_sections
            require_role(request.user,thread.account.company_id,['supervisor','quality'])
            from desk.replies.greetings import marketplace_author
            case,_=ReplyTestCase.objects.get_or_create(source_thread=thread,defaults={'account':thread.account,'product':thread.product,'kind':thread.kind,'customer':marketplace_author(thread)[:120],'text':thread.text,'rating':thread.rating,'review_sections':case_sections(thread),'title':f'Обращение №{thread.pk}','created_by':request.user})
            messages.success(request,'Случай сохранён в испытаниях. AI ещё не вызывался.')
            return redirect('reply_tests')
        elif action == 'analyze':
            if thread.account.company.ai_provider == 'none':
                from .ai.provider import analyze
                analyze(thread, request.user)
                messages.success(request, 'Выполнена предварительная разметка по правилам. AI не подключён.')
            else:
                from .ai.queue import enqueue
                job = enqueue(thread, request.user, retry_failed=True)
                messages.success(request, 'Анализ этой версии уже готов.' if job.status == 'done' else 'AI-анализ поставлен в очередь. Результат появится после обработки.')
        elif action == 'send':
            from .messaging.service import enqueue
            if request.POST.get('feedback')=='rejected': raise ValueError('Черновик отмечен как неподходящий. Исправьте текст и результат проверки перед отправкой.')
            with transaction.atomic():
                item=enqueue(thread, request.user, request.POST.get('text', ''), request.POST.get('request_key'))
                if request.POST.get('reply_draft'):
                    from .replies.service import save_draft
                    from .models import ReplyDraft
                    linked=ReplyDraft.objects.filter(pk=request.POST['reply_draft'],thread=thread,outgoing=item).exists()
                    if not linked:
                        feedback=request.POST.get('feedback','unreviewed')
                        draft=save_draft(thread,request.user,request.POST.get('text',''),draft_id=request.POST['reply_draft'],version=request.POST.get('draft_version',''),feedback='accepted' if feedback=='unreviewed' else feedback,comment=request.POST.get('feedback_comment',''))
                        draft.outgoing=item;draft.save(update_fields=['outgoing','updated_at'])
                        item.ai_draft=draft.original_text;item.save(update_fields=['ai_draft'])
                        Audit.objects.create(company=thread.account.company,actor=request.user,action='outgoing.draft.selected',object_id=str(item.pk),after={'draft':draft.pk,'original_text':draft.original_text})
            messages.success(request, 'Ответ сохранён в очереди. Статус публикации появится после обработки worker.')
        elif action == 'retry':
            from .messaging.service import retry
            item = get_object_or_404(Outgoing, pk=request.POST.get('outgoing_id'), thread=thread)
            retry(item, request.user)
            messages.success(request, 'Ответ поставлен на повторную отправку.')
        elif action == 'reconcile':
            require_role(request.user, thread.account.company_id, ['supervisor'])
            from .messaging.service import reconcile
            item = get_object_or_404(Outgoing, pk=request.POST.get('outgoing_id'), thread=thread)
            reconcile(item, request.user, verified_absent=request.POST.get('verified_absent') == 'yes')
            messages.success(request, 'Сверка выполнена. Проверьте обновлённый статус ответа.')
        elif action == 'escalate':
            from .incidents.service import escalate
            effective = effective_analysis(thread)
            signal = grouped_signals(effective.signals)[0] if effective and effective.signals else 'OTHER'
            incident = escalate(thread, signal, request.user)
            messages.success(request, f'Обращение связано с расследованием #{incident.pk}.')
        elif action == 'status':
            status = request.POST.get('status')
            if status not in dict(Thread.STATUSES): raise ValueError('Неизвестный статус.')
            before = thread.status
            thread.status, thread.closed_at = status, timezone.now() if status == 'Закрыто' else None
            thread.save(update_fields=['status', 'closed_at'])
            Audit.objects.create(company=thread.account.company, actor=request.user, action='thread.status', object_id=str(thread.pk), before={'status': before}, after={'status': status})
        elif action == 'assign':
            user_id = request.POST.get('operator', '')
            member = get_object_or_404(Membership, pk=user_id, company=thread.account.company, role__in=OPERATORS)
            before = thread.assigned_to_id
            thread.assigned_to, thread.status = member.user, 'Назначено'
            thread.save(update_fields=['assigned_to', 'status'])
            Audit.objects.create(company=thread.account.company, actor=request.user, action='thread.assign', object_id=str(thread.pk), before={'user_id': before}, after={'user_id': member.user_id})
        else: raise ValueError('Неизвестное действие.')
    except (ValueError, IntegrationError) as e: messages.error(request, str(e))
    return redirect('thread', pk=pk)


@login_required
def products(request):
    items = Product.objects.filter(company__in=companies_for(request.user)).select_related('company').annotate(mapping_count=Count('mappings'))
    if request.GET.get('q'): items = items.filter(Q(code__icontains=request.GET['q']) | Q(name__icontains=request.GET['q']))
    page = Paginator(items.order_by('code', 'pk'), 48).get_page(request.GET.get('page'))
    return render(request, 'desk/products.html', context(request, products=page, product_page=page))


@login_required
def product_detail(request, pk):
    product = get_object_or_404(Product, pk=pk, company__in=companies_for(request.user))
    window = request.GET.get('window', '100')
    if window not in {'20', '50', '100', '200', '7d', '30d', '60d', '90d'}: window = '100'
    sales = []
    family = product_family(product, companies_for(request.user))
    product_accounts = Account.objects.filter(productmapping__product__in=family).distinct()
    selected_account = request.GET.get('account', '')
    account_id = int(selected_account) if selected_account.isdigit() and product_accounts.filter(pk=int(selected_account)).exists() else None
    from .voc.dataset import ControlDataset
    dataset = ControlDataset([product], companies_for(request.user))
    metrics = [evaluate(product, s, window, account_id=account_id, dataset=dataset) for s in ['NEGATIVE', *PRODUCT_SIGNALS]]
    summary = review_summary(product, window, account_id=account_id, dataset=dataset)
    mappings = ProductMapping.objects.filter(product__in=family).select_related('account')
    if account_id is not None: mappings = mappings.filter(account_id=account_id)
    event_filter = Q(pk__in=[])
    for mapping in mappings:
        if mapping.account.platform in {'wb','ym'} and mapping.account.sales_enabled:
            source = SaleEvent.objects.filter(account=mapping.account, sku=mapping.sku, occurred_at__lte=timezone.now())
            totals = {'redemptions': source.filter(kind='sale').count(), 'returns': source.filter(kind='return').count(), 'orders': None}
            source_label = 'API WB (оперативные данные)' if mapping.account.platform=='wb' else 'API Яндекс Маркета (заказы и статусы)'
        else:
            totals = SalesDaily.objects.filter(account=mapping.account, sku=mapping.sku).aggregate(redemptions=Sum('redemptions'), returns=Sum('returns'), orders=Sum('orders'))
            source_label = 'Импорт файла'
        event_filter |= Q(account=mapping.account, sku=mapping.sku)
        sales.append({'mapping': mapping, 'source_label': source_label, **totals})
    recent_sales = SaleEvent.objects.filter(event_filter, kind='sale', occurred_at__lte=summary['period']['until']).select_related('account').order_by('-occurred_at', '-pk')[:100]
    from .voc.uncertainty import overlaps
    account_names={a.pk:a.name for a in product_accounts}
    market_issues=[{**issue,'account_name':account_names.get(issue['account_id'],'')} for issue in dataset.uncertainties[product.code]
                   if (account_id is None or issue['account_id']==account_id) and overlaps(issue,summary['period']['start_at'],summary['period']['until'])]
    history = scoped_threads(request.user).filter(product__in=family)
    if account_id is not None: history = history.filter(account_id=account_id)
    baseline = dataset.baselines.get((product.code, 'NEGATIVE'))
    account_metrics = [{'name': account.name, **evaluate(product, 'NEGATIVE', window, dataset=dataset, account_id=account.pk)} for account in product_accounts]
    initial = {'warning': baseline.low, 'threshold': baseline.high, 'min_redemptions': baseline.min_redemptions, 'min_signals': baseline.min_signals, 'cluster_count': baseline.cluster_count, 'cluster_days': baseline.cluster_days} if baseline else {}
    return render(request, 'desk/product.html', context(request, product=product, metrics=metrics, sales=sales, window=window, threads=history[:30], summary=summary, product_accounts=product_accounts, selected_account=str(account_id or ''), recent_sales=recent_sales,market_issues=market_issues[:20],
        can_manage_rules=can_manage_rules(request.user, product), negative_rule=baseline, rule_form=NegativeRuleForm(initial=initial), account_metrics=account_metrics))


def can_manage_rules(user, product):
    if user.is_superuser: return True
    company_ids = set(product_family(product).values_list('company_id', flat=True))
    allowed = set(Membership.objects.filter(user=user, role__in=QUALITY).values_list('company_id', flat=True))
    return company_ids <= allowed


@login_required
@require_POST
@transaction.atomic
def product_rules(request, pk):
    product = get_object_or_404(Product, pk=pk, company__in=companies_for(request.user))
    if not can_manage_rules(request.user, product): raise PermissionDenied()
    form = NegativeRuleForm(request.POST)
    if not form.is_valid():
        messages.error(request, 'Правило не сохранено. ' + '; '.join(str(e) for errors in form.errors.values() for e in errors))
        return redirect('product', pk=pk)
    canonical = product_family(product).order_by('pk').first()
    data = form.cleaned_data
    before = Baseline.objects.filter(product=canonical, signal='NEGATIVE').values().first()
    baseline, _ = Baseline.objects.update_or_create(product=canonical, signal='NEGATIVE', defaults={
        'low': data['warning'], 'high': data['threshold'], 'min_redemptions': data['min_redemptions'],
        'min_signals': data['min_signals'], 'cluster_count': data['cluster_count'], 'cluster_days': data['cluster_days'], 'source': 'manual'})
    Audit.objects.create(company=canonical.company, actor=request.user, action='control.rule', object_id=str(baseline.pk), before=before or {}, after=data)
    messages.success(request, 'Правило сохранено. Фоновая проверка применит его без отправки ответов покупателям.')
    return redirect('product', pk=pk)


@login_required
def voc(request):
    from .voc.dataset import ControlDataset
    from .voc.queue import product_queue
    allowed = companies_for(request.user)
    incidents = list(visible_incidents(request.user).exclude(status='CLOSED').select_related('product', 'owner').prefetch_related('threads'))
    candidates = Product.objects.filter(company__in=allowed).filter(Q(thread__kind='review') | Q(code__in=[i.product.code for i in incidents]))
    if request.GET.get('q'):
        candidates = candidates.filter(Q(code__icontains=request.GET['q']) | Q(name__icontains=request.GET['q']))
    ids = candidates.values('code').annotate(product_id=Min('pk')).values_list('product_id', flat=True)
    products = list(Product.objects.filter(pk__in=ids))
    rows = product_queue(products, ControlDataset(products, allowed), incidents)
    totals = {'all': len(rows), 'attention': sum(r['attention'] for r in rows), 'incidents': sum(bool(r['incidents']) for r in rows),
              'insufficient': sum(r['incomplete'] for r in rows), 'pending': sum(r['pending'] > 0 for r in rows)}
    mode = request.GET.get('filter', 'all')
    if mode == 'attention': rows = [r for r in rows if r['attention']]
    elif mode == 'incidents': rows = [r for r in rows if r['incidents']]
    elif mode == 'insufficient': rows = [r for r in rows if r['incomplete']]
    elif mode == 'pending': rows = [r for r in rows if r['pending']]
    page = Paginator(rows, 20).get_page(request.GET.get('page'))
    return render(request, 'desk/voc.html', context(request, product_page=page, incidents=incidents, totals=totals, queue_filter=mode))


@login_required
def imports(request):
    if request.method == 'POST':
        company = get_object_or_404(companies_for(request.user), pk=request.POST.get('company'))
        require_role(request.user, company.pk, ['supervisor', 'quality'])
        from .products.imports import import_mapping, import_sales, ImportProblem
        try:
            if 'file' not in request.FILES: raise ImportProblem('Выберите CSV или XLSX.')
            mode = request.POST.get('mode')
            if mode not in {'mapping', 'sales'}: raise ImportProblem('Неизвестный тип импорта.')
            count = (import_mapping if mode == 'mapping' else import_sales)(company, request.FILES['file'], request.user)
            messages.success(request, f'Импорт завершён: {count} строк. Изменения сохранены в аудите.')
        except ImportProblem as e: messages.error(request, str(e))
        return redirect('imports')
    return render(request, 'desk/imports.html', context(request, unmapped=scoped_threads(request.user).filter(product__isnull=True)))


@login_required
def integrations(request):
    from .integrations.window import sync_window
    since, until = sync_window()
    recent = Q(thread__received_at__gte=since, thread__received_at__lte=until)
    accounts = Account.objects.filter(company__in=companies_for(request.user)).select_related('company', 'sync_state', 'sales_state').prefetch_related('history_states').annotate(total_threads=Count('thread'), sync_threads=Count('thread', filter=recent), full_threads=Count('thread', filter=recent & Q(thread__source_synced_at__isnull=False)), history_threads=Count('thread', filter=recent & Q(thread__history_synced_at__isnull=False))).order_by('company__name', 'name')
    import os
    from .integrations.base import get_adapter
    rows = [{'account': a, 'has_key': bool(os.environ.get(a.token_env)), 'sales_has_key': bool(os.environ.get(a.sales_token_env or a.token_env)), 'capabilities': get_adapter(a).capabilities, 'next_attempt': a.sync_state.next_attempt if hasattr(a, 'sync_state') else None, 'paused': hasattr(a, 'sync_state') and a.sync_state.next_attempt > timezone.now() and 'Пауза ' in a.sync_status} for a in accounts]
    return render(request, 'desk/integrations.html', context(request, rows=rows, logs=SyncLog.objects.filter(account__in=accounts).select_related('account').order_by('-created_at')[:20]))


@login_required
def tone(request):
    if request.method == 'POST':
        company = get_object_or_404(companies_for(request.user), pk=request.POST.get('company'))
        require_role(request.user, company.pk, ['supervisor'])
        from .ai.documents import normalize_newlines
        voice = normalize_newlines(request.POST.get('tone_of_voice', company.tone_of_voice))
        knowledge = normalize_newlines(request.POST.get('knowledge', company.knowledge))
        working = normalize_newlines(request.POST.get('ai_working_style', company.ai_working_style))
        if request.FILES.get('voice_file'):
            from .ai.documents import extract_voice
            try: voice = extract_voice(request.FILES['voice_file'])
            except ValueError as e:
                messages.error(request, str(e))
                return redirect('tone')
        if request.FILES.get('knowledge_file'):
            from .ai.documents import extract_voice
            try: knowledge = extract_voice(request.FILES['knowledge_file'], max_chars=24000)
            except ValueError as e:
                messages.error(request, str(e))
                return redirect('tone')
        if request.FILES.get('working_style_file'):
            from .ai.documents import extract_voice
            try: working = extract_voice(request.FILES['working_style_file'],max_chars=5000)
            except ValueError as error:
                messages.error(request,str(error));return redirect('tone')
        exceeded = [f'{name}: {len(text)} символов при лимите {limit}.'
                    for name,text,limit in [('Tone of Voice',voice,16000),('Знания',knowledge,24000),('Рабочие правила',working,5000)]
                    if len(text)>limit]
        if exceeded:
            messages.error(request, ' '.join(exceeded))
        else:
            with transaction.atomic():
                company = Company.objects.select_for_update(no_key=True).get(pk=company.pk)
                before = {'tone_of_voice': company.tone_of_voice, 'knowledge': company.knowledge, 'ai_working_style':company.ai_working_style}
                company.tone_of_voice, company.knowledge = voice, knowledge
                company.ai_working_style=working
                company.save(update_fields=['tone_of_voice', 'knowledge','ai_working_style'])
                Audit.objects.create(company=company, actor=request.user, action='tone.update', object_id=str(company.pk), before=before, after={'tone_of_voice': voice, 'knowledge': knowledge,'ai_working_style':working})
            messages.success(request, 'Профиль компании сохранён.')
        return redirect('tone')
    return render(request, 'desk/tone.html', context(request))


@login_required
def ai_queue(request):
    from .models import AIJob, AIRequest, AIBackfillState
    scope = companies_for(request.user)
    jobs = AIJob.objects.filter(Q(thread__account__company__in=scope)|Q(test_case__account__company__in=scope)).select_related('thread__account__company', 'thread__product','test_case__account__company').order_by('-updated_at', '-pk')
    if request.GET.get('status') in dict(AIJob.STATUSES): jobs = jobs.filter(status=request.GET['status'])
    page = Paginator(jobs, 40).get_page(request.GET.get('page'))
    spent = AIRequest.objects.filter(company__in=scope, month=timezone.localdate().replace(day=1)).values('company_id').annotate(tokens=Sum('budget_tokens'), input=Sum('input_tokens'), output=Sum('output_tokens'))
    totals = {row['company_id']: row for row in spent}
    settings = [{'company': c, 'usage': totals.get(c.pk, {})} for c in scope]
    backfill = AIBackfillState.objects.filter(account__company__in=scope).select_related('account')
    return render(request, 'desk/ai_queue.html', context(request, ai_jobs=page, ai_settings=settings, ai_statuses=AIJob.STATUSES, backfill_states=backfill))


@login_required
def incident_detail(request, pk):
    from django.contrib.auth import get_user_model
    from django.utils.dateparse import parse_datetime
    incident = get_object_or_404(visible_incidents(request.user).select_related('product'), pk=pk)
    case_companies = set(incident.threads.values_list('account__company_id', flat=True)) | {incident.product.company_id}
    allowed_companies = set(companies_for(request.user).values_list('pk', flat=True))
    owners = get_user_model().objects.filter(is_active=True).filter(Q(is_superuser=True) | Q(membership__company_id__in=case_companies & allowed_companies, membership__role__in=QUALITY)).distinct().order_by('username')
    can_edit = request.user.is_superuser or Membership.objects.filter(user=request.user, role__in=QUALITY, company_id__in=case_companies).exists()
    if request.method == 'POST':
        if not can_edit: raise PermissionDenied()
        try:
            if request.POST.get('action') == 'bitrix':
                from .incidents.service import sync_bitrix
                sync_bitrix(incident)
                Audit.objects.create(company=incident.product.company, actor=request.user, action='incident.bitrix', object_id=str(incident.pk), after={'bitrix_id': incident.bitrix_id})
                messages.success(request, 'Карточка передана в Bitrix24.')
            else:
                status = request.POST.get('status')
                if status not in {'INVESTIGATION', 'CAPA', 'CONTROL', 'CLOSED'}: raise ValueError('Неизвестный статус.')
                if request.POST.get('version') != incident.updated_at.isoformat(): raise ValueError('Расследование изменилось. Обновите страницу перед сохранением.')
                if status == 'CLOSED' and not request.POST.get('result', '').strip(): raise ValueError('Для закрытия укажите результат проверки.')
                owner_id = request.POST.get('owner', '')
                owner = get_object_or_404(owners, pk=owner_id) if owner_id.isdecimal() else None
                if owner_id and not owner_id.isdecimal(): raise ValueError('Выберите ответственного из списка.')
                due = parse_datetime(request.POST['due_at']) if request.POST.get('due_at') else None
                if request.POST.get('due_at') and due is None: raise ValueError('Некорректный срок проверки.')
                if due and timezone.is_naive(due): due = timezone.make_aware(due)
                fields = ['status', 'hypothesis', 'actions', 'result', 'owner_id', 'due_at']
                def values():
                    return {k: (getattr(incident, k).isoformat() if hasattr(getattr(incident, k), 'isoformat') else getattr(incident, k)) for k in fields}
                before = values()
                for field in ['hypothesis', 'actions', 'result']: setattr(incident, field, request.POST.get(field, '')[:12000])
                with transaction.atomic():
                    locked = Incident.objects.select_for_update().get(pk=incident.pk)
                    if locked.updated_at != incident.updated_at: raise ValueError('Расследование изменилось. Обновите страницу перед сохранением.')
                    incident.status, incident.owner, incident.due_at = status, owner, due
                    incident.save()
                    Audit.objects.create(company=incident.product.company, actor=request.user, action='incident.update', object_id=str(incident.pk), before=before, after=values())
                messages.success(request, 'Расследование обновлено.')
        except ValueError as e: messages.error(request, str(e))
        return redirect('incident', pk=pk)
    snapshot = incident.opening_snapshot if set(incident.opening_snapshot.get('company_ids', [])) <= allowed_companies else None
    return render(request, 'desk/incident.html', context(request, incident=incident, incident_threads=scoped_threads(request.user).filter(incident=incident), owners=owners, can_edit=can_edit,
        snapshot=snapshot, snapshot_start=parse_datetime(snapshot['start']) if snapshot and snapshot.get('start') else None,
        snapshot_until=parse_datetime(snapshot['until']) if snapshot and snapshot.get('until') else None,
        snapshot_hidden=bool(incident.opening_snapshot and snapshot is None), incident_statuses=Incident._meta.get_field('status').choices))


def visible_incidents(user):
    allowed = companies_for(user)
    return Incident.objects.filter(Q(product__company__in=allowed) | Q(threads__account__company__in=allowed)).distinct()
