import importlib
from unittest.mock import Mock
from django.apps import apps
from django.db import transaction
from django.utils import timezone
from desk.integrations.sync import ingest
from desk.integrations.content import content_view
from desk.integrations.wildberries import Wildberries
from desk.models import Audit, Message, Product, ProductMapping, SalesDaily
from desk.products.imports import ImportProblem, import_mapping
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.exceptions import ValidationError
from .base import DeskCase
from .test_imports import HEADER


class WBIdentifierTests(DeskCase):
    def row(self, card=123, **kwargs):
        return {'id': 'wb-card-review', 'text': 'Комментарий', 'productValuation': 5,
                'productDetails': {'supplierArticle': 'sku1', 'nmId': card}, **kwargs}

    def test_review_and_question_use_card_id_even_with_same_seller_article(self):
        ProductMapping.objects.create(account=self.account, product=self.product, sku='123', external_product_id='123')
        other = Product.objects.create(company=self.company, code='P2', name='Другой товар')
        ProductMapping.objects.create(account=self.account, product=other, sku='456', external_product_id='456')
        for kind in ['review', 'question']:
            for card, product in [(123, self.product), (456, other)]:
                thread, _ = ingest(self.account, Wildberries(self.account).parse(self.row(card, id=f'{kind}-{card}'), kind))
                self.assertEqual(thread.sku, str(card))
                self.assertEqual(thread.product, product)
                self.assertEqual(thread.source_data['productDetails']['supplierArticle'], 'sku1')
                self.assertIn({'label': 'Артикул продавца', 'value': 'sku1'}, content_view(thread)['info'])

    def test_missing_or_invalid_nm_id_never_falls_back_to_seller_article(self):
        for value in [None, '', 0, -1, True, 12.5, '123.0', 'unknown']:
            with self.subTest(value=value):
                incoming = Wildberries(self.account).parse(self.row(value, id=f'missing-{value}'), 'review')
                thread, _ = ingest(self.account, incoming)
                self.assertEqual(thread.sku, '')
                self.assertIsNone(thread.product_id)

    def test_refresh_corrects_legacy_identity_without_reopening_or_reanalyzing(self):
        ProductMapping.objects.create(account=self.account, product=self.product, sku='123', external_product_id='123')
        self.thread.status = 'Закрыто'
        self.thread.closed_at = timezone.now()
        self.thread.save()
        analysis = self.analysis()
        row = self.row(id=self.thread.external_id, text=self.thread.text, productValuation=self.thread.rating)
        thread, changed = ingest(self.account, Wildberries(self.account).parse(row, 'review'))
        self.assertFalse(changed)
        self.assertEqual(thread.pk, self.thread.pk)
        self.assertEqual(thread.sku, '123')
        self.assertEqual(thread.product, self.product)
        self.assertEqual(thread.status, 'Закрыто')
        self.assertEqual(thread.closed_at, self.thread.closed_at)
        self.assertEqual(thread.analysis.pk, analysis.pk)

    def test_import_binds_existing_card_review_and_keeps_other_account_separate(self):
        incoming = Wildberries(self.account).parse(self.row(), 'review')
        thread, _ = ingest(self.account, incoming)
        other = self.make_thread('other-account', account=self.other_account, sku='123', product=None)
        self.assertIsNone(thread.product_id)
        upload = SimpleUploadedFile('data.csv', (HEADER + 'P2,New,Brand,50,EDP,WB,WB1,ООО A,123,123\n').encode())
        import_mapping(self.company, upload, self.user)
        thread.refresh_from_db(); other.refresh_from_db()
        self.assertEqual(thread.product.code, 'P2')
        self.assertIsNone(other.product_id)

    def test_import_rejects_seller_sku_or_conflicting_card_ids(self):
        for sku, external in [('supplier-123', '123'), ('123', '456'), ('0', '0')]:
            with self.subTest(sku=sku), self.assertRaises(ImportProblem):
                import_mapping(self.company, SimpleUploadedFile('data.csv', (HEADER + f'P2,New,Brand,50,EDP,WB,WB1,ООО A,{sku},{external}\n').encode()), self.user)
        self.assertFalse(Product.objects.filter(code='P2').exists())

    def test_admin_model_validation_rejects_seller_articles(self):
        mapping = ProductMapping(account=self.account, product=self.product, sku='seller-article', external_product_id='123')
        with self.assertRaises(ValidationError):
            mapping.full_clean()
        mapping.sku = '000123'
        mapping.full_clean()
        self.assertEqual((mapping.sku, mapping.external_product_id), ('123', '123'))


class WBIdentifierMigrationTests(DeskCase):
    def migrate(self):
        migration = importlib.import_module('desk.migrations.0005_wb_card_identifiers')
        with transaction.atomic():
            migration.migrate_wb_identifiers(apps, Mock(connection=Mock(alias='default')))

    def test_migration_preserves_sales_messages_ai_and_closed_status(self):
        mapping = ProductMapping.objects.get(account=self.account)
        mapping.external_product_id = '123'; mapping.save()
        sale = self.sales(31)
        self.thread.source_data = self.row_source = {'productDetails': {'nmId': 123, 'supplierArticle': 'sku1'}}
        self.thread.status, self.thread.closed_at = 'Закрыто', timezone.now()
        self.thread.save()
        analysis = self.analysis()
        message = Message.objects.create(thread=self.thread, external_id='sent-answer', direction='out', text='Уже опубликован')
        ym_mapping = ProductMapping.objects.get(account=self.ym)
        self.migrate()
        self.thread.refresh_from_db(); mapping.refresh_from_db(); sale.refresh_from_db(); ym_mapping.refresh_from_db()
        self.assertEqual((mapping.sku, mapping.external_product_id), ('123', '123'))
        self.assertEqual((sale.sku, sale.redemptions), ('123', 31))
        self.assertEqual((self.thread.sku, self.thread.product_id, self.thread.status), ('123', self.product.pk, 'Закрыто'))
        self.assertEqual(self.thread.source_data, self.row_source)
        self.assertEqual(self.thread.analysis.pk, analysis.pk)
        self.assertEqual(self.thread.messages.get().pk, message.pk)
        self.assertEqual(ym_mapping.sku, 'sku1')
        self.assertTrue(Audit.objects.filter(action='wb.nmid.thread', object_id=str(self.thread.pk), before__sku='sku1').exists())

    def test_unknown_seller_sku_cannot_be_mistaken_for_numeric_card(self):
        self.thread.sku = '123'; self.thread.save()
        self.migrate()
        self.thread.refresh_from_db()
        self.assertEqual(self.thread.sku, '')
        self.assertTrue(Audit.objects.filter(action='wb.nmid.thread', before__sku='123').exists())

    def test_unambiguous_raw_card_recovers_mapping_without_external_id(self):
        self.thread.source_data = {'productDetails': {'nmId': 123}}
        self.thread.save()
        self.migrate()
        self.assertEqual(ProductMapping.objects.get(account=self.account).sku, '123')
        self.thread.refresh_from_db()
        self.assertEqual(self.thread.product_id, self.product.pk)

    def test_conflicting_mappings_abort_without_partial_changes(self):
        ProductMapping.objects.filter(account=self.account).update(external_product_id='123')
        ProductMapping.objects.create(account=self.account, product=self.product, sku='other-seller', external_product_id='123')
        with self.assertRaisesRegex(RuntimeError, 'один nmId'):
            self.migrate()
        self.assertTrue(ProductMapping.objects.filter(account=self.account, sku='sku1').exists())
        self.assertFalse(Audit.objects.filter(action__startswith='wb.nmid.').exists())

    def test_sales_collision_is_not_summed_or_overwritten(self):
        ProductMapping.objects.filter(account=self.account).update(external_product_id='123')
        sale = self.sales(31)
        SalesDaily.objects.create(account=self.account, sku='123', date=sale.date, redemptions=99)
        with self.assertRaisesRegex(RuntimeError, 'конфликт дневных продаж'):
            self.migrate()
        self.assertEqual(set(SalesDaily.objects.values_list('sku', 'redemptions')), {('sku1', 31), ('123', 99)})

    def test_swapping_numeric_seller_keys_preserves_two_products_and_sales(self):
        mapping = ProductMapping.objects.get(account=self.account)
        mapping.sku, mapping.external_product_id = '123', '456'; mapping.save()
        other = Product.objects.create(company=self.company, code='P2', name='Другой')
        other_mapping = ProductMapping.objects.create(account=self.account, product=other, sku='456', external_product_id='123')
        sale = self.sales(31); sale.sku = '123'; sale.save()
        other_sale = SalesDaily.objects.create(account=self.account, sku='456', date=sale.date, redemptions=99)
        self.migrate()
        mapping.refresh_from_db(); other_mapping.refresh_from_db(); sale.refresh_from_db(); other_sale.refresh_from_db()
        self.assertEqual((mapping.sku, mapping.product_id, sale.sku, sale.redemptions), ('456', self.product.pk, '456', 31))
        self.assertEqual((other_mapping.sku, other_mapping.product_id, other_sale.sku, other_sale.redemptions), ('123', other.pk, '123', 99))
