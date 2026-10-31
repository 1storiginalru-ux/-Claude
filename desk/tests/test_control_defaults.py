import json
import stat
import tempfile
from io import StringIO
from pathlib import Path
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import override_settings
from django.test.utils import CaptureQueriesContext
from django.db import connection
from desk.models import Account, AIJob, AIRequest, Audit, Baseline, ControlDefaults, Incident, Outgoing, Product, ProductMapping
from desk.incidents.service import recheck_products
from desk.voc.dataset import ControlDataset
from desk.voc.rules import evaluate
from .test_review_control import ReviewControlCase


class ControlDefaultsTests(ReviewControlCase):
    def defaults(self, account=None, **changes):
        return ControlDefaults.objects.create(account=account or self.account, **changes)

    def test_disabled_until_explicitly_configured_and_applies_to_future_mappings(self):
        self.assertIsNone(evaluate(self.product, 'NEGATIVE')['baseline'])
        self.defaults()
        rule = evaluate(self.product, 'NEGATIVE')['baseline']
        self.assertEqual((rule.low, rule.high, rule.min_redemptions, rule.min_signals, rule.cluster_count, rule.cluster_days), (5, 10, 100, 3, 4, 3))
        product = Product.objects.create(company=self.company, code='FUTURE', name='Новый товар')
        ProductMapping.objects.create(account=self.account, product=product, sku='future')
        self.assertEqual(evaluate(product, 'NEGATIVE')['baseline'], rule)
        self.assertFalse(Baseline.objects.exists())

    def test_product_override_has_priority_and_other_themes_are_unchanged(self):
        self.defaults()
        override = self.rule(low=2, high=7)
        self.assertEqual(evaluate(self.product, 'NEGATIVE')['baseline'], override)
        self.assertIsNone(evaluate(self.product, 'LONG_AUTH')['baseline'])
        dataset = ControlDataset([self.product], self.company.__class__.objects.filter(pk=self.company.pk))
        self.assertEqual(dataset.baseline(self.product.code, 'NEGATIVE'), override)
        self.assertEqual(dataset.baseline(self.product.code, 'NEGATIVE', self.account.pk), override)

    def test_fallback_matches_batched_calculation_and_does_not_leak_to_another_company(self):
        self.events(); self.analysis(); self.defaults()
        other_product = Product.objects.create(company=self.other, code=self.product.code, name='PRIVATE')
        ProductMapping.objects.create(account=self.other_account, product=other_product, sku='private')
        self.defaults(self.other_account, warning=1, threshold=2)
        scope = self.company.__class__.objects.filter(pk=self.company.pk)
        dataset = ControlDataset([self.product], scope, now=self.now)
        for account_id in [None, self.account.pk]:
            individual = evaluate(self.product, 'NEGATIVE', now=self.now, scope=scope, account_id=account_id)
            batched = evaluate(self.product, 'NEGATIVE', dataset=dataset, account_id=account_id)
            self.assertEqual(individual, batched)
            self.assertEqual(batched['baseline'].high, 10)
        self.assertIsNone(evaluate(self.product, 'NEGATIVE')['baseline'])  # Conflicting combined policies.
        self.assertEqual(evaluate(other_product, 'NEGATIVE', scope=self.company.__class__.objects.filter(pk=self.other.pk))['baseline'].high, 2)

    def test_conflicting_defaults_keep_each_account_rule_without_choosing_a_global_rule(self):
        self.defaults(); self.defaults(self.ym, warning=6, threshold=12)
        ProductMapping.objects.create(account=self.ym, product=self.product, sku='ym-sku')
        dataset = ControlDataset([self.product], self.company.__class__.objects.filter(pk=self.company.pk))
        self.assertTrue(dataset.has_rule(self.product.code, 'NEGATIVE'))
        self.assertIsNone(dataset.baseline(self.product.code, 'NEGATIVE'))
        self.assertEqual(dataset.baseline(self.product.code, 'NEGATIVE', self.account.pk).high, 10)
        self.assertEqual(dataset.baseline(self.product.code, 'NEGATIVE', self.ym.pk).high, 12)

    def test_unselected_account_has_no_fallback_even_when_another_account_has_one(self):
        self.defaults()
        ProductMapping.objects.create(account=self.ym, product=self.product, sku='ym-sku')
        dataset = ControlDataset([self.product], self.company.__class__.objects.filter(pk=self.company.pk))
        self.assertIsNone(evaluate(self.product, 'NEGATIVE', account_id=self.ym.pk)['baseline'])
        self.assertIsNone(evaluate(self.product, 'NEGATIVE', dataset=dataset, account_id=self.ym.pk)['baseline'])

    def test_exact_warning_and_investigation_with_complete_data(self):
        self.events(); self.defaults(cluster_count=100)
        for i in range(5): self.analysis(self.thread if i == 0 else self.make_thread(f'neg-{i}'))
        metric = evaluate(self.product, 'NEGATIVE')
        self.assertEqual((metric['status'], metric['reason'], metric['rate']), ('WATCH', 'Достигнут порог предупреждения', 5))
        for i in range(5, 10): self.analysis(self.make_thread(f'neg-{i}'))
        self.assertEqual(evaluate(self.product, 'NEGATIVE')['status'], 'ANOMALY')
        recheck_products()
        case = Incident.objects.get()
        self.assertEqual((case.signal, case.opening_snapshot['threshold'], case.threads.count()), ('NEGATIVE', 10, 10))

    def test_defaults_cannot_make_incomplete_classification_a_complete_percentage(self):
        self.events(); self.defaults(cluster_count=100)
        for i in range(10): self.analysis(self.thread if i == 0 else self.make_thread(f'neg-{i}'))
        self.make_thread('unclassified')
        metric = evaluate(self.product, 'NEGATIVE')
        self.assertEqual(metric['status'], 'WATCH'); self.assertIsNone(metric['rate'])
        recheck_products(); self.assertFalse(Incident.objects.exists())

    def test_similar_series_can_start_a_check_without_sales(self):
        self.defaults()
        for i in range(4): self.analysis(self.thread if i == 0 else self.make_thread(f'cluster-{i}'))
        recheck_products()
        case = Incident.objects.get()
        self.assertEqual(case.opening_snapshot['trigger']['type'], 'cluster')
        self.assertEqual(case.opening_snapshot['cluster_count'], 4)
        self.assertIsNone(case.opening_snapshot['rate'])

    def test_policy_query_count_does_not_grow_per_product_and_get_does_not_create_cases(self):
        self.defaults(); self.events(); self.analysis()
        with CaptureQueriesContext(connection) as first: self.client.get('/voc/')
        for i in range(6):
            p = Product.objects.create(company=self.company, code=f'N{i}', name=f'Новый {i}')
            ProductMapping.objects.create(account=self.account, product=p, sku=f'future-sku-{i}')
            self.make_thread(f'new-{i}', product=p)
        with CaptureQueriesContext(connection) as many: response = self.client.get('/voc/')
        self.assertLessEqual(len(many), len(first) + 3)
        self.assertContains(response, 'Общее правило кабинетов')
        self.assertNotContains(response, 'Пороги не настроены')
        self.assertFalse(Incident.objects.exists())
        self.assertContains(self.client.get(f'/products/{self.product.pk}/'), 'сохранение формы ниже задаст отдельное правило')


class ConfigureControlTests(ReviewControlCase):
    def test_six_cabinets_of_three_shops_are_configured_without_touching_velato(self):
        self.account.name = 'ВБ_1'; self.account.save()
        self.ym.name = 'ЯМ_1'; self.ym.save()
        names = [self.account.name, self.ym.name]
        for number, entity in [(2, 'ЕС'), (3, 'ОП')]:
            company = self.company.__class__.objects.create(name=entity, legal_entity=entity)
            for prefix, platform in [('ВБ', 'wb'), ('ЯМ', 'ym')]:
                account = Account.objects.create(company=company, name=f'{prefix}_{number}', platform=platform)
                names.append(account.name)
        velato = ControlDefaults.objects.create(account=self.other_account, warning=3, threshold=15)
        before = ControlDefaults.objects.values().get(pk=velato.pk)
        with tempfile.TemporaryDirectory() as folder, override_settings(STATE_DIR=Path(folder)):
            call_command('configure_control', *[arg for name in names for arg in ('--account', name)], apply=True, stdout=StringIO())
        self.assertEqual(ControlDefaults.objects.filter(account__name__in=names).count(), 6)
        self.assertEqual(ControlDefaults.objects.values().get(pk=velato.pk), before)

    def test_preview_and_apply_are_scoped_backed_up_idempotent_and_preserve_overrides(self):
        override = self.rule(low=3, high=8)
        before = Baseline.objects.values().get(pk=override.pk)
        with tempfile.TemporaryDirectory() as folder, override_settings(STATE_DIR=Path(folder)):
            out = StringIO()
            call_command('configure_control', account=[self.account.name], stdout=out)
            self.assertFalse(ControlDefaults.objects.exists()); self.assertFalse(Audit.objects.exists())
            self.assertFalse((Path(folder) / 'backups').exists())
            call_command('configure_control', account=[self.account.name], apply=True, stdout=out)
            call_command('configure_control', account=[self.account.name], apply=True, stdout=out)
            self.assertEqual(ControlDefaults.objects.count(), 1)
            self.assertEqual(Audit.objects.filter(action='control.defaults').count(), 1)
            backups = list((Path(folder) / 'backups').glob('control-before-*.json'))
            self.assertEqual(len(backups), 1)
            self.assertEqual(stat.S_IMODE(backups[0].stat().st_mode), 0o600)
            self.assertEqual(json.loads(backups[0].read_text())['before'], [])
        self.assertEqual(ControlDefaults.objects.get().account_id, self.account.pk)
        self.assertEqual(Baseline.objects.values().get(pk=override.pk), before)
        self.assertFalse(Incident.objects.exists()); self.assertFalse(AIJob.objects.exists())
        self.assertFalse(AIRequest.objects.exists()); self.assertFalse(Outgoing.objects.exists())

    def test_unknown_demo_or_ozon_account_aborts_the_whole_operation(self):
        ozon = Account.objects.create(company=self.company, name='Ozon untouched', platform='ozon')
        demo = self.company.__class__.objects.create(name='Demo', is_demo=True)
        demo_account = Account.objects.create(company=demo, name='Demo WB', platform='wb')
        with tempfile.TemporaryDirectory() as folder, override_settings(STATE_DIR=Path(folder)):
            for name in ['Missing', ozon.name, demo_account.name]:
                with self.subTest(name=name), self.assertRaises(CommandError):
                    call_command('configure_control', '--account', self.account.name, '--account', name, apply=True, stdout=StringIO())
                self.assertFalse(ControlDefaults.objects.exists())
            self.assertFalse((Path(folder) / 'backups').exists())

    def test_invalid_thresholds_do_not_write_and_changed_defaults_keep_a_backup(self):
        with tempfile.TemporaryDirectory() as folder, override_settings(STATE_DIR=Path(folder)):
            for values in [{'warning': 10, 'threshold': 5}, {'warning': float('nan')}, {'threshold': float('inf')}, {'cluster_count': 1}, {'cluster_days': 91}, {'min_signals': 0}]:
                with self.subTest(values=values), self.assertRaises(CommandError):
                    call_command('configure_control', account=[self.account.name], apply=True, stdout=StringIO(), **values)
            self.assertFalse(ControlDefaults.objects.exists())
            call_command('configure_control', account=[self.account.name], apply=True, stdout=StringIO())
            call_command('configure_control', account=[self.account.name], warning=4, threshold=9, apply=True, stdout=StringIO())
            backups = sorted((Path(folder) / 'backups').glob('*.json'))
            self.assertEqual(len(backups), 2)
            self.assertEqual(json.loads(backups[-1].read_text())['before'][0]['threshold'], 10)
            self.assertEqual(ControlDefaults.objects.get().threshold, 9)
