import json
import uuid
from io import StringIO
from unittest.mock import patch
from django.core.management import call_command
from django.urls import reverse
from desk.ai.context import prepare
from desk.ai.queue import enqueue, process_one
from desk.models import ReplyMaterial, ReplyRule, ReplyDraft, ReplyTestCase, KnowledgeEntry, Thread, Analysis, Outgoing, AIRequest, Audit, Membership
from desk.replies.policy import plan, public_plan
from desk.replies.service import prepare_reply, save_draft, propose_example, enqueue_test, draft_current
from .test_ai_queue import AIConfiguredCase, POSITIVE, NEGATIVE


class ReplyWorkbenchTests(AIConfiguredCase):
    def material(self,**kwargs):
        return ReplyMaterial.objects.create(**{'company':self.company,'title':'Проверенный пример','kind':'example','customer_text':'Аромат понравился','answer':'Здравствуйте! Рады, что аромат Вам понравился.','sentiment':'positive','approved':True,**kwargs})
    def rule(self,**kwargs):
        return ReplyRule.objects.create(**{'company':self.company,'name':'Сценарий','conditions':{},**kwargs})
    def supervisor(self):
        Membership.objects.filter(user=self.user,company=self.company).update(role='supervisor');self.client.force_login(self.user)

    @patch('desk.ai.queue.complete')
    def test_screenshot_longevity_example_is_sent_to_guided_ai_but_not_substituted(self, complete):
        case=ReplyTestCase.objects.create(account=self.ym,title='Обращение №18452',rating=2,product=self.product,
            text='На пару часов хватает, потом выветривается полностью, не оригинал, пустая трата денег')
        material=self.material(title='Образец: Слабая стойкость',sentiment='negative',topic='LONG_AUTH',
            customer_text='Красивый аромат, но держится совсем недолго.',answer='APPROVED_LONGEVITY_WORDING')
        authenticity=self.material(title='Образец: Сомнение в оригинальности',sentiment='negative',topic='LONG_AUTH',
            customer_text='Есть сомнения, что это оригинал.',answer='APPROVED_AUTHENTICITY_WORDING')
        self.material(company=self.other,title='PRIVATE_OTHER_EXAMPLE',sentiment='negative',topic='LONG_AUTH')
        complete.return_value=self.answer({'draft':'Адаптированный ответ модели','used_fact_ids':[]})
        enqueue_test(case,self.user,mode='baseline');enqueue_test(case,self.user,mode='guided')
        process_one();process_one()
        old_context,new_context=[call.args[2] for call in complete.call_args_list]
        self.assertEqual(old_context['reply_examples'],[])
        self.assertEqual([row['id'] for row in new_context['reply_examples']],[material.pk,authenticity.pk])
        self.assertNotIn('PRIVATE_OTHER_EXAMPLE',json.dumps(new_context))
        guided=ReplyDraft.objects.get(test_case=case,method='ai_example')
        self.assertEqual(guided.text,'Адаптированный ответ модели')
        self.assertEqual(guided.explanation['examples'][0]['id'],material.pk)
        self.assertFalse(guided.explanation['template'])
        self.assertFalse(Outgoing.objects.exists());self.assertFalse(Analysis.objects.exists())

    def test_example_order_follows_complaints_without_changing_analytics_or_treating_praise_as_complaint(self):
        from desk.ai.classification import local_classify
        longevity=self.material(title='Слабая стойкость',sentiment='negative',topic='LONG_AUTH',customer_text='Держится совсем недолго.')
        authenticity=self.material(title='Сомнение в оригинальности',sentiment='negative',topic='LONG_AUTH',customer_text='Это не оригинал.')
        for text,first in [('Не оригинал, аромат не держится',authenticity),
                           ('Стойкость отличная, но не оригинал',authenticity),
                           ('Не стойкий, но не подделка',longevity)]:
            with self.subTest(text=text):
                self.thread.text=text;self.thread.rating=2;self.thread.save()
                before=local_classify(text,2)
                self.assertEqual(plan(self.thread)['examples'][0]['id'],first.pk)
                self.assertEqual(local_classify(text,2),before)

    @patch('desk.ai.queue.complete')
    def test_negative_trial_template_scenario_substitutes_exact_text_without_ai(self, complete):
        material=self.material(kind='template',sentiment='negative',topic='LONG_AUTH',answer='Здравствуйте! Точный утверждённый ответ.')
        self.rule(action='template',material=material,conditions={'sentiment':'negative','signals':['LONG_AUTH']})
        case=ReplyTestCase.objects.create(account=self.ym,title='Тест шаблона',rating=2,text='Не стойкий, не оригинал',product=self.product)
        job=enqueue_test(case,self.user);process_one();job.refresh_from_db()
        self.assertEqual(job.status,'done');complete.assert_not_called()
        draft=ReplyDraft.objects.get();self.assertEqual(draft.text,material.answer);self.assertEqual(draft.method,'template')
        self.assertEqual(draft.explanation['template']['id'],material.pk)
        self.assertFalse(AIRequest.objects.exists());self.assertFalse(Outgoing.objects.exists())

    @patch('desk.ai.queue.complete')
    def test_saved_trial_context_shows_frozen_example_version_and_escapes_content(self, complete):
        from desk.replies.diagnostics import saved_context
        material=self.material(answer='<script>FROZEN_EXAMPLE</script>')
        case=ReplyTestCase.objects.create(account=self.account,title='Тест',text=self.thread.text,rating=5,product=self.product)
        complete.return_value=self.answer({'draft':'Ответ модели','used_fact_ids':[]})
        enqueue_test(case,self.user);process_one()
        material.answer='NEW_EXAMPLE';material.version+=1;material.save()
        result=ReplyDraft.objects.select_related('job').get()
        trace=saved_context(result)
        self.assertEqual(trace['examples'][0]['version'],1)
        self.assertEqual(trace['examples'][0]['answer'],'<script>FROZEN_EXAMPLE</script>')
        self.client.force_login(self.user)
        page=self.client.get(reverse('reply_tests'))
        self.assertContains(page,'Как подготовлен ответ');self.assertContains(page,'Основной пример')
        self.assertContains(page,'&lt;script&gt;FROZEN_EXAMPLE&lt;/script&gt;')
        self.assertNotContains(page,'<script>FROZEN_EXAMPLE</script>')
        self.assertNotContains(page,'NEW_EXAMPLE')

    @patch('desk.ai.queue.complete')
    def test_explain_command_is_free_read_only_and_compares_saved_and_current_selection(self, complete):
        material=self.material(title='APPROVED_LOCAL_EXAMPLE')
        rejected=self.material(title='UNAPPROVED_LOCAL_EXAMPLE',approved=False)
        self.material(company=self.other,title='PRIVATE_OTHER_MATERIAL')
        case=ReplyTestCase.objects.create(account=self.account,title='Тест',text=self.thread.text,rating=5,product=self.product,source_thread=self.thread)
        complete.return_value=self.answer({'draft':'Ответ модели','used_fact_ids':[]})
        enqueue_test(case,self.user);process_one()
        material.approved=False;material.save()
        counts=[model.objects.count() for model in [ReplyDraft,AIRequest,Audit,Analysis,Outgoing]]
        output=StringIO();call_command('explain_reply',thread=self.thread.pk,stdout=output)
        text=output.getvalue()
        self.assertIn('Сохранённый вариант',text);self.assertIn('Примеры, переданные AI: #',text)
        self.assertIn('APPROVED_LOCAL_EXAMPLE',text);self.assertIn('UNAPPROVED_LOCAL_EXAMPLE',text)
        self.assertIn('не подтверждён',text);self.assertNotIn('PRIVATE_OTHER_MATERIAL',text)
        self.assertEqual(complete.call_count,1)
        self.assertEqual(counts,[model.objects.count() for model in [ReplyDraft,AIRequest,Audit,Analysis,Outgoing]])

    def test_material_rejection_explains_scope_and_unrouted_template(self):
        from desk.replies.diagnostics import current_selection
        self.material(platform='ym',title='WRONG_PLATFORM')
        self.material(approved=False,title='NOT_APPROVED')
        self.material(kind='template',title='NO_SCENARIO')
        selection={m['title']:m['status'] for m in current_selection(self.thread)['candidates']}
        self.assertEqual(selection['WRONG_PLATFORM'],'другая площадка')
        self.assertEqual(selection['NOT_APPROVED'],'не подтверждён')
        self.assertIn('не выбран действием сценария',selection['NO_SCENARIO'])

    def test_library_filters_company_product_platform_approval_sentiment_and_exclusion(self):
        wanted=self.material()
        for changes in [{'company':self.other},{'product_code':'P2'},{'platform':'ym'},{'approved':False},{'active':False},{'sentiment':'negative'},{'excluded_words':'понравилось'}]:self.material(**changes)
        routing=plan(self.thread)
        self.assertEqual([m['id'] for m in routing['examples']],[wanted.pk])
        snapshot=prepare(self.thread)[2]
        self.assertNotIn('PROFILE_B_PRIVATE',json.dumps(snapshot))

    def test_high_rating_with_complaint_never_selects_generic_positive_example(self):
        self.thread.text='Упаковка отличная, но аромат не держится';self.thread.save()
        self.material()
        negative=self.material(sentiment='negative',topic='LONG_AUTH',answer='Проверим жалобу')
        routing=plan(self.thread,data=POSITIVE)
        self.assertEqual(routing['situation'],'negative');self.assertIn('LONG',routing['all_topics'])
        self.assertEqual([m['id'] for m in routing['examples']],[negative.pk])
        self.assertTrue(routing['requires_human'])

    def test_rule_priority_all_any_and_account_scope_are_deterministic(self):
        wrong=self.rule(name='Другой кабинет',priority=1);wrong.accounts.add(self.ym)
        self.rule(name='Все не совпали',priority=2,conditions={'ratings':[1],'sentiment':'positive'},condition_mode='all')
        expected=self.rule(name='Любое совпало',priority=3,conditions={'ratings':[1],'sentiment':'positive'},condition_mode='any')
        self.rule(name='Поздний',priority=4)
        self.assertEqual(plan(self.thread)['rule']['id'],expected.pk)

    @patch('desk.ai.queue.complete')
    def test_approved_template_requires_no_ai_key_or_http_and_creates_no_outgoing(self,complete):
        material=self.material(kind='template',answer='Здравствуйте! Спасибо за оценку.')
        self.rule(action='template',material=material)
        self.company.ai_provider='none';self.company.save()
        draft=prepare_reply(self.thread,self.user)
        self.assertEqual(draft.method,'template');self.assertEqual(draft.text,material.answer)
        self.assertEqual(prepare_reply(self.thread,self.user).pk,draft.pk)
        complete.assert_not_called();self.assertFalse(Outgoing.objects.exists());self.assertFalse(AIRequest.objects.exists())

    @patch('desk.ai.queue.complete')
    def test_wrong_sentiment_or_revoked_template_falls_back_to_ai_without_publishing(self,complete):
        material=self.material(kind='template');self.rule(action='template',material=material)
        material.approved=False;material.save()
        self.responses(complete)
        job=prepare_reply(self.thread,self.user);process_one();job.refresh_from_db()
        self.assertEqual(job.status,'done');self.assertEqual(complete.call_count,2)
        self.assertEqual(ReplyDraft.objects.get().method,'ai');self.assertFalse(Outgoing.objects.exists())
        self.assertTrue(ReplyDraft.objects.get().explanation['notes'])

    @patch('desk.ai.queue.complete')
    def test_selected_examples_go_to_drafter_but_never_classifier_or_facts(self,complete):
        material=self.material(answer='EXAMPLE_STYLE_ONLY')
        self.responses(complete);job=enqueue(self.thread);process_one()
        self.assertNotIn('EXAMPLE_STYLE_ONLY',json.dumps(complete.call_args_list[0].args[2]))
        draft_context=complete.call_args_list[1].args[2]
        self.assertEqual(draft_context['reply_examples'][0]['id'],material.pk)
        self.assertNotIn('EXAMPLE_STYLE_ONLY',json.dumps(draft_context['verified_facts']))
        self.assertEqual(ReplyDraft.objects.get().method,'ai_example')

    @patch('desk.ai.queue.complete')
    def test_material_or_rule_change_cancels_old_queued_context_before_http(self,complete):
        material=self.material();job=enqueue(self.thread)
        material.answer='Изменённый пример';material.save()
        process_one();job.refresh_from_db();self.assertEqual(job.status,'cancelled');complete.assert_not_called()

    @patch('desk.ai.queue.complete')
    def test_working_style_is_used_and_full_document_is_preserved(self,complete):
        self.company.ai_working_style='COMPACT_WORKING_RULES';self.company.save()
        self.responses(complete);enqueue(self.thread);process_one()
        self.assertEqual(complete.call_args.args[2]['tone_of_voice'],'COMPACT_WORKING_RULES')
        self.company.refresh_from_db();self.assertEqual(self.company.tone_of_voice,'PROFILE_A_ONLY')

    @patch('desk.ai.queue.complete')
    def test_new_variant_reuses_classification_and_keeps_previous_human_edit(self,complete):
        self.responses(complete);job=enqueue(self.thread);process_one()
        old=ReplyDraft.objects.get();save_draft(self.thread,self.user,'Моя правка',draft_id=old.pk,version=old.version)
        complete.side_effect=[self.answer({'draft':'Другой вариант','used_fact_ids':[]})]
        prepare_reply(self.thread,self.user,another=True);process_one()
        self.assertEqual(complete.call_count,3);self.assertEqual(ReplyDraft.objects.count(),2)
        old.refresh_from_db();self.assertEqual(old.text,'Моя правка')

    def test_manual_save_does_not_change_analysis_or_send_and_rejects_stale_version(self):
        analysis=self.analysis()
        draft=save_draft(self.thread,self.user,'Первый текст')
        save_draft(self.thread,self.user,'Правка',draft_id=draft.pk,version=draft.version)
        with self.assertRaises(ValueError):save_draft(self.thread,self.user,'Затереть',draft_id=draft.pk,version=draft.version)
        analysis.refresh_from_db();self.assertEqual(analysis.draft,'Здравствуйте! Уточните, пожалуйста, условия использования.')
        self.assertFalse(Outgoing.objects.exists())

    def test_draft_becomes_stale_when_fact_or_profile_changes(self):
        draft=save_draft(self.thread,self.user,'Текст')
        self.assertTrue(draft_current(draft))
        self.company.ai_working_style='Новые правила';self.company.save()
        self.assertFalse(draft_current(draft))
        with self.assertRaises(ValueError):save_draft(self.thread,self.user,'Правка',draft_id=draft.pk,version=draft.version)

    def test_proposed_example_is_unapproved_and_not_used(self):
        material=propose_example(self.thread,self.user,'PROPOSED_ONLY')
        self.assertFalse(material.approved);self.assertEqual(material.source_thread_id,self.thread.pk)
        self.assertNotIn('PROPOSED_ONLY',json.dumps(prepare(self.thread)[2]))

    @patch('desk.ai.queue.complete')
    def test_sensitive_complaint_can_use_reviewable_draft_and_keeps_all_topics(self,complete):
        self.thread.text='Коробка помята, это подделка';self.thread.save()
        material=self.material(sentiment='negative',topic='PACK')
        self.responses(complete,classification={**NEGATIVE,'signals':['PACK','AUTH-UNKNOWN']})
        enqueue(self.thread);process_one()
        draft=ReplyDraft.objects.get()
        self.assertIn('AUTH-UNKNOWN',draft.explanation['all_topics']);self.assertIn('PACK',draft.explanation['all_topics'])
        self.assertTrue(draft.explanation['requires_human']);self.assertTrue(draft.text);self.assertFalse(Outgoing.objects.exists())

    @patch('desk.ai.queue.complete')
    def test_manual_rule_is_explicit_and_does_not_call_provider(self,complete):
        self.rule(action='manual',conditions={'words':['понравилось']})
        draft=prepare_reply(self.thread,self.user)
        self.assertEqual(draft.method,'manual');self.assertEqual(draft.text,'');complete.assert_not_called()

    def test_empty_review_placeholder_matches_no_text_rule(self):
        self.thread.text='(Отзыв без текста)';self.thread.save()
        expected=self.rule(conditions={'has_text':False,'ratings':[5]})
        self.assertEqual(plan(self.thread)['rule']['id'],expected.pk)

    @patch('desk.ai.queue.complete')
    def test_comparison_is_metered_but_never_creates_thread_analysis_or_outgoing(self,complete):
        case=ReplyTestCase.objects.create(account=self.account,title='Тест',text='Приятный аромат',rating=5,product=self.product)
        initial=Thread.objects.count()
        self.material()
        complete.return_value=self.answer({'draft':'Здравствуйте! Спасибо за отзыв.','used_fact_ids':[]})
        before=enqueue_test(case,self.user,mode='baseline');after=enqueue_test(case,self.user,mode='guided')
        process_one();process_one()
        self.assertEqual(ReplyDraft.objects.filter(test_case=case).count(),2)
        self.assertEqual(Thread.objects.count(),initial);self.assertFalse(Analysis.objects.exists());self.assertFalse(Outgoing.objects.exists())
        self.assertEqual(AIRequest.objects.filter(stage='trial').count(),2)
        self.assertEqual(complete.call_count,2)
        self.assertFalse(complete.call_args_list[0].args[2]['reply_examples']);self.assertTrue(complete.call_args_list[1].args[2]['reply_examples'])
        self.assertEqual(enqueue_test(case,self.user).pk,after.pk)
        self.assertFalse(process_one())

    @patch('desk.ai.queue.complete')
    def test_test_case_change_or_exhausted_budget_never_escapes_queue_guards(self,complete):
        case=ReplyTestCase.objects.create(account=self.account,title='Тест',text='Текст',rating=5)
        job=enqueue_test(case,self.user);case.text='Другой текст';case.save();process_one();job.refresh_from_db()
        self.assertEqual(job.status,'cancelled');complete.assert_not_called()
        self.company.ai_token_limit=1;self.company.save();self.account.company.refresh_from_db()
        job=enqueue_test(case,self.user);process_one();job.refresh_from_db()
        self.assertEqual(job.status,'blocked');complete.assert_not_called()

    def test_other_company_or_wrong_folder_test_model_is_rejected(self):
        from desk.ai.provider import AIProblem
        case=ReplyTestCase.objects.create(account=self.account,title='Тест',text='Текст',rating=5)
        with self.assertRaises(AIProblem):enqueue_test(case,self.user,model='gpt://other/model/latest')
        self.assertFalse(case.ai_jobs.exists())

    def test_web_pages_render_and_other_company_records_are_not_visible(self):
        self.material(title='OUR_MATERIAL');self.material(company=self.other,title='PRIVATE_OTHER_MATERIAL')
        self.client.force_login(self.user)
        for name in ['reply_materials','reply_rules','reply_knowledge','reply_drafts','reply_tests']:
            with self.subTest(page=name):self.assertEqual(self.client.get(reverse(name)).status_code,200)
        page=self.client.get(reverse('reply_materials'))
        self.assertContains(page,'OUR_MATERIAL');self.assertNotContains(page,'PRIVATE_OTHER_MATERIAL')

    def test_operator_can_propose_but_cannot_approve_material_or_modify_rules(self):
        self.client.force_login(self.user)
        for name in ['reply_materials','reply_rules','reply_knowledge']:
            self.assertEqual(self.client.post(reverse(name),{'company':self.company.pk}).status_code,403)
        self.client.post(reverse('thread_action',args=[self.thread.pk]),{'action':'propose_example','text':'Кандидат'})
        self.assertFalse(ReplyMaterial.objects.get().approved)

    def test_supervisor_edits_are_audited_and_cannot_cross_company_or_template_scope(self):
        self.supervisor()
        material=self.material()
        payload={'company':self.company.pk,'version':material.version,'kind':'example','title':material.title,'customer_text':'Текст','answer':'Новый ответ','approved':'on','active':'on'}
        response=self.client.post(reverse('reply_material_edit',args=[material.pk]),payload)
        self.assertEqual(response.status_code,302);material.refresh_from_db();self.assertEqual(material.version,2)
        self.assertEqual(material.approved_by_id,self.user.pk)
        response=self.client.post(reverse('reply_material_edit',args=[material.pk]),payload)
        material.refresh_from_db();self.assertEqual(material.answer,'Новый ответ')
        self.assertContains(response,'изменён другим сотрудником')
        other=self.material(company=self.other,kind='template')
        page=self.client.post(reverse('reply_rules'),{'company':self.company.pk,'name':'Недопустимый','active':'on','priority':1,'condition_mode':'all','action':'template','material':other.pk})
        self.assertEqual(page.status_code,200);self.assertFalse(ReplyRule.objects.exists())
        self.assertTrue(Audit.objects.filter(action='reply.material.saved').exists())

    def test_knowledge_editor_requires_source_and_matching_product(self):
        self.supervisor()
        page=self.client.post(reverse('reply_knowledge'),{'company':self.company.pk,'product_code':'P2','title':'Факт','content':'Контент','source':'Документ','approved':'on','active':'on'})
        self.assertEqual(page.status_code,200);self.assertFalse(KnowledgeEntry.objects.exists())
        self.client.post(reverse('reply_knowledge'),{'company':self.company.pk,'product_code':'P1','title':'Факт','content':'Комплектация','source':'Документ','approved':'on','active':'on'})
        self.assertTrue(KnowledgeEntry.objects.get().approved)

    def test_add_test_from_real_review_is_idempotent_and_keeps_structured_sections(self):
        self.supervisor()
        before=Thread.objects.count()
        for _ in range(2):self.client.post(reverse('thread_action',args=[self.thread.pk]),{'action':'add_test'})
        self.assertEqual(ReplyTestCase.objects.count(),1);self.assertEqual(Thread.objects.count(),before)

    def test_draft_save_web_persists_and_protects_unfilled_send(self):
        self.client.force_login(self.user)
        self.client.post(reverse('thread_action',args=[self.thread.pk]),{'action':'save_draft','text':'Здравствуйте! {{имя_сотрудника}}'})
        draft=ReplyDraft.objects.get()
        page=self.client.get(reverse('thread',args=[self.thread.pk]))
        self.assertContains(page,'Сохранённые варианты');self.assertContains(page,'имя_сотрудника')
        self.client.post(reverse('thread_action',args=[self.thread.pk]),{'action':'send','text':draft.text,'reply_draft':draft.pk,'draft_version':draft.version,'request_key':uuid.uuid4()})
        self.assertFalse(Outgoing.objects.exists())

    def test_pilot_and_seed_commands_are_free_scoped_and_do_not_pollute_inbox(self):
        self.company.legal_entity='АМ';self.company.save()
        before=Thread.objects.count()
        for command in ['seed_reply_materials','prepare_reply_pilot']:
            call_command(command,legal_entity='АМ',stdout=StringIO())
            self.assertFalse(ReplyTestCase.objects.exists());self.assertFalse(ReplyMaterial.objects.exists())
        for _ in range(2):
            call_command('seed_reply_materials',legal_entity='АМ',apply=True,stdout=StringIO())
            call_command('prepare_reply_pilot',legal_entity='АМ',apply=True,stdout=StringIO())
        self.assertEqual(ReplyMaterial.objects.count(),22);self.assertFalse(ReplyMaterial.objects.filter(approved=True).exists())
        self.assertEqual(ReplyTestCase.objects.count(),1);self.assertEqual(Thread.objects.count(),before)
        self.assertFalse(AIRequest.objects.exists());self.assertFalse(Outgoing.objects.exists())

    @patch('desk.ai.queue.complete')
    def test_send_is_idempotent_and_records_selected_original_variant(self,complete):
        self.responses(complete);enqueue(self.thread);process_one();draft=ReplyDraft.objects.get()
        self.client.force_login(self.user);key=uuid.uuid4()
        payload={'action':'send','text':'Здравствуйте! Проверенный ответ сотрудника.','reply_draft':draft.pk,'draft_version':draft.version,'feedback':'accepted','request_key':key}
        for _ in range(2):self.assertEqual(self.client.post(reverse('thread_action',args=[self.thread.pk]),payload).status_code,302)
        self.assertEqual(Outgoing.objects.count(),1)
        item=Outgoing.objects.get();draft.refresh_from_db()
        self.assertEqual(item.ai_draft,draft.original_text);self.assertEqual(draft.feedback,'edited');self.assertEqual(draft.outgoing_id,item.pk)

    def test_stale_version_rolls_back_outgoing_created_in_same_request(self):
        draft=save_draft(self.thread,self.user,'Первый текст')
        save_draft(self.thread,self.user,'Новая правка',draft_id=draft.pk,version=draft.version)
        self.client.force_login(self.user)
        self.client.post(reverse('thread_action',args=[self.thread.pk]),{'action':'send','text':'Старый текст','reply_draft':draft.pk,'draft_version':draft.version,'request_key':uuid.uuid4()})
        self.assertFalse(Outgoing.objects.exists())

    def test_knowledge_edit_accepts_browser_local_timezone_version(self):
        from django.utils import timezone
        self.supervisor()
        fact=KnowledgeEntry.objects.create(company=self.company,product_code='P1',title='Объём',content='100 мл',source='Документ',approved=True)
        response=self.client.post(reverse('reply_knowledge_edit',args=[fact.pk]),{'company':self.company.pk,'version':timezone.localtime(fact.updated_at).isoformat(),'product_code':'P1','title':'Объём','content':'Объём: 100 мл','source':'Новый документ','approved':'on','active':'on'})
        self.assertEqual(response.status_code,302);fact.refresh_from_db();self.assertEqual(fact.source,'Новый документ')

    def test_quality_can_propose_facts_but_does_not_approve_them(self):
        Membership.objects.filter(user=self.user,company=self.company).update(role='quality');self.client.force_login(self.user)
        self.client.post(reverse('reply_knowledge'),{'company':self.company.pk,'title':'Факт','content':'Сведения','source':'Документ','approved':'on','active':'on'})
        self.assertFalse(KnowledgeEntry.objects.get().approved)

    def test_sensitive_topic_cannot_be_hidden_by_packaging_template(self):
        self.thread.text='Коробка помята, товар подделка';self.thread.save()
        material=self.material(kind='template',sentiment='negative',topic='PACK')
        self.rule(action='template',material=material)
        routing=plan(self.thread)
        self.assertEqual(routing['action'],'ai');self.assertTrue(routing['notes'])
        self.assertIn('AUTH-UNKNOWN',routing['all_topics'])

    def test_questions_select_product_examples_without_becoming_negative_reviews(self):
        self.thread.kind='question';self.thread.rating=None;self.thread.text='Какой объём у этой версии?';self.thread.save()
        good=self.material(sentiment='neutral',topic='CONTENT',thread_kind='question')
        self.material(sentiment='positive',thread_kind='review')
        routing=plan(self.thread)
        self.assertEqual(routing['situation'],'neutral')
        self.assertEqual([m['id'] for m in routing['examples']],[good.pk])

    def test_date_filter_includes_whole_moscow_end_day_and_rejects_invalid_range(self):
        from django.utils import timezone
        from datetime import datetime
        self.thread.received_at=timezone.make_aware(datetime(2026,10,29,23,55));self.thread.save()
        self.client.force_login(self.user)
        page=self.client.get(reverse('inbox'),{'since':'2026-10-29','until':'2026-10-29'})
        self.assertContains(page,self.thread.text)
        page=self.client.get(reverse('inbox'),{'since':'2026-10-30','until':'2026-10-29'})
        self.assertContains(page,'Проверьте диапазон дат')

    @patch('desk.ai.queue.complete')
    def test_trial_cannot_commit_after_case_changes_during_http(self,complete):
        case=ReplyTestCase.objects.create(account=self.account,title='Тест',text='Аромат понравился',rating=5)
        def change(*args):
            case.text='Новый текст';case.save()
            return self.answer({'draft':'Устаревший ответ','used_fact_ids':[]})
        complete.side_effect=change
        job=enqueue_test(case,self.user);process_one();job.refresh_from_db()
        self.assertEqual(job.status,'cancelled');self.assertFalse(ReplyDraft.objects.exists())
        self.assertEqual(AIRequest.objects.get().state,'confirmed')

    def test_material_editor_never_exposes_or_edits_another_company(self):
        self.material();self.client.force_login(self.user)
        other=self.material(company=self.other)
        self.assertEqual(self.client.get(reverse('reply_material_edit',args=[other.pk]),{'company':self.company.pk}).status_code,404)
        self.assertEqual(self.client.post(reverse('reply_material_edit',args=[other.pk]),{'company':self.other.pk}).status_code,404)

    def test_rejected_draft_cannot_be_published_by_send_button(self):
        self.client.force_login(self.user)
        draft=save_draft(self.thread,self.user,'Неудачный текст')
        self.client.post(reverse('thread_action',args=[self.thread.pk]),{'action':'send','text':draft.text,'reply_draft':draft.pk,'draft_version':draft.version,'feedback':'rejected','request_key':uuid.uuid4()})
        self.assertFalse(Outgoing.objects.exists())

    @patch('desk.ai.queue.complete')
    def test_accepting_previously_edited_ai_draft_keeps_edit_classification(self,complete):
        self.responses(complete);enqueue(self.thread);process_one();draft=ReplyDraft.objects.get()
        saved=save_draft(self.thread,self.user,'Изменённый ответ',draft_id=draft.pk,version=draft.version,feedback='edited')
        saved=save_draft(self.thread,self.user,saved.text,draft_id=saved.pk,version=saved.version,feedback='accepted')
        self.assertEqual(saved.feedback,'edited')

    @patch('desk.ai.queue.complete')
    def test_two_models_can_run_same_case_without_cancelling_each_other(self,complete):
        case=ReplyTestCase.objects.create(account=self.account,title='Сравнение моделей',text='Аромат понравился',rating=5)
        model=self.company.ai_model
        one=enqueue_test(case,self.user,model=model)
        two=enqueue_test(case,self.user,model='gpt://folder-test/second-model/v1')
        one.refresh_from_db();self.assertEqual(one.status,'queued')
        complete.return_value=self.answer({'draft':'Здравствуйте! Спасибо за отзыв.','used_fact_ids':[]})
        process_one();process_one();one.refresh_from_db();two.refresh_from_db()
        self.assertEqual((one.status,two.status),('done','done'))
        self.assertEqual(ReplyDraft.objects.count(),2);self.assertEqual(AIRequest.objects.count(),2)
        self.company.refresh_from_db();self.assertEqual(self.company.ai_model,model)

    def test_pilot_preserves_attachment_presence_without_sending_urls_or_order_ids(self):
        from desk.replies.service import case_sections, prepare_case
        self.thread.account=self.ym
        self.thread.source_data={'identifiers':{'orderId':'PRIVATE_ID'},'media':{'photos':['https://example.com/private-photo.jpg']}}
        self.thread.save()
        case=ReplyTestCase.objects.create(account=self.ym,title='Фото',text=self.thread.text,rating=5,review_sections=case_sections(self.thread))
        snapshot=prepare_case(case,'guided')[2]
        self.assertEqual(snapshot['available_materials']['photos'],1)
        self.assertTrue(snapshot['available_materials']['order_reference_available'])
        self.assertNotIn('PRIVATE_ID',json.dumps(snapshot));self.assertNotIn('private-photo.jpg',json.dumps(snapshot))

    def test_previous_edits_are_available_as_escaped_restorable_text(self):
        draft=save_draft(self.thread,self.user,'Первый текст <script>NOT_EXECUTABLE</script>')
        save_draft(self.thread,self.user,'Вторая редакция',draft_id=draft.pk,version=draft.version)
        self.client.force_login(self.user)
        page=self.client.get(reverse('thread',args=[self.thread.pk]))
        self.assertContains(page,'История сохранённых правок');self.assertContains(page,'Восстановить в поле')
        self.assertContains(page,'&lt;script&gt;NOT_EXECUTABLE&lt;/script&gt;')
        self.assertNotContains(page,'<script>NOT_EXECUTABLE</script>')

    def test_trial_batch_preview_and_apply_are_scoped_and_never_publish(self):
        from desk.models import AIJob
        self.company.legal_entity='АМ';self.company.save()
        case=ReplyTestCase.objects.create(account=self.account,title='Наш случай',text='Аромат понравился',rating=5)
        other=ReplyTestCase.objects.create(account=self.other_account,title='Чужой случай',text='Private text',rating=5)
        call_command('enqueue_reply_tests',legal_entity='АМ',stdout=StringIO())
        self.assertFalse(AIJob.objects.exists())
        for _ in range(2):call_command('enqueue_reply_tests',legal_entity='АМ',apply=True,stdout=StringIO())
        self.assertEqual(AIJob.objects.count(),1)
        self.assertFalse(other.ai_jobs.exists());self.assertFalse(Outgoing.objects.exists());self.assertFalse(AIRequest.objects.exists())
