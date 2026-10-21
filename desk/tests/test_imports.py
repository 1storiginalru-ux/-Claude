import io
from datetime import timedelta
from django.core.files.uploadedfile import SimpleUploadedFile
from django.utils import timezone
from openpyxl import Workbook
from desk.products.imports import import_mapping, import_sales, ImportProblem
from desk.models import Product, ProductMapping, SalesDaily
from .base import DeskCase

HEADER = 'ProductID,Product Name,Brand,Volume,Concentration,Platform,Account,Legal Entity,SKU,External Product ID\n'


class ImportTests(DeskCase):
    def upload(self, text): return SimpleUploadedFile('data.csv', text.encode())

    def test_import_new_product_and_two_account_mapping(self):
        data = HEADER + 'P2,New,Brand,50,EDP,WB,WB1,ООО A,sku2,100\nP2,New,Brand,50,EDP,YM,YM1,ООО A,sku3,200\n'
        self.assertEqual(import_mapping(self.company, self.upload(data), self.user), 2)
        p = Product.objects.get(company=self.company, code='P2')
        self.assertEqual(p.mappings.count(), 2)
        import_mapping(self.company, self.upload(data), self.user)
        self.assertEqual(p.mappings.count(), 2)

    def test_conflict_rolls_back_entire_file(self):
        data = HEADER + 'P2,New,Brand,50,EDP,WB,WB1,ООО A,sku2,100\nP3,Other,Brand,50,EDP,WB,WB1,ООО A,sku1,999\n'
        with self.assertRaises(ImportProblem): import_mapping(self.company, self.upload(data), self.user)
        self.assertFalse(Product.objects.filter(code='P2').exists())
        self.assertFalse(ProductMapping.objects.filter(sku='sku2').exists())

    def test_cross_company_account_is_rejected(self):
        data = HEADER + 'P2,New,Brand,50,EDP,YM,Other,ООО A,sku2,100\n'
        with self.assertRaises(ImportProblem): import_mapping(self.company, self.upload(data), self.user)

    def test_xlsx_mapping(self):
        book = Workbook(); sheet = book.active
        sheet.append(HEADER.strip().split(',')); sheet.append(['P2','New','Brand','50','EDP','WB','WB1','ООО A','sku2','100'])
        buffer = io.BytesIO(); book.save(buffer)
        self.assertEqual(import_mapping(self.company, SimpleUploadedFile('data.xlsx', buffer.getvalue()), self.user), 1)

    def test_xlsx_formula_is_rejected(self):
        book = Workbook(); sheet = book.active
        sheet.append(HEADER.strip().split(',')); sheet.append(['=1+1','New','Brand','50','EDP','WB','WB1','ООО A','sku2','100'])
        buffer = io.BytesIO(); book.save(buffer)
        with self.assertRaises(ImportProblem): import_mapping(self.company, SimpleUploadedFile('data.xlsx', buffer.getvalue()), self.user)

    def test_duplicate_sku_is_rejected_atomically(self):
        row = 'P2,New,Brand,50,EDP,WB,WB1,ООО A,sku2,100\n'
        with self.assertRaises(ImportProblem): import_mapping(self.company, self.upload(HEADER + row + row), self.user)
        self.assertFalse(Product.objects.filter(code='P2').exists())

    def test_sales_reimport_keeps_history_and_changes_only_requested_day(self):
        today, old = timezone.localdate(), timezone.localdate() - timedelta(days=10)
        header = 'Account,SKU,Date,Redemptions\n'
        import_sales(self.company, self.upload(header + f'WB1,sku1,{old},20\nWB1,sku1,{today},30\n'), self.user)
        import_sales(self.company, self.upload(header + f'WB1,sku1,{today},35\n'), self.user)
        self.assertEqual(SalesDaily.objects.count(), 2)
        self.assertEqual(SalesDaily.objects.get(date=old).redemptions, 20)
        self.assertEqual(SalesDaily.objects.get(date=today).redemptions, 35)

    def test_invalid_sales_do_not_partially_save(self):
        today = timezone.localdate()
        with self.assertRaises(ImportProblem):
            import_sales(self.company, self.upload(f'Account,SKU,Date,Redemptions\nWB1,sku1,{today},20\nWB1,sku1,{today},-3\n'), self.user)
        self.assertEqual(SalesDaily.objects.count(), 0)
