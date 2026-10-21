import uuid
from unittest.mock import patch
from django.contrib.auth import get_user_model
from django.test import Client
from desk.models import Membership, Outgoing
from .base import DeskCase


class WebTests(DeskCase):
    def setUp(self):
        super().setUp()
        self.client.force_login(self.user)

    def test_login_is_required_and_real_login_works(self):
        self.client.logout()
        self.assertRedirects(self.client.get('/'), '/login/?next=/', fetch_redirect_response=False)
        self.assertTrue(self.client.login(username='operator', password='test-password-only'))
        self.assertContains(self.client.get('/'), 'Единый Inbox')

    def test_all_workflow_pages_render(self):
        self.analysis(); self.sales(); self.baseline()
        for url in ['/', '/?negative=1', '/?severity=P2', '/products/', f'/products/{self.product.pk}/', '/voc/', '/imports/', '/integrations/', '/tone/', f'/threads/{self.thread.pk}/']:
            with self.subTest(url=url): self.assertEqual(self.client.get(url).status_code, 200)

    def test_cross_company_read_and_write_are_denied(self):
        other = self.make_thread('other', account=self.other_account, product=None, customer='PRIVATE_CUSTOMER_B')
        self.assertNotContains(self.client.get('/'), 'PRIVATE_CUSTOMER_B')
        self.assertNotContains(self.client.get('/tone/'), 'PROFILE_B_PRIVATE')
        self.assertEqual(self.client.get(f'/threads/{other.pk}/').status_code, 404)
        self.assertEqual(self.client.post(f'/threads/{other.pk}/action/', {'action': 'status', 'status': 'Закрыто'}).status_code, 404)

    def test_viewer_cannot_send_change_status_or_modify_tone(self):
        Membership.objects.filter(user=self.user).update(role='viewer')
        for data in [{'action': 'send', 'text': 'Ответ', 'request_key': str(uuid.uuid4())}, {'action': 'status', 'status': 'Закрыто'}, {'action': 'analyze'}]:
            self.assertEqual(self.client.post(f'/threads/{self.thread.pk}/action/', data).status_code, 403)
        self.assertEqual(self.client.post('/tone/', {'company': self.company.pk, 'tone_of_voice': 'x'}).status_code, 403)
        self.assertEqual(Outgoing.objects.count(), 0)

    def test_post_is_csrf_protected_and_get_cannot_mutate(self):
        c = Client(enforce_csrf_checks=True); c.force_login(self.user)
        url = f'/threads/{self.thread.pk}/action/'
        self.assertEqual(c.post(url, {'action': 'status', 'status': 'Закрыто'}).status_code, 403)
        self.assertEqual(self.client.get(url).status_code, 405)

    def test_operator_submission_retains_final_text_and_draft(self):
        analysis = self.analysis()
        response = self.client.post(f'/threads/{self.thread.pk}/action/', {'action': 'send', 'text': 'Ответ после правок', 'request_key': str(uuid.uuid4())})
        self.assertEqual(response.status_code, 302)
        item = Outgoing.objects.get()
        self.assertEqual(item.text, 'Ответ после правок'); self.assertEqual(item.ai_draft, analysis.draft)

    def test_customer_html_is_escaped(self):
        self.thread.text = '<script>alert("x")</script>'; self.thread.save()
        self.assertNotContains(self.client.get('/'), '<script>alert(')
        self.assertContains(self.client.get('/'), '&lt;script&gt;')

    def test_staff_cannot_open_global_admin(self):
        self.user.is_staff = True; self.user.save()
        self.assertEqual(self.client.get('/admin/desk/company/').status_code, 302)
