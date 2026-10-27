import copy
import json
import os
import uuid
from io import StringIO
from unittest.mock import Mock, patch
from django.core.management import call_command
from django.test import SimpleTestCase
from django.utils import timezone
from desk.ai.classification import local_classify, review_sections, RULES_VERSION, PROMPT_VERSION
from desk.ai.provider import analyze
from desk.integrations.sync import ingest
from desk.integrations.yandex import YandexMarket
from desk.integrations.wildberries import Wildberries
from desk.models import Analysis, Audit, Incident, Outgoing, ReviewAssessment
from desk.reviews import assess_review, assessment_is_current, fingerprint
from .base import DeskCase


SCREENSHOT_CASES = [
    {'advantages': 'Вроде не подделка, норм!'},
    {'advantages': 'аромат,стойкость'},
    {'advantages': 'Цена, качество, быстрая доставка. Спасибо за подарок.'},
    {'advantages': 'все понравилось, прислали быстро, упаковали дополнительно в коробку, туалетная вода пришла в идеальном состоянии, запах соответствует оригиналу. Цена очень привлекательная.', 'disadvantages': 'нет'},
    {'advantages': 'Однозначно оригинал... Аромат именно тот!!! Классика', 'disadvantages': 'нет и быть не может, не первый раз заказываю', 'comment': 'Заказывал в данном магазине не одну туалетную воду и всегда радует качество, скорость доставки... лично мне всегда приходили оригинальные товары'},
]


class ComplaintRuleTests(SimpleTestCase):
    def test_five_reported_examples_are_not_hidden_negatives(self):
        for sections in SCREENSHOT_CASES:
            with self.subTest(sections=sections):
                text = '\n'.join(sections.get(k, '') for k in ['advantages', 'disadvantages', 'comment']).strip()
                result = local_classify(text, 5, sections=sections)
                self.assertEqual((result['sentiment'], result['hidden_negative'], result['signals']), ('positive', False, []))
                self.assertFalse(result['objective_issue'])

    def test_no_disadvantages_and_positive_mentions_do_not_mean_complaints(self):
        cases = ['Нет', 'Нету', 'Не обнаружено', 'Недостатков нет', 'Минусов не нашла', 'Не выявлено!',
                 'Нет и быть не может, не первый раз заказываю', 'Нет, не заметила', 'Никаких недостатков не обнаружила']
        for text in cases:
            with self.subTest(text=text):
                result = local_classify('Хороший аромат\n'+text, 5, sections={'advantages':'Хороший аромат','disadvantages':text})
                self.assertFalse(result['hidden_negative'])
                self.assertEqual(result['signals'], [])
        positives = ['Не подделка', 'Не сомневаюсь в оригинальности', 'Нет сомнений в оригинальности',
                     'Сомнений в оригинальности нет', 'Не могу назвать это подделкой', 'Оригинальность на высоте',
                     'Стойкость хорошая', 'Отличная упаковка', 'Упаковка не помята', 'Флакон не протёк',
                     'Без повреждений, коробка целая', 'Нет проблем с упаковкой', 'Упаковка не имеет повреждений',
                     'Не пахнет спиртом', 'Аромат не выветривается', 'Возврат не понадобился',
                     'Не пришлось возвращать товар', 'Маркировка читается', 'DataMatrix сканируется',
                     'Доставка не опоздала', 'Быстрая доставка, хорошая упаковка', 'Через час аромат всё ещё держится']
        for text in positives:
            with self.subTest(text=text):
                result=local_classify(text, 5)
                self.assertFalse(result['hidden_negative'])
                self.assertEqual(result['signals'], [])

    def test_real_complaints_still_survive_five_stars_and_word_net(self):
        cases = [('Нет стойкости','LONG'), ('Стойкости нет','LONG'), ('Совсем не стойкий','LONG'),
                 ('Аромат не держится даже час','LONG'), ('Стойкость слабая','LONG'), ('Стойкость хотелось бы лучше','LONG'),
                 ('Аромат исчезает через час','LONG'), ('Через час запах исчезает','LONG'), ('Держится всего 15 минут','LONG'),
                 ('Нет запаха','SCENT'), ('Не нравится запах','SCENT'), ('Не пахнет','SCENT'), ('Пахнет спиртом','SCENT'),
                 ('Это подделка','AUTH-UNKNOWN'), ('Нет, это подделка!','AUTH-UNKNOWN'), ('Не оригинал','AUTH-UNKNOWN'),
                 ('Сомневаюсь в оригинальности','AUTH-UNKNOWN'), ('Оригинальность под вопросом','AUTH-UNKNOWN'),
                 ('Не подделка ли это?','AUTH-UNKNOWN'), ('Не подделка?','AUTH-UNKNOWN'),
                 ('Флакон протёк','DEFECT'), ('Распылитель не работает','DEFECT'), ('Не работает распылитель','DEFECT'),
                 ('Нет крышки','DEFECT'), ('Упаковка помята','PACK'), ('Коробка вскрыта','PACK'), ('Нет коробки','PACK'),
                 ('Упаковка не очень хорошая','PACK'), ('Маркировка не читается','MARK'), ('Нет маркировки','MARK'),
                 ('DataMatrix не сканируется','MARK'), ('Доставили поздно','DELIVERY'), ('Доставка долгая','DELIVERY'),
                 ('Хочу вернуть товар','RETURN'), ('Как оформить возврат?','RETURN')]
        for text, signal in cases:
            with self.subTest(text=text):
                result=local_classify(text, 5)
                self.assertEqual(result['sentiment'], 'negative')
                self.assertTrue(result['hidden_negative'])
                self.assertIn(signal, result['signals'])

    def test_negation_scope_does_not_hide_a_different_real_issue(self):
        cases = [('Упаковка отличная, нет стойкости', ['LONG']),
                 ('Это не подделка, но упаковка помята', ['PACK']),
                 ('Коробка не повреждена, но флакон протёк', ['DEFECT']),
                 ('Недостатков нет, но запах не держится', ['LONG']),
                 ('Упаковка отличная\nНет стойкости', ['LONG']),
                 ('Доставка быстрая, но аромат не держится', ['LONG'])]
        for text, signals in cases:
            with self.subTest(text=text):
                result=local_classify(text, 5)
                self.assertEqual(result['signals'], signals)
                self.assertTrue(result['hidden_negative'])

    def test_structured_field_role_and_unstructured_labels_are_respected(self):
        self.assertEqual(local_classify('стойкость',5,sections={'advantages':'стойкость'})['signals'], [])
        self.assertEqual(local_classify('стойкость',5,sections={'disadvantages':'стойкость'})['signals'], ['LONG'])
        self.assertFalse(local_classify('Достоинства: стойкость\nНедостатки: нет',5)['hidden_negative'])
        self.assertTrue(local_classify('Достоинства: аромат\nНедостатки: нет стойкости',5)['hidden_negative'])
        for text in ['Упаковка не помята', 'Стойкость не слабая', 'Быстрая доставка', 'Приятный аромат', 'Коробка целая']:
            with self.subTest(misplaced_positive=text):
                self.assertFalse(local_classify(text,5,sections={'disadvantages':text})['hidden_negative'])

    def test_low_rating_contradiction_is_left_for_review_and_questions_are_not_hidden_reviews(self):
        result=local_classify('Недостатков нет',1)
        self.assertEqual(result['sentiment'],'negative')
        self.assertFalse(result['hidden_negative'])
        self.assertEqual(result['confidence'],0)
        self.assertFalse(local_classify('Нет стойкости',5,kind='question')['hidden_negative'])


class MarketFixture(DeskCase):
    def market(self, description, external_id=901):
        row={'feedbackId':external_id,'createdAt':timezone.now().isoformat(),'identifiers':{'offerId':'sku1'},'statistics':{'rating':5,'commentsCount':0},'description':description}
        thread, changed=ingest(self.ym,YandexMarket(self.ym).parse(row,'review'))
        return thread,changed,row

class StructuredClassificationTests(MarketFixture):
    def test_actual_yandex_parse_ingest_and_analysis_pass_five_examples(self):
        self.client.force_login(self.user)
        for index,sections in enumerate(SCREENSHOT_CASES):
            with self.subTest(index=index):
                thread,_,_=self.market(sections,external_id=901+index)
                original=thread.text
                result=analyze(thread)
                self.assertEqual((result.sentiment,result.hidden_negative,result.signals),('positive',False,[]))
                self.assertEqual(result.prompt_version,RULES_VERSION)
                thread.refresh_from_db(); self.assertEqual(thread.text,original)
                response=self.client.get(f'/threads/{thread.pk}/')
                self.assertNotContains(response,'Обнаружен скрытый негатив')
                self.assertNotContains(response,'Возможный скрытый негатив')
                self.assertNotContains(response,'Human Only:')

    def test_wb_pros_cons_use_the_same_semantics(self):
        row={'id':'wb-structured','createdDate':timezone.now().isoformat(),'productDetails':{'nmId':123},'productValuation':5,'text':'Спасибо!','pros':'Стойкость, упаковка','cons':'Не обнаружено'}
        thread,_=ingest(self.account,Wildberries(self.account).parse(row,'review'))
        result=analyze(thread)
        self.assertFalse(result.hidden_negative); self.assertEqual(result.signals,[])
        row['cons']='Нет стойкости'
        thread,changed=ingest(self.account,Wildberries(self.account).parse(row,'review'))
        self.assertTrue(changed)
        self.assertIn('LONG',analyze(thread).signals)

    def test_stale_structured_payload_cannot_override_new_text(self):
        thread,_,_=self.market({'advantages':'Стойкость'})
        thread.text='Нет стойкости';thread.save()
        self.assertIsNone(review_sections(thread))
        self.assertIn('LONG',analyze(thread).signals)

    def test_moving_same_word_from_advantages_to_disadvantages_invalidates_manual_confirmation(self):
        thread,_,row=self.market({'advantages':'стойкость'})
        analyze(thread)
        original=thread.text
        assess_review(thread.pk,self.user,{'decision':'non_negative','signals':[],'severity':'P2','objective_issue':False,'comment':'Решение сотрудника','source_fingerprint':fingerprint(thread),'assessment_version':''})
        row['description']={'disadvantages':'стойкость'}
        thread,changed=ingest(self.ym,YandexMarket(self.ym).parse(row,'review'))
        self.assertEqual(thread.text,original); self.assertTrue(changed)
        self.assertFalse(assessment_is_current(thread))
        assessment=ReviewAssessment.objects.get(thread=thread)
        self.assertEqual((assessment.decision,assessment.comment),('non_negative','Решение сотрудника'))
        self.assertEqual(Audit.objects.filter(action='review.context_changed').count(),1)
        self.assertIn('LONG',analyze(thread).signals)

    def test_metadata_change_keeps_confirmed_decision_and_does_not_reanalyze(self):
        thread,_,row=self.market({'advantages':'стойкость'})
        analyze(thread)
        assess_review(thread.pk,self.user,{'decision':'non_negative','signals':[],'severity':'P2','objective_issue':False,'comment':'Проверено','source_fingerprint':fingerprint(thread),'assessment_version':''})
        row['statistics']['commentsCount']=2;row['needReaction']=False
        thread,changed=ingest(self.ym,YandexMarket(self.ym).parse(row,'review'))
        self.assertFalse(changed);self.assertTrue(assessment_is_current(thread))

    @patch('desk.ai.provider.request_analysis')
    def test_positive_model_topic_is_not_forced_to_hidden_negative(self, request):
        result={'sentiment':'positive','hidden_negative':False,'signals':['LONG'],'severity':'P3','objective_issue':False,'requires_human':False,'confidence':.95,'summary':'Хорошая стойкость','draft':'Спасибо!'}
        request.return_value=result,'openai'
        analysis=analyze(self.thread)
        self.assertFalse(analysis.hidden_negative)
        self.assertEqual(analysis.prompt_version,PROMPT_VERSION)
        self.client.force_login(self.user)
        self.assertEqual(self.client.get('/?negative=1').context['page'].paginator.count,0)

    @patch('desk.ai.provider.requests.post')
    @patch.dict(os.environ,{'CLASSIFICATION_TEST_AI_KEY':'offline-test-only'})
    def test_ai_receives_named_sections_and_explanation(self,post):
        thread,_,_=self.market(SCREENSHOT_CASES[-1])
        self.company.ai_provider='openai';self.company.ai_key_env='CLASSIFICATION_TEST_AI_KEY';self.company.ai_model='test-model';self.company.save()
        post.return_value=Mock(status_code=200)
        result={'sentiment':'positive','hidden_negative':False,'signals':[],'severity':'P3','objective_issue':False,'requires_human':False,'confidence':.95,'summary':'Положительный отзыв','draft':'Спасибо!'}
        post.return_value.json.return_value={'choices':[{'message':{'content':json.dumps(result)}}]}
        analyze(thread)
        messages=post.call_args.kwargs['json']['messages']
        context=json.loads(messages[1]['content'])
        self.assertEqual(context['review_sections']['disadvantages'],SCREENSHOT_CASES[-1]['disadvantages'])
        self.assertIn('a topic word alone never establishes hidden negativity',messages[0]['content'])


class RepairRulesTests(MarketFixture):
    def test_repair_clears_legacy_false_flags_but_preserves_data_history_and_incident(self):
        thread,_,_=self.market(SCREENSHOT_CASES[-1])
        old=self.analysis(thread,provider='rules',confidence=0,signals=['DELIVERY'],hidden_negative=True)
        thread.status='Закрыто';thread.closed_at=timezone.now();thread.save()
        item=Outgoing.objects.create(thread=thread,sender=self.user,request_key=uuid.uuid4(),text='Опубликованный ответ',status='Confirmed')
        case=Incident.objects.create(product=self.product,signal='LONG',severity='P2',status='CONTROL');case.threads.add(thread)
        text=thread.text;source=copy.deepcopy(thread.source_data);message_count=thread.messages.count()
        output=StringIO();call_command('refresh_rules',apply=True,stdout=output)
        old.refresh_from_db();thread.refresh_from_db();item.refresh_from_db();case.refresh_from_db()
        self.assertEqual((old.hidden_negative,old.signals,old.prompt_version),(False,[],RULES_VERSION))
        self.assertEqual((thread.text,thread.source_data,thread.status,thread.messages.count()),(text,source,'Закрыто',message_count))
        self.assertEqual((item.status,case.status,case.threads.count()),('Confirmed','CONTROL',1))
        audit=Audit.objects.get(action='analysis.rules_refresh')
        self.assertTrue(audit.before['hidden_negative']);self.assertFalse(audit.after['hidden_negative'])
        self.assertIn('Сохранено: 1',output.getvalue())
        output=StringIO();call_command('refresh_rules',apply=True,stdout=output)
        self.assertIn('Сохранено: 0',output.getvalue())
        self.assertEqual(Audit.objects.filter(action='analysis.rules_refresh').count(),1)

    def test_preview_and_account_filter_do_not_change_other_accounts(self):
        thread,_,_=self.market(SCREENSHOT_CASES[0]);old=self.analysis(thread,provider='rules',confidence=0)
        output=StringIO();call_command('refresh_rules',stdout=output)
        old.refresh_from_db();self.assertTrue(old.hidden_negative)
        self.assertFalse(Audit.objects.filter(action='analysis.rules_refresh').exists())
        call_command('refresh_rules',apply=True,account=self.account.name,stdout=StringIO())
        old.refresh_from_db();self.assertTrue(old.hidden_negative)
        call_command('refresh_rules',apply=True,account=self.ym.name,stdout=StringIO())
        old.refresh_from_db();self.assertFalse(old.hidden_negative)

    def test_ai_results_and_all_human_decisions_are_preserved(self):
        self.analysis(provider='openai')
        thread,_,_=self.market(SCREENSHOT_CASES[0]);raw=self.analysis(thread,provider='rules',confidence=0)
        ReviewAssessment.objects.create(thread=thread,decision='non_negative',source_fingerprint=fingerprint(thread),reviewed_by=self.user,comment='Ручная проверка')
        call_command('refresh_rules',apply=True,stdout=StringIO())
        raw.refresh_from_db();self.assertTrue(raw.hidden_negative)
        self.assertEqual(ReviewAssessment.objects.get().comment,'Ручная проверка')
        self.assertEqual(Analysis.objects.get(thread=self.thread).provider,'openai')
        self.assertFalse(Audit.objects.filter(action='analysis.rules_refresh').exists())

    def test_real_five_star_complaint_is_not_removed_by_repair(self):
        thread,_,_=self.market({'advantages':'Хороший аромат','disadvantages':'Нет стойкости'})
        old=self.analysis(thread,provider='rules',confidence=0)
        call_command('refresh_rules',apply=True,stdout=StringIO())
        old.refresh_from_db()
        self.assertTrue(old.hidden_negative);self.assertIn('LONG',old.signals)
