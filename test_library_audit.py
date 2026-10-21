from pathlib import Path
import tempfile
import unittest
import json
from PIL import Image
from library_audit import inspect_chapter, inspect_series, sortable_name, rename_plan, apply_renames, scan_library


class AuditTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.series = self.root / '漫画'
        self.series.mkdir()

    def chapter(self, name, numbers=(1, 2, 3)):
        path = self.series / name
        path.mkdir()
        for n in numbers:
            Image.new('RGB', (8, 8)).save(path / f'{n:03}.png')
        return path

    def test_numeric_order(self):
        for name in ['第11话', '第11.5话', '第9话']:
            self.chapter(name)
        row = inspect_series(self.series)
        self.assertEqual(row['latest'], '第11.5话')
        ordered = [sortable_name(n) for n in ['第11话', '第11.5话', '第12话']]
        self.assertEqual(ordered, sorted(ordered))
        self.assertEqual(ordered[0], '第0011.000话')
        self.assertIn('疑似缺话', ' '.join(row['issues']))

    def test_missing_chapters_before_first_folder(self):
        for name in ['第15话', '第16话', '第16.5话番外']:
            self.chapter(name)
        row = inspect_series(self.series)
        self.assertIn('1–14', ' '.join(row['issues']))
        self.assertEqual(row['latest'], '第16.5话番外')

    def test_missing_pages_before_first_file(self):
        result = inspect_chapter(self.chapter('第01话', (15, 16)))
        self.assertIn('1–14', ' '.join(result['issues']))

    def test_gap_damage_zero_bytes(self):
        path = self.chapter('第01话', (1, 3))
        (path / '004.png').write_bytes(b'not image')
        (path / '005.png').touch()
        result = inspect_chapter(path, True)
        issues = '\n'.join(result['issues'])
        self.assertIn('疑似缺页', issues)
        self.assertIn('004.png', issues)
        self.assertIn('零字节', issues)

    def test_manifest_missing_last_page(self):
        path = self.chapter('第01话', (1,))
        (path / '.aidoku-conversion.json').write_text(json.dumps({'pages': {'001.png': 'bad', '002.png': 'missing'}}))
        issues = '\n'.join(inspect_chapter(path, True)['issues'])
        self.assertIn('确认缺页', issues)
        self.assertIn('校验不一致', issues)

    def test_empty_and_unknown(self):
        self.chapter('番外', ())
        row = inspect_series(self.series)
        self.assertEqual(row['latest'], '未知')
        self.assertTrue(row['chapters'][0]['issues'])

    def test_rename_and_conflict(self):
        self.chapter('第11话')
        self.chapter('第11.5话')
        plan = rename_plan(inspect_series(self.series))
        journal = apply_renames(plan, self.root / 'logs')
        self.assertTrue(journal.is_file())
        self.assertTrue((self.series / '第0011.500话' / '001.png').exists())
        self.assertEqual(rename_plan(inspect_series(self.series)), [])
        self.chapter('第11话')
        plan = rename_plan(inspect_series(self.series))
        self.assertTrue(plan[0]['error'])
        with self.assertRaises(ValueError):
            apply_renames(plan, self.root / 'logs')

    def test_precision_and_suffix(self):
        self.assertEqual(sortable_name('第11.5话试看'), '第0011.500话试看')
        self.assertEqual(sortable_name('番外'), '番外')
        with self.assertRaises(ValueError):
            sortable_name('第11.1234话')

    def test_cancel(self):
        self.chapter('第01话')
        result = scan_library(self.root, stop=lambda: True)
        self.assertTrue(result['cancelled'])
        self.assertEqual(result['series'], [])


if __name__ == '__main__':
    unittest.main()
