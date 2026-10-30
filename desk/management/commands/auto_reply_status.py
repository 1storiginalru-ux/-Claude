import os
from django.core.management.base import BaseCommand, CommandError
from django.db.models import Q
from desk.models import Account, ReplyMaterial, ReplyRule


class Command(BaseCommand):
    help='Проверить включение новых черновиков по кабинетам, без запросов к AI.'
    def add_arguments(self,p):p.add_argument('--account',action='append',required=True)
    def handle(self,*args,**o):
        accounts=list(Account.objects.filter(name__in=o['account'],company__is_demo=False).select_related('company').order_by('pk'))
        if {a.name for a in accounts}!=set(o['account']):raise CommandError('Не все кабинеты найдены.')
        for a in accounts:
            c=a.company
            auto=a.enabled and a.ai_auto_enabled and c.ai_auto_enabled and c.ai_provider!='none'
            materials=ReplyMaterial.objects.filter(company=c,approved=True,active=True).filter(Q(platform='')|Q(platform=a.platform))
            rules=ReplyRule.objects.filter(company=c,active=True,material__in=materials).filter(Q(accounts=a)|Q(accounts__isnull=True)).distinct()
            self.stdout.write(f'{a.name} · {c.legal_entity}: новые отзывы → черновики {"ВКЛ" if auto else "ВЫКЛ"}; {c.get_ai_provider_display()}; ключ {"задан" if os.environ.get(c.ai_key_env) else "не задан"}.')
            self.stdout.write(f'  Материалов подтверждено и включено: {materials.count()}; сценариев с ними: {rules.count()}; негатив: {"шаблон / сотрудник" if c.reply_sensitive_templates else "общие правила"}.')
            self.stdout.write(f'  Модель: {c.ai_model or "не задана"}. Лимит компании: {c.ai_token_limit} токенов/месяц. Подпись: {a.reply_signature or "не задана"}.')
