import json
from pathlib import Path
import tempfile
import unittest

from Crypto.Cipher import AES
from Crypto.Util.Padding import pad

from copy_manga_update import AccessProblem, Chapter, chapter_title, decrypt_web, parse_slug, plan_update
from decimal import Decimal


class UpdateTests(unittest.TestCase):
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
