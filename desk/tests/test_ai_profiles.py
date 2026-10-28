import json
import tempfile
import uuid
from io import StringIO
from pathlib import Path
from unittest.mock import patch
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import override_settings
from django.urls import reverse
from desk.ai.context import prepare
from desk.ai.documents import extract_voice
from desk.messaging.service import enqueue, process_one
from desk.models import AIJob, AIRequest, Account, Audit, Company, Outgoing
from .base import DeskCase


class AIProfileTests(DeskCase):
    def setUp(self):
        super().setUp()
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.voice = self.root / 'voice.md'; self.voice.write_text('На Вы, с обязательным приветствием и подписью.')
        self.knowledge = self.root / 'knowledge.md'; self.knowledge.write_text('[ALL] Правила компании.\n\n[WB] Правило WB.\n\n[YM] Правило Маркета.')
        self.company.legal_entity = 'АМ'; self.company.ai_model = 'preserve-model'; self.company.ai_key_env = 'PRESERVE_KEY_BINDING'
        self.company.save()
        self.args = {'legal_entity': 'АМ', 'account': [self.account.name, self.ym.name],
                     'voice': self.voice, 'knowledge': self.knowledge, 'stdout': StringIO()}

    def run_import(self, **kwargs):
        options = {**self.args, **kwargs}
        names = options.pop('account')
        account_args = [arg for name in names for arg in ('--account', name)]
        with override_settings(STATE_DIR=self.root):
            call_command('import_ai_profile', *account_args, **options)

    def test_preview_is_free_and_does_not_change_profiles_or_create_backups(self):
        self.run_import()
        self.company.refresh_from_db()
        self.assertEqual(self.company.tone_of_voice, 'PROFILE_A_ONLY')
        self.assertFalse((self.root / 'backups').exists())
        self.assertFalse(AIJob.objects.exists()); self.assertFalse(AIRequest.objects.exists())

    def test_apply_is_scoped_audited_idempotent_and_preserves_model_settings(self):
        self.run_import(apply=True)
        self.company.refresh_from_db(); self.other.refresh_from_db()
        self.assertEqual(self.company.tone_of_voice, self.voice.read_text())
        self.assertEqual(self.company.knowledge, self.knowledge.read_text())
        self.assertEqual(self.company.ai_model, 'preserve-model')
        self.assertEqual(self.company.ai_key_env, 'PRESERVE_KEY_BINDING')
        self.assertFalse(self.company.ai_auto_enabled)
        self.assertEqual(self.other.tone_of_voice, 'PROFILE_B_PRIVATE')
        backup = next((self.root / 'backups').glob('profile-before-*.json'))
        self.assertEqual(backup.stat().st_mode & 0o777, 0o600)
        self.assertEqual(json.loads(backup.read_text())['profiles'][0]['tone_of_voice'], 'PROFILE_A_ONLY')
        self.assertEqual(Audit.objects.filter(action='ai.profile.import').count(), 1)
        self.run_import(apply=True)
        self.assertEqual(Audit.objects.filter(action='ai.profile.import').count(), 1)
        self.assertEqual(len(list((self.root / 'backups').glob('*.json'))), 1)

    def test_missing_account_or_wrong_entity_rolls_back_all_targets(self):
        for changes in [{'account': [self.account.name, 'missing']}, {'legal_entity': 'ОП'}, {'account': [self.account.name, self.other_account.name]}]:
            with self.subTest(changes=changes), self.assertRaises(CommandError): self.run_import(apply=True, **changes)
        self.company.refresh_from_db(); self.assertEqual(self.company.tone_of_voice, 'PROFILE_A_ONLY')
        self.assertFalse(Audit.objects.filter(action='ai.profile.import').exists())

    def test_shared_company_requires_all_accounts_to_be_explicit(self):
        with self.assertRaises(CommandError): self.run_import(apply=True, account=[self.account.name])
        self.company.refresh_from_db(); self.assertEqual(self.company.tone_of_voice, 'PROFILE_A_ONLY')

    def test_two_distinct_am_companies_are_updated_together(self):
        extra = Company.objects.create(name='AM second', legal_entity='АМ', knowledge='old-knowledge')
        account = Account.objects.create(name='AM2', company=extra, platform='ym', token_env='TEST_YM2')
        self.run_import(apply=True, account=[self.account.name, self.ym.name, account.name])
        self.company.refresh_from_db(); extra.refresh_from_db()
        self.assertEqual(extra.knowledge, self.company.knowledge)
        self.assertEqual(Audit.objects.filter(action='ai.profile.import').count(), 2)

    def test_backup_failure_does_not_write_the_database(self):
        (self.root / 'backups').write_text('existing-file')
        with self.assertRaises(CommandError): self.run_import(apply=True)
        self.company.refresh_from_db(); self.assertEqual(self.company.tone_of_voice, 'PROFILE_A_ONLY')
        self.assertFalse(Audit.objects.filter(action='ai.profile.import').exists())

    def test_oversized_non_utf8_and_nul_files_are_rejected_without_changes(self):
        for data in [b'\xff', b'a\x00b', ('x' * 16001).encode(), b'']:
            self.voice.write_bytes(data)
            with self.subTest(data=data[:15]), self.assertRaises(CommandError): self.run_import(apply=True)
        self.company.refresh_from_db(); self.assertEqual(self.company.tone_of_voice, 'PROFILE_A_ONLY')

    def test_missing_local_file_does_not_change_a_profile(self):
        with self.assertRaises(CommandError): self.run_import(apply=True, voice=self.root / 'missing.md')

    def test_knowledge_markers_filter_wrong_platform_even_with_identical_words(self):
        self.company.knowledge = '[WB] Возврат доставка WB_ONLY.\n\n[YM] Возврат доставка YM_ONLY.\n\n[ALL] Возврат доставка COMMON.'
        self.company.save(); self.thread.text = 'Возврат доставка'; self.thread.save()
        _, _, wb = prepare(self.thread)
        self.assertEqual(wb['source']['platform'], 'wb')
        self.assertEqual(wb['source']['channel'], 'Wildberries'); self.assertTrue(wb['source']['is_public'])
        wb_text = json.dumps(wb['facts'], ensure_ascii=False)
        self.assertIn('WB_ONLY', wb_text); self.assertNotIn('YM_ONLY', wb_text); self.assertIn('COMMON', wb_text)
        self.thread.account = self.ym; self.thread.save()
        _, _, ym = prepare(self.thread)
        ym_text = json.dumps(ym['facts'], ensure_ascii=False)
        self.assertIn('YM_ONLY', ym_text); self.assertNotIn('WB_ONLY', ym_text)

    def test_knowledge_document_accepts_24000_characters_and_style_keeps_16000_limit(self):
        body = ('Знание ' * 2700).encode()
        self.assertGreater(len(body.decode()), 16000)
        self.assertEqual(extract_voice(SimpleUploadedFile('knowledge.md', body), max_chars=24000), body.decode().strip())
        with self.assertRaises(ValueError): extract_voice(SimpleUploadedFile('style.md', body))

    def test_tone_form_uploads_both_files_and_only_selected_company(self):
        self.user.is_superuser = True; self.user.save(); self.client.force_login(self.user)
        response = self.client.post(reverse('tone'), {'company': self.company.pk,
            'voice_file': SimpleUploadedFile('voice.md', 'Здравствуйте, на Вы'.encode()),
            'knowledge_file': SimpleUploadedFile('knowledge.md', '[ALL] Только подтверждённые факты'.encode())})
        self.assertEqual(response.status_code, 302)
        self.company.refresh_from_db(); self.other.refresh_from_db()
        self.assertEqual(self.company.tone_of_voice, 'Здравствуйте, на Вы')
        self.assertEqual(self.company.knowledge, '[ALL] Только подтверждённые факты')
        self.assertEqual(self.other.tone_of_voice, 'PROFILE_B_PRIVATE')
        self.assertFalse(AIJob.objects.exists())

    def test_bad_knowledge_upload_does_not_partially_save_style(self):
        self.user.is_superuser = True; self.user.save(); self.client.force_login(self.user)
        self.client.post(reverse('tone'), {'company': self.company.pk,
            'voice_file': SimpleUploadedFile('voice.md', b'NEW STYLE'),
            'knowledge_file': SimpleUploadedFile('knowledge.exe', b'invalid file format')})
        self.company.refresh_from_db(); self.assertEqual(self.company.tone_of_voice, 'PROFILE_A_ONLY')

    def test_operator_cannot_change_company_profile(self):
        self.client.force_login(self.user)
        response = self.client.post(reverse('tone'), {'company': self.company.pk,
            'voice_file': SimpleUploadedFile('voice.md', b'NEW STYLE'),
            'knowledge_file': SimpleUploadedFile('knowledge.md', b'NEW FACTS')})
        self.assertEqual(response.status_code, 403)

    def test_unfilled_signature_cannot_be_enqueued_but_real_name_can(self):
        for placeholder in ['{{имя_сотрудника}}', '[имя сотрудника]', '[ФИО]']:
            with self.subTest(placeholder=placeholder), self.assertRaises(ValueError):
                enqueue(self.thread, self.user, f'Здравствуйте! Отдел заботы, {placeholder}', uuid.uuid4())
        self.assertFalse(Outgoing.objects.exists())
        item = enqueue(self.thread, self.user, 'Здравствуйте! Отдел заботы о клиентах, 1st ORIGINAL, Анна', uuid.uuid4())
        self.assertEqual(item.status, 'Queued')

    @patch('desk.messaging.service.get_adapter')
    def test_worker_rejects_old_queued_template_before_contacting_marketplace(self, adapter):
        item = Outgoing.objects.create(thread=self.thread, sender=self.user, text='Здравствуйте! {{имя_сотрудника}}', request_key=uuid.uuid4())
        self.assertTrue(process_one()); adapter.assert_not_called()
        item.refresh_from_db(); self.assertEqual(item.status, 'Failed'); self.assertTrue(item.retry_safe)
