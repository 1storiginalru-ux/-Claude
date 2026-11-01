"""Regression for real missing August orders and false 90-day certificates."""
import copy
import importlib
import os
from datetime import timedelta
from io import StringIO
from unittest.mock import patch
from django.apps import apps
from django.db import connection
from django.utils import timezone
from django.core.management import call_command
from desk.integrations.base import IntegrationError
from desk.integrations.ym_scan import STATS_LAG, scan_page
from desk.integrations.ym_sales import YandexSales, sync_ym_sales
from desk.models import AIJob, ApiThrottle, Audit, HistorySyncState, Outgoing, SaleEvent, SalesSyncState
from desk.integrations.rate_limit import scope_for
from desk.integrations.history import sales_history_allowed, history_turn, sync_history
from .test_ym_sales import MarketSalesCase


class MarketSalesScanTests(MarketSalesCase):
    @patch.object(YandexSales,'request')
    def test_august_delivery_is_restored_by_its_actual_date_and_replay_does_not_duplicate(self,request):
        created=self.now-timedelta(days=80)
        delivered=self.now-timedelta(days=76)
        order=self.order(count=1,creationDate=created.isoformat(),updateDate=delivered.isoformat())
        stats=self.stats(creationDate=timezone.localdate(created).isoformat(),statusUpdateDate=delivered.isoformat())
        cursor={'since':(self.now-timedelta(days=90)).isoformat(),'until':self.now.isoformat(),'days':90}
        state=HistorySyncState.objects.create(account=self.ym,kind='sales',cursor=cursor)
        for _ in range(2):
            request.side_effect=self.responses([order],[stats])
            count,next_cursor,complete=scan_page(self.ym,state,cursor)
            self.assertFalse(complete)
            self.assertEqual(next_cursor['until'],(self.now-STATS_LAG).isoformat())
            body=request.call_args.kwargs['json']
            self.assertEqual(body['dates']['creationDateFrom'],timezone.localdate(created).isoformat())
            self.assertEqual(body['orderIds'],[1])
        self.assertEqual(SaleEvent.objects.count(),1)
        self.assertEqual(SaleEvent.objects.get().occurred_at,delivered)

    @patch.object(YandexSales,'request')
    def test_recently_delivered_order_created_before_the_update_window_is_not_lost(self,request):
        created=self.now-timedelta(days=200)
        order=self.order(count=1,creationDate=created.isoformat())
        stats=self.stats(creationDate=timezone.localdate(created).isoformat())
        request.side_effect=self.responses([order],[stats])
        self.assertEqual(sync_ym_sales(self.ym),1)
        self.assertEqual(SaleEvent.objects.get().occurred_at,self.now-timedelta(days=1))
        self.assertTrue(request.call_args.kwargs['json']['dates']['creationDateFrom']<timezone.localdate(self.now-timedelta(days=30)).isoformat())

    @patch.object(YandexSales,'request')
    def test_shop_without_access_cannot_certify_scope_and_does_not_erase_saved_events(self,request):
        # The archived shop is listed first by the API; active shops are restored first.
        campaigns={'campaigns':[{'id':1,'business':{'id':123},'apiAvailability':'DISABLED_BY_INACTIVITY'},
                               {'id':10,'business':{'id':123},'apiAvailability':'AVAILABLE'}]}
        request.side_effect=[campaigns,*self.responses([self.order()],[self.stats()])[1:]]
        sync_ym_sales(self.ym)
        state=SalesSyncState.objects.get();self.assertIsNone(state.coverage_until)
        self.assertEqual(SaleEvent.objects.count(),3)
        old_cursor=copy.deepcopy(state.cursor)
        self.due();request.reset_mock()
        with self.assertRaisesRegex(IntegrationError,'Маркета 1.*DISABLED_BY_INACTIVITY'):
            sync_ym_sales(self.ym)
        request.assert_not_called();state.refresh_from_db()
        self.assertEqual(state.cursor,old_cursor);self.assertIsNone(state.coverage_until)
        self.assertEqual(SaleEvent.objects.count(),3)

    @patch.object(YandexSales,'request')
    def test_foreign_business_shop_is_not_queried_or_imported(self,request):
        request.side_effect=[{'campaigns':[{'id':99,'business':{'id':999}},*self.campaigns()['campaigns']]},
                             *self.responses([self.order()],[self.stats()])[1:]]
        sync_ym_sales(self.ym)
        self.assertNotIn('/v2/campaigns/99/stats/orders',[c.args[1] for c in request.call_args_list])

    @patch.object(YandexSales,'request')
    def test_partial_campaign_pages_cannot_mark_a_period_complete(self,request):
        request.return_value={**self.campaigns(),'paging':{'nextPageToken':'more'}}
        sync_ym_sales(self.ym)
        state=SalesSyncState.objects.get()
        self.assertEqual(state.cursor['campaign_token'],'more')
        self.assertIsNone(state.coverage_until);self.assertEqual(request.call_count,1)

    @patch.object(YandexSales,'request')
    def test_missing_or_foreign_exact_order_blocks_the_whole_page(self,request):
        order=self.order();order['campaignId']=99
        request.side_effect=self.responses([order],[self.stats()])
        with self.assertRaises(IntegrationError):sync_ym_sales(self.ym)
        self.assertFalse(SaleEvent.objects.exists())
        self.assertIsNone(SalesSyncState.objects.get().coverage_until)

    @patch.object(YandexSales,'request')
    def test_orders_on_distant_creation_dates_use_separate_bounded_batches(self,request):
        old_created=self.now-timedelta(days=200)
        old=self.order(2,count=1,creationDate=old_created.isoformat())
        stats=[self.stats(),self.stats(2,creationDate=timezone.localdate(old_created).isoformat())]
        request.side_effect=[self.campaigns(),{'status':'OK','result':{'orders':stats}},
                             {'orders':[old]},{'orders':[self.order()]}]
        sync_ym_sales(self.ym)
        for call in request.call_args_list[2:]:
            dates=call.kwargs['json']['dates']
            self.assertLessEqual(timezone.datetime.fromisoformat(dates['creationDateTo'])-
                                 timezone.datetime.fromisoformat(dates['creationDateFrom']),timedelta(days=30))
        self.assertEqual(SaleEvent.objects.count(),4)

    @patch.object(YandexSales,'request')
    def test_primed_recovery_can_progress_while_a_current_shop_is_blocked(self,request):
        state=SalesSyncState.objects.create(account=self.ym,cursor={'partial':'keep'},
            next_attempt=self.now+timedelta(hours=1),status='Магазин: HTTP 403')
        self.assertTrue(sales_history_allowed(self.ym,state))
        state.next_attempt=self.now+timedelta(seconds=61)
        self.assertTrue(sales_history_allowed(self.ym,state))
        HistorySyncState.objects.create(account=self.ym,kind='sales',cursor={
            'since':(self.now-timedelta(days=90)).isoformat(),'until':(self.now-STATS_LAG).isoformat(),
            'days':90,'ym_version':2})
        created=self.now-timedelta(days=80)
        delivered=self.now-timedelta(days=76)
        request.side_effect=self.responses([self.order(count=1,creationDate=created.isoformat(),updateDate=delivered.isoformat())],
            [self.stats(creationDate=timezone.localdate(created).isoformat(),statusUpdateDate=delivered.isoformat())])
        self.assertEqual(sync_history(self.ym,'sales'),1)
        self.assertEqual(SaleEvent.objects.get().occurred_at,delivered)
        self.assertIsNone(SalesSyncState.objects.get().coverage_until)


class MarketSalesSchedulingTests(MarketSalesCase):
    def setUp(self):
        super().setUp()
        self.account.enabled=False;self.account.save()
        self.current=SalesSyncState.objects.create(account=self.ym,cursor={'ym_version':2,'page_token':'unfinished-current'},
            next_attempt=self.now-timedelta(seconds=1))
        self.history=HistorySyncState.objects.create(account=self.ym,kind='sales',cursor={
            'since':(self.now-timedelta(days=90)).isoformat(),'until':(self.now-STATS_LAG).isoformat(),
            'days':90,'ym_version':2},next_attempt=self.now-timedelta(seconds=1))

    def poll(self):
        with patch('desk.management.commands.sync_channels.sync_account'), \
                patch('desk.integrations.wb_sales.sync_wb_sales') as wb, \
                patch('desk.integrations.ym_sales.sync_ym_sales',return_value=0) as current, \
                patch('desk.integrations.history.sync_history',return_value=0) as history, \
                patch('desk.incidents.service.recheck_products'):
            call_command('sync_channels',stdout=StringIO(),stderr=StringIO())
            return current,history,wb

    def test_current_continuation_and_due_history_both_get_a_turn_before_any_coverage(self):
        self.history.last_request_at=self.now-timedelta(minutes=1);self.history.save()
        current,history,wb=self.poll()
        current.assert_called_once();wb.assert_not_called()
        self.assertTrue(any(c.args[0].pk==self.ym.pk and c.args[1]=='sales' for c in history.call_args_list))
        self.current.refresh_from_db();self.history.refresh_from_db()
        self.assertIsNone(self.current.coverage_until);self.assertIsNone(self.history.coverage_until)
        self.assertEqual(self.current.cursor['page_token'],'unfinished-current')

    def test_recent_history_request_does_not_suppress_current_reader(self):
        self.current.coverage_since=self.now-timedelta(days=7);self.current.coverage_until=self.now;self.current.save()
        self.history.last_request_at=self.now-timedelta(minutes=1);self.history.save()
        self.assertFalse(history_turn(self.ym,'sales',self.now))
        current,history,wb=self.poll()
        current.assert_called_once()
        self.assertTrue(any(c.args[1]=='sales' for c in history.call_args_list))

    def test_primed_history_gets_a_reserved_turn_without_a_current_certificate(self):
        self.assertTrue(history_turn(self.ym,'sales',self.now))
        current,history,wb=self.poll()
        current.assert_not_called()
        self.assertTrue(any(c.args[1]=='sales' for c in history.call_args_list))

    def test_scarce_slots_are_not_repeatedly_reserved_while_current_has_not_advanced(self):
        self.history.last_request_at=self.now-timedelta(minutes=6);self.history.save()
        self.current.cursor['page_completed_at']=(self.now-timedelta(minutes=7)).isoformat();self.current.save()
        self.assertFalse(history_turn(self.ym,'sales',self.now))
        current,history,wb=self.poll()
        current.assert_called_once()
        self.current.cursor['page_completed_at']=(self.now-timedelta(minutes=1)).isoformat();self.current.save()
        self.assertTrue(history_turn(self.ym,'sales',self.now))

    def test_live_current_lease_defers_history_without_modifying_retry_deadlines(self):
        self.current.lease_until=self.now+timedelta(minutes=5);self.current.save()
        current,history,wb=self.poll()
        self.assertFalse(any(c.args[1]=='sales' for c in history.call_args_list))
        self.current.refresh_from_db()
        self.assertEqual(self.current.next_attempt,self.now-timedelta(seconds=1))

    def test_wb_unfinished_stream_remains_exclusive_even_after_a_long_pause(self):
        wb=SalesSyncState.objects.create(account=self.account,cursor={'page_from':'pending'},
            next_attempt=self.now+timedelta(hours=2))
        self.assertFalse(sales_history_allowed(self.account,wb,self.now))

    @patch('desk.integrations.base.requests.request')
    @patch.dict(os.environ,{'TEST_YM_TOKEN':'fairness-test-only'})
    def test_a_shared_api_pause_still_blocks_the_historical_http_request(self,request):
        deadline=self.now+timedelta(minutes=10)
        ApiThrottle.objects.create(scope=scope_for('fairness-test-only','ym-sales'),next_request_at=deadline)
        self.current.next_attempt=deadline;self.current.save()
        self.assertTrue(sales_history_allowed(self.ym,self.current,self.now))
        with self.assertRaises(IntegrationError):sync_history(self.ym,'sales')
        request.assert_not_called()
        self.history.refresh_from_db();self.current.refresh_from_db()
        self.assertGreaterEqual(self.history.next_attempt,deadline)
        self.assertEqual(self.current.next_attempt,deadline)


class MarketCoverageMigrationTests(MarketSalesCase):
    def test_migration_withdraws_old_certificates_and_primes_recovery_without_deleting_data(self):
        self.coverage(90);self.store(self.order(),self.stats())
        state=SalesSyncState.objects.get(account=self.ym)
        deadline=self.now+timedelta(hours=2)
        state.next_attempt=deadline;state.cursor={'old_page':'audit'};state.save()
        history=HistorySyncState.objects.create(account=self.ym,kind='sales',days=90,exhausted=True,
            coverage_since=self.now-timedelta(days=90),coverage_until=self.now)
        wb=SalesSyncState.objects.create(account=self.account,coverage_since=self.now-timedelta(days=90),coverage_until=self.now)
        before=list(SaleEvent.objects.values())
        migration=importlib.import_module('desk.migrations.0020_recheck_market_sales_coverage')
        migration.revoke_coverage(apps,type('Editor',(),{'connection':connection})())
        state.refresh_from_db();history.refresh_from_db();wb.refresh_from_db()
        self.assertIsNone(state.coverage_since);self.assertIsNone(history.coverage_until)
        self.assertEqual(state.next_attempt,deadline);self.assertGreaterEqual(history.next_attempt,deadline)
        self.assertEqual(history.cursor['days'],90);self.assertFalse(history.exhausted)
        self.assertEqual(list(SaleEvent.objects.values()),before)
        self.assertEqual(wb.coverage_until,self.now)
        self.assertEqual(Audit.objects.filter(action='ym.sales.recheck_coverage').count(),1)
        self.assertFalse(AIJob.objects.exists() or Outgoing.objects.exists())
