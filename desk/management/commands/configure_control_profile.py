"""Preview and explicitly enable the agreed topic profile for selected cabinets."""
import json
import os
from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone
from desk.models import Account
from desk.voc.profile import policy_state, preview_profile, save_rule, validate_rule
from desk.voc.rules import signal_label
from desk.voc.topics import PROFILE_VERSION, starter_profile


def write_private(path, data):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with open(path, 'x', encoding='utf-8', opener=lambda p,f: os.open(p,f,0o600)) as stream:
        json.dump(data, stream, ensure_ascii=False, indent=2, default=str)


class Command(BaseCommand):
    help = 'Предпросмотр и включение тематических порогов. Без --apply только чтение.'

    def add_arguments(self, parser):
        parser.add_argument('--account', action='append', required=True)
        parser.add_argument('--apply', action='store_true')
        parser.add_argument('--report', action='store_true', help='Сохранить закрытый JSON-отчёт в exports.')
        parser.add_argument('--limit', type=int, default=20, help='Число товаров в выводе; отчёт содержит все.')

    def handle(self, *args, **options):
        accounts = list(Account.objects.filter(name__in=options['account']).select_related('company').order_by('pk'))
        if {a.name for a in accounts} != set(options['account']): raise CommandError('Не все кабинеты найдены.')
        if any(a.company.is_demo or a.platform not in {'wb','ym'} for a in accounts):
            raise CommandError('Выберите только реальные кабинеты WB и Маркета.')
        if options['limit'] < 1 or options['limit'] > 1000: raise CommandError('Лимит вывода: от 1 до 1000.')
        profile = starter_profile()
        try:
            for account in accounts:
                for signal, fields in profile.items(): validate_rule(account, signal, fields)
        except ValidationError as error: raise CommandError(str(error)) from error
        before = policy_state(accounts)
        report = preview_profile(accounts)
        self.stdout.write('Профиль: ' + PROFILE_VERSION + '. Кабинеты: ' + ', '.join(a.name for a in accounts))
        for signal, fields in profile.items():
            series = f'{fields["cluster_count"]} за {fields["cluster_days"]} дн.' if fields['cluster_enabled'] else 'выключена'
            self.stdout.write(f'{signal_label(signal)}: предупреждение от {fields["warning"]}, расследование от {fields["threshold"]}; серия: {series}.')
        summary = report['summary']
        self.stdout.write(f'Товаров: {summary["products"]}; требуют внимания до/после: {summary["attention_before"]}/{summary["attention_after"]}; новых: {summary["new_attention"]}; неполные данные: {summary["incomplete"]}.')
        shown = [r for r in report['items'] if r['attention_after']]
        for row in shown[:options['limit']]:
            self.stdout.write(f'{row["code"]}: {row["name"]} — {row["before"]} → {row["after"]}; {row["reason"]}')
            for alert in row['alerts']:
                self.stdout.write(f'  {alert["account"]}, {alert["label"]}: {alert["count"]}/{alert["redemptions"]}; {alert["reason"]}.')
        if len(shown) > options['limit']:
            self.stdout.write(f'Показано {options["limit"]} из {len(shown)} товаров; полный список доступен с --report.')
        self.stdout.write(f'Индивидуальных правил сохранится: {len(report["preserved_overrides"])}. Предпросмотр не создаёт расследований.')
        stamp = timezone.now().strftime('%Y%m%dT%H%M%S%fZ')
        if options['report']:
            path = settings.STATE_DIR / 'exports' / ('control-profile-preview-' + stamp + '.json')
            try: write_private(path, report)
            except OSError as error: raise CommandError('Не удалось сохранить отчёт; настройки не изменены.') from error
            self.stdout.write('Отчёт до включения: ' + str(path))
        if not options['apply']:
            self.stdout.write('Только предпросмотр. Для сохранения добавьте --apply.')
            return
        with transaction.atomic():
            list(Account.objects.select_for_update(of=('self',),no_key=True).filter(pk__in=[a.pk for a in accounts]).order_by('pk'))
            if policy_state(accounts) != before:
                raise CommandError('Во время предпросмотра правила изменились. Повторите команду.')
            changed = any(not any(old['account_id']==a.pk and (signal=='NEGATIVE' or old['signal']==signal)
                and all(old[k]==v for k,v in fields.items() if signal!='NEGATIVE' or k!='cluster_enabled')
                for old in before['overall' if signal=='NEGATIVE' else 'topics']) for a in accounts for signal,fields in profile.items())
            if not changed:
                self.stdout.write('Все правила уже совпадают. Изменений нет.');return
            backup = settings.STATE_DIR / 'backups' / ('control-profile-before-' + stamp + '.json')
            try: write_private(backup, {'format':PROFILE_VERSION,'accounts':[a.pk for a in accounts],'before':before,'profile':profile})
            except OSError as error: raise CommandError('Не удалось создать резервную копию; настройки не изменены.') from error
            count = 0
            for account in accounts:
                for signal, fields in profile.items(): count += save_rule(account,signal,fields)
            self.stdout.write(f'Сохранено правил: {count}. Резервная копия: {backup}')
            self.stdout.write('Фоновая проверка применит пороги. Решения сотрудников, ответы и бюджеты AI сохранены.')
