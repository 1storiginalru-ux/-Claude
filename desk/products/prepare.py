"""Convert a local wide catalog into bounded, ordinary mapping imports."""
import os
from pathlib import Path
from openpyxl import Workbook
from .identifiers import wb_nm_id
from .imports import ImportProblem, read_table, required


HEADER = ['ProductID', 'Product Name', 'Brand', 'Volume', 'Concentration',
          'Platform', 'Account', 'Legal Entity', 'SKU', 'External Product ID']


def prepare_mapping(upload, bindings, legal_entity, output):
    """bindings: explicit (platform, source column, exact account name) tuples.

    No database access, credentials, external requests or inferred account names.
    Validate everything before creating output files.
    """
    if not bindings or not legal_entity.strip():
        raise ImportProblem('Укажите кабинеты и юридическое лицо.')
    if len({column for _, column, _ in bindings}) != len(bindings):
        raise ImportProblem('Один столбец нельзя привязать к нескольким кабинетам.')
    if len({account for _, _, account in bindings}) != len(bindings):
        raise ImportProblem('Один кабинет нельзя указать несколько раз.')
    if any(platform not in {'WB', 'YM'} or not column or not account or len(account) > 80 for platform, column, account in bindings):
        raise ImportProblem('Проверьте площадку, столбец и имя кабинета.')
    rows = read_table(upload, max_rows=100000, max_uncompressed=30 * 1024 * 1024)
    columns = ['ID', 'Брэнд', 'Товар', 'Тип', 'Объем'] + [column for _, column, _ in bindings]
    if any(column not in rows[0] for column in columns):
        raise ImportProblem('Нужны столбцы: ' + ', '.join(columns) + '.')
    catalog, seen = {}, set()
    batches = [[] for _ in bindings]
    for line, row in enumerate(rows, 2):
        if not any(row[column] for _, column, _ in bindings):
            continue
        required(row, ['ID', 'Брэнд', 'Товар', 'Тип', 'Объем'], line)
        description = [row['Товар'], row['Брэнд'], row['Объем'], row['Тип']]
        if any(len(value) > limit for value, limit in zip([row['ID'], *description], [100, 200, 100, 240, 40])):
            raise ImportProblem(f'Строка {line}: слишком длинное описание товара.')
        if row['ID'] in catalog and catalog[row['ID']] != description:
            raise ImportProblem(f'Строка {line}: разные описания одного ID {row["ID"]}.')
        catalog[row['ID']] = description
        for index, (platform, column, account) in enumerate(bindings):
            sku = row[column]
            if not sku:
                continue
            if platform == 'WB':
                sku = wb_nm_id(sku)
                if not sku:
                    raise ImportProblem(f'Строка {line}, {column}: нужен числовой артикул WB (nmId).')
            if len(sku) > 120:
                raise ImportProblem(f'Строка {line}, {column}: слишком длинный артикул.')
            if (account, sku) in seen:
                raise ImportProblem(f'Строка {line}: повтор артикула {sku} в кабинете {account}.')
            seen.add((account, sku))
            # YM uses offerId; WB uses nmId. Both fields explicitly contain the
            # supplied identifier, not a fabricated marketSku or seller article.
            batches[index].append([row['ID'], *description, platform, account, legal_entity.strip(), sku, sku])
    if any(not batch for batch in batches):
        raise ImportProblem('В одном из выбранных столбцов нет артикулов.')
    output = Path(output)
    output.mkdir(mode=0o700, parents=True, exist_ok=False)
    result = []
    for index, ((platform, column, account), batch) in enumerate(zip(bindings, batches), 1):
        for part, start in enumerate(range(0, len(batch), 5000), 1):
            book = Workbook(write_only=True)
            sheet = book.create_sheet('Mapping')
            sheet.append(HEADER)
            portion = batch[start:start + 5000]
            for values in portion:
                sheet.append(values)
            path = output / f'{platform}-{index}-{part:02d}.xlsx'
            with open(path, 'xb', opener=lambda p, flags: os.open(p, flags, 0o600)) as stream:
                book.save(stream)
            book.close()
            result.append({'path': path, 'account': account, 'column': column, 'rows': len(portion)})
    return result
