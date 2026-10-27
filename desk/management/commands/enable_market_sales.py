import os
from django.core.management.base import BaseCommand, CommandError
from django.core.exceptions import ValidationError
from django.db import transaction
from desk.models import Account,Audit


class Command(BaseCommand):
    help='Включить продажи ЯМ_1, ЯМ_2, ЯМ_3; Velato не меняется. По умолчанию только проверка.'

    def add_arguments(self,parser):parser.add_argument('--apply',action='store_true')

    @transaction.atomic
    def handle(self,*args,**options):
        selected=[('ЯМ_1','АМ'),('ЯМ_2','ЕС'),('ЯМ_3','ОП')]
        seen=set()
        for name,legal_entity in selected:
            account=Account.objects.select_for_update().select_related('company').filter(name=name,platform='ym',company__legal_entity=legal_entity,company__is_demo=False).first()
            if not account or not account.enabled: raise CommandError(f'Сначала подключите реальный кабинет {name} юрлица {legal_entity}.')
            if account.business_id in seen: raise CommandError('Несколько кабинетов относятся к одному Business ID. Продажи нельзя учитывать повторно.')
            seen.add(account.business_id)
            if not os.environ.get(account.sales_token_env or account.token_env):raise CommandError(f'Не задан ключ продаж для {name}.')
            before=account.sales_enabled
            account.sales_enabled=True
            try:account.full_clean()
            except ValidationError as error:raise CommandError('; '.join(error.messages)) from error
            self.stdout.write(f'{name} · {legal_entity}: продажи {"уже включены" if before else "готовы к включению"}; права API проверит опрос.')
            if options['apply'] and not before:
                account.save(update_fields=['sales_enabled'])
                Audit.objects.create(company=account.company,action='sales.enable',object_id=str(account.pk),before={'sales_enabled':False},after={'sales_enabled':True,'platform':'ym'})
        self.stdout.write('Настройки сохранены. Velato не изменён.' if options['apply'] else 'Проверка завершена. Для включения добавьте --apply.')
