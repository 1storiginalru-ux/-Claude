from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from desk.models import ReplyTestCase
from desk.replies.service import enqueue_test
from desk.ai.provider import AIProblem

class Command(BaseCommand):
    help='Партия изолированных AI-испытаний. По умолчанию просмотр; --apply ставит платные задачи.'
    def add_arguments(self,p):
        p.add_argument('--legal-entity',required=True);p.add_argument('--limit',type=int,default=30)
        p.add_argument('--mode',choices=['both','baseline','guided'],default='guided');p.add_argument('--apply',action='store_true')
    @transaction.atomic
    def handle(self,*args,**o):
        if not 1<=o['limit']<=50: raise CommandError('Размер партии от 1 до 50.')
        cases=list(ReplyTestCase.objects.filter(account__company__legal_entity=o['legal_entity'],account__company__is_demo=False,account__enabled=True).exclude(account__company__ai_provider='none').select_related('account__company','product').order_by('pk')[:o['limit']])
        modes=['baseline','guided'] if o['mode']=='both' else [o['mode']]
        for case in cases:
            self.stdout.write(f'{case.pk}: {case.account.name}, {case.title}')
            if o['apply']:
                for mode in modes:
                    try:job=enqueue_test(case,None,mode=mode)
                    except AIProblem as error:raise CommandError(str(error)) from error
                    self.stdout.write(f'  {mode}: задача {job.pk}, {job.get_status_display()}')
        self.stdout.write(f'Случаев: {len(cases)}, вариантов на случай: {len(modes)}.')
        self.stdout.write('Платные задачи поставлены в очередь; повторы неизменённых версий переиспользуют результат.' if o['apply'] else 'AI не вызывался. Для постановки платных задач добавьте --apply.')
