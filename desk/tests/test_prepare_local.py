from io import StringIO
from unittest.mock import patch
from django.core.management import call_command
from django.core.management.base import CommandError
from django.contrib.auth import get_user_model
from django.test import TestCase
from desk.models import Company, Product, Thread


class PrepareLocalTests(TestCase):
    def test_fresh_database_is_seeded_only_once_without_default_password(self):
        output = StringIO()
        call_command('prepare_local', no_input=True, stdout=output)
        self.assertEqual((Company.objects.count(), Product.objects.count(), Thread.objects.count()), (1, 3, 12))
        call_command('prepare_local', no_input=True, stdout=output)
        self.assertEqual((Company.objects.count(), Product.objects.count(), Thread.objects.count()), (1, 3, 12))
        self.assertFalse(get_user_model().objects.exists())

    def test_existing_company_and_user_are_preserved_without_demo_or_prompt(self):
        company = Company.objects.create(name='Рабочая компания')
        user = get_user_model().objects.create_user('existing-user')
        with patch('desk.management.commands.prepare_local.call_command') as command:
            call_command('prepare_local', stdout=StringIO())
            command.assert_not_called()
        self.assertEqual(list(Company.objects.all()), [company])
        self.assertEqual(list(get_user_model().objects.all()), [user])

    def test_first_login_cannot_silently_skip_account_creation_without_terminal(self):
        Company.objects.create(name='Рабочая компания')
        with patch('desk.management.commands.prepare_local.sys.stdin.isatty', return_value=False):
            with self.assertRaises(CommandError): call_command('prepare_local', stdout=StringIO())
