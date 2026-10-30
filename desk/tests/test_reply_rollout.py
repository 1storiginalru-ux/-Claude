import json
import os
import tempfile
from io import StringIO
from pathlib import Path
from unittest.mock import patch
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import override_settings
from desk.models import Account, Audit, Company, KnowledgeEntry, ReplyMaterial, ReplyRule, AIJob, Outgoing
from desk.management.commands.apply_reply_rollout import digest, GUARD_FIELDS, MATERIAL_FIELDS, PROFILE_FIELDS, AI_FIELDS
from .test_ai_queue import AIConfiguredCase


class RolloutTests(AIConfiguredCase):
    def package(self):
        source=self.ym
        source.company=self.other;source.save()
        for field in AI_FIELDS:setattr(self.other,field,getattr(self.company,field))
        self.other.tone_of_voice='SOURCE_STYLE';self.other.knowledge='SOURCE_FACTS';self.other.ai_working_style='SOURCE_WORKING'
        self.other.reply_sensitive_templates=True;self.other.save()
        Account.objects.create(company=self.company,name='Ozon sibling',platform='ozon',enabled=False)
        material=ReplyMaterial.objects.create(company=self.other,title='Source template',kind='template',answer='{{приветствие}} Source body.',platform='ym',approved=True,approved_by=self.user,topic='LONG_AUTH',sentiment='negative')
        rule=ReplyRule.objects.create(company=self.other,name=material.title,material=material,action='template',conditions={'signals':['LONG_AUTH']});rule.accounts.set([source.pk])
        row=ReplyMaterial.objects.filter(pk=material.pk).values().get()
        source_rules=[{**r,'accounts':[source.pk]} for r in ReplyRule.objects.filter(company=self.other).values()]
        targets=[]
        for a in [source,self.account]:
            fields={key:row[key] for key in MATERIAL_FIELDS}
            if a.pk!=source.pk:fields.update(title='Target template',platform='wb',answer='{{приветствие}} Target body.')
            targets.append({'account_name':a.name,'account_id':a.pk,'company_id':a.company_id,'platform':a.platform,'store':'Test shop',
                'profiles':{key:getattr(self.other,key) for key in PROFILE_FIELDS},'ai':{key:getattr(self.other,key) for key in AI_FIELDS},'strict_templates':True,
                'signature':'Test shop, {{имя_сотрудника}}','materials':[{'source_id':material.pk,'approved_by_id':self.user.pk,'fields':fields}],
                'rules':[{'name':fields['title'],'active':True,'priority':100,'condition_mode':'all','conditions':{'signals':['LONG_AUTH']},'action':'template','material_source_id':material.pk}]})
        return {'format':'voice-reply-rollout-1','source_account_id':source.pk,'source_account_name':source.name,
            'source_guard':{'profiles':digest(Company.objects.filter(pk=self.other.pk).values(*GUARD_FIELDS).get()),'materials':digest([row]),'rules':digest(source_rules),'knowledge':digest([])},'targets':targets},material

    def test_rollout_preserves_source_approves_targets_excludes_sibling_and_repeats_without_duplicates(self):
        package,source=self.package()
        before=ReplyMaterial.objects.filter(pk=source.pk).values().get()
        old=ReplyMaterial.objects.create(company=self.company,title='Obsolete',answer='KEEP_ARCHIVED_BODY',kind='template',approved=True)
        with tempfile.TemporaryDirectory() as directory,override_settings(STATE_DIR=Path(directory)):
            path=Path(directory)/'rollout.json';path.write_text(json.dumps(package,default=str))
            call_command('apply_reply_rollout',file=path,stdout=StringIO())
            self.company.refresh_from_db();self.assertFalse(self.company.ai_auto_enabled)
            for _ in range(2):call_command('apply_reply_rollout',file=path,apply=True,stdout=StringIO())
            self.assertEqual(len(list((Path(directory)/'backups').glob('*.json'))),1)
        self.assertEqual(ReplyMaterial.objects.filter(pk=source.pk).values().get(),before)
        self.assertTrue(ReplyMaterial.objects.get(title='Target template').approved)
        self.assertEqual(ReplyMaterial.objects.filter(title='Target template').count(),1)
        self.assertFalse(Account.objects.get(name='Ozon sibling').ai_auto_enabled)
        self.company.refresh_from_db();self.assertTrue(self.company.ai_auto_enabled)
        old.refresh_from_db();self.assertFalse(old.active);self.assertEqual(old.answer,'KEEP_ARCHIVED_BODY');self.assertEqual(old.version,2)
        self.assertEqual(Audit.objects.filter(action='reply.rollout.applied').count(),2)
        self.assertFalse(AIJob.objects.exists());self.assertFalse(Outgoing.objects.exists())

    def test_source_change_after_export_aborts_before_any_target_write(self):
        package,source=self.package();source.answer='Fresh manager edit';source.save()
        with tempfile.TemporaryDirectory() as directory,override_settings(STATE_DIR=Path(directory)):
            path=Path(directory)/'rollout.json';path.write_text(json.dumps(package,default=str))
            with self.assertRaisesMessage(CommandError,'изменился после экспорта'):call_command('apply_reply_rollout',file=path,apply=True,stdout=StringIO())
            self.assertFalse((Path(directory)/'backups').exists())
        self.company.refresh_from_db();self.assertFalse(self.company.ai_auto_enabled)
        self.assertFalse(ReplyMaterial.objects.filter(title='Target template').exists())

    def test_missing_provider_key_aborts_before_enabling_automatic_work(self):
        package,_=self.package()
        with tempfile.TemporaryDirectory() as directory,override_settings(STATE_DIR=Path(directory)),patch.dict(os.environ,{'TEST_AI_KEY':''}):
            path=Path(directory)/'rollout.json';path.write_text(json.dumps(package,default=str))
            with self.assertRaises(CommandError):call_command('apply_reply_rollout',file=path,apply=True,stdout=StringIO())
            self.assertFalse((Path(directory)/'backups').exists())
        self.assertFalse(Audit.objects.exists());self.assertFalse(AIJob.objects.exists())
