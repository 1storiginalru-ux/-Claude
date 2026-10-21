import io
import os
import zipfile
from unittest.mock import Mock, patch
from django.core.files.uploadedfile import SimpleUploadedFile
from desk.ai.documents import extract_voice
from desk.incidents.service import escalate, sync_bitrix
from .base import DeskCase


class DocumentTests(DeskCase):
    def test_txt_and_markdown(self):
        for name in ['voice.txt', 'voice.md']:
            self.assertEqual(extract_voice(SimpleUploadedFile(name, 'Уважительно и по делу'.encode())), 'Уважительно и по делу')

    def test_docx_extracts_paragraphs(self):
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, 'w') as archive:
            archive.writestr('word/document.xml', '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>Голос бренда</w:t></w:r></w:p></w:body></w:document>')
        self.assertEqual(extract_voice(SimpleUploadedFile('voice.docx', stream.getvalue())), 'Голос бренда')

    def test_empty_pdf_requires_ocr_or_text(self):
        from pypdf import PdfWriter
        writer = PdfWriter(); writer.add_blank_page(width=100, height=100)
        stream = io.BytesIO(); writer.write(stream)
        with self.assertRaises(ValueError): extract_voice(SimpleUploadedFile('scan.pdf', stream.getvalue()))

    def test_document_size_and_type_limits(self):
        for name, body in [('file.exe', b'hello'), ('voice.txt', b'a' * 16001)]:
            with self.assertRaises(ValueError): extract_voice(SimpleUploadedFile(name, body))


@patch.dict(os.environ, {'VOC_BITRIX_DOMAIN': 'example.bitrix24.ru', 'VOC_BITRIX_TOKEN': 'test-only-token', 'VOC_BITRIX_OWNER_ID': '1', 'VOC_BITRIX_NAMESPACE': 'unit-test'})
class BitrixTests(DeskCase):
    def response(self, data):
        response = Mock(status_code=200); response.json.return_value = data
        return response

    @patch('desk.incidents.service.requests.post')
    def test_create_then_update_same_task(self, post):
        item = escalate(self.thread, 'LONG')
        post.side_effect = [self.response({'result': {'tasks': []}}), self.response({'result': {'task': {'id': 42}}}), self.response({'result': {}})]
        sync_bitrix(item); self.assertEqual(item.bitrix_id, '42')
        sync_bitrix(item)
        self.assertIn('tasks.task.update', post.call_args.args[0])
        self.assertEqual(post.call_args.kwargs['json']['taskId'], '42')

    @patch('desk.incidents.service.requests.post')
    def test_existing_external_marker_is_adopted_instead_of_duplicate(self, post):
        item = escalate(self.thread, 'LONG')
        post.side_effect = [self.response({'result': {'tasks': [{'id': 84}]}}), self.response({'result': {}})]
        sync_bitrix(item)
        self.assertEqual(item.bitrix_id, '84')
        self.assertFalse(any('tasks.task.add' in call.args[0] for call in post.call_args_list))

    @patch('desk.incidents.service.requests.post')
    def test_timeout_followed_by_unresolved_lookup_cannot_create_again(self, post):
        import requests
        item = escalate(self.thread, 'LONG')
        post.side_effect = [self.response({'result': {'tasks': []}}), requests.ReadTimeout()]
        with self.assertRaises(ValueError): sync_bitrix(item)
        post.reset_mock(); post.side_effect = [self.response({'result': {'tasks': []}})]
        with self.assertRaises(ValueError): sync_bitrix(item)
        self.assertEqual(post.call_count, 1)
