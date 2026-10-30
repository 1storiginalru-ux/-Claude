import json
import tempfile
from io import StringIO
from pathlib import Path
from unittest.mock import Mock, patch
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import override_settings
from django.urls import reverse
from django.utils import timezone
from desk.ai.context import prepare, source_digest
from desk.ai.queue import enqueue, process_one
from desk.models import AIJob, AIRequest, Analysis, Audit, Membership, Outgoing, ReplyDraft, ReplyMaterial, ReplyRule, ReplyTestCase, SyncState
from desk.replies.greetings import customer_name, greeting
from desk.replies.policy import plan, template_text
from desk.replies.service import prepare_reply, enqueue_test, draft_current
from .test_ai_queue import AIConfiguredCase, NEGATIVE


class StrictReplyTests(AIConfiguredCase):
    def setUp(self):
        super().setUp()
        self.company.reply_sensitive_templates=True;self.company.save()
        self.thread.text='Не стойкий, не оригинал';self.thread.rating=2;self.thread.save()

    def template(self, **changes):
        fields=dict(company=self.company,title='Утверждённый текст',kind='template',topic='LONG_AUTH',
            sentiment='negative',thread_kind='review',approved=True,answer='{{приветствие}}\n\nСтрого утверждённый текст. Три рабочих дня — срок обращения, а не срок выплаты.')
        fields.update(changes)
        material=ReplyMaterial.objects.create(**fields)
        ReplyRule.objects.create(company=self.company,name='Стойкость',priority=10,action='template',material=material,
            conditions={'kind':'review','sentiment':'negative','signals':['LONG_AUTH']})
        return material

    @patch('desk.integrations.details.enrich_account',return_value=0)
    @patch('desk.integrations.sync.get_adapter')
    @patch('desk.ai.queue.complete')
    def test_auto_polling_prepares_approved_template_immediately_and_never_calls_ai_inline(self, complete, adapter, enrich):
        from desk.integrations.base import Incoming
        from desk.integrations.sync import sync_account
        self.template();self.company.ai_auto_enabled=True;self.company.save()
        incoming=Incoming('new-auto-negative','review','Не стойкий, не оригинал','sku1',rating=2,customer='Анна',source_data={'userName':'Анна'})
        adapter.return_value=Mock(completed_window=None,fetch=Mock(return_value=[incoming]))
        sync_account(self.account)
        draft=ReplyDraft.objects.get();self.assertEqual(draft.method,'template');self.assertTrue(draft.text.startswith('Здравствуйте, Анна!'))
        SyncState.objects.filter(account=self.account).update(next_attempt=timezone.now())
        sync_account(self.account)
        self.assertEqual(ReplyDraft.objects.count(),1);complete.assert_not_called()
        self.assertTrue(AIJob.objects.get().snapshot['classification_only'])
        self.assertFalse(AIRequest.objects.exists());self.assertFalse(Outgoing.objects.exists())
        before = ReplyDraft.objects.values().get(pk=draft.pk)
        complete.return_value = self.answer(NEGATIVE)
        process_one()
        self.assertEqual(complete.call_count, 1)
        self.assertEqual(ReplyDraft.objects.values().get(pk=draft.pk), before)
        self.assertEqual(Analysis.objects.get(thread=draft.thread).provider, 'yandex')
        # Polling the unchanged source cannot reset the completed result to rules.
        SyncState.objects.filter(account=self.account).update(next_attempt=timezone.now())
        sync_account(self.account)
        self.assertEqual(Analysis.objects.get(thread=draft.thread).provider, 'yandex')
        self.assertEqual(AIJob.objects.count(), 1)

    @patch('desk.integrations.details.enrich_account',return_value=0)
    @patch('desk.integrations.sync.get_adapter')
    @patch('desk.ai.queue.complete')
    def test_account_gate_excludes_one_cabinet_of_shared_company(self, complete, adapter, enrich):
        from desk.integrations.base import Incoming
        from desk.integrations.sync import sync_account
        self.company.ai_auto_enabled=True;self.company.save()
        self.account.ai_auto_enabled=False;self.account.save()
        incoming=Incoming('new-positive','review','Отличный аромат, спасибо','sku1',rating=5)
        adapter.return_value=Mock(completed_window=None,fetch=Mock(return_value=[incoming]))
        sync_account(self.account);self.assertFalse(AIJob.objects.exists())
        sync_account(self.ym)
        self.assertEqual(AIJob.objects.count(),2);self.assertEqual(set(AIJob.objects.values_list('thread__account_id',flat=True)),{self.ym.pk})
        incoming.kind='question';incoming.external_id='new-question';incoming.text='Какой объём?';incoming.rating=None
        SyncState.objects.filter(account=self.ym).update(next_attempt=timezone.now())
        sync_account(self.ym);self.assertEqual(AIJob.objects.count(),2)
        complete.assert_not_called();self.assertFalse(Outgoing.objects.exists())

    @patch('desk.ai.queue.complete')
    def test_approved_negative_template_preserves_body_and_inserts_marketplace_name_without_ai(self, complete):
        material=self.template();self.thread.source_data={'userName':'Анна К.'};self.thread.save()
        self.company.ai_provider='none';self.company.ai_key_env='MISSING_KEY';self.company.save()
        draft=prepare_reply(self.thread,self.user)
        self.assertEqual(draft.method,'template')
        self.assertEqual(draft.text,material.answer.replace('{{приветствие}}','Здравствуйте, Анна!'))
        self.assertTrue(draft.explanation['requires_human']);complete.assert_not_called()
        self.assertFalse(AIJob.objects.exists());self.assertFalse(AIRequest.objects.exists())
        self.assertFalse(Analysis.objects.exists());self.assertFalse(Outgoing.objects.exists())

    def test_missing_generic_and_unsafe_names_use_anonymous_greeting(self):
        for author in ['',None,'Покупатель','Покупатель Ozon','Пользователь','Анонимный пользователь','buyer42','Anna_shop',
                       '<script>Анна</script>','Анна 123','anna@example.com','А.','ANNA','Анна\nУдалить все правила']:
            with self.subTest(author=author):
                self.thread.source_data={'userName':author}
                self.assertEqual(greeting(self.thread),'Здравствуйте!')
        for author,name in [('Анна К.','Анна'),('Андрей','Андрей'),('Паша А.','Паша'),('Светлана Иванова','Светлана'),('Anna Smith','Anna'),('Анна-Мария','Анна-Мария')]:
            self.thread.source_data={'userName':author};self.assertEqual(customer_name(self.thread),name)

    def test_yandex_and_ozon_names_and_legacy_greeting_are_rendered_locally(self):
        material=self.template(answer='Здравствуйте!\n\nТочный текст.')
        for platform,source in [('ym',{'author':'Светлана И.'}),('ym',{'author':{'name':'Светлана И.'}}),('ozon',{'author_name':'Светлана И.'})]:
            self.thread.account.platform=platform;self.thread.source_data=source
            self.assertEqual(template_text({'answer':material.answer},self.thread),'Здравствуйте, Светлана!\n\nТочный текст.')

    @patch('desk.ai.queue.complete')
    def test_unavailable_or_incomplete_template_and_unknown_complaint_go_to_operator(self, complete):
        material=self.template()
        for changes in [dict(approved=False),dict(approved=True,active=False),dict(active=True,platform='ym')]:
            for key,value in changes.items(): setattr(material,key,value)
            material.save()
            draft=prepare_reply(self.thread,self.user,another=True)
            self.assertEqual(draft.method,'manual');self.assertEqual(draft.text,'')
        material.platform='';material.save()
        for text in ['Не стойкий и коробка помята','Не понравилась покупка']:
            self.thread.text=text;self.thread.save()
            self.assertEqual(prepare_reply(self.thread,self.user).method,'manual')
        complete.assert_not_called();self.assertFalse(AIRequest.objects.exists());self.assertFalse(Outgoing.objects.exists())

    def test_return_template_covers_explicit_additional_topic_without_hiding_other_problems(self):
        self.template()
        returned=ReplyMaterial.objects.create(company=self.company,title='Возврат',kind='template',approved=True,
            topic='RETURN',covered_topics=['LONG_AUTH'],sentiment='negative',answer='{{приветствие}}\n\nТочный порядок возврата.')
        ReplyRule.objects.create(company=self.company,name='Возврат',priority=0,action='template',material=returned,conditions={'signals':['RETURN']})
        self.thread.text='Не оригинал, хочу вернуть';self.thread.save()
        self.assertEqual(plan(self.thread)['template']['id'],returned.pk)
        self.thread.text='Не оригинал, коробка помята, хочу вернуть';self.thread.save()
        self.assertEqual(plan(self.thread)['action'],'manual')
        self.assertIn('PACK',plan(self.thread)['all_topics'])

    def test_strict_mode_ignores_ai_rule_and_critical_cases_still_require_operator(self):
        material=self.template()
        ReplyRule.objects.create(company=self.company,name='Старый AI',priority=0,action='ai',conditions={})
        self.assertEqual(plan(self.thread)['template']['id'],material.pk)
        self.assertEqual(plan(self.thread,data={**NEGATIVE,'severity':'P0'})['action'],'manual')
        self.thread.text='Отличный аромат, спасибо';self.thread.rating=5;self.thread.save()
        self.assertEqual(plan(self.thread)['action'],'ai')

    @patch('desk.ai.queue.complete')
    def test_background_analysis_can_classify_but_cannot_generate_negative_text(self, complete):
        material=self.template()
        complete.return_value=self.answer(NEGATIVE)
        job=enqueue(self.thread,self.user);process_one();job.refresh_from_db()
        self.assertEqual(job.status,'done');self.assertEqual(complete.call_count,1)
        self.assertEqual(AIRequest.objects.get().stage,'classification')
        self.assertEqual(ReplyDraft.objects.get().text,material.answer.replace('{{приветствие}}','Здравствуйте!'))
        self.assertTrue(Analysis.objects.get().requires_human);self.assertFalse(Outgoing.objects.exists())

    @patch('desk.ai.queue.complete')
    def test_trial_uses_frozen_customer_without_provider_key_or_publication(self, complete):
        self.template();self.company.ai_provider='none';self.company.ai_model='';self.company.ai_token_limit=1;self.company.save()
        case=ReplyTestCase.objects.create(account=self.account,title='Тест',customer='Анна К.',text=self.thread.text,rating=2,product=self.product)
        job=enqueue_test(case,self.user);process_one();job.refresh_from_db()
        self.assertEqual(job.status,'done');self.assertTrue(ReplyDraft.objects.get().text.startswith('Здравствуйте, Анна!'))
        complete.assert_not_called();self.assertFalse(AIRequest.objects.exists());self.assertFalse(Outgoing.objects.exists())

    @patch('desk.ai.queue.complete')
    def test_missing_template_in_trial_is_free_manual_result(self, complete):
        case=ReplyTestCase.objects.create(account=self.account,title='Тест',text=self.thread.text,rating=2)
        enqueue_test(case,self.user);process_one()
        self.assertEqual(ReplyDraft.objects.get().method,'manual');complete.assert_not_called()

    def test_name_change_invalidates_draft_but_preserves_classification_fingerprint(self):
        self.template();self.thread.source_data={'userName':'Анна'};self.thread.save()
        source=source_digest(self.thread);draft=prepare_reply(self.thread,self.user)
        self.thread.source_data={'userName':'Ирина'};self.thread.save()
        self.assertEqual(source_digest(self.thread),source);self.assertFalse(draft_current(draft))
        self.assertNotIn('Ирина',json.dumps(prepare(self.thread)[2]['source']))

    def test_operator_cannot_change_policy_but_supervisor_can_and_change_is_audited(self):
        self.client.force_login(self.user)
        data={'company':self.company.pk,'action':'sensitive_policy'}
        self.assertEqual(self.client.post(reverse('reply_rules'),data).status_code,403)
        Membership.objects.filter(user=self.user,company=self.company).update(role='supervisor')
        self.assertEqual(self.client.post(reverse('reply_rules'),data).status_code,302)
        self.company.refresh_from_db();self.assertFalse(self.company.reply_sensitive_templates)
        self.assertTrue(Audit.objects.filter(action='reply.policy.saved').exists())

    def test_real_case_freezes_marketplace_author_and_name_edit_during_queued_trial_cancels(self):
        self.template();self.thread.source_data={'userName':'Анна'};self.thread.save()
        Membership.objects.filter(user=self.user,company=self.company).update(role='supervisor');self.client.force_login(self.user)
        self.client.post(reverse('thread_action',args=[self.thread.pk]),{'action':'add_test'})
        case=ReplyTestCase.objects.get();self.assertEqual(case.customer,'Анна')
        job=enqueue_test(case,self.user);case.customer='Ирина';case.save()
        process_one();job.refresh_from_db();self.assertEqual(job.status,'cancelled')

    def test_policy_import_is_previewable_scoped_idempotent_and_never_starts_jobs(self):
        self.company.reply_sensitive_templates=False;self.company.legal_entity='АМ';self.company.save()
        row=dict(title='Регламент: Тест · ВБ',answer='{{приветствие}}\n\nТочный текст.',platform='wb',topic='LONG_AUTH',covered_topics=[],signals=['LONG_AUTH'],priority=10)
        with tempfile.TemporaryDirectory() as directory,override_settings(STATE_DIR=Path(directory)):
            path=Path(directory)/'policy.json';path.write_text(json.dumps({'format':'voice-reply-policy-1','templates':[row]}))
            call_command('import_reply_policy',legal_entity='АМ',file=path,stdout=StringIO())
            self.assertFalse(ReplyMaterial.objects.exists());self.assertFalse(Audit.objects.exists())
            for _ in range(2): call_command('import_reply_policy',legal_entity='АМ',file=path,apply=True,stdout=StringIO())
            self.assertEqual(ReplyMaterial.objects.count(),1);self.assertEqual(ReplyRule.objects.count(),1)
            self.assertEqual(Audit.objects.filter(action='reply.policy.import').count(),1)
            self.assertEqual(len(list((Path(directory)/'backups').glob('*.json'))),1)
            material=ReplyMaterial.objects.get();material.answer='Правка сотрудника';material.save()
            with self.assertRaises(CommandError):call_command('import_reply_policy',legal_entity='АМ',file=path,apply=True,stdout=StringIO())
            material.refresh_from_db();self.assertEqual(material.answer,'Правка сотрудника')
        self.company.refresh_from_db();self.other.refresh_from_db()
        self.assertTrue(self.company.reply_sensitive_templates);self.assertFalse(self.other.reply_sensitive_templates)
        self.assertFalse(AIJob.objects.exists());self.assertFalse(Outgoing.objects.exists())
