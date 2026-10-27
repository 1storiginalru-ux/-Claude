import os
import uuid
from datetime import timedelta
from io import StringIO
from unittest.mock import patch
from django.core.management import call_command
from django.utils import timezone
from desk.integrations.base import IntegrationError
from desk.integrations.coverage import feedback_coverage
from desk.integrations.history import history_need, history_turn, sync_history
from desk.integrations.rate_limit import scope_for
from desk.integrations.wb_sales import WBSales
from desk.integrations.wildberries import Wildberries
from desk.integrations.yandex import YandexMarket
from desk.models import (Analysis, ApiThrottle, HistorySyncState, Message, Outgoing, Product,
                         ProductMapping, SaleEvent, SalesDaily, SalesSyncState, SyncState, Thread)
from desk.voc.rules import review_summary
from .base import DeskCase


class HistoryCase(DeskCase):
    def setUp(self):
        super().setUp()
        # Model field defaults hold the original callable; keep them behind the
        # frozen clock so newly created jobs are due on their first test turn.
        self.now=timezone.now()+timedelta(seconds=1)
        clock=patch('desk.integrations.history.timezone.now', side_effect=lambda:self.now)
        clock.start();self.addCleanup(clock.stop)
        self.account.sales_enabled=True;self.account.save()
        ProductMapping.objects.filter(account=self.account).update(sku='123',external_product_id='123')
        ProductMapping.objects.filter(account=self.ym).delete()
        self.thread.sku='123';self.thread.save()
        self.analysis(sentiment='positive',hidden_negative=False,signals=[])
        SalesSyncState.objects.create(account=self.account,coverage_since=self.now-timedelta(days=7),coverage_until=self.now)
        SyncState.objects.create(account=self.account,coverage_since=self.now-timedelta(days=7),coverage_until=self.now)

    def due(self,kind):
        self.now+=timedelta(seconds=62)
        HistorySyncState.objects.filter(account=self.account,kind=kind).update(next_attempt=self.now-timedelta(seconds=1))

    def sale(self,i,days):
        moment=self.now-timedelta(days=days,seconds=i)
        return {'saleID':f'S-history-{i}','nmId':123,'srid':f'order-{i}','date':moment.isoformat(),'lastChangeDate':moment.isoformat()}

    def feedback(self,i,days,**kwargs):
        row={'id':f'history-review-{i}','createdDate':(self.now-timedelta(days=days)).isoformat(),'text':'Нет стойкости','productValuation':5,'productDetails':{'nmId':123},'userName':'Покупатель'}
        row.update(kwargs);return row

    def prime_sales(self,count=100,days=20):
        rows=[self.sale(i,days) for i in range(count)]
        SaleEvent.objects.bulk_create([SaleEvent(account=self.account,external_id=r['saleID'],sku='123',kind='sale',occurred_at=timezone.datetime.fromisoformat(r['date']),changed_at=timezone.datetime.fromisoformat(r['lastChangeDate'])) for r in rows])
        return rows


class AdaptiveSalesTests(HistoryCase):
    @patch.object(WBSales,'request')
    def test_expands_14_to_30_then_stops_at_100_and_replays_without_duplicates(self,request):
        fresh=self.prime_sales(20,2)
        older=[self.sale(i,10) for i in range(20,60)]
        oldest=[self.sale(i,20) for i in range(60,100)]
        request.side_effect=[sorted(older,key=lambda r:r['lastChangeDate']),[],sorted(fresh+older+oldest,key=lambda r:r['lastChangeDate']),[]]
        self.assertEqual(sync_history(self.account,'sales'),40)
        first=HistorySyncState.objects.get(kind='sales')
        self.assertEqual(first.cursor['days'],14)
        self.assertIsNone(first.coverage_since)
        self.due('sales');sync_history(self.account,'sales')
        self.assertEqual(SaleEvent.objects.count(),60)
        self.due('sales');self.assertEqual(sync_history(self.account,'sales'),100)
        self.due('sales');sync_history(self.account,'sales')
        state=HistorySyncState.objects.get(kind='sales')
        self.assertEqual((state.days,bool(state.cursor)),(30,False))
        self.assertEqual(SaleEvent.objects.count(),100)
        self.assertLessEqual(SalesSyncState.objects.get().coverage_since,self.now-timedelta(days=29))
        self.due('sales');request.reset_mock()
        sync_history(self.account,'sales');request.assert_not_called()
        self.assertIn('100 выкупов доступны',HistorySyncState.objects.get(kind='sales').status)

    @patch.object(WBSales,'request',return_value=[])
    def test_rare_products_stop_at_90_days_without_inventing_sales(self,request):
        depths=[]
        for _ in range(4):
            sync_history(self.account,'sales')
            depths.append(HistorySyncState.objects.get(kind='sales').days)
            self.due('sales')
        self.assertEqual(depths,[14,30,60,90])
        request.reset_mock();sync_history(self.account,'sales');request.assert_not_called()
        state=HistorySyncState.objects.get(kind='sales')
        self.assertTrue(state.exhausted)
        self.assertIn('ещё нет 100',state.status)
        self.assertIsNone(review_summary(self.product)['per100'])

    @patch.object(WBSales,'request')
    def test_imported_100_rows_still_require_verified_period(self,request):
        self.prime_sales(100,20)
        request.return_value=[]
        sync_history(self.account,'sales');self.due('sales')
        sync_history(self.account,'sales')
        self.assertEqual(request.call_count,2)
        self.assertEqual(HistorySyncState.objects.get(kind='sales').days,30)

    @patch.object(WBSales,'request',return_value=[])
    def test_one_shared_report_keeps_loading_for_another_rare_product(self,request):
        self.prime_sales(100,2)
        second=Product.objects.create(company=self.company,code='P2',name='Редкий товар')
        ProductMapping.objects.create(account=self.account,product=second,sku='456',external_product_id='456')
        sync_history(self.account,'sales')
        self.assertEqual(HistorySyncState.objects.get(kind='sales').days,14)
        self.assertEqual(request.call_count,1)
        self.assertNotIn('nmId',request.call_args.kwargs['params'])
        self.assertEqual(SaleEvent.objects.count(),100)

    @patch.object(WBSales,'request')
    def test_accounts_merge_to_100_not_a_quota_of_100_each(self,request):
        self.prime_sales(30,2)
        ProductMapping.objects.create(account=self.ym,product=self.product,sku='ym-product')
        SalesDaily.objects.create(account=self.ym,sku='ym-product',date=timezone.localdate(self.now),redemptions=70)
        SyncState.objects.create(account=self.ym,coverage_since=self.now-timedelta(days=7),coverage_until=self.now)
        sync_history(self.account,'sales');request.assert_not_called()
        self.assertIn('100 выкупов доступны',HistorySyncState.objects.get(kind='sales').status)

    @patch.object(WBSales,'request')
    def test_waits_for_initial_current_report_and_does_not_reset_its_cursor(self,request):
        SalesSyncState.objects.update(coverage_since=None,coverage_until=None,cursor={'fresh':'pending'},next_attempt=self.now+timedelta(hours=2))
        sync_history(self.account,'sales');request.assert_not_called()
        state=SalesSyncState.objects.get()
        self.assertEqual(state.cursor,{'fresh':'pending'})
        self.assertEqual(state.next_attempt,self.now+timedelta(hours=2))

    @patch('desk.integrations.base.requests.request')
    @patch.dict(os.environ,{'TEST_WB_TOKEN':'history-test-only'})
    def test_shares_persistent_wb_budget_and_never_bypasses_a_real_pause(self,request):
        until=self.now+timedelta(hours=2)
        ApiThrottle.objects.create(scope=scope_for('history-test-only','wb-sales'),next_request_at=until,strikes=2)
        SalesSyncState.objects.update(next_attempt=until,cursor={'fresh':'keep'})
        with self.assertRaises(IntegrationError):sync_history(self.account,'sales')
        request.assert_not_called()
        state=HistorySyncState.objects.get(kind='sales')
        self.assertIsNone(state.last_request_at)
        self.assertEqual(state.cursor['days'],14)
        self.assertGreaterEqual(state.next_attempt,until)
        self.assertEqual(SalesSyncState.objects.get().cursor,{'fresh':'keep'})
        self.assertEqual(ApiThrottle.objects.get().next_request_at,until)

    @patch.object(WBSales,'request')
    def test_bad_page_and_stale_lease_do_not_declare_coverage(self,request):
        request.return_value=[self.sale(1,10),{'saleID':'S-invalid'}]
        with self.assertRaises(IntegrationError):sync_history(self.account,'sales')
        self.assertEqual(SaleEvent.objects.count(),0)
        self.assertIsNone(HistorySyncState.objects.get().coverage_since)
        state=HistorySyncState.objects.get()
        state.next_attempt=self.now-timedelta(seconds=1);state.lease_token=uuid.uuid4();state.lease_until=self.now+timedelta(minutes=5);state.save()
        request.reset_mock();sync_history(self.account,'sales');request.assert_not_called()

    @patch.object(WBSales,'request')
    def test_lost_lease_after_http_cannot_insert_a_page_or_advance_coverage(self,request):
        def response(*args,**kwargs):
            HistorySyncState.objects.filter(account=self.account,kind='sales').update(lease_token=uuid.uuid4())
            return [self.sale(1,10)]
        request.side_effect=response
        with self.assertRaises(IntegrationError):sync_history(self.account,'sales')
        self.assertEqual(SaleEvent.objects.count(),0)
        state=HistorySyncState.objects.get(kind='sales')
        self.assertIsNone(state.coverage_since)
        self.assertNotIn('page_from',state.cursor)

    @patch.object(WBSales,'request',return_value=[])
    def test_real_gap_after_outage_is_reloaded_instead_of_hiding_behind_exhausted_flag(self,request):
        HistorySyncState.objects.create(account=self.account,kind='sales',days=90,exhausted=True,coverage_since=self.now-timedelta(days=90),coverage_until=self.now-timedelta(days=20))
        sync_history(self.account,'sales')
        self.assertEqual(HistorySyncState.objects.get(kind='sales').days,14)
        self.assertEqual(request.call_count,1)


class HistoricalReviewTests(HistoryCase):
    def test_disabling_history_never_turns_missing_archive_into_verified_feedback(self):
        self.account.history_enabled=False;self.account.save()
        state=feedback_coverage([self.account.pk])[self.account.pk]
        self.assertIsNone(state.coverage_since)

    def test_review_archive_does_not_claim_full_historical_question_coverage(self):
        self.prime_sales(100,20)
        SalesSyncState.objects.update(coverage_since=self.now-timedelta(days=30))
        with patch.object(Wildberries,'request',return_value={'data':{'feedbacks':[]}}):
            for _ in range(3):
                sync_history(self.account,'reviews');self.due('reviews')
        self.assertLess(HistorySyncState.objects.get().coverage_since,self.now-timedelta(days=19))
        self.assertGreater(SyncState.objects.get().coverage_since,self.now-timedelta(days=8))

    @patch.object(Wildberries,'request')
    def test_all_three_wb_streams_are_required_and_old_feedback_has_full_payload(self,request):
        self.prime_sales(100,20)
        SalesSyncState.objects.update(coverage_since=self.now-timedelta(days=30))
        old=self.feedback(1,15,photoLinks=[{'fullSize':'https://cdn.example.com/history.jpg'}])
        request.side_effect=[{'data':{'feedbacks':[old]}},{'data':{'feedbacks':[]}},{'data':{'feedbacks':[old]}}]
        sync_history(self.account,'reviews')
        self.assertIsNone(HistorySyncState.objects.get(kind='reviews').coverage_since)
        self.assertIsNone(review_summary(self.product)['per100'])
        self.due('reviews');sync_history(self.account,'reviews')
        self.assertIsNone(HistorySyncState.objects.get(kind='reviews').coverage_since)
        self.due('reviews');sync_history(self.account,'reviews')
        state=HistorySyncState.objects.get(kind='reviews')
        self.assertFalse(state.cursor)
        self.assertLess(state.coverage_since,self.now-timedelta(days=19))
        self.assertEqual(request.call_args_list[-1].args[1],'/api/v1/feedbacks/archive')
        self.assertNotIn('dateFrom',request.call_args_list[-1].kwargs['params'])
        thread=Thread.objects.get(external_id=old['id'])
        self.assertEqual(thread.source_data['photoLinks'],old['photoLinks'])
        self.assertEqual(Thread.objects.filter(external_id=old['id']).count(),1)
        self.assertEqual(thread.analysis.confidence,0)
        Analysis.objects.filter(thread=thread).update(provider='test',confidence=.95)
        self.assertEqual(review_summary(self.product)['per100'],1)

    @patch.object(Wildberries,'request')
    def test_archive_stops_at_period_boundary_and_does_not_store_old_or_future_records(self,request):
        state=HistorySyncState.objects.create(account=self.account,kind='reviews',cursor={'since':(self.now-timedelta(days=14)).isoformat(),'until':self.now.isoformat(),'days':14,'stream':2,'skip':0})
        request.return_value={'data':{'feedbacks':[self.feedback(1,10),self.feedback(2,20)]}}
        sync_history(self.account,'reviews')
        self.assertFalse(Thread.objects.filter(external_id='history-review-2').exists())
        self.assertTrue(Thread.objects.filter(external_id='history-review-1').exists())
        self.assertFalse(HistorySyncState.objects.get(pk=state.pk).cursor)

    @patch.object(Wildberries,'request')
    def test_repeated_or_unordered_archive_page_and_missing_dates_never_claim_completion(self,request):
        for rows in [[self.feedback(1,20),self.feedback(2,10)],[self.feedback(3,10,createdDate=None)]]:
            with self.subTest(rows=rows):
                HistorySyncState.objects.update_or_create(account=self.account,kind='reviews',defaults={'cursor':{'since':(self.now-timedelta(days=30)).isoformat(),'until':self.now.isoformat(),'days':30,'stream':2,'skip':0},'next_attempt':self.now-timedelta(seconds=1)})
                request.return_value={'data':{'feedbacks':rows}}
                with self.assertRaises(IntegrationError):sync_history(self.account,'reviews')
                self.assertIsNone(HistorySyncState.objects.get().coverage_since)

    @patch.object(Wildberries,'request')
    def test_older_replay_does_not_overwrite_a_fresh_edit_or_reopen_closed_thread(self,request):
        self.thread.external_id='history-review-9';self.thread.text='Новая версия';self.thread.source_synced_at=self.now;self.thread.status='Закрыто';self.thread.save()
        cursor={'since':(self.now-timedelta(days=30)).isoformat(),'until':(self.now-timedelta(minutes=1)).isoformat(),'days':30,'stream':0,'skip':0}
        HistorySyncState.objects.create(account=self.account,kind='reviews',cursor=cursor)
        request.return_value={'data':{'feedbacks':[self.feedback(9,20)]}}
        sync_history(self.account,'reviews');self.thread.refresh_from_db()
        self.assertEqual((self.thread.text,self.thread.status),('Новая версия','Закрыто'))

    @patch.object(YandexMarket,'request')
    def test_market_loads_same_period_in_pages_including_exact_boundary(self,request):
        ProductMapping.objects.create(account=self.ym,product=self.product,sku='sku1')
        self.prime_sales(100,20)
        SalesSyncState.objects.update(coverage_since=self.now-timedelta(days=30))
        SyncState.objects.create(account=self.ym,coverage_since=self.now-timedelta(days=7),coverage_until=self.now)
        row={'feedbackId':55,'createdAt':(self.now-timedelta(days=20)).isoformat(),'identifiers':{'offerId':'sku1'},'statistics':{'rating':5,'commentsCount':0},'description':{'disadvantages':'Нет стойкости'}}
        request.side_effect=[{'result':{'feedbacks':[row],'paging':{'nextPageToken':'next'}}},{'result':{'feedbacks':[]}}]
        sync_history(self.ym,'reviews')
        state=HistorySyncState.objects.get(account=self.ym,kind='reviews')
        self.assertEqual(state.cursor['page_token'],'next')
        self.now+=timedelta(seconds=62)
        HistorySyncState.objects.filter(pk=state.pk).update(next_attempt=self.now-timedelta(seconds=1))
        sync_history(self.ym,'reviews');state.refresh_from_db()
        self.assertFalse(state.cursor)
        self.assertLess(state.coverage_since,self.now-timedelta(days=19))
        self.assertTrue(Thread.objects.filter(account=self.ym,external_id='55').exists())
        self.assertIn('dateTimeFrom',request.call_args_list[0].kwargs['json'])
        self.assertEqual(request.call_args_list[1].kwargs['params']['page_token'],'next')

    @patch.object(YandexMarket,'request')
    def test_malformed_market_paging_releases_lease_without_claiming_coverage(self,request):
        ProductMapping.objects.create(account=self.ym,product=self.product,sku='sku1')
        self.prime_sales(100,20)
        SyncState.objects.create(account=self.ym,coverage_since=self.now-timedelta(days=7),coverage_until=self.now)
        request.return_value={'result':{'feedbacks':[],'paging':['invalid']}}
        with self.assertRaises(IntegrationError):sync_history(self.ym,'reviews')
        state=HistorySyncState.objects.get(account=self.ym,kind='reviews')
        self.assertIsNone(state.coverage_since)
        self.assertIsNone(state.lease_token)
        self.assertTrue(state.cursor)

    @patch.object(Wildberries,'request')
    def test_api_failure_preserves_position_and_schedules_retry(self,request):
        request.side_effect=IntegrationError('HTTP 429',retryable=True,rate_limited=True,retry_after=900)
        with self.assertRaises(IntegrationError):sync_history(self.account,'reviews')
        state=HistorySyncState.objects.get(kind='reviews')
        self.assertTrue(state.cursor)
        self.assertEqual(state.next_attempt,self.now+timedelta(seconds=900))
        self.assertIsNone(state.coverage_since)


class HistorySchedulingTests(HistoryCase):
    def test_shared_budget_turn_is_bounded_and_outgoing_has_priority(self):
        state=HistorySyncState.objects.create(account=self.account,kind='reviews')
        self.assertTrue(history_turn(self.account,'reviews'))
        state.last_request_at=self.now;state.save()
        self.assertFalse(history_turn(self.account,'reviews'))
        state.last_request_at=None;state.save()
        Outgoing.objects.create(thread=self.thread,sender=self.user,request_key=uuid.uuid4(),text='Приоритетный ответ',status='Queued')
        self.assertFalse(history_turn(self.account,'reviews'))

    @patch.object(WBSales,'request')
    @patch.object(Wildberries,'request')
    def test_demo_disabled_history_or_disabled_account_never_calls_api(self,reviews,sales):
        self.account.history_enabled=False;self.account.save()
        sync_history(self.account,'sales');sync_history(self.account,'reviews')
        self.account.history_enabled=True;self.account.enabled=False;self.account.save()
        sync_history(self.account,'sales');sync_history(self.account,'reviews')
        self.account.enabled=True;self.company.is_demo=True;self.company.save();self.account.company=self.company
        sync_history(self.account,'sales');sync_history(self.account,'reviews')
        reviews.assert_not_called();sales.assert_not_called()

    def test_status_command_and_ui_do_not_send_requests_or_expose_secrets(self):
        HistorySyncState.objects.create(account=self.account,kind='sales',days=30,status='История 30 дней загружена')
        output=StringIO();call_command('history_status',account=self.account.name,stdout=output)
        self.assertIn('30',output.getvalue())
        self.client.force_login(self.user)
        self.assertContains(self.client.get('/integrations/'),'История до 100 выкупов')
        self.assertContains(self.client.get(f'/products/{self.product.pk}/'),'90 дней')

    @patch('desk.management.commands.sync_channels.sync_account')
    @patch('desk.integrations.wb_sales.sync_wb_sales')
    @patch('desk.integrations.history.sync_history')
    def test_polling_reserves_history_slot_without_stopping_other_channel(self,history,sales,reviews):
        self.ym.enabled=False;self.ym.save()
        HistorySyncState.objects.create(account=self.account,kind='sales')
        history.return_value=0
        call_command('sync_channels',stdout=StringIO(),stderr=StringIO())
        sales.assert_not_called()
        reviews.assert_called_once()
        self.assertTrue(any(call.args[1]=='sales' for call in history.call_args_list))
