import os
from django.core.management.base import BaseCommand, CommandError
from desk.models import Company
from desk.ai.client import list_yandex_models, complete, ProviderProblem


class Command(BaseCommand):
    help = 'Проверка AI без вывода ключей. --probe отправляет один небольшой платный тестовый запрос.'

    def add_arguments(self, parser):
        parser.add_argument('--company')
        parser.add_argument('--folder-id')
        parser.add_argument('--key-env', default='VOC_YANDEX_AI_KEY')
        parser.add_argument('--list-models', action='store_true')
        parser.add_argument('--probe', action='store_true')

    def handle(self, *args, **o):
        company = Company.objects.filter(name=o['company'], is_demo=False).first() if o['company'] else None
        if o['company'] and not company: raise CommandError('Реальная компания не найдена.')
        folder = o['folder_id'] or (company.ai_folder_id if company else '')
        key_env = company.ai_key_env if company else o['key_env']
        self.stdout.write(f'Ключ {key_env}: {"задан" if os.environ.get(key_env) else "не задан"}.')
        if company:
            self.stdout.write(f'{company.name}: {company.get_ai_provider_display()}; модель {company.ai_model or "не задана"}; автоанализ {company.ai_auto_enabled}.')
            self.stdout.write(f'Профиль общения: {bool(company.tone_of_voice.strip())}; знания: {bool(company.knowledge.strip())}.')
        try:
            if o['list_models']:
                if company and company.ai_provider != 'yandex': raise CommandError('Список моделей Яндекса требует отдельного ключа Яндекса. Укажите --folder-id и --key-env без --company.')
                for model in list_yandex_models(folder, key_env): self.stdout.write(model)
            if o['probe']:
                if not company: raise CommandError('Для --probe укажите --company.')
                if company.ai_provider == 'none': raise CommandError('AI не подключён.')
                snap = {'provider': company.ai_provider, 'model': company.ai_model, 'folder': folder, 'key_env': key_env}
                raw, counts, stopped = complete(snap, 'Return only JSON: {"ok": true}.', {'test': 'connection'}, 32)
                import json
                if not stopped or json.loads(raw) != {'ok': True}: raise CommandError('API ответил, но тестовый формат не подтверждён.')
                self.stdout.write(f'API доступен, JSON подтверждён. Токены вход/выход: {counts or "не сообщены"}.')
                self.stdout.write('Это синтетический тест; отзывы и классификации не изменены. Пробный запрос не входит в локальный лимит очереди.')
        except (ProviderProblem, ValueError) as error: raise CommandError(str(error)) from error
