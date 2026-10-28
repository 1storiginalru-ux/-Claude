import re
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from desk.models import Company, Audit
from desk.ai.client import validate_settings, ProviderProblem


class Command(BaseCommand):
    help = 'Подключить Яндекс для выбранной компании или юрлица. По умолчанию только проверка.'

    def add_arguments(self, parser):
        scope = parser.add_mutually_exclusive_group(required=True)
        scope.add_argument('--company'); scope.add_argument('--legal-entity')
        parser.add_argument('--folder-id', required=True)
        parser.add_argument('--model', required=True, help='Полный gpt:// URI из diagnose_ai --list-models')
        parser.add_argument('--key-env', default='VOC_YANDEX_AI_KEY')
        parser.add_argument('--token-limit', type=int, default=1000000)
        parser.add_argument('--auto', choices=['on', 'off'], default='off')
        parser.add_argument('--apply', action='store_true')

    @transaction.atomic
    def handle(self, *args, **o):
        if not re.fullmatch(r'[A-Z][A-Z0-9_]*', o['key_env']) or not 1 <= o['token_limit'] <= 100000000:
            raise CommandError('Проверьте имя переменной и лимит токенов (1…100000000).')
        if len(o['model']) > 100 or re.search(r'\s', o['model']): raise CommandError('Проверьте URI модели.')
        snapshot = {'provider': 'yandex', 'model': o['model'], 'folder': o['folder_id'], 'key_env': o['key_env']}
        try: validate_settings(snapshot)
        except ProviderProblem as error: raise CommandError(str(error)) from error
        rows = Company.objects.select_for_update().filter(is_demo=False)
        rows = rows.filter(name=o['company']) if o['company'] else rows.filter(legal_entity=o['legal_entity'])
        rows = list(rows)
        if not rows: raise CommandError('Реальная компания не найдена.')
        fields = {'ai_provider': 'yandex', 'ai_model': o['model'], 'ai_folder_id': o['folder_id'], 'ai_key_env': o['key_env'],
                  'ai_auto_enabled': o['auto'] == 'on', 'ai_token_limit': o['token_limit']}
        for company in rows:
            before = {field: getattr(company, field) for field in fields}
            self.stdout.write(f'{company.name}: Яндекс; модель {o["model"]}; автоанализ {fields["ai_auto_enabled"]}; лимит {o["token_limit"]} токенов/мес.')
            if o['apply'] and before != fields:
                for field, value in fields.items(): setattr(company, field, value)
                company.full_clean(); company.save(update_fields=list(fields))
                Audit.objects.create(company=company, action='ai.configure', object_id=str(company.pk), before=before, after=fields)
        self.stdout.write('Настройки сохранены. Существующая история не поставлена на платный анализ.' if o['apply'] else 'Проверка готова. Для сохранения добавьте --apply. Доступ к API ещё не проверялся.')
