from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from desk.models import Company, ReplyMaterial, Audit
from desk.replies.starters import EXAMPLES

class Command(BaseCommand):
    help='Подготовить неподтверждённые примеры и два шаблона. По умолчанию просмотр, без AI и отправок.'
    def add_arguments(self,parser):
        parser.add_argument('--legal-entity',required=True);parser.add_argument('--apply',action='store_true')
    @transaction.atomic
    def handle(self,*args,**options):
        companies=list(Company.objects.select_for_update(no_key=True).filter(legal_entity=options['legal_entity'],is_demo=False).order_by('pk'))
        if not companies: raise CommandError('Реальные компании юрлица не найдены.')
        self.stdout.write(f'Компаний: {len(companies)}. На каждую: 20 примеров и 2 шаблона, все без подтверждения.')
        if not options['apply']:
            self.stdout.write('Для создания добавьте --apply. Рабочие отзывы и AI не изменяются.');return
        count=0
        for company in companies:
            for title,text,answer,sentiment,topic in EXAMPLES:
                _,created=ReplyMaterial.objects.get_or_create(company=company,kind='example',title='Образец: '+title,defaults={'customer_text':text,'answer':answer,'sentiment':sentiment,'topic':topic,'thread_kind':'question' if text.endswith('?') else 'review','explanation':'Стартовый кандидат. Проверьте формулировку, подпись, область действия и применимость условий. Это пример стиля, не подтверждение фактов.', 'approved':False})
                count+=created
            for rating in [4,5]:
                _,created=ReplyMaterial.objects.get_or_create(company=company,kind='template',title=f'Шаблон: {rating} звёзд без текста',defaults={'answer':'Здравствуйте! Спасибо за оценку!\n\n{{утверждённая_подпись}}','sentiment':'positive','thread_kind':'review','explanation':f'Только оценка {rating}, без текста и жалоб. Создайте сценарий с этими условиями и подтвердите подпись.','approved':False})
                count+=created
            Audit.objects.create(company=company,action='reply.library.seeded',object_id=str(company.pk),after={'approved':False})
        self.stdout.write(f'Создано кандидатов: {count}. Подтвердите нужные в «Ответы и знания → Примеры и шаблоны». AI и отправки не запускались.')
