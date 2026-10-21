import json
from pathlib import Path
import tempfile
import unittest
import zipfile

from PIL import Image

from aidoku_library_import import archive_metadata, discover, existing_series


SLUG = 'samplemanga'
CHAPTER = '12345678-1234-1234-1234-123456789abc'


def make_cbz(path: Path, series='测试漫画', slug=SLUG, chapter=CHAPTER):
    path.parent.mkdir(parents=True, exist_ok=True)
    notes = '!AIDOKUDATA' + json.dumps({'mangaKey': slug, 'chapterKey': chapter})
    xml = f'<ComicInfo><Series>{series}</Series><Number>1</Number><Notes>{notes}</Notes></ComicInfo>'
    image = path.with_suffix('.png')
    Image.new('RGB', (2, 2), 'white').save(image)
    with zipfile.ZipFile(path, 'w') as archive:
        archive.writestr('ComicInfo.xml', xml)
        archive.write(image, '001.png')
    image.unlink()


class ImportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.downloads = self.root / 'downloads' / 'zh.copymanga'
        self.library = self.root / 'Manga'
        self.library.mkdir()
        self.mapping = self.root / 'copy_sources.json'
        self.mapping.write_text('{}', encoding='utf-8')

    def tearDown(self):
        self.temp.cleanup()

    def test_metadata_and_url_are_derived_from_aidoku_keys(self):
        archive = self.downloads / SLUG / f'{CHAPTER}.cbz'
        make_cbz(archive)
        data = archive_metadata(archive)
        self.assertEqual(data['slug'], SLUG)
        self.assertEqual(data['chapter_id'], CHAPTER)
        self.assertEqual(data['url'], 'https://www.copy20.com/comic/samplemanga')

    def test_mismatched_path_is_rejected(self):
        archive = self.downloads / SLUG / f'{CHAPTER}.cbz'
        make_cbz(archive, slug='different')
        with self.assertRaisesRegex(ValueError, '不一致'):
            archive_metadata(archive)

    def test_existing_mapped_slug_is_skipped(self):
        old = self.library / '已存在'
        old.mkdir()
        self.mapping.write_text(json.dumps({str(old): SLUG}), encoding='utf-8')
        archive = self.downloads / SLUG / f'{CHAPTER}.cbz'
        make_cbz(archive)
        rows = discover(self.downloads.parent, self.library, self.mapping)
        self.assertEqual(rows[0]['status'], '已跳过')
        self.assertEqual(Path(rows[0]['existing']), old)

    def test_exact_and_author_suffix_names_are_existing(self):
        exact = self.library / '测试漫画'
        exact.mkdir()
        found, _ = existing_series('测试漫画', SLUG, self.library, {})
        self.assertEqual(found, exact)
        exact.rmdir()
        authored = self.library / '测试漫画_作者'
        authored.mkdir()
        found, _ = existing_series('测试漫画', SLUG, self.library, {})
        self.assertEqual(found, authored)

    def test_new_series_is_pending_without_writes(self):
        archive = self.downloads / SLUG / f'{CHAPTER}.cbz'
        make_cbz(archive)
        rows = discover(self.downloads.parent, self.library, self.mapping)
        self.assertEqual(rows[0]['status'], '待导入')
        self.assertEqual(list(self.library.iterdir()), [])

    def test_duplicate_downloads_mark_every_copy_error(self):
        first = self.downloads / SLUG / f'{CHAPTER}.cbz'
        second_id = 'aaaaaaaa-1234-1234-1234-123456789abc'
        make_cbz(first)
        make_cbz(self.downloads / SLUG / f'{second_id}.cbz', chapter=second_id)
        rows = discover(self.downloads.parent, self.library, self.mapping)
        self.assertEqual([row['status'] for row in rows], ['错误', '错误'])


if __name__ == '__main__':
    unittest.main()
