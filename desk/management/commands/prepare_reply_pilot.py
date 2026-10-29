from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from desk.models import Thread, ReplyTestCase, Audit
from desk.replies.service import case_sections

class Command(BaseCommand):
    help='Скопировать реальные обращения в изолированный пилот. Без AI и изменения Inbox, по умолчанию просмотр.'
    def add_arguments(self,p):
        p.add_argument('--legal-entity',required=True);p.add_argument('--limit',type=int,default=30);p.add_argument('--apply',action='store_true')
    @transaction.atomic
    def handle(self,*args,**o):
        if not 1<=o['limit']<=50: raise CommandError('Размер пилота от 1 до 50.')
        base=Thread.objects.filter(account__company__legal_entity=o['legal_entity'],account__company__is_demo=False,account__enabled=True).select_related('account__company','product').order_by('-received_at','-pk')
        # Round-robin keeps both platforms, low ratings, positive and questions.
        groups=[iter(base.filter(kind='review',account__platform=platform,rating__in=ratings)[:o['limit']]) for platform in ['wb','ym','ozon'] for ratings in [[1,2,3],[4,5]]]
        groups.append(iter(base.filter(kind='question')[:o['limit']]))
        selected=[]
        while groups and len(selected)<o['limit']:
            remaining=[]
            for group in groups:
                t=next(group,None)
                if t is not None:
                    selected.append(t);remaining.append(group)
                    if len(selected)>=o['limit']:break
            groups=remaining
        count=0
        for t in selected:
            self.stdout.write(f'{t.pk}: {t.account.name}, {t.kind}, {t.rating or "—"} ★, ProductID {t.product.code if t.product else "—"}')
            if o['apply']:
                _,created=ReplyTestCase.objects.get_or_create(source_thread=t,defaults={'account':t.account,'product':t.product,'title':f'Обращение №{t.pk}','kind':t.kind,'text':t.text,'rating':t.rating,'review_sections':case_sections(t)})
                count+=created
        if o['apply']:
            for company_id in {t.account.company_id for t in selected}: Audit.objects.create(company_id=company_id,action='reply.pilot.prepared',after={'selected':[t.pk for t in selected if t.account.company_id==company_id]})
        self.stdout.write(f'Выбрано: {len(selected)}. Создано случаев: {count}.' if o['apply'] else f'Выбрано: {len(selected)}. Для создания добавьте --apply.')
        self.stdout.write('AI и отправки не запускались. Рабочие обращения и их анализ не изменялись.')
