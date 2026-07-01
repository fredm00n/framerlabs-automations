"""Tests for scripts/shared.py — shared utilities used by all monitoring scripts."""
import json
import os
import sys
import tempfile
import unittest
import urllib.error
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, mock_open, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'scripts'))
import shared

# Exercise the production side-effect paths (writes/alerts enabled) regardless
# of where the suite runs. The observe-only gate has dedicated tests that
# override this explicitly.
os.environ['ENABLE_SIDE_EFFECTS'] = '1'

_error_log_patcher = patch('error_log.log_error')


def setUpModule():
    _error_log_patcher.start()


def tearDownModule():
    _error_log_patcher.stop()


# ---------------------------------------------------------------------------
# load_dotenv
# ---------------------------------------------------------------------------

class TestLoadDotenv(unittest.TestCase):

    _KEYS = ('_SHTEST_A', '_SHTEST_B')

    def setUp(self):
        for k in self._KEYS:
            os.environ.pop(k, None)

    def tearDown(self):
        for k in self._KEYS:
            os.environ.pop(k, None)

    def _mock_env_file(self, content):
        return patch('builtins.open', mock_open(read_data=content))

    def test_loads_key_value_pair(self):
        with self._mock_env_file('_SHTEST_A=hello\n'):
            shared.load_dotenv()
        self.assertEqual(os.environ.get('_SHTEST_A'), 'hello')

    def test_skips_comment_lines(self):
        with self._mock_env_file('# comment\n_SHTEST_A=hi\n'):
            shared.load_dotenv()
        self.assertEqual(os.environ.get('_SHTEST_A'), 'hi')

    def test_does_not_overwrite_existing_var(self):
        os.environ['_SHTEST_A'] = 'original'
        with self._mock_env_file('_SHTEST_A=new\n'):
            shared.load_dotenv()
        self.assertEqual(os.environ['_SHTEST_A'], 'original')

    def test_missing_env_file_is_silent(self):
        with patch('builtins.open', side_effect=FileNotFoundError):
            shared.load_dotenv()

    def test_value_containing_equals_sign(self):
        with self._mock_env_file('_SHTEST_A=val=ue\n'):
            shared.load_dotenv()
        self.assertEqual(os.environ.get('_SHTEST_A'), 'val=ue')


# ---------------------------------------------------------------------------
# _should_retry
# ---------------------------------------------------------------------------

class TestShouldRetry(unittest.TestCase):

    def test_retries_on_429(self):
        exc = urllib.error.HTTPError(None, 429, 'Too Many Requests', {}, None)
        self.assertTrue(shared._should_retry(exc))

    def test_retries_on_5xx(self):
        for code in (500, 502, 503, 504, 529):
            with self.subTest(code=code):
                exc = urllib.error.HTTPError(None, code, 'err', {}, None)
                self.assertTrue(shared._should_retry(exc))

    def test_does_not_retry_on_400(self):
        exc = urllib.error.HTTPError(None, 400, 'Bad Request', {}, None)
        self.assertFalse(shared._should_retry(exc))

    def test_does_not_retry_on_404(self):
        exc = urllib.error.HTTPError(None, 404, 'Not Found', {}, None)
        self.assertFalse(shared._should_retry(exc))

    def test_retries_on_url_error(self):
        self.assertTrue(shared._should_retry(urllib.error.URLError('network unreachable')))

    def test_retries_on_bare_timeout_error(self):
        self.assertTrue(shared._should_retry(TimeoutError('The read operation timed out')))

    def test_retries_on_socket_timeout(self):
        import socket
        self.assertTrue(shared._should_retry(socket.timeout('timed out')))

    def test_does_not_retry_on_generic_exception(self):
        self.assertFalse(shared._should_retry(ValueError('bad value')))


# ---------------------------------------------------------------------------
# _retry
# ---------------------------------------------------------------------------

class TestRetry(unittest.TestCase):

    def test_returns_value_on_first_success(self):
        fn = MagicMock(return_value='ok')
        with patch('time.sleep'):
            result = shared._retry(fn, max_attempts=3)
        self.assertEqual(result, 'ok')
        fn.assert_called_once()

    def test_retries_on_retryable_error_then_succeeds(self):
        exc = urllib.error.URLError('transient')
        fn = MagicMock(side_effect=[exc, exc, 'success'])
        with patch('time.sleep'):
            result = shared._retry(fn, max_attempts=4)
        self.assertEqual(result, 'success')

    def test_raises_after_max_attempts(self):
        exc = urllib.error.URLError('persistent failure')
        fn = MagicMock(side_effect=exc)
        with patch('time.sleep'):
            with self.assertRaises(urllib.error.URLError):
                shared._retry(fn, max_attempts=3)

    def test_does_not_retry_non_retryable_error(self):
        exc = urllib.error.HTTPError(None, 400, 'Bad Request', {}, None)
        fn = MagicMock(side_effect=exc)
        with patch('time.sleep'):
            with self.assertRaises(urllib.error.HTTPError):
                shared._retry(fn, max_attempts=4)
        fn.assert_called_once()

    def test_exponential_backoff_delays(self):
        exc = urllib.error.URLError('fail')
        fn = MagicMock(side_effect=[exc, exc, 'ok'])
        with patch('time.sleep') as mock_sleep:
            shared._retry(fn, max_attempts=4)
        delays = [c[0][0] for c in mock_sleep.call_args_list]
        self.assertEqual(delays, [2, 4])


# ---------------------------------------------------------------------------
# _parse_retry_after / Retry-After honouring inside _retry
# ---------------------------------------------------------------------------

def _make_http_error(code, headers=None):
    import email.message
    msg = email.message.Message()
    for k, v in (headers or {}).items():
        msg[k] = v
    return urllib.error.HTTPError(None, code, 'err', msg, None)


class TestParseRetryAfter(unittest.TestCase):

    def test_empty_returns_none(self):
        self.assertIsNone(shared._parse_retry_after(''))

    def test_integer_seconds(self):
        self.assertEqual(shared._parse_retry_after('5'), 5.0)

    def test_float_seconds(self):
        self.assertEqual(shared._parse_retry_after('2.5'), 2.5)

    def test_zero(self):
        self.assertEqual(shared._parse_retry_after('0'), 0.0)

    def test_negative_seconds_returns_none(self):
        self.assertIsNone(shared._parse_retry_after('-3'))

    def test_strips_whitespace(self):
        self.assertEqual(shared._parse_retry_after('  7  '), 7.0)

    def test_garbage_returns_none(self):
        self.assertIsNone(shared._parse_retry_after('soon-ish'))

    def test_http_date_in_future(self):
        result = shared._parse_retry_after('Wed, 21 Oct 2099 07:28:00 GMT')
        self.assertIsNotNone(result)
        self.assertGreater(result, 0)

    def test_http_date_in_past_returns_zero(self):
        result = shared._parse_retry_after('Wed, 21 Oct 1999 07:28:00 GMT')
        self.assertEqual(result, 0.0)


class TestRetryAfter(unittest.TestCase):

    def test_429_with_integer_retry_after_sleeps_for_header_value(self):
        exc = _make_http_error(429, {'Retry-After': '5'})
        fn = MagicMock(side_effect=[exc, 'ok'])
        with patch('time.sleep') as mock_sleep:
            result = shared._retry(fn, max_attempts=4)
        self.assertEqual(result, 'ok')
        delays = [c[0][0] for c in mock_sleep.call_args_list]
        self.assertEqual(delays, [5])

    def test_429_with_smaller_retry_after_uses_default_backoff(self):
        exc = _make_http_error(429, {'Retry-After': '1'})
        fn = MagicMock(side_effect=[exc, 'ok'])
        with patch('time.sleep') as mock_sleep:
            shared._retry(fn, max_attempts=4)
        delays = [c[0][0] for c in mock_sleep.call_args_list]
        self.assertEqual(delays, [2])

    def test_429_without_retry_after_uses_default_backoff(self):
        exc = _make_http_error(429, {})
        fn = MagicMock(side_effect=[exc, exc, 'ok'])
        with patch('time.sleep') as mock_sleep:
            shared._retry(fn, max_attempts=4)
        delays = [c[0][0] for c in mock_sleep.call_args_list]
        self.assertEqual(delays, [2, 4])

    def test_429_retry_after_clamped_to_max(self):
        exc = _make_http_error(429, {'Retry-After': '600'})
        fn = MagicMock(side_effect=[exc, 'ok'])
        with patch('time.sleep') as mock_sleep:
            shared._retry(fn, max_attempts=4)
        delays = [c[0][0] for c in mock_sleep.call_args_list]
        self.assertEqual(delays, [shared._RETRY_AFTER_MAX_SECONDS])

    def test_5xx_with_retry_after_is_ignored(self):
        exc = _make_http_error(503, {'Retry-After': '10'})
        fn = MagicMock(side_effect=[exc, 'ok'])
        with patch('time.sleep') as mock_sleep:
            shared._retry(fn, max_attempts=4)
        delays = [c[0][0] for c in mock_sleep.call_args_list]
        self.assertEqual(delays, [2])


# ---------------------------------------------------------------------------
# is_valid_iso8601_date
# ---------------------------------------------------------------------------

class TestIsValidIso8601Date(unittest.TestCase):

    def test_valid_utc_datetime(self):
        self.assertTrue(shared.is_valid_iso8601_date('2024-03-01T10:00:00+00:00'))

    def test_valid_datetime_with_offset(self):
        self.assertTrue(shared.is_valid_iso8601_date('2024-03-01T08:00:00-05:00'))

    def test_valid_date_only(self):
        self.assertTrue(shared.is_valid_iso8601_date('2024-03-01'))

    def test_empty_string_returns_false(self):
        self.assertFalse(shared.is_valid_iso8601_date(''))

    def test_garbage_string_returns_false(self):
        self.assertFalse(shared.is_valid_iso8601_date('not-a-date'))

    def test_partial_date_invalid(self):
        self.assertFalse(shared.is_valid_iso8601_date('2024-03'))

    def test_timestamp_with_z_suffix(self):
        result = shared.is_valid_iso8601_date('2024-03-01T10:00:00Z')
        self.assertIsInstance(result, bool)

    def test_residual_dollar_d_prefix_rejected(self):
        self.assertFalse(shared.is_valid_iso8601_date('$D2024-03-01'))


# ---------------------------------------------------------------------------
# truncate_for_notion
# ---------------------------------------------------------------------------

class TestTruncateForNotion(unittest.TestCase):

    def test_empty_string_returns_empty(self):
        self.assertEqual(shared.truncate_for_notion(''), '')

    def test_short_ascii_returned_unchanged(self):
        self.assertEqual(shared.truncate_for_notion('hello'), 'hello')

    def test_long_ascii_truncated_to_limit(self):
        self.assertEqual(len(shared.truncate_for_notion('x' * 3000)), 2000)

    def test_supplementary_chars_fit_utf16_limit(self):
        result = shared.truncate_for_notion('\U0001F600' * 1500)
        utf16_units = len(result.encode('utf-16-le')) // 2
        self.assertLessEqual(utf16_units, 2000)

    def test_custom_limit(self):
        self.assertEqual(len(shared.truncate_for_notion('x' * 100, limit=10)), 10)


# ---------------------------------------------------------------------------
# Alert suppression
# ---------------------------------------------------------------------------

class TestAlertSuppression(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.state_path = os.path.join(self._tmp.name, 'alert_state.json')

    def tearDown(self):
        self._tmp.cleanup()

    def test_no_state_file_means_not_suppressed(self):
        self.assertFalse(shared.should_suppress_alert('anything', self.state_path))

    def test_record_then_should_suppress(self):
        shared.record_alert_sent('test:key', self.state_path)
        self.assertTrue(shared.should_suppress_alert('test:key', self.state_path))

    def test_different_keys_do_not_collide(self):
        shared.record_alert_sent('test:a', self.state_path)
        self.assertTrue(shared.should_suppress_alert('test:a', self.state_path))
        self.assertFalse(shared.should_suppress_alert('test:b', self.state_path))

    def test_outside_window_not_suppressed(self):
        past = datetime.now(timezone.utc) - timedelta(minutes=120)
        shared.record_alert_sent('test:key', self.state_path, now=past)
        self.assertFalse(shared.should_suppress_alert(
            'test:key', self.state_path, suppress_minutes=60))

    def test_inside_window_suppressed(self):
        recent = datetime.now(timezone.utc) - timedelta(minutes=10)
        shared.record_alert_sent('test:key', self.state_path, now=recent)
        self.assertTrue(shared.should_suppress_alert(
            'test:key', self.state_path, suppress_minutes=60))

    def test_corrupt_state_file_treated_as_empty(self):
        with open(self.state_path, 'w') as f:
            f.write('{not valid json')
        self.assertFalse(shared.should_suppress_alert('anything', self.state_path))

    def test_malformed_timestamp_treated_as_not_recorded(self):
        with open(self.state_path, 'w') as f:
            f.write('{"test:key": "not-a-timestamp"}')
        self.assertFalse(shared.should_suppress_alert('test:key', self.state_path))


# ---------------------------------------------------------------------------
# warn_discord
# ---------------------------------------------------------------------------

class TestWarnDiscord(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.state_path = os.path.join(self._tmp.name, 'alert_state.json')
        os.environ['DISCORD_ALERTS_WEBHOOK_URL'] = 'https://discord.com/api/webhooks/test-alerts'

    def tearDown(self):
        os.environ.pop('DISCORD_ALERTS_WEBHOOK_URL', None)
        self._tmp.cleanup()

    def test_posts_content_message_to_alerts_webhook(self):
        with patch('shared.http_post', return_value={}) as mock_post:
            shared.warn_discord('test warning', 'test_script', self.state_path)
        mock_post.assert_called_once()
        url, payload = mock_post.call_args[0]
        self.assertIn('test-alerts', url)
        self.assertIn('test warning', payload['content'])

    def test_no_op_when_env_var_missing(self):
        os.environ.pop('DISCORD_ALERTS_WEBHOOK_URL', None)
        with patch('shared.http_post') as mock_post:
            shared.warn_discord('msg', 'test_script', self.state_path)
        mock_post.assert_not_called()

    def test_exception_does_not_propagate(self):
        with patch('shared.http_post', side_effect=Exception('network error')):
            shared.warn_discord('msg', 'test_script', self.state_path)

    def test_dedup_key_suppresses_second_call(self):
        with patch('shared.http_post', return_value={}) as mock_post:
            shared.warn_discord('first', 'test', self.state_path, dedup_key='k')
            shared.warn_discord('second', 'test', self.state_path, dedup_key='k')
        self.assertEqual(mock_post.call_count, 1)

    def test_no_dedup_key_never_suppresses(self):
        with patch('shared.http_post', return_value={}) as mock_post:
            shared.warn_discord('first', 'test', self.state_path)
            shared.warn_discord('second', 'test', self.state_path)
        self.assertEqual(mock_post.call_count, 2)

    def test_failed_send_does_not_record(self):
        import io
        err = urllib.error.HTTPError(
            'url', 503, 'Service Unavailable', {}, io.BytesIO(b''))
        with patch('shared.http_post', side_effect=err):
            shared.warn_discord('boom', 'test', self.state_path, dedup_key='k')
        self.assertFalse(shared.should_suppress_alert('k', self.state_path))


# ---------------------------------------------------------------------------
# write_summary
# ---------------------------------------------------------------------------

class TestWriteSummary(unittest.TestCase):

    def tearDown(self):
        os.environ.pop('GITHUB_STEP_SUMMARY', None)

    def test_writes_to_file_when_env_set(self):
        with patch.dict('os.environ', {'GITHUB_STEP_SUMMARY': '/tmp/summary.md'}), \
             patch('builtins.open', mock_open()) as m:
            shared.write_summary('## Test\nhello')
        m.assert_called_once_with('/tmp/summary.md', 'a')
        m().write.assert_called_once_with('## Test\nhello\n')

    def test_no_op_when_env_not_set(self):
        os.environ.pop('GITHUB_STEP_SUMMARY', None)
        with patch('builtins.open') as m:
            shared.write_summary('ignored')
        m.assert_not_called()


class TestSideEffectsGate(unittest.TestCase):
    """The monitoring flow must only perform external side effects in the
    production cron (GITHUB_ACTIONS) or under an explicit override; sandbox /
    self-improvement runs stay observe-only."""

    def test_enabled_under_github_actions(self):
        with patch.dict(os.environ, {'GITHUB_ACTIONS': 'true', 'ENABLE_SIDE_EFFECTS': ''}):
            self.assertTrue(shared.side_effects_enabled())

    def test_enabled_with_explicit_override(self):
        with patch.dict(os.environ, {'GITHUB_ACTIONS': '', 'ENABLE_SIDE_EFFECTS': '1'}):
            self.assertTrue(shared.side_effects_enabled())

    def test_disabled_when_neither_set(self):
        with patch.dict(os.environ, {'GITHUB_ACTIONS': '', 'ENABLE_SIDE_EFFECTS': ''}):
            self.assertFalse(shared.side_effects_enabled())

    def test_warn_discord_skips_post_when_disabled(self):
        with patch.dict(os.environ, {'GITHUB_ACTIONS': '', 'ENABLE_SIDE_EFFECTS': '',
                                     'DISCORD_ALERTS_WEBHOOK_URL': 'https://example.test/hook'}):
            with patch('shared.http_post') as post_mock:
                shared.warn_discord('boom', 'reddit_leads', '/tmp/state.json')
        post_mock.assert_not_called()

    def test_warn_discord_posts_when_enabled(self):
        with patch.dict(os.environ, {'ENABLE_SIDE_EFFECTS': '1',
                                     'DISCORD_ALERTS_WEBHOOK_URL': 'https://example.test/hook'}):
            with patch('shared.http_post') as post_mock:
                shared.warn_discord('boom', 'reddit_leads', '/tmp/state.json')
        post_mock.assert_called_once()


if __name__ == '__main__':
    unittest.main()
