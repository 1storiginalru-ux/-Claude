"""Install explicitly supplied approved templates; no customer or AI calls."""
import hashlib
import json
import os
from pathlib import Path
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone
from desk.models import Account, Audit, Company, ReplyMaterial, ReplyRule
from desk.replies.forms import TOPICS


def read_policy(path):
    try:
        with path.open('rb') as stream: raw=stream.read(1024*1024+1)
        if len(raw)>1024*1024: raise ValueError
        data=json.loads(raw.decode('utf-8-sig'))
        if not isinstance(data,dict) or data.get('format')!='voice-reply-policy-1': raise ValueError
        rows=data['templates']
        if not isinstance(rows,list) or not 1<=len(rows)<=50: raise ValueError
        topics={topic for topic,_ in TOPICS if topic}
        titles=set()
        for row in rows:
            if not isinstance(row,dict) or set(row)!={'title','answer','platform','topic','covered_topics','signals','priority'}: raise ValueError
            if not isinstance(row['title'],str) or not 1<=len(row['title'])<=190 or row['title'] in titles: raise ValueError
            titles.add(row['title'])
            if not isinstance(row['answer'],str) or not 1<=len(row['answer'])<=3000 or not row['answer'].startswith('{{приветствие}}'): raise ValueError
            if row['platform'] not in {'wb','ym','ozon'} or row['topic'] not in topics: raise ValueError
            for field in ['covered_topics','signals']:
                if not isinstance(row[field],list) or any(not isinstance(x,str) or x not in topics for x in row[field]): raise ValueError
            if not row['signals'] or type(row['priority']) is not int or not 0<=row['priority']<=10000: raise ValueError
        return rows,hashlib.sha256(raw).hexdigest()
    except (OSError,UnicodeError,ValueError,KeyError,TypeError) as error:
        raise CommandError('Нужен корректный JSON voice-reply-policy-1, до 50 шаблонов. Начало текста: {{приветствие}}; проверьте темы, площадки и приоритеты.') from error


class Command(BaseCommand):
    help='Установить утверждённые шаблоны и сценарии юрлица, включить режим «негатив — шаблон или сотрудник». По умолчанию просмотр.'
    def add_arguments(self,p):
        p.add_argument('--legal-entity',required=True);p.add_argument('--file',required=True,type=Path)
        p.add_argument('--apply',action='store_true');p.add_argument('--replace',action='store_true',help='Явно разрешить замену ранее изменённых шаблонов/сценариев пакета.')

    @transaction.atomic
    def handle(self,*args,**o):
        rows,sha=read_policy(o['file'])
        ids=Account.objects.filter(company__legal_entity=o['legal_entity'],company__is_demo=False,platform__in={r['platform'] for r in rows}).values('company_id')
        companies=list(Company.objects.select_for_update(no_key=True).filter(pk__in=ids).order_by('pk'))
        if not companies: raise CommandError('Реальные кабинеты юрлица для площадок пакета не найдены.')
        operations=[]
        for company in companies:
            for row in rows:
                accounts=list(Account.objects.filter(company=company,platform=row['platform']).values_list('pk',flat=True))
                if not accounts: continue
                fields={k:row[k] for k in ['title','answer','platform','topic','covered_topics']}
                fields.update(kind='template',thread_kind='review',sentiment='negative',approved=True,active=True)
                matches=ReplyMaterial.objects.filter(company=company,title=row['title'])
                rules=ReplyRule.objects.filter(company=company,name=row['title'])
                if matches.count()>1 or rules.count()>1: raise CommandError('Найдены дубли названий пакета. Уточните названия перед импортом.')
                material=matches.first();rule=rules.first()
                conditions={'kind':'review','sentiment':'negative','signals':row['signals']}
                changed=not material or any(getattr(material,k)!=v for k,v in fields.items())
                rule_changed=not rule or (rule.material_id!=(material.pk if material else None) or rule.action!='template' or rule.priority!=row['priority'] or rule.conditions!=conditions or rule.condition_mode!='all' or not rule.active or set(rule.accounts.values_list('pk',flat=True))!=set(accounts))
                if ((material and changed) or (rule and rule_changed)) and not o['replace']:
                    raise CommandError(f'{company.name}: «{row["title"]}» уже изменён. Для явной замены нужен --replace; изменений нет.')
                operations.append((company,row,accounts,fields,conditions,material,rule,changed,rule_changed))
                self.stdout.write(f'{company.name}: {row["title"]}; приоритет {row["priority"]}; шаблон → проверка сотрудником.')
        self.stdout.write(f'Компаний: {len(companies)}. Сценариев пакета: {len(operations)}. Негатив без подходящего шаблона → сотруднику.')
        if not o['apply']:
            self.stdout.write('Просмотр. --apply утвердит тексты пакета и включит режим. API и отправки не запускаются.');return
        if not any(op[-2] or op[-1] for op in operations) and all(c.reply_sensitive_templates for c in companies):
            self.stdout.write('Эта версия уже установлена. Изменений нет.');return
        backup=settings.STATE_DIR/'backups'/('reply-policy-before-'+timezone.now().strftime('%Y%m%dT%H%M%S%fZ')+'.json')
        before={'companies':list(Company.objects.filter(pk__in=[c.pk for c in companies]).values('id','name','reply_sensitive_templates')),
                'materials':list(ReplyMaterial.objects.filter(pk__in=[op[5].pk for op in operations if op[5]]).values()),
                'rules':[{**r,'accounts':list(ReplyRule.objects.get(pk=r['id']).accounts.values_list('pk',flat=True))} for r in ReplyRule.objects.filter(pk__in=[op[6].pk for op in operations if op[6]]).values()]}
        try:
            backup.parent.mkdir(parents=True,exist_ok=True)
            with open(backup,'x',encoding='utf-8',opener=lambda p,f:os.open(p,f,0o600)) as stream: json.dump(before,stream,ensure_ascii=False,indent=2,default=str)
        except OSError as error: raise CommandError('Не удалось сохранить прежние настройки. Импорт отменён.') from error
        for company,row,accounts,fields,conditions,material,rule,changed,rule_changed in operations:
            if changed:
                material=material or ReplyMaterial(company=company)
                if material.pk: material.version+=1
                for key,value in fields.items(): setattr(material,key,value)
                material.approved_by=None;material.full_clean(exclude=['approved_by']);material.save()
            if rule_changed:
                rule=rule or ReplyRule(company=company)
                if rule.pk: rule.version+=1
                rule.name=row['title'];rule.priority=row['priority'];rule.action='template';rule.material=material
                rule.active=True;rule.condition_mode='all';rule.conditions=conditions;rule.full_clean();rule.save();rule.accounts.set(accounts)
        for company in companies:
            company.reply_sensitive_templates=True;company.save(update_fields=['reply_sensitive_templates'])
            Audit.objects.create(company=company,action='reply.policy.import',object_id=str(company.pk),after={'sha256':sha,'strict_templates':True,'backup':str(backup)})
        self.stdout.write(f'Шаблоны и сценарии установлены. Резервная копия: {backup}')
        self.stdout.write('Профили, ключи, автоанализ и рабочие отзывы не менялись. Платные задачи и отправки не создавались.')
