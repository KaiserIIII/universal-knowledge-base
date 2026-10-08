"""Local parser selection, receipts and tenant-scoped file workflow evidence."""
import csv
import io
import tempfile
import unittest
from pathlib import Path
from tests.support import ApiTestCase


class ParserTests(unittest.TestCase):
    def test_html_honors_selected_chinese_encoding(self):
        from tests.test_knowledge import AdapterTests
        from app.parsing import ParserOptions
        processor = AdapterTests().processor()
        processor.parser_options = ParserOptions(text_encoding='gb18030')
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'terms.html'
            path.write_bytes('<p>合同条款</p><script>hidden</script>'.encode('gb18030'))
            result = processor._parse_file(str(path), path.name)
            self.assertIn('合同条款', result)
            self.assertNotIn('hidden', result)
    def test_delimited_parser_preserves_chinese_and_limits_rows(self):
        from app.parsing import ParserOptions, parse_tabular
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'table.csv'
            path.write_bytes('姓名,条款\n张三,两年\n李四,三年\n'.encode('gb18030'))
            result = parse_tabular(path, '.csv', ParserOptions(max_rows=2))
            self.assertIn('张三', result)
            self.assertIn('两年', result)
            self.assertNotIn('李四', result)
            self.assertIn('truncated', result)

    def test_explicit_encoding_and_unsupported_selection_fail_closed(self):
        from app.parsing import ParserOptions, decode_text, validate_selection
        self.assertEqual(decode_text('合同'.encode('gb18030'), 'gb18030'), '合同')
        with self.assertRaises(ValueError):
            decode_text(b'\xff\xfe\x00', 'utf-8')
        with self.assertRaises(ValueError):
            validate_selection('handbook.pdf', ParserOptions(mode='text'))


class ParserApiTests(ApiTestCase):
    async def test_changed_source_is_rejected_before_parsing(self):
        from app.saas.ingestion_models import IngestionJob
        await self.register('receipt@example.test')
        kb = (await self.client.post('/api/v1/knowledge-bases', json={'name': 'Receipts'})).json()['id']
        upload = await self.client.post(f'/api/v1/kb/{kb}/documents', files={'files': ('receipt.txt', b'original')})
        doc = upload.json()['documents'][0]['id']
        async with self.app.state.session_factory() as db:
            from sqlalchemy import select
            job = await db.scalar(select(IngestionJob).where(IngestionJob.doc_id == doc))
            Path(job.source_path).write_bytes(b'changed')
        self.retriever.chunks = [{'content': 'changed'}]
        await self.app.state.ingestion_worker.drain()
        result = (await self.client.get(f'/api/v1/kb/{kb}/documents/{doc}')).json()
        self.assertEqual(result['status'], 'failed')
        self.assertEqual((await self.client.get(f'/api/v1/kb/{kb}/documents/{doc}/chunks')).json(), [])

    async def test_parser_settings_reach_durable_import_and_preserve_receipt(self):
        await self.register('parser@example.test')
        response = await self.client.post('/api/v1/knowledge-bases', json={
            'name': 'Files', 'parser_config': {'mode': 'native', 'text_encoding': 'gb18030', 'max_rows': 100}})
        self.assertEqual(response.status_code, 201, response.text)
        kb = response.json()['id']
        captured = []
        async def parse(**kwargs):
            captured.append(kwargs)
            return [{'content': '合同条款'}]
        self.retriever.parse = parse
        upload = await self.client.post(f'/api/v1/kb/{kb}/documents', files={'files': ('terms.txt', '合同'.encode('gb18030'))})
        self.assertEqual(upload.status_code, 202)
        doc = upload.json()['documents'][0]['id']
        await self.app.state.ingestion_worker.drain()
        self.assertEqual(captured[0]['parser_config']['mode'], 'native')
        chunks = (await self.client.get(f'/api/v1/kb/{kb}/documents/{doc}/chunks')).json()
        self.assertEqual(chunks[0]['content'], '合同条款')
        self.assertIn('source_sha256', chunks[0]['metadata'])
        self.assertEqual(len(chunks[0]['metadata']['source_sha256']), 64)
        catalog = await self.client.get('/api/v1/parsers')
        self.assertEqual(catalog.status_code, 200)
        self.assertIn('native', [row['id'] for row in catalog.json()['parsers']])
