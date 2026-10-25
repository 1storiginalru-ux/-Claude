import io
import tempfile
from pathlib import Path
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import SimpleTestCase
from openpyxl import Workbook
from desk.products.imports import ImportProblem, read_table
from desk.products.prepare import prepare_mapping


class PrepareMappingTests(SimpleTestCase):
    def upload(self, rows):
        book = Workbook()
        book.active.append(['ID', 'ВБ1', 'ВБ2', 'ЯМ1', 'Брэнд', 'Товар', 'Тип', 'Объем'])
        for row in rows:
            book.active.append(row)
        data = io.BytesIO(); book.save(data); book.close()
        return SimpleUploadedFile('catalog.xlsx', data.getvalue())

    def test_conversion_preserves_internal_ids_leading_zero_ym_ids_and_explicit_accounts(self):
        volume = 'Clementine California 100 мл + Orange Sanguine 30 мл + кожаный чехол'
        upload = self.upload([[9, 357900140, 999, '00009', 'Adidas', 'Dynamic Pulse', 'туалетная вода', volume],
                              [10, None, 888, None, None, 'Товар без нужных артикулов', None, None]])
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / 'result'
            result = prepare_mapping(upload, [('WB', 'ВБ1', 'ВБ_1'), ('YM', 'ЯМ1', 'ЯМ_1')], 'АМ', output)
            self.assertEqual([(r['account'], r['rows']) for r in result], [('ВБ_1', 1), ('ЯМ_1', 1)])
            for item, expected in zip(result, ['357900140', '00009']):
                with item['path'].open('rb') as file:
                    row = read_table(file)[0]
                self.assertEqual(row['ProductID'], '9')
                self.assertEqual(row['Account'], item['account'])
                self.assertEqual(row['SKU'], expected)
                self.assertEqual(row['External Product ID'], expected)
                self.assertEqual(row['Legal Entity'], 'АМ')
                self.assertEqual(row['Volume'], volume)
                self.assertEqual(item['path'].stat().st_mode & 0o777, 0o600)

    def test_large_catalog_is_split_into_files_accepted_by_normal_reader(self):
        upload = self.upload([[n, 10000 + n, None, None, 'Brand', f'Name {n}', 'EDT', 50] for n in range(1, 5002)])
        with tempfile.TemporaryDirectory() as directory:
            result = prepare_mapping(upload, [('WB', 'ВБ1', 'ВБ_1')], 'АМ', Path(directory) / 'result')
            self.assertEqual([item['rows'] for item in result], [5000, 1])
            for item in result:
                with item['path'].open('rb') as file:
                    self.assertEqual(len(read_table(file)), item['rows'])

    def test_conflicts_are_rejected_before_any_files_are_created(self):
        cases = [
            [[1, 123, None, None, 'Brand', 'Name', 'EDT', 50], [2, 123, None, None, 'Brand', 'Other', 'EDT', 50]],
            [[1, 123, None, None, None, 'Name', 'EDT', 50]],
            [[1, 'seller-article', None, None, 'Brand', 'Name', 'EDT', 50]],
        ]
        with tempfile.TemporaryDirectory() as directory:
            for index, rows in enumerate(cases):
                path = Path(directory) / str(index)
                with self.subTest(index=index), self.assertRaises(ImportProblem):
                    prepare_mapping(self.upload(rows), [('WB', 'ВБ1', 'ВБ_1')], 'АМ', path)
                self.assertFalse(path.exists())

    def test_existing_output_is_never_overwritten(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(FileExistsError):
                prepare_mapping(self.upload([[1, 123, None, None, 'Brand', 'Name', 'EDT', 50]]), [('WB', 'ВБ1', 'ВБ_1')], 'АМ', directory)
