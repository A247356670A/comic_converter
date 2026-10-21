import json
from pathlib import Path
import tempfile
import unittest

from copy_manga_update import Chapter
from decimal import Decimal
from source_page_audit import (apply_source_page_deletions, format_pages,
                               plan_source_page_audit)


class FakeClient:
    def __init__(self, catalog, page_counts):
        self.catalog_rows = catalog
        self.page_counts = page_counts

    def catalog(self, _slug, **_kwargs):
        return self.catalog_rows

    def pages(self, _slug, chapter):
        return [f'https://img/{index}' for index in range(self.page_counts[chapter.id])]


class SourcePageAuditTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.root = Path(self.temporary.name)
        self.library = self.root / 'Manga'
        self.series = self.library / '作品'
        self.chapter = self.series / '第0001.000话'
        self.chapter.mkdir(parents=True)
        self.mapping = self.root / 'copy_sources.json'
        self.mapping.write_text(json.dumps({str(self.series): 'work'}), encoding='utf-8')
        self.catalog = [Chapter(Decimal(1), 'chapter-id', '第0001.000话', 'default')]

    def tearDown(self):
        self.temporary.cleanup()

    def test_formats_detailed_page_ranges(self):
        self.assertEqual(format_pages([1, 2, 3, 7, 9, 10]), '001–003、007、009–010')

    def test_missing_pages_become_unselected_candidate(self):
        for number in (1, 2, 4, 7):
            (self.chapter / f'{number:03d}.png').write_bytes(b'image')
        report = plan_source_page_audit(
            self.library, self.mapping, client=FakeClient(self.catalog, {'chapter-id': 7}))
        self.assertEqual(len(report['candidates']), 1)
        row = report['candidates'][0]
        self.assertFalse(row['selected'])
        self.assertEqual(row['missing_pages'], [3, 5, 6])
        self.assertEqual(row['missing_text'], '003、005–006')

    def test_missing_url_skips_without_web_calls(self):
        self.mapping.write_text('{}', encoding='utf-8')
        report = plan_source_page_audit(self.library, self.mapping,
                                        client=FakeClient([], {}))
        self.assertFalse(report['candidates'])
        self.assertIn('没有对应网址', report['skipped'][0]['reason'])

    def test_non_numeric_pages_are_skipped(self):
        (self.chapter / 'cover.png').write_bytes(b'image')
        report = plan_source_page_audit(
            self.library, self.mapping, client=FakeClient(self.catalog, {'chapter-id': 3}))
        self.assertFalse(report['candidates'])
        self.assertIn('非纯数字', report['skipped'][0]['reason'])

    def test_only_explicit_rows_delete_folder_and_zip(self):
        for number in (1, 3):
            (self.chapter / f'{number:03d}.png').write_bytes(b'image')
        archive = self.chapter.with_name(self.chapter.name + '.zip')
        archive.write_bytes(b'zip')
        report = plan_source_page_audit(
            self.library, self.mapping, client=FakeClient(self.catalog, {'chapter-id': 3}))
        results, log = apply_source_page_deletions(
            report['candidates'], self.library, self.root / 'logs')
        self.assertEqual(results[0]['status'], '已删除')
        self.assertFalse(self.chapter.exists())
        self.assertFalse(archive.exists())
        self.assertTrue(log.is_file())

    def test_changed_folder_is_never_deleted(self):
        (self.chapter / '001.png').write_bytes(b'image')
        report = plan_source_page_audit(
            self.library, self.mapping, client=FakeClient(self.catalog, {'chapter-id': 2}))
        (self.chapter / '002.png').write_bytes(b'new')
        results, _ = apply_source_page_deletions(
            report['candidates'], self.library, self.root / 'logs')
        self.assertEqual(results[0]['status'], '失败')
        self.assertTrue(self.chapter.exists())


if __name__ == '__main__':
    unittest.main()
