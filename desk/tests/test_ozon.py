import copy
import os
import uuid
from datetime import timedelta
from io import StringIO
from unittest.mock import Mock, patch
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command, CommandError
from django.utils import timezone
from desk.integrations.base import IntegrationError, get_adapter
from desk.integrations.content import content_view
from desk.integrations.details import enrich_account
from desk.integrations.history import sync_history
from desk.integrations.ozon import Ozon
from desk.integrations.rate_limit import scope_for
from desk.integrations.sync import ingest, ingest_reply, sync_account
from desk.messaging.service import enqueue, process_one
from desk.models import (Account, ApiThrottle, Audit, Company, HistorySyncState, Membership,
                         Message, Outgoing, ProductMapping, SalesDaily, SyncState, Thread)
from desk.products.imports import import_mapping, ImportProblem
from .base import DeskCase


class OzonCase(DeskCase):
    def setUp(self):
        super().setUp()
        self.ozon=Account.objects.create(company=self.company,name='Ozon test',platform='ozon',token_env='TEST_OZON_KEY',ozon_client_id='12345',enabled=True)
        ProductMapping.objects.create(account=self.ozon,product=self.product,sku='123456789',external_product_id='123456789')
        self.adapter=Ozon(self.ozon)
        self.when=(timezone.now()-timedelta(hours=1)).isoformat()

    def row(self,identifier='review-ozon',**extra):
        return {'id':identifier,'sku':123456789,'text':'Отличный аромат','rating':5,
                'published_at':self.when,'photos_amount':0,'videos_amount':0,'comments_amount':0,**extra}

    def ozon_thread(self,kind='review'):
        thread,_=ingest(self.ozon,self.adapter.parse(self.row(),kind))
        return thread


class OzonTransportTests(OzonCase):
    @patch.object(Ozon,'request')
    def test_diagnostics_are_read_only_unless_a_probe_is_requested(self,request):
        output=StringIO()
        call_command('diagnose_ozon',account=self.ozon.name,stdout=output)
        request.assert_not_called();self.assertIn('не отправлялся',output.getvalue())

    @patch.object(Ozon,'request',return_value={'reviews':[],'has_next':False})
    def test_probe_makes_one_bounded_request_without_importing(self,request):
        before=Thread.objects.count();output=StringIO()
        call_command('diagnose_ozon',account=self.ozon.name,probe=True,stdout=output)
        request.assert_called_once();self.assertEqual(request.call_args.kwargs['json']['limit'],20)
        self.assertEqual(Thread.objects.count(),before)
        self.assertFalse(SyncState.objects.exists());self.assertFalse(HistorySyncState.objects.exists())

    @patch('desk.integrations.base.requests.request')
    @patch.dict(os.environ,{'TEST_OZON_KEY':'ozon-test-only-secret'})
    def test_both_headers_are_scoped_and_no_token_is_stored_in_budget(self,request):
        request.return_value=Mock(status_code=200,headers={},content=b'{}')
        request.return_value.json.return_value={'reviews':[],'has_next':False}
        self.adapter.request('POST','/v2/review/list',json={'limit':100})
        self.assertEqual(request.call_args.kwargs['headers'],{'Client-Id':'12345','Api-Key':'ozon-test-only-secret'})
        self.assertEqual(ApiThrottle.objects.get().scope,scope_for('ozon-test-only-secret','ozon-feedbacks:12345'))
        self.assertFalse(request.call_args.kwargs['allow_redirects'])

    @patch('desk.integrations.base.requests.request')
    @patch.dict(os.environ,{'TEST_OZON_KEY':'ozon-test-only-secret'})
    def test_403_is_explicit_without_printing_response_or_credentials(self,request):
        request.return_value=Mock(status_code=403,headers={},text='private response')
        with self.assertRaises(IntegrationError) as error:self.adapter.request('POST','/v2/review/list')
        self.assertIn('Premium Plus',str(error.exception))
        self.assertNotIn('private response',str(error.exception));self.assertNotIn('ozon-test-only-secret',str(error.exception))

    @patch('desk.integrations.base.requests.request')
    @patch.dict(os.environ,{'TEST_OZON_KEY':'ozon-test-only-secret'})
    def test_429_is_shared_with_sender_and_survives_another_adapter(self,request):
        request.return_value=Mock(status_code=429,headers={'Retry-After':'120'})
        with self.assertRaises(IntegrationError):self.adapter.request('POST','/v2/review/list')
        request.reset_mock()
        with self.assertRaises(IntegrationError) as error:Ozon(self.ozon).send(self.ozon_thread(),'Ответ оператором')
        self.assertFalse(error.exception.request_sent);request.assert_not_called()

    @patch('desk.integrations.base.requests.request')
    @patch.dict(os.environ,{'TEST_OZON_KEY':'ozon-test-only-secret'})
    def test_missing_client_id_prevents_http(self,request):
        self.ozon.ozon_client_id=''
        with self.assertRaises(IntegrationError) as error:self.adapter.request('POST','/v2/review/list')
        self.assertFalse(error.exception.request_sent);request.assert_not_called()


class OzonReadTests(OzonCase):
    @patch.object(Ozon,'request')
    def test_current_pages_include_processed_reviews_and_questions_without_duplicates(self,request):
        request.side_effect=[{'reviews':[self.row()],'has_next':True,'last_id':'page2'},
                             {'reviews':[],'has_next':False},
                             {'questions':[self.row('question-1',author_name='Покупатель')],'has_next':False}]
        self.adapter.checkpoint=lambda cursor:setattr(self.adapter,'sync_cursor',copy.deepcopy(cursor))
        rows=list(self.adapter.fetch())
        for incoming in rows:ingest(self.ozon,incoming);ingest(self.ozon,incoming)
        self.assertEqual(Thread.objects.filter(account=self.ozon).count(),2)
        self.assertEqual(self.adapter.sync_cursor,{})
        self.assertEqual(request.call_args_list[0].kwargs['json']['filters']['status'],'ALL')
        self.assertEqual(request.call_args_list[1].kwargs['json']['last_id'],'page2')
        self.assertIn('date_from',request.call_args_list[2].kwargs['json']['filter'])
        self.assertTrue(hasattr(self.adapter,'completed_window'))

    @patch.object(Ozon,'request')
    def test_list_media_are_loaded_from_info_and_source_fields_are_preserved(self,request):
        row=self.row(photos_amount=1,videos_amount=1)
        detail={**row,'photos':[{'url':'https://cdn.example.com/photo.jpg'}],'videos':[{'url':'https://cdn.example.com/video.mp4','preview_url':'https://cdn.example.com/preview.jpg'}],'unknown_field':{'retained':True}}
        request.side_effect=[{'reviews':[row],'has_next':False},detail]
        now=timezone.now();rows,_=self.adapter.page('review',now-timedelta(days=7),now)
        thread,_=ingest(self.ozon,self.adapter.parse(rows[0],'review'))
        self.assertEqual(content_view(thread)['photos'][0]['url'],detail['photos'][0]['url'])
        self.assertEqual(content_view(thread)['videos'][0]['preview'],detail['videos'][0]['preview_url'])
        self.assertEqual(thread.source_data['unknown_field'],{'retained':True})
        self.assertEqual(request.call_args.args[1],'/v2/review/info')

    @patch.object(Ozon,'request')
    def test_bad_page_never_claims_full_coverage(self,request):
        request.return_value={'reviews':[self.row(),self.row('bad',published_at=None)],'has_next':False}
        with self.assertRaises(IntegrationError):list(self.adapter.fetch())
        self.assertFalse(hasattr(self.adapter,'completed_window'))

    @patch.object(Ozon,'request')
    def test_older_and_future_rows_stay_outside_current_window(self,request):
        now=timezone.now()
        request.side_effect=[{'reviews':[self.row('old',published_at=(now-timedelta(days=9)).isoformat()),self.row('future',published_at=(now+timedelta(days=1)).isoformat())],'has_next':False},{'questions':[],'last_id':''}]
        self.assertEqual(list(self.adapter.fetch()),[])

    @patch.object(Ozon,'request')
    def test_repeated_cursor_is_not_treated_as_completion(self,request):
        request.return_value={'reviews':[self.row()],'has_next':True,'last_id':'same'}
        now=timezone.now()
        with self.assertRaises(IntegrationError):self.adapter.page('review',now-timedelta(days=7),now,{'last_id':'same'})

    @patch.object(Ozon,'request')
    def test_resume_after_429_keeps_fixed_upper_boundary(self,request):
        saved=[];self.adapter.checkpoint=lambda cursor:saved.append(copy.deepcopy(cursor))
        request.side_effect=[{'reviews':[self.row()],'has_next':True,'last_id':'next'},IntegrationError('429',retry_after=60,retryable=True)]
        with self.assertRaises(IntegrationError):list(self.adapter.fetch())
        self.adapter.sync_cursor=saved[-1]
        fixed_until=saved[-1]['until']
        request.side_effect=[{'reviews':[],'has_next':False},{'questions':[],'has_next':False}]
        self.assertEqual(list(self.adapter.fetch()),[])
        self.assertEqual(request.call_args_list[-2].kwargs['json']['last_id'],'next')
        self.assertEqual(self.adapter.completed_window[1].isoformat(),fixed_until)

    @patch.object(Ozon,'request')
    def test_details_load_media_and_paginated_history_for_current_review(self,request):
        thread=self.ozon_thread();Thread.objects.filter(pk=thread.pk).update(source_synced_at=None)
        comment={'id':'comment-1','text':'Спасибо','published_at':self.when,'is_owner':True,'is_published':True}
        request.side_effect=[self.row(photos=[{'url':'https://cdn.example.com/a.jpg'}]),{'comments':[comment]}]
        self.assertEqual(enrich_account(self.ozon,self.adapter),1)
        thread.refresh_from_db();self.assertTrue(thread.history_synced_at)
        self.assertEqual(thread.messages.filter(direction='out').count(),1)

    @patch.object(Ozon,'request')
    def test_history_uses_same_product_sales_period(self,request):
        now=timezone.now()
        ProductMapping.objects.filter(account__in=[self.account,self.ym]).delete()
        SalesDaily.objects.create(account=self.ozon,sku='123456789',date=timezone.localdate(now-timedelta(days=20)),redemptions=100)
        SyncState.objects.create(account=self.ozon,coverage_since=now-timedelta(days=7),coverage_until=now)
        request.return_value={'reviews':[self.row('historical',published_at=(now-timedelta(days=15)).isoformat())],'has_next':False}
        sync_history(self.ozon,'reviews')
        state=HistorySyncState.objects.get(account=self.ozon)
        self.assertFalse(state.cursor);self.assertLess(state.coverage_since,now-timedelta(days=19))
        self.assertTrue(Thread.objects.filter(account=self.ozon,external_id='historical').exists())
        self.assertLess(timezone.datetime.fromisoformat(request.call_args.kwargs['json']['filters']['published_from']),now-timedelta(days=19))


class OzonSendTests(OzonCase):
    @patch.object(Ozon,'request')
    def test_send_then_confirm_only_published_owned_review_comment(self,request):
        thread=self.ozon_thread()
        queued=enqueue(thread,self.user,'Спасибо за отзыв!',uuid.uuid4())
        request.return_value={'comment_id':'our-comment'}
        self.assertTrue(process_one());queued.refresh_from_db();self.assertEqual(queued.status,'Sent')
        body=request.call_args.kwargs['json'];self.assertFalse(body['mark_review_as_processed'])
        self.assertTrue(request.call_args.kwargs['writing'])
        for published,expected in [(False,'Sent'),(True,'Confirmed')]:
            reply=self.adapter.parse_reply({'id':'our-comment','text':queued.text,'is_owner':True,'is_published':published,'published_at':self.when},thread)
            ingest_reply(thread,reply);queued.refresh_from_db();self.assertEqual(queued.status,expected)

    def test_official_ozon_comment_is_not_our_company_answer(self):
        thread=self.ozon_thread()
        queued=Outgoing.objects.create(thread=thread,sender=self.user,request_key=uuid.uuid4(),text='Совпадение',status='Sent',external_id='official-comment')
        reply=self.adapter.parse_reply({'id':'official-comment','text':'Совпадение','is_owner':False,'is_official':True,'is_published':True},thread)
        ingest_reply(thread,reply);queued.refresh_from_db();self.assertEqual(queued.status,'Sent')

    @patch.object(Ozon,'request')
    def test_question_answer_uses_sku_and_our_id_for_confirmation(self,request):
        thread=self.ozon_thread('question')
        queued=enqueue(thread,self.user,'Объём 100 мл',uuid.uuid4())
        request.return_value={'answer_id':'answer-ours'};process_one();queued.refresh_from_db()
        self.assertEqual(request.call_args.kwargs['json']['sku'],123456789)
        reply=self.adapter.parse_reply({'id':'answer-ours','text':queued.text,'status_publication':'PUBLISHED','published_at':self.when},thread)
        ingest_reply(thread,reply);queued.refresh_from_db();self.assertEqual(queued.status,'Confirmed')

    def test_other_question_answer_never_reopens_closed_case(self):
        thread=self.ozon_thread('question');thread.status='Закрыто';thread.save()
        reply=self.adapter.parse_reply({'id':'someone-else','text':'Чужой ответ','author_name':'Другой участник','status_publication':'PUBLISHED','published_at':self.when},thread)
        ingest_reply(thread,reply);thread.refresh_from_db()
        self.assertEqual(thread.status,'Закрыто');self.assertFalse(reply['owned'])
        self.assertEqual(thread.messages.last().source_author,'Другой участник')

    @patch.object(Ozon,'request')
    def test_missing_created_id_is_ambiguous_and_is_not_automatically_retried(self,request):
        item=enqueue(self.ozon_thread(),self.user,'Спасибо за отзыв',uuid.uuid4())
        request.return_value={};process_one();item.refresh_from_db()
        self.assertEqual(item.status,'Failed');self.assertFalse(item.retry_safe)
        self.assertFalse(process_one());self.assertEqual(request.call_count,1)

    def test_moderation_rejection_is_visible_and_does_not_confirm(self):
        thread=self.ozon_thread()
        item=Outgoing.objects.create(thread=thread,sender=self.user,text='Ответ',request_key=uuid.uuid4(),status='Sent',external_id='rejected')
        reply=self.adapter.parse_reply({'id':'rejected','text':'Ответ','is_owner':True,'is_rejected':True,'is_published':False},thread)
        ingest_reply(thread,reply);item.refresh_from_db()
        self.assertEqual(item.status,'Failed');self.assertIsNone(item.confirmed_at)
        self.assertIn('модерацией',item.error)

    @patch.object(Ozon,'request')
    def test_length_is_validated_before_enqueue_or_http(self,request):
        thread=self.ozon_thread()
        for text in ['x','x'*3001]:
            with self.assertRaises(ValueError):enqueue(thread,self.user,text,uuid.uuid4())
            with self.assertRaises(IntegrationError):self.adapter.send(thread,text)
        request.assert_not_called()

    @patch.object(Ozon,'request')
    def test_comments_resume_with_offset_and_never_confirm_awaiting_moderation(self,request):
        thread=self.ozon_thread();saved=[]
        rows=[{'id':str(i),'text':'Ответ','is_owner':True,'is_published':False} for i in range(100)]
        request.side_effect=[{'comments':rows},IntegrationError('429',retryable=True,retry_after=60)]
        with self.assertRaises(IntegrationError):list(self.adapter.history(thread,checkpoint=lambda c:saved.append(copy.deepcopy(c))))
        self.assertEqual(saved[-1]['offset'],100)
        request.side_effect=[{'comments':[]}]
        self.assertEqual(list(self.adapter.history(thread,cursor=saved[-1])),[])
        self.assertEqual(request.call_args.kwargs['json']['offset'],100)


class CorporateAccountsTests(DeskCase):
    def test_preview_is_read_only_and_apply_is_idempotent_preserving_existing_accounts(self):
        before=list(Account.objects.values())
        call_command('provision_accounts',stdout=StringIO())
        self.assertEqual(list(Account.objects.values()),before)
        call_command('provision_accounts',apply=True,stdout=StringIO())
        new=Account.objects.exclude(pk__in=[a['id'] for a in before])
        self.assertEqual(new.count(),8);self.assertFalse(new.filter(enabled=True).exists())
        self.assertEqual(Company.objects.filter(name__in=['ЕС','ОП','Velato']).count(),3)
        self.assertEqual(set(new.filter(platform='ozon').values_list('name',flat=True)),{'Ozon_2','Ozon_Velato'})
        snapshots=list(Account.objects.values());audits=Audit.objects.count()
        call_command('provision_accounts',apply=True,stdout=StringIO())
        self.assertEqual(list(Account.objects.values()),snapshots);self.assertEqual(Audit.objects.count(),audits)
        self.assertEqual(Membership.objects.count(),1)

    def test_conflicting_existing_account_rolls_back_new_companies(self):
        Account.objects.create(company=self.other,name='ВБ_3',platform='wb',token_env='OTHER_TOKEN')
        before=list(Company.objects.values())
        with self.assertRaises(CommandError):call_command('provision_accounts',apply=True,stdout=StringIO())
        self.assertEqual(list(Company.objects.values()),before)
        self.assertFalse(Account.objects.filter(name='ВБ_2').exists())

    def test_configure_requires_key_and_correct_platform_identity(self):
        call_command('provision_accounts',apply=True,stdout=StringIO())
        with self.assertRaises(CommandError):call_command('configure_account',account='Ozon_2',enable=True,client_id='100',stdout=StringIO())
        account=Account.objects.get(name='Ozon_2');self.assertFalse(account.enabled);self.assertEqual(account.ozon_client_id,'')
        with patch.dict(os.environ,{'VOC_OZON_2_TOKEN':'test-only'}):
            call_command('configure_account',account='Ozon_2',enable=True,client_id='100',stdout=StringIO())
        account.refresh_from_db();self.assertTrue(account.enabled);self.assertEqual(account.ozon_client_id,'100')
        with self.assertRaises(CommandError):call_command('configure_account',account='ВБ_2',business_id='100',stdout=StringIO())

    def test_model_requires_client_id_to_enable_ozon(self):
        account=Account(company=self.company,name='Ozon',platform='ozon',token_env='VOC_OZON_TOKEN',enabled=True)
        with self.assertRaises(ValidationError):account.full_clean()


class OzonPresentationTests(OzonCase):
    def test_channel_filter_and_company_scope(self):
        thread=self.ozon_thread()
        self.client.force_login(self.user)
        response=self.client.get('/?platform=ozon')
        self.assertContains(response,'Ozon');self.assertContains(response,thread.text)
        response=self.client.get('/integrations/')
        self.assertContains(response,'Client-Id');self.assertContains(response,'Premium Plus')
        self.assertNotContains(response,self.other_account.name)

    def test_unsafe_media_urls_and_html_are_not_rendered_as_active_content(self):
        thread,_=ingest(self.ozon,self.adapter.parse(self.row(text='<script>evil()</script>',photos=[{'url':'javascript:alert(1)'},{'url':'https://127.0.0.1/photo'}],videos=[{'url':'http://unsafe.example/a'}]),'review'))
        self.assertEqual(content_view(thread)['photos'],[]);self.assertEqual(content_view(thread)['videos'],[])
        self.client.force_login(self.user)
        self.assertNotContains(self.client.get(f'/threads/{thread.pk}/'),'<script>evil()</script>')

    def test_import_ozon_mapping_uses_numeric_market_sku_not_seller_offer_id(self):
        header='ProductID,Product Name,Brand,Volume,Concentration,Platform,Account,Legal Entity,SKU,External Product ID\n'
        data=header+'P1,Парфюм,Brand,100,EDT,Ozon,Ozon test,ООО A,987654,987654\n'
        import_mapping(self.company,SimpleUploadedFile('mapping.csv',data.encode()),self.user)
        self.assertTrue(ProductMapping.objects.filter(account=self.ozon,sku='987654').exists())
        bad=data.replace('987654','seller-article')
        with self.assertRaises(ImportProblem):import_mapping(self.company,SimpleUploadedFile('mapping.csv',bad.encode()),self.user)
