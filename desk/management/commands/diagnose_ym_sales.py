import os
from datetime import timedelta
from django.core.management.base import BaseCommand,CommandError
from django.utils import timezone
from desk.models import Account
from desk.integrations.base import IntegrationError
from desk.integrations.ym_sales import YandexSales,identifier


class Command(BaseCommand):
    help='Проверить настройку продаж Маркета; --probe читает до одного заказа и его статистику без импорта.'

    def add_arguments(self,parser):
        parser.add_argument('--account',required=True)
        parser.add_argument('--probe',action='store_true')

    def handle(self,*args,**options):
        account=Account.objects.select_related('company').filter(name=options['account'],platform='ym').first()
        if not account:raise CommandError('Кабинет Маркета не найден.')
        key=account.sales_token_env or account.token_env
        self.stdout.write(f'{account.name} · {account.company.name}. Продажи: {account.sales_enabled}. Business ID: {"задан" if account.business_id else "не задан"}.')
        self.stdout.write(f'Переменная {key}: {"задана" if os.environ.get(key) else "не задана"}.')
        if not options['probe']:
            self.stdout.write('Сетевые запросы не выполнялись. Для проверки прав добавьте --probe.');return
        adapter=YandexSales(account);now=timezone.now()
        try:
            response=adapter.request('POST',adapter.path('orders',1),json={'fake':False,'dates':{'updateDateFrom':(now-timedelta(days=7)).isoformat(),'updateDateTo':now.isoformat()}},params={'limit':1})
            rows=response.get('orders') if isinstance(response,dict) else None
            if not isinstance(rows,list):raise IntegrationError('Неожиданный формат ответа заказов Маркета.')
            self.stdout.write(f'API заказов доступен. В проверочном ответе: {len(rows)} заказов.')
            if rows:
                campaign,order=identifier(rows[0].get('campaignId')),identifier(rows[0].get('orderId'))
                data=adapter.request('POST',f'/v2/campaigns/{campaign}/stats/orders',json={'orders':[int(order)]},params={'limit':200})
                result=data.get('result') if isinstance(data,dict) else None
                if not isinstance(result,dict) or not isinstance(result.get('orders'),list):raise IntegrationError('Неожиданный формат статистики заказов Маркета.')
                self.stdout.write('API статистики доступен. Его данные могут отставать до 40 минут.')
            else:self.stdout.write('Права статистики проверятся, когда появится заказ с известным магазином.')
        except IntegrationError as error:raise CommandError(str(error)) from error
        self.stdout.write('Обращения, продажи и позиции загрузки не изменены.')
