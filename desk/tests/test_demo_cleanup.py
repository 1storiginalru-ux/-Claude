import uuid
from datetime import timedelta
from io import StringIO
from unittest.mock import patch
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db.models.deletion import ProtectedError
from django.db.models.query import QuerySet
from django.test import TestCase
from django.utils import timezone
from desk.models import (Account, Analysis, ApiThrottle, Audit, Baseline, Company, Incident,
                         Membership, Message, Outgoing, Product, ProductMapping, ReviewAssessment,
                         SaleEvent, SalesDaily, SalesSyncState, SyncState, Thread)
from desk.reviews import fingerprint
from .base import DeskCase


class DemoCleanupTests(DeskCase):
    def setUp(self):
        super().setUp()
        call_command('seed_demo', stdout=StringIO())
        self.demo = Company.objects.get(is_demo=True)
        self.demo_account = Account.objects.filter(company=self.demo).first()
        self.demo_product = Product.objects.filter(company=self.demo).first()
        self.demo_thread = Thread.objects.filter(account__company=self.demo).first()

    def apply(self):
        output = StringIO()
        call_command('purge_demo', apply=True, stdout=output)
        return output.getvalue()

    def counts(self):
        return {m.__name__: m.objects.count() for m in [Company, Account, Product, Thread, Message, Analysis, Outgoing, Incident, Baseline, SalesDaily, ProductMapping, Audit]}

    def test_preview_lists_exact_seed_records_without_changing_anything(self):
        before = self.counts()
        output = StringIO(); call_command('purge_demo', stdout=output)
        self.assertEqual(self.counts(), before)
        self.assertIn('Отзывы: 10', output.getvalue())
        self.assertIn('Вопросы: 2', output.getvalue())
        self.assertIn('Демо-товары: 3', output.getvalue())
        self.assertIn('Демо-кабинеты: 2', output.getvalue())
        self.assertIn('Строки искусственных продаж: 12', output.getvalue())

    def test_apply_preserves_every_real_record_users_memberships_cursors_and_throttles(self):
        self.analysis(); self.sales(123); self.baseline()
        Membership.objects.create(company=self.demo, user=self.user, role='viewer')
        ReviewAssessment.objects.create(thread=self.thread, decision='negative', signals=['LONG_AUTH'], source_fingerprint=fingerprint(self.thread), reviewed_by=self.user)
        outgoing = Outgoing.objects.create(thread=self.thread, sender=self.user, request_key=uuid.uuid4(), status='Confirmed', external_id='real-answer', text='Рабочий опубликованный ответ')
        Message.objects.create(thread=self.thread, external_id='real-message', direction='out', text='Рабочая история')
        Incident.objects.create(product=self.product, signal='LONG_AUTH', severity='P2', status='CONTROL')
        future = timezone.now() + timedelta(hours=2)
        SyncState.objects.create(account=self.account, cursor={'skip':1000}, next_attempt=future, lease_token=uuid.uuid4(), lease_until=future)
        SalesSyncState.objects.create(account=self.account, cursor={'page_from':'retained'}, next_attempt=future)
        ApiThrottle.objects.create(scope='a'*64, next_request_at=future, strikes=3)
        SaleEvent.objects.create(account=self.account, external_id='S-real', sku='sku1', kind='sale', occurred_at=timezone.now(), changed_at=timezone.now())
        keep = [self.user, self.company, self.account, self.ym, self.product, self.thread, outgoing]
        snapshots = {(obj._meta.label, obj.pk): type(obj).objects.filter(pk=obj.pk).values().get() for obj in keep}
        state = SyncState.objects.filter(account=self.account).values().get()
        sales_state = SalesSyncState.objects.filter(account=self.account).values().get()
        memberships = list(Membership.objects.order_by('pk').values())
        output = self.apply()
        for obj in keep:
            self.assertEqual(type(obj).objects.filter(pk=obj.pk).values().get(), snapshots[(obj._meta.label,obj.pk)])
        self.assertEqual(SyncState.objects.filter(account=self.account).values().get(), state)
        self.assertEqual(SalesSyncState.objects.filter(account=self.account).values().get(), sales_state)
        self.assertEqual(list(Membership.objects.order_by('pk').values()), memberships)
        self.assertEqual(ApiThrottle.objects.get().next_request_at, future)
        self.assertEqual(SaleEvent.objects.get().external_id, 'S-real')
        self.assertEqual(Incident.objects.get().status, 'CONTROL')
        self.assertEqual(ReviewAssessment.objects.get().thread_id, self.thread.pk)
        self.assertEqual(SalesDaily.objects.get().redemptions, 123)
        self.assertFalse(Thread.objects.filter(account__company=self.demo).exists())
        self.assertFalse(Account.objects.filter(company=self.demo).exists())
        self.assertFalse(Product.objects.filter(company=self.demo).exists())
        self.assertEqual(Audit.objects.filter(action='demo.purge').count(),1)
        self.assertIn('Отзывы: 10',output)
        self.client.force_login(self.user)
        self.assertFalse(self.client.get('/').context['has_demo'])
        self.assertNotContains(self.client.get('/products/'), 'Lacoste L.12.12 Blanc')

    def test_repeated_cleanup_is_idempotent_and_does_not_duplicate_audit(self):
        self.apply()
        self.assertIn('Отзывы: 0',self.apply())
        self.assertEqual(Audit.objects.filter(action='demo.purge').count(),1)

    def test_unknown_review_marker_blocks_whole_cleanup(self):
        self.make_thread('marketplace-real-id',account=self.demo_account,product=self.demo_product)
        before=self.counts()
        with self.assertRaisesMessage(CommandError,'без стандартной демо-метки'):self.apply()
        self.assertEqual(self.counts(),before)

    def test_source_payload_in_demo_review_is_not_assumed_fake(self):
        self.demo_thread.source_data={'feedbackId':123456789};self.demo_thread.save()
        before=self.counts()
        with self.assertRaisesMessage(CommandError,'исходные данные маркетплейса'):self.apply()
        self.assertEqual(self.counts(),before)

    def test_mixed_incident_never_deletes_real_or_demo_partially(self):
        case=Incident.objects.create(product=self.demo_product,signal='PACK',severity='P2')
        case.threads.add(self.demo_thread,self.thread)
        before=self.counts()
        with self.assertRaisesMessage(CommandError,'смешанное расследование'):self.apply()
        self.assertEqual(self.counts(),before)
        self.assertEqual(case.threads.count(),2)

    def test_real_mapping_or_review_referencing_demo_product_is_protected(self):
        mapping=ProductMapping.objects.create(account=self.account,product=self.demo_product,sku='cross-company')
        with self.assertRaisesMessage(CommandError,'связан с рабочим кабинетом'):self.apply()
        mapping.delete()
        self.thread.product=self.demo_product;self.thread.save()
        with self.assertRaisesMessage(CommandError,'обращением вне очистки'):self.apply()
        self.assertTrue(Product.objects.filter(pk=self.demo_product.pk).exists())

    def test_shared_physical_product_code_is_not_deleted_from_global_analytics(self):
        Product.objects.create(company=self.company,code=self.demo_product.code,name=self.demo_product.name)
        with self.assertRaisesMessage(CommandError,'используется рабочей компанией'):self.apply()
        self.assertEqual(Product.objects.filter(code=self.demo_product.code).count(),2)

    def test_non_demo_record_with_demo_looking_id_is_preserved(self):
        self.thread.external_id='demo-0';self.thread.save()
        self.apply()
        self.assertTrue(Thread.objects.filter(pk=self.thread.pk).exists())

    def test_enabled_account_and_api_sale_are_blocked(self):
        self.demo_account.enabled=True;self.demo_account.save()
        with self.assertRaisesMessage(CommandError,'включён для внешней интеграции'):self.apply()
        self.demo_account.enabled=False;self.demo_account.save()
        SaleEvent.objects.create(account=self.demo_account,external_id='S-demo-unexpected',sku=self.demo_product.code,kind='sale',occurred_at=timezone.now(),changed_at=timezone.now())
        with self.assertRaisesMessage(CommandError,'события продаж API'):self.apply()

    def test_nonstandard_sales_and_active_lease_are_blocked(self):
        sale=SalesDaily.objects.filter(account=self.demo_account).first()
        sale.redemptions=999;sale.save()
        with self.assertRaisesMessage(CommandError,'продажи вне стандартного набора'):self.apply()
        sale.redemptions=25;sale.save()
        SyncState.objects.create(account=self.demo_account,lease_until=timezone.now()+timedelta(minutes=2))
        with self.assertRaisesMessage(CommandError,'блокировка синхронизации'):self.apply()

    def test_sent_or_sending_response_is_not_silently_deleted(self):
        out=Outgoing.objects.create(thread=self.demo_thread,sender=self.user,request_key=uuid.uuid4(),text='Проверить отправку',status='Sending')
        with self.assertRaisesMessage(CommandError,'внешней отправкой'):self.apply()
        out.status='Confirmed';out.external_id='external-id';out.save()
        with self.assertRaisesMessage(CommandError,'внешней отправкой'):self.apply()
        self.assertTrue(Outgoing.objects.filter(pk=out.pk).exists())

    def test_queued_demo_draft_and_manual_demo_decision_are_removed_without_deleting_user(self):
        out=Outgoing.objects.create(thread=self.demo_thread,sender=self.user,request_key=uuid.uuid4(),text='Демо-черновик',status='Queued')
        assessment=ReviewAssessment.objects.create(thread=self.demo_thread,decision='pending',source_fingerprint=fingerprint(self.demo_thread),reviewed_by=self.user)
        self.apply()
        self.assertFalse(Outgoing.objects.filter(pk=out.pk).exists())
        self.assertFalse(ReviewAssessment.objects.filter(pk=assessment.pk).exists())
        self.assertTrue(get_user_model().objects.filter(pk=self.user.pk).exists())

    def test_external_case_and_global_snapshot_are_protected(self):
        case=Incident.objects.create(product=self.demo_product,signal='GLOBAL-TEST',severity='P2',bitrix_id='external-task')
        with self.assertRaisesMessage(CommandError,'внешней задачей'):self.apply()
        case.bitrix_id='';case.opening_snapshot={'company_ids':[self.company.pk]};case.save()
        with self.assertRaisesMessage(CommandError,'включает рабочую компанию'):self.apply()

    def test_any_late_protected_relation_rolls_back_all_deletions(self):
        before=self.counts()
        original=QuerySet.delete
        def fail_at_products(queryset):
            if queryset.model is Product:raise ProtectedError('Unexpected relation',[])
            return original(queryset)
        with patch.object(QuerySet,'delete',new=fail_at_products):
            with self.assertRaisesMessage(CommandError,'данные не удалены'):self.apply()
        self.assertEqual(self.counts(),before)
        self.assertFalse(Audit.objects.filter(action='demo.purge').exists())


class EmptyDemoCleanupTests(TestCase):
    def test_cleaned_demo_never_reappears_even_before_first_user_is_created(self):
        call_command('seed_demo',stdout=StringIO())
        call_command('purge_demo',apply=True,stdout=StringIO())
        self.assertFalse(Thread.objects.exists())
        self.assertFalse(Account.objects.exists())
        self.assertFalse(Product.objects.exists())
        self.assertEqual(Company.objects.filter(is_demo=True).count(),1)
        call_command('prepare_local',no_input=True,stdout=StringIO())
        self.assertFalse(Thread.objects.exists())
        self.assertFalse(Account.objects.exists())
        self.assertFalse(get_user_model().objects.exists())
