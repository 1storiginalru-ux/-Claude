import copy
import os
import uuid
from datetime import timedelta
from io import StringIO
from unittest.mock import Mock,patch
from django.core.management import call_command,CommandError
from django.core.exceptions import ValidationError
from django.utils import timezone
from desk.integrations.base import IntegrationError
from desk.integrations.history import sync_history
from desk.integrations.ym_sales import YandexSales,plan_order,save_order,sync_ym_sales
from desk.integrations.rate_limit import scope_for
from desk.models import (Account,ApiThrottle,Audit,Company,HistorySyncState,ProductMapping,
                         SaleEvent,SalesDaily,SalesSyncState,SyncState,YandexOrderState)
from desk.voc.dataset import ControlDataset
from desk.voc.rules import review_summary
from .base import DeskCase


class MarketSalesCase(DeskCase):
    def setUp(self):
        super().setUp()
        self.now=timezone.now()+timedelta(seconds=1)
        clock=patch('desk.integrations.ym_sales.timezone.now',side_effect=lambda:self.now)
        clock.start();self.addCleanup(clock.stop)
        self.ym.sales_enabled=True;self.ym.save()
        ProductMapping.objects.filter(account=self.account).delete()
        self.thread.received_at=self.now-timedelta(days=200);self.thread.save()

    def order(self,number=1,count=3,status='DELIVERED',**extras):
        row={'orderId':number,'campaignId':10,'creationDate':(self.now-timedelta(days=3)).isoformat(),
             'updateDate':(self.now-timedelta(hours=1)).isoformat(),'status':status,'fake':False,
             'items':[{'id':100,'offerId':'sku1','count':count}]}
        row.update(extras);return row

    def stats(self,number=1,status='DELIVERED',**extras):
        return {'id':number,'status':status,'statusUpdateDate':(self.now-timedelta(days=1)).isoformat(),'fake':False,**extras}

    def responses(self,orders,stats,token=None):
        return [{'orders':orders,**({'paging':{'nextPageToken':token}}if token else{})},
                {'status':'OK','result':{'orders':stats}}]

    def due(self,kind='current'):
        self.now+=timedelta(seconds=62)
        model=SalesSyncState if kind=='current' else HistorySyncState
        model.objects.filter(account=self.ym).update(next_attempt=self.now-timedelta(seconds=1))

    def store(self,row,stats):return save_order(self.ym,plan_order(row,stats,self.now))

    def coverage(self,days=30):
        SalesSyncState.objects.update_or_create(account=self.ym,defaults={'coverage_since':self.now-timedelta(days=days),'coverage_until':self.now})
        SyncState.objects.update_or_create(account=self.ym,defaults={'coverage_since':self.now-timedelta(days=days),'coverage_until':self.now})


class MarketSalesUnitTests(MarketSalesCase):
    def test_count_is_units_not_orders_and_replay_is_idempotent(self):
        self.store(self.order(count=3),self.stats())
        self.assertEqual(SaleEvent.objects.filter(kind='sale').count(),3)
        self.assertEqual({e.sku for e in SaleEvent.objects.all()},{'sku1'})
        self.store(self.order(count=3),self.stats())
        self.assertEqual(SaleEvent.objects.count(),3);self.assertEqual(Audit.objects.filter(action='ym.sales.snapshot').count(),1)

    def test_two_lines_of_same_sku_have_independent_unit_ids(self):
        row=self.order(count=2);row['items'].append({'id':101,'offerId':'sku1','count':3})
        self.store(row,self.stats());self.assertEqual(SaleEvent.objects.count(),5)

    def test_cancellation_and_reduced_quantity_replace_old_events(self):
        row=self.order(count=3);self.store(row,self.stats())
        row['items'][0]['count']=2;row['updateDate']=self.now.isoformat()
        self.store(row,self.stats());self.assertEqual(SaleEvent.objects.count(),2)
        row['status']='CANCELLED';self.store(row,None)
        self.assertFalse(SaleEvent.objects.exists())

    def test_older_order_version_cannot_restore_cancelled_sale(self):
        old=self.order();self.store(old,self.stats())
        changed=self.order(status='CANCELLED',updateDate=self.now.isoformat());self.store(changed,None)
        self.store(old,self.stats());self.assertFalse(SaleEvent.objects.exists())

    def test_unknown_new_order_status_preserves_proven_sales_and_blocks_trust(self):
        row=self.order();self.store(row,self.stats())
        row['status']='UNKNOWN_NEW';row['updateDate']=self.now.isoformat()
        self.store(row,self.stats(status='UNKNOWN'))
        self.assertEqual(SaleEvent.objects.count(),3)
        self.assertTrue(YandexOrderState.objects.get().issues)

    def test_pickup_processing_and_fake_orders_are_not_redemptions(self):
        for number,status in enumerate(['PICKUP','DELIVERY','PROCESSING','UNPAID','CANCELLED'],1):
            self.store(self.order(number,status=status),None)
        self.store(self.order(100,fake=True),None)
        self.assertFalse(SaleEvent.objects.exists())

    def test_unit_statuses_exclude_rejected_cancelled_and_lost_units(self):
        row=self.order(count=8)
        row['items'][0]['itemStatuses']=[{'status':'DELIVERED_TO_BUYER','count':3},{'status':'REJECTED','count':2},{'status':'CANCELLED','count':2},{'status':'LOST','count':1}]
        self.store(row,self.stats());self.assertEqual(SaleEvent.objects.filter(kind='sale').count(),3)

    def test_returns_keep_original_sale_dates_and_add_separate_units(self):
        row=self.order(count=3);self.store(row,self.stats())
        original=list(SaleEvent.objects.order_by('external_id').values_list('occurred_at',flat=True))
        row['status']='PARTIALLY_RETURNED';row['updateDate']=self.now.isoformat()
        row['items'][0]['itemStatuses']=[{'status':'DELIVERED_TO_BUYER','count':2},{'status':'RETURNED','count':1}]
        self.store(row,self.stats(status='PARTIALLY_RETURNED',statusUpdateDate=self.now.isoformat()))
        self.assertEqual(SaleEvent.objects.filter(kind='sale').count(),3)
        self.assertEqual(SaleEvent.objects.filter(kind='return').count(),1)
        self.assertEqual(list(SaleEvent.objects.filter(kind='sale').order_by('external_id').values_list('occurred_at',flat=True)),original)
        self.assertFalse(YandexOrderState.objects.get().issues)

    def test_backfilled_return_never_fabricates_original_sale_date(self):
        row=self.order(count=2,status='RETURNED')
        self.store(row,self.stats(status='RETURNED'))
        self.assertFalse(SaleEvent.objects.filter(kind='sale').exists())
        self.assertEqual(SaleEvent.objects.filter(kind='return').count(),2)
        self.assertEqual(YandexOrderState.objects.get().issues[0]['count'],2)

    def test_return_can_use_proven_delivery_date_still_present_in_stats(self):
        self.store(self.order(count=2,status='RETURNED'),self.stats())
        self.assertEqual(SaleEvent.objects.filter(kind='sale').count(),2)
        self.assertEqual(SaleEvent.objects.filter(kind='return').count(),2)

    def test_partial_return_without_unit_counts_is_explicitly_unknown(self):
        row=self.order(count=3);self.store(row,self.stats())
        row['status']='PARTIALLY_RETURNED';row['updateDate']=self.now.isoformat()
        self.store(row,self.stats(status='PARTIALLY_RETURNED'))
        self.assertEqual(SaleEvent.objects.filter(kind='sale').count(),3)
        self.assertIsNone(YandexOrderState.objects.get().issues[0]['count'])

    def test_stats_lag_retries_and_unknown_units_are_not_trusted(self):
        with self.assertRaises(IntegrationError) as error:plan_order(self.order(),self.stats(status='PICKUP'),self.now)
        self.assertTrue(error.exception.retryable)
        row=self.order();row['items'][0]['itemStatuses']=[{'status':'NEW_UNKNOWN','count':3}]
        with self.assertRaises(IntegrationError):plan_order(row,self.stats(),self.now)

    def test_source_keeps_no_buyer_addresses_or_notes(self):
        row=self.order(delivery={'address':'private'},buyer={'name':'private'},notes='private')
        self.store(row,self.stats())
        self.assertNotIn('private',str(YandexOrderState.objects.get().source_data))
        self.assertNotIn('private',str(SaleEvent.objects.first().source_data))


class MarketSalesPollingTests(MarketSalesCase):
    @patch.object(YandexSales,'request')
    def test_diagnostic_reads_without_import_or_moving_a_cursor(self,request):
        call_command('diagnose_ym_sales',account=self.ym.name,stdout=StringIO());request.assert_not_called()
        request.side_effect=self.responses([self.order()],[self.stats()])
        output=StringIO();call_command('diagnose_ym_sales',account=self.ym.name,probe=True,stdout=output)
        self.assertIn('статистики доступен',output.getvalue())
        self.assertEqual(request.call_count,2)
        self.assertFalse(SaleEvent.objects.exists());self.assertFalse(SalesSyncState.objects.exists())

    @patch('desk.management.commands.sync_channels.sync_account')
    @patch('desk.integrations.ym_sales.sync_ym_sales',return_value=0)
    @patch('desk.integrations.wb_sales.sync_wb_sales',return_value=0)
    @patch('desk.integrations.history.sync_history',return_value=0)
    def test_polling_dispatches_market_and_leaves_feedback_independent(self,history,wb,sales,feedback):
        self.account.enabled=False;self.account.save()
        call_command('sync_channels',stdout=StringIO(),stderr=StringIO())
        sales.assert_called_once();wb.assert_not_called()
        self.assertTrue(any(call.args[0].pk==self.ym.pk for call in feedback.call_args_list))

    @patch.object(YandexSales,'request')
    def test_business_api_and_stats_ids_and_completed_coverage(self,request):
        request.side_effect=self.responses([self.order()],[self.stats()])
        self.assertEqual(sync_ym_sales(self.ym),3)
        self.assertEqual(request.call_args_list[0].args[1],'/v1/businesses/123/orders')
        self.assertFalse(request.call_args_list[0].kwargs['json']['fake'])
        self.assertEqual(request.call_args_list[1].args[1],'/v2/campaigns/10/stats/orders')
        self.assertEqual(request.call_args_list[1].kwargs['json'],{'orders':[1]})
        state=SalesSyncState.objects.get();self.assertFalse(state.cursor)
        self.assertEqual(state.coverage_until,self.now);self.assertIsNone(state.lease_token)

    @patch.object(YandexSales,'request')
    def test_next_page_and_429_resume_same_snapshot(self,request):
        request.side_effect=self.responses([self.order()],[self.stats()],token='next')
        sync_ym_sales(self.ym);state=SalesSyncState.objects.get();cursor=copy.deepcopy(state.cursor)
        self.assertIsNone(state.coverage_until);self.assertEqual(cursor['page_token'],'next')
        self.due();request.side_effect=IntegrationError('420',retry_after=300,retryable=True)
        with self.assertRaises(IntegrationError):sync_ym_sales(self.ym)
        state.refresh_from_db();self.assertEqual(state.cursor,cursor)
        self.due();request.side_effect=[{'orders':[]}]
        sync_ym_sales(self.ym);state.refresh_from_db()
        self.assertEqual(state.coverage_until,timezone.datetime.fromisoformat(cursor['until']))
        self.assertEqual(request.call_args.kwargs['params']['page_token'],'next')

    @patch.object(YandexSales,'request')
    def test_bad_second_order_does_not_partially_commit_first(self,request):
        invalid=self.order(2);invalid['items'][0]['count']=-1
        request.side_effect=self.responses([self.order(),invalid],[self.stats(),self.stats(2)])
        with self.assertRaises(IntegrationError):sync_ym_sales(self.ym)
        self.assertFalse(SaleEvent.objects.exists());self.assertFalse(YandexOrderState.objects.exists())
        self.assertIsNone(SalesSyncState.objects.get().coverage_until)

    @patch.object(YandexSales,'request')
    def test_missing_stats_never_marks_period_complete(self,request):
        request.side_effect=self.responses([self.order()],[])
        with self.assertRaises(IntegrationError):sync_ym_sales(self.ym)
        self.assertIsNone(SalesSyncState.objects.get().coverage_until)

    @patch.object(YandexSales,'request')
    def test_repeated_token_and_lost_lease_do_not_advance(self,request):
        state=SalesSyncState.objects.create(account=self.ym,cursor={'ym_version':1,'since':(self.now-timedelta(days=7)).isoformat(),'until':self.now.isoformat(),'page_token':'same'})
        request.return_value={'orders':[self.order()],'paging':{'nextPageToken':'same'}}
        with self.assertRaises(IntegrationError):sync_ym_sales(self.ym)
        self.assertFalse(SaleEvent.objects.exists())
        self.due()
        def response(*args,**kwargs):
            SalesSyncState.objects.filter(pk=state.pk).update(lease_token=uuid.uuid4());return {'orders':[]}
        request.side_effect=response
        with self.assertRaises(IntegrationError):sync_ym_sales(self.ym)
        state.refresh_from_db();self.assertIsNone(state.coverage_until)

    @patch.object(YandexSales,'request')
    def test_demo_disabled_and_sales_off_do_not_call_api(self,request):
        self.ym.sales_enabled=False
        with self.assertRaises(IntegrationError):sync_ym_sales(self.ym)
        self.ym.sales_enabled=True;self.ym.enabled=False
        with self.assertRaises(IntegrationError):sync_ym_sales(self.ym)
        self.ym.enabled=True;self.company.is_demo=True;self.company.save();self.ym.company=self.company
        with self.assertRaises(IntegrationError):sync_ym_sales(self.ym)
        request.assert_not_called()

    @patch.object(YandexSales,'request',return_value={'orders':[]})
    def test_history_splits_90_days_into_at_most_30_day_slices(self,request):
        self.coverage(7)
        state=HistorySyncState.objects.create(account=self.ym,kind='sales',cursor={'since':(self.now-timedelta(days=90)).isoformat(),'until':self.now.isoformat(),'days':90})
        for _ in range(3):sync_history(self.ym,'sales');self.due('history')
        state.refresh_from_db();self.assertFalse(state.cursor);self.assertEqual(state.days,90)
        self.assertEqual(request.call_count,3)
        for call in request.call_args_list:
            dates=call.kwargs['json']['dates']
            self.assertLessEqual(timezone.datetime.fromisoformat(dates['updateDateTo'])-timezone.datetime.fromisoformat(dates['updateDateFrom']),timedelta(days=30))

    @patch.object(YandexSales,'request',return_value={'orders':[]})
    def test_history_expands_14_then_30_for_rare_product(self,request):
        self.coverage(7)
        sync_history(self.ym,'sales');self.assertEqual(HistorySyncState.objects.get().days,14)
        self.due('history');sync_history(self.ym,'sales');self.assertEqual(HistorySyncState.objects.get().days,30)

    @patch('desk.integrations.base.requests.request')
    @patch.dict(os.environ,{'TEST_YM_TOKEN':'market-sales-test-only'})
    def test_403_explains_permissions_without_exposing_secret(self,request):
        request.return_value=Mock(status_code=403,headers={},text='secret body')
        with self.assertRaises(IntegrationError) as error:sync_ym_sales(self.ym)
        self.assertIn('чтения заказов',str(error.exception));self.assertNotIn('secret',str(error.exception))
        self.assertIsNone(SalesSyncState.objects.get().coverage_until)

    @patch('desk.integrations.base.requests.request')
    @patch.dict(os.environ,{'TEST_YM_TOKEN':'market-sales-test-only'})
    def test_throttle_is_persistent_and_separate_from_feedback_and_wb(self,request):
        ApiThrottle.objects.create(scope=scope_for('market-sales-test-only','ym-sales'),next_request_at=self.now+timedelta(minutes=10))
        with self.assertRaises(IntegrationError) as error:sync_ym_sales(self.ym)
        self.assertFalse(error.exception.request_sent);request.assert_not_called()
        self.assertGreater(SalesSyncState.objects.get().next_attempt,self.now+timedelta(minutes=9))

    def test_duplicate_business_id_is_blocked_before_network(self):
        Account.objects.create(company=self.other,name='Mirror',platform='ym',token_env='UNSET',business_id=self.ym.business_id,sales_enabled=True)
        with self.assertRaises(ValidationError):self.ym.full_clean()
        with self.assertRaises(IntegrationError):sync_ym_sales(self.ym)


class MarketSalesMetricTests(MarketSalesCase):
    def test_wb_40_market_60_combine_to_one_exact_100_with_no_csv_double_count(self):
        self.coverage()
        self.account.sales_enabled=True;self.account.save()
        ProductMapping.objects.create(account=self.account,product=self.product,sku='123',external_product_id='123')
        SaleEvent.objects.bulk_create([SaleEvent(account=self.account,external_id=f'S-{i}',sku='123',kind='sale',occurred_at=self.now-timedelta(days=2,seconds=i),changed_at=self.now)for i in range(40)])
        SalesSyncState.objects.create(account=self.account,coverage_since=self.now-timedelta(days=30),coverage_until=self.now)
        SyncState.objects.create(account=self.account,coverage_since=self.now-timedelta(days=30),coverage_until=self.now)
        HistorySyncState.objects.create(account=self.account,kind='reviews',coverage_since=self.now-timedelta(days=30),coverage_until=self.now)
        self.store(self.order(count=60),self.stats())
        SalesDaily.objects.create(account=self.ym,sku='sku1',date=timezone.localdate(self.now),redemptions=999)
        summary=review_summary(self.product,now=self.now)
        self.assertEqual(summary['period']['redemptions'],100);self.assertTrue(summary['period']['exact'])
        self.assertEqual(summary['per100'],0)
        dataset=ControlDataset([self.product],Company.objects.filter(pk=self.company.pk),now=self.now)
        self.assertEqual(dataset.period(self.product.code),summary['period'])

    def test_undated_returned_sale_blocks_rate_only_for_relevant_sku_and_interval(self):
        self.coverage();self.store(self.order(count=100),self.stats())
        row=self.order(2,count=1,status='RETURNED')
        self.store(row,self.stats(2,status='RETURNED'))
        summary=review_summary(self.product,now=self.now)
        self.assertEqual(summary['period']['redemptions'],100)
        self.assertTrue(summary['period']['window_ambiguous']);self.assertIsNone(summary['per100']);self.assertIsNone(summary['review_rate'])
        self.client.force_login(self.user)
        self.assertContains(self.client.get(f'/products/{self.product.pk}/'),'Неподтверждённые выкупы Маркета')
        dataset=ControlDataset([self.product],Company.objects.filter(pk=self.company.pk),now=self.now)
        self.assertTrue(dataset.period(self.product.code)['window_ambiguous'])
        state=YandexOrderState.objects.get(order_id='2');state.issues[0]['sku']='other-sku';state.save()
        self.assertEqual(review_summary(self.product,now=self.now)['per100'],0)

    def test_undated_sale_before_oldest_100_does_not_block_new_full_window(self):
        self.coverage();self.store(self.order(count=100),self.stats())
        row=self.order(2,count=1,status='RETURNED',creationDate=(self.now-timedelta(days=20)).isoformat(),updateDate=(self.now-timedelta(days=10)).isoformat())
        self.store(row,self.stats(2,status='RETURNED',statusUpdateDate=(self.now-timedelta(days=10)).isoformat()))
        self.assertEqual(review_summary(self.product,now=self.now)['per100'],0)

    def test_status_and_ui_read_do_not_request_api(self):
        self.coverage();self.store(self.order(count=100),self.stats())
        output=StringIO();call_command('sales_status',account=self.ym.name,sku=['sku1'],stdout=output)
        self.assertIn('offerId sku1: выкупов 100',output.getvalue())
        self.client.force_login(self.user)
        self.assertContains(self.client.get('/integrations/'),'Продажи Маркета')
        self.assertContains(self.client.get(f'/products/{self.product.pk}/'),'API Яндекс Маркета')


class EnableMarketSalesTests(DeskCase):
    def setUp(self):
        super().setUp()
        self.accounts=[]
        for index,name in enumerate(['АМ','ЕС','ОП','Velato'],1):
            company=Company.objects.create(name=name,legal_entity=name)
            account=Account.objects.create(company=company,name='ЯМ_'+str(index)if index<4 else'ЯМ_Velato',platform='ym',business_id=str(index),enabled=True,token_env='TEST_ENABLE_YM_KEY')
            self.accounts.append(account)

    @patch.dict(os.environ,{'TEST_ENABLE_YM_KEY':'test-only'})
    def test_preview_then_apply_only_am_es_op_idempotently(self):
        call_command('enable_market_sales',stdout=StringIO())
        self.assertFalse(Account.objects.filter(sales_enabled=True).exists())
        call_command('enable_market_sales',apply=True,stdout=StringIO())
        self.assertEqual(set(Account.objects.filter(sales_enabled=True).values_list('name',flat=True)),{'ЯМ_1','ЯМ_2','ЯМ_3'})
        before=Audit.objects.count();call_command('enable_market_sales',apply=True,stdout=StringIO())
        self.assertEqual(Audit.objects.count(),before)

    @patch.dict(os.environ,{'TEST_ENABLE_YM_KEY':'test-only'})
    def test_conflict_rolls_back_all_changes(self):
        self.accounts[2].business_id='1';self.accounts[2].save()
        with self.assertRaises(CommandError):call_command('enable_market_sales',apply=True,stdout=StringIO())
        self.assertFalse(Account.objects.filter(sales_enabled=True).exists())
