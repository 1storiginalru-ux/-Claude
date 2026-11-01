"""Topic/cabinet boundaries, independent series and a non-mutating rollout preview."""
from datetime import timedelta
from io import StringIO
import json
from pathlib import Path
import tempfile
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import override_settings
from django.test.utils import CaptureQueriesContext
from django.db import connection
from desk.models import Account, AIJob, AIRequest, Analysis, Audit, Baseline, Company, ControlDefaults, ControlTopicRule, HistorySyncState, Incident, Membership, Outgoing, Product, ProductMapping, SaleEvent, SalesSyncState, SyncState
from desk.voc.dataset import ControlDataset
from desk.voc.profile import preview_profile
from desk.voc.rules import evaluate
from desk.voc.queue import product_queue
from desk.voc.topics import CONTROL_SIGNALS, PROFILE_RATES, starter_profile
from desk.incidents.service import evaluate_incidents, recheck_products
from .test_review_control import ReviewControlCase


class TopicControlCase(ReviewControlCase):
    def topic(self, signal, account=None, **changes):
        values=starter_profile()[signal];values.update(changes)
        return ControlTopicRule.objects.create(account=account or self.account,signal=signal,**values)

    def negatives(self,count,signals):
        for i in range(count):self.analysis(self.thread if i==0 else self.make_thread(f'n-{i}'),signals=signals)



class TopicControlTests(TopicControlCase):
    def test_approved_rates_and_series_are_explicitly_configured_not_created_by_reading(self):
        self.assertEqual(PROFILE_RATES,{'NEGATIVE':(5,10),'LONG_AUTH':(3,5),'SCENT':(4,7),'PACK':(2,4),
            'DEFECT':(1,2),'MARK':(1,2),'RETURN':(3,5),'CONTENT':(2,4),'DELIVERY':(3,6),
            'SERVICE':(2,4),'PRICE':(5,10),'OTHER':(3,6)})
        for signal in CONTROL_SIGNALS:self.assertIsNone(evaluate(self.product,signal)['baseline'])
        self.assertFalse(ControlTopicRule.objects.exists())
        profile=starter_profile()
        self.assertEqual((profile['DEFECT']['cluster_count'],profile['DEFECT']['cluster_days']),(3,7))
        self.assertEqual((profile['CONTENT']['cluster_count'],profile['CONTENT']['cluster_days']),(4,3))
        self.assertFalse(profile['PRICE']['cluster_enabled'])

    def test_exact_topic_warning_and_high_threshold_are_inclusive(self):
        self.events();self.topic('PACK',cluster_enabled=False)
        self.negatives(2,['PACK'])
        metric=evaluate(self.product,'PACK')
        self.assertEqual((metric['count'],metric['rate'],metric['status'],metric['warning']),(2,2,'WATCH',True))
        for i in range(2,4):self.analysis(self.make_thread(f'n-{i}'),signals=['PACK'])
        self.assertEqual(evaluate(self.product,'PACK')['status'],'ANOMALY')
        recheck_products();case=Incident.objects.get()
        self.assertEqual((case.signal,case.threads.count(),case.opening_snapshot['threshold']),('PACK',4,4))
        recheck_products();self.assertEqual(Incident.objects.count(),1)

    def test_rare_topic_warning_starts_at_one_and_investigation_at_two(self):
        self.events();self.topic('DEFECT',cluster_enabled=False)
        self.negatives(1,['DEFECT'])
        self.assertTrue(evaluate(self.product,'DEFECT')['warning'])
        self.analysis(self.make_thread('defect-2'),signals=['DEFECT'])
        self.assertEqual(evaluate(self.product,'DEFECT')['status'],'ANOMALY')

    def test_minimum_count_is_additional_to_rate(self):
        self.events();self.topic('DEFECT',min_signals=3,cluster_enabled=False)
        self.negatives(2,['DEFECT'])
        self.assertEqual(evaluate(self.product,'DEFECT')['reason'],'Недостаточно сигналов для аномалии')
        self.assertFalse(evaluate(self.product,'DEFECT')['warning'])

    def test_individual_product_rules_override_cabinet_defaults_in_both_engines(self):
        self.events();self.topic('PACK');self.negatives(4,['PACK'])
        override=self.baseline(signal='PACK',low=1,high=9,cluster_enabled=False,rule_kind='thresholds')
        scope=Company.objects.filter(pk=self.company.pk)
        dataset=ControlDataset([self.product],scope,now=self.now)
        for account in [None,self.account.pk]:
            one=evaluate(self.product,'PACK',scope=scope,now=self.now,account_id=account)
            bulk=evaluate(self.product,'PACK',dataset=dataset,account_id=account)
            self.assertEqual(one,bulk);self.assertEqual(bulk['baseline'],override)
            self.assertEqual(bulk['status'],'WATCH')

    def test_historical_range_keeps_previous_interpretation(self):
        self.events();self.negatives(4,['PACK']);self.topic('PACK')
        self.baseline(signal='PACK',source='historical',low=2,high=4,cluster_enabled=False)
        metric=evaluate(self.product,'PACK')
        self.assertEqual(metric['status'],'NORMAL');self.assertFalse(metric['warning'])
        self.assertFalse(metric['threshold_rule'])

    def test_themes_share_negative_review_but_overall_counts_it_once(self):
        self.events();self.analysis(signals=['LONG','AUTH-UNKNOWN','SCENT'])
        self.topic('LONG_AUTH');self.topic('SCENT');ControlDefaults.objects.create(account=self.account)
        self.assertEqual(evaluate(self.product,'NEGATIVE')['count'],1)
        self.assertEqual(evaluate(self.product,'LONG_AUTH')['count'],1)
        self.assertEqual(evaluate(self.product,'SCENT')['count'],1)

    def test_three_rare_complaints_in_seven_days_escalate_without_a_denominator(self):
        self.topic('MARK')
        for i in range(3):
            t=self.thread if i==0 else self.make_thread(f'mark-{i}')
            t.received_at=self.now-timedelta(days=i+1);t.save()
            self.analysis(t,signals=['MARK'])
        self.make_thread('still-unclassified')
        metric=evaluate(self.product,'MARK',now=self.now)
        self.assertTrue(metric['cluster']);self.assertIsNone(metric['rate'])
        self.assertEqual(metric['status'],'ANOMALY')
        recheck_products();self.assertEqual(Incident.objects.get().signal,'MARK')

    def test_untrusted_positive_future_and_old_reviews_do_not_form_series(self):
        self.topic('DEFECT')
        self.analysis(signals=['DEFECT'])
        for name,changes in [('old',{'received_at':self.now-timedelta(days=8)}),('future',{'received_at':self.now+timedelta(days=1)}),('low',{})]:
            t=self.make_thread(name,**changes);self.analysis(t,signals=['DEFECT'],confidence=.4 if name=='low' else .95)
        t=self.make_thread('praise');self.analysis(t,signals=['DEFECT'],sentiment='positive',hidden_negative=False)
        self.assertFalse(evaluate(self.product,'DEFECT',now=self.now)['cluster'])

    def test_incomplete_sales_or_classification_never_produce_a_reliable_rate(self):
        self.events(67);self.topic('SCENT',cluster_enabled=False);self.negatives(6,['SCENT'])
        self.assertIsNone(evaluate(self.product,'SCENT')['rate'])
        self.assertEqual(evaluate(self.product,'SCENT')['status'],'WATCH')
        self.assertFalse(evaluate(self.product,'SCENT')['warning'])

    def test_valid_sales_with_an_unclassified_review_cannot_trigger_a_rate_alert(self):
        self.events();self.topic('SCENT',cluster_enabled=False);self.negatives(7,['SCENT'])
        self.make_thread('pending')
        metric=evaluate(self.product,'SCENT')
        self.assertIsNone(metric['rate']);self.assertEqual(metric['status'],'WATCH')
        self.assertFalse(metric['warning'])

    def test_one_serious_p0_report_can_open_a_check_without_objective_flag(self):
        self.analysis(signals=['OTHER'],severity='P0',objective_issue=False)
        evaluate_incidents(self.thread)
        case=Incident.objects.get()
        self.assertEqual(case.opening_snapshot['trigger']['type'],'serious')
        self.assertEqual(case.signal,'OTHER')

    def test_background_check_handles_serious_reports_without_reopening_closed_evidence(self):
        self.topic('DEFECT');self.analysis(signals=['DEFECT'],severity='P1',objective_issue=True)
        recheck_products();case=Incident.objects.get()
        self.assertEqual(case.opening_snapshot['trigger']['type'],'serious')
        case.status='CLOSED';case.result='Проверено';case.save()
        recheck_products();evaluate_incidents(self.thread)
        self.assertEqual(Incident.objects.count(),1)
        new=self.make_thread('new-serious');self.analysis(new,signals=['DEFECT'],severity='P1',objective_issue=True)
        recheck_products();self.assertEqual(Incident.objects.count(),2)

    def test_neutral_return_question_does_not_count_as_a_negative_or_an_actual_return(self):
        self.events();self.topic('RETURN')
        self.analysis(signals=['RETURN'],sentiment='neutral',hidden_negative=False)
        self.assertEqual(evaluate(self.product,'RETURN')['count'],0)
        self.assertFalse(Incident.objects.exists())

    def test_process_alert_is_labelled_as_process_control(self):
        self.events();self.topic('SERVICE');self.negatives(4,['SERVICE'])
        recheck_products()
        self.assertEqual(Incident.objects.get().opening_snapshot['control_kind'],'process')

    def test_local_theme_anomaly_is_not_hidden_by_another_cabinets_sales(self):
        self.events();self.topic('PACK',cluster_enabled=False);self.negatives(4,['PACK'])
        self.ym.sales_enabled=True;self.ym.save()
        ProductMapping.objects.create(account=self.ym,product=self.product,sku='ym-sku')
        for model in [SalesSyncState,SyncState]:model.objects.create(account=self.ym,coverage_since=self.now-timedelta(days=7),coverage_until=self.now)
        HistorySyncState.objects.create(account=self.ym,kind='reviews',coverage_since=self.now-timedelta(days=7),coverage_until=self.now)
        SaleEvent.objects.bulk_create([SaleEvent(account=self.ym,external_id=f'YM-{i}',sku='ym-sku',kind='sale',occurred_at=self.now-timedelta(minutes=1),changed_at=self.now) for i in range(100)])
        self.topic('PACK',self.ym,cluster_enabled=False)
        self.assertEqual(evaluate(self.product,'PACK',now=self.now)['status'],'NORMAL')
        self.assertEqual(evaluate(self.product,'PACK',now=self.now,account_id=self.account.pk)['status'],'ANOMALY')
        recheck_products();case=Incident.objects.get()
        self.assertEqual((case.signal,case.opening_snapshot['account_id']),('PACK',self.account.pk))
        self.assertEqual(case.threads.count(),4)

    def test_conflicting_topics_keep_individual_account_rules_and_permission_scope(self):
        self.topic('PACK');self.topic('PACK',self.ym,threshold=9)
        ProductMapping.objects.create(account=self.ym,product=self.product,sku='ym-sku')
        dataset=ControlDataset([self.product],Company.objects.filter(pk=self.company.pk))
        self.assertIsNone(dataset.baseline(self.product.code,'PACK'))
        self.assertEqual(dataset.baseline(self.product.code,'PACK',self.account.pk).high,4)
        self.assertEqual(dataset.baseline(self.product.code,'PACK',self.ym.pk).high,9)
        other=Product.objects.create(company=self.other,code=self.product.code,name='PRIVATE')
        ProductMapping.objects.create(account=self.other_account,product=other,sku='private')
        self.topic('PACK',self.other_account,warning=10,threshold=20)
        limited=ControlDataset([self.product],Company.objects.filter(pk=self.company.pk))
        self.assertEqual(limited.baseline(self.product.code,'PACK',self.account.pk).high,4)


class ControlProfileCommandTests(TopicControlCase):
    def test_preview_report_and_apply_are_safe_idempotent_and_keep_individual_rules(self):
        self.events();self.negatives(2,['PACK'])
        override=self.baseline(signal='MARK',high=8);before=Baseline.objects.values().get(pk=override.pk)
        with tempfile.TemporaryDirectory() as directory,override_settings(STATE_DIR=Path(directory)):
            output=StringIO()
            call_command('configure_control_profile',account=[self.account.name],report=True,stdout=output)
            self.assertFalse(ControlTopicRule.objects.exists() or ControlDefaults.objects.exists() or Incident.objects.exists())
            self.assertFalse(Audit.objects.exists())
            report=next((Path(directory)/'exports').glob('*.json'))
            packet=json.loads(report.read_text());self.assertEqual(packet['format'],'control-topics-1')
            self.assertEqual(packet['items'][0]['after'],'Предупреждение')
            self.assertEqual(report.stat().st_mode&0o777,0o600)
            call_command('configure_control_profile',account=[self.account.name],apply=True,stdout=output)
            self.assertEqual(ControlTopicRule.objects.count(),11)
            self.assertEqual(ControlDefaults.objects.count(),1)
            self.assertFalse(Incident.objects.exists() or AIJob.objects.exists() or AIRequest.objects.exists() or Outgoing.objects.exists())
            self.assertEqual(Baseline.objects.values().get(pk=override.pk),before)
            audits=Audit.objects.count()
            call_command('configure_control_profile',account=[self.account.name],apply=True,stdout=output)
            self.assertEqual(Audit.objects.count(),audits)
            self.assertEqual(len(list((Path(directory)/'backups').glob('*.json'))),1)

    def test_all_six_accounts_are_enabled_without_other_companies_or_ozon(self):
        self.account.name='ВБ_1';self.account.save();self.ym.name='ЯМ_1';self.ym.save()
        names=['ВБ_1','ЯМ_1']
        for n in [2,3]:
            company=Company.objects.create(name=f'Shop {n}')
            for prefix,platform in [('ВБ','wb'),('ЯМ','ym')]:
                a=Account.objects.create(company=company,name=f'{prefix}_{n}',platform=platform);names.append(a.name)
        with tempfile.TemporaryDirectory() as folder,override_settings(STATE_DIR=Path(folder)):
            call_command('configure_control_profile',*[arg for name in names for arg in ('--account',name)],apply=True,stdout=StringIO())
        self.assertEqual(ControlTopicRule.objects.count(),66)
        self.assertEqual(ControlDefaults.objects.count(),6)
        self.assertFalse(ControlTopicRule.objects.filter(account=self.other_account).exists())

    def test_unknown_or_ozon_account_aborts_before_writing_anything(self):
        ozon=Account.objects.create(company=self.company,name='Ozon',platform='ozon')
        for name in ['Missing',ozon.name]:
            with self.subTest(name=name),self.assertRaises(CommandError):
                call_command('configure_control_profile','--account',self.account.name,'--account',name,apply=True,stdout=StringIO())
            self.assertFalse(ControlDefaults.objects.exists() or ControlTopicRule.objects.exists() or Audit.objects.exists())


class ControlTopicPermissionsTests(TopicControlCase):
    def payload(self,signal='PACK'):
        return {'account':str(self.account.pk),'signal':signal,'version':'',**starter_profile()[signal]}

    def test_settings_are_scoped_and_operator_cannot_change_them(self):
        response=self.client.get('/voc/rules/',{'account':self.account.pk})
        self.assertContains(response,'Пороги по кабинетам');self.assertNotContains(response,'PRIVATE')
        self.assertEqual(self.client.post('/voc/rules/',self.payload()).status_code,403)
        self.assertEqual(self.client.get('/voc/rules/',{'account':self.other_account.pk}).status_code,404)
        self.assertEqual(self.client.post('/voc/rules/',{**self.payload(),'account':self.other_account.pk}).status_code,404)
        self.assertFalse(ControlTopicRule.objects.exists())

    def test_quality_can_edit_topic_and_stale_form_cannot_overwrite_new_settings(self):
        Membership.objects.filter(user=self.user,company=self.company).update(role='quality')
        self.assertEqual(self.client.post('/voc/rules/',self.payload()).status_code,302)
        row=ControlTopicRule.objects.get();version=row.updated_at.isoformat()
        data={**self.payload(),'version':version,'threshold':6}
        self.assertEqual(self.client.post('/voc/rules/',data).status_code,302)
        self.assertEqual(self.client.post('/voc/rules/',{**data,'threshold':8}).status_code,200)
        self.assertEqual(ControlTopicRule.objects.get().threshold,6)
        self.assertEqual(Audit.objects.filter(action='control.topic_rule').count(),2)

    def test_card_and_queue_show_topic_warning_and_all_twelve_topics(self):
        self.events();self.topic('PACK',cluster_enabled=False);self.negatives(2,['PACK'])
        response=self.client.get(f'/products/{self.product.pk}/')
        self.assertContains(response,'Предупреждение');self.assertContains(response,'Правило кабинетов по теме')
        self.assertEqual(len(response.context['metrics']),12)
        self.assertEqual(len(response.context['account_metrics']),12)
        response=self.client.get('/voc/')
        self.assertContains(response,'Предупреждение');self.assertContains(response,'Пороги по кабинетам')
        self.assertFalse(Incident.objects.exists())

    def test_rating_provenance_does_not_add_queries_per_product_or_topic(self):
        from desk.ai.ratings import classify_rating,EMPTY_TEXT
        self.topic('PACK')
        self.thread.text=EMPTY_TEXT;self.thread.save();classify_rating(self.thread)
        with CaptureQueriesContext(connection) as first:self.client.get('/voc/')
        for i in range(6):
            p=Product.objects.create(company=self.company,code=f'R{i}',name='Рейтинг без текста')
            ProductMapping.objects.create(product=p,account=self.account,sku=f'rating-{i}')
            t=self.make_thread(f'rating-{i}',product=p,text=EMPTY_TEXT,source_data={'productValuation':5})
            classify_rating(t)
        with CaptureQueriesContext(connection) as many:self.client.get('/voc/')
        self.assertLessEqual(len(many),len(first)+3)
