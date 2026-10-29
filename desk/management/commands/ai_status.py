from django.core.management.base import BaseCommand, CommandError
from django.db.models import Count, Sum, Q
from django.utils import timezone
from desk.models import Company, AIJob, AIRequest


class Command(BaseCommand):
    help = 'Очередь AI и расход токенов, без сетевых запросов.'

    def add_arguments(self, parser): parser.add_argument('--company', required=True)

    def handle(self, *args, **o):
        company = Company.objects.filter(name=o['company'], is_demo=False).first()
        if not company: raise CommandError('Компания не найдена.')
        calls = AIRequest.objects.filter(company=company, month=timezone.localdate().replace(day=1))
        totals = calls.aggregate(input=Sum('input_tokens'), output=Sum('output_tokens'), budget=Sum('budget_tokens'))
        self.stdout.write(f'{company.name}: {company.get_ai_provider_display()}, автоматический анализ: {company.ai_auto_enabled}.')
        self.stdout.write(f'Токены по usage: вход {totals["input"] or 0}, выход {totals["output"] or 0}.')
        self.stdout.write(f'В лимите учтено {totals["budget"] or 0} / {company.ai_token_limit}; сюда входят консервативные резервы запросов с неизвестным расходом.')
        jobs = AIJob.objects.filter(Q(thread__account__company=company)|Q(test_case__account__company=company))
        self.stdout.write(str(list(jobs.values('status').annotate(count=Count('pk')).order_by('status'))))
        for job in jobs.exclude(error='').order_by('-updated_at')[:5]: self.stdout.write(f'Задача {job.pk}: {job.get_status_display()}; {job.error}')
