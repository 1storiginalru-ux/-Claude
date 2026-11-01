"""Inspect saved data only. Exports contain internal catalogue IDs, not review text."""
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from desk.models import Account
from desk.voc.data_quality import CAUSES, control_data_report
from .configure_control_profile import write_private


class Command(BaseCommand):
    help = 'Причины неполноты контроля, сначала товары с негативом. Только чтение, без API и AI.'

    def add_arguments(self, parser):
        parser.add_argument('--account', action='append', required=True)
        parser.add_argument('--report', action='store_true')
        parser.add_argument('--limit', type=int, default=20)

    def handle(self, *args, **options):
        accounts = list(Account.objects.filter(name__in=options['account']).select_related('company').order_by('pk'))
        if {a.name for a in accounts} != set(options['account']):
            raise CommandError('Не все кабинеты найдены.')
        if any(a.company.is_demo or a.platform not in {'wb', 'ym'} for a in accounts):
            raise CommandError('Выберите только реальные кабинеты WB и Маркета.')
        if not 1 <= options['limit'] <= 1000:
            raise CommandError('Лимит вывода: от 1 до 1000.')
        report = control_data_report(accounts)
        self.stdout.write('Диагностика только читает данные. API и AI не вызываются. Период приоритета: последние 6 месяцев.')
        summary = report['summary']
        self.stdout.write(f'Товаров каталога: {summary["catalogue_products"]}; с отзывами за 6 мес.: {summary["products_with_reviews"]}; с учтённым негативом: {summary["negative_products"]}.')
        self.stdout.write(f'Пар товар/кабинет: {summary["pairs"]}; без сохранённой активности: {summary["no_activity_pairs"]}; готовы к расчёту: {summary["ready_pairs"]}; активных с неполными данными: {summary["incomplete_active_pairs"]}.')
        self.stdout.write(f'Приоритет — пары с негативом и неполными данными: {summary["negative_incomplete_pairs"]}. Отзывов без ProductID: {summary["unmapped_reviews"]}.')
        for account in report['accounts']:
            storage, sales = account['storage'], account['sales']
            self.stdout.write(f'{account["name"]}: сохранено выкупов {storage["count"]}; последнее сохранение {storage["saved_at"]}; заказы с неопределённостью {account["uncertain_orders"]}.')
            self.stdout.write(f'  Включён: {account["enabled"]}; продажи: {account["sales_enabled"]}; история: {account["history_enabled"]}; глубина {account["history_days"]} дн.; без ProductID: {account["unmapped_reviews"]}.')
            if sales:
                self.stdout.write(f'  Подтверждено: {sales["coverage_since"]} — {sales["coverage_until"]}; продолжение: {sales["continuation"]}; следующая попытка: {sales["next_attempt"]}. {sales["status"]}')
            else:
                self.stdout.write('  Продажи ещё не загружались.')
        self.stdout.write('Причины по активным парам (могут пересекаться):')
        for cause in report['causes']:
            self.stdout.write(f'  {cause["label"]}: {cause["count"]}.')
        problems = [r for r in report['items'] if r['activity'] and not r['ready']]
        for row in problems[:options['limit']]:
            self.stdout.write(f'{row["account"]} · {row["code"]} {row["name"]}: {row["redemptions"]} выкупов; негатив за 6 мес. {row["negative"]}, в окне {row["window_negative"]}; неразобранных в окне {row["window_pending"]}.')
            self.stdout.write('  ' + '; '.join(CAUSES[code][0] for code in row['causes']))
        if len(problems) > options['limit']:
            self.stdout.write(f'Показано {options["limit"]} из {len(problems)} проблемных пар; полный список — с --report.')
        if options['report']:
            stamp = report['generated_at'].strftime('%Y%m%dT%H%M%S%fZ')
            path = settings.STATE_DIR / 'exports' / f'control-data-{stamp}.json'
            try:
                write_private(path, report)
            except OSError as error:
                raise CommandError('Не удалось сохранить закрытый отчёт.') from error
            self.stdout.write('Полный отчёт: ' + str(path))
