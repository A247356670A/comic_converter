import unittest

from progress_ui import estimate_remaining, format_duration


class ProgressHelpersTests(unittest.TestCase):
    def test_duration_format(self):
        self.assertEqual(format_duration(5), '5秒')
        self.assertEqual(format_duration(65), '1分05秒')
        self.assertEqual(format_duration(3661), '1小时01分01秒')
        self.assertEqual(format_duration(None), '计算中')

    def test_eta(self):
        self.assertEqual(estimate_remaining(20, 2, 10), 80)
        self.assertEqual(estimate_remaining(20, 10, 10), 0)
        self.assertIsNone(estimate_remaining(20, 0, 10))


if __name__ == '__main__':
    unittest.main()
