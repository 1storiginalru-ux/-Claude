"""Explicit, backed-up rollout of shared thresholds; never runs AI or incident checks."""
import json
import os
from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone
from desk.forms import NegativeRuleForm
from desk.models import Account, Audit, Baseline, ControlDefaults, ProductMapping
from desk.voc.defaults import FIELDS


class Command(BaseCommand):
    help = 'Общие пороги для текущих и новых товаров выбранных кабинетов. По умолчанию просмотр.'

    def add_arguments(self, parser):
        parser.add_argument('--account', action='append', required=True)
        parser.add_argument('--warning', type=float, default=5)
        parser.add_argument('--threshold', type=float, default=10)
        parser.add_argument('--min-signals', type=int, default=3)
        parser.add_argument('--cluster-count', type=int, default=4)
        parser.add_argument('--cluster-days', type=int, default=3)
        parser.add_argument('--apply', action='store_true')

    @transaction.atomic
    def handle(self, *args, **options):
        form = NegativeRuleForm({**{key: options[key] for key in FIELDS if key != 'min_redemptions'}, 'min_redemptions': 100})
        if not form.is_valid():
            raise CommandError('Проверьте пороги: ' + '; '.join(str(e) for errors in form.errors.values() for e in errors))
        data = form.cleaned_data
        accounts = list(Account.objects.select_for_update(of=('self',), no_key=True).select_related('company')
                        .filter(name__in=options['account']).order_by('pk'))
        if {a.name for a in accounts} != set(options['account']):
            raise CommandError('Не все кабинеты найдены. Изменений нет.')
        old = {row.account_id: row for row in ControlDefaults.objects.filter(account__in=accounts)}
        operations = []
        for account in accounts:
            policy = ControlDefaults(account=account, **data)
            try: policy.full_clean(exclude=['id', 'account'])
            except ValidationError as error: raise CommandError(f'{account.name}: {error}') from error
            if account.company.is_demo or account.platform not in {'wb', 'ym'}:
                raise CommandError('Выберите только реальные кабинеты WB и Маркета. Изменений нет.')
            existing = old.get(account.pk)
            changed = not existing or any(getattr(existing, key) != data[key] for key in FIELDS)
            operations.append((account, changed))
            self.stdout.write(f'{account.name}: предупреждение от {data["warning"]:g}, расследование от {data["threshold"]:g} на 100 выкупов; '
                              f'минимум {data["min_signals"]} жалобы; серия {data["cluster_count"]} за {data["cluster_days"]} дн.'
                              + (' Будет сохранено.' if changed else ' Уже настроено.'))
        codes = ProductMapping.objects.filter(account__in=accounts).values('product__code')
        overrides = Baseline.objects.filter(product__code__in=codes, signal='NEGATIVE').count()
        self.stdout.write(f'Отдельных правил товаров сохранится без изменений: {overrides}. '
                          'Общее правило применяется к товарам без своего правила, включая новые сопоставления.')
        if not options['apply']:
            self.stdout.write('Просмотр. Для сохранения добавьте --apply. AI, отправки и расследования не запускаются.')
            return
        if not any(changed for _, changed in operations):
            self.stdout.write('Настройки уже совпадают. Изменений нет.')
            return
        before = list(ControlDefaults.objects.filter(account__in=accounts).values())
        backup = settings.STATE_DIR / 'backups' / ('control-before-' + timezone.now().strftime('%Y%m%dT%H%M%S%fZ') + '.json')
        try:
            backup.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            with open(backup, 'x', encoding='utf-8', opener=lambda path, flags: os.open(path, flags, 0o600)) as stream:
                json.dump({'format': 'voice-control-defaults-1', 'accounts': [a.pk for a in accounts],
                           'before': before, 'after': data}, stream, ensure_ascii=False, indent=2, default=str)
        except OSError as error:
            raise CommandError('Не удалось сохранить резервную копию. Настройки не изменены.') from error
        for account, changed in operations:
            if not changed: continue
            previous = old.get(account.pk)
            policy, _ = ControlDefaults.objects.update_or_create(account=account, defaults=data)
            Audit.objects.create(company=account.company, action='control.defaults', object_id=str(policy.pk),
                before={key: getattr(previous, key) for key in FIELDS} if previous else {},
                after={**data, 'account': account.pk, 'backup': str(backup)})
        self.stdout.write(f'Пороги сохранены. Резервная копия: {backup}')
        self.stdout.write('Фоновый опрос применит правила. Неполные данные остаются неполными; AI-задачи и исходящие ответы не создавались.')
