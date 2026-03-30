import json
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'scripts'))
import error_log


class TestLogError(unittest.TestCase):

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.log_path = os.path.join(self.tmpdir.name, 'errors.jsonl')

    def tearDown(self):
        self.tmpdir.cleanup()

    def _read_entries(self):
        with open(self.log_path) as f:
            return [json.loads(line) for line in f if line.strip()]

    def test_appends_json_line(self):
        error_log.log_error('test_script', 'error', 'something broke', log_path=self.log_path)
        entries = self._read_entries()
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]['script'], 'test_script')
        self.assertEqual(entries[0]['severity'], 'error')
        self.assertEqual(entries[0]['message'], 'something broke')

    def test_timestamp_is_iso_utc(self):
        error_log.log_error('s', 'warning', 'msg', log_path=self.log_path)
        entry = self._read_entries()[0]
        ts = entry['timestamp']
        # ISO 8601 UTC ends with +00:00
        self.assertTrue(ts.endswith('+00:00'), f'Expected UTC timestamp, got: {ts}')

    def test_severity_warning(self):
        error_log.log_error('s', 'warning', 'heads up', log_path=self.log_path)
        self.assertEqual(self._read_entries()[0]['severity'], 'warning')

    def test_context_dict_included(self):
        error_log.log_error('s', 'error', 'msg', context={'count': 3, 'url': 'http://x'}, log_path=self.log_path)
        entry = self._read_entries()[0]
        self.assertEqual(entry['context'], {'count': 3, 'url': 'http://x'})

    def test_context_defaults_to_empty_dict(self):
        error_log.log_error('s', 'error', 'msg', log_path=self.log_path)
        self.assertEqual(self._read_entries()[0]['context'], {})

    def test_creates_directory_if_missing(self):
        nested_path = os.path.join(self.tmpdir.name, 'sub', 'dir', 'errors.jsonl')
        error_log.log_error('s', 'error', 'msg', log_path=nested_path)
        self.assertTrue(os.path.exists(nested_path))

    def test_appends_multiple_entries(self):
        error_log.log_error('s', 'error', 'first', log_path=self.log_path)
        error_log.log_error('s', 'warning', 'second', log_path=self.log_path)
        entries = self._read_entries()
        self.assertEqual(len(entries), 2)
        self.assertEqual(entries[0]['message'], 'first')
        self.assertEqual(entries[1]['message'], 'second')

    def test_exception_does_not_propagate(self):
        """A failure inside log_error must never crash the caller."""
        with patch('builtins.open', side_effect=OSError('disk full')):
            # Should not raise
            error_log.log_error('s', 'error', 'msg', log_path=self.log_path)

    def test_exception_prints_to_stderr(self):
        with patch('builtins.open', side_effect=OSError('disk full')):
            with patch('sys.stderr') as mock_stderr:
                error_log.log_error('s', 'error', 'msg', log_path=self.log_path)
                mock_stderr.write.assert_called()

    def test_custom_log_path(self):
        custom = os.path.join(self.tmpdir.name, 'custom.jsonl')
        error_log.log_error('s', 'error', 'msg', log_path=custom)
        self.assertTrue(os.path.exists(custom))
        with open(custom) as f:
            entry = json.loads(f.read().strip())
        self.assertEqual(entry['message'], 'msg')

    def test_default_repo_field(self):
        error_log.log_error('s', 'error', 'msg', log_path=self.log_path)
        entry = self._read_entries()[0]
        self.assertEqual(entry['repo'], 'framerlabs-automations')

    def test_custom_repo_field(self):
        error_log.log_error('s', 'error', 'msg', repo='other-repo', log_path=self.log_path)
        entry = self._read_entries()[0]
        self.assertEqual(entry['repo'], 'other-repo')


if __name__ == '__main__':
    unittest.main()
