"""Company-scoped cabinet thresholds. Viewing settings never opens cases."""
from django import forms
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.shortcuts import get_object_or_404, redirect, render
from django.http import Http404
from django.utils import timezone
from desk.access import companies_for
from desk.forms import TopicRuleForm
from desk.models import Account, ControlDefaults, ControlTopicRule, Membership
from desk.voc.profile import save_rule
from desk.voc.rules import signal_label
from desk.voc.topics import CONTROL_SIGNALS, starter_profile


@login_required
def control_rules(request):
    accounts = Account.objects.filter(company__in=companies_for(request.user), company__is_demo=False,
        platform__in=['wb','ym']).select_related('company').order_by('name')
    selected = request.POST.get('account') if request.method == 'POST' else request.GET.get('account')
    if selected and not selected.isdigit(): raise Http404()
    account = (get_object_or_404(accounts,pk=int(selected)) if selected else
               accounts.filter(control_defaults__isnull=False).first() or accounts.first())
    can_edit = bool(account and (request.user.is_superuser or Membership.objects.filter(user=request.user,
        company=account.company,role__in=['supervisor','quality']).exists()))
    if request.method == 'POST' and not can_edit: raise PermissionDenied()
    profile = starter_profile(); signals = ['NEGATIVE',*CONTROL_SIGNALS]
    signal = request.POST.get('signal') if request.method == 'POST' else None
    if request.method == 'POST' and signal not in signals: raise Http404()
    bound = None
    if request.method == 'POST':
        bound = TopicRuleForm(request.POST)
        if bound.is_valid():
            try:
                save_rule(account,signal,bound.cleaned_data,request.user,expected_version=request.POST.get('version',''))
                messages.success(request,'Порог кабинета сохранён. Фоновая проверка применит его к текущим и новым товарам без индивидуального исключения.')
                return redirect('/voc/rules/?account='+str(account.pk))
            except (ValueError,ValidationError) as error: bound.add_error(None,str(error))
    rows=[]
    if account:
        overall=ControlDefaults.objects.filter(account=account).first()
        topics={row.signal:row for row in ControlTopicRule.objects.filter(account=account)}
        for topic in signals:
            row=overall if topic=='NEGATIVE' else topics.get(topic)
            values={key:getattr(row,key,True) for key in profile[topic]} if row else profile[topic]
            form=bound if topic==signal else TopicRuleForm(initial=values)
            form.fields['min_redemptions'].widget=forms.HiddenInput()
            if topic=='NEGATIVE':form.fields['cluster_enabled'].widget=forms.HiddenInput()
            rows.append({'signal':topic,'label':signal_label(topic),'form':form,'values':values,
                         'configured':row is not None,'version':row.updated_at.isoformat() if row else ''})
    return render(request,'desk/control_rules.html',{'companies':companies_for(request.user),'now':timezone.now(),
        'accounts':accounts,'selected_account':account,'can_edit':can_edit,'rows':rows})
