from pathlib import Path
import os
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from zip_packager import (apply_zero_zip_cleanup, apply_zip_pack,
                          plan_zero_zip_cleanup, plan_zip_pack, write_journal,
                          zip_signature)


class ZipPackagerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.library = self.root / 'Manga'
        self.chapter = self.library / '漫画' / '第0001.000话'
        self.chapter.mkdir(parents=True)
        (self.chapter / '001.png').write_bytes(b'first image bytes')
        (self.chapter / '002.png').write_bytes(b'second image bytes')
        (self.chapter / '.copy-manga-update.json').write_text('{}', encoding='utf-8')

    def tearDown(self):
        self.temp.cleanup()

    def test_journal_retries_transient_windows_permission_error(self):
        target = self.root / 'journal.json'
        real_replace = os.replace
        calls = []

        def flaky_replace(source, destination):
            calls.append(1)
            if len(calls) < 3:
                raise PermissionError('temporarily locked')
            return real_replace(source, destination)

        with patch('zip_packager.os.replace', side_effect=flaky_replace), \
             patch('zip_packager.time.sleep'):
            write_journal(target, [{'status': 'ok'}])
        self.assertEqual(len(calls), 3)
        self.assertIn('"status": "ok"', target.read_text(encoding='utf-8'))

    def test_pack_matches_legacy_root_deflate_layout(self):
        rows = plan_zip_pack(self.library)
        result, _ = apply_zip_pack(rows, self.library, self.root / 'logs')
        target = self.chapter.with_name(self.chapter.name + '.zip')
        self.assertEqual(result[0]['status'], '已完成')
        self.assertTrue(self.chapter.is_dir())
        with zipfile.ZipFile(target) as archive:
            self.assertEqual(archive.namelist(), ['001.png', '002.png'])
            self.assertEqual(archive.comment, b'')
            self.assertTrue(all(row.compress_type == zipfile.ZIP_DEFLATED
                                for row in archive.infolist()))

    def test_existing_identical_zip_never_removes_loose_folder(self):
        rows = plan_zip_pack(self.library)
        result, _ = apply_zip_pack(rows, self.library, self.root / 'logs')
        target = self.chapter.with_name(self.chapter.name + '.zip')
        target_signature = zip_signature(target)
        rows = plan_zip_pack(self.library)
        result, _ = apply_zip_pack(rows, self.library, self.root / 'logs2')
        self.assertEqual(result[0]['status'], '已有 ZIP，跳过')
        self.assertTrue(self.chapter.is_dir())
        self.assertEqual(zip_signature(target), target_signature)

    def test_different_existing_zip_keeps_both(self):
        target = self.chapter.with_name(self.chapter.name + '.zip')
        with zipfile.ZipFile(target, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr('001.png', b'different')
        rows = plan_zip_pack(self.library)
        result, _ = apply_zip_pack(rows, self.library, self.root / 'logs')
        self.assertEqual(result[0]['status'], '已有 ZIP，跳过')
        self.assertTrue(self.chapter.is_dir())
        self.assertTrue(target.is_file())

    def test_unknown_files_are_skipped(self):
        (self.chapter / 'notes.txt').write_text('keep me', encoding='utf-8')
        rows = plan_zip_pack(self.library)
        self.assertEqual(rows[0]['status'], '跳过')
        self.assertIn('非图片', rows[0]['error'])

    def test_active_hidden_download_stage_is_never_packed(self):
        stage = self.library / '漫画' / '.copy-updating-active'
        stage.mkdir()
        (stage / '001.png').write_bytes(b'incomplete')
        rows = plan_zip_pack(self.library)
        self.assertEqual([Path(row['source']).name for row in rows], ['第0001.000话'])

    def test_zero_zip_cleanup_deletes_zip_and_matching_folder_only(self):
        zero = self.chapter.with_name(self.chapter.name + '.zip')
        zero.write_bytes(b'')
        nonzero = self.chapter.parent / '第0002.000话.zip'
        nonzero.write_bytes(b'not empty')
        rows = plan_zero_zip_cleanup(self.library)
        self.assertEqual([Path(row['archive']).name for row in rows], [zero.name])
        result, _ = apply_zero_zip_cleanup(rows, self.library, self.root / 'cleanup-logs')
        self.assertEqual(result[0]['status'], '已删除')
        self.assertFalse(zero.exists())
        self.assertFalse(self.chapter.exists())
        self.assertTrue(nonzero.is_file())

    def test_zero_zip_without_matching_folder_is_deleted(self):
        zero = self.chapter.parent / '第0003.000话.zip'
        zero.write_bytes(b'')
        rows = plan_zero_zip_cleanup(self.library)
        selected = [row for row in rows if Path(row['archive']) == zero]
        result, _ = apply_zero_zip_cleanup(selected, self.library, self.root / 'cleanup-logs')
        self.assertEqual(result[0]['status'], '已删除')
        self.assertFalse(zero.exists())


if __name__ == '__main__':
    unittest.main()
