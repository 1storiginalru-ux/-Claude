"""Import reviewed local profile files; never calls AI or changes credentials."""
import hashlib
import json
import os
from pathlib import Path
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone
from desk.models import Account, Audit, Company


def read_profile(path, limit):
    try:
        with path.open('rb') as stream: raw = stream.read(5 * 1024 * 1024 + 1)
        if len(raw) > 5 * 1024 * 1024: raise CommandError('Файл больше 5 МБ.')
        content = raw.decode('utf-8-sig').replace('\r\n', '\n').strip()
    except (OSError, UnicodeError) as error: raise CommandError('Не удалось прочитать профиль. Нужен локальный текстовый файл UTF-8.') from error
    if not content or '\x00' in content or len(content) > limit:
        raise CommandError(f'Файл профиля должен содержать от 1 до {limit} символов без NUL.')
    return content


class Command(BaseCommand):
    help = 'Загрузить стиль и знания из локальных UTF-8 файлов в явно выбранные кабинеты. По умолчанию просмотр.'

    def add_arguments(self, parser):
        parser.add_argument('--legal-entity', required=True)
        parser.add_argument('--account', action='append', required=True)
        parser.add_argument('--voice', type=Path, required=True)
        parser.add_argument('--knowledge', type=Path, required=True)
        parser.add_argument('--apply', action='store_true')

    @transaction.atomic
    def handle(self, *args, **o):
        voice = read_profile(o['voice'], 16000)
        knowledge = read_profile(o['knowledge'], 24000)
        names = set(o['account'])
        accounts = list(Account.objects.filter(name__in=names, company__is_demo=False,
                        company__legal_entity=o['legal_entity']).select_related('company'))
        if {account.name for account in accounts} != names:
            raise CommandError('Не все кабинеты найдены у указанного реального юрлица. Профили не изменены.')
        companies = list(Company.objects.select_for_update(no_key=True).filter(pk__in={a.company_id for a in accounts}).order_by('pk'))
        # One profile belongs to a company. Never silently affect an unselected
        # account that happens to share that company (e.g. ES with WB and YM).
        if Account.objects.filter(company__in=companies).exclude(name__in=names).exists():
            raise CommandError('Профиль общий для нескольких кабинетов компании. Явно перечислите все её кабинеты через --account.')
        self.stdout.write(f'Стиль: {len(voice)} символов; знания: {len(knowledge)}. Подготовлено компаний: {len(companies)}.')
        self.stdout.write('Кабинеты: ' + ', '.join(sorted(names)))
        fields = {'tone_of_voice': voice, 'knowledge': knowledge}
        before = [{'id': c.pk, 'name': c.name, **{key: getattr(c, key) for key in fields}} for c in companies]
        changed = [c for c in companies if any(getattr(c, key) != value for key, value in fields.items())]
        if not o['apply']:
            self.stdout.write(f'Будут обновлены: {len(changed)}. Для загрузки добавьте --apply. API не вызывается.')
            return
        if not changed:
            self.stdout.write('Эта версия профиля уже загружена. Изменений нет.'); return
        directory = settings.STATE_DIR / 'backups'
        backup = directory / ('profile-before-' + timezone.now().strftime('%Y%m%dT%H%M%S%fZ') + '.json')
        try:
            directory.mkdir(parents=True, exist_ok=True)
            with open(backup, 'x', encoding='utf-8', opener=lambda p, flags: os.open(p, flags, 0o600)) as stream:
                json.dump({'format': 'voice-profile-backup-1', 'profiles': before}, stream, ensure_ascii=False, indent=2)
        except OSError as error: raise CommandError('Не удалось сохранить прежние профили. Импорт отменён.') from error
        hashes = {key: hashlib.sha256(value.encode()).hexdigest() for key, value in fields.items()}
        for company in changed:
            previous = {key: getattr(company, key) for key in fields}
            for key, value in fields.items(): setattr(company, key, value)
            company.save(update_fields=list(fields))
            Audit.objects.create(company=company, action='ai.profile.import', object_id=str(company.pk),
                                 before=previous, after={**fields, 'sha256': hashes})
        self.stdout.write(f'Профили загружены. Предыдущая версия: {backup}')
        self.stdout.write('AI-задачи не создавались; параметры модели и автоанализа сохранены.')
