import uuid
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.db.models import Count, Q, Sum
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST
from django.http import JsonResponse
from .access import OPERATORS, QUALITY, companies_for, require_role
from .models import Account, Audit, Company, Incident, Membership, Outgoing, Product, ProductMapping, SalesDaily, SyncLog, Thread
from .voc.rules import SIGNALS, evaluate, human_only, product_family
from .integrations.base import IntegrationError


def health(request):
    from django.db import connection
    with connection.cursor() as cursor: cursor.execute('SELECT 1')
    return JsonResponse({'status': 'ok'})


def scoped_threads(user):
    return Thread.objects.filter(account__company__in=companies_for(user)).select_related('account__company', 'product', 'analysis', 'assigned_to')


def context(request, **kwargs):
    return {'companies': companies_for(request.user), 'has_demo': companies_for(request.user).filter(is_demo=True).exists(), 'now': timezone.now(), **kwargs}


@login_required
def inbox(request):
    base = scoped_threads(request.user)
    threads = base
    q = request.GET.get('q', '').strip()
    if q: threads = threads.filter(Q(text__icontains=q) | Q(product__name__icontains=q) | Q(customer__icontains=q) | Q(product__code__icontains=q))
    filters = {'platform': 'account__platform', 'account': 'account__name', 'company': 'account__company__name', 'status': 'status', 'kind': 'kind', 'severity': 'analysis__severity', 'rating': 'rating', 'operator': 'assigned_to__username'}
    for key, field in filters.items():
        value = request.GET.get(key, '')
        if value and (key != 'rating' or value.isdigit()): threads = threads.filter(**{field: value})
    if request.GET.get('negative'): threads = threads.filter(Q(analysis__sentiment='negative') | Q(analysis__hidden_negative=True))
    if request.GET.get('unmapped'): threads = threads.filter(product__isnull=True)
    if request.GET.get('overdue'): threads = threads.filter(due_at__lt=timezone.now()).exclude(status__in=['Закрыто', 'Ответ отправлен', 'Ожидаем клиента'])
    signal = request.GET.get('signal')
    if signal in SIGNALS:
        ids = [t.pk for t in threads if hasattr(t, 'analysis') and signal in t.analysis.signals]
        threads = threads.filter(pk__in=ids)
    from django.core.paginator import Paginator
    page = Paginator(threads, 30).get_page(request.GET.get('page'))
    return render(request, 'desk/inbox.html', context(request, page=page, statuses=Thread.STATUSES, signals=sorted(SIGNALS), accounts=Account.objects.filter(company__in=companies_for(request.user)), total=base.count(), fresh=base.filter(status='Новое').count(), overdue=base.filter(due_at__lt=timezone.now()).exclude(status__in=['Закрыто', 'Ответ отправлен', 'Ожидаем клиента']).count(), failed=base.filter(status='Ошибка отправки').count()))


@login_required
def thread_detail(request, pk):
    thread = get_object_or_404(scoped_threads(request.user), pk=pk)
    analysis = getattr(thread, 'analysis', None)
    metrics = [evaluate(thread.product, s, scope=companies_for(request.user)) for s in analysis.signals] if thread.product_id and analysis else []
    can_reply = request.user.is_superuser or Membership.objects.filter(user=request.user, company=thread.account.company, role__in=OPERATORS).exists()
    return render(request, 'desk/thread.html', context(request, thread=thread, analysis=analysis, metrics=metrics, request_key=str(uuid.uuid4()), can_reply=can_reply, human_only=human_only({'signals': analysis.signals, 'severity': analysis.severity}) if analysis else False, statuses=Thread.STATUSES, operators=Membership.objects.filter(company=thread.account.company, role__in=OPERATORS).select_related('user'), active_outgoing=thread.outgoing.filter(status__in=['Queued', 'Sending', 'Sent']).exists()))


@login_required
@require_POST
def thread_action(request, pk):
    thread = get_object_or_404(scoped_threads(request.user), pk=pk)
    require_role(request.user, thread.account.company_id, OPERATORS)
    action = request.POST.get('action')
    try:
        if action == 'analyze':
            from .ai.provider import analyze
            result = analyze(thread, request.user)
            messages.success(request, 'AI-анализ обновлён.' if result.provider != 'rules' else 'Выполнена предварительная разметка по правилам. AI не подключён.')
        elif action == 'send':
            from .messaging.service import enqueue
            enqueue(thread, request.user, request.POST.get('text', ''), request.POST.get('request_key'))
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
            signal = thread.analysis.signals[0] if hasattr(thread, 'analysis') and thread.analysis.signals else 'OTHER'
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
    return render(request, 'desk/products.html', context(request, products=items))


@login_required
def product_detail(request, pk):
    product = get_object_or_404(Product, pk=pk, company__in=companies_for(request.user))
    window = request.GET.get('window', '100')
    if window not in {'20', '50', '100', '200', '30d', '60d', '90d'}: window = '100'
    metrics = [evaluate(product, s, window, scope=companies_for(request.user)) for s in ['AUTH', 'LONG', 'SCENT', 'PACK', 'DEFECT', 'MARK', 'RETURN']]
    sales = []
    family = product_family(product, companies_for(request.user))
    for mapping in ProductMapping.objects.filter(product__in=family).select_related('account'):
        totals = SalesDaily.objects.filter(account=mapping.account, sku=mapping.sku).aggregate(redemptions=Sum('redemptions'), returns=Sum('returns'), orders=Sum('orders'))
        sales.append({'mapping': mapping, **totals})
    return render(request, 'desk/product.html', context(request, product=product, metrics=metrics, sales=sales, window=window, threads=scoped_threads(request.user).filter(product__in=family)[:30]))


@login_required
def voc(request):
    rows = []
    seen = set()
    for product in Product.objects.filter(company__in=companies_for(request.user)):
        if product.code in seen: continue
        seen.add(product.code)
        for signal in ['AUTH', 'LONG', 'DEFECT', 'PACK', 'MARK', 'RETURN']:
            metric = evaluate(product, signal, scope=companies_for(request.user))
            if metric['count'] or metric['status'] == 'ANOMALY': rows.append({'product': product, **metric})
    rows.sort(key=lambda x: ({'ANOMALY': 0, 'WATCH': 1, 'NORMAL': 2}[x['status']], -x['count']))
    return render(request, 'desk/voc.html', context(request, metrics=rows, incidents=visible_incidents(request.user).exclude(status='CLOSED').select_related('product')))


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
    accounts = Account.objects.filter(company__in=companies_for(request.user)).select_related('company')
    import os
    from .integrations.base import get_adapter
    rows = [{'account': a, 'has_key': bool(os.environ.get(a.token_env)), 'capabilities': get_adapter(a).capabilities} for a in accounts]
    return render(request, 'desk/integrations.html', context(request, rows=rows, logs=SyncLog.objects.filter(account__in=accounts).select_related('account').order_by('-created_at')[:20]))


@login_required
def tone(request):
    if request.method == 'POST':
        company = get_object_or_404(companies_for(request.user), pk=request.POST.get('company'))
        require_role(request.user, company.pk, ['supervisor'])
        before = {'tone_of_voice': company.tone_of_voice, 'knowledge': company.knowledge}
        voice, knowledge = request.POST.get('tone_of_voice', ''), request.POST.get('knowledge', '')
        if request.FILES.get('voice_file'):
            from .ai.documents import extract_voice
            try: voice = extract_voice(request.FILES['voice_file'])
            except ValueError as e:
                messages.error(request, str(e))
                return redirect('tone')
        if len(voice) > 16000 or len(knowledge) > 24000:
            messages.error(request, 'Лимиты: Tone of Voice — 16000, знания — 24000 символов.')
        else:
            company.tone_of_voice, company.knowledge = voice, knowledge
            company.save(update_fields=['tone_of_voice', 'knowledge'])
            Audit.objects.create(company=company, actor=request.user, action='tone.update', object_id=str(company.pk), before=before, after={'tone_of_voice': voice, 'knowledge': knowledge})
            messages.success(request, 'Профиль компании сохранён.')
        return redirect('tone')
    return render(request, 'desk/tone.html', context(request))


@login_required
def incident_detail(request, pk):
    incident = get_object_or_404(visible_incidents(request.user).select_related('product'), pk=pk)
    if request.method == 'POST':
        if not request.user.is_superuser:
            allowed = Membership.objects.filter(user=request.user, role__in=QUALITY).values_list('company_id', flat=True)
            if not incident.threads.filter(account__company_id__in=allowed).exists(): raise PermissionDenied()
        try:
            if request.POST.get('action') == 'bitrix':
                from .incidents.service import sync_bitrix
                sync_bitrix(incident)
                Audit.objects.create(company=incident.product.company, actor=request.user, action='incident.bitrix', object_id=str(incident.pk), after={'bitrix_id': incident.bitrix_id})
                messages.success(request, 'Карточка передана в Bitrix24.')
            else:
                status = request.POST.get('status')
                if status not in {'INVESTIGATION', 'CAPA', 'CONTROL', 'CLOSED'}: raise ValueError('Неизвестный статус.')
                before = {k: getattr(incident, k) for k in ['status', 'hypothesis', 'actions', 'result']}
                for field in ['hypothesis', 'actions', 'result']: setattr(incident, field, request.POST.get(field, '')[:12000])
                incident.status = status
                incident.save()
                Audit.objects.create(company=incident.product.company, actor=request.user, action='incident.update', object_id=str(incident.pk), before=before, after={k: getattr(incident, k) for k in before})
                messages.success(request, 'Расследование обновлено.')
        except ValueError as e: messages.error(request, str(e))
        return redirect('incident', pk=pk)
    return render(request, 'desk/incident.html', context(request, incident=incident, incident_threads=scoped_threads(request.user).filter(incident=incident)))


def visible_incidents(user):
    allowed = companies_for(user)
    return Incident.objects.filter(Q(product__company__in=allowed) | Q(threads__account__company__in=allowed)).distinct()
