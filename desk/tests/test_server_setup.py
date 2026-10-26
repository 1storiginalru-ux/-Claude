import json
from io import StringIO
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace
from unittest.mock import patch
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import SimpleTestCase, TestCase
from deploy.environment import read_environment
from desk.models import Company


class EnvironmentTests(SimpleTestCase):
    def test_values_are_literal_and_not_executed_as_shell(self):
        with tempfile.TemporaryDirectory() as directory:
            file = Path(directory) / 'voice.env'
            file.write_text('# comment\nPGDATABASE=voice\nKEY="literal $(command) `command` $HOME"\nEMPTY=\n')
            self.assertEqual(read_environment(file), {'PGDATABASE': 'voice', 'KEY': 'literal $(command) `command` $HOME', 'EMPTY': ''})

    def test_unsupported_syntax_does_not_expose_secret_values_in_errors(self):
        with tempfile.TemporaryDirectory() as directory:
            file = Path(directory) / 'voice.env'
            for value in ['bad-key=private-secret', 'KEY=private-secret more']:
                file.write_text(value)
                with self.subTest(value=value), self.assertRaises(ValueError) as error:
                    read_environment(file)
                self.assertNotIn('private-secret', str(error.exception))


class BackupTests(SimpleTestCase):
    def test_sqlite_backup_can_be_restored_and_is_private(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / 'source.sqlite3'
            with sqlite3.connect(source) as connection:
                connection.execute('CREATE TABLE example (value TEXT)')
                connection.execute("INSERT INTO example VALUES ('retained')")
            databases = {'default': {'ENGINE': 'django.db.backends.sqlite3', 'NAME': source}}
            with patch('desk.management.commands.backup_db.settings', SimpleNamespace(STATE_DIR=Path(directory), DATABASES=databases)):
                call_command('backup_db', stdout=StringIO())
            backup = next((Path(directory) / 'backups').glob('*.sqlite3'))
            with sqlite3.connect(backup) as restored:
                self.assertEqual(restored.execute('SELECT value FROM example').fetchone()[0], 'retained')
            self.assertEqual(backup.stat().st_mode & 0o777, 0o600)

    def test_failed_pg_dump_cleans_up_partial_backup_without_logging_password(self):
        with tempfile.TemporaryDirectory() as directory:
            databases = {'default': {'ENGINE': 'django.db.backends.postgresql', 'NAME': 'voice', 'PASSWORD': 'private-secret'}}
            with patch('desk.management.commands.backup_db.settings', SimpleNamespace(STATE_DIR=Path(directory), DATABASES=databases)), patch('desk.management.commands.backup_db.subprocess.run', side_effect=OSError('not found')):
                with self.assertRaises(CommandError) as error:
                    call_command('backup_db', stdout=StringIO())
            self.assertNotIn('private-secret', str(error.exception))
            self.assertEqual(list((Path(directory) / 'backups').iterdir()), [])


class TransferTests(TestCase):
    def test_existing_data_blocks_import_without_changes(self):
        company = Company.objects.create(name='Keep this company')
        with self.assertRaises(CommandError):
            call_command('import_transfer', '/does-not-exist.json', stdout=StringIO())
        self.assertTrue(Company.objects.filter(pk=company.pk).exists())

    def test_export_preserves_company_and_never_overwrites_existing_file(self):
        company = Company.objects.create(name='Компания для переноса', tone_of_voice='Сохранить стиль')
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / 'transfer.json'
            call_command('export_transfer', output=output, stdout=StringIO())
            objects = json.loads(output.read_text())
            item = next(item for item in objects if item['model'] == 'desk.company')
            self.assertEqual(item['pk'], company.pk)
            self.assertEqual(item['fields']['tone_of_voice'], 'Сохранить стиль')
            self.assertFalse(any(item['model'] in {'auth.permission', 'contenttypes.contenttype', 'sessions.session'} for item in objects))
            self.assertEqual(output.stat().st_mode & 0o777, 0o600)
            original = output.read_bytes()
            with self.assertRaises(CommandError):
                call_command('export_transfer', output=output, stdout=StringIO())
            self.assertEqual(output.read_bytes(), original)

    def test_import_into_empty_database_preserves_source_ids(self):
        with tempfile.TemporaryDirectory() as directory:
            file = Path(directory) / 'transfer.json'
            file.write_text(json.dumps([{'model': 'desk.company', 'pk': 73, 'fields': {'name': 'Transferred', 'is_demo': False}}]))
            call_command('import_transfer', str(file), stdout=StringIO())
        self.assertEqual(Company.objects.get(pk=73).name, 'Transferred')
