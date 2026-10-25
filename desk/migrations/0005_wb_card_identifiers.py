"""Switch real WB accounts from seller articles to nmId, without API requests."""
import re
from collections import defaultdict
from django.db import migrations, models


def nm_id(value):
    # Keep migration independent of future application changes.
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        return ''
    value = str(value).strip()
    return str(int(value)) if re.fullmatch(r'[0-9]{1,120}', value) and int(value) else ''


def migrate_wb_identifiers(apps, schema_editor):
    alias = schema_editor.connection.alias
    Account = apps.get_model('desk', 'Account')
    Mapping = apps.get_model('desk', 'ProductMapping')
    Thread = apps.get_model('desk', 'Thread')
    Sales = apps.get_model('desk', 'SalesDaily')
    Audit = apps.get_model('desk', 'Audit')
    accounts = dict(Account.objects.using(alias).filter(platform='wb', company__is_demo=False).values_list('pk', 'company_id'))
    observed = defaultdict(set)
    threads = list(Thread.objects.using(alias).filter(account_id__in=accounts))
    for thread in threads:
        source = thread.source_data if isinstance(thread.source_data, dict) else {}
        product = source.get('productDetails') or {}
        card = nm_id(product.get('nmId')) if isinstance(product, dict) else ''
        if card and thread.sku:
            observed[(thread.account_id, thread.sku)].add(card)

    mappings = list(Mapping.objects.using(alias).filter(account_id__in=accounts))
    targets, by_old, by_card = {}, {}, {}
    for mapping in mappings:
        key = (mapping.account_id, mapping.sku)
        cards = observed[key]
        card = nm_id(mapping.external_product_id) or (next(iter(cards)) if len(cards) == 1 else '')
        target = card or mapping.sku
        target_key = (mapping.account_id, target)
        if target_key in targets:
            raise RuntimeError(f'WB: сопоставления {targets[target_key]} и {mapping.pk} переходят на один nmId {target}. Исправьте конфликт в Product Mapping перед обновлением. Изменения отменены.')
        targets[target_key] = mapping.pk
        if card:
            by_old[key] = card
            by_card[(mapping.account_id, card)] = mapping.product_id

    # Do not sum or overwrite possibly duplicated daily sales during a key change.
    sales = list(Sales.objects.using(alias).filter(account_id__in=accounts))
    sales_targets = set()
    for sale in sales:
        target = by_old.get((sale.account_id, sale.sku), sale.sku)
        key = (sale.account_id, target, sale.date)
        if key in sales_targets:
            raise RuntimeError(f'WB: конфликт дневных продаж для nmId {target} за {sale.date}. Исправьте повтор перед обновлением. Изменения отменены.')
        sales_targets.add(key)

    audits, changed_mappings, changed_sales, changed_threads = [], [], [], []
    def audit(account_id, kind, pk, before, after):
        audits.append(Audit(company_id=accounts[account_id], action=f'wb.nmid.{kind}', object_id=str(pk), before=before, after=after))

    for mapping in mappings:
        card = by_old.get((mapping.account_id, mapping.sku))
        if card and (mapping.sku != card or mapping.external_product_id != card):
            audit(mapping.account_id, 'mapping', mapping.pk, {'sku': mapping.sku, 'external_product_id': mapping.external_product_id}, {'sku': card, 'external_product_id': card})
            mapping.sku = mapping.external_product_id = card
            changed_mappings.append(mapping)
    # Two mappings may exchange their old numeric seller SKUs. Stage keys first.
    occupied = {(m.account_id, m.sku) for m in mappings}
    occupied.update(Mapping.objects.using(alias).filter(account_id__in=accounts).values_list('account_id', 'sku'))
    for mapping in changed_mappings:
        temporary = f'__wb_nmId_migration_{mapping.pk}__'
        while (mapping.account_id, temporary) in occupied:
            temporary += '_'
        occupied.add((mapping.account_id, temporary))
        Mapping.objects.using(alias).filter(pk=mapping.pk).update(sku=temporary)
    Mapping.objects.using(alias).bulk_update(changed_mappings, ['sku', 'external_product_id'], batch_size=500)

    for sale in sales:
        target = by_old.get((sale.account_id, sale.sku))
        if target and target != sale.sku:
            audit(sale.account_id, 'sales', sale.pk, {'sku': sale.sku}, {'sku': target})
            sale.sku = target
            changed_sales.append(sale)
    # Dates have a unique(account, sku, date) constraint too; stage all changed keys.
    for sale in changed_sales:
        temporary = f'__wb_sale_migration_{sale.pk}__'
        while Sales.objects.using(alias).filter(account_id=sale.account_id, sku=temporary, date=sale.date).exists():
            temporary += '_'
        Sales.objects.using(alias).filter(pk=sale.pk).update(sku=temporary)
    Sales.objects.using(alias).bulk_update(changed_sales, ['sku'], batch_size=500)

    for thread in threads:
        source = thread.source_data if isinstance(thread.source_data, dict) else {}
        details = source.get('productDetails') or {}
        card = nm_id(details.get('nmId')) if isinstance(details, dict) else ''
        card = card or by_old.get((thread.account_id, thread.sku), '')
        # Unknown seller articles are preserved in audit, not guessed as card IDs.
        product_id = by_card.get((thread.account_id, card)) if card else thread.product_id
        if thread.sku != card or thread.product_id != product_id:
            audit(thread.account_id, 'thread', thread.pk, {'sku': thread.sku, 'product_id': thread.product_id}, {'sku': card, 'product_id': product_id})
            thread.sku, thread.product_id = card, product_id
            changed_threads.append(thread)
    Thread.objects.using(alias).bulk_update(changed_threads, ['sku', 'product'], batch_size=500)
    Audit.objects.using(alias).bulk_create(audits, batch_size=500)


class Migration(migrations.Migration):
    dependencies = [('desk', '0004_full_marketplace_content')]
    operations = [
        migrations.AlterField(model_name='product', name='volume', field=models.CharField(blank=True, max_length=240)),
        migrations.RunPython(migrate_wb_identifiers),
    ]
