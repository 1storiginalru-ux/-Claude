from django.core.management.base import BaseCommand, CommandError
from django.db.models import Q
from desk.models import Account, HistorySyncState, SaleEvent


class Command(BaseCommand):
    help = 'Состояние исторической загрузки, без запросов API и вывода ключей.'

    def add_arguments(self, parser): parser.add_argument('--account', required=True)

    def handle(self, *args, **options):
        value=str(options['account']); match=Q(name=value)
        if value.isdecimal(): match |= Q(pk=int(value))
        account=Account.objects.filter(match).first()
        if not account: raise CommandError('Кабинет не найден.')
        self.stdout.write(f'Кабинет: {account.name}. Дозагрузка до 100 выкупов: {account.history_enabled}.')
        self.stdout.write('Предел автоматического расширения: 90 дней. Текущий опрос — отдельно, за 7 суток.')
        self.stdout.write(f'Всего сохранённых выкупов: {SaleEvent.objects.filter(account=account,kind="sale").count()}.')
        states=list(HistorySyncState.objects.filter(account=account).order_by('kind'))
        if not states: self.stdout.write('Историческая загрузка ещё не запускалась.')
        for state in states:
            self.stdout.write(f'{state.get_kind_display()}: {state.status or "Ожидает запуска"}')
            depth=f'{state.days} дн.' if state.coverage_since else 'ещё не подтверждена'
            self.stdout.write(f'Завершённая глубина: {depth}; продолжение: {bool(state.cursor)}; предел достигнут: {state.exhausted}.')
            self.stdout.write(f'Покрытие UTC: {state.coverage_since} — {state.coverage_until}. Следующая попытка UTC: {state.next_attempt}.')
