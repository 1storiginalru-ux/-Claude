from datetime import timedelta
from unittest.mock import patch
from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone
from desk.models import Account, Analysis, Baseline, Company, Membership, Product, ProductMapping, SalesDaily, Thread


class DeskCase(TestCase):
    def setUp(self):
        # Tests cannot accidentally send any external customer communication.
        self.network = patch('requests.sessions.Session.request', side_effect=AssertionError('Unexpected network access in tests'))
        self.network.start(); self.addCleanup(self.network.stop)
        self.company = Company.objects.create(name='Компания A', legal_entity='ООО A', tone_of_voice='PROFILE_A_ONLY')
        self.other = Company.objects.create(name='Компания B', legal_entity='ООО B', tone_of_voice='PROFILE_B_PRIVATE')
        self.user = get_user_model().objects.create_user('operator', password='test-password-only')
        Membership.objects.create(user=self.user, company=self.company, role='operator')
        self.account = Account.objects.create(name='WB1', company=self.company, platform='wb', token_env='TEST_WB_TOKEN', enabled=True)
        self.ym = Account.objects.create(name='YM1', company=self.company, platform='ym', token_env='TEST_YM_TOKEN', business_id='123', enabled=True)
        self.other_account = Account.objects.create(name='Other', company=self.other, platform='ym', token_env='TEST_OTHER_TOKEN')
        self.product = Product.objects.create(company=self.company, code='P1', name='Парфюм', brand='Brand', volume='100', concentration='EDT')
        for a in [self.account, self.ym]: ProductMapping.objects.create(product=self.product, account=a, sku='sku1')
        self.thread = self.make_thread('review-1')

    def make_thread(self, external_id, **kwargs):
        data = {'account': self.account, 'external_id': external_id, 'kind': 'review', 'sku': 'sku1', 'product': self.product, 'text': 'Аромат исчезает через час', 'rating': 5, 'received_at': timezone.now() - timedelta(minutes=10), 'due_at': timezone.now() + timedelta(hours=1)}
        data.update(kwargs)
        return Thread.objects.create(**data)

    def analysis(self, thread=None, **kwargs):
        data = {'sentiment': 'negative', 'hidden_negative': True, 'signals': ['LONG'], 'severity': 'P2', 'objective_issue': False, 'requires_human': True, 'confidence': 0.95, 'summary': 'Проблема стойкости', 'draft': 'Здравствуйте! Уточните, пожалуйста, условия использования.', 'provider': 'test'}
        data.update(kwargs)
        return Analysis.objects.create(thread=thread or self.thread, **data)

    def sales(self, count=100, account=None, days_ago=0):
        return SalesDaily.objects.create(account=account or self.account, sku='sku1', date=timezone.localdate() - timedelta(days=days_ago), redemptions=count)

    def baseline(self, **kwargs):
        data = {'product': self.product, 'signal': 'LONG', 'low': 2, 'high': 5, 'cluster_count': 100}
        data.update(kwargs)
        return Baseline.objects.create(**data)
