import json
import uuid
from unittest.mock import patch
from django.urls import reverse
from desk.ai.context import PIPELINE_VERSION, prepare
from desk.ai.queue import enqueue, process_one
from desk.messaging.service import enqueue as enqueue_reply
from desk.models import Analysis, Message, Outgoing
from desk.reviews import effective_analysis
from .test_ai_queue import AIConfiguredCase, NEGATIVE, POSITIVE


class DraftingTests(AIConfiguredCase):
    @patch('desk.ai.queue.complete')
    def test_negative_human_only_includes_style_facts_and_separate_operator_notes(self, complete):
        self.thread.text = 'Не стойкий, хочу вернуть'; self.thread.save()
        self.company.tone_of_voice = 'НОТА: конкретный опыт, позиция, действие, сопровождение.'
        self.company.knowledge = '[ALL] Дополнительная гарантия: безоговорочный возврат при сомнениях в течение трёх рабочих дней.\n\n[WB] Wildberries: профиль → «Заказы» → «Возвраты».'
        self.company.save()
        draft = 'Здравствуйте! Вы пишете, что аромат быстро исчезает. {{проверенный_следующий_шаг}}. Напишем Вам {{подтверждённый_срок_контакта}}.'
        def respond(snapshot, system, context, max_tokens):
            if context.get('classification'):
                return self.answer({'draft': draft, 'used_fact_ids': [context['verified_facts'][0]['id']],
                                    'operator_notes': ['Проверьте дату получения и применимость гарантии.']})
            return self.answer(NEGATIVE)
        complete.side_effect = respond
        job = enqueue(self.thread, self.user); process_one(); job.refresh_from_db()
        self.assertEqual(job.status, 'done')
        result = Analysis.objects.get(thread=self.thread)
        self.assertTrue(result.requires_human); self.assertEqual(result.draft, draft)
        self.assertIn('Проверьте дату получения и применимость гарантии.', result.evidence['operator_notes'])
        self.assertEqual(result.evidence['version'], PIPELINE_VERSION)
        self.assertIn('НОТА', complete.call_args.args[2]['tone_of_voice'])
        self.assertEqual(complete.call_args.args[2]['review']['platform'], 'wb')
        self.assertFalse(Outgoing.objects.exists())
        self.client.force_login(self.user)
        page = self.client.get(reverse('thread', args=[self.thread.pk]))
        self.assertContains(page, 'Требуется проверка сотрудником')
        self.assertContains(page, 'Перед отправкой — только для сотрудника')
        textarea = page.content.decode().split('<textarea name="text"', 1)[1].split('</textarea>', 1)[0]
        self.assertIn('проверенный_следующий_шаг', textarea)
        self.assertNotIn('Проверьте дату получения', textarea)

    @patch('desk.ai.queue.complete')
    def test_each_sensitive_category_including_p0_and_serious_defect_can_have_draft(self, complete):
        for signals, severity in [(['LONG'], 'P2'), (['AUTH-UNKNOWN'], 'P1'), (['RETURN'], 'P2'),
                                  (['MARK'], 'P1'), (['DEFECT'], 'P1'), (['OTHER'], 'P0')]:
            with self.subTest(signals=signals, severity=severity):
                self.company.ai_model += 'x'; self.company.save()
                self.responses(complete, classification={**NEGATIVE, 'signals': signals, 'severity': severity})
                enqueue(self.thread); process_one()
                result = Analysis.objects.get(thread=self.thread)
                self.assertTrue(result.draft); self.assertTrue(result.requires_human)
                self.assertFalse(Outgoing.objects.exists())

    @patch('desk.ai.queue.complete')
    def test_uncertain_or_model_flagged_result_still_gets_cautious_draft(self, complete):
        self.responses(complete, classification={**POSITIVE, 'requires_human': False, 'confidence': 0.4})
        enqueue(self.thread); process_one()
        result = Analysis.objects.get(thread=self.thread)
        self.assertEqual(result.confidence, 0.4); self.assertTrue(result.draft); self.assertTrue(result.requires_human)
        self.assertTrue(any('неоднозначна' in note for note in result.evidence['operator_notes']))
        self.assertFalse(Outgoing.objects.exists())

    @patch('desk.ai.queue.complete')
    def test_current_operator_decision_is_passed_but_not_changed_by_ai(self, complete):
        human = self.assess('negative', ['LONG'])
        self.responses(complete)
        enqueue(self.thread); process_one()
        decision = complete.call_args.args[2]['operator_assessment']
        self.assertEqual(decision['decision'], 'negative'); self.assertEqual(decision['signals'], ['LONG'])
        human.refresh_from_db(); self.assertEqual(human.decision, 'negative')
        self.thread.refresh_from_db(); self.assertEqual(effective_analysis(self.thread).provider, 'human')
        self.assertTrue(Analysis.objects.get(thread=self.thread).draft)

    @patch('desk.ai.queue.complete')
    def test_operator_edit_during_generation_cancels_outdated_context(self, complete):
        human = self.assess('negative', ['LONG'])
        def respond(*args):
            human.signals = ['PACK']; human.save()
            return self.answer(POSITIVE)
        complete.side_effect = respond
        job = enqueue(self.thread); process_one(); job.refresh_from_db()
        self.assertEqual(job.status, 'cancelled'); self.assertFalse(Analysis.objects.exists())

    @patch('desk.ai.queue.complete')
    def test_style_change_hides_previous_draft_and_requeues_new_version(self, complete):
        self.responses(complete, draft={'draft': 'OLD_STYLE_REPLY', 'used_fact_ids': []})
        old = enqueue(self.thread); process_one()
        self.company.tone_of_voice = 'NEW_NOTA_STYLE'; self.company.save()
        self.client.force_login(self.user)
        response = self.client.get(reverse('thread', args=[self.thread.pk]))
        self.assertNotContains(response, 'OLD_STYLE_REPLY')
        self.assertContains(response, 'Стиль общения, знания или решение сотрудника изменились')
        new = enqueue(self.thread); self.assertNotEqual(old.pk, new.pk)

    def test_short_longevity_complaint_retrieves_guarantee_and_correct_marketplace_route(self):
        self.thread.text = 'Не стойкий'; self.thread.save()
        self.company.knowledge = '\n\n'.join([
            '[ALL] Источник документа: только для сотрудников.',
            '[ALL] Компания продаёт только оригинальную парфюмерию.',
            '[ALL] Дополнительная гарантия: безоговорочный возврат при сомнениях.',
            '[ALL] Допустимы вскрытие и распыление.',
            '[WB] Wildberries: профиль → «Заказы» → «Возвраты». WB_ROUTE',
            '[YM] Яндекс Маркет: выбирает заказ и «Вернуть заказ». YM_ROUTE',
            '[ALL] Правила будущих проектов.',
        ])
        self.company.save()
        for account, wanted, forbidden in [(self.account, 'WB_ROUTE', 'YM_ROUTE'), (self.ym, 'YM_ROUTE', 'WB_ROUTE')]:
            self.thread.account = account; self.thread.save()
            _, _, snapshot = prepare(self.thread)
            facts = json.dumps(snapshot['facts'], ensure_ascii=False)
            self.assertIn(wanted, facts); self.assertNotIn(forbidden, facts)
            self.assertIn('безоговорочный возврат', facts); self.assertIn('только оригинальную', facts)
            self.assertLessEqual(sum(len(f['text']) for f in snapshot['facts']), 6000)

    def test_return_route_is_selected_even_without_matching_complaint_words(self):
        self.thread.text = 'Не стойкий'; self.thread.account = self.ym; self.thread.save()
        self.company.knowledge = '\n\n'.join([
            '[ALL] Дополнительная гарантия: безоговорочный возврат при сомнениях.',
            '[ALL] По гарантии допустимы вскрытие и распыление.',
            '[ALL] Компания продаёт только оригинальную парфюмерию.',
            '[ALL] Стойкость и оригинальность требуют внимания сотрудника.',
            '[ALL] Сомнения в качестве не повод для спора.',
            '[ALL] Дополнительная гарантия охватывает все кабинеты.',
            '[YM] Покупатель открывает «Заказы», выбирает заказ и «Вернуть заказ». YM_ROUTE',
        ])
        self.company.save()
        _, _, snapshot = prepare(self.thread)
        self.assertIn('YM_ROUTE', json.dumps(snapshot['facts'], ensure_ascii=False))
        self.assertEqual(len(snapshot['facts']), 6)

    def test_attachments_and_order_presence_are_available_without_pii_or_images(self):
        self.thread.account = self.ym
        self.thread.source_data = {'identifiers': {'orderId': 'PRIVATE_ORDER_NUMBER'},
                                   'media': {'photos': ['https://example.com/private-buyer-photo.jpg']}}
        self.thread.save()
        _, _, snapshot = prepare(self.thread)
        self.assertEqual(snapshot['available_materials'], {'photos': 1, 'videos': 0,
                         'images_analyzed': False, 'order_reference_available': True})
        payload = json.dumps(snapshot)
        self.assertNotIn('PRIVATE_ORDER_NUMBER', payload); self.assertNotIn('private-buyer-photo', payload)

    @patch('desk.ai.queue.complete')
    def test_new_message_cancels_queued_reply_with_outdated_history(self, complete):
        job = enqueue(self.thread)
        Message.objects.create(thread=self.thread, direction='in', text='Добавлю: возврат уже оформлен')
        process_one(); job.refresh_from_db()
        self.assertEqual(job.status, 'cancelled'); complete.assert_not_called()

    @patch('desk.ai.queue.complete')
    def test_notes_invalid_format_or_internal_note_in_customer_text_is_not_saved(self, complete):
        for draft in [
            {'draft': '', 'used_fact_ids': []},
            {'draft': 'Здравствуйте!', 'used_fact_ids': [], 'operator_notes': 'не список'},
            {'draft': 'Здравствуйте! не подтверждено — уточнить у ответственного', 'used_fact_ids': []},
        ]:
            with self.subTest(draft=draft):
                self.company.ai_model += 'x'; self.company.save()
                self.responses(complete, draft=draft)
                job = enqueue(self.thread); process_one(); job.refresh_from_db()
                self.assertEqual(job.status, 'retry'); self.assertTrue(job.classification)
                self.assertEqual(Analysis.objects.get(thread=self.thread).draft, '')

    def test_customer_send_blocks_all_nota_placeholders_and_internal_missing_data_note(self):
        for text in ['{{подтверждённый_срок_контакта}}', '{{проверенный приватный канал}}',
                     '{{' + 'x' * 200 + '}}', 'не подтверждено — уточнить у ответственного']:
            with self.subTest(text=text), self.assertRaises(ValueError):
                enqueue_reply(self.thread, self.user, 'Здравствуйте! ' + text, uuid.uuid4())
        self.assertFalse(Outgoing.objects.exists())

    @patch('desk.ai.queue.complete')
    def test_generated_operator_notes_are_escaped_and_not_copied_into_customer_reply(self, complete):
        self.responses(complete, draft={'draft': 'Здравствуйте! Спасибо за отзыв.', 'used_fact_ids': [],
                                       'operator_notes': ['<script>INTERNAL_ONLY</script>']})
        enqueue(self.thread); process_one()
        self.client.force_login(self.user)
        page = self.client.get(reverse('thread', args=[self.thread.pk]))
        self.assertContains(page, '&lt;script&gt;INTERNAL_ONLY&lt;/script&gt;')
        self.assertNotContains(page, '<script>INTERNAL_ONLY</script>')
        textarea = page.content.decode().split('<textarea name="text"', 1)[1].split('</textarea>', 1)[0]
        self.assertNotIn('INTERNAL_ONLY', textarea)
        self.assertFalse(Outgoing.objects.exists())
