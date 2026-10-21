import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from batch_update import write_json


class StatusTests(unittest.TestCase):
    def test_replace_retries_transient_windows_lock(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'latest_status.json'
            actual_replace = os.replace
            attempts = []

            def intermittent_replace(source, target):
                attempts.append(1)
                if len(attempts) < 3:
                    raise PermissionError(5, 'Access is denied')
                actual_replace(source, target)

            with patch('batch_update.os.replace', side_effect=intermittent_replace), \
                 patch('batch_update.time.sleep'):
                write_json(path, {'running': True})
            self.assertEqual(len(attempts), 3)
            self.assertTrue(json.loads(path.read_text(encoding='utf-8'))['running'])
            self.assertEqual(list(Path(folder).glob('*.tmp')), [])


if __name__ == '__main__':
    unittest.main()
