import os
from django.core.management.base import BaseCommand, CommandError
from django.db.models import Q
from desk.integrations.window import sync_window
from desk.models import Account, ProductMapping, SaleEvent, YandexOrderState


class Command(BaseCommand):
    help = 'Состояние продаж и количества за 7 суток, без сетевых запросов и вывода ключей.'

    def add_arguments(self, parser):
        parser.add_argument('--account', required=True)
        parser.add_argument('--sku', action='append', default=[], help='WB nmId или offerId Маркета; можно указать несколько раз.')

    def handle(self, *args, **options):
        value = str(options['account'])
        match = Q(name=value)
        if value.isdigit(): match |= Q(pk=int(value))
        account = Account.objects.filter(match, platform__in=['wb','ym']).first()
        if not account: raise CommandError('Кабинет WB или Маркета не найден.')
        self.stdout.write(f'Кабинет: {account.name}. Продажи включены: {account.sales_enabled}.')
        key = account.sales_token_env or account.token_env
        self.stdout.write(f'Переменная: {key}. Ключ: {"задан" if os.environ.get(key) else "не задан"}.')
        since, until = sync_window()
        self.stdout.write(f'Период UTC: {since.isoformat()} — {until.isoformat()}')
        events = SaleEvent.objects.filter(account=account, occurred_at__gte=since, occurred_at__lte=until)
        self.stdout.write(f'За период: выкупов {events.filter(kind="sale").count()}, возвратов {events.filter(kind="return").count()}.')
        if account.platform=='ym':
            self.stdout.write(f'Заказов с неподтверждёнными данными выкупа: {YandexOrderState.objects.filter(account=account).exclude(issues=[]).count()}. Возвраты датируются подтверждением статуса, не датой финансовой операции.')
        for sku in options['sku']:
            subset = events.filter(sku=sku)
            label='nmId' if account.platform=='wb' else 'offerId'
            self.stdout.write(f'{label} {sku}: выкупов {subset.filter(kind="sale").count()}, возвратов {subset.filter(kind="return").count()}; ProductID: {ProductMapping.objects.filter(account=account, sku=sku).values_list("product__code", flat=True).first() or "не сопоставлен"}')
        if hasattr(account, 'sales_state'):
            state = account.sales_state
            self.stdout.write(f'Результат: {state.status}. Следующая попытка: {state.next_attempt.isoformat()}')
            self.stdout.write(f'Подтверждённое покрытие UTC: {state.coverage_since} — {state.coverage_until}. Продолжение страницы: {bool(state.cursor)}')
        else:
            self.stdout.write('Продажи ещё не загружались.')
