import os
from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from desk.models import Account, Audit


class Command(BaseCommand):
    help = 'Настроить публичные идентификаторы кабинета и включить его после сохранения ключа.'

    def add_arguments(self, parser):
        parser.add_argument('--account',required=True)
        parser.add_argument('--business-id')
        parser.add_argument('--client-id')
        group=parser.add_mutually_exclusive_group()
        group.add_argument('--enable',action='store_true')
        group.add_argument('--disable',action='store_true')

    @transaction.atomic
    def handle(self,*args,**options):
        account=Account.objects.select_for_update().filter(name=options['account']).select_related('company').first()
        if not account: raise CommandError('Кабинет не найден.')
        before={'enabled':account.enabled,'business_id':account.business_id,'ozon_client_id':account.ozon_client_id}
        for option,platform,field in [('business_id','ym','business_id'),('client_id','ozon','ozon_client_id')]:
            value=options.get(option)
            if value is not None:
                if account.platform!=platform or not value.isdecimal() or not value.isascii():
                    raise CommandError('Идентификатор должен быть числовым и соответствовать площадке кабинета.')
                setattr(account,field,value)
        if options['enable']:
            if not os.environ.get(account.token_env): raise CommandError(f'Сначала сохраните ключ: voice-set-key {account.token_env}')
            if account.platform=='ym' and not account.business_id.isdigit(): raise CommandError('Для Маркета нужен --business-id.')
            account.enabled=True
        if options['disable']: account.enabled=False
        try: account.full_clean()
        except ValidationError as error: raise CommandError('; '.join(error.messages)) from error
        after={key:getattr(account,key) for key in before}
        if before!=after:
            account.save(update_fields=list(before))
            Audit.objects.create(company=account.company,action='account.configure',object_id=str(account.pk),before=before,after=after)
        self.stdout.write(f'{account.name}: {"включён" if account.enabled else "отключён"}. Ключ не выводится. Для применения окружения перезапустите службы.')
