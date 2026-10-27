"""Explicit, idempotent provisioning. Never runs during an empty DB transfer."""
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from desk.models import Account, Audit, Company

STRUCTURE = (
    ('ЕС', (('ВБ_2', 'wb', 'VOC_WB_2_TOKEN'), ('ЯМ_2', 'ym', 'VOC_YM_2_TOKEN'), ('Ozon_2', 'ozon', 'VOC_OZON_2_TOKEN'))),
    ('ОП', (('ВБ_3', 'wb', 'VOC_WB_3_TOKEN'), ('ЯМ_3', 'ym', 'VOC_YM_3_TOKEN'))),
    ('Velato', (('ВБ_Velato', 'wb', 'VOC_WB_VELATO_TOKEN'), ('ЯМ_Velato', 'ym', 'VOC_YM_VELATO_TOKEN'), ('Ozon_Velato', 'ozon', 'VOC_OZON_VELATO_TOKEN'))),
)


class Command(BaseCommand):
    help = 'Подготовить ЕС, ОП, Velato и 8 отключённых кабинетов. По умолчанию только проверка.'

    def add_arguments(self, parser): parser.add_argument('--apply', action='store_true')

    @transaction.atomic
    def handle(self, *args, **options):
        for name, accounts in STRUCTURE:
            companies=list(Company.objects.select_for_update().filter(name=name))
            if len(companies)>1 or (companies and (companies[0].is_demo or companies[0].legal_entity!=name)):
                raise CommandError(f'Конфликт компании {name}. Существующие данные не изменены.')
            company=companies[0] if companies else None
            if company is None and options['apply']:
                company=Company.objects.create(name=name,legal_entity=name)
                Audit.objects.create(company=company,action='company.provision',object_id=str(company.pk),after={'name':name})
            for account_name, platform, token_env in accounts:
                existing=Account.objects.select_for_update().filter(name=account_name).first()
                if existing and (not company or existing.company_id!=company.pk or existing.platform!=platform):
                    raise CommandError(f'Конфликт кабинета {account_name}. Существующие данные не изменены.')
                if existing:
                    self.stdout.write(f'{name} · {account_name}: уже существует, настройки сохранены.')
                else:
                    self.stdout.write(f'{name} · {account_name}: подготовлен, подключение отдельно, переменная {token_env}.')
                    if options['apply']:
                        account=Account.objects.create(company=company,name=account_name,platform=platform,token_env=token_env,enabled=False)
                        Audit.objects.create(company=company,action='account.provision',object_id=str(account.pk),after={'name':account_name,'platform':platform,'enabled':False})
        self.stdout.write('Готово. Ключи и идентификаторы вводятся отдельно; права сотрудников автоматически не расширяются.' if options['apply'] else 'Проверка завершена. Для создания добавьте --apply.')
