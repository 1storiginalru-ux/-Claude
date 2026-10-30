"""Apply a reviewed, scoped rollout derived from an exact live source snapshot."""
import hashlib
import json
import os
from pathlib import Path
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.db.models import F
from django.utils import timezone
from desk.models import Account, Audit, Company, KnowledgeEntry, ReplyMaterial, ReplyRule
from desk.ai.client import validate_settings
from desk.ai.provider import AIProblem

PROFILE_FIELDS=['tone_of_voice','knowledge','ai_working_style']
AI_FIELDS=['ai_provider','ai_model','ai_folder_id','ai_key_env']
GUARD_FIELDS=['name','legal_entity','brand',*PROFILE_FIELDS,*AI_FIELDS,'ai_token_limit','reply_sensitive_templates']
MATERIAL_FIELDS=['kind','title','customer_text','answer','explanation','topic','covered_topics','sentiment','thread_kind','platform','product_code','brand','excluded_words','active']


def digest(value):
    return hashlib.sha256(json.dumps(value,ensure_ascii=False,sort_keys=True,default=str).encode()).hexdigest()


class Command(BaseCommand):
    help='Применить проверенный перенос подтверждённой библиотеки в явно выбранные кабинеты. Без --apply только проверка; с --apply включает новые черновики.'
    def add_arguments(self,p):
        p.add_argument('--file',type=Path,required=True);p.add_argument('--apply',action='store_true')

    @transaction.atomic
    def handle(self,*args,**o):
        try:
            with o['file'].open('rb') as stream: raw=stream.read(4*1024*1024+1)
            if len(raw)>4*1024*1024: raise ValueError
            payload=json.loads(raw)
            if payload['format']!='voice-reply-rollout-1' or not isinstance(payload['targets'],list) or not 1<=len(payload['targets'])<=30: raise ValueError
            names=[t['account_name'] for t in payload['targets']]
            if len(set(names))!=len(names): raise ValueError
        except (OSError,KeyError,ValueError,TypeError) as error: raise CommandError('Нужен корректный JSON voice-reply-rollout-1, до 4 МБ и 30 кабинетов.') from error
        source=Account.objects.select_related('company').filter(pk=payload['source_account_id'],name=payload['source_account_name'],company__is_demo=False).first()
        if not source: raise CommandError('Исходный кабинет не найден.')
        accounts=list(Account.objects.select_for_update(of=('self',),no_key=True).select_related('company').filter(name__in=names,company__is_demo=False))
        if {a.name for a in accounts}!=set(names): raise CommandError('Не все целевые кабинеты найдены.')
        companies={c.pk:c for c in Company.objects.select_for_update(no_key=True).filter(pk__in={a.company_id for a in accounts}|{source.company_id})}
        source.company=companies[source.company_id]
        guard=payload['source_guard']
        profiles=Company.objects.filter(pk=source.company_id).values(*GUARD_FIELDS).get()
        approved=list(ReplyMaterial.objects.filter(company=source.company,approved=True).order_by('pk').values())
        generated_names=set(payload.get('generated_source_rule_names',[]))
        current_rules=[{**row,'accounts':sorted(ReplyRule.objects.get(pk=row['id']).accounts.values_list('pk',flat=True))}
            for row in ReplyRule.objects.filter(company=source.company).exclude(name__in=generated_names).order_by('pk').values()]
        if digest(profiles)!=guard['profiles'] or digest(approved)!=guard['materials'] or digest(current_rules)!=guard['rules']:
            raise CommandError('Исходный ЯМ_1 изменился после экспорта. Нужен новый экспорт; перенос отменён.')
        facts=list(KnowledgeEntry.objects.filter(company=source.company,approved=True).order_by('pk').values())
        if digest(facts)!=guard['knowledge']: raise CommandError('Подтверждённые факты изменились после экспорта; перенос отменён.')
        by_name={a.name:a for a in accounts};desired={};ops=[]
        for target in payload['targets']:
            account=by_name[target['account_name']]
            if account.pk!=target['account_id'] or account.company_id!=target['company_id'] or account.platform!=target['platform'] or not account.enabled:
                raise CommandError(f'{account.name}: привязка или состояние кабинета изменились.')
            company_fields={**target['profiles'],**target['ai'],'reply_sensitive_templates':target['strict_templates']}
            if set(target['profiles'])!=set(PROFILE_FIELDS) or set(target['ai'])!=set(AI_FIELDS): raise CommandError('Недопустимые поля профиля/AI.')
            if any(not isinstance(target['profiles'][key],str) or len(target['profiles'][key])>limit for key,limit in [('tone_of_voice',16000),('knowledge',24000),('ai_working_style',5000)]): raise CommandError('Профиль превышает лимит текстового поля.')
            if not isinstance(target['signature'],str) or len(target['signature'])>500:raise CommandError('Подпись должна содержать до 500 символов.')
            if account.company_id in desired and desired[account.company_id]!=company_fields: raise CommandError('Профили общей компании противоречат друг другу.')
            desired[account.company_id]=company_fields
            try:validate_settings({'provider':target['ai']['ai_provider'],'model':target['ai']['ai_model'],'folder':target['ai']['ai_folder_id'],'key_env':target['ai']['ai_key_env']})
            except AIProblem as error:raise CommandError(str(error)) from error
            for material in target['materials']:
                if set(material['fields'])!=set(MATERIAL_FIELDS) or material['fields']['platform'] not in ({'',account.platform} if account.pk==source.pk else {account.platform}): raise CommandError('Недопустимые поля или площадка материала.')
                if material['source_id'] not in {m['id'] for m in approved}: raise CommandError('Материал не входит в подтверждённый источник.')
                rows=ReplyMaterial.objects.filter(company_id=account.company_id,title=material['fields']['title'])
                if rows.count()>1: raise CommandError('Дубли названий материалов: перенос отменён.')
                existing=rows.first()
                if account.pk==source.pk and (not existing or existing.pk!=material['source_id'] or any(getattr(existing,k)!=v for k,v in material['fields'].items())):
                    raise CommandError('Пакет пытается изменить исходный материал ЯМ_1.')
                ops.append((account,material,existing))
            self.stdout.write(f'{account.name}: {target["store"]}; подтверждённых материалов {len(target["materials"])}; сценариев {len(target["rules"])}; автоподготовка будет включена.')
        if not o['apply']:
            self.stdout.write('Проверка готова. --apply сохранит резервную копию, перенесёт библиотеку и включит новые черновики. API не вызывался.');return
        sha=hashlib.sha256(raw).hexdigest()
        # Repeat of the same package after a successful rollout does not overwrite
        # later target edits or reactivate anything behind the operator's back.
        if all(Audit.objects.filter(company_id=pk,action='reply.rollout.applied',after__package=sha).exists() for pk in desired):
            self.stdout.write('Этот пакет уже применён. Изменений нет.');return
        before={'companies':list(Company.objects.filter(pk__in=desired).values()),
                'accounts':list(Account.objects.filter(company_id__in=desired).values()),
                'materials':list(ReplyMaterial.objects.filter(company_id__in=desired).values()),
                'rules':[{**row,'accounts':list(ReplyRule.objects.get(pk=row['id']).accounts.values_list('pk',flat=True))} for row in ReplyRule.objects.filter(company_id__in=desired).values()],
                'knowledge':list(KnowledgeEntry.objects.filter(company_id__in=desired).values())}
        backup=settings.STATE_DIR/'backups'/('reply-rollout-before-'+timezone.now().strftime('%Y%m%dT%H%M%S%fZ')+'.json')
        try:
            backup.parent.mkdir(parents=True,exist_ok=True)
            with open(backup,'x',encoding='utf-8',opener=lambda p,f:os.open(p,f,0o600)) as stream:json.dump(before,stream,ensure_ascii=False,indent=2,default=str)
        except OSError as error:raise CommandError('Не удалось сохранить предыдущие настройки; перенос отменён.') from error
        installed={};material_ids={pk:set() for pk in desired};rule_ids={pk:set() for pk in desired}
        for account,material,existing in ops:
            obj=existing or ReplyMaterial(company_id=account.company_id)
            changed=not existing or not existing.approved or any(getattr(obj,k)!=v for k,v in material['fields'].items())
            if changed:
                if obj.pk:obj.version+=1
                for key,value in material['fields'].items():setattr(obj,key,value)
                obj.approved=True;obj.approved_by_id=material.get('approved_by_id');obj.source_thread=None
                obj.full_clean(exclude=['approved_by']);obj.save()
            installed[(account.pk,material['source_id'])]=obj
            material_ids[account.company_id].add(obj.pk)
        for target in payload['targets']:
            account=by_name[target['account_name']]
            for row in target['rules']:
                candidates=ReplyRule.objects.filter(company_id=account.company_id,name=row['name'])
                if candidates.count()>1:raise CommandError('Дубли названий сценариев: перенос отменён.')
                rule=candidates.first() or ReplyRule(company_id=account.company_id)
                wanted_material=installed[(account.pk,row['material_source_id'])] if row.get('material_source_id') else None
                same=bool(rule.pk and all(getattr(rule,key)==row[key] for key in ['name','active','priority','condition_mode','conditions','action']) and rule.material_id==(wanted_material.pk if wanted_material else None) and set(rule.accounts.values_list('pk',flat=True))=={account.pk})
                if same:
                    rule_ids[account.company_id].add(rule.pk);continue
                if rule.pk:rule.version+=1
                for field in ['name','active','priority','condition_mode','conditions','action']:setattr(rule,field,row[field])
                rule.material=wanted_material
                rule.full_clean();rule.save();rule.accounts.set([account.pk]);rule_ids[account.company_id].add(rule.pk)
        for pk,fields in desired.items():
            company=companies[pk]
            if pk!=source.company_id:
                # Preserve archived rows and their historical drafts. Only the
                # current scoped copy participates in WB/YM generation.
                ReplyMaterial.objects.filter(company_id=pk,platform__in=['','wb','ym'],active=True).exclude(pk__in=material_ids[pk]).update(active=False,version=F('version')+1,updated_at=timezone.now())
                ReplyRule.objects.filter(company_id=pk,active=True).exclude(pk__in=rule_ids[pk]).exclude(accounts__platform='ozon').update(active=False,version=F('version')+1,updated_at=timezone.now())
            if not company.ai_auto_enabled:
                Account.objects.filter(company_id=pk).exclude(pk__in=[a.pk for a in accounts]).update(ai_auto_enabled=False)
            for field,value in fields.items():setattr(company,field,value)
            company.ai_auto_enabled=True;company.full_clean();company.save(update_fields=[*fields,'ai_auto_enabled'])
            Audit.objects.create(company=company,action='reply.rollout.applied',object_id=str(pk),after={'package':sha,'source_account':source.pk,'backup':str(backup),'accounts':[a.name for a in accounts if a.company_id==pk]})
        for target in payload['targets']:
            Account.objects.filter(pk=by_name[target['account_name']].pk).update(ai_auto_enabled=True,reply_signature=target['signature'])
        self.stdout.write(f'Перенос завершён. Резервная копия: {backup}')
        self.stdout.write('Автоподготовка включена только для выбранных кабинетов. Старые отзывы массово не поставлены в AI; исходящих сообщений не создано.')
