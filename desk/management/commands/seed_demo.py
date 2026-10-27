from datetime import timedelta
from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone
from desk.ai.provider import local_classify
from desk.integrations.base import Incoming
from desk.integrations.sync import ingest
from desk.models import Account, Analysis, Baseline, Company, Product, ProductMapping, SalesDaily
from desk.incidents.service import evaluate_incidents
from desk.demo_data import PRODUCTS, EXAMPLES


class Command(BaseCommand):
    help = 'Добавить помеченные демо-данные без ключей, пользователей и внешних запросов. Повторный запуск безопасен.'

    @transaction.atomic
    def handle(self, *args, **options):
        company, _ = Company.objects.get_or_create(name='Original Parfum (демо)', defaults={'is_demo': True, 'legal_entity': 'Демонстрационное ООО', 'brand': 'Original', 'tone_of_voice': 'Общаемся уважительно, спокойно и по делу. Не спорим с опытом клиента. Не подтверждаем факты, которых нет в базе знаний.', 'knowledge': 'Технические и коммерческие условия требуют проверки оператором. Не обещать возврат или компенсацию без подтверждённых оснований.'})
        if not company.is_demo:
            self.stderr.write('Имя демо-компании занято. Данные не изменены.'); return
        wb, _ = Account.objects.get_or_create(name='WB · Original (демо)', defaults={'company': company, 'platform': 'wb', 'token_env': 'VOC_WB_TOKEN'})
        ym, _ = Account.objects.get_or_create(name='ЯМ · Original (демо)', defaults={'company': company, 'platform': 'ym', 'token_env': 'VOC_YM_TOKEN', 'business_id': '0'})
        products = []
        for code, name, brand, volume, conc in PRODUCTS:
            product, _ = Product.objects.get_or_create(company=company, code=code, defaults={'name': name, 'brand': brand, 'volume': volume, 'concentration': conc})
            products.append(product)
            for account in [wb, ym]:
                ProductMapping.objects.get_or_create(account=account, sku=code, defaults={'product': product, 'external_product_id': code})
                for d in [0, 1]:
                    SalesDaily.objects.get_or_create(account=account, sku=code, date=timezone.localdate() - timedelta(days=d), defaults={'redemptions': 25, 'orders': 28, 'delivered': 26, 'returns': 1})
            for signal in ['AUTH', 'AUTH-UNKNOWN', 'LONG', 'SCENT', 'PACK', 'DEFECT', 'MARK', 'RETURN']:
                Baseline.objects.get_or_create(product=product, signal=signal, defaults={'low': 2 if signal == 'LONG' else 0, 'high': 5, 'cluster_count': 4})
        for n, (name, text, rating, product_index, kind) in enumerate(EXAMPLES):
            account = wb if n % 2 == 0 else ym
            item, changed = ingest(account, Incoming(f'demo-{n}', kind, text, products[product_index].code, name, rating, timezone.now() - timedelta(minutes=20 + n * 31)))
            if changed or not hasattr(item, 'analysis'):
                data = local_classify(text, rating)
                data['summary'] = 'Демонстрационный пример. ' + data['summary']
                Analysis.objects.update_or_create(thread=item, defaults={**data, 'provider': 'demo'})
                evaluate_incidents(item)
        self.stdout.write('Демо-данные добавлены. Кабинеты выключены. Реальные сообщения не отправлялись.')
