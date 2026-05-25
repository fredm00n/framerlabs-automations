#!/usr/bin/env python3
"""Tests for scripts/reddit_leads.py"""
import json
import os
import sys
import unittest
import urllib.error
from datetime import datetime
from unittest.mock import MagicMock, call, patch

sys.path.insert(0, '.')
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'scripts'))
from scripts.reddit_leads import (
    _ALWAYS_EXCLUDE_WORD_START_PHRASES,
    _RETRY_AFTER_MAX_SECONDS,
    _VALID_STATUSES,
    _clean_html,
    _has_word_start_phrase,
    _is_valid_iso8601_date,
    _parse_retry_after,
    _retry,
    _should_retry,
    _truncate_for_notion,
    cli,
    fetch_reddit_posts,
    get_lead_by_id,
    get_pending_leads,
    get_unnotified_approved_leads,
    mark_notified,
    notify_discord_lead,
    passes_light_filter,
    save_failed_sentinel_to_notion,
    save_lead_to_notion,
    update_lead_status,
    url_exists_in_notion,
)

# ---------------------------------------------------------------------------
# Prevent test runs from writing to the real logs/errors.jsonl file.
# Any test that exercises a code path which calls error_log.log_error would
# otherwise create real entries in the shared log, polluting it with test noise.
# ---------------------------------------------------------------------------
_error_log_patcher = patch('error_log.log_error')


def setUpModule():  # noqa: N802
    _error_log_patcher.start()


def tearDownModule():  # noqa: N802
    _error_log_patcher.stop()


# ---------------------------------------------------------------------------
# TestShouldRetry
# ---------------------------------------------------------------------------

class TestShouldRetry(unittest.TestCase):

    def test_retries_on_429(self):
        exc = urllib.error.HTTPError(None, 429, 'Too Many Requests', {}, None)
        self.assertTrue(_should_retry(exc))

    def test_retries_on_500(self):
        exc = urllib.error.HTTPError(None, 500, 'Server Error', {}, None)
        self.assertTrue(_should_retry(exc))

    def test_retries_on_502(self):
        exc = urllib.error.HTTPError(None, 502, 'Bad Gateway', {}, None)
        self.assertTrue(_should_retry(exc))

    def test_retries_on_503(self):
        exc = urllib.error.HTTPError(None, 503, 'Service Unavailable', {}, None)
        self.assertTrue(_should_retry(exc))

    def test_retries_on_504(self):
        exc = urllib.error.HTTPError(None, 504, 'Gateway Timeout', {}, None)
        self.assertTrue(_should_retry(exc))

    def test_does_not_retry_on_400(self):
        exc = urllib.error.HTTPError(None, 400, 'Bad Request', {}, None)
        self.assertFalse(_should_retry(exc))

    def test_does_not_retry_on_404(self):
        exc = urllib.error.HTTPError(None, 404, 'Not Found', {}, None)
        self.assertFalse(_should_retry(exc))

    def test_retries_on_url_error(self):
        import urllib.error as ue
        exc = ue.URLError('network unreachable')
        self.assertTrue(_should_retry(exc))

    def test_retries_on_bare_timeout_error(self):
        # ``response.read()`` timeouts (e.g. the recurring "The read operation
        # timed out" entries observed in logs/errors.jsonl) surface as bare
        # ``TimeoutError``, NOT as ``URLError``, so without an explicit branch
        # they bypass retry entirely and abort on the first attempt.
        self.assertTrue(_should_retry(TimeoutError('The read operation timed out')))

    def test_retries_on_socket_timeout(self):
        import socket
        # ``socket.timeout`` is an alias for ``TimeoutError`` on Python 3.10+;
        # this assertion documents the intent and guards against any future
        # divergence.
        self.assertTrue(_should_retry(socket.timeout('timed out')))

    def test_does_not_retry_on_generic_exception(self):
        self.assertFalse(_should_retry(ValueError('bad value')))


# ---------------------------------------------------------------------------
# TestRetry
# ---------------------------------------------------------------------------

class TestRetry(unittest.TestCase):

    def test_returns_value_on_first_success(self):
        fn = MagicMock(return_value='ok')
        with patch('time.sleep'):
            result = _retry(fn, max_attempts=3)
        self.assertEqual(result, 'ok')
        fn.assert_called_once()

    def test_retries_on_retryable_error_then_succeeds(self):
        exc = urllib.error.URLError('transient')
        fn = MagicMock(side_effect=[exc, exc, 'success'])
        with patch('time.sleep'):
            result = _retry(fn, max_attempts=4)
        self.assertEqual(result, 'success')
        self.assertEqual(fn.call_count, 3)

    def test_raises_after_max_attempts(self):
        exc = urllib.error.URLError('persistent failure')
        fn = MagicMock(side_effect=exc)
        with patch('time.sleep'):
            with self.assertRaises(urllib.error.URLError):
                _retry(fn, max_attempts=3)
        self.assertEqual(fn.call_count, 3)

    def test_does_not_retry_non_retryable_error(self):
        exc = urllib.error.HTTPError(None, 400, 'Bad Request', {}, None)
        fn = MagicMock(side_effect=exc)
        with patch('time.sleep'):
            with self.assertRaises(urllib.error.HTTPError):
                _retry(fn, max_attempts=4)
        fn.assert_called_once()

    def test_exponential_backoff_delays(self):
        exc = urllib.error.URLError('fail')
        fn = MagicMock(side_effect=[exc, exc, 'ok'])
        with patch('time.sleep') as mock_sleep:
            _retry(fn, max_attempts=4)
        delays = [c[0][0] for c in mock_sleep.call_args_list]
        self.assertEqual(delays, [2, 4])


# ---------------------------------------------------------------------------
# TestParseRetryAfter / TestRetryAfter
# ---------------------------------------------------------------------------


def _make_http_error(code: int, headers: dict | None = None) -> urllib.error.HTTPError:
    """Build an ``HTTPError`` whose ``.headers`` is a real email.Message.

    Passing ``{}`` directly as the headers argument means ``HTTPError.headers``
    is a bare dict, but real responses expose an ``email.message.Message``-like
    object whose ``.get()`` is case-insensitive.  Mirroring that here keeps the
    tests realistic for the ``Retry-After`` lookup.
    """
    import email.message
    msg = email.message.Message()
    for k, v in (headers or {}).items():
        msg[k] = v
    return urllib.error.HTTPError(None, code, 'err', msg, None)


class TestParseRetryAfter(unittest.TestCase):
    """``_parse_retry_after`` accepts both integer-seconds and HTTP-date forms."""

    def test_empty_returns_none(self):
        self.assertIsNone(_parse_retry_after(''))

    def test_integer_seconds(self):
        self.assertEqual(_parse_retry_after('5'), 5.0)

    def test_float_seconds(self):
        self.assertEqual(_parse_retry_after('2.5'), 2.5)

    def test_zero(self):
        self.assertEqual(_parse_retry_after('0'), 0.0)

    def test_negative_seconds_returns_none(self):
        # A negative ``Retry-After`` is nonsensical; fall back to default backoff.
        self.assertIsNone(_parse_retry_after('-3'))

    def test_strips_whitespace(self):
        self.assertEqual(_parse_retry_after('  7  '), 7.0)

    def test_garbage_returns_none(self):
        self.assertIsNone(_parse_retry_after('soon-ish'))

    def test_http_date_in_future(self):
        # Pick a fixed future date and verify parse returns >= 0.
        # We can't assert an exact value because the helper diffs from "now".
        result = _parse_retry_after('Wed, 21 Oct 2099 07:28:00 GMT')
        self.assertIsNotNone(result)
        self.assertGreater(result, 0)

    def test_http_date_in_past_returns_zero(self):
        # A date in the past should clamp to 0 rather than going negative.
        result = _parse_retry_after('Wed, 21 Oct 1999 07:28:00 GMT')
        self.assertEqual(result, 0.0)


class TestRetryAfter(unittest.TestCase):
    """``_retry`` honours ``Retry-After`` on HTTP 429 responses."""

    def test_429_with_integer_retry_after_sleeps_for_header_value(self):
        exc = _make_http_error(429, {'Retry-After': '5'})
        fn = MagicMock(side_effect=[exc, 'ok'])
        with patch('time.sleep') as mock_sleep:
            result = _retry(fn, max_attempts=4)
        self.assertEqual(result, 'ok')
        # default backoff would have been 2s; the 5s header beats it
        delays = [c[0][0] for c in mock_sleep.call_args_list]
        self.assertEqual(delays, [5])

    def test_429_with_smaller_retry_after_uses_default_backoff(self):
        # If the server says 1s but our backoff says 2s, we keep the longer
        # delay — the server's value is a *minimum* per RFC 7231.
        exc = _make_http_error(429, {'Retry-After': '1'})
        fn = MagicMock(side_effect=[exc, 'ok'])
        with patch('time.sleep') as mock_sleep:
            _retry(fn, max_attempts=4)
        delays = [c[0][0] for c in mock_sleep.call_args_list]
        self.assertEqual(delays, [2])

    def test_429_without_retry_after_uses_default_backoff(self):
        exc = _make_http_error(429, {})
        fn = MagicMock(side_effect=[exc, exc, 'ok'])
        with patch('time.sleep') as mock_sleep:
            _retry(fn, max_attempts=4)
        delays = [c[0][0] for c in mock_sleep.call_args_list]
        self.assertEqual(delays, [2, 4])

    def test_429_with_malformed_retry_after_uses_default_backoff(self):
        exc = _make_http_error(429, {'Retry-After': 'not-a-number'})
        fn = MagicMock(side_effect=[exc, 'ok'])
        with patch('time.sleep') as mock_sleep:
            _retry(fn, max_attempts=4)
        delays = [c[0][0] for c in mock_sleep.call_args_list]
        self.assertEqual(delays, [2])

    def test_429_retry_after_clamped_to_max(self):
        # Reddit can return very large ``Retry-After`` values; we should cap.
        exc = _make_http_error(429, {'Retry-After': '600'})
        fn = MagicMock(side_effect=[exc, 'ok'])
        with patch('time.sleep') as mock_sleep:
            _retry(fn, max_attempts=4)
        delays = [c[0][0] for c in mock_sleep.call_args_list]
        self.assertEqual(delays, [_RETRY_AFTER_MAX_SECONDS])

    def test_5xx_with_retry_after_is_ignored(self):
        # Only 429 triggers the special header lookup; a 503 with Retry-After
        # still uses exponential backoff to keep the existing behaviour stable.
        exc = _make_http_error(503, {'Retry-After': '10'})
        fn = MagicMock(side_effect=[exc, 'ok'])
        with patch('time.sleep') as mock_sleep:
            _retry(fn, max_attempts=4)
        delays = [c[0][0] for c in mock_sleep.call_args_list]
        self.assertEqual(delays, [2])


# ---------------------------------------------------------------------------
# Sample Atom RSS fixture
# ---------------------------------------------------------------------------

_ATOM_FEED = """\
<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <title>r/forhire</title>
  <entry>
    <title>[HIRING] Framer developer needed for landing page</title>
    <link href="https://www.reddit.com/r/forhire/comments/abc123/hiring_framer/"/>
    <published>2024-03-01T08:00:00+00:00</published>
    <updated>2024-03-01T10:00:00+00:00</updated>
    <content type="html">&lt;p&gt;Looking for a Framer developer. Budget $500.&lt;/p&gt;</content>
  </entry>
  <entry>
    <title>No title entry</title>
    <link href="https://www.reddit.com/r/forhire/comments/def456/no_title/"/>
    <published>2024-03-01T07:00:00+00:00</published>
    <updated>2024-03-01T09:00:00+00:00</updated>
    <content type="html">&lt;p&gt;Some content here.&lt;/p&gt;</content>
  </entry>
</feed>"""

# Feed where entries have only <updated> and no <published> element
_ATOM_FEED_UPDATED_ONLY = """\
<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <title>r/forhire</title>
  <entry>
    <title>[HIRING] Framer dev needed</title>
    <link href="https://www.reddit.com/r/forhire/comments/xyz789/hiring_framer/"/>
    <updated>2024-04-01T12:00:00+00:00</updated>
    <content type="html">&lt;p&gt;Need a Framer dev.&lt;/p&gt;</content>
  </entry>
</feed>"""

_EMPTY_FEED = """\
<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
</feed>"""

_MALFORMED_FEED = "this is not xml"


# ---------------------------------------------------------------------------
# TestCleanHtml
# ---------------------------------------------------------------------------

class TestCleanHtml(unittest.TestCase):
    def test_strips_tags(self):
        self.assertEqual(_clean_html('<p>Hello <b>world</b></p>'), 'Hello world')

    def test_decodes_entities(self):
        # Entities are decoded first, then tags stripped — so &lt;p&gt; → <p> → removed
        self.assertEqual(_clean_html('&lt;p&gt;Hello &amp; world&lt;/p&gt;'), 'Hello & world')

    def test_collapses_whitespace(self):
        self.assertEqual(_clean_html('<p>hello   \n  world</p>'), 'hello world')

    def test_empty_string(self):
        self.assertEqual(_clean_html(''), '')

    def test_none_equivalent(self):
        self.assertEqual(_clean_html(''), '')


# ---------------------------------------------------------------------------
# TestIsValidIso8601Date
# ---------------------------------------------------------------------------

class TestIsValidIso8601Date(unittest.TestCase):

    def test_valid_utc_datetime(self):
        self.assertTrue(_is_valid_iso8601_date('2024-03-01T10:00:00+00:00'))

    def test_valid_datetime_with_offset(self):
        self.assertTrue(_is_valid_iso8601_date('2024-03-01T08:00:00-05:00'))

    def test_valid_date_only(self):
        self.assertTrue(_is_valid_iso8601_date('2024-03-01'))

    def test_empty_string_returns_false(self):
        self.assertFalse(_is_valid_iso8601_date(''))

    def test_none_equivalent_empty_returns_false(self):
        self.assertFalse(_is_valid_iso8601_date(''))

    def test_garbage_string_returns_false(self):
        self.assertFalse(_is_valid_iso8601_date('not-a-date'))

    def test_partial_date_invalid(self):
        # '2024-03' is not a valid isoformat string Python accepts
        self.assertFalse(_is_valid_iso8601_date('2024-03'))

    def test_timestamp_with_z_suffix(self):
        # Python 3.11+ parses 'Z' as UTC; on older Pythons this may fail.
        # Either outcome is acceptable — we test that the function doesn't raise.
        result = _is_valid_iso8601_date('2024-03-01T10:00:00Z')
        self.assertIsInstance(result, bool)


# ---------------------------------------------------------------------------
# TestPassesLightFilter
# ---------------------------------------------------------------------------

class TestPassesLightFilter(unittest.TestCase):

    # --- Hiring subreddits ---

    def test_hiring_sub_passes_with_web_signal(self):
        self.assertTrue(passes_light_filter(
            '[HIRING] Need a Framer developer', 'Budget $500 for landing page', 'forhire'
        ))

    def test_hiring_sub_passes_website_signal(self):
        self.assertTrue(passes_light_filter(
            'Looking for web designer', 'Need a website built for my business', 'hiring'
        ))

    def test_hiring_sub_fails_without_web_signal(self):
        self.assertFalse(passes_light_filter(
            'Looking for a plumber', 'Need plumbing work done', 'forhire'
        ))

    def test_hiring_sub_blocked_by_job_seeker_signal(self):
        self.assertFalse(passes_light_filter(
            '[FOR HIRE] Available for hire', 'Check out my portfolio', 'forhire'
        ))

    def test_hiring_sub_blocked_by_always_exclude(self):
        self.assertFalse(passes_light_filter(
            'Framer tutorial needed', 'How to use Framer tutorial course', 'forhire'
        ))

    # --- Design/tech subreddits ---

    def test_design_tech_passes_framer_plus_intent(self):
        self.assertTrue(passes_light_filter(
            'Looking for a Framer expert', 'Need someone to build my site', 'framer'
        ))

    def test_design_tech_passes_hiring_plus_payment(self):
        self.assertTrue(passes_light_filter(
            'Hiring a web developer', 'Budget $1000, need someone', 'webdev'
        ))

    def test_design_tech_fails_framer_without_intent(self):
        self.assertFalse(passes_light_filter(
            'Framer is great for animations', 'Love using Framer for my projects', 'framer'
        ))

    def test_design_tech_fails_intent_without_framer_or_payment(self):
        self.assertFalse(passes_light_filter(
            'Looking for someone', 'Need help with something', 'webdev'
        ))

    # --- No-code subreddits ---

    def test_nocode_passes_all_three_signals(self):
        self.assertTrue(passes_light_filter(
            'Hiring a Webflow developer', 'Need a website, budget $500, willing to pay', 'nocode'
        ))

    def test_nocode_fails_missing_payment(self):
        self.assertFalse(passes_light_filter(
            'Looking for a website developer', 'Need someone to build my site', 'nocode'
        ))

    # --- Business subreddits ---

    def test_business_passes_web_plus_hiring(self):
        self.assertTrue(passes_light_filter(
            'Need a landing page developer', 'Looking to hire someone for my startup website', 'startups'
        ))

    def test_business_fails_no_web_signal(self):
        self.assertFalse(passes_light_filter(
            'Looking to hire a salesperson', 'Need someone for business development', 'startups'
        ))

    # --- Marketing/industry subreddits ---

    def test_marketing_passes_web_plus_hiring(self):
        self.assertTrue(passes_light_filter(
            'Need a web designer for landing page', 'Looking for someone to hire', 'marketing'
        ))

    def test_industry_passes_web_plus_hiring(self):
        self.assertTrue(passes_light_filter(
            'Need a website for my restaurant', 'Looking to hire a web designer', 'restaurateur'
        ))

    def test_marketing_passes_framer_signal(self):
        # 'framer' is a valid web signal for marketing/industry subreddits
        self.assertTrue(passes_light_filter(
            'Looking to hire a Framer designer', 'Need someone to build my site in Framer', 'marketing'
        ))

    def test_marketing_passes_figma_signal(self):
        # 'figma' is a valid web signal for marketing/industry subreddits
        self.assertTrue(passes_light_filter(
            'Hiring a Figma to Framer developer', 'Looking to hire someone', 'digitalmarketing'
        ))

    def test_industry_passes_framer_signal(self):
        # 'framer' should pass for industry subreddits (e.g. restaurateur)
        self.assertTrue(passes_light_filter(
            'Want to hire a Framer expert for my restaurant site', 'Looking for someone', 'restaurateur'
        ))

    def test_marketing_fails_without_web_or_framer_signal(self):
        # No web/framer signal — should not pass
        self.assertFalse(passes_light_filter(
            'Looking to hire a content writer', 'Need someone to write blog posts', 'marketing'
        ))

    # --- Always-exclude rules ---

    def test_always_exclude_feedback(self):
        self.assertFalse(passes_light_filter(
            'Feedback on my Framer website', 'Need honest feedback on my landing page', 'framer'
        ))

    def test_always_exclude_framer_pricing(self):
        self.assertFalse(passes_light_filter(
            'How much does Framer cost?', 'Framer pricing comparison vs Webflow', 'framer'
        ))

    def test_always_exclude_tutorial(self):
        self.assertFalse(passes_light_filter(
            'Best Framer tutorial for beginners', 'Tutorial course for learning framer', 'webdev'
        ))

    # --- Word-start exclusion: 'rate my' must not match 'migrate my' ---

    def test_rate_my_design_post_excluded(self):
        # Genuine feedback-request post — should be excluded as it was before.
        self.assertFalse(passes_light_filter(
            'Rate my Framer portfolio',
            'Just finished my site, please rate my design',
            'framer',
        ))

    def test_rate_my_website_post_excluded(self):
        # Another feedback-request variant — should be excluded.
        self.assertFalse(passes_light_filter(
            'Rate my website',
            'Just launched my new website, rate my work please',
            'webdev',
        ))

    def test_migrate_my_website_hiring_post_passes(self):
        # Textbook high-intent hiring post.  Under plain-substring matching
        # ``'rate my'`` would silently match inside ``'migrate my'`` and the
        # post would be dropped despite carrying both a hire signal and a
        # payment signal — exactly the lead the reviewer most wants to see.
        self.assertTrue(passes_light_filter(
            '[HIRING] Need someone to migrate my website from Squarespace to Framer',
            'Budget $3000, looking to hire a Framer expert. Please DM with quote.',
            'forhire',
        ))

    def test_migrate_my_site_hiring_post_passes_in_business_sub(self):
        # Same regression in a business subreddit (uses BUSINESS_WEB + HIRE
        # gate).  ``'website'`` satisfies BUSINESS_WEB and ``'looking to
        # hire'`` satisfies HIRE_SIGNALS — the only thing that would block
        # this lead is the spurious ``'rate my'`` substring inside
        # ``'migrate my'``.
        self.assertTrue(passes_light_filter(
            'Looking to hire — need to migrate my website',
            'We need to migrate my website from Wix to Framer, paid project.',
            'startups',
        ))

    def test_has_word_start_phrase_matches_rate_my_at_start(self):
        # 'rate my' as the leading two words is a true exclusion target.
        self.assertTrue(_has_word_start_phrase('rate my framer site', frozenset({'rate my'})))

    def test_has_word_start_phrase_matches_rate_my_after_word_boundary(self):
        # 'rate my' preceded by whitespace is still a whole-word match for 'rate'.
        self.assertTrue(_has_word_start_phrase(
            'please rate my framer site', frozenset({'rate my'})
        ))

    def test_has_word_start_phrase_rejects_migrate_my(self):
        # 'migrate my' must NOT match the phrase 'rate my' even though 'rate'
        # is a substring of 'migrate' — this is the whole point of the helper.
        self.assertFalse(_has_word_start_phrase(
            'need to migrate my website from wix', frozenset({'rate my'})
        ))

    def test_has_word_start_phrase_rejects_celebrate_my(self):
        # Same defensive check for 'celebrate my' / 'berate my' — any word
        # ending in 'rate' would false-match plain substring 'rate my'.
        self.assertFalse(_has_word_start_phrase(
            'help me celebrate my launch', frozenset({'rate my'})
        ))
        self.assertFalse(_has_word_start_phrase(
            'someone keeps trying to berate my work', frozenset({'rate my'})
        ))

    def test_rate_my_is_in_word_start_phrases_set(self):
        # Regression guard: if someone moves 'rate my' back into _ALWAYS_EXCLUDE
        # (plain substring matching) the original 'migrate my' bug returns.
        self.assertIn('rate my', _ALWAYS_EXCLUDE_WORD_START_PHRASES)

    # --- Unknown subreddit ---

    def test_unknown_sub_requires_all_three(self):
        self.assertTrue(passes_light_filter(
            'Hiring web designer', 'Need a website built, budget $500 rate hourly', 'unknownsub'
        ))

    def test_unknown_sub_fails_missing_payment(self):
        self.assertFalse(passes_light_filter(
            'Hiring web designer', 'Need a website built', 'unknownsub'
        ))


# ---------------------------------------------------------------------------
# TestFetchRedditPosts
# ---------------------------------------------------------------------------

class TestFetchRedditPosts(unittest.TestCase):

    @patch('scripts.reddit_leads.http_get')
    def test_parses_entries(self, mock_get):
        mock_get.return_value = _ATOM_FEED
        posts = fetch_reddit_posts('forhire', 'https://www.reddit.com/r/forhire/.rss')
        self.assertEqual(len(posts), 2)
        self.assertEqual(posts[0]['title'], '[HIRING] Framer developer needed for landing page')
        self.assertEqual(posts[0]['url'], 'https://www.reddit.com/r/forhire/comments/abc123/hiring_framer/')
        self.assertEqual(posts[0]['subreddit'], 'forhire')
        self.assertIn('Framer developer', posts[0]['content'])

    @patch('scripts.reddit_leads.http_get')
    def test_post_date_uses_published_when_available(self, mock_get):
        # When both <published> and <updated> are present, post_date should be
        # the <published> value (original creation time), not <updated>.
        mock_get.return_value = _ATOM_FEED
        posts = fetch_reddit_posts('forhire', 'https://www.reddit.com/r/forhire/.rss')
        self.assertEqual(posts[0]['post_date'], '2024-03-01T08:00:00+00:00')

    @patch('scripts.reddit_leads.http_get')
    def test_post_date_falls_back_to_updated_when_no_published(self, mock_get):
        # When only <updated> is present (no <published>), post_date should
        # fall back to the <updated> value so we always capture a timestamp.
        mock_get.return_value = _ATOM_FEED_UPDATED_ONLY
        posts = fetch_reddit_posts('forhire', 'https://www.reddit.com/r/forhire/.rss')
        self.assertEqual(posts[0]['post_date'], '2024-04-01T12:00:00+00:00')

    @patch('scripts.reddit_leads.http_get')
    def test_html_stripped_from_content(self, mock_get):
        mock_get.return_value = _ATOM_FEED
        posts = fetch_reddit_posts('forhire', 'https://www.reddit.com/r/forhire/.rss')
        self.assertNotIn('<p>', posts[0]['content'])
        self.assertNotIn('&lt;', posts[0]['content'])

    @patch('scripts.reddit_leads.http_get')
    def test_empty_feed_returns_empty_list(self, mock_get):
        mock_get.return_value = _EMPTY_FEED
        posts = fetch_reddit_posts('forhire', 'https://www.reddit.com/r/forhire/.rss')
        self.assertEqual(posts, [])

    @patch('scripts.reddit_leads.http_get')
    def test_malformed_xml_returns_none(self, mock_get):
        mock_get.return_value = _MALFORMED_FEED
        posts = fetch_reddit_posts('forhire', 'https://www.reddit.com/r/forhire/.rss')
        self.assertIsNone(posts)

    @patch('scripts.reddit_leads.http_get', side_effect=Exception('network error'))
    def test_fetch_error_returns_none(self, mock_get):
        posts = fetch_reddit_posts('forhire', 'https://www.reddit.com/r/forhire/.rss')
        self.assertIsNone(posts)

    @patch('scripts.reddit_leads.http_get')
    def test_empty_feed_returns_empty_list_not_none(self, mock_get):
        mock_get.return_value = _EMPTY_FEED
        posts = fetch_reddit_posts('forhire', 'https://www.reddit.com/r/forhire/.rss')
        self.assertEqual(posts, [])

    # ------------------------------------------------------------------
    # error_samples optional parameter — populated only on failure paths
    # so the caller can surface a sample of root causes in the aggregated
    # fetch-failure Discord alert.
    # ------------------------------------------------------------------

    @patch('scripts.reddit_leads.http_get')
    def test_error_samples_not_appended_on_success(self, mock_get):
        """A successful fetch must not append anything to error_samples."""
        mock_get.return_value = _ATOM_FEED
        samples: list[str] = []
        fetch_reddit_posts('forhire', 'https://www.reddit.com/r/forhire/.rss', samples)
        self.assertEqual(samples, [])

    @patch('scripts.reddit_leads.http_get')
    def test_error_samples_appended_on_http_error(self, mock_get):
        """HTTPError must append ``r/<sub> HTTP <code>``."""
        mock_get.side_effect = urllib.error.HTTPError(
            'https://www.reddit.com/r/forhire/.rss', 500, 'Server Error', {}, None,
        )
        samples: list[str] = []
        result = fetch_reddit_posts('forhire', 'https://www.reddit.com/r/forhire/.rss', samples)
        self.assertIsNone(result)
        self.assertEqual(samples, ['r/forhire HTTP 500'])

    @patch('scripts.reddit_leads.http_get')
    def test_error_samples_appended_on_http_429(self, mock_get):
        """A 429 must show as ``HTTP 429`` so rate-limiting is visibly distinct from 500s."""
        mock_get.side_effect = urllib.error.HTTPError(
            'https://www.reddit.com/r/figma/.rss', 429, 'Too Many Requests', {}, None,
        )
        samples: list[str] = []
        fetch_reddit_posts('figma', 'https://www.reddit.com/r/figma/.rss', samples)
        self.assertEqual(samples, ['r/figma HTTP 429'])

    @patch('scripts.reddit_leads.http_get', side_effect=urllib.error.URLError('refused'))
    def test_error_samples_appended_on_url_error(self, mock_get):
        """A URLError (e.g. connection refused) must append the exception class name."""
        samples: list[str] = []
        fetch_reddit_posts('forhire', 'https://www.reddit.com/r/forhire/.rss', samples)
        self.assertEqual(samples, ['r/forhire URLError'])

    @patch('scripts.reddit_leads.http_get', side_effect=Exception('boom'))
    def test_error_samples_appended_on_generic_exception(self, mock_get):
        """A generic Exception must append the class name (not the message)."""
        samples: list[str] = []
        fetch_reddit_posts('forhire', 'https://www.reddit.com/r/forhire/.rss', samples)
        self.assertEqual(samples, ['r/forhire Exception'])

    @patch('scripts.reddit_leads.http_get')
    def test_error_samples_appended_on_parse_error(self, mock_get):
        """A malformed feed (XML ParseError) must append ``r/<sub> ParseError``."""
        mock_get.return_value = _MALFORMED_FEED
        samples: list[str] = []
        result = fetch_reddit_posts('forhire', 'https://www.reddit.com/r/forhire/.rss', samples)
        self.assertIsNone(result)
        self.assertEqual(samples, ['r/forhire ParseError'])

    @patch('scripts.reddit_leads.http_get', side_effect=Exception('network error'))
    def test_default_no_error_samples_param_does_not_raise(self, mock_get):
        """Backward-compat: calling without error_samples must still work and return None."""
        result = fetch_reddit_posts('forhire', 'https://www.reddit.com/r/forhire/.rss')
        self.assertIsNone(result)

    # ------------------------------------------------------------------
    # body_preview capture on XML ParseError — without this, the log only
    # contains the xml.etree parse-position message (e.g. "syntax error:
    # line 1, column 0") which gives no signal whether Reddit returned an
    # HTML "reddit broke!" page (with HTTP 200, observed in some shard-
    # outage modes), a captcha/auth challenge page, a JSON rate-limit body,
    # or something else entirely.  Mirrors the body_preview capture already
    # in place for HTTP errors above the parse step.
    # ------------------------------------------------------------------

    @patch('scripts.reddit_leads.http_get')
    def test_parse_error_logs_body_preview(self, mock_get):
        """A ParseError must log the first 500 chars of the response body as
        ``body_preview`` so the maintainer can see what Reddit returned."""
        import error_log as el
        # Simulate the "reddit broke!" HTML page Reddit serves on outage.
        # Use unclosed/mismatched tags so xml.etree raises ParseError —
        # well-formed HTML happens to also be well-formed XML and would
        # parse as an empty entry list instead of triggering ParseError.
        html_body = (
            '<html><head><title>reddit broke!</title>'
            '<body><p>Sorry, an error occurred.</br></body>'
        )
        mock_get.return_value = html_body
        with patch.object(el, 'log_error') as mock_log:
            result = fetch_reddit_posts('forhire', 'https://www.reddit.com/r/forhire/.rss')
        self.assertIsNone(result)
        self.assertTrue(mock_log.called)
        ctx = mock_log.call_args[0][3]
        self.assertIn('body_preview', ctx)
        self.assertIn('reddit broke!', ctx['body_preview'])
        # Existing diagnostic field is preserved.
        self.assertIn('error', ctx)
        # Severity remains 'warning'.
        self.assertEqual(mock_log.call_args[0][1], 'warning')

    @patch('scripts.reddit_leads.http_get')
    def test_parse_error_truncates_long_body_preview(self, mock_get):
        """The captured body must be capped at 500 chars to keep
        ``logs/errors.jsonl`` lines manageable."""
        import error_log as el
        # 1000-char body that does not parse as XML.
        mock_get.return_value = 'A' * 1000
        with patch.object(el, 'log_error') as mock_log:
            fetch_reddit_posts('forhire', 'https://www.reddit.com/r/forhire/.rss')
        ctx = mock_log.call_args[0][3]
        self.assertLessEqual(len(ctx['body_preview']), 500)

    @patch('scripts.reddit_leads.http_get')
    def test_parse_error_empty_body_preview_is_empty_string(self, mock_get):
        """If the body itself is empty (rare but possible — a transient
        proxy/CDN bug), ``body_preview`` must be ``''`` not raise."""
        import error_log as el
        mock_get.return_value = ''
        with patch.object(el, 'log_error') as mock_log:
            fetch_reddit_posts('forhire', 'https://www.reddit.com/r/forhire/.rss')
        ctx = mock_log.call_args[0][3]
        self.assertEqual(ctx.get('body_preview', None), '')

    # ------------------------------------------------------------------
    # body_preview capture on HTTPError — the log must include the first
    # 500 chars of the response body so the operator can see what Reddit
    # returned (e.g. the "reddit broke!" HTML page).  Previously this was
    # truncated to 200 chars, which cut off the useful content and was
    # inconsistent with the 500-char cap used by the ParseError branch
    # above and by all other body_preview captures in both scripts.
    # ------------------------------------------------------------------

    @patch('scripts.reddit_leads.http_get')
    def test_http_error_logs_body_preview(self, mock_get):
        """An HTTPError must log the response body as ``body_preview``."""
        import io
        import error_log as el
        response_body = b'<html><head><title>reddit broke!</title></head></html>'
        mock_get.side_effect = urllib.error.HTTPError(
            'https://www.reddit.com/r/forhire/.rss', 500, 'Internal Server Error',
            {}, io.BytesIO(response_body),
        )
        with patch.object(el, 'log_error') as mock_log:
            result = fetch_reddit_posts('forhire', 'https://www.reddit.com/r/forhire/.rss')
        self.assertIsNone(result)
        self.assertTrue(mock_log.called)
        ctx = mock_log.call_args[0][3]
        self.assertIn('body_preview', ctx)
        self.assertIn('reddit broke!', ctx['body_preview'])

    @patch('scripts.reddit_leads.http_get')
    def test_http_error_truncates_body_preview_to_500(self, mock_get):
        """The HTTPError body_preview must be capped at 500 chars, consistent
        with the ParseError branch and all other body captures in the codebase."""
        import io
        import error_log as el
        long_body = b'X' * 1000
        mock_get.side_effect = urllib.error.HTTPError(
            'https://www.reddit.com/r/forhire/.rss', 500, 'Internal Server Error',
            {}, io.BytesIO(long_body),
        )
        with patch.object(el, 'log_error') as mock_log:
            fetch_reddit_posts('forhire', 'https://www.reddit.com/r/forhire/.rss')
        ctx = mock_log.call_args[0][3]
        self.assertLessEqual(len(ctx['body_preview']), 500)
        self.assertGreater(len(ctx['body_preview']), 200,
                           'body_preview must capture more than the old 200-char limit')


# ---------------------------------------------------------------------------
# TestUrlExistsInNotion
# ---------------------------------------------------------------------------

class TestUrlExistsInNotion(unittest.TestCase):

    @patch('scripts.reddit_leads.http_post')
    def test_returns_true_when_found(self, mock_post):
        mock_post.return_value = {'results': [{'id': 'page-123'}]}
        self.assertTrue(url_exists_in_notion('https://reddit.com/r/foo/1', 'db-id'))

    @patch('scripts.reddit_leads.http_post')
    def test_returns_false_when_not_found(self, mock_post):
        mock_post.return_value = {'results': []}
        self.assertFalse(url_exists_in_notion('https://reddit.com/r/foo/1', 'db-id'))

    @patch('scripts.reddit_leads.http_post')
    def test_sends_url_filter(self, mock_post):
        mock_post.return_value = {'results': []}
        url_exists_in_notion('https://reddit.com/r/foo/1', 'db-123')
        _, kwargs = mock_post.call_args
        body = mock_post.call_args[0][1]
        self.assertEqual(body['filter']['property'], 'URL')
        self.assertEqual(body['filter']['url']['equals'], 'https://reddit.com/r/foo/1')
        self.assertEqual(body['page_size'], 1)


# ---------------------------------------------------------------------------
# TestSaveLeadToNotion
# ---------------------------------------------------------------------------

class TestSaveLeadToNotion(unittest.TestCase):

    @patch('scripts.reddit_leads.http_post')
    def test_saves_with_pending_status(self, mock_post):
        mock_post.return_value = {}
        lead = {
            'title': 'Hiring Framer dev',
            'url': 'https://reddit.com/r/forhire/1',
            'subreddit': 'forhire',
            'content': 'Need a developer with Framer skills',
            'post_date': '2024-03-01T10:00:00+00:00',
        }
        save_lead_to_notion(lead, 'db-id')
        body = mock_post.call_args[0][1]
        props = body['properties']
        self.assertEqual(props['Status']['select']['name'], 'pending')

    @patch('scripts.reddit_leads.http_post')
    def test_subreddit_is_select_type(self, mock_post):
        mock_post.return_value = {}
        lead = {
            'title': 'Test', 'url': 'https://reddit.com/1',
            'subreddit': 'framer', 'content': 'content', 'post_date': '',
        }
        save_lead_to_notion(lead, 'db-id')
        props = mock_post.call_args[0][1]['properties']
        self.assertIn('select', props['Subreddit'])
        self.assertEqual(props['Subreddit']['select']['name'], 'framer')

    @patch('scripts.reddit_leads.http_post')
    def test_content_truncated_to_2000(self, mock_post):
        mock_post.return_value = {}
        lead = {
            'title': 'Test', 'url': 'https://reddit.com/1',
            'subreddit': 'framer', 'content': 'x' * 3000, 'post_date': '',
        }
        save_lead_to_notion(lead, 'db-id')
        props = mock_post.call_args[0][1]['properties']
        content_val = props['Content']['rich_text'][0]['text']['content']
        self.assertEqual(len(content_val), 2000)

    @patch('scripts.reddit_leads.http_post')
    def test_content_with_supplementary_emoji_fits_notion_utf16_limit(self, mock_post):
        """Content with supplementary-plane chars must fit Notion's UTF-16 limit.

        Reproduces the 2026-04-29 r/smallbusiness 400 error where a Python
        ``[:2000]`` slice yielded 2001 UTF-16 code units in Notion's count.
        """
        mock_post.return_value = {}
        # U+1F600 = 1 code point, 2 UTF-16 code units
        lead = {
            'title': 'Test', 'url': 'https://reddit.com/1',
            'subreddit': 'framer', 'content': '\U0001F600' * 1500, 'post_date': '',
        }
        save_lead_to_notion(lead, 'db-id')
        props = mock_post.call_args[0][1]['properties']
        content_val = props['Content']['rich_text'][0]['text']['content']
        utf16_units = len(content_val.encode('utf-16-le')) // 2
        self.assertLessEqual(utf16_units, 2000)

    @patch('scripts.reddit_leads.http_post')
    def test_title_with_supplementary_emoji_fits_notion_utf16_limit(self, mock_post):
        """Title field must also be truncated UTF-16-aware."""
        mock_post.return_value = {}
        lead = {
            'title': '\U0001F600' * 1500, 'url': 'https://reddit.com/1',
            'subreddit': 'framer', 'content': 'short', 'post_date': '',
        }
        save_lead_to_notion(lead, 'db-id')
        props = mock_post.call_args[0][1]['properties']
        title_val = props['Name']['title'][0]['text']['content']
        utf16_units = len(title_val.encode('utf-16-le')) // 2
        self.assertLessEqual(utf16_units, 2000)

    @patch('scripts.reddit_leads.http_post')
    def test_post_date_included_when_present(self, mock_post):
        mock_post.return_value = {}
        lead = {
            'title': 'Test', 'url': 'https://reddit.com/1',
            'subreddit': 'framer', 'content': 'content',
            'post_date': '2024-03-01T10:00:00+00:00',
        }
        save_lead_to_notion(lead, 'db-id')
        props = mock_post.call_args[0][1]['properties']
        self.assertIn('Post Date', props)

    @patch('scripts.reddit_leads.http_post')
    def test_post_date_omitted_when_empty(self, mock_post):
        mock_post.return_value = {}
        lead = {
            'title': 'Test', 'url': 'https://reddit.com/1',
            'subreddit': 'framer', 'content': 'content', 'post_date': '',
        }
        save_lead_to_notion(lead, 'db-id')
        props = mock_post.call_args[0][1]['properties']
        self.assertNotIn('Post Date', props)

    @patch('scripts.reddit_leads.http_post')
    def test_discovered_timestamp_is_utc(self, mock_post):
        """Discovered date must be a UTC-aware ISO 8601 timestamp (ends with +00:00)."""
        mock_post.return_value = {}
        lead = {
            'title': 'Test', 'url': 'https://reddit.com/1',
            'subreddit': 'framer', 'content': 'content', 'post_date': '',
        }
        save_lead_to_notion(lead, 'db-id')
        props = mock_post.call_args[0][1]['properties']
        discovered = props['Discovered']['date']['start']
        self.assertTrue(
            discovered.endswith('+00:00'),
            f'Expected UTC timestamp ending in +00:00, got: {discovered!r}',
        )

    @patch('scripts.reddit_leads.http_post')
    def test_discovered_timestamp_is_parseable_iso8601(self, mock_post):
        """Discovered date must be a valid ISO 8601 datetime string."""
        from datetime import datetime
        mock_post.return_value = {}
        lead = {
            'title': 'Test', 'url': 'https://reddit.com/1',
            'subreddit': 'framer', 'content': 'content', 'post_date': '',
        }
        save_lead_to_notion(lead, 'db-id')
        props = mock_post.call_args[0][1]['properties']
        discovered = props['Discovered']['date']['start']
        dt = datetime.fromisoformat(discovered)
        self.assertIsNotNone(dt.tzinfo, 'Discovered timestamp must be timezone-aware')


# ---------------------------------------------------------------------------
# TestSaveFailedSentinelToNotion
# ---------------------------------------------------------------------------

class TestSaveFailedSentinelToNotion(unittest.TestCase):

    @patch('scripts.reddit_leads.http_post')
    def test_writes_sentinel_page_with_failed_status(self, mock_post):
        mock_post.return_value = {}
        lead = {'url': 'https://reddit.com/r/forhire/1', 'title': 'Bad lead'}
        save_failed_sentinel_to_notion(lead, 'db-id')
        mock_post.assert_called_once()
        props = mock_post.call_args[0][1]['properties']
        self.assertEqual(props['Status']['select']['name'], 'failed')

    @patch('scripts.reddit_leads.http_post')
    def test_stores_url_in_sentinel(self, mock_post):
        mock_post.return_value = {}
        lead = {'url': 'https://reddit.com/r/forhire/2', 'title': 'Bad lead'}
        save_failed_sentinel_to_notion(lead, 'db-id')
        props = mock_post.call_args[0][1]['properties']
        self.assertEqual(props['URL']['url'], 'https://reddit.com/r/forhire/2')

    @patch('scripts.reddit_leads.http_post')
    def test_sentinel_name_is_placeholder(self, mock_post):
        """Sentinel must not use lead title (which may have triggered the 400)."""
        mock_post.return_value = {}
        lead = {'url': 'https://reddit.com/r/forhire/3', 'title': 'Problematic title \u0000'}
        save_failed_sentinel_to_notion(lead, 'db-id')
        props = mock_post.call_args[0][1]['properties']
        name = props['Name']['title'][0]['text']['content']
        self.assertEqual(name, '[save-failed sentinel]')

    @patch('scripts.reddit_leads.http_post', side_effect=Exception('Notion down'))
    def test_swallows_exception_when_sentinel_write_fails(self, mock_post):
        """If writing the sentinel itself fails, no exception must propagate."""
        lead = {'url': 'https://reddit.com/r/forhire/4', 'title': 'Test'}
        # Should not raise
        save_failed_sentinel_to_notion(lead, 'db-id')

    @patch('scripts.reddit_leads.http_post', side_effect=Exception('Notion down'))
    def test_logs_error_when_sentinel_write_fails(self, mock_post):
        """If writing the sentinel itself fails, it must be logged."""
        import error_log as el
        lead = {'url': 'https://reddit.com/r/forhire/5', 'title': 'Test'}
        with patch.object(el, 'log_error') as mock_log:
            save_failed_sentinel_to_notion(lead, 'db-id')
        self.assertTrue(mock_log.called)
        ctx = mock_log.call_args[0][3]
        self.assertEqual(ctx.get('url'), 'https://reddit.com/r/forhire/5')
        self.assertIn('error', ctx)


# ---------------------------------------------------------------------------
# TestGetPendingLeads
# ---------------------------------------------------------------------------

class TestGetPendingLeads(unittest.TestCase):

    @patch('scripts.reddit_leads.http_post')
    def test_applies_pending_filter(self, mock_post):
        mock_post.return_value = {'results': [], 'has_more': False}
        get_pending_leads('db-id')
        body = mock_post.call_args[0][1]
        self.assertEqual(body['filter']['property'], 'Status')
        self.assertEqual(body['filter']['select']['equals'], 'pending')

    @patch('scripts.reddit_leads.http_post')
    def test_parses_results(self, mock_post):
        mock_post.return_value = {
            'results': [{
                'id': 'page-abc',
                'properties': {
                    'Name': {'title': [{'plain_text': 'My lead'}]},
                    'URL': {'url': 'https://reddit.com/1'},
                    'Subreddit': {'select': {'name': 'framer'}},
                    'Content': {'rich_text': [{'plain_text': 'Some content'}]},
                },
            }],
            'has_more': False,
        }
        leads = get_pending_leads('db-id')
        self.assertEqual(len(leads), 1)
        self.assertEqual(leads[0]['page_id'], 'page-abc')
        self.assertEqual(leads[0]['title'], 'My lead')
        self.assertEqual(leads[0]['url'], 'https://reddit.com/1')
        self.assertEqual(leads[0]['subreddit'], 'framer')

    @patch('scripts.reddit_leads.http_post')
    def test_paginates(self, mock_post):
        mock_post.side_effect = [
            {
                'results': [{'id': 'p1', 'properties': {
                    'Name': {'title': [{'plain_text': 'Lead 1'}]},
                    'URL': {'url': 'https://reddit.com/1'},
                    'Subreddit': {'select': {'name': 'framer'}},
                    'Content': {'rich_text': []},
                }}],
                'has_more': True,
                'next_cursor': 'cursor-abc',
            },
            {
                'results': [{'id': 'p2', 'properties': {
                    'Name': {'title': [{'plain_text': 'Lead 2'}]},
                    'URL': {'url': 'https://reddit.com/2'},
                    'Subreddit': {'select': None},
                    'Content': {'rich_text': []},
                }}],
                'has_more': False,
            },
        ]
        leads = get_pending_leads('db-id')
        self.assertEqual(len(leads), 2)
        self.assertEqual(mock_post.call_count, 2)
        second_call_body = mock_post.call_args_list[1][0][1]
        self.assertEqual(second_call_body['start_cursor'], 'cursor-abc')

    @patch('scripts.reddit_leads.http_post')
    def test_includes_post_date_when_present(self, mock_post):
        """post_date must be extracted from the Notion Post Date field when present."""
        mock_post.return_value = {
            'results': [{
                'id': 'page-dt',
                'properties': {
                    'Name': {'title': [{'plain_text': 'Lead with date'}]},
                    'URL': {'url': 'https://reddit.com/3'},
                    'Subreddit': {'select': {'name': 'forhire'}},
                    'Content': {'rich_text': [{'plain_text': 'Some content'}]},
                    'Post Date': {'date': {'start': '2026-04-20T08:00:00+00:00'}},
                },
            }],
            'has_more': False,
        }
        leads = get_pending_leads('db-id')
        self.assertEqual(leads[0]['post_date'], '2026-04-20T08:00:00+00:00')

    @patch('scripts.reddit_leads.http_post')
    def test_post_date_empty_string_when_field_absent(self, mock_post):
        """post_date must be an empty string when the Post Date property is missing."""
        mock_post.return_value = {
            'results': [{
                'id': 'page-nodt',
                'properties': {
                    'Name': {'title': [{'plain_text': 'Lead no date'}]},
                    'URL': {'url': 'https://reddit.com/4'},
                    'Subreddit': {'select': {'name': 'framer'}},
                    'Content': {'rich_text': []},
                },
            }],
            'has_more': False,
        }
        leads = get_pending_leads('db-id')
        self.assertEqual(leads[0]['post_date'], '')

    @patch('scripts.reddit_leads.http_post')
    def test_post_date_empty_string_when_date_value_is_null(self, mock_post):
        """post_date must be empty string when Post Date.date is null (Notion unset date)."""
        mock_post.return_value = {
            'results': [{
                'id': 'page-nulldt',
                'properties': {
                    'Name': {'title': [{'plain_text': 'Lead null date'}]},
                    'URL': {'url': 'https://reddit.com/5'},
                    'Subreddit': {'select': {'name': 'framer'}},
                    'Content': {'rich_text': []},
                    'Post Date': {'date': None},
                },
            }],
            'has_more': False,
        }
        leads = get_pending_leads('db-id')
        self.assertEqual(leads[0]['post_date'], '')


# ---------------------------------------------------------------------------
# TestGetUnnotifiedApprovedLeads
# ---------------------------------------------------------------------------

class TestGetUnnotifiedApprovedLeads(unittest.TestCase):

    @patch('scripts.reddit_leads.http_post')
    def test_applies_approved_and_unnotified_filter(self, mock_post):
        """The Notion query must AND together Status=approved and Notified=False."""
        mock_post.return_value = {'results': [], 'has_more': False}
        get_unnotified_approved_leads('db-id')
        body = mock_post.call_args[0][1]
        self.assertIn('and', body['filter'])
        clauses = body['filter']['and']
        self.assertEqual(len(clauses), 2)
        # Status=approved
        status_clause = next(c for c in clauses if c.get('property') == 'Status')
        self.assertEqual(status_clause['select']['equals'], 'approved')
        # Notified checkbox=False
        notified_clause = next(c for c in clauses if c.get('property') == 'Notified')
        self.assertEqual(notified_clause['checkbox']['equals'], False)

    @patch('scripts.reddit_leads.http_post')
    def test_query_targets_correct_database(self, mock_post):
        mock_post.return_value = {'results': [], 'has_more': False}
        get_unnotified_approved_leads('db-xyz')
        called_url = mock_post.call_args[0][0]
        self.assertIn('db-xyz', called_url)
        self.assertTrue(called_url.endswith('/query'))

    @patch('scripts.reddit_leads.http_post')
    def test_parses_results_includes_review_notes(self, mock_post):
        """review_notes must be extracted so --notify can rebuild the Discord embed."""
        mock_post.return_value = {
            'results': [{
                'id': 'page-abc',
                'properties': {
                    'Name': {'title': [{'plain_text': 'Approved lead'}]},
                    'URL': {'url': 'https://reddit.com/1'},
                    'Subreddit': {'select': {'name': 'forhire'}},
                    'Content': {'rich_text': [{'plain_text': 'Some content'}]},
                    'Review Notes': {'rich_text': [{'plain_text': 'Genuine client hiring'}]},
                },
            }],
            'has_more': False,
        }
        leads = get_unnotified_approved_leads('db-id')
        self.assertEqual(len(leads), 1)
        self.assertEqual(leads[0]['page_id'], 'page-abc')
        self.assertEqual(leads[0]['title'], 'Approved lead')
        self.assertEqual(leads[0]['url'], 'https://reddit.com/1')
        self.assertEqual(leads[0]['subreddit'], 'forhire')
        self.assertEqual(leads[0]['review_notes'], 'Genuine client hiring')

    @patch('scripts.reddit_leads.http_post')
    def test_review_notes_empty_string_when_field_missing(self, mock_post):
        mock_post.return_value = {
            'results': [{
                'id': 'page-1',
                'properties': {
                    'Name': {'title': [{'plain_text': 'Lead'}]},
                    'URL': {'url': 'https://reddit.com/1'},
                    'Subreddit': {'select': {'name': 'framer'}},
                    'Content': {'rich_text': []},
                },
            }],
            'has_more': False,
        }
        leads = get_unnotified_approved_leads('db-id')
        self.assertEqual(leads[0]['review_notes'], '')

    @patch('scripts.reddit_leads.http_post')
    def test_paginates(self, mock_post):
        mock_post.side_effect = [
            {
                'results': [{'id': 'p1', 'properties': {
                    'Name': {'title': [{'plain_text': 'Lead 1'}]},
                    'URL': {'url': 'https://reddit.com/1'},
                    'Subreddit': {'select': {'name': 'framer'}},
                    'Content': {'rich_text': []},
                    'Review Notes': {'rich_text': []},
                }}],
                'has_more': True,
                'next_cursor': 'cursor-abc',
            },
            {
                'results': [{'id': 'p2', 'properties': {
                    'Name': {'title': [{'plain_text': 'Lead 2'}]},
                    'URL': {'url': 'https://reddit.com/2'},
                    'Subreddit': {'select': None},
                    'Content': {'rich_text': []},
                    'Review Notes': {'rich_text': []},
                }}],
                'has_more': False,
            },
        ]
        leads = get_unnotified_approved_leads('db-id')
        self.assertEqual(len(leads), 2)
        self.assertEqual(mock_post.call_count, 2)
        second_call_body = mock_post.call_args_list[1][0][1]
        self.assertEqual(second_call_body['start_cursor'], 'cursor-abc')

    @patch('scripts.reddit_leads.http_post')
    def test_includes_post_date_when_present(self, mock_post):
        mock_post.return_value = {
            'results': [{
                'id': 'page-dt',
                'properties': {
                    'Name': {'title': [{'plain_text': 'Lead with date'}]},
                    'URL': {'url': 'https://reddit.com/3'},
                    'Subreddit': {'select': {'name': 'forhire'}},
                    'Content': {'rich_text': []},
                    'Review Notes': {'rich_text': []},
                    'Post Date': {'date': {'start': '2026-04-20T08:00:00+00:00'}},
                },
            }],
            'has_more': False,
        }
        leads = get_unnotified_approved_leads('db-id')
        self.assertEqual(leads[0]['post_date'], '2026-04-20T08:00:00+00:00')

    @patch('scripts.reddit_leads.http_post')
    def test_returns_empty_list_when_no_unnotified_approved(self, mock_post):
        """No approved-but-unnotified leads is the steady-state expected case."""
        mock_post.return_value = {'results': [], 'has_more': False}
        leads = get_unnotified_approved_leads('db-id')
        self.assertEqual(leads, [])


# ---------------------------------------------------------------------------
# TestGetLeadById
# ---------------------------------------------------------------------------

class TestGetLeadById(unittest.TestCase):

    @patch('scripts.reddit_leads.http_get')
    def test_parses_page(self, mock_get):
        mock_get.return_value = json.dumps({
            'id': 'page-xyz',
            'properties': {
                'Name': {'title': [{'plain_text': 'Hiring Framer dev'}]},
                'URL': {'url': 'https://reddit.com/r/forhire/1'},
                'Subreddit': {'select': {'name': 'forhire'}},
                'Content': {'rich_text': [{'plain_text': 'Need a developer'}]},
                'Review Notes': {'rich_text': [{'plain_text': 'Good lead with budget'}]},
            },
        })
        lead = get_lead_by_id('page-xyz')
        self.assertEqual(lead['page_id'], 'page-xyz')
        self.assertEqual(lead['title'], 'Hiring Framer dev')
        self.assertEqual(lead['review_notes'], 'Good lead with budget')

    @patch('scripts.reddit_leads.http_get')
    def test_empty_review_notes(self, mock_get):
        mock_get.return_value = json.dumps({
            'id': 'page-xyz',
            'properties': {
                'Name': {'title': [{'plain_text': 'Test'}]},
                'URL': {'url': 'https://reddit.com/1'},
                'Subreddit': {'select': {'name': 'framer'}},
                'Content': {'rich_text': []},
                'Review Notes': {'rich_text': []},
            },
        })
        lead = get_lead_by_id('page-xyz')
        self.assertEqual(lead['review_notes'], '')

    @patch('scripts.reddit_leads.http_get')
    def test_includes_post_date_when_present(self, mock_get):
        """get_lead_by_id must surface Post Date so notify can render embed timestamp."""
        mock_get.return_value = json.dumps({
            'id': 'page-xyz',
            'properties': {
                'Name': {'title': [{'plain_text': 'Test'}]},
                'URL': {'url': 'https://reddit.com/1'},
                'Subreddit': {'select': {'name': 'framer'}},
                'Content': {'rich_text': []},
                'Review Notes': {'rich_text': []},
                'Post Date': {'date': {'start': '2024-03-01T10:00:00+00:00'}},
            },
        })
        lead = get_lead_by_id('page-xyz')
        self.assertEqual(lead['post_date'], '2024-03-01T10:00:00+00:00')

    @patch('scripts.reddit_leads.http_get')
    def test_post_date_empty_string_when_field_absent(self, mock_get):
        """Pages saved before Post Date was tracked must still parse cleanly."""
        mock_get.return_value = json.dumps({
            'id': 'page-xyz',
            'properties': {
                'Name': {'title': [{'plain_text': 'Test'}]},
                'URL': {'url': 'https://reddit.com/1'},
                'Subreddit': {'select': {'name': 'framer'}},
                'Content': {'rich_text': []},
                'Review Notes': {'rich_text': []},
            },
        })
        lead = get_lead_by_id('page-xyz')
        self.assertEqual(lead['post_date'], '')

    @patch('scripts.reddit_leads.http_get')
    def test_post_date_empty_string_when_date_value_is_null(self, mock_get):
        """Notion may return ``Post Date.date: null`` for an empty date field."""
        mock_get.return_value = json.dumps({
            'id': 'page-xyz',
            'properties': {
                'Name': {'title': [{'plain_text': 'Test'}]},
                'URL': {'url': 'https://reddit.com/1'},
                'Subreddit': {'select': {'name': 'framer'}},
                'Content': {'rich_text': []},
                'Review Notes': {'rich_text': []},
                'Post Date': {'date': None},
            },
        })
        lead = get_lead_by_id('page-xyz')
        self.assertEqual(lead['post_date'], '')


# ---------------------------------------------------------------------------
# TestUpdateLeadStatus
# ---------------------------------------------------------------------------

class TestUpdateLeadStatus(unittest.TestCase):

    @patch('scripts.reddit_leads.http_patch')
    def test_patches_correct_page(self, mock_patch):
        mock_patch.return_value = {}
        update_lead_status('page-xyz', 'approved', 'Looks like a real lead')
        url = mock_patch.call_args[0][0]
        self.assertIn('page-xyz', url)

    @patch('scripts.reddit_leads.http_patch')
    def test_sets_status_and_notes(self, mock_patch):
        mock_patch.return_value = {}
        update_lead_status('page-xyz', 'rejected', 'Just asking for feedback')
        props = mock_patch.call_args[0][1]['properties']
        self.assertEqual(props['Status']['select']['name'], 'rejected')
        notes = props['Review Notes']['rich_text'][0]['text']['content']
        self.assertEqual(notes, 'Just asking for feedback')

    @patch('scripts.reddit_leads.http_patch')
    def test_notes_truncated_to_2000(self, mock_patch):
        mock_patch.return_value = {}
        update_lead_status('page-xyz', 'approved', 'x' * 3000)
        props = mock_patch.call_args[0][1]['properties']
        notes = props['Review Notes']['rich_text'][0]['text']['content']
        self.assertEqual(len(notes), 2000)

    @patch('scripts.reddit_leads.http_patch')
    def test_notes_with_supplementary_emoji_fits_notion_utf16_limit(self, mock_patch):
        """Review notes with supplementary-plane chars must fit UTF-16 limit."""
        mock_patch.return_value = {}
        update_lead_status('page-xyz', 'approved', '\U0001F600' * 1500)
        props = mock_patch.call_args[0][1]['properties']
        notes = props['Review Notes']['rich_text'][0]['text']['content']
        utf16_units = len(notes.encode('utf-16-le')) // 2
        self.assertLessEqual(utf16_units, 2000)

    @patch('scripts.reddit_leads.http_patch')
    def test_accepts_all_valid_statuses(self, mock_patch):
        """Each canonical status must round-trip cleanly through update_lead_status."""
        mock_patch.return_value = {}
        for status in ('pending', 'approved', 'rejected', 'failed'):
            update_lead_status('page-xyz', status, 'note')
            props = mock_patch.call_args[0][1]['properties']
            self.assertEqual(props['Status']['select']['name'], status)

    @patch('scripts.reddit_leads.http_patch')
    def test_rejects_typo_status_without_hitting_notion(self, mock_patch):
        """A typo like 'approve' must raise ValueError before any HTTP call."""
        # Without the validation guard, Notion silently creates a new select
        # option for any string, orphaning the lead from every later query.
        with self.assertRaises(ValueError) as cm:
            update_lead_status('page-xyz', 'approve', 'looks fine')
        # The error message must name the bad value and the accepted set so
        # an operator can fix the typo from the log alone.
        self.assertIn('approve', str(cm.exception))
        self.assertIn('approved', str(cm.exception))
        # No Notion patch should have been issued.
        mock_patch.assert_not_called()

    @patch('scripts.reddit_leads.http_patch')
    def test_rejects_empty_status(self, mock_patch):
        """An empty status string must raise rather than silently clearing the field."""
        with self.assertRaises(ValueError):
            update_lead_status('page-xyz', '', 'note')
        mock_patch.assert_not_called()

    @patch('scripts.reddit_leads.http_patch')
    def test_rejects_uppercase_status(self, mock_patch):
        """Status matching is case-sensitive — 'Approved' is not 'approved'."""
        # Notion select option names are case-sensitive; the existing 'approved'
        # option would not be matched by 'Approved', so we reject that too.
        with self.assertRaises(ValueError):
            update_lead_status('page-xyz', 'Approved', 'note')
        mock_patch.assert_not_called()

    def test_valid_statuses_set_is_frozen(self):
        """``_VALID_STATUSES`` must be a frozenset so it cannot be mutated at runtime."""
        # A regular ``set`` would be vulnerable to an accidental ``.add`` that
        # widens the accepted values without code review; use frozenset.
        self.assertIsInstance(_VALID_STATUSES, frozenset)
        self.assertEqual(
            _VALID_STATUSES, frozenset({'pending', 'approved', 'rejected', 'failed'})
        )


# ---------------------------------------------------------------------------
# TestUpdateStatusCli — the --update-status CLI must reject invalid statuses
# with a non-zero exit so a reviewer typo cannot silently orphan a lead.
# ---------------------------------------------------------------------------

class TestUpdateStatusCli(unittest.TestCase):

    @patch.dict('os.environ', {
        'NOTION_TOKEN': 'ntn_test',
        'NOTION_REDDIT_LEADS_DB_ID': 'db-test',
    })
    @patch('scripts.reddit_leads.load_dotenv')
    @patch('scripts.reddit_leads.update_lead_status')
    def test_passes_valid_status_through(self, mock_update, mock_env):
        cli(['--update-status', 'page-1', 'approved', 'Looks', 'fine'])
        # The notes argument joins all trailing args with spaces.
        mock_update.assert_called_once_with('page-1', 'approved', 'Looks fine')

    @patch.dict('os.environ', {
        'NOTION_TOKEN': 'ntn_test',
        'NOTION_REDDIT_LEADS_DB_ID': 'db-test',
    })
    @patch('scripts.reddit_leads.load_dotenv')
    def test_typo_status_exits_non_zero(self, mock_env):
        """A reviewer typo like 'approve' must exit non-zero without writing to Notion."""
        # We let the real update_lead_status raise; no Notion mock needed
        # because the ValueError must fire before any HTTP call.
        with patch('scripts.reddit_leads.http_patch') as mock_patch:
            with self.assertRaises(SystemExit) as cm:
                cli(['--update-status', 'page-1', 'approve', 'reason'])
            self.assertNotEqual(cm.exception.code, 0)
            mock_patch.assert_not_called()


# ---------------------------------------------------------------------------
# TestTruncateForNotion
# ---------------------------------------------------------------------------

class TestTruncateForNotion(unittest.TestCase):

    def test_empty_string_returns_empty(self):
        self.assertEqual(_truncate_for_notion(''), '')

    def test_short_ascii_returned_unchanged(self):
        self.assertEqual(_truncate_for_notion('hello'), 'hello')

    def test_long_ascii_truncated_to_limit(self):
        self.assertEqual(len(_truncate_for_notion('x' * 3000)), 2000)

    def test_supplementary_chars_fit_utf16_limit(self):
        # 1500 emoji = 1500 code points but 3000 UTF-16 code units
        result = _truncate_for_notion('\U0001F600' * 1500)
        utf16_units = len(result.encode('utf-16-le')) // 2
        self.assertLessEqual(utf16_units, 2000)

    def test_mixed_chars_fit_utf16_limit(self):
        result = _truncate_for_notion(('a' * 1900) + ('\U0001F600' * 200))
        utf16_units = len(result.encode('utf-16-le')) // 2
        self.assertLessEqual(utf16_units, 2000)

    def test_supplementary_chars_not_split_mid_surrogate(self):
        """Result must not contain a lone surrogate from cutting an emoji in half."""
        result = _truncate_for_notion('\U0001F600' * 1500)
        # Re-encoding as UTF-8 should succeed without errors if no lone surrogates.
        result.encode('utf-8')

    def test_custom_limit(self):
        self.assertEqual(len(_truncate_for_notion('x' * 100, limit=10)), 10)

    def test_value_at_exact_limit_unchanged(self):
        s = 'x' * 2000
        self.assertEqual(_truncate_for_notion(s), s)


# ---------------------------------------------------------------------------
# TestNotifyDiscordLead
# ---------------------------------------------------------------------------

class TestNotifyDiscordLead(unittest.TestCase):

    @patch.dict('os.environ', {'DISCORD_WEBHOOK_URL_LEADS': 'https://discord.com/webhook/leads'})
    @patch('scripts.reddit_leads.http_post')
    def test_sends_embed(self, mock_post):
        mock_post.return_value = {}
        lead = {
            'title': 'Hiring Framer dev',
            'url': 'https://reddit.com/r/forhire/1',
            'subreddit': 'forhire',
            'content': 'Need a Framer developer',
            'page_id': 'page-1',
        }
        notify_discord_lead(lead)
        url, body = mock_post.call_args[0]
        self.assertEqual(url, 'https://discord.com/webhook/leads')
        embed = body['embeds'][0]
        self.assertEqual(embed['title'], 'Hiring Framer dev')
        self.assertEqual(embed['url'], 'https://reddit.com/r/forhire/1')
        self.assertIn('forhire', embed['author']['name'])
        self.assertNotIn('footer', embed)
        self.assertNotIn('Need a Framer developer', embed['description'])

    @patch.dict('os.environ', {'DISCORD_WEBHOOK_URL_LEADS': 'https://discord.com/webhook/leads'})
    @patch('scripts.reddit_leads.http_post')
    def test_includes_review_notes_in_embed(self, mock_post):
        mock_post.return_value = {}
        lead = {
            'title': 'Hiring Framer dev',
            'url': 'https://reddit.com/r/forhire/1',
            'subreddit': 'forhire',
            'content': 'Need a Framer developer',
            'review_notes': 'Clear budget and timeline for Framer landing page',
        }
        notify_discord_lead(lead)
        embed = mock_post.call_args[0][1]['embeds'][0]
        self.assertEqual(
            embed['description'],
            '**Why this is a lead:** Clear budget and timeline for Framer landing page',
        )

    @patch.dict('os.environ', {'DISCORD_WEBHOOK_URL_LEADS': 'https://discord.com/webhook/leads'})
    @patch('scripts.reddit_leads.http_post')
    def test_omits_review_notes_when_empty(self, mock_post):
        mock_post.return_value = {}
        lead = {
            'title': 'Test', 'url': 'https://x.com', 'subreddit': 'framer',
            'content': 'Some content', 'review_notes': '',
        }
        notify_discord_lead(lead)
        embed = mock_post.call_args[0][1]['embeds'][0]
        self.assertEqual(embed['description'], '')

    @patch.dict('os.environ', {'DISCORD_WEBHOOK_URL_LEADS': 'https://discord.com/webhook/leads'})
    @patch('scripts.reddit_leads.http_post', side_effect=Exception('webhook down'))
    def test_swallows_exception(self, mock_post):
        lead = {'title': 'Test', 'url': 'https://x.com', 'subreddit': 'framer', 'content': ''}
        # Should not raise; should report failure to caller via False return.
        self.assertFalse(notify_discord_lead(lead))

    @patch.dict('os.environ', {'DISCORD_WEBHOOK_URL_LEADS': 'https://discord.com/webhook/leads'})
    @patch('scripts.reddit_leads.http_post')
    def test_returns_true_on_success(self, mock_post):
        mock_post.return_value = {}
        lead = {
            'title': 'Hiring Framer dev',
            'url': 'https://reddit.com/r/forhire/1',
            'subreddit': 'forhire',
            'content': '',
        }
        self.assertTrue(notify_discord_lead(lead))

    @patch.dict('os.environ', {'DISCORD_WEBHOOK_URL_LEADS': 'https://discord.com/webhook/leads'})
    @patch('scripts.reddit_leads.http_post')
    def test_includes_post_date_as_embed_timestamp(self, mock_post):
        """post_date on the lead must surface as the Discord embed ``timestamp``."""
        mock_post.return_value = {}
        lead = {
            'title': 'Hiring Framer dev',
            'url': 'https://reddit.com/r/forhire/1',
            'subreddit': 'forhire',
            'content': '',
            'review_notes': 'Real client',
            'post_date': '2024-03-01T10:00:00+00:00',
        }
        notify_discord_lead(lead)
        embed = mock_post.call_args[0][1]['embeds'][0]
        self.assertEqual(embed['timestamp'], '2024-03-01T10:00:00+00:00')

    @patch.dict('os.environ', {'DISCORD_WEBHOOK_URL_LEADS': 'https://discord.com/webhook/leads'})
    @patch('scripts.reddit_leads.http_post')
    def test_omits_timestamp_when_post_date_missing(self, mock_post):
        """Leads stored before Post Date was tracked must not crash the notify path."""
        mock_post.return_value = {}
        lead = {
            'title': 'Hiring Framer dev',
            'url': 'https://reddit.com/r/forhire/1',
            'subreddit': 'forhire',
            'content': '',
        }
        notify_discord_lead(lead)
        embed = mock_post.call_args[0][1]['embeds'][0]
        self.assertNotIn('timestamp', embed)

    @patch.dict('os.environ', {'DISCORD_WEBHOOK_URL_LEADS': 'https://discord.com/webhook/leads'})
    @patch('scripts.reddit_leads.http_post')
    def test_omits_timestamp_when_post_date_empty_string(self, mock_post):
        """An explicit empty ``post_date`` must not produce a malformed embed."""
        mock_post.return_value = {}
        lead = {
            'title': 'Hiring Framer dev',
            'url': 'https://reddit.com/r/forhire/1',
            'subreddit': 'forhire',
            'content': '',
            'post_date': '',
        }
        notify_discord_lead(lead)
        embed = mock_post.call_args[0][1]['embeds'][0]
        self.assertNotIn('timestamp', embed)

    @patch.dict('os.environ', {'DISCORD_WEBHOOK_URL_LEADS': 'https://discord.com/webhook/leads'})
    @patch('scripts.reddit_leads.http_post')
    def test_omits_timestamp_when_post_date_unparseable(self, mock_post):
        """A malformed post_date must be silently dropped, not 400 the webhook."""
        mock_post.return_value = {}
        lead = {
            'title': 'Hiring Framer dev',
            'url': 'https://reddit.com/r/forhire/1',
            'subreddit': 'forhire',
            'content': '',
            'post_date': 'not-an-iso-date',
        }
        notify_discord_lead(lead)
        embed = mock_post.call_args[0][1]['embeds'][0]
        self.assertNotIn('timestamp', embed)

    @patch.dict('os.environ', {'DISCORD_WEBHOOK_URL_LEADS': 'https://discord.com/webhook/leads'})
    @patch('scripts.reddit_leads.http_post', side_effect=Exception('webhook down'))
    def test_error_log_includes_url_and_subreddit_on_failure(self, mock_post):
        """When Discord notification fails, the error log context must include url and subreddit."""
        import error_log as el
        lead = {
            'title': 'Hiring Framer dev',
            'url': 'https://reddit.com/r/forhire/abc',
            'subreddit': 'forhire',
            'content': 'Need a developer',
        }
        with patch.object(el, 'log_error') as mock_log:
            notify_discord_lead(lead)
        self.assertTrue(mock_log.called)
        ctx = mock_log.call_args[0][3]
        self.assertEqual(ctx.get('url'), 'https://reddit.com/r/forhire/abc')
        self.assertEqual(ctx.get('subreddit'), 'forhire')
        self.assertIn('error', ctx)

    @patch.dict('os.environ', {'DISCORD_WEBHOOK_URL_LEADS': 'https://discord.com/webhook/leads'})
    def test_http_error_logs_discord_response_body(self):
        """When the Discord webhook raises an HTTPError, the API response body
        must be captured as ``discord_response`` so an operator can distinguish
        between a revoked webhook (401), deleted webhook (404), rate-limit
        (429), and malformed-payload rejection (400) -- all of which would
        otherwise log only ``"HTTP Error <code>: <reason>"``."""
        import io
        import error_log as el
        body = b'{"message": "Invalid Webhook Token", "code": 50027}'
        http_err = urllib.error.HTTPError(
            'https://discord.com/webhook/leads',
            401, 'Unauthorized', {}, io.BytesIO(body),
        )
        lead = {
            'title': 'Hiring Framer dev',
            'url': 'https://reddit.com/r/forhire/abc',
            'subreddit': 'forhire',
            'content': '',
        }
        with patch('scripts.reddit_leads.http_post', side_effect=http_err), \
             patch.object(el, 'log_error') as mock_log:
            result = notify_discord_lead(lead)
        # Failure must be reported to caller via False return.
        self.assertFalse(result)
        self.assertTrue(mock_log.called)
        ctx = mock_log.call_args[0][3]
        self.assertEqual(ctx.get('status'), 401)
        self.assertIn('discord_response', ctx)
        self.assertIn('Invalid Webhook Token', ctx['discord_response'])
        # Existing diagnostic context fields are preserved.
        self.assertEqual(ctx.get('url'), 'https://reddit.com/r/forhire/abc')
        self.assertEqual(ctx.get('subreddit'), 'forhire')
        self.assertIn('error', ctx)
        # Severity remains 'warning' (a Discord webhook failure is recoverable
        # via --list-unnotified-approved retry, not a hard error).
        self.assertEqual(mock_log.call_args[0][1], 'warning')

    @patch.dict('os.environ', {'DISCORD_WEBHOOK_URL_LEADS': 'https://discord.com/webhook/leads'})
    def test_http_error_truncates_long_discord_response_body(self):
        """The captured Discord response body must be capped at 500 chars to
        keep ``logs/errors.jsonl`` lines manageable."""
        import io
        import error_log as el
        body = b'A' * 1000
        # 400 is non-retriable so the http_post side_effect runs only once,
        # not 4x with exponential backoff.
        http_err = urllib.error.HTTPError(
            'https://discord.com/webhook/leads',
            400, 'Bad Request', {}, io.BytesIO(body),
        )
        lead = {
            'title': 'T', 'url': 'https://x.com', 'subreddit': 'forhire', 'content': '',
        }
        with patch('scripts.reddit_leads.http_post', side_effect=http_err), \
             patch.object(el, 'log_error') as mock_log:
            notify_discord_lead(lead)
        ctx = mock_log.call_args[0][3]
        self.assertLessEqual(len(ctx['discord_response']), 500)

    @patch.dict('os.environ', {'DISCORD_WEBHOOK_URL_LEADS': 'https://discord.com/webhook/leads'})
    @patch('scripts.reddit_leads.http_post',
           side_effect=TimeoutError('The read operation timed out'))
    def test_non_http_error_keeps_lighter_context(self, mock_post):
        """Non-HTTP exceptions must keep the existing lighter ``{error, url,
        subreddit}`` context (no ``status`` / ``discord_response`` -- there is
        no HTTP body to capture)."""
        import error_log as el
        lead = {
            'title': 'Hiring Framer dev',
            'url': 'https://reddit.com/r/forhire/abc',
            'subreddit': 'forhire',
            'content': '',
        }
        with patch.object(el, 'log_error') as mock_log:
            result = notify_discord_lead(lead)
        self.assertFalse(result)
        ctx = mock_log.call_args[0][3]
        self.assertNotIn('status', ctx)
        self.assertNotIn('discord_response', ctx)
        self.assertIn('error', ctx)
        self.assertEqual(ctx.get('url'), 'https://reddit.com/r/forhire/abc')
        self.assertEqual(ctx.get('subreddit'), 'forhire')


# ---------------------------------------------------------------------------
# TestMarkNotified
# ---------------------------------------------------------------------------

class TestMarkNotified(unittest.TestCase):

    @patch('scripts.reddit_leads.http_patch')
    def test_sets_notified_checkbox(self, mock_patch):
        mock_patch.return_value = {}
        mark_notified('page-abc')
        url = mock_patch.call_args[0][0]
        body = mock_patch.call_args[0][1]
        self.assertIn('page-abc', url)
        self.assertTrue(body['properties']['Notified']['checkbox'])


# ---------------------------------------------------------------------------
# TestNotifyCli — the --notify CLI handler must not flip the Notified
# checkbox when the Discord webhook fails, otherwise the lead is silently
# lost (never delivered, never retried).
# ---------------------------------------------------------------------------

class TestNotifyCli(unittest.TestCase):

    @patch('scripts.reddit_leads.load_dotenv')
    @patch('scripts.reddit_leads.mark_notified')
    @patch('scripts.reddit_leads.notify_discord_lead', return_value=True)
    @patch('scripts.reddit_leads.get_lead_by_id', return_value={
        'page_id': 'page-1', 'title': 'T', 'url': 'u', 'subreddit': 's', 'content': '',
    })
    def test_marks_notified_when_discord_succeeds(self, mock_get, mock_notify, mock_mark, mock_env):
        cli(['--notify', 'page-1'])
        mock_notify.assert_called_once()
        mock_mark.assert_called_once_with('page-1')

    @patch('scripts.reddit_leads.load_dotenv')
    @patch('scripts.reddit_leads.mark_notified')
    @patch('scripts.reddit_leads.notify_discord_lead', return_value=False)
    @patch('scripts.reddit_leads.get_lead_by_id', return_value={
        'page_id': 'page-1', 'title': 'T', 'url': 'u', 'subreddit': 's', 'content': '',
    })
    def test_does_not_mark_notified_when_discord_fails(self, mock_get, mock_notify, mock_mark, mock_env):
        with self.assertRaises(SystemExit) as cm:
            cli(['--notify', 'page-1'])
        # Non-zero exit so the reviewer session knows the notify failed.
        self.assertNotEqual(cm.exception.code, 0)
        mock_notify.assert_called_once()
        mock_mark.assert_not_called()


# ---------------------------------------------------------------------------
# TestListUnnotifiedApprovedCli — the --list-unnotified-approved CLI lets the
# reviewer recover from a previous --notify failure: a lead that was approved
# but whose Discord webhook POST failed is now Status=approved + Notified=False
# and would otherwise never be re-tried (--list-pending only sees Status=pending).
# ---------------------------------------------------------------------------

class TestListUnnotifiedApprovedCli(unittest.TestCase):

    @patch.dict('os.environ', {'NOTION_REDDIT_LEADS_DB_ID': 'db-test'})
    @patch('scripts.reddit_leads.load_dotenv')
    @patch('scripts.reddit_leads.get_unnotified_approved_leads')
    def test_calls_get_unnotified_approved_leads(self, mock_get, mock_env):
        mock_get.return_value = []
        cli(['--list-unnotified-approved'])
        mock_get.assert_called_once_with('db-test')

    @patch.dict('os.environ', {'NOTION_REDDIT_LEADS_DB_ID': 'db-test'})
    @patch('scripts.reddit_leads.load_dotenv')
    @patch('scripts.reddit_leads.get_unnotified_approved_leads')
    def test_prints_json_to_stdout(self, mock_get, mock_env):
        from io import StringIO
        sample_lead = {
            'page_id': 'p1', 'title': 'T', 'url': 'u', 'subreddit': 's',
            'content': '', 'review_notes': 'Real client', 'post_date': '',
        }
        mock_get.return_value = [sample_lead]
        with patch('sys.stdout', new_callable=StringIO) as mock_stdout:
            cli(['--list-unnotified-approved'])
        printed = json.loads(mock_stdout.getvalue())
        self.assertEqual(printed, [sample_lead])

    @patch.dict('os.environ', {'NOTION_REDDIT_LEADS_DB_ID': 'db-test'})
    @patch('scripts.reddit_leads.load_dotenv')
    @patch('scripts.reddit_leads.get_unnotified_approved_leads')
    def test_empty_result_prints_empty_array(self, mock_get, mock_env):
        from io import StringIO
        mock_get.return_value = []
        with patch('sys.stdout', new_callable=StringIO) as mock_stdout:
            cli(['--list-unnotified-approved'])
        self.assertEqual(json.loads(mock_stdout.getvalue()), [])


# ---------------------------------------------------------------------------
# TestMain
# ---------------------------------------------------------------------------

class TestMain(unittest.TestCase):

    def setUp(self):
        # Patch time.sleep for every test in this class so the inter-feed delay
        # does not cause real waits during unit tests.
        self._sleep_patcher = patch('scripts.reddit_leads.time.sleep')
        self._sleep_patcher.start()
        # Default: Notion preflight passes so existing dedup/main tests run
        # unchanged.  Tests that exercise preflight-failure paths start their
        # own patch (or stop this one) explicitly.
        self._preflight_patcher = patch(
            'scripts.reddit_leads._notion_preflight',
            return_value=(True, '', None, ''),
        )
        self._preflight_patcher.start()
        # Default: alert-suppression and state writes are no-ops in tests so
        # the suite does not touch a real state file or hide alerts based on
        # whatever state happens to be on disk.
        self._suppress_patcher = patch(
            'scripts.reddit_leads._should_suppress_alert',
            return_value=False,
        )
        self._suppress_patcher.start()
        self._record_patcher = patch('scripts.reddit_leads._record_alert_sent')
        self._record_patcher.start()

    def tearDown(self):
        self._sleep_patcher.stop()
        self._preflight_patcher.stop()
        self._suppress_patcher.stop()
        self._record_patcher.stop()

    @patch.dict('os.environ', {
        'NOTION_TOKEN': 'ntn_test',
        'NOTION_REDDIT_LEADS_DB_ID': 'db-test',
    })
    @patch('scripts.reddit_leads.save_lead_to_notion')
    @patch('scripts.reddit_leads.url_exists_in_notion', return_value=False)
    @patch('scripts.reddit_leads.fetch_reddit_posts')
    def test_saves_filtered_new_leads(self, mock_fetch, mock_exists, mock_save):
        mock_fetch.return_value = [{
            'title': '[HIRING] Need a Framer developer',
            'url': 'https://reddit.com/r/forhire/1',
            'subreddit': 'forhire',
            'content': 'Budget $500 for landing page website',
            'post_date': '2024-03-01T10:00:00+00:00',
        }]
        from scripts.reddit_leads import main
        main()
        mock_save.assert_called()

    @patch.dict('os.environ', {
        'NOTION_TOKEN': 'ntn_test',
        'NOTION_REDDIT_LEADS_DB_ID': 'db-test',
    })
    @patch('scripts.reddit_leads.save_lead_to_notion')
    @patch('scripts.reddit_leads.url_exists_in_notion', return_value=True)
    @patch('scripts.reddit_leads.fetch_reddit_posts')
    def test_skips_existing_urls(self, mock_fetch, mock_exists, mock_save):
        mock_fetch.return_value = [{
            'title': '[HIRING] Need a Framer developer',
            'url': 'https://reddit.com/r/forhire/1',
            'subreddit': 'forhire',
            'content': 'Budget $500 for landing page website',
            'post_date': '2024-03-01T10:00:00+00:00',
        }]
        from scripts.reddit_leads import main
        main()
        mock_save.assert_not_called()

    @patch.dict('os.environ', {
        'NOTION_TOKEN': 'ntn_test',
        'NOTION_REDDIT_LEADS_DB_ID': 'db-test',
        'DISCORD_ALERTS_WEBHOOK_URL': 'https://discord.com/alerts',
    })
    @patch('scripts.reddit_leads._warn_discord')
    @patch('scripts.reddit_leads.fetch_reddit_posts', return_value=None)
    def test_warns_when_all_fetches_fail(self, mock_fetch, mock_warn):
        from scripts.reddit_leads import main
        main()
        mock_warn.assert_called_once()

    @patch.dict('os.environ', {
        'NOTION_TOKEN': 'ntn_test',
        'NOTION_REDDIT_LEADS_DB_ID': 'db-test',
    })
    @patch('scripts.reddit_leads._warn_discord')
    @patch('scripts.reddit_leads.fetch_reddit_posts', return_value=[])
    def test_empty_feeds_do_not_count_as_errors(self, mock_fetch, mock_warn):
        """An empty feed (valid but no entries) must not increment fetch_errors."""
        from scripts.reddit_leads import main
        main()
        mock_warn.assert_not_called()

    @patch.dict('os.environ', {
        'NOTION_TOKEN': 'ntn_test',
        'NOTION_REDDIT_LEADS_DB_ID': 'db-test',
    })
    @patch('scripts.reddit_leads.save_failed_sentinel_to_notion')
    @patch('scripts.reddit_leads.save_lead_to_notion',
           side_effect=urllib.error.HTTPError(None, 400, 'Bad Request', {}, None))
    @patch('scripts.reddit_leads.url_exists_in_notion', return_value=False)
    @patch('scripts.reddit_leads.fetch_reddit_posts')
    def test_writes_sentinel_on_400_save_failure(self, mock_fetch, mock_exists, mock_save, mock_sentinel):
        """When save_lead_to_notion raises a 400, a sentinel page must be written."""
        mock_fetch.return_value = [{
            'title': '[HIRING] Need a Framer developer',
            'url': 'https://reddit.com/r/forhire/1',
            'subreddit': 'forhire',
            'content': 'Budget $500 for landing page website',
            'post_date': '2024-03-01T10:00:00+00:00',
        }]
        from scripts.reddit_leads import main
        main()
        # The mock returns the same post for every subreddit, so the sentinel
        # may be called multiple times (once per subreddit that passes the light
        # filter and gets a 400).  We assert it was called at least once.
        mock_sentinel.assert_called()

    @patch.dict('os.environ', {
        'NOTION_TOKEN': 'ntn_test',
        'NOTION_REDDIT_LEADS_DB_ID': 'db-test',
    })
    @patch('scripts.reddit_leads.save_failed_sentinel_to_notion')
    @patch('scripts.reddit_leads.save_lead_to_notion',
           side_effect=urllib.error.HTTPError(None, 500, 'Server Error', {}, None))
    @patch('scripts.reddit_leads.url_exists_in_notion', return_value=False)
    @patch('scripts.reddit_leads.fetch_reddit_posts')
    def test_does_not_write_sentinel_on_retriable_save_failure(self, mock_fetch, mock_exists, mock_save, mock_sentinel):
        """When save_lead_to_notion raises a retriable 500 error, no sentinel must be written."""
        mock_fetch.return_value = [{
            'title': '[HIRING] Need a Framer developer',
            'url': 'https://reddit.com/r/forhire/1',
            'subreddit': 'forhire',
            'content': 'Budget $500 for landing page website',
            'post_date': '2024-03-01T10:00:00+00:00',
        }]
        from scripts.reddit_leads import main
        main()
        mock_sentinel.assert_not_called()

    @patch.dict('os.environ', {
        'NOTION_TOKEN': 'ntn_test',
        'NOTION_REDDIT_LEADS_DB_ID': 'db-test',
        'DISCORD_ALERTS_WEBHOOK_URL': 'https://discord.com/alerts',
    })
    @patch('scripts.reddit_leads._warn_discord')
    @patch('scripts.reddit_leads.fetch_reddit_posts')
    def test_warns_when_majority_of_fetches_fail(self, mock_fetch, mock_warn):
        """A Discord alert must fire when >50% of feeds fail (but not all)."""
        from scripts.reddit_leads import main, REDDIT_FEEDS
        total = len(REDDIT_FEEDS)
        # Return None for just over half the feeds, [] for the rest
        majority_fail = total // 2 + 1
        mock_fetch.side_effect = [None] * majority_fail + [[]] * (total - majority_fail)
        main()
        mock_warn.assert_called_once()
        msg = mock_warn.call_args[0][0]
        self.assertIn('partial', msg.lower())

    @patch.dict('os.environ', {
        'NOTION_TOKEN': 'ntn_test',
        'NOTION_REDDIT_LEADS_DB_ID': 'db-test',
    })
    @patch('scripts.reddit_leads._warn_discord')
    @patch('scripts.reddit_leads.fetch_reddit_posts')
    def test_no_partial_warn_when_exactly_half_fail(self, mock_fetch, mock_warn):
        """No partial-failure alert when exactly half of feeds fail (threshold is >50%)."""
        from scripts.reddit_leads import main, REDDIT_FEEDS
        total = len(REDDIT_FEEDS)
        half = total // 2
        mock_fetch.side_effect = [None] * half + [[]] * (total - half)
        main()
        mock_warn.assert_not_called()

    @patch.dict('os.environ', {
        'NOTION_TOKEN': 'ntn_test',
        'NOTION_REDDIT_LEADS_DB_ID': 'db-test',
        'DISCORD_ALERTS_WEBHOOK_URL': 'https://discord.com/alerts',
    })
    @patch('scripts.reddit_leads._warn_discord')
    @patch('scripts.reddit_leads.fetch_reddit_posts', return_value=None)
    def test_all_fail_triggers_error_not_partial_warn(self, mock_fetch, mock_warn):
        """When all feeds fail, the all-fail branch fires (not the partial branch)."""
        from scripts.reddit_leads import main
        main()
        mock_warn.assert_called_once()
        msg = mock_warn.call_args[0][0]
        # All-fail message must not say "partial"
        self.assertNotIn('partial', msg.lower())

    @patch.dict('os.environ', {
        'NOTION_TOKEN': 'ntn_test',
        'NOTION_REDDIT_LEADS_DB_ID': 'db-test',
        'DISCORD_ALERTS_WEBHOOK_URL': 'https://discord.com/alerts',
    })
    @patch('scripts.reddit_leads._warn_discord')
    @patch('scripts.reddit_leads.fetch_reddit_posts')
    def test_partial_fail_alert_includes_samples(self, mock_fetch, mock_warn):
        """The partial-failure Discord alert must include a ``Samples:`` section
        so an operator can spot a dominant root cause (e.g. all ``HTTP 500``)
        without opening logs/errors.jsonl.  Mirrors the diagnostic pattern
        already used by the dedup-failure alert.
        """
        from scripts.reddit_leads import main, REDDIT_FEEDS
        total = len(REDDIT_FEEDS)
        majority_fail = total // 2 + 1

        # Side-effect function that appends a sample to the list provided by
        # main() and then returns None (mirroring a real fetch failure).  Using
        # a side_effect callable instead of a fixed list lets us simulate the
        # real interaction between main() and fetch_reddit_posts: the latter
        # populates the caller-owned ``error_samples`` list on each failure.
        call_count = [0]
        def fake_fetch(subreddit, feed_url, samples=None):
            call_count[0] += 1
            if call_count[0] <= majority_fail:
                if samples is not None:
                    samples.append(f'r/{subreddit} HTTP 500')
                return None
            return []
        mock_fetch.side_effect = fake_fetch

        main()
        mock_warn.assert_called_once()
        msg = mock_warn.call_args[0][0]
        self.assertIn('partial', msg.lower())
        self.assertIn('Samples:', msg)
        # First sample should be present (we cap at 5)
        self.assertIn('HTTP 500', msg)

    @patch.dict('os.environ', {
        'NOTION_TOKEN': 'ntn_test',
        'NOTION_REDDIT_LEADS_DB_ID': 'db-test',
        'DISCORD_ALERTS_WEBHOOK_URL': 'https://discord.com/alerts',
    })
    @patch('scripts.reddit_leads._warn_discord')
    @patch('scripts.reddit_leads.fetch_reddit_posts')
    def test_partial_fail_alert_caps_samples_at_five(self, mock_fetch, mock_warn):
        """The Samples section must include at most 5 entries — more would make
        the Discord alert hard to scan and the full per-feed context is already
        in logs/errors.jsonl for deeper inspection.
        """
        from scripts.reddit_leads import main, REDDIT_FEEDS
        total = len(REDDIT_FEEDS)
        # Fail just over half so the partial branch fires (which reports the
        # numeric ratio).  We collect more than 5 samples so the cap matters.
        majority_fail = total // 2 + 1
        self.assertGreater(majority_fail, 5)  # guard the test's premise
        call_count = [0]
        def fake_fetch(subreddit, feed_url, samples=None):
            call_count[0] += 1
            if call_count[0] <= majority_fail:
                if samples is not None:
                    samples.append(f'r/{subreddit} HTTP 500')
                return None
            return []
        mock_fetch.side_effect = fake_fetch
        main()
        mock_warn.assert_called_once()
        msg = mock_warn.call_args[0][0]
        # Count occurrences of "HTTP 500" in the alert message — should be 5
        # (the cap), not majority_fail (which is the full failure count and
        # would clutter the alert if not capped).
        self.assertEqual(msg.count('HTTP 500'), 5)
        # The full failure count is still surfaced for context in the partial
        # branch's "X/Y subreddit feeds" prefix.
        self.assertIn(f'{majority_fail}/{total}', msg)

    @patch.dict('os.environ', {
        'NOTION_TOKEN': 'ntn_test',
        'NOTION_REDDIT_LEADS_DB_ID': 'db-test',
        'DISCORD_ALERTS_WEBHOOK_URL': 'https://discord.com/alerts',
    })
    @patch('scripts.reddit_leads._warn_discord')
    @patch('scripts.reddit_leads.fetch_reddit_posts')
    def test_all_fail_alert_includes_samples(self, mock_fetch, mock_warn):
        """The all-fail alert must also include the ``Samples:`` section so the
        operator can see the dominant failure mode immediately."""
        from scripts.reddit_leads import main
        def fake_fetch(subreddit, feed_url, samples=None):
            if samples is not None:
                samples.append(f'r/{subreddit} HTTP 503')
            return None
        mock_fetch.side_effect = fake_fetch
        main()
        mock_warn.assert_called_once()
        msg = mock_warn.call_args[0][0]
        self.assertNotIn('partial', msg.lower())  # all-fail branch, not partial
        self.assertIn('Samples:', msg)
        self.assertIn('HTTP 503', msg)

    @patch.dict('os.environ', {
        'NOTION_TOKEN': 'ntn_test',
        'NOTION_REDDIT_LEADS_DB_ID': 'db-test',
        'DISCORD_ALERTS_WEBHOOK_URL': 'https://discord.com/alerts',
    })
    @patch('scripts.reddit_leads._warn_discord')
    @patch('scripts.reddit_leads.fetch_reddit_posts')
    def test_no_alert_below_threshold_does_not_reference_samples(self, mock_fetch, mock_warn):
        """When the failure rate is at or below the 50% threshold, no alert
        fires at all — independent of whether samples were collected."""
        from scripts.reddit_leads import main, REDDIT_FEEDS
        total = len(REDDIT_FEEDS)
        half = total // 2  # exactly half — below the >50% threshold
        call_count = [0]
        def fake_fetch(subreddit, feed_url, samples=None):
            call_count[0] += 1
            if call_count[0] <= half:
                if samples is not None:
                    samples.append(f'r/{subreddit} HTTP 500')
                return None
            return []
        mock_fetch.side_effect = fake_fetch
        main()
        mock_warn.assert_not_called()

    @patch.dict('os.environ', {
        'NOTION_TOKEN': 'ntn_test',
        'NOTION_REDDIT_LEADS_DB_ID': 'db-test',
    })
    @patch('scripts.reddit_leads.save_lead_to_notion')
    @patch('scripts.reddit_leads.url_exists_in_notion', return_value=False)
    @patch('scripts.reddit_leads.fetch_reddit_posts')
    def test_filters_out_non_leads(self, mock_fetch, mock_exists, mock_save):
        mock_fetch.return_value = [{
            'title': 'Framer tutorial for beginners',
            'url': 'https://reddit.com/r/framer/1',
            'subreddit': 'framer',
            'content': 'How to use Framer, a beginner tutorial course',
            'post_date': '',
        }]
        from scripts.reddit_leads import main
        main()
        mock_save.assert_not_called()

    @patch.dict('os.environ', {
        'NOTION_TOKEN': 'ntn_test',
        'NOTION_REDDIT_LEADS_DB_ID': 'db-test',
    })
    @patch('scripts.reddit_leads.save_failed_sentinel_to_notion')
    @patch('scripts.reddit_leads.save_lead_to_notion')
    @patch('scripts.reddit_leads.url_exists_in_notion',
           side_effect=Exception('Notion connection error'))
    @patch('scripts.reddit_leads.fetch_reddit_posts')
    def test_dedup_error_skips_post_without_sentinel(self, mock_fetch, mock_exists, mock_save, mock_sentinel):
        """When url_exists_in_notion raises, the post must be skipped and no sentinel written."""
        mock_fetch.return_value = [{
            'title': '[HIRING] Need a Framer developer',
            'url': 'https://reddit.com/r/forhire/1',
            'subreddit': 'forhire',
            'content': 'Budget $500 for landing page website',
            'post_date': '2024-03-01T10:00:00+00:00',
        }]
        from scripts.reddit_leads import main
        main()
        mock_save.assert_not_called()
        mock_sentinel.assert_not_called()

    @patch.dict('os.environ', {
        'NOTION_TOKEN': 'ntn_test',
        'NOTION_REDDIT_LEADS_DB_ID': 'db-test',
    })
    @patch('scripts.reddit_leads.save_failed_sentinel_to_notion')
    @patch('scripts.reddit_leads.save_lead_to_notion')
    @patch('scripts.reddit_leads.url_exists_in_notion',
           side_effect=Exception('Notion connection error'))
    @patch('scripts.reddit_leads.fetch_reddit_posts')
    def test_dedup_error_logs_warning_not_error(self, mock_fetch, mock_exists, mock_save, mock_sentinel):
        """A dedup-check failure must be logged as 'warning', not 'error', and include the URL."""
        import error_log as el
        mock_fetch.return_value = [{
            'title': '[HIRING] Need a Framer developer',
            'url': 'https://reddit.com/r/forhire/dedup-fail',
            'subreddit': 'forhire',
            'content': 'Budget $500 for landing page website',
            'post_date': '2024-03-01T10:00:00+00:00',
        }]
        with patch.object(el, 'log_error') as mock_log:
            from scripts.reddit_leads import main
            main()
        # At least one log_error call must be for the dedup failure
        dedup_calls = [
            c for c in mock_log.call_args_list
            if 'dedup' in (c[0][2] if len(c[0]) > 2 else '').lower()
        ]
        self.assertTrue(len(dedup_calls) >= 1, 'Expected at least one dedup warning log entry')
        severity = dedup_calls[0][0][1]
        self.assertEqual(severity, 'warning')
        ctx = dedup_calls[0][0][3]
        self.assertIn('url', ctx)
        self.assertIn('https://reddit.com/r/forhire/dedup-fail', ctx['url'])

    @patch.dict('os.environ', {
        'NOTION_TOKEN': 'ntn_test',
        'NOTION_REDDIT_LEADS_DB_ID': 'db-test',
    })
    @patch('scripts.reddit_leads.save_failed_sentinel_to_notion')
    @patch('scripts.reddit_leads.save_lead_to_notion')
    @patch('scripts.reddit_leads.fetch_reddit_posts')
    def test_dedup_http_error_logs_notion_response_body(
        self, mock_fetch, mock_save, mock_sentinel,
    ):
        """When dedup-check raises an HTTPError, the Notion response body must be logged.

        This mirrors the pattern in save_lead_to_notion and is needed to diagnose
        the recurring HTTP 404s observed for url_exists_in_notion in
        logs/errors.jsonl: a 404 alone could be a deleted DB, a revoked
        integration, or a transient Notion outage; only the response body shows
        which.
        """
        import io
        import error_log as el
        body = (
            b'{"object":"error","status":404,"code":"object_not_found",'
            b'"message":"Could not find database with ID: db-test."}'
        )
        http_err = urllib.error.HTTPError(
            'https://api.notion.com/v1/databases/db-test/query',
            404, 'Not Found', {}, io.BytesIO(body),
        )
        mock_fetch.return_value = [{
            'title': '[HIRING] Need a Framer developer',
            'url': 'https://reddit.com/r/forhire/dedup-http-404',
            'subreddit': 'forhire',
            'content': 'Budget $500 for landing page website',
            'post_date': '2024-03-01T10:00:00+00:00',
        }]
        with patch('scripts.reddit_leads.url_exists_in_notion', side_effect=http_err), \
             patch.object(el, 'log_error') as mock_log:
            from scripts.reddit_leads import main
            main()
        dedup_calls = [
            c for c in mock_log.call_args_list
            if 'dedup' in (c[0][2] if len(c[0]) > 2 else '').lower()
        ]
        self.assertTrue(len(dedup_calls) >= 1, 'Expected at least one dedup warning log entry')
        # Severity is still 'warning' (HTTPError is a transient/skip case here)
        self.assertEqual(dedup_calls[0][0][1], 'warning')
        ctx = dedup_calls[0][0][3]
        self.assertEqual(ctx.get('status'), 404)
        self.assertIn('notion_response', ctx)
        self.assertIn('object_not_found', ctx['notion_response'])
        # Response body is truncated to 500 chars
        self.assertLessEqual(len(ctx['notion_response']), 500)
        # save_lead_to_notion must not be called when dedup fails
        mock_save.assert_not_called()
        mock_sentinel.assert_not_called()

    @patch.dict('os.environ', {
        'NOTION_TOKEN': 'ntn_test',
        'NOTION_REDDIT_LEADS_DB_ID': 'db-test',
    })
    @patch('scripts.reddit_leads.save_failed_sentinel_to_notion')
    @patch('scripts.reddit_leads.save_lead_to_notion')
    @patch('scripts.reddit_leads.fetch_reddit_posts')
    def test_dedup_non_http_error_still_logs_warning_without_status(
        self, mock_fetch, mock_save, mock_sentinel,
    ):
        """A non-HTTP exception during dedup must still log a warning, but
        without a 'status' or 'notion_response' field (they only apply to
        HTTPError)."""
        import error_log as el
        mock_fetch.return_value = [{
            'title': '[HIRING] Need a Framer developer',
            'url': 'https://reddit.com/r/forhire/dedup-timeout',
            'subreddit': 'forhire',
            'content': 'Budget $500 for landing page website',
            'post_date': '2024-03-01T10:00:00+00:00',
        }]
        with patch('scripts.reddit_leads.url_exists_in_notion',
                   side_effect=TimeoutError('The read operation timed out')), \
             patch.object(el, 'log_error') as mock_log:
            from scripts.reddit_leads import main
            main()
        dedup_calls = [
            c for c in mock_log.call_args_list
            if 'dedup' in (c[0][2] if len(c[0]) > 2 else '').lower()
        ]
        self.assertTrue(len(dedup_calls) >= 1)
        ctx = dedup_calls[0][0][3]
        self.assertNotIn('status', ctx)
        self.assertNotIn('notion_response', ctx)
        self.assertIn('error', ctx)

    @patch.dict('os.environ', {
        'NOTION_TOKEN': 'ntn_test',
        'NOTION_REDDIT_LEADS_DB_ID': 'db-test',
        'DISCORD_ALERTS_WEBHOOK_URL': 'https://discord.com/alerts',
    })
    @patch('scripts.reddit_leads.save_failed_sentinel_to_notion')
    @patch('scripts.reddit_leads.save_lead_to_notion')
    @patch('scripts.reddit_leads._warn_discord')
    @patch('scripts.reddit_leads.fetch_reddit_posts')
    def test_dedup_object_not_found_triggers_alert(
        self, mock_fetch, mock_warn, mock_save, mock_sentinel,
    ):
        """A single ``object_not_found`` from Notion must fire an ERROR-level
        Discord alert — the misconfiguration would otherwise silently halt
        every lead save."""
        import io
        body = (
            b'{"object":"error","status":404,"code":"object_not_found",'
            b'"message":"Could not find database with ID: db-test."}'
        )
        http_err = urllib.error.HTTPError(
            'https://api.notion.com/v1/databases/db-test/query',
            404, 'Not Found', {}, io.BytesIO(body),
        )
        mock_fetch.return_value = [{
            'title': '[HIRING] Need a Framer developer',
            'url': 'https://reddit.com/r/forhire/abc',
            'subreddit': 'forhire',
            'content': 'Budget $500 for landing page website',
            'post_date': '2024-03-01T10:00:00+00:00',
        }]
        # Each call must raise a fresh HTTPError (body stream is consumed once).
        def fresh_http_err(*_a, **_kw):
            raise urllib.error.HTTPError(
                'https://api.notion.com/v1/databases/db-test/query',
                404, 'Not Found', {}, io.BytesIO(body),
            )
        with patch('scripts.reddit_leads.url_exists_in_notion',
                   side_effect=fresh_http_err):
            from scripts.reddit_leads import main
            main()
        # The save path must never be reached when dedup fails.
        mock_save.assert_not_called()
        mock_sentinel.assert_not_called()
        # An alert must have been sent and the message must mention object_not_found
        # so an operator immediately sees the cause.
        mock_warn.assert_called_once()
        msg = mock_warn.call_args[0][0]
        self.assertIn('object_not_found', msg)
        self.assertIn('NOTION_REDDIT_LEADS_DB_ID', msg)

    @patch.dict('os.environ', {
        'NOTION_TOKEN': 'ntn_test',
        'NOTION_REDDIT_LEADS_DB_ID': 'db-test',
        'DISCORD_ALERTS_WEBHOOK_URL': 'https://discord.com/alerts',
    })
    @patch('scripts.reddit_leads.save_failed_sentinel_to_notion')
    @patch('scripts.reddit_leads.save_lead_to_notion')
    @patch('scripts.reddit_leads._warn_discord')
    @patch('scripts.reddit_leads.fetch_reddit_posts')
    def test_dedup_other_failures_below_threshold_no_alert(
        self, mock_fetch, mock_warn, mock_save, mock_sentinel,
    ):
        """Non-``object_not_found`` dedup failures below the burst threshold
        must not fire an alert (one transient timeout is normal noise)."""
        # One filtered post per subreddit means one failure per subreddit; the
        # majority-fetch warning would interfere, so let only the *first* feed
        # return a post and the rest return [].
        from scripts.reddit_leads import REDDIT_FEEDS
        single_post = [{
            'title': '[HIRING] Need a Framer developer',
            'url': 'https://reddit.com/r/forhire/abc',
            'subreddit': list(REDDIT_FEEDS.keys())[0],
            'content': 'Budget $500 for landing page website',
            'post_date': '2024-03-01T10:00:00+00:00',
        }]
        mock_fetch.side_effect = [single_post] + [[]] * (len(REDDIT_FEEDS) - 1)
        with patch('scripts.reddit_leads.url_exists_in_notion',
                   side_effect=TimeoutError('The read operation timed out')):
            from scripts.reddit_leads import main
            main()
        mock_warn.assert_not_called()

    @patch.dict('os.environ', {
        'NOTION_TOKEN': 'ntn_test',
        'NOTION_REDDIT_LEADS_DB_ID': 'db-test',
        'DISCORD_ALERTS_WEBHOOK_URL': 'https://discord.com/alerts',
    })
    @patch('scripts.reddit_leads.save_failed_sentinel_to_notion')
    @patch('scripts.reddit_leads.save_lead_to_notion')
    @patch('scripts.reddit_leads._warn_discord')
    @patch('scripts.reddit_leads.fetch_reddit_posts')
    def test_dedup_other_failures_at_threshold_warns(
        self, mock_fetch, mock_warn, mock_save, mock_sentinel,
    ):
        """A burst of >=5 non-``object_not_found`` dedup failures must fire a
        WARNING-level alert, but not the object_not_found-specific ERROR text."""
        from scripts.reddit_leads import REDDIT_FEEDS, _DEDUP_OTHER_FAILURE_ALERT_THRESHOLD
        # Make at least the threshold-many subreddits each yield one filtered
        # post; the rest return [].  Threshold is 5.
        n_failing = max(_DEDUP_OTHER_FAILURE_ALERT_THRESHOLD, 5)
        feed_iter = iter(REDDIT_FEEDS.keys())
        failing_feeds = []
        for _ in range(n_failing):
            failing_feeds.append([{
                'title': '[HIRING] Need a Framer developer',
                'url': f'https://reddit.com/{next(feed_iter)}/abc',
                'subreddit': 'forhire',
                'content': 'Budget $500 for landing page website',
                'post_date': '2024-03-01T10:00:00+00:00',
            }])
        mock_fetch.side_effect = failing_feeds + [[]] * (len(REDDIT_FEEDS) - n_failing)
        with patch('scripts.reddit_leads.url_exists_in_notion',
                   side_effect=TimeoutError('The read operation timed out')):
            from scripts.reddit_leads import main
            main()
        mock_warn.assert_called_once()
        msg = mock_warn.call_args[0][0]
        self.assertIn('dedup-check failure', msg)
        # Must not be the object_not_found-specific message.
        self.assertNotIn('object_not_found', msg)

    @patch.dict('os.environ', {
        'NOTION_TOKEN': 'ntn_test',
        'NOTION_REDDIT_LEADS_DB_ID': 'db-test',
        'DISCORD_ALERTS_WEBHOOK_URL': 'https://discord.com/alerts',
    })
    @patch('scripts.reddit_leads.save_failed_sentinel_to_notion')
    @patch('scripts.reddit_leads.save_lead_to_notion')
    @patch('scripts.reddit_leads._warn_discord')
    @patch('scripts.reddit_leads.url_exists_in_notion', return_value=False)
    @patch('scripts.reddit_leads.fetch_reddit_posts', return_value=[])
    def test_no_dedup_alert_when_no_dedup_errors(
        self, mock_fetch, mock_exists, mock_warn, mock_save, mock_sentinel,
    ):
        """Healthy run (zero dedup failures) must not emit any dedup alert."""
        from scripts.reddit_leads import main
        main()
        mock_warn.assert_not_called()

    @patch.dict('os.environ', {
        'NOTION_TOKEN': 'ntn_test',
        'NOTION_REDDIT_LEADS_DB_ID': 'db-test',
        'DISCORD_ALERTS_WEBHOOK_URL': 'https://discord.com/alerts',
    })
    @patch('scripts.reddit_leads.save_failed_sentinel_to_notion')
    @patch('scripts.reddit_leads.save_lead_to_notion')
    @patch('scripts.reddit_leads._warn_discord')
    @patch('scripts.reddit_leads.fetch_reddit_posts')
    def test_dedup_object_not_found_short_circuits_remaining_feeds(
        self, mock_fetch, mock_warn, mock_save, mock_sentinel,
    ):
        """After the first ``object_not_found`` dedup failure, the outer loop
        must break: every subsequent dedup attempt would fail with the same
        404 and consume the rest of the 15-minute cron window on doomed Notion
        + Reddit RSS calls.  This test confirms that:
          1. only the first subreddit's RSS feed is fetched,
          2. ``url_exists_in_notion`` is called exactly once, and
          3. the single Discord alert at the end of the run still fires.
        """
        import io
        from scripts.reddit_leads import REDDIT_FEEDS
        body = (
            b'{"object":"error","status":404,"code":"object_not_found",'
            b'"message":"Could not find database with ID: db-test."}'
        )
        # Every feed returns the same single hiring post — but only the first
        # one should ever be inspected because the dedup failure on its post
        # triggers the early break.
        filtered_post = [{
            'title': '[HIRING] Need a Framer developer',
            'url': 'https://reddit.com/r/forhire/abc',
            'subreddit': list(REDDIT_FEEDS.keys())[0],
            'content': 'Budget $500 for landing page website',
            'post_date': '2024-03-01T10:00:00+00:00',
        }]
        mock_fetch.return_value = filtered_post

        def fresh_http_err(*_a, **_kw):
            raise urllib.error.HTTPError(
                'https://api.notion.com/v1/databases/db-test/query',
                404, 'Not Found', {}, io.BytesIO(body),
            )
        with patch('scripts.reddit_leads.url_exists_in_notion',
                   side_effect=fresh_http_err) as mock_exists:
            from scripts.reddit_leads import main
            main()
        # Only one subreddit feed should have been fetched — the outer loop
        # must break after the first object_not_found, not iterate through
        # all 43 feeds.
        self.assertEqual(mock_fetch.call_count, 1)
        # Exactly one dedup attempt (against the first post) must have been
        # made — any further calls would be wasted Notion traffic.
        self.assertEqual(mock_exists.call_count, 1)
        # Save path must never be reached.
        mock_save.assert_not_called()
        mock_sentinel.assert_not_called()
        # The single ERROR-level alert must still fire.
        mock_warn.assert_called_once()
        msg = mock_warn.call_args[0][0]
        self.assertIn('object_not_found', msg)
        self.assertIn('NOTION_REDDIT_LEADS_DB_ID', msg)

    @patch.dict('os.environ', {
        'NOTION_TOKEN': 'ntn_test',
        'NOTION_REDDIT_LEADS_DB_ID': 'db-test',
        'DISCORD_ALERTS_WEBHOOK_URL': 'https://discord.com/alerts',
    })
    @patch('scripts.reddit_leads.save_failed_sentinel_to_notion')
    @patch('scripts.reddit_leads.save_lead_to_notion')
    @patch('scripts.reddit_leads._warn_discord')
    @patch('scripts.reddit_leads.fetch_reddit_posts')
    def test_dedup_transient_404_without_object_not_found_routes_to_general_short_circuit(
        self, mock_fetch, mock_warn, mock_save, mock_sentinel,
    ):
        """A 404 whose body does not contain ``object_not_found`` (e.g. a
        Cloudflare-style 404 HTML page, or a Notion transient outage with a
        stripped error body) must NOT fire the DB-misconfigured alert path,
        but it MUST trip the general consecutive-failure short-circuit after
        ``_CONSECUTIVE_DEDUP_FAILURE_SHORT_CIRCUIT`` failures.

        This is the generalisation of PR #92: in-flight Notion degradation
        of any kind (404, timeout, 5xx, URLError) should bail out the run
        once we have strong evidence Notion is down (no successes between
        N failures), not just the narrow ``object_not_found`` case.
        """
        import io
        from scripts.reddit_leads import (
            REDDIT_FEEDS, _CONSECUTIVE_DEDUP_FAILURE_SHORT_CIRCUIT,
        )
        # 404 body that does NOT mention object_not_found.
        body = b'<html><title>Not Found</title><body>Generic 404</body></html>'
        n = len(REDDIT_FEEDS)
        feed_iter = iter(REDDIT_FEEDS.keys())
        side = []
        for _ in range(n):
            side.append([{
                'title': '[HIRING] Need a Framer developer',
                'url': f'https://reddit.com/{next(feed_iter)}/abc',
                'subreddit': 'forhire',
                'content': 'Budget $500 for landing page website',
                'post_date': '2024-03-01T10:00:00+00:00',
            }])
        mock_fetch.side_effect = side

        def fresh_http_err(*_a, **_kw):
            raise urllib.error.HTTPError(
                'https://api.notion.com/v1/databases/db-test/query',
                404, 'Not Found', {}, io.BytesIO(body),
            )
        with patch('scripts.reddit_leads.url_exists_in_notion',
                   side_effect=fresh_http_err) as mock_exists:
            from scripts.reddit_leads import main
            main()
        # The run must short-circuit after the threshold of consecutive
        # failures, NOT after exhausting all 43 feeds.
        self.assertEqual(mock_fetch.call_count,
                         _CONSECUTIVE_DEDUP_FAILURE_SHORT_CIRCUIT)
        self.assertEqual(mock_exists.call_count,
                         _CONSECUTIVE_DEDUP_FAILURE_SHORT_CIRCUIT)
        # The single alert that fires must NOT be the object_not_found path
        # (since the body lacks the marker) — it must be the general
        # in-flight "Notion appears unreachable" alert from the new
        # consecutive-failure short-circuit.
        mock_warn.assert_called_once()
        msg = mock_warn.call_args[0][0]
        self.assertNotIn('object_not_found', msg)
        self.assertIn('Notion appears unreachable', msg)


# ---------------------------------------------------------------------------
# _write_summary
# ---------------------------------------------------------------------------

from unittest.mock import mock_open
import scripts.reddit_leads as rl


class TestWriteSummary(unittest.TestCase):

    def setUp(self):
        # Patch time.sleep so inter-feed delays don't slow down tests that call main().
        self._sleep_patcher = patch('scripts.reddit_leads.time.sleep')
        self._sleep_patcher.start()
        # Default-pass preflight so existing summary tests can reach the
        # per-feed loop (see TestMain.setUp for the same pattern).
        self._preflight_patcher = patch(
            'scripts.reddit_leads._notion_preflight',
            return_value=(True, '', None, ''),
        )
        self._preflight_patcher.start()
        self._suppress_patcher = patch(
            'scripts.reddit_leads._should_suppress_alert',
            return_value=False,
        )
        self._suppress_patcher.start()
        self._record_patcher = patch('scripts.reddit_leads._record_alert_sent')
        self._record_patcher.start()

    def tearDown(self):
        self._sleep_patcher.stop()
        self._preflight_patcher.stop()
        self._suppress_patcher.stop()
        self._record_patcher.stop()
        os.environ.pop('GITHUB_STEP_SUMMARY', None)

    def test_writes_to_file_when_env_set(self):
        with patch.dict('os.environ', {'GITHUB_STEP_SUMMARY': '/tmp/summary.md'}), \
             patch('builtins.open', mock_open()) as m:
            rl._write_summary('## Reddit Leads Monitor\nhello')
        m.assert_called_once_with('/tmp/summary.md', 'a')
        m().write.assert_called_once_with('## Reddit Leads Monitor\nhello\n')

    def test_no_op_when_env_not_set(self):
        os.environ.pop('GITHUB_STEP_SUMMARY', None)
        with patch('builtins.open') as m:
            rl._write_summary('ignored')
        m.assert_not_called()

    @patch.dict('os.environ', {'NOTION_TOKEN': 'ntn_test', 'NOTION_REDDIT_LEADS_DB_ID': 'db-test'})
    @patch('scripts.reddit_leads._write_summary')
    @patch('scripts.reddit_leads.fetch_reddit_posts', return_value=[])
    def test_main_writes_summary_when_no_leads(self, mock_fetch, mock_summary):
        from scripts.reddit_leads import main
        main()
        mock_summary.assert_called_once()
        summary_text = mock_summary.call_args[0][0]
        self.assertIn('0 new lead', summary_text)

    @patch.dict('os.environ', {'NOTION_TOKEN': 'ntn_test', 'NOTION_REDDIT_LEADS_DB_ID': 'db-test'})
    @patch('scripts.reddit_leads._write_summary')
    @patch('scripts.reddit_leads.save_lead_to_notion')
    @patch('scripts.reddit_leads.url_exists_in_notion', return_value=False)
    @patch('scripts.reddit_leads.fetch_reddit_posts')
    def test_main_writes_summary_with_saved_leads(self, mock_fetch, mock_exists, mock_save, mock_summary):
        mock_fetch.return_value = [{
            'title': 'Need Framer designer for landing page hire budget $500',
            'url': 'https://reddit.com/r/forhire/1',
            'subreddit': 'forhire',
            'content': 'Need website landing page designer hire budget $500',
            'post_date': '2024-03-01T10:00:00+00:00',
        }]
        from scripts.reddit_leads import main
        main()
        mock_summary.assert_called_once()
        summary_text = mock_summary.call_args[0][0]
        self.assertIn('new lead(s) saved', summary_text)
        self.assertNotIn('0 new lead', summary_text)

    @patch.dict('os.environ', {
        'NOTION_TOKEN': 'ntn_test',
        'NOTION_REDDIT_LEADS_DB_ID': 'db-test',
        'DISCORD_ALERTS_WEBHOOK_URL': 'https://discord.com/alerts',
    })
    @patch('scripts.reddit_leads._write_summary')
    @patch('scripts.reddit_leads._warn_discord')
    @patch('scripts.reddit_leads.fetch_reddit_posts', return_value=None)
    def test_main_summary_includes_unreachable_count(self, mock_fetch, mock_warn, mock_summary):
        from scripts.reddit_leads import main, REDDIT_FEEDS
        main()
        mock_summary.assert_called_once()
        summary_text = mock_summary.call_args[0][0]
        self.assertIn(f'{len(REDDIT_FEEDS)}/{len(REDDIT_FEEDS)}', summary_text)
        self.assertIn('unreachable', summary_text)


# ---------------------------------------------------------------------------
# TestWarnDiscord
# ---------------------------------------------------------------------------

class TestWarnDiscord(unittest.TestCase):

    def setUp(self):
        os.environ['DISCORD_ALERTS_WEBHOOK_URL'] = 'https://discord.com/api/webhooks/test-alerts'

    def tearDown(self):
        os.environ.pop('DISCORD_ALERTS_WEBHOOK_URL', None)

    def test_posts_content_message_to_alerts_webhook(self):
        with patch('scripts.reddit_leads.http_post', return_value={}) as mock_post:
            rl._warn_discord('test warning message')
        mock_post.assert_called_once()
        url, payload = mock_post.call_args[0]
        self.assertIn('test-alerts', url)
        self.assertIn('content', payload)
        self.assertIn('test warning message', payload['content'])

    def test_exception_is_caught_and_does_not_propagate(self):
        with patch('scripts.reddit_leads.http_post', side_effect=Exception('network error')):
            rl._warn_discord('msg')  # must not raise

    def test_no_op_when_env_var_missing(self):
        """_warn_discord must not raise when DISCORD_ALERTS_WEBHOOK_URL is unset."""
        os.environ.pop('DISCORD_ALERTS_WEBHOOK_URL', None)
        with patch('scripts.reddit_leads.http_post') as mock_post:
            rl._warn_discord('msg')  # must not raise
        mock_post.assert_not_called()

    def test_http_error_logs_discord_response_body(self):
        """An HTTPError on the alerts webhook (e.g. revoked URL, deleted
        channel, rate-limit) must log the Discord response body so the
        misconfiguration can be diagnosed from logs alone -- otherwise the
        log entry just says ``"HTTP Error <code>: <reason>"``."""
        import io
        import error_log as el
        body = b'{"message": "Unknown Webhook", "code": 10015}'
        http_err = urllib.error.HTTPError(
            'https://discord.com/api/webhooks/test-alerts',
            404, 'Not Found', {}, io.BytesIO(body),
        )
        with patch('scripts.reddit_leads.http_post', side_effect=http_err), \
             patch.object(el, 'log_error') as mock_log:
            rl._warn_discord('msg')  # must not raise
        self.assertTrue(mock_log.called)
        ctx = mock_log.call_args[0][3]
        self.assertEqual(ctx.get('status'), 404)
        self.assertIn('discord_response', ctx)
        self.assertIn('Unknown Webhook', ctx['discord_response'])
        self.assertIn('error', ctx)

    def test_http_error_truncates_long_discord_response_body(self):
        """The captured Discord response body must be capped at 500 chars."""
        import io
        import error_log as el
        body = b'B' * 1500
        # 400 is non-retriable so the side_effect runs only once.
        http_err = urllib.error.HTTPError(
            'https://discord.com/api/webhooks/test-alerts',
            400, 'Bad Request', {}, io.BytesIO(body),
        )
        with patch('scripts.reddit_leads.http_post', side_effect=http_err), \
             patch.object(el, 'log_error') as mock_log:
            rl._warn_discord('msg')
        ctx = mock_log.call_args[0][3]
        self.assertLessEqual(len(ctx['discord_response']), 500)


# ---------------------------------------------------------------------------
# TestRateLimiting — inter-feed delay and User-Agent
# ---------------------------------------------------------------------------

class TestRateLimiting(unittest.TestCase):
    """Tests for the inter-feed delay and Reddit-specific User-Agent."""

    def setUp(self):
        # Default-pass preflight so the main() loop is exercised in the
        # tests below (see TestMain.setUp).
        self._preflight_patcher = patch(
            'scripts.reddit_leads._notion_preflight',
            return_value=(True, '', None, ''),
        )
        self._preflight_patcher.start()

    def tearDown(self):
        self._preflight_patcher.stop()

    def test_inter_feed_delay_constant_positive(self):
        """_INTER_FEED_DELAY must be a positive number."""
        self.assertGreater(rl._INTER_FEED_DELAY, 0)

    def test_reddit_user_agent_not_generic(self):
        """_REDDIT_USER_AGENT must not be the generic 'automation-bot/1.0' string
        that Reddit commonly blocks."""
        self.assertNotEqual(rl._REDDIT_USER_AGENT, 'automation-bot/1.0')
        self.assertTrue(len(rl._REDDIT_USER_AGENT) > 10)

    @patch.dict('os.environ', {
        'NOTION_TOKEN': 'ntn_test',
        'NOTION_REDDIT_LEADS_DB_ID': 'db-test',
    })
    @patch('scripts.reddit_leads.time.sleep')
    @patch('scripts.reddit_leads.fetch_reddit_posts', return_value=[])
    def test_main_sleeps_between_feeds(self, mock_fetch, mock_sleep):
        """main() must call time.sleep between feed fetches (not before the first)."""
        from scripts.reddit_leads import main, REDDIT_FEEDS
        main()
        # sleep should be called once fewer than the number of feeds
        expected_sleep_calls = len(REDDIT_FEEDS) - 1
        self.assertEqual(mock_sleep.call_count, expected_sleep_calls)

    @patch.dict('os.environ', {
        'NOTION_TOKEN': 'ntn_test',
        'NOTION_REDDIT_LEADS_DB_ID': 'db-test',
    })
    @patch('scripts.reddit_leads.time.sleep')
    @patch('scripts.reddit_leads.fetch_reddit_posts', return_value=[])
    def test_main_sleep_uses_inter_feed_delay(self, mock_fetch, mock_sleep):
        """Each sleep call must use the _INTER_FEED_DELAY constant."""
        from scripts.reddit_leads import main
        main()
        for call_args in mock_sleep.call_args_list:
            self.assertEqual(call_args[0][0], rl._INTER_FEED_DELAY)


# ---------------------------------------------------------------------------
# Notion preflight (#2 of mitigations from 2026-05-21 incident)
# ---------------------------------------------------------------------------

class TestNotionPreflight(unittest.TestCase):
    """Direct tests for ``_notion_preflight``."""

    @patch('scripts.reddit_leads.http_post', return_value={'results': []})
    def test_preflight_ok_returns_true_tuple(self, mock_post):
        from scripts.reddit_leads import _notion_preflight
        ok, err, status, body = _notion_preflight('db-test')
        self.assertTrue(ok)
        self.assertEqual(err, '')
        self.assertIsNone(status)
        self.assertEqual(body, '')
        # The probe must hit the configured DB id, not a hard-coded one.
        called_url = mock_post.call_args[0][0]
        self.assertIn('db-test', called_url)

    def test_preflight_object_not_found_returns_marker(self):
        import io
        body = (
            b'{"object":"error","status":404,"code":"object_not_found",'
            b'"message":"Could not find database with ID: db-test."}'
        )

        def raise_404(*_a, **_kw):
            raise urllib.error.HTTPError(
                'https://api.notion.com/v1/databases/db-test/query',
                404, 'Not Found', {}, io.BytesIO(body),
            )
        with patch('scripts.reddit_leads.http_post', side_effect=raise_404):
            from scripts.reddit_leads import _notion_preflight
            ok, err, status, body_preview = _notion_preflight('db-test')
        self.assertFalse(ok)
        self.assertEqual(err, 'object_not_found')
        self.assertEqual(status, 404)
        self.assertIn('object_not_found', body_preview)

    def test_preflight_generic_http_error_returns_status_token(self):
        import io
        body = b'<html>500 Internal Server Error</html>'

        def raise_500(*_a, **_kw):
            raise urllib.error.HTTPError(
                'https://api.notion.com/v1/databases/db-test/query',
                500, 'Internal Server Error', {}, io.BytesIO(body),
            )
        with patch('scripts.reddit_leads.http_post', side_effect=raise_500):
            from scripts.reddit_leads import _notion_preflight
            ok, err, status, _ = _notion_preflight('db-test')
        self.assertFalse(ok)
        self.assertEqual(err, 'HTTP 500')
        self.assertEqual(status, 500)

    def test_preflight_timeout_returns_class_name(self):
        with patch('scripts.reddit_leads.http_post', side_effect=TimeoutError('read timed out')):
            from scripts.reddit_leads import _notion_preflight
            ok, err, status, _ = _notion_preflight('db-test')
        self.assertFalse(ok)
        self.assertEqual(err, 'TimeoutError')
        self.assertIsNone(status)

    def test_preflight_url_error_returns_class_name(self):
        with patch('scripts.reddit_leads.http_post',
                   side_effect=urllib.error.URLError('connection refused')):
            from scripts.reddit_leads import _notion_preflight
            ok, err, status, _ = _notion_preflight('db-test')
        self.assertFalse(ok)
        self.assertEqual(err, 'URLError')


class TestMainPreflightBehaviour(unittest.TestCase):
    """Integration tests: how main() reacts to preflight outcomes."""

    def setUp(self):
        self._sleep_patcher = patch('scripts.reddit_leads.time.sleep')
        self._sleep_patcher.start()
        # Disable suppression so the alert always fires.
        self._suppress_patcher = patch(
            'scripts.reddit_leads._should_suppress_alert', return_value=False)
        self._suppress_patcher.start()
        self._record_patcher = patch('scripts.reddit_leads._record_alert_sent')
        self._record_patcher.start()

    def tearDown(self):
        self._sleep_patcher.stop()
        self._suppress_patcher.stop()
        self._record_patcher.stop()

    @patch.dict('os.environ', {
        'NOTION_TOKEN': 'ntn_test',
        'NOTION_REDDIT_LEADS_DB_ID': 'db-test',
        'DISCORD_ALERTS_WEBHOOK_URL': 'https://discord.com/alerts',
    })
    @patch('scripts.reddit_leads._warn_discord')
    @patch('scripts.reddit_leads.fetch_reddit_posts')
    @patch('scripts.reddit_leads._notion_preflight',
           return_value=(False, 'object_not_found', 404,
                         '{"code":"object_not_found"}'))
    def test_preflight_object_not_found_exits_without_fetching(
        self, mock_preflight, mock_fetch, mock_warn,
    ):
        from scripts.reddit_leads import main
        main()
        # No subreddit feed must be touched when preflight fails.
        mock_fetch.assert_not_called()
        # Exactly one alert must fire, with the object_not_found wording.
        mock_warn.assert_called_once()
        msg = mock_warn.call_args[0][0]
        self.assertIn('object_not_found', msg)
        self.assertIn('NOTION_REDDIT_LEADS_DB_ID', msg)
        # Dedup key must be present so subsequent runs are suppressed.
        self.assertEqual(
            mock_warn.call_args.kwargs.get('dedup_key'),
            'reddit_leads:notion_preflight_object_not_found',
        )

    @patch.dict('os.environ', {
        'NOTION_TOKEN': 'ntn_test',
        'NOTION_REDDIT_LEADS_DB_ID': 'db-test',
        'DISCORD_ALERTS_WEBHOOK_URL': 'https://discord.com/alerts',
    })
    @patch('scripts.reddit_leads._warn_discord')
    @patch('scripts.reddit_leads.fetch_reddit_posts')
    @patch('scripts.reddit_leads._notion_preflight',
           return_value=(False, 'TimeoutError', None, ''))
    def test_preflight_timeout_exits_without_fetching(
        self, mock_preflight, mock_fetch, mock_warn,
    ):
        from scripts.reddit_leads import main
        main()
        mock_fetch.assert_not_called()
        mock_warn.assert_called_once()
        msg = mock_warn.call_args[0][0]
        self.assertIn('preflight failed', msg)
        self.assertIn('TimeoutError', msg)
        # Generic preflight failures route to the dedicated "other" key so
        # they don't collide with the object_not_found suppression window.
        self.assertEqual(
            mock_warn.call_args.kwargs.get('dedup_key'),
            'reddit_leads:notion_preflight_other',
        )

    @patch.dict('os.environ', {
        'NOTION_TOKEN': 'ntn_test',
        'NOTION_REDDIT_LEADS_DB_ID': 'db-test',
    })
    @patch('scripts.reddit_leads.fetch_reddit_posts', return_value=[])
    @patch('scripts.reddit_leads._notion_preflight',
           return_value=(True, '', None, ''))
    def test_preflight_ok_proceeds_to_feed_loop(self, mock_preflight, mock_fetch):
        from scripts.reddit_leads import main, REDDIT_FEEDS
        main()
        # All 43 feeds should be inspected when preflight passes.
        self.assertEqual(mock_fetch.call_count, len(REDDIT_FEEDS))


# ---------------------------------------------------------------------------
# Consecutive-dedup-failure short-circuit (#1 of mitigations)
# ---------------------------------------------------------------------------

class TestConsecutiveDedupFailureShortCircuit(unittest.TestCase):
    """When dedup keeps failing with no successes between, bail out early."""

    def setUp(self):
        self._sleep_patcher = patch('scripts.reddit_leads.time.sleep')
        self._sleep_patcher.start()
        self._preflight_patcher = patch(
            'scripts.reddit_leads._notion_preflight',
            return_value=(True, '', None, ''),
        )
        self._preflight_patcher.start()
        self._suppress_patcher = patch(
            'scripts.reddit_leads._should_suppress_alert', return_value=False)
        self._suppress_patcher.start()
        self._record_patcher = patch('scripts.reddit_leads._record_alert_sent')
        self._record_patcher.start()

    def tearDown(self):
        self._sleep_patcher.stop()
        self._preflight_patcher.stop()
        self._suppress_patcher.stop()
        self._record_patcher.stop()

    @patch.dict('os.environ', {
        'NOTION_TOKEN': 'ntn_test',
        'NOTION_REDDIT_LEADS_DB_ID': 'db-test',
        'DISCORD_ALERTS_WEBHOOK_URL': 'https://discord.com/alerts',
    })
    @patch('scripts.reddit_leads.save_failed_sentinel_to_notion')
    @patch('scripts.reddit_leads.save_lead_to_notion')
    @patch('scripts.reddit_leads._warn_discord')
    @patch('scripts.reddit_leads.fetch_reddit_posts')
    def test_three_consecutive_timeouts_short_circuit(
        self, mock_fetch, mock_warn, mock_save, mock_sentinel,
    ):
        from scripts.reddit_leads import (
            REDDIT_FEEDS, _CONSECUTIVE_DEDUP_FAILURE_SHORT_CIRCUIT,
        )
        n = len(REDDIT_FEEDS)
        feed_iter = iter(REDDIT_FEEDS.keys())
        side = []
        for _ in range(n):
            side.append([{
                'title': '[HIRING] Need a Framer developer',
                'url': f'https://reddit.com/{next(feed_iter)}/abc',
                'subreddit': 'forhire',
                'content': 'Budget $500 for landing page website',
                'post_date': '2024-03-01T10:00:00+00:00',
            }])
        mock_fetch.side_effect = side

        with patch('scripts.reddit_leads.url_exists_in_notion',
                   side_effect=TimeoutError('read timed out')) as mock_exists:
            from scripts.reddit_leads import main
            main()
        # Only the threshold number of fetches and dedup calls should happen.
        self.assertEqual(mock_fetch.call_count,
                         _CONSECUTIVE_DEDUP_FAILURE_SHORT_CIRCUIT)
        self.assertEqual(mock_exists.call_count,
                         _CONSECUTIVE_DEDUP_FAILURE_SHORT_CIRCUIT)
        # The short-circuit alert must fire (Notion appears unreachable).
        mock_warn.assert_called_once()
        msg = mock_warn.call_args[0][0]
        self.assertIn('Notion appears unreachable', msg)
        self.assertNotIn('object_not_found', msg)
        self.assertEqual(
            mock_warn.call_args.kwargs.get('dedup_key'),
            'reddit_leads:dedup_notion_likely_down',
        )

    @patch.dict('os.environ', {
        'NOTION_TOKEN': 'ntn_test',
        'NOTION_REDDIT_LEADS_DB_ID': 'db-test',
        'DISCORD_ALERTS_WEBHOOK_URL': 'https://discord.com/alerts',
    })
    @patch('scripts.reddit_leads.save_failed_sentinel_to_notion')
    @patch('scripts.reddit_leads.save_lead_to_notion')
    @patch('scripts.reddit_leads._warn_discord')
    @patch('scripts.reddit_leads.fetch_reddit_posts')
    def test_success_resets_consecutive_failure_counter(
        self, mock_fetch, mock_warn, mock_save, mock_sentinel,
    ):
        """A single dedup success between failures must reset the counter
        so a long run of intermittent (but recovering) failures does NOT
        trip the short-circuit."""
        from scripts.reddit_leads import REDDIT_FEEDS
        n = len(REDDIT_FEEDS)
        feed_iter = iter(REDDIT_FEEDS.keys())
        side = []
        for _ in range(n):
            side.append([{
                'title': '[HIRING] Need a Framer developer',
                'url': f'https://reddit.com/{next(feed_iter)}/abc',
                'subreddit': 'forhire',
                'content': 'Budget $500 for landing page website',
                'post_date': '2024-03-01T10:00:00+00:00',
            }])
        mock_fetch.side_effect = side

        # Alternate fail, fail, success, fail, fail, success, ...
        # Never reaches 3 consecutive failures so should NOT short-circuit.
        outcomes = []
        for i in range(n):
            if (i + 1) % 3 == 0:
                outcomes.append(False)  # success: URL doesn't exist
            else:
                outcomes.append(TimeoutError('read timed out'))

        def side_fn(*_a, **_kw):
            outcome = outcomes.pop(0)
            if isinstance(outcome, Exception):
                raise outcome
            return outcome

        with patch('scripts.reddit_leads.url_exists_in_notion',
                   side_effect=side_fn) as mock_exists:
            from scripts.reddit_leads import main
            main()
        # All feeds should be processed — never 3 in a row without success.
        self.assertEqual(mock_fetch.call_count, n)

    @patch.dict('os.environ', {
        'NOTION_TOKEN': 'ntn_test',
        'NOTION_REDDIT_LEADS_DB_ID': 'db-test',
        'DISCORD_ALERTS_WEBHOOK_URL': 'https://discord.com/alerts',
    })
    @patch('scripts.reddit_leads.save_failed_sentinel_to_notion')
    @patch('scripts.reddit_leads.save_lead_to_notion')
    @patch('scripts.reddit_leads._warn_discord')
    @patch('scripts.reddit_leads.fetch_reddit_posts')
    def test_consecutive_5xx_short_circuits(
        self, mock_fetch, mock_warn, mock_save, mock_sentinel,
    ):
        """The same short-circuit must fire on consecutive HTTP 5xx errors,
        not just timeouts."""
        import io
        from scripts.reddit_leads import (
            REDDIT_FEEDS, _CONSECUTIVE_DEDUP_FAILURE_SHORT_CIRCUIT,
        )
        n = len(REDDIT_FEEDS)
        feed_iter = iter(REDDIT_FEEDS.keys())
        side = []
        for _ in range(n):
            side.append([{
                'title': '[HIRING] Need a Framer developer',
                'url': f'https://reddit.com/{next(feed_iter)}/abc',
                'subreddit': 'forhire',
                'content': 'Budget $500 for landing page website',
                'post_date': '2024-03-01T10:00:00+00:00',
            }])
        mock_fetch.side_effect = side

        body = b'{"object":"error","status":503,"message":"Service unavailable"}'

        def raise_503(*_a, **_kw):
            raise urllib.error.HTTPError(
                'https://api.notion.com/v1/databases/db-test/query',
                503, 'Service Unavailable', {}, io.BytesIO(body),
            )
        with patch('scripts.reddit_leads.url_exists_in_notion',
                   side_effect=raise_503) as mock_exists:
            from scripts.reddit_leads import main
            main()
        self.assertEqual(mock_fetch.call_count,
                         _CONSECUTIVE_DEDUP_FAILURE_SHORT_CIRCUIT)
        self.assertEqual(mock_exists.call_count,
                         _CONSECUTIVE_DEDUP_FAILURE_SHORT_CIRCUIT)
        mock_warn.assert_called_once()
        self.assertIn('Notion appears unreachable', mock_warn.call_args[0][0])


# ---------------------------------------------------------------------------
# Alert suppression / state persistence (#3 of mitigations)
# ---------------------------------------------------------------------------

class TestAlertSuppression(unittest.TestCase):
    """Tests for the cross-run alert dedup helpers."""

    def setUp(self):
        import tempfile
        self._tmp = tempfile.TemporaryDirectory()
        self.state_path = os.path.join(self._tmp.name, 'alert_state.json')

    def tearDown(self):
        self._tmp.cleanup()

    def test_no_state_file_means_not_suppressed(self):
        from scripts.reddit_leads import _should_suppress_alert
        self.assertFalse(_should_suppress_alert('anything', state_path=self.state_path))

    def test_record_then_should_suppress(self):
        from scripts.reddit_leads import _record_alert_sent, _should_suppress_alert
        _record_alert_sent('reddit_leads:test_key', state_path=self.state_path)
        self.assertTrue(_should_suppress_alert(
            'reddit_leads:test_key', state_path=self.state_path))

    def test_different_keys_do_not_collide(self):
        from scripts.reddit_leads import _record_alert_sent, _should_suppress_alert
        _record_alert_sent('reddit_leads:a', state_path=self.state_path)
        self.assertTrue(_should_suppress_alert('reddit_leads:a', state_path=self.state_path))
        self.assertFalse(_should_suppress_alert('reddit_leads:b', state_path=self.state_path))

    def test_outside_window_not_suppressed(self):
        from scripts.reddit_leads import _record_alert_sent, _should_suppress_alert
        from datetime import datetime, timedelta, timezone
        past = datetime.now(timezone.utc) - timedelta(minutes=120)
        _record_alert_sent('reddit_leads:test', state_path=self.state_path, now=past)
        self.assertFalse(_should_suppress_alert(
            'reddit_leads:test', state_path=self.state_path,
            suppress_minutes=60,
        ))

    def test_inside_window_suppressed(self):
        from scripts.reddit_leads import _record_alert_sent, _should_suppress_alert
        from datetime import datetime, timedelta, timezone
        recent = datetime.now(timezone.utc) - timedelta(minutes=10)
        _record_alert_sent('reddit_leads:test', state_path=self.state_path, now=recent)
        self.assertTrue(_should_suppress_alert(
            'reddit_leads:test', state_path=self.state_path,
            suppress_minutes=60,
        ))

    def test_corrupt_state_file_treated_as_empty(self):
        with open(self.state_path, 'w') as f:
            f.write('{not valid json')
        from scripts.reddit_leads import _should_suppress_alert
        self.assertFalse(_should_suppress_alert(
            'anything', state_path=self.state_path))

    def test_malformed_timestamp_treated_as_not_recorded(self):
        with open(self.state_path, 'w') as f:
            f.write('{"reddit_leads:test": "not-a-timestamp"}')
        from scripts.reddit_leads import _should_suppress_alert
        self.assertFalse(_should_suppress_alert(
            'reddit_leads:test', state_path=self.state_path))

    def test_state_path_constant_is_per_script(self):
        """State path must be reddit-specific to avoid cross-script push races."""
        from scripts.reddit_leads import _ALERT_STATE_PATH
        self.assertIn('reddit_leads', _ALERT_STATE_PATH)
        self.assertTrue(_ALERT_STATE_PATH.startswith('state/'))


class TestWarnDiscordSuppression(unittest.TestCase):
    """_warn_discord must honour the dedup_key suppression contract."""

    def setUp(self):
        import tempfile
        self._tmp = tempfile.TemporaryDirectory()
        self.state_path = os.path.join(self._tmp.name, 'alert_state.json')
        self._state_patcher = patch(
            'scripts.reddit_leads._ALERT_STATE_PATH', self.state_path)
        self._state_patcher.start()
        os.environ['DISCORD_ALERTS_WEBHOOK_URL'] = 'https://discord.com/api/webhooks/test'

    def tearDown(self):
        self._state_patcher.stop()
        os.environ.pop('DISCORD_ALERTS_WEBHOOK_URL', None)
        self._tmp.cleanup()

    @patch('scripts.reddit_leads.http_post')
    def test_first_call_with_dedup_key_sends_and_records(self, mock_post):
        from scripts.reddit_leads import _warn_discord, _should_suppress_alert
        _warn_discord('hello', dedup_key='reddit_leads:k')
        mock_post.assert_called_once()
        # The next call would be suppressed.
        self.assertTrue(_should_suppress_alert(
            'reddit_leads:k', state_path=self.state_path))

    @patch('scripts.reddit_leads.http_post')
    def test_second_call_within_window_is_suppressed(self, mock_post):
        from scripts.reddit_leads import _warn_discord
        _warn_discord('first', dedup_key='reddit_leads:k')
        _warn_discord('second', dedup_key='reddit_leads:k')
        # Only the first call should have hit Discord.
        self.assertEqual(mock_post.call_count, 1)

    @patch('scripts.reddit_leads.http_post')
    def test_no_dedup_key_never_suppresses(self, mock_post):
        from scripts.reddit_leads import _warn_discord
        _warn_discord('first')
        _warn_discord('second')
        self.assertEqual(mock_post.call_count, 2)

    @patch('scripts.reddit_leads.http_post')
    def test_failed_send_does_not_record(self, mock_post):
        """A transient Discord 5xx must not record a 'sent' timestamp;
        otherwise the next alert would be silently suppressed even though
        the first never reached the channel."""
        import io
        mock_post.side_effect = urllib.error.HTTPError(
            'https://discord.com/api/webhooks/test', 503, 'Service Unavailable',
            {}, io.BytesIO(b''),
        )
        from scripts.reddit_leads import _warn_discord, _should_suppress_alert
        _warn_discord('boom', dedup_key='reddit_leads:k')
        self.assertFalse(_should_suppress_alert(
            'reddit_leads:k', state_path=self.state_path))


if __name__ == '__main__':
    unittest.main()
