from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import Max, Q, Count
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST
from django.utils.dateparse import parse_datetime
from datetime import date, datetime, time, timedelta
from django.utils import timezone
from desk.access import companies_for, require_role
from desk.models import Account, Audit, Company, KnowledgeEntry, Membership, ReplyDraft, ReplyMaterial, ReplyRule, ReplyTestCase, Thread
from desk.views import context
from .forms import MaterialForm, RuleForm, FactForm, TestCaseForm
from .service import draft_current, enqueue_test
from .diagnostics import saved_context


def manager(user, company):
    return user.is_superuser or Membership.objects.filter(user=user,company=company,role='supervisor').exists()


def selected_company(request):
    scope=companies_for(request.user).filter(is_demo=False)
    pk=request.POST.get('company') or request.GET.get('company')
    if pk: return get_object_or_404(scope,pk=pk)
    company=scope.order_by('pk').first()
    if not company: raise PermissionDenied('Нет доступных реальных компаний.')
    return company


def finish(request,name,company): return redirect(reverse(name)+'?company='+str(company.pk))


def audit(request,company,action,obj,before,after):
    Audit.objects.create(company=company,actor=request.user,action=action,object_id=str(obj.pk),before=before,after=after)


def filter_dates(request,rows,field):
    try:
        start=date.fromisoformat(request.GET['since']) if request.GET.get('since') else None
        end=date.fromisoformat(request.GET['until']) if request.GET.get('until') else None
        if start and end and start>end: raise ValueError
    except ValueError:
        messages.error(request,'Проверьте диапазон дат.');return rows.none()
    if start: rows=rows.filter(**{field+'__gte':timezone.make_aware(datetime.combine(start,time.min))})
    if end: rows=rows.filter(**{field+'__lt':timezone.make_aware(datetime.combine(end+timedelta(days=1),time.min))})
    return rows


@login_required
def materials(request,pk=None):
    company=selected_company(request)
    obj=get_object_or_404(ReplyMaterial,pk=pk,company=company) if pk else ReplyMaterial(company=company)
    form=MaterialForm(request.POST or None,instance=obj,company=company)
    if request.method=='POST':
        require_role(request.user,company.pk,['supervisor'])
        if form.is_valid():
            with transaction.atomic():
                old=ReplyMaterial.objects.select_for_update().filter(pk=obj.pk,company=company).first() if obj.pk else None
                if old and str(old.version)!=request.POST.get('version'):
                    form.add_error(None,'Материал изменён другим сотрудником. Обновите страницу.')
                else:
                    item=form.save(commit=False);item.company=company;item.version=(old.version+1 if old else 1)
                    item.approved_by=request.user if item.approved else None;item.save()
                    audit(request,company,'reply.material.saved',item,{'version':old.version,'answer':old.answer,'customer_text':old.customer_text,'approved':old.approved} if old else {},{'version':item.version,'approved':item.approved,'title':item.title,'answer':item.answer})
                    messages.success(request,'Материал сохранён. В генерации используются только активные подтверждённые материалы.')
                    return finish(request,'reply_materials',company)
    rows=ReplyMaterial.objects.filter(company=company).order_by('-updated_at')
    if request.GET.get('q'): rows=rows.filter(Q(title__icontains=request.GET['q'])|Q(answer__icontains=request.GET['q']))
    return render(request,'desk/replies/materials.html',context(request,selected_company=company,form=form,item=obj,items=Paginator(rows,20).get_page(request.GET.get('page')),can_manage=manager(request.user,company)))


@login_required
def rules(request,pk=None):
    company=selected_company(request)
    if request.method == 'POST' and request.POST.get('action') == 'sensitive_policy':
        require_role(request.user,company.pk,['supervisor'])
        with transaction.atomic():
            company=Company.objects.select_for_update(no_key=True).get(pk=company.pk)
            before=company.reply_sensitive_templates
            company.reply_sensitive_templates=request.POST.get('enabled')=='yes'
            company.save(update_fields=['reply_sensitive_templates'])
            audit(request,company,'reply.policy.saved',company,{'strict_templates':before},{'strict_templates':company.reply_sensitive_templates})
        messages.success(request,'Режим обработки негатива сохранён.')
        return finish(request,'reply_rules',company)
    obj=get_object_or_404(ReplyRule,pk=pk,company=company) if pk else ReplyRule(company=company)
    form=RuleForm(request.POST or None,instance=obj,company=company)
    if request.method=='POST':
        require_role(request.user,company.pk,['supervisor'])
        if form.is_valid():
            with transaction.atomic():
                old=ReplyRule.objects.select_for_update().filter(pk=obj.pk).first() if obj.pk else None
                if old and str(old.version)!=request.POST.get('version'): form.add_error(None,'Сценарий изменён. Обновите страницу.')
                else:
                    rule=form.save(commit=False);rule.company=company;rule.conditions=form.conditions();rule.version=(old.version+1 if old else 1);rule.save();form.save_m2m()
                    audit(request,company,'reply.rule.saved',rule,{'conditions':old.conditions,'action':old.action,'version':old.version} if old else {}, {'name':rule.name,'conditions':rule.conditions,'action':rule.action,'version':rule.version})
                    messages.success(request,'Сценарий сохранён. Автоматическая отправка не включается.');return finish(request,'reply_rules',company)
    return render(request,'desk/replies/rules.html',context(request,selected_company=company,form=form,item=obj,items=ReplyRule.objects.filter(company=company).order_by('priority','pk'),can_manage=manager(request.user,company)))


@login_required
def knowledge(request,pk=None):
    company=selected_company(request)
    obj=get_object_or_404(KnowledgeEntry,pk=pk,company=company) if pk else KnowledgeEntry(company=company,product_code=request.GET.get('product',''))
    form=FactForm(request.POST or None,instance=obj,company=company)
    can_edit=request.user.is_superuser or Membership.objects.filter(user=request.user,company=company,role__in=['supervisor','quality']).exists()
    if request.method=='POST':
        require_role(request.user,company.pk,['supervisor','quality'])
        if form.is_valid():
            with transaction.atomic():
                old=KnowledgeEntry.objects.select_for_update().filter(pk=obj.pk).first() if obj.pk else None
                if old and old.updated_at!=parse_datetime(request.POST.get('version','')): form.add_error(None,'Факт изменён. Обновите страницу.')
                else:
                    fact=form.save(commit=False);fact.company=company
                    if not manager(request.user,company): fact.approved=False
                    fact.save();audit(request,company,'reply.knowledge.saved',fact,{'content':old.content,'source':old.source,'approved':old.approved} if old else {}, {'product':fact.product_code,'content':fact.content,'source':fact.source,'approved':fact.approved})
                    messages.success(request,'Факт сохранён. Неподтверждённые знания не передаются AI.');return finish(request,'reply_knowledge',company)
    rows=KnowledgeEntry.objects.filter(company=company).order_by('product_code','title')
    if request.GET.get('product'): rows=rows.filter(product_code=request.GET['product'])
    if request.GET.get('q'): rows=rows.filter(Q(product_code__icontains=request.GET['q'])|Q(title__icontains=request.GET['q'])|Q(content__icontains=request.GET['q']))
    return render(request,'desk/replies/knowledge.html',context(request,selected_company=company,form=form,item=obj,items=Paginator(rows,25).get_page(request.GET.get('page')),can_manage=can_edit))


@login_required
def drafts(request):
    scope=companies_for(request.user)
    latest=ReplyDraft.objects.filter(thread__account__company__in=scope).values('thread_id').annotate(last=Max('pk')).values('last')
    rows=ReplyDraft.objects.filter(pk__in=latest).select_related('thread__account__company','thread__product','edited_by','outgoing').order_by('-created_at')
    for arg,field in [('company','thread__account__company_id'),('account','thread__account__name'),('feedback','feedback')]:
        if request.GET.get(arg): rows=rows.filter(**{field:request.GET[arg]})
    if request.GET.get('q'): rows=rows.filter(Q(thread__text__icontains=request.GET['q'])|Q(thread__product__code__icontains=request.GET['q'])|Q(thread__product__name__icontains=request.GET['q']))
    rows=filter_dates(request,rows,'thread__received_at')
    state=request.GET.get('state')
    if state=='ready': rows=rows.exclude(text='').exclude(text__contains='{{').filter(outgoing__isnull=True)
    if state=='needs_info': rows=rows.filter(Q(text='')|Q(text__contains='{{'),outgoing__isnull=True)
    if state=='sent': rows=rows.filter(outgoing__isnull=False)
    page=Paginator(rows,25).get_page(request.GET.get('page'))
    for item in page: item.current=draft_current(item)
    totals={v['feedback']:v['count'] for v in ReplyDraft.objects.filter(pk__in=latest).exclude(method='manual').values('feedback').annotate(count=Count('pk'))}
    return render(request,'desk/replies/drafts.html',context(request,items=page,feedbacks=ReplyDraft._meta.get_field('feedback').choices,totals=totals))


@login_required
def tests(request):
    scope=companies_for(request.user).filter(is_demo=False)
    case=get_object_or_404(ReplyTestCase,pk=request.GET['case'],account__company__in=scope) if request.GET.get('case') else None
    form=TestCaseForm(request.POST or None,instance=case,scope=scope)
    if request.method=='POST' and form.is_valid():
        require_role(request.user,form.cleaned_data['account'].company_id,['supervisor','quality'])
        item=form.save(commit=False);item.created_by=request.user
        if case and 'text' in form.changed_data: item.review_sections={}
        item.save();audit(request,item.account.company,'reply.test.saved',item,{}, {'title':item.title})
        return redirect('reply_tests')
    cases=Paginator(ReplyTestCase.objects.filter(account__company__in=scope).select_related('account__company','product').order_by('-pk'),10).get_page(request.GET.get('page'))
    for item in cases:
        item.results=list(item.reply_drafts.select_related('job').order_by('-pk')[:6])
        for result in item.results:
            result.current=draft_current(result)
            result.trace=saved_context(result)
        item.jobs=item.ai_jobs.order_by('-pk')[:4]
    return render(request,'desk/replies/tests.html',context(request,form=form,items=cases))


@login_required
@require_POST
def test_run(request,pk):
    case=get_object_or_404(ReplyTestCase.objects.select_related('account__company','product'),pk=pk,account__company__in=companies_for(request.user))
    require_role(request.user,case.account.company_id,['supervisor','quality'])
    from desk.ai.provider import AIProblem
    try:
        mode=request.POST.get('mode','guided'); model=request.POST.get('model','').strip()
        with transaction.atomic():
            for variant in (['baseline','guided'] if mode=='both' else [mode]): enqueue_test(case,request.user,mode=variant,model=model,force=request.POST.get('repeat')=='yes')
        messages.success(request,'Испытание поставлено в очередь. Шаблон или ответ сотрудника обходятся без AI-запроса; генерация оплачивается по тарифу модели. Рабочие отзывы и отправки не меняются.')
    except AIProblem as error: messages.error(request,str(error))
    return redirect('reply_tests')


@login_required
@require_POST
def test_feedback(request,pk):
    item=get_object_or_404(ReplyDraft,pk=pk,test_case__account__company__in=companies_for(request.user))
    require_role(request.user,item.test_case.account.company_id,['supervisor','quality'])
    if not draft_current(item):
        messages.error(request,'Испытание или настройки изменились. Оцените результат новой версии.');return redirect('reply_tests')
    feedback=request.POST.get('feedback')
    if feedback in {'accepted','rejected'}:
        item.feedback=feedback;item.feedback_comment=request.POST.get('comment','')[:2000];item.edited_by=request.user;item.version+=1;item.save()
        audit(request,item.test_case.account.company,'reply.test.feedback',item,{}, {'feedback':feedback,'comment':item.feedback_comment})
    return redirect('reply_tests')
