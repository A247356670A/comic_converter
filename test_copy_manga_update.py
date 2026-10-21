import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

from Crypto.Cipher import AES
from Crypto.Util.Padding import pad

from copy_manga_update import (AccessProblem, Chapter, Client, IncompleteCatalog,
                               TemporaryNetworkProblem,
                               apply_volume_renames, chapter_title, decrypt_web,
                               deduplicate_catalog, local_volume_number, parse_slug,
                               parse_preferred_groups, plan_update, plan_volume_renames, sorted_mapping_paths,
                               volume_title, web_volume_number)
from decimal import Decimal


class UpdateTests(unittest.TestCase):
    def test_incomplete_catalog_is_requested_again(self):
        messages, calls = [], []
        expected = [Chapter(Decimal(1), 'id', '第0001.000话', 'default')]
        client = Client(delay=0, on_retry=messages.append, catalog_retry_delays=(0,))

        def catalog_once(_slug):
            calls.append(1)
            if len(calls) == 1:
                raise IncompleteCatalog('章节目录数量不完整（default：声明 2，实际 1）')
            return expected

        client._catalog_once = catalog_once
        self.assertEqual(client.catalog('example'), expected)
        self.assertEqual(len(calls), 2)
        self.assertIn('重新读取完整目录', messages[0])

    def test_persistently_incomplete_catalog_stops_after_bound(self):
        client = Client(delay=0, catalog_retry_delays=(0, 0))
        calls = []

        def catalog_once(_slug):
            calls.append(1)
            raise IncompleteCatalog('章节目录数量不完整')

        client._catalog_once = catalog_once
        with self.assertRaisesRegex(IncompleteCatalog, '重新请求 2 次'):
            client.catalog('example')
        self.assertEqual(len(calls), 3)

    def test_duplicate_volume_prefers_unique_tankobon_group(self):
        variants = [
            Chapter(Decimal(7), 'old', '007', 'default', 'volume'),
            Chapter(Decimal(7), 'new', '007', 'tankobon', 'volume'),
        ]
        self.assertEqual(deduplicate_catalog(variants)[0].id, 'new')

    def test_ambiguous_duplicate_still_stops(self):
        duplicate_chapters = [
            Chapter(Decimal(7), 'a', '第0007.000话', 'default'),
            Chapter(Decimal(7), 'b', '第0007.000话', 'other'),
        ]
        duplicate_volumes = [
            Chapter(Decimal(7), 'a', '007', 'default', 'volume'),
            Chapter(Decimal(7), 'b', '007', 'other', 'volume'),
        ]
        with self.assertRaises(AccessProblem):
            deduplicate_catalog(duplicate_chapters)
        with self.assertRaises(AccessProblem):
            deduplicate_catalog(duplicate_volumes)

    def test_downloaded_chapter_id_resolves_same_number_duplicate(self):
        variants = [
            Chapter(Decimal(1), 'older', '第0001.000话', 'default'),
            Chapter(Decimal(1), 'downloaded', '第0001.000话', 'default'),
        ]
        selected = deduplicate_catalog(variants, {'downloaded'})
        self.assertEqual([row.id for row in selected], ['downloaded'])

    def test_downloaded_release_group_resolves_later_duplicates(self):
        variants = [
            Chapter(Decimal(1), 'seed', '第0001.000话', 'default'),
            Chapter(Decimal(11), 'default-11', '第0011.000话', 'default'),
            Chapter(Decimal(11), 'team-11', '第0011.000话', 'other_honyakuchimu'),
        ]
        selected = deduplicate_catalog(variants, {'seed'})
        self.assertEqual([row.id for row in selected], ['seed', 'default-11'])

    def test_persisted_group_affinity_does_not_cross_content_kind(self):
        variants = [
            Chapter(Decimal(11), 'default-11', '第0011.000话', 'default'),
            Chapter(Decimal(11), 'team-11', '第0011.000话', 'other_honyakuchimu'),
            Chapter(Decimal(2), 'default-volume', '002', 'default', 'volume'),
            Chapter(Decimal(2), 'tankobon-volume', '002', 'tankobon', 'volume'),
        ]
        preference = parse_preferred_groups([{'kind': 'chapter', 'group': 'default'}])
        selected = deduplicate_catalog(variants, preferred_groups=preference)
        self.assertEqual([row.id for row in selected], ['default-11', 'tankobon-volume'])

    def test_volume_and_same_number_chapter_are_distinct(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / '第0005.000话').mkdir()
            catalog = [
                Chapter(Decimal(5), 'chapter', '第0005.000话', 'default', 'chapter'),
                Chapter(Decimal(5), 'volume', '005', 'tankobon', 'volume'),
            ]
            result = plan_update(root, catalog)
            self.assertEqual([(row.kind, row.number) for row in result['missing']],
                             [('volume', Decimal(5))])

    def test_volume_folder_format_and_rename_plan(self):
        self.assertEqual(volume_title(Decimal(5)), '005')
        self.assertEqual(volume_title(Decimal(10)), '010')
        self.assertEqual(local_volume_number('第005卷'), Decimal(5))
        self.assertIsNone(local_volume_number('2024'))
        self.assertEqual(web_volume_number('第005卷'), Decimal(5))
        self.assertEqual(web_volume_number('05卷'), Decimal(5))
        self.assertIsNone(web_volume_number('2024卷'))
        self.assertIsNone(web_volume_number('1.5卷番外'))
        self.assertIsNone(web_volume_number('1-3卷蜜瓜特典'))
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            series = root / '漫画'
            series.mkdir()
            (series / '第5卷').mkdir()
            (series / '0010').mkdir()
            (series / '第0005.000话').mkdir()
            rows = plan_volume_renames(root)
            self.assertEqual([(row['source'].name, row['target'].name) for row in rows],
                             [('0010', '010'), ('第5卷', '005')])
            log = apply_volume_renames(rows, root / 'logs')
            self.assertTrue((series / '005').is_dir())
            self.assertTrue((series / '010').is_dir())
            self.assertTrue(log.is_file())

    def test_volume_rename_conflict_is_not_applied(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            series = root / '漫画'
            series.mkdir()
            (series / '第5卷').mkdir()
            (series / '005').mkdir()
            rows = plan_volume_renames(root)
            self.assertTrue(rows[0]['error'])
            with self.assertRaises(ValueError):
                apply_volume_renames(rows, root / 'logs')
            self.assertTrue((series / '第5卷').is_dir())

    def test_table_sort_numeric_and_unknown_last(self):
        mappings = {
            r'Z:\Manga\作品十': 'ten',
            r'Z:\Manga\作品二': 'two',
            r'Z:\Manga\未检查': 'unknown',
        }
        plans = {
            r'Z:\Manga\作品十': {'local_latest': Decimal('10'), 'remote_latest': Decimal('12'),
                                  'missing': [1, 2]},
            r'Z:\Manga\作品二': {'local_latest': Decimal('2'), 'remote_latest': Decimal('3'),
                                 'missing': [1]},
        }
        self.assertEqual(sorted_mapping_paths(mappings, plans, 'local'),
                         [r'Z:\Manga\作品二', r'Z:\Manga\作品十', r'Z:\Manga\未检查'])
        self.assertEqual(sorted_mapping_paths(mappings, plans, 'missing', True),
                         [r'Z:\Manga\作品十', r'Z:\Manga\作品二', r'Z:\Manga\未检查'])

    def test_wait_never_sleeps_negative_at_deadline(self):
        client = Client()
        sleeps = []
        with patch('copy_manga_update.time.monotonic', side_effect=[100, 100.05, 100.2]), \
             patch('copy_manga_update.time.sleep', side_effect=sleeps.append):
            client.wait(0.1)
        self.assertEqual(len(sleeps), 1)
        self.assertGreater(sleeps[0], 0)

    def test_http_520_recovers_after_wait(self):
        calls, messages = [], []

        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *_):
                return False

            def geturl(self):
                return 'https://www.copy20.com/comic/test'

            def read(self, _):
                return b'ok'

        def opener(request, timeout):
            calls.append(request.full_url)
            if len(calls) == 1:
                raise HTTPError(request.full_url, 520, 'origin error', {}, None)
            return Response()

        client = Client(delay=0, opener=opener, on_retry=messages.append, retry_delays=(0,))
        self.assertEqual(client.fetch('https://www.copy20.com/comic/test', 20, 'https://www.copy20.com/'), b'ok')
        self.assertEqual(len(calls), 2)
        self.assertIn('HTTP 520', messages[0])

    def test_http_520_stops_after_bounded_retries(self):
        calls = []

        def opener(request, timeout):
            calls.append(request.full_url)
            raise HTTPError(request.full_url, 520, 'origin error', {}, None)

        client = Client(delay=0, opener=opener, retry_delays=(0, 0))
        with self.assertRaises(TemporaryNetworkProblem):
            client.fetch('https://www.copy20.com/comic/test', 20, 'https://www.copy20.com/')
        self.assertEqual(len(calls), 3)

    def test_http_403_does_not_retry(self):
        calls = []

        def opener(request, timeout):
            calls.append(request.full_url)
            raise HTTPError(request.full_url, 403, 'forbidden', {}, None)

        client = Client(delay=0, opener=opener, retry_delays=(0,))
        with self.assertRaises(AccessProblem):
            client.fetch('https://www.copy20.com/comic/test', 20, 'https://www.copy20.com/')
        self.assertEqual(len(calls), 1)

    def test_web_envelope_decrypt(self):
        key = '0p0zzpvv.nmn.oop'
        iv = 'abcdefghijklmnop'
        encoded = iv + AES.new(key.encode(), AES.MODE_CBC, iv.encode()).encrypt(
            pad(json.dumps({'groups': {}}).encode(), 16)).hex()
        self.assertEqual(decrypt_web(encoded, key), {'groups': {}})

    def test_restricted_or_wrong_domain(self):
        with self.assertRaises(AccessProblem):
            decrypt_web('error', '0p0zzpvv.nmn.oop')
        with self.assertRaises(ValueError):
            parse_slug('https://unrelated.example/comic/test')

    def test_missing_before_first_and_after_latest(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / '第02话').mkdir()
            (root / '第03话.zip').write_bytes(b'previous archive')
            catalog = [Chapter(Decimal(n), f'id{n}', chapter_title(f'第{n}话', Decimal(n)), 'default')
                       for n in (1, 2, 3, 4)]
            result = plan_update(root, catalog)
            self.assertEqual([c.number for c in result['missing']], [Decimal(1), Decimal(4)])
            self.assertEqual(result['remote_latest'], Decimal(4))

    def test_shortened_catalog_is_not_accepted(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / '第09话').mkdir()
            with self.assertRaises(AccessProblem):
                plan_update(root, [Chapter(Decimal(1), 'id1', '第0001.000话', 'default')])


if __name__ == '__main__':
    unittest.main()
