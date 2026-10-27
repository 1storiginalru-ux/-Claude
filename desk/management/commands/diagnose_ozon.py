import os
from datetime import timedelta
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone
from desk.integrations.base import IntegrationError
from desk.integrations.ozon import Ozon
from desk.models import Account, ProductMapping, Thread


class Command(BaseCommand):
    help = 'Проверить настройку Ozon. --probe делает один запрос без изменения обращений.'

    def add_arguments(self, parser):
        parser.add_argument('--account',required=True)
        parser.add_argument('--probe',action='store_true')
        parser.add_argument('--kind',choices=['review','question'],default='review')

    def handle(self,*args,**options):
        account=Account.objects.select_related('company').filter(name=options['account'],platform='ozon').first()
        if not account: raise CommandError('Кабинет Ozon не найден.')
        self.stdout.write(f'Кабинет: {account.name}. Юрлицо: {account.company.name}. Включён: {account.enabled}.')
        self.stdout.write(f'Client-Id: {"задан" if account.ozon_client_id else "не задан"}. API-ключ: {"задан" if os.environ.get(account.token_env) else "не задан"}.')
        self.stdout.write(f'Сопоставлений: {ProductMapping.objects.filter(account=account).count()}. Обращений: {Thread.objects.filter(account=account).count()}.')
        if not options['probe']:
            self.stdout.write('Запрос API не отправлялся. Для одного проверочного запроса добавьте --probe.')
            return
        now=timezone.now();since=now-timedelta(days=7)
        if options['kind']=='review':
            path,key='/v2/review/list','reviews'
            body={'limit':20,'sort_dir':'DESC','filters':{'status':'ALL','order_status':'ALL','published_from':since.isoformat(),'published_to':now.isoformat()}}
        else:
            path,key='/v1/question/list','questions'
            body={'limit':20,'sort_dir':'DESC','filter':{'status':'ALL','date_from':since.isoformat(),'date_to':now.isoformat()}}
        try: response=Ozon(account).request('POST',path,json=body)
        except IntegrationError as error: raise CommandError(str(error)) from error
        rows=response.get(key) if isinstance(response,dict) else None
        if not isinstance(rows,list): raise CommandError('Ozon ответил в неожиданном формате; полнота не подтверждена.')
        self.stdout.write(f'Ozon принял запрос {path}. В проверочном ответе: {len(rows)} объектов.')
        self.stdout.write('Позиции загрузки и обращения не изменены. Действуют общие ограничения API.')
