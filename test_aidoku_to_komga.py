import io
from pathlib import Path
import tempfile
import unittest
import zipfile
from PIL import Image
from aidoku_to_komga import Resolver, scan, convert, safe_name, chapter_name, HeaderParser


class ConverterTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.source = self.root / 'input' / 'comic-slug'
        self.source.mkdir(parents=True)
        self.output = self.root / 'output'

    def archive(self, bad=False, xml=True):
        p = self.source / 'chapter-id.cbz'
        with zipfile.ZipFile(p, 'w') as z:
            if xml:
                z.writestr('ComicInfo.xml', '<ComicInfo><Series>漫画</Series><Title>第1話试看</Title></ComicInfo>')
            for n, color in [(10, 'blue'), (2, 'red'), (1, 'green')]:
                data = io.BytesIO()
                Image.new('RGB', (10, 12), color).save(data, 'PNG')
                z.writestr(f'{n}.webp', b'broken' if bad and n == 2 else data.getvalue())
        return p

    def rows(self):
        return scan(self.source.parent, self.output, Resolver(online=False), lambda _: None)

    def test_order_png_idempotence_and_modified_output(self):
        p = self.archive()
        row = self.rows()[0]
        self.assertEqual(row['chapter'], '第01话试看')
        self.assertEqual(convert(row)['status'], '已完成')
        target = Path(row['target'])
        with Image.open(target / '001.png') as im:
            self.assertEqual(im.format, 'PNG')
            self.assertEqual(im.getpixel((0, 0)), (0, 128, 0))
        self.assertEqual(convert(row)['status'], '已跳过（校验一致）')
        (target / '001.png').write_bytes(b'changed')
        self.assertEqual(convert(row)['status'], '失败')
        self.assertTrue(p.exists())

    def test_corrupt_image_leaves_no_chapter(self):
        self.archive(bad=True)
        row = self.rows()[0]
        self.assertEqual(convert(row)['status'], '失败')
        self.assertFalse(Path(row['target']).exists())
        self.assertEqual(list(self.output.rglob('.converting-*')), [])

    def test_existing_directory_never_overwritten(self):
        self.archive()
        row = self.rows()[0]
        target = Path(row['target'])
        target.mkdir(parents=True)
        (target / 'old.txt').write_text('keep')
        self.assertEqual(convert(row)['status'], '失败')
        self.assertEqual((target / 'old.txt').read_text(), 'keep')

    def test_traversal_rejected(self):
        p = self.archive()
        with zipfile.ZipFile(p, 'a') as z:
            z.writestr('../escape.png', b'bad')
        self.assertEqual(self.rows()[0]['status'], '失败')

    def test_missing_metadata_offline(self):
        self.archive(xml=False)
        self.assertEqual(self.rows()[0]['status'], '失败')

    def test_source_changed_after_preview(self):
        p = self.archive()
        row = self.rows()[0]
        with zipfile.ZipFile(p, 'a') as z:
            z.writestr('notes.txt', 'changed')
        self.assertEqual(convert(row)['status'], '失败')

    def test_duplicate_chapter(self):
        p = self.archive()
        (self.source / 'duplicate.cbz').write_bytes(p.read_bytes())
        self.assertEqual(sum(r['status'] == '失败' for r in self.rows()), 1)

    def test_cancel(self):
        self.archive()
        row = self.rows()[0]
        self.assertEqual(convert(row, lambda: True)['status'], '已取消')
        self.assertFalse(self.output.exists())

    def test_names_and_page_parser(self):
        self.assertEqual(safe_name('CON'), '_CON')
        self.assertEqual(chapter_name('第1.5話 番外'), '第01.5话 番外')
        self.assertEqual(chapter_name('第5卷'), '005')
        self.assertEqual(chapter_name('第010卷'), '010')
        self.assertEqual(chapter_name('第2024卷'), '第2024卷')
        self.assertEqual(chapter_name('第1.5卷番外'), '第1.5卷番外')
        p = HeaderParser()
        p.feed('<h4 class="header"><b>漫画</b>/第283话试看</h4>')
        self.assertEqual(''.join(p.parts), '漫画/第283话试看')

    def test_nested_output_rejected(self):
        with self.assertRaises(ValueError):
            scan(self.source.parent, self.source / 'out', Resolver(online=False))

    def test_sortable_name_reuses_legacy_directory(self):
        self.archive()
        rows = scan(self.source.parent, self.output, Resolver(online=False, sortable=True), lambda _: None)
        self.assertEqual(rows[0]['chapter'], '第0001.000话试看')
        legacy = self.output / '漫画' / '第01话试看'
        legacy.mkdir(parents=True)
        rows = scan(self.source.parent, self.output, Resolver(online=False, sortable=True), lambda _: None)
        self.assertEqual(Path(rows[0]['target']), legacy)
        self.assertEqual(convert(rows[0])['status'], '失败')


if __name__ == '__main__':
    unittest.main()
