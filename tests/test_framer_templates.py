"""
Tests for scripts/framer_templates.py

Run with:
    python3 -m unittest discover -s tests -p "test_*.py" -v
"""
import json
import os
import sys
import unittest
import urllib.error
from unittest.mock import MagicMock, mock_open, patch, ANY

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'scripts'))
import framer_templates as ft

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
# _extract_json_object
# ---------------------------------------------------------------------------

class TestExtractJsonObject(unittest.TestCase):

    def test_simple_flat_object(self):
        self.assertEqual(ft._extract_json_object('{"a": 1}', 0), {'a': 1})

    def test_nested_object(self):
        self.assertEqual(ft._extract_json_object('{"a": {"b": 2}}', 0), {'a': {'b': 2}})

    def test_string_containing_braces(self):
        self.assertEqual(ft._extract_json_object('{"k": "v{a}l"}', 0), {'k': 'v{a}l'})

    def test_escaped_quote_inside_string(self):
        raw = r'{"k": "say \"hi\""}'
        self.assertEqual(ft._extract_json_object(raw, 0), {'k': 'say "hi"'})

    def test_non_zero_start_position(self):
        s = 'abc{"x": 42}xyz'
        self.assertEqual(ft._extract_json_object(s, 3), {'x': 42})

    def test_stops_at_first_balanced_closing_brace(self):
        s = '{"a": 1}{"b": 2}'
        self.assertEqual(ft._extract_json_object(s, 0), {'a': 1})

    def test_unclosed_object_raises_value_error(self):
        with self.assertRaises(ValueError):
            ft._extract_json_object('{"a": 1', 0)


# ---------------------------------------------------------------------------
# load_dotenv
# ---------------------------------------------------------------------------

class TestLoadDotenv(unittest.TestCase):

    _KEYS = ('_FTTEST_A', '_FTTEST_B')

    def setUp(self):
        for k in self._KEYS:
            os.environ.pop(k, None)

    def tearDown(self):
        for k in self._KEYS:
            os.environ.pop(k, None)

    def _mock_env_file(self, content):
        return patch('builtins.open', mock_open(read_data=content))

    def test_loads_key_value_pair(self):
        with self._mock_env_file('_FTTEST_A=hello\n'):
            ft.load_dotenv()
        self.assertEqual(os.environ.get('_FTTEST_A'), 'hello')

    def test_skips_comment_lines(self):
        with self._mock_env_file('# comment\n_FTTEST_A=hi\n'):
            ft.load_dotenv()
        self.assertEqual(os.environ.get('_FTTEST_A'), 'hi')

    def test_skips_blank_lines(self):
        with self._mock_env_file('\n\n_FTTEST_A=val\n'):
            ft.load_dotenv()
        self.assertEqual(os.environ.get('_FTTEST_A'), 'val')

    def test_does_not_overwrite_existing_var(self):
        os.environ['_FTTEST_A'] = 'original'
        with self._mock_env_file('_FTTEST_A=new\n'):
            ft.load_dotenv()
        self.assertEqual(os.environ['_FTTEST_A'], 'original')

    def test_missing_env_file_is_silent(self):
        with patch('builtins.open', side_effect=FileNotFoundError):
            ft.load_dotenv()  # must not raise

    def test_value_containing_equals_sign(self):
        # partition('=') means only the first '=' is the delimiter
        with self._mock_env_file('_FTTEST_A=val=ue\n'):
            ft.load_dotenv()
        self.assertEqual(os.environ.get('_FTTEST_A'), 'val=ue')


# ---------------------------------------------------------------------------
# fetch_from_rsc
# ---------------------------------------------------------------------------

def _rsc_item(slug, id_='abc', title='T', price='Free', author='A',
              author_slug='a-studio',
              thumbnail='https://cdn.example.com/t.jpg', published='$D2024-01-15',
              meta_title='A Great Template', demo_url='https://demo.framer.website/',
              remixes=5):
    return (
        f'"item":{{"id":"{id_}","slug":"{slug}","title":"{title}",'
        f'"metaTitle":"{meta_title}",'
        f'"price":"{price}","creator":{{"name":"{author}","slug":"{author_slug}"}},'
        f'"thumbnail":"{thumbnail}","publishedAt":"{published}",'
        f'"publishedUrl":"{demo_url}","remixes":{remixes}}}'
    )


def _rsc_item_key(slug, key='"item":', id_='abc', title='T', price='Free',
                  author='A', author_slug='a-studio',
                  thumbnail='https://cdn.example.com/t.jpg', published='$D2024-01-15',
                  meta_title='A Great Template', demo_url='https://demo.framer.website/',
                  remixes=5):
    """Like _rsc_item but allows specifying a custom RSC key prefix."""
    body = _rsc_item(slug, id_=id_, title=title, price=price, author=author,
                     author_slug=author_slug, thumbnail=thumbnail, published=published,
                     meta_title=meta_title, demo_url=demo_url, remixes=remixes)
    # Replace the '"item":' prefix with the desired key
    return body.replace('"item":', key, 1)


def _full_page(offset=0):
    """Return an RSC body string containing exactly 20 templates."""
    return '\n'.join(_rsc_item(f'slug-{offset + i}', id_=str(offset + i)) for i in range(20))


class TestFetchFromRsc(unittest.TestCase):

    def _fetch(self, body):
        with patch('framer_templates.http_get', return_value=body):
            return ft.fetch_from_rsc()

    def test_parses_multiple_templates(self):
        body = _rsc_item('slug-a', id_='1') + '\n' + _rsc_item('slug-b', id_='2')
        templates = self._fetch(body)
        self.assertEqual(len(templates), 2)
        slugs = {t['slug'] for t in templates}
        self.assertEqual(slugs, {'slug-a', 'slug-b'})

    def test_extracts_all_fields_correctly(self):
        body = _rsc_item('cool-template', title='Cool Template', author='John Doe', author_slug='john-doe',
                         meta_title='Portfolio Website', demo_url='https://cool.framer.website/', remixes=7)
        templates = self._fetch(body)
        t = templates[0]
        self.assertEqual(t['title'], 'Cool Template')
        self.assertEqual(t['author'], 'John Doe')
        self.assertEqual(t['author_slug'], 'john-doe')
        self.assertEqual(t['price'], 'Free')  # no $$ prefix → unchanged
        self.assertEqual(t['url'], 'https://www.framer.com/marketplace/templates/cool-template/')
        self.assertEqual(t['thumbnail'], 'https://cdn.example.com/t.jpg')
        self.assertEqual(t['published_at'], '2024-01-15')  # $D prefix stripped
        self.assertEqual(t['meta_title'], 'Portfolio Website')
        self.assertEqual(t['demo_url'], 'https://cool.framer.website/')
        self.assertEqual(t['remixes'], 7)

    def test_strips_one_dollar_from_rsc_encoded_price(self):
        # RSC encodes literal "$" as "$$"; stripping the first "$" yields the actual price
        body = _rsc_item('s', price='$$29')
        t = self._fetch(body)[0]
        self.assertEqual(t['price'], '$29')

    def test_price_without_prefix_is_unchanged(self):
        body = _rsc_item('s', price='Free')
        t = self._fetch(body)[0]
        self.assertEqual(t['price'], 'Free')

    def test_strips_dollar_d_published_prefix(self):
        body = _rsc_item('s', published='$D2024-06-01')
        t = self._fetch(body)[0]
        self.assertEqual(t['published_at'], '2024-06-01')

    def test_deduplicates_by_slug(self):
        body = _rsc_item('dup', id_='1') + '\n' + _rsc_item('dup', id_='2')
        templates = self._fetch(body)
        self.assertEqual(len(templates), 1)

    def test_skips_items_without_slug(self):
        body = _rsc_item('', id_='1')
        templates = self._fetch(body)
        self.assertEqual(len(templates), 0)

    def test_warns_when_fewer_than_five_templates(self):
        body = _rsc_item('only-one')
        with patch('framer_templates.http_get', return_value=body), \
             patch('builtins.print') as mock_print:
            ft.fetch_from_rsc()
        output = ' '.join(str(c) for c in mock_print.call_args_list)
        self.assertIn('WARNING', output)

    def test_error_log_includes_body_preview_when_fewer_than_five_templates(self):
        """When < 5 templates parsed, the error log context must include a body_preview."""
        import error_log as el
        body = _rsc_item('only-one')
        with patch('framer_templates.http_get', return_value=body), \
             patch.object(el, 'log_error') as mock_log:
            ft.fetch_from_rsc()
        self.assertTrue(mock_log.called)
        # Find the call that logs the low-count warning
        low_count_calls = [
            c for c in mock_log.call_args_list
            if len(c[0]) >= 4 and isinstance(c[0][3], dict) and 'count' in c[0][3]
        ]
        self.assertTrue(low_count_calls, 'Expected at least one log_error call with count in context')
        ctx = low_count_calls[0][0][3]
        self.assertIn('body_preview', ctx)
        self.assertIsInstance(ctx['body_preview'], str)
        # body_preview should contain at least the start of the RSC body
        self.assertTrue(len(ctx['body_preview']) > 0)

    def test_body_preview_is_capped_at_500_chars(self):
        """body_preview in the error log context must be at most 500 characters."""
        import error_log as el
        # Construct a body with a single template (< 5) but very long content
        long_body = _rsc_item('only-one') + ('x' * 2000)
        with patch('framer_templates.http_get', return_value=long_body), \
             patch.object(el, 'log_error') as mock_log:
            ft.fetch_from_rsc()
        low_count_calls = [
            c for c in mock_log.call_args_list
            if len(c[0]) >= 4 and isinstance(c[0][3], dict) and 'count' in c[0][3]
        ]
        self.assertTrue(low_count_calls)
        ctx = low_count_calls[0][0][3]
        self.assertLessEqual(len(ctx['body_preview']), 500)

    def test_error_log_includes_parse_errors_count_in_low_count_context(self):
        """When < 5 templates parsed, the error log context must include parse_errors count."""
        import error_log as el
        body = _rsc_item('only-one')
        with patch('framer_templates.http_get', return_value=body), \
             patch.object(el, 'log_error') as mock_log:
            ft.fetch_from_rsc()
        low_count_calls = [
            c for c in mock_log.call_args_list
            if len(c[0]) >= 4 and isinstance(c[0][3], dict) and 'count' in c[0][3]
        ]
        self.assertTrue(low_count_calls, 'Expected at least one log_error call with count in context')
        ctx = low_count_calls[0][0][3]
        self.assertIn('parse_errors', ctx)
        self.assertIsInstance(ctx['parse_errors'], int)

    def test_parse_errors_nonzero_when_body_has_malformed_items(self):
        """parse_errors in error log context reflects actual JSON parse failures in the stream."""
        import error_log as el
        # One valid item + one unclosed JSON object → parse_errors should be 1
        good = _rsc_item('good-slug', id_='1')
        bad = '"item":{"id":"2","slug":"bad-open"'  # unclosed JSON object
        body = good + '\n' + bad
        with patch('framer_templates.http_get', return_value=body), \
             patch.object(el, 'log_error') as mock_log:
            ft.fetch_from_rsc()
        low_count_calls = [
            c for c in mock_log.call_args_list
            if len(c[0]) >= 4 and isinstance(c[0][3], dict) and 'count' in c[0][3]
        ]
        # Should have logged a low-count warning (only 1 valid template < 5)
        self.assertTrue(low_count_calls)
        ctx = low_count_calls[0][0][3]
        self.assertGreaterEqual(ctx['parse_errors'], 1)

    def test_no_warning_with_five_or_more_templates(self):
        body = '\n'.join(_rsc_item(f'slug-{i}', id_=str(i)) for i in range(5))
        with patch('framer_templates.http_get', return_value=body), \
             patch('builtins.print') as mock_print:
            ft.fetch_from_rsc()
        output = ' '.join(str(c) for c in mock_print.call_args_list)
        self.assertNotIn('WARNING', output)

    def test_fetches_page_2_when_page_1_is_full(self):
        # A full first page (20 items) means there may be more; page 2 must be fetched.
        page2_body = _rsc_item('slug-20', id_='20')  # 1 new item on page 2
        with patch('framer_templates.http_get', side_effect=[_full_page(), page2_body]) as mock_get:
            templates = ft.fetch_from_rsc()
        self.assertEqual(mock_get.call_count, 2)
        self.assertEqual(len(templates), 21)

    def test_does_not_fetch_page_2_when_page_1_is_partial(self):
        body = '\n'.join(_rsc_item(f'slug-{i}', id_=str(i)) for i in range(5))
        with patch('framer_templates.http_get', side_effect=[body]) as mock_get:
            templates = ft.fetch_from_rsc()
        self.assertEqual(mock_get.call_count, 1)
        self.assertEqual(len(templates), 5)

    def test_page_2_url_includes_page_param(self):
        page2_body = _rsc_item('slug-20', id_='20')
        with patch('framer_templates.http_get', side_effect=[_full_page(), page2_body]) as mock_get:
            ft.fetch_from_rsc()
        second_call_url = mock_get.call_args_list[1][0][0]
        self.assertIn('page=2', second_call_url)

    def test_page_1_url_does_not_include_page_param(self):
        with patch('framer_templates.http_get', side_effect=[_rsc_item('s')]) as mock_get:
            ft.fetch_from_rsc()
        first_call_url = mock_get.call_args_list[0][0][0]
        self.assertNotIn('page=', first_call_url)

    def test_stops_after_max_2_pages(self):
        # Two full pages — loop must stop at page 2 without fetching a 3rd.
        with patch('framer_templates.http_get',
                   side_effect=[_full_page(0), _full_page(20)]) as mock_get:
            templates = ft.fetch_from_rsc()
        self.assertEqual(mock_get.call_count, 2)
        self.assertEqual(len(templates), 40)

    def test_parses_templates_when_item_colon_has_space(self):
        # Regression: RSC format may emit "item": { instead of "item":{
        body = _rsc_item('whitespace-slug', id_='77').replace('"item":{', '"item": {')
        templates = self._fetch(body)
        self.assertEqual(len(templates), 1)
        self.assertEqual(templates[0]['slug'], 'whitespace-slug')

    def test_cumulative_pages_deduplicate_correctly(self):
        # page=2 from Framer is cumulative: it contains page=1 items + some new ones.
        # This page adds items 0-19 again (duplicates) plus 15 new items — total 35 unique.
        page1 = _full_page(0)
        extra = '\n'.join(_rsc_item(f'slug-{20 + i}', id_=str(20 + i)) for i in range(15))
        page2 = _full_page(0) + '\n' + extra  # 20 dups + 15 new → new_this_page=15 → stops
        with patch('framer_templates.http_get', side_effect=[page1, page2]):
            templates = ft.fetch_from_rsc()
        self.assertEqual(len(templates), 35)


# ---------------------------------------------------------------------------
# _parse_rsc_body
# ---------------------------------------------------------------------------

class TestParseRscBody(unittest.TestCase):

    def _parse(self, body):
        seen: set = set()
        templates: list = []
        ft._parse_rsc_body(body, seen, templates)
        return seen, templates

    def test_appends_new_templates(self):
        _, templates = self._parse(_rsc_item('s1', id_='1') + '\n' + _rsc_item('s2', id_='2'))
        self.assertEqual(len(templates), 2)

    def test_skips_slugs_already_in_seen(self):
        seen = {'existing'}
        templates: list = []
        ft._parse_rsc_body(_rsc_item('existing', id_='1'), seen, templates)
        self.assertEqual(len(templates), 0)

    def test_updates_seen_with_new_slugs(self):
        seen, _ = self._parse(_rsc_item('new-slug', id_='1'))
        self.assertIn('new-slug', seen)

    def test_skips_item_without_slug(self):
        _, templates = self._parse(_rsc_item('', id_='1'))
        self.assertEqual(len(templates), 0)

    def test_multiple_calls_accumulate(self):
        seen: set = set()
        templates: list = []
        ft._parse_rsc_body(_rsc_item('s1', id_='1'), seen, templates)
        ft._parse_rsc_body(_rsc_item('s2', id_='2'), seen, templates)
        self.assertEqual(len(templates), 2)

    def test_parses_item_with_space_after_colon(self):
        # RSC may emit "item": {"id":... (space between colon and brace)
        body = _rsc_item('spaced-slug', id_='42').replace('"item":{', '"item": {')
        _, templates = self._parse(body)
        self.assertEqual(len(templates), 1)
        self.assertEqual(templates[0]['slug'], 'spaced-slug')

    def test_parses_item_with_newline_after_colon(self):
        # RSC may emit "item":\n{"id":... (newline between colon and brace)
        body = _rsc_item('newline-slug', id_='43').replace('"item":{', '"item":\n{')
        _, templates = self._parse(body)
        self.assertEqual(len(templates), 1)
        self.assertEqual(templates[0]['slug'], 'newline-slug')

    def test_skips_item_key_not_followed_by_object(self):
        # "item": "string" — not a JSON object, should be skipped gracefully
        body = '"item":"just a string"'
        _, templates = self._parse(body)
        self.assertEqual(len(templates), 0)

    def test_skips_object_without_id_field(self):
        # A JSON object after "item": that has no "id" key should be skipped
        body = '"item":{"slug":"no-id","title":"T"}'
        _, templates = self._parse(body)
        self.assertEqual(len(templates), 0)

    def test_continues_after_non_object_item(self):
        # A non-object "item": followed by a valid template item must still parse the latter
        item_str = _rsc_item('after-junk', id_='99')
        body = '"item":"junk"\n' + item_str
        _, templates = self._parse(body)
        self.assertEqual(len(templates), 1)
        self.assertEqual(templates[0]['slug'], 'after-junk')

    def test_custom_search_key_parses_templateItem(self):
        # _parse_rsc_body must work with alternative RSC key "templateItem":
        body = _rsc_item_key('alt-slug', key='"templateItem":', id_='55')
        seen: set = set()
        templates: list = []
        ft._parse_rsc_body(body, seen, templates, search_key='"templateItem":')
        self.assertEqual(len(templates), 1)
        self.assertEqual(templates[0]['slug'], 'alt-slug')

    def test_custom_search_key_ignores_primary_key_items(self):
        # When searching with a fallback key, items using the primary key are not found
        body = _rsc_item('primary-slug', id_='1')  # uses '"item":'
        seen: set = set()
        templates: list = []
        ft._parse_rsc_body(body, seen, templates, search_key='"templateItem":')
        self.assertEqual(len(templates), 0)

    def test_default_search_key_is_primary(self):
        # Calling without search_key uses the default '"item":' key
        body = _rsc_item('default-key-slug', id_='1')
        seen: set = set()
        templates: list = []
        ft._parse_rsc_body(body, seen, templates)
        self.assertEqual(len(templates), 1)

    def test_returns_zero_parse_errors_on_clean_body(self):
        body = _rsc_item('clean-slug', id_='1')
        seen: set = set()
        templates: list = []
        errors = ft._parse_rsc_body(body, seen, templates)
        self.assertEqual(errors, 0)

    def test_returns_parse_error_count_for_unclosed_json(self):
        # An unclosed JSON object after "item": should be counted as a parse error
        body = '"item":{"id":"1","slug":"broken"'  # unclosed — ValueError from _extract_json_object
        seen: set = set()
        templates: list = []
        errors = ft._parse_rsc_body(body, seen, templates)
        self.assertEqual(errors, 1)
        self.assertEqual(len(templates), 0)

    def test_parse_errors_counted_across_multiple_bad_items(self):
        # Two unclosed objects should yield two parse errors
        bad = '"item":{"id":"1","slug":"bad1"'
        body = bad + '\n' + bad.replace('"1"', '"2"').replace('"bad1"', '"bad2"')
        seen: set = set()
        templates: list = []
        errors = ft._parse_rsc_body(body, seen, templates)
        self.assertEqual(errors, 2)

    def test_mixed_good_and_bad_items_counted_correctly(self):
        # One good item and one unclosed object
        good = _rsc_item('good-slug', id_='1')
        bad = '"item":{"id":"2","slug":"bad"'  # unclosed
        body = good + '\n' + bad
        seen: set = set()
        templates: list = []
        errors = ft._parse_rsc_body(body, seen, templates)
        self.assertEqual(errors, 1)
        self.assertEqual(len(templates), 1)


# ---------------------------------------------------------------------------
# fetch_from_rsc — fallback key behaviour
# ---------------------------------------------------------------------------

class TestFetchFromRscFallback(unittest.TestCase):
    """Tests for automatic fallback to alternative RSC search keys."""

    def test_uses_fallback_key_when_primary_yields_fewer_than_five(self):
        # Body has items under "templateItem": only — primary key finds nothing
        body = _rsc_item_key('fb-slug', key='"templateItem":', id_='1')
        with patch('framer_templates.http_get', return_value=body):
            templates = ft.fetch_from_rsc()
        self.assertEqual(len(templates), 1)
        self.assertEqual(templates[0]['slug'], 'fb-slug')

    def test_uses_marketplaceItem_fallback_key(self):
        body = _rsc_item_key('mp-slug', key='"marketplaceItem":', id_='2')
        with patch('framer_templates.http_get', return_value=body):
            templates = ft.fetch_from_rsc()
        self.assertEqual(len(templates), 1)
        self.assertEqual(templates[0]['slug'], 'mp-slug')

    def test_fallback_prefers_key_with_more_results(self):
        # Primary key gives 1 item; "templateItem": gives 3 items; "marketplaceItem": gives 2.
        # Fallback should pick "templateItem": as the winner.
        primary_items = _rsc_item('prim', id_='0')
        template_items = '\n'.join(
            _rsc_item_key(f'ti-{i}', key='"templateItem":', id_=str(10 + i))
            for i in range(3)
        )
        body = primary_items + '\n' + template_items + '\n' + '\n'.join(
            _rsc_item_key(f'mi-{i}', key='"marketplaceItem":', id_=str(20 + i))
            for i in range(2)
        )
        with patch('framer_templates.http_get', return_value=body):
            templates = ft.fetch_from_rsc()
        # Best fallback (templateItem) wins with 3 templates
        self.assertEqual(len(templates), 3)
        slugs = {t['slug'] for t in templates}
        self.assertTrue(all(s.startswith('ti-') for s in slugs))

    def test_primary_key_used_when_yields_five_or_more(self):
        # Primary gives >= 5 results — fallback must NOT be attempted
        body = '\n'.join(_rsc_item(f's-{i}', id_=str(i)) for i in range(5))
        with patch('framer_templates.http_get', return_value=body), \
             patch('framer_templates._parse_rsc_body', wraps=ft._parse_rsc_body) as mock_parse:
            templates = ft.fetch_from_rsc()
        self.assertEqual(len(templates), 5)
        # _parse_rsc_body should only be called with the primary key
        called_keys = [call[0][3] if len(call[0]) > 3 else call[1].get('search_key', ft._RSC_PRIMARY_KEY)
                       for call in mock_parse.call_args_list]
        self.assertTrue(all(k == ft._RSC_PRIMARY_KEY for k in called_keys))

    def test_fallback_logs_warning_when_better_key_found(self):
        import error_log as el
        body = _rsc_item_key('fb-warn', key='"templateItem":', id_='5')
        with patch('framer_templates.http_get', return_value=body), \
             patch.object(el, 'log_error') as mock_log:
            ft.fetch_from_rsc()
        # Should log a warning about the primary key yielding too few results
        fallback_calls = [
            c for c in mock_log.call_args_list
            if len(c[0]) >= 3 and 'fallback' in c[0][2].lower()
        ]
        self.assertTrue(fallback_calls, 'Expected a log_error call mentioning fallback key')

    def test_no_fallback_log_when_primary_succeeds(self):
        import error_log as el
        body = '\n'.join(_rsc_item(f's-{i}', id_=str(i)) for i in range(5))
        with patch('framer_templates.http_get', return_value=body), \
             patch.object(el, 'log_error') as mock_log:
            ft.fetch_from_rsc()
        fallback_calls = [
            c for c in mock_log.call_args_list
            if len(c[0]) >= 3 and 'fallback' in c[0][2].lower()
        ]
        self.assertEqual(fallback_calls, [], 'No fallback log expected when primary key succeeds')

    def test_fallback_still_warns_when_all_keys_yield_fewer_than_five(self):
        # When all keys fail to find >= 5 templates, the standard low-count warning fires
        body = _rsc_item('lone-item', id_='1')  # only 1 template under primary key
        with patch('framer_templates.http_get', return_value=body), \
             patch('builtins.print') as mock_print:
            ft.fetch_from_rsc()
        output = ' '.join(str(c) for c in mock_print.call_args_list)
        self.assertIn('WARNING', output)


# ---------------------------------------------------------------------------
# _find_candidate_rsc_keys
# ---------------------------------------------------------------------------

class TestFindCandidateRscKeys(unittest.TestCase):

    def test_finds_key_before_object_with_id_and_slug(self):
        # A JSON object with both "id": and "slug": preceded by a quoted key
        body = '"newKey":{"id":"1","slug":"my-template","title":"T"}'
        candidates = ft._find_candidate_rsc_keys(body)
        self.assertIn('"newKey":', candidates)

    def test_returns_empty_list_when_no_candidates(self):
        # RSC body with no objects containing both id and slug
        body = '"item":{"title":"No id or slug here"}'
        candidates = ft._find_candidate_rsc_keys(body)
        self.assertEqual(candidates, [])

    def test_deduplicates_same_key_appearing_multiple_times(self):
        # The same key "newKey": appears before two different objects
        obj = '{"id":"1","slug":"s1","title":"T"}'
        body = f'"newKey":{obj}\n"newKey":' + '{"id":"2","slug":"s2","title":"T"}'
        candidates = ft._find_candidate_rsc_keys(body)
        self.assertEqual(candidates.count('"newKey":'), 1)

    def test_respects_max_results_limit(self):
        # Five different keys with valid objects — max_results=3 should return only 3
        parts = [f'"key{i}":' + '{"id":"' + str(i) + '","slug":"s' + str(i) + '"}' for i in range(5)]
        body = '\n'.join(parts)
        candidates = ft._find_candidate_rsc_keys(body, max_results=3)
        self.assertEqual(len(candidates), 3)

    def test_ignores_objects_missing_id(self):
        body = '"noId":{"slug":"s1","title":"T"}'
        candidates = ft._find_candidate_rsc_keys(body)
        self.assertEqual(candidates, [])

    def test_ignores_objects_missing_slug(self):
        body = '"noSlug":{"id":"1","title":"T"}'
        candidates = ft._find_candidate_rsc_keys(body)
        self.assertEqual(candidates, [])

    def test_handles_empty_body(self):
        candidates = ft._find_candidate_rsc_keys('')
        self.assertEqual(candidates, [])

    def test_candidate_key_format_includes_quotes_and_colon(self):
        # Returned candidates should be in the form '"keyName":' (with surrounding quotes)
        body = '"templateData":{"id":"1","slug":"s1"}'
        candidates = ft._find_candidate_rsc_keys(body)
        self.assertTrue(len(candidates) >= 1)
        self.assertTrue(candidates[0].startswith('"'))
        self.assertTrue(candidates[0].endswith(':'))


class TestFetchFromRscCandidateKeys(unittest.TestCase):
    """Tests for candidate_keys logging in fetch_from_rsc."""

    def test_candidate_keys_logged_when_zero_templates_and_zero_parse_errors(self):
        """When 0 templates + 0 parse errors, candidate_keys must appear in the error log
        if _find_candidate_rsc_keys finds any candidates."""
        import error_log as el
        # Build a body with a new key that produces id+slug objects, but no known RSC key
        body = '"unknownKey":{"id":"1","slug":"s1","title":"T"}'
        with patch('framer_templates.http_get', return_value=body), \
             patch.object(el, 'log_error') as mock_log:
            ft.fetch_from_rsc()
        low_count_calls = [
            c for c in mock_log.call_args_list
            if len(c[0]) >= 4 and isinstance(c[0][3], dict) and 'count' in c[0][3]
        ]
        self.assertTrue(low_count_calls, 'Expected a low-count log_error call')
        ctx = low_count_calls[0][0][3]
        self.assertIn('candidate_keys', ctx)
        self.assertIn('"unknownKey":', ctx['candidate_keys'])

    def test_candidate_keys_absent_when_no_candidates_found(self):
        """When _find_candidate_rsc_keys returns empty, candidate_keys must not appear in ctx."""
        import error_log as el
        # Body with no objects containing both id and slug
        body = 'no templates here, no id/slug objects'
        with patch('framer_templates.http_get', return_value=body), \
             patch.object(el, 'log_error') as mock_log:
            ft.fetch_from_rsc()
        low_count_calls = [
            c for c in mock_log.call_args_list
            if len(c[0]) >= 4 and isinstance(c[0][3], dict) and 'count' in c[0][3]
        ]
        self.assertTrue(low_count_calls)
        ctx = low_count_calls[0][0][3]
        self.assertNotIn('candidate_keys', ctx)

    def test_candidate_keys_absent_when_parse_errors_present(self):
        """When parse_errors > 0, candidate key scanning is skipped (JSON is found but malformed)."""
        import error_log as el
        # One valid template (< 5) + one unclosed object = parse_errors > 0
        # Candidate scanning should not run because parse_errors != 0
        body = _rsc_item('valid-one', id_='1') + '\n"item":{"id":"2","slug":"bad"'  # unclosed
        with patch('framer_templates.http_get', return_value=body), \
             patch.object(el, 'log_error') as mock_log:
            ft.fetch_from_rsc()
        low_count_calls = [
            c for c in mock_log.call_args_list
            if len(c[0]) >= 4 and isinstance(c[0][3], dict) and 'count' in c[0][3]
        ]
        self.assertTrue(low_count_calls)
        ctx = low_count_calls[0][0][3]
        # candidate_keys should not appear — parse_errors > 0 means the key IS known
        self.assertNotIn('candidate_keys', ctx)

    def test_candidate_keys_absent_when_templates_found(self):
        """When >= 5 templates are found, no low-count error is logged at all."""
        import error_log as el
        body = '\n'.join(_rsc_item(f's-{i}', id_=str(i)) for i in range(5))
        with patch('framer_templates.http_get', return_value=body), \
             patch.object(el, 'log_error') as mock_log:
            ft.fetch_from_rsc()
        low_count_calls = [
            c for c in mock_log.call_args_list
            if len(c[0]) >= 4 and isinstance(c[0][3], dict) and 'count' in c[0][3]
        ]
        self.assertEqual(low_count_calls, [], 'No low-count error expected when >= 5 templates found')

    def test_candidate_keys_scanned_across_all_pages_not_just_last(self):
        """Candidate keys from page 1 must be included even when page 2 has none.

        fetch_from_rsc fetches up to 2 pages.  If the new RSC key only appears on
        page 1 and page 2 is a stripped-down response (e.g. just the RSC frame with
        no template objects), the scanner must still report the key from page 1.
        """
        import error_log as el
        # Page 1: contains a template under a new unknown key (triggers 20 new items
        # because we need >=20 new items to fetch page 2 — but we only have 1 here,
        # so fetch_from_rsc will stop at page 1).  Use _full_page-style body with
        # the unknown key so we get exactly 20 items (triggers page 2 fetch).
        page1_items = '\n'.join(
            f'"unknownKey":{{"id":"{i}","slug":"s{i}","title":"T"}}'
            for i in range(20)
        )
        # Page 2: empty RSC frame — no template objects
        page2 = '1:"$Sreact.fragment"\n'
        with patch('framer_templates.http_get', side_effect=[page1_items, page2]), \
             patch.object(el, 'log_error') as mock_log:
            ft.fetch_from_rsc()
        low_count_calls = [
            c for c in mock_log.call_args_list
            if len(c[0]) >= 4 and isinstance(c[0][3], dict) and 'count' in c[0][3]
        ]
        self.assertTrue(low_count_calls, 'Expected a low-count log_error call')
        ctx = low_count_calls[0][0][3]
        self.assertIn('candidate_keys', ctx,
                      'candidate_keys must be logged even when the key only appears on page 1')
        self.assertIn('"unknownKey":', ctx['candidate_keys'])

    def test_candidate_keys_deduped_across_pages(self):
        """The same key appearing on both pages must only appear once in candidate_keys."""
        import error_log as el
        page1 = '"unknownKey":{"id":"1","slug":"s1","title":"T"}'
        page2 = '"unknownKey":{"id":"2","slug":"s2","title":"T"}'
        # page1 has 1 item → fetch_from_rsc stops (< 20 new), no page 2 fetch.
        # To force both pages to be fetched, make page1 have exactly 20 items.
        page1_full = '\n'.join(
            f'"unknownKey":{{"id":"{i}","slug":"s{i}","title":"T"}}'
            for i in range(20)
        )
        with patch('framer_templates.http_get', side_effect=[page1_full, page2]), \
             patch.object(el, 'log_error') as mock_log:
            ft.fetch_from_rsc()
        low_count_calls = [
            c for c in mock_log.call_args_list
            if len(c[0]) >= 4 and isinstance(c[0][3], dict) and 'count' in c[0][3]
        ]
        self.assertTrue(low_count_calls)
        ctx = low_count_calls[0][0][3]
        candidate_keys = ctx.get('candidate_keys', [])
        self.assertEqual(
            candidate_keys.count('"unknownKey":'), 1,
            'Same key from multiple pages must not be duplicated in candidate_keys',
        )


# ---------------------------------------------------------------------------
# _rsc_payload_type
# ---------------------------------------------------------------------------

class TestRscPayloadType(unittest.TestCase):

    def test_chunk_reference_normalises_to_I_bracket(self):
        self.assertEqual(ft._rsc_payload_type('I[339756,[],\"default\"]'), 'I[')

    def test_chunk_reference_with_different_id_same_type(self):
        # Different chunk IDs must produce the same label
        self.assertEqual(ft._rsc_payload_type('I[837457,[]]'), 'I[')
        self.assertEqual(ft._rsc_payload_type('I[100000,[]]'), 'I[')

    def test_react_server_component_ref(self):
        self.assertEqual(ft._rsc_payload_type('"$Sreact.fragment"'), '"$S"')

    def test_react_special_string(self):
        self.assertEqual(ft._rsc_payload_type('"$undefined"'), '"$"')

    def test_plain_string_literal(self):
        self.assertEqual(ft._rsc_payload_type('"hello world"'), '"str"')

    def test_inline_json_object(self):
        self.assertEqual(ft._rsc_payload_type('{"id":"1"}'), '{')

    def test_json_array(self):
        self.assertEqual(ft._rsc_payload_type('[1,2,3]'), '[')

    def test_fallback_for_unknown_payload(self):
        # Unknown payload types fall back to first 4 characters
        self.assertEqual(ft._rsc_payload_type('null'), 'null')
        self.assertEqual(ft._rsc_payload_type('true'), 'true')
        self.assertEqual(ft._rsc_payload_type('XY'), 'XY')

    def test_empty_payload_fallback(self):
        self.assertEqual(ft._rsc_payload_type(''), '')


# ---------------------------------------------------------------------------
# _sample_rsc_line_prefixes
# ---------------------------------------------------------------------------

class TestSampleRscLinePrefixes(unittest.TestCase):

    def test_returns_empty_list_for_empty_body(self):
        self.assertEqual(ft._sample_rsc_line_prefixes(''), [])

    def test_returns_empty_list_when_no_numbered_lines(self):
        body = 'just some text\nnot RSC format'
        self.assertEqual(ft._sample_rsc_line_prefixes(body), [])

    def test_extracts_single_rsc_line(self):
        body = '1:"$Sreact.fragment"'
        result = ft._sample_rsc_line_prefixes(body)
        self.assertEqual(len(result), 1)
        self.assertTrue(result[0].startswith('1:'))
        self.assertIn('"$S"', result[0])

    def test_deduplicates_same_payload_type(self):
        # Two "$S" lines (react.fragment and react.suspense) — same type, deduplicated
        body = '1:"$Sreact.fragment"\n2:"$Sreact.suspense"'
        result = ft._sample_rsc_line_prefixes(body)
        self.assertEqual(len(result), 1)

    def test_chunk_references_with_different_ids_deduplicated(self):
        # Multiple I[...] lines with different chunk IDs must produce exactly one entry
        body = (
            '5:I[339756,["/chunk-a.js"],"default"]\n'
            '6:I[837457,["/chunk-b.js"],"default"]\n'
            '7:I[100000,["/chunk-c.js"],"default"]\n'
        )
        result = ft._sample_rsc_line_prefixes(body)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0], '5:I[')

    def test_collects_distinct_payload_types(self):
        body = '1:"$Sreact.fragment"\n3:I[339756,[]]\n5:{"id":"1"}'
        result = ft._sample_rsc_line_prefixes(body)
        # Three distinct types: '"$S"', 'I[', '{'
        self.assertEqual(len(result), 3)
        type_labels = [r.split(':', 1)[1] for r in result]
        self.assertIn('"$S"', type_labels)
        self.assertIn('I[', type_labels)
        self.assertIn('{', type_labels)

    def test_respects_max_lines_limit(self):
        # 15 lines each with a distinct fallback payload (uppercase letters)
        import string
        prefixes = list(string.ascii_uppercase[:15])
        lines = [f'{i}:{p}xxx_payload_{i}' for i, p in enumerate(prefixes)]
        body = '\n'.join(lines)
        result = ft._sample_rsc_line_prefixes(body, max_lines=5)
        self.assertEqual(len(result), 5)

    def test_skips_blank_lines(self):
        body = '\n\n1:"$Sreact.fragment"\n\n3:I[1,[]]'
        result = ft._sample_rsc_line_prefixes(body)
        self.assertEqual(len(result), 2)

    def test_format_is_row_colon_type(self):
        # Each returned string should be "<row>:<normalised_type>"
        body = '5:I[339756,[]]'
        result = ft._sample_rsc_line_prefixes(body)
        self.assertEqual(len(result), 1)
        self.assertTrue(result[0].startswith('5:'))
        self.assertEqual(result[0], '5:I[')

    def test_ignores_non_numeric_prefix_lines(self):
        body = 'abc:notanrscline\n1:"$Sreact.fragment"'
        result = ft._sample_rsc_line_prefixes(body)
        self.assertEqual(len(result), 1)
        self.assertIn('"$S"', result[0])

    def test_handles_body_matching_april20_error_preview(self):
        # Reproduce the body_preview from the April 20 framer_templates warning.
        # Multiple I[...] lines with different chunk IDs must collapse to one entry.
        body = (
            '1:"$Sreact.fragment"\n'
            '3:"$Sreact.suspense"\n'
            '5:I[339756,["/creators-assets/_next/static/chunks/6005aca2ea3cc118.js"],"default"]\n'
            '6:I[837457,["/creators-assets/_next/static/chunks/c059ee9b9697f96a.js"],"default"]\n'
        )
        result = ft._sample_rsc_line_prefixes(body)
        self.assertGreater(len(result), 0)
        # Should capture the "$S" type and the "I[" chunk-reference type, but NOT
        # duplicate I[ entries for different chunk IDs.
        type_labels = [r.split(':', 1)[1] for r in result]
        self.assertIn('"$S"', type_labels)
        self.assertIn('I[', type_labels)
        self.assertEqual(type_labels.count('I['), 1, 'Chunk references must not be duplicated')


class TestFetchFromRscLineTypeLogging(unittest.TestCase):
    """Tests for rsc_line_types fallback diagnostic when no candidates found."""

    def test_rsc_line_types_logged_when_no_candidate_keys_and_rsc_format_body(self):
        """When 0 templates + 0 parse_errors + 0 candidate_keys, rsc_line_types must
        be logged if the body contains RSC flight-format lines."""
        import error_log as el
        # Body that looks like RSC flight format (no JSON template objects)
        body = (
            '1:"$Sreact.fragment"\n'
            '3:"$Sreact.suspense"\n'
            '5:I[339756,[],"default"]\n'
        )
        with patch('framer_templates.http_get', return_value=body), \
             patch.object(el, 'log_error') as mock_log:
            ft.fetch_from_rsc()
        low_count_calls = [
            c for c in mock_log.call_args_list
            if len(c[0]) >= 4 and isinstance(c[0][3], dict) and 'count' in c[0][3]
        ]
        self.assertTrue(low_count_calls, 'Expected a low-count log_error call')
        ctx = low_count_calls[0][0][3]
        self.assertIn('rsc_line_types', ctx,
                      'rsc_line_types must be logged when RSC flight lines are present but no templates found')
        self.assertIsInstance(ctx['rsc_line_types'], list)
        self.assertGreater(len(ctx['rsc_line_types']), 0)

    def test_rsc_line_types_absent_when_candidate_keys_found(self):
        """When candidate_keys are found, rsc_line_types must NOT appear (redundant)."""
        import error_log as el
        # Body with a new key that has id+slug objects — candidate_keys will be found
        body = '"newFormatKey":{"id":"1","slug":"s1","title":"T"}'
        with patch('framer_templates.http_get', return_value=body), \
             patch.object(el, 'log_error') as mock_log:
            ft.fetch_from_rsc()
        low_count_calls = [
            c for c in mock_log.call_args_list
            if len(c[0]) >= 4 and isinstance(c[0][3], dict) and 'count' in c[0][3]
        ]
        self.assertTrue(low_count_calls)
        ctx = low_count_calls[0][0][3]
        self.assertNotIn('rsc_line_types', ctx,
                         'rsc_line_types must not appear when candidate_keys was already logged')

    def test_rsc_line_types_absent_when_parse_errors_nonzero(self):
        """When parse_errors > 0, rsc_line_types must not appear (wrong diagnostic branch)."""
        import error_log as el
        body = _rsc_item('only-one', id_='1') + '\n"item":{"id":"2","slug":"bad"'  # unclosed
        with patch('framer_templates.http_get', return_value=body), \
             patch.object(el, 'log_error') as mock_log:
            ft.fetch_from_rsc()
        low_count_calls = [
            c for c in mock_log.call_args_list
            if len(c[0]) >= 4 and isinstance(c[0][3], dict) and 'count' in c[0][3]
        ]
        self.assertTrue(low_count_calls)
        ctx = low_count_calls[0][0][3]
        self.assertNotIn('rsc_line_types', ctx)

    def test_rsc_line_types_absent_when_body_has_no_rsc_lines(self):
        """When body has no RSC flight lines, rsc_line_types must not appear."""
        import error_log as el
        # Body with no templates and no RSC-format lines
        body = 'no templates here, plain text only'
        with patch('framer_templates.http_get', return_value=body), \
             patch.object(el, 'log_error') as mock_log:
            ft.fetch_from_rsc()
        low_count_calls = [
            c for c in mock_log.call_args_list
            if len(c[0]) >= 4 and isinstance(c[0][3], dict) and 'count' in c[0][3]
        ]
        self.assertTrue(low_count_calls)
        ctx = low_count_calls[0][0][3]
        self.assertNotIn('rsc_line_types', ctx)


# ---------------------------------------------------------------------------
# infer_category / group_by_category
# ---------------------------------------------------------------------------

def _template(title='T', meta_title='', slug='s', price='Free', author='A'):
    return {
        'title': title, 'meta_title': meta_title, 'slug': slug,
        'url': f'https://www.framer.com/marketplace/templates/{slug}/',
        'author': author, 'author_slug': '', 'price': price,
        'thumbnail': '', 'published_at': '', 'demo_url': '', 'remixes': 0,
    }


class TestInferCategory(unittest.TestCase):

    def test_matches_title_keyword(self):
        self.assertEqual(ft.infer_category(_template(title='Gym Fitness Pro')), 'Health & Fitness')

    def test_matches_meta_title_keyword(self):
        self.assertEqual(ft.infer_category(_template(title='Flavor', meta_title='Restaurant Website')), 'Food & Dining')

    def test_case_insensitive(self):
        self.assertEqual(ft.infer_category(_template(title='PORTFOLIO Site')), 'Portfolio & Creative')

    def test_returns_other_when_no_match(self):
        self.assertEqual(ft.infer_category(_template(title='Abstract Minimal')), 'Other')

    def test_first_matching_category_wins(self):
        # "SaaS" appears before "Landing Page" in CATEGORY_KEYWORDS
        self.assertEqual(ft.infer_category(_template(title='SaaS Landing Page')), 'SaaS & Tech')

    def test_multi_word_keyword(self):
        self.assertEqual(ft.infer_category(_template(title='Luxury Real Estate')), 'Real Estate')


class TestGroupByCategory(unittest.TestCase):

    def test_groups_templates_correctly(self):
        templates = [
            _template(title='Gym Pro', slug='gym'),
            _template(title='My Portfolio', slug='port'),
            _template(title='Yoga Studio', slug='yoga'),
        ]
        grouped = ft.group_by_category(templates)
        self.assertIn('Health & Fitness', grouped)
        self.assertEqual(len(grouped['Health & Fitness']), 2)
        self.assertIn('Portfolio & Creative', grouped)
        self.assertEqual(len(grouped['Portfolio & Creative']), 1)

    def test_other_category_at_end(self):
        templates = [
            _template(title='Abstract Minimal', slug='abs'),
            _template(title='Gym Pro', slug='gym'),
        ]
        grouped = ft.group_by_category(templates)
        keys = list(grouped.keys())
        self.assertEqual(keys[-1], 'Other')

    def test_empty_list(self):
        self.assertEqual(ft.group_by_category([]), {})

    def test_preserves_category_order(self):
        templates = [
            _template(title='My Blog', slug='blog'),
            _template(title='Restaurant', slug='rest'),
            _template(title='SaaS App', slug='saas'),
        ]
        grouped = ft.group_by_category(templates)
        keys = list(grouped.keys())
        # Food & Dining comes before SaaS & Tech in CATEGORY_KEYWORDS
        self.assertLess(keys.index('Food & Dining'), keys.index('SaaS & Tech'))
        self.assertLess(keys.index('SaaS & Tech'), keys.index('Blog & Magazine'))


# ---------------------------------------------------------------------------
# get_seen_slugs
# ---------------------------------------------------------------------------

def _notion_response(slugs, has_more=False, next_cursor=None):
    resp = {
        'results': [
            {'properties': {'Slug': {'rich_text': [{'plain_text': s}]}}}
            for s in slugs
        ],
        'has_more': has_more,
    }
    if next_cursor:
        resp['next_cursor'] = next_cursor
    return resp


class TestGetSeenSlugs(unittest.TestCase):

    def setUp(self):
        os.environ['NOTION_TOKEN'] = 'test_token'
        os.environ['NOTION_DATABASE_ID'] = 'test_db_id'

    def test_returns_slugs_from_single_page(self):
        with patch('framer_templates.http_post', return_value=_notion_response(['alpha', 'beta'])):
            self.assertEqual(ft.get_seen_slugs(), {'alpha', 'beta'})

    def test_empty_database_returns_empty_set(self):
        with patch('framer_templates.http_post', return_value=_notion_response([])):
            self.assertEqual(ft.get_seen_slugs(), set())

    def test_paginates_across_multiple_pages(self):
        page1 = _notion_response(['a', 'b'], has_more=True, next_cursor='cur1')
        page2 = _notion_response(['c'])
        with patch('framer_templates.http_post', side_effect=[page1, page2]):
            self.assertEqual(ft.get_seen_slugs(), {'a', 'b', 'c'})

    def test_passes_cursor_on_subsequent_page(self):
        page1 = _notion_response(['a'], has_more=True, next_cursor='my_cursor')
        page2 = _notion_response(['b'])
        with patch('framer_templates.http_post', side_effect=[page1, page2]) as mock_post:
            ft.get_seen_slugs()
        second_call_body = mock_post.call_args_list[1][0][1]
        self.assertEqual(second_call_body.get('start_cursor'), 'my_cursor')


# ---------------------------------------------------------------------------
# save_to_notion
# ---------------------------------------------------------------------------

_BASE_TEMPLATE = {
    'title': 'My Template',
    'slug': 'my-template',
    'url': 'https://www.framer.com/marketplace/templates/my-template/',
    'author': 'Alice',
    'author_slug': '',
    'price': 'Free',
    'published_at': '2024-01-15',
    'thumbnail': '',
    'meta_title': '',
    'demo_url': '',
    'remixes': 0,
}


class TestSaveToNotion(unittest.TestCase):

    def setUp(self):
        os.environ['NOTION_TOKEN'] = 'test_token'
        os.environ['NOTION_DATABASE_ID'] = 'test_db_id'

    def test_happy_path_calls_http_post_once(self):
        with patch('framer_templates.http_post', return_value={}) as mock_post:
            ft.save_to_notion(_BASE_TEMPLATE)
        mock_post.assert_called_once()

    def test_includes_thumbnail_property_when_present(self):
        t = {**_BASE_TEMPLATE, 'thumbnail': 'https://example.com/t.jpg'}
        with patch('framer_templates.http_post', return_value={}) as mock_post:
            ft.save_to_notion(t)
        props = mock_post.call_args[0][1]['properties']
        self.assertIn('Thumbnail', props)
        self.assertEqual(props['Thumbnail']['url'], 'https://example.com/t.jpg')

    def test_excludes_thumbnail_property_when_empty(self):
        with patch('framer_templates.http_post', return_value={}) as mock_post:
            ft.save_to_notion(_BASE_TEMPLATE)
        props = mock_post.call_args[0][1]['properties']
        self.assertNotIn('Thumbnail', props)

    def test_includes_published_property_when_present(self):
        with patch('framer_templates.http_post', return_value={}) as mock_post:
            ft.save_to_notion(_BASE_TEMPLATE)
        props = mock_post.call_args[0][1]['properties']
        self.assertIn('Published', props)
        self.assertEqual(props['Published']['date']['start'], '2024-01-15')

    def test_excludes_published_property_when_empty(self):
        t = {**_BASE_TEMPLATE, 'published_at': ''}
        with patch('framer_templates.http_post', return_value={}) as mock_post:
            ft.save_to_notion(t)
        props = mock_post.call_args[0][1]['properties']
        self.assertNotIn('Published', props)

    def test_400_with_thumbnail_retries_without_it(self):
        t = {**_BASE_TEMPLATE, 'thumbnail': 'https://example.com/t.jpg'}
        error = urllib.error.HTTPError(None, 400, 'Bad Request', {}, None)
        with patch('framer_templates.http_post', side_effect=[error, {}]) as mock_post:
            ft.save_to_notion(t)
        self.assertEqual(mock_post.call_count, 2)
        retry_props = mock_post.call_args_list[1][0][1]['properties']
        self.assertNotIn('Thumbnail', retry_props)

    def test_400_with_thumbnail_logs_notion_response(self):
        """Notion 400 on Thumbnail path logs the response body and slug for diagnosis."""
        import io
        t = {**_BASE_TEMPLATE, 'slug': 'my-template', 'thumbnail': 'https://example.com/t.jpg'}
        response_body = b'{"message": "Property not found"}'
        error = urllib.error.HTTPError(
            None, 400, 'Bad Request', {}, io.BytesIO(response_body)
        )
        with patch('framer_templates.http_post', side_effect=[error, {}]):
            with patch('error_log.log_error') as mock_log:
                ft.save_to_notion(t)
        mock_log.assert_called_once()
        call_kwargs = mock_log.call_args
        ctx = call_kwargs[0][3]  # positional arg: context dict
        self.assertIn('notion_response', ctx)
        self.assertIn('Property not found', ctx['notion_response'])
        self.assertEqual(ctx['slug'], 'my-template')

    def test_includes_author_url_when_slug_present(self):
        t = {**_BASE_TEMPLATE, 'author_slug': 'alice-studio'}
        with patch('framer_templates.http_post', return_value={}) as mock_post:
            ft.save_to_notion(t)
        props = mock_post.call_args[0][1]['properties']
        self.assertIn('Author URL', props)
        self.assertEqual(
            props['Author URL']['url'],
            'https://www.framer.com/marketplace/profiles/alice-studio/',
        )

    def test_excludes_author_url_when_slug_absent(self):
        t = {**_BASE_TEMPLATE, 'author_slug': ''}
        with patch('framer_templates.http_post', return_value={}) as mock_post:
            ft.save_to_notion(t)
        props = mock_post.call_args[0][1]['properties']
        self.assertNotIn('Author URL', props)

    def test_400_without_thumbnail_reraises(self):
        error = urllib.error.HTTPError(None, 400, 'Bad Request', {}, None)
        with patch('framer_templates.http_post', side_effect=error):
            with self.assertRaises(urllib.error.HTTPError):
                ft.save_to_notion(_BASE_TEMPLATE)

    def test_non_400_http_error_reraises(self):
        t = {**_BASE_TEMPLATE, 'thumbnail': 'https://example.com/t.jpg'}
        error = urllib.error.HTTPError(None, 500, 'Server Error', {}, None)
        with patch('framer_templates.http_post', side_effect=error):
            with self.assertRaises(urllib.error.HTTPError):
                ft.save_to_notion(t)

    def test_includes_meta_title_when_present(self):
        t = {**_BASE_TEMPLATE, 'meta_title': 'Gym & Fitness Studio Website'}
        with patch('framer_templates.http_post', return_value={}) as mock_post:
            ft.save_to_notion(t)
        props = mock_post.call_args[0][1]['properties']
        self.assertIn('Meta Title', props)
        self.assertEqual(props['Meta Title']['rich_text'][0]['text']['content'], 'Gym & Fitness Studio Website')

    def test_excludes_meta_title_when_empty(self):
        with patch('framer_templates.http_post', return_value={}) as mock_post:
            ft.save_to_notion(_BASE_TEMPLATE)
        props = mock_post.call_args[0][1]['properties']
        self.assertNotIn('Meta Title', props)

    def test_includes_demo_url_when_present(self):
        t = {**_BASE_TEMPLATE, 'demo_url': 'https://mysite.framer.website/'}
        with patch('framer_templates.http_post', return_value={}) as mock_post:
            ft.save_to_notion(t)
        props = mock_post.call_args[0][1]['properties']
        self.assertIn('Demo URL', props)
        self.assertEqual(props['Demo URL']['url'], 'https://mysite.framer.website/')

    def test_excludes_demo_url_when_empty(self):
        with patch('framer_templates.http_post', return_value={}) as mock_post:
            ft.save_to_notion(_BASE_TEMPLATE)
        props = mock_post.call_args[0][1]['properties']
        self.assertNotIn('Demo URL', props)

    def test_includes_remixes_when_nonzero(self):
        t = {**_BASE_TEMPLATE, 'remixes': 12}
        with patch('framer_templates.http_post', return_value={}) as mock_post:
            ft.save_to_notion(t)
        props = mock_post.call_args[0][1]['properties']
        self.assertIn('Remixes', props)
        self.assertEqual(props['Remixes']['number'], 12)

    def test_excludes_remixes_when_zero(self):
        with patch('framer_templates.http_post', return_value={}) as mock_post:
            ft.save_to_notion(_BASE_TEMPLATE)
        props = mock_post.call_args[0][1]['properties']
        self.assertNotIn('Remixes', props)

    def test_includes_category_select_property(self):
        t = {**_BASE_TEMPLATE, 'title': 'Gym & Fitness Pro', 'meta_title': ''}
        with patch('framer_templates.http_post', return_value={}) as mock_post:
            ft.save_to_notion(t)
        props = mock_post.call_args[0][1]['properties']
        self.assertIn('Category', props)
        self.assertEqual(props['Category']['select']['name'], 'Health & Fitness')

    def test_category_defaults_to_other_when_no_match(self):
        t = {**_BASE_TEMPLATE, 'title': 'Abstract Minimal', 'meta_title': ''}
        with patch('framer_templates.http_post', return_value={}) as mock_post:
            ft.save_to_notion(t)
        props = mock_post.call_args[0][1]['properties']
        self.assertEqual(props['Category']['select']['name'], 'Other')

    def test_category_uses_meta_title_for_inference(self):
        t = {**_BASE_TEMPLATE, 'title': 'Minimal', 'meta_title': 'Restaurant Website Template'}
        with patch('framer_templates.http_post', return_value={}) as mock_post:
            ft.save_to_notion(t)
        props = mock_post.call_args[0][1]['properties']
        self.assertEqual(props['Category']['select']['name'], 'Food & Dining')

    def test_discovered_timestamp_is_utc(self):
        """Discovered date must be a UTC-aware ISO 8601 timestamp (ends with +00:00)."""
        with patch('framer_templates.http_post', return_value={}) as mock_post:
            ft.save_to_notion(_BASE_TEMPLATE)
        props = mock_post.call_args[0][1]['properties']
        discovered = props['Discovered']['date']['start']
        self.assertTrue(
            discovered.endswith('+00:00'),
            f'Expected UTC timestamp ending in +00:00, got: {discovered!r}',
        )

    def test_discovered_timestamp_is_parseable_iso8601(self):
        """Discovered date must be a valid ISO 8601 datetime string."""
        from datetime import datetime, timezone
        with patch('framer_templates.http_post', return_value={}) as mock_post:
            ft.save_to_notion(_BASE_TEMPLATE)
        props = mock_post.call_args[0][1]['properties']
        discovered = props['Discovered']['date']['start']
        # fromisoformat should not raise
        dt = datetime.fromisoformat(discovered)
        self.assertIsNotNone(dt.tzinfo, 'Discovered timestamp must be timezone-aware')

    def test_title_truncated_to_2000(self):
        """Name title field must be truncated to 2000 chars to avoid Notion 400 errors."""
        t = {**_BASE_TEMPLATE, 'title': 'x' * 3000}
        with patch('framer_templates.http_post', return_value={}) as mock_post:
            ft.save_to_notion(t)
        props = mock_post.call_args[0][1]['properties']
        name_val = props['Name']['title'][0]['text']['content']
        self.assertEqual(len(name_val), 2000)

    def test_author_truncated_to_2000(self):
        """Author rich_text field must be truncated to 2000 chars."""
        t = {**_BASE_TEMPLATE, 'author': 'A' * 3000}
        with patch('framer_templates.http_post', return_value={}) as mock_post:
            ft.save_to_notion(t)
        props = mock_post.call_args[0][1]['properties']
        author_val = props['Author']['rich_text'][0]['text']['content']
        self.assertEqual(len(author_val), 2000)

    def test_meta_title_truncated_to_2000(self):
        """Meta Title rich_text field must be truncated to 2000 chars."""
        t = {**_BASE_TEMPLATE, 'meta_title': 'M' * 3000}
        with patch('framer_templates.http_post', return_value={}) as mock_post:
            ft.save_to_notion(t)
        props = mock_post.call_args[0][1]['properties']
        meta_val = props['Meta Title']['rich_text'][0]['text']['content']
        self.assertEqual(len(meta_val), 2000)

    def test_price_truncated_to_2000(self):
        """Price rich_text field must be truncated to 2000 chars."""
        t = {**_BASE_TEMPLATE, 'price': '$' * 3000}
        with patch('framer_templates.http_post', return_value={}) as mock_post:
            ft.save_to_notion(t)
        props = mock_post.call_args[0][1]['properties']
        price_val = props['Price']['rich_text'][0]['text']['content']
        self.assertEqual(len(price_val), 2000)


# ---------------------------------------------------------------------------
# _build_embed
# ---------------------------------------------------------------------------

_DISCORD_TEMPLATE = {
    'title': 'Test Template',
    'url': 'https://www.framer.com/marketplace/templates/test/',
    'author': 'Bob',
    'author_slug': 'bob-studio',
    'price': '$10',
    'thumbnail': '',
}


class TestBuildEmbed(unittest.TestCase):

    def test_basic_embed_structure(self):
        embed = ft._build_embed(_DISCORD_TEMPLATE)
        self.assertEqual(embed['title'], 'Test Template')
        self.assertEqual(embed['url'], 'https://www.framer.com/marketplace/templates/test/')
        self.assertEqual(embed['color'], 0x5865F2)

    def test_description_includes_author_link(self):
        embed = ft._build_embed(_DISCORD_TEMPLATE)
        self.assertIn('[Bob](https://www.framer.com/marketplace/profiles/bob-studio/)', embed['description'])
        self.assertIn('**$10**', embed['description'])

    def test_description_plain_author_when_no_slug(self):
        t = {**_DISCORD_TEMPLATE, 'author_slug': ''}
        embed = ft._build_embed(t)
        self.assertEqual(embed['description'], 'by Bob · **$10**')

    def test_includes_image_when_thumbnail_present(self):
        t = {**_DISCORD_TEMPLATE, 'thumbnail': 'https://cdn.example.com/img.jpg'}
        embed = ft._build_embed(t)
        self.assertIn('image', embed)
        self.assertEqual(embed['image']['url'], 'https://cdn.example.com/img.jpg')

    def test_no_image_key_when_thumbnail_absent(self):
        embed = ft._build_embed(_DISCORD_TEMPLATE)
        self.assertNotIn('image', embed)

    def test_description_includes_meta_title_when_present(self):
        t = {**_DISCORD_TEMPLATE, 'meta_title': 'Gym & Fitness Website'}
        embed = ft._build_embed(t)
        self.assertIn('Gym & Fitness Website', embed['description'])

    def test_description_excludes_meta_title_when_absent(self):
        embed = ft._build_embed(_DISCORD_TEMPLATE)
        lines = embed['description'].splitlines()
        self.assertEqual(len(lines), 1)

    def test_description_includes_demo_url_when_present(self):
        t = {**_DISCORD_TEMPLATE, 'demo_url': 'https://mysite.framer.website/'}
        embed = ft._build_embed(t)
        self.assertIn('[Live Demo](https://mysite.framer.website/)', embed['description'])

    def test_description_excludes_demo_url_when_absent(self):
        embed = ft._build_embed(_DISCORD_TEMPLATE)
        self.assertNotIn('Live Demo', embed['description'])

    def test_description_includes_meta_title_and_demo_url_together(self):
        t = {**_DISCORD_TEMPLATE, 'author_slug': '', 'price': 'Free',
             'meta_title': 'Portfolio Template', 'demo_url': 'https://demo.framer.website/'}
        embed = ft._build_embed(t)
        self.assertIn('Portfolio Template', embed['description'])
        self.assertIn('[Live Demo](https://demo.framer.website/)', embed['description'])

    def test_timestamp_set_when_published_at_present(self):
        t = {**_DISCORD_TEMPLATE, 'published_at': '2026-03-15T10:00:00Z'}
        embed = ft._build_embed(t)
        self.assertEqual(embed.get('timestamp'), '2026-03-15T10:00:00Z')

    def test_no_timestamp_key_when_published_at_absent(self):
        embed = ft._build_embed(_DISCORD_TEMPLATE)
        self.assertNotIn('timestamp', embed)

    def test_no_timestamp_key_when_published_at_empty(self):
        t = {**_DISCORD_TEMPLATE, 'published_at': ''}
        embed = ft._build_embed(t)
        self.assertNotIn('timestamp', embed)

    def test_description_includes_remixes_when_nonzero(self):
        t = {**_DISCORD_TEMPLATE, 'remixes': 5}
        embed = ft._build_embed(t)
        self.assertIn('5 remixes', embed['description'])

    def test_description_remix_singular_when_one(self):
        t = {**_DISCORD_TEMPLATE, 'remixes': 1}
        embed = ft._build_embed(t)
        self.assertIn('1 remix', embed['description'])
        self.assertNotIn('1 remixes', embed['description'])

    def test_description_excludes_remixes_when_zero(self):
        t = {**_DISCORD_TEMPLATE, 'remixes': 0}
        embed = ft._build_embed(t)
        self.assertNotIn('remix', embed['description'])

    def test_description_excludes_remixes_when_absent(self):
        embed = ft._build_embed(_DISCORD_TEMPLATE)
        self.assertNotIn('remix', embed['description'])


# ---------------------------------------------------------------------------
# _build_summary_embed
# ---------------------------------------------------------------------------

class TestBuildSummaryEmbed(unittest.TestCase):

    def test_singular_title_for_one_template(self):
        embed = ft._build_summary_embed([_template(title='Gym Pro')])
        self.assertEqual(embed['title'], '1 new Framer template')

    def test_plural_title_for_multiple_templates(self):
        templates = [_template(title='A', slug='a'), _template(title='B', slug='b')]
        embed = ft._build_summary_embed(templates)
        self.assertEqual(embed['title'], '2 new Framer templates')

    def test_description_contains_category_headers(self):
        templates = [
            _template(title='Gym Pro', slug='gym'),
            _template(title='My Portfolio', slug='port'),
        ]
        embed = ft._build_summary_embed(templates)
        self.assertIn('**Health & Fitness**', embed['description'])
        self.assertIn('**Portfolio & Creative**', embed['description'])

    def test_description_contains_template_links(self):
        t = _template(title='Cool Template', slug='cool', author='Alice', price='$29')
        embed = ft._build_summary_embed([t])
        self.assertIn('[Cool Template]', embed['description'])
        self.assertIn('by Alice', embed['description'])
        self.assertIn('$29', embed['description'])

    def test_color_matches_brand(self):
        embed = ft._build_summary_embed([_template()])
        self.assertEqual(embed['color'], 0x5865F2)

    def test_url_points_to_marketplace(self):
        embed = ft._build_summary_embed([_template()])
        self.assertIn('marketplace', embed['url'])

    def test_truncates_long_description(self):
        # 60 templates with long names should trigger truncation
        templates = [
            _template(title=f'Very Long Template Name Number {i} With Extra Words', slug=f's-{i}')
            for i in range(60)
        ]
        embed = ft._build_summary_embed(templates)
        self.assertLessEqual(len(embed['description']), 4096)
        self.assertIn('... and', embed['description'])

    def test_no_orphaned_category_header_when_truncation_fires_on_first_category_item(self):
        # Fill the description with Health & Fitness templates so that the very first
        # SaaS & Tech item triggers truncation.  The SaaS & Tech header must NOT appear
        # in the output because no items from that category were included.
        # We need enough Health & Fitness content to be near 3900 chars.
        # Each line is roughly ~80 chars; 50 items × 80 = ~4000 chars → truncation at ~48 items.
        health_templates = [
            _template(title=f'Fitness Studio Plan {i} Extra Long Name For Padding', slug=f'fit-{i}')
            for i in range(50)
        ]
        saas_templates = [
            _template(title='SaaS Dashboard Pro', slug='saas-1', meta_title='saas app')
        ]
        embed = ft._build_summary_embed(health_templates + saas_templates)
        # If truncation fired before any SaaS item, the SaaS header should not appear
        # alone (without any items) right before "... and N more".
        lines = embed['description'].splitlines()
        for i, line in enumerate(lines):
            if '... and' in line and i > 0:
                # The line before "... and N more" must not be a bare category header
                # (i.e. a line starting with "**" and ending with "**" with no items below)
                prev = lines[i - 1]
                self.assertFalse(
                    prev.startswith('**') and prev.endswith('**'),
                    f'Orphaned category header before truncation line: {prev!r}',
                )


# ---------------------------------------------------------------------------
# notify_discord_batch
# ---------------------------------------------------------------------------

class TestNotifyDiscordBatch(unittest.TestCase):

    def setUp(self):
        os.environ['DISCORD_WEBHOOK_URL_TEMPLATES'] = 'https://discord.com/api/webhooks/test'

    def test_single_template_summary_embed(self):
        with patch('framer_templates.http_post', return_value={}) as mock_post:
            ft.notify_discord_batch([_DISCORD_TEMPLATE])
        # 1 summary embed + 1 detail embed = 2 calls
        self.assertEqual(mock_post.call_count, 2)
        summary_payload = mock_post.call_args_list[0][0][1]
        self.assertIn('embeds', summary_payload)
        self.assertEqual(summary_payload['embeds'][0]['title'], '1 new Framer template')
        embed_payload = mock_post.call_args_list[1][0][1]
        self.assertEqual(len(embed_payload['embeds']), 1)

    def test_multiple_templates_summary_plural(self):
        templates = [{**_DISCORD_TEMPLATE, 'slug': f'slug-{i}', 'title': f'T{i}'} for i in range(3)]
        with patch('framer_templates.http_post', return_value={}) as mock_post:
            ft.notify_discord_batch(templates)
        # 1 summary + 3 embeds = 4 calls
        self.assertEqual(mock_post.call_count, 4)
        summary_payload = mock_post.call_args_list[0][0][1]
        self.assertIn('embeds', summary_payload)
        self.assertEqual(summary_payload['embeds'][0]['title'], '3 new Framer templates')
        for i in range(1, 4):
            self.assertEqual(len(mock_post.call_args_list[i][0][1]['embeds']), 1)

    def test_many_templates_sends_one_embed_per_message(self):
        templates = [{**_DISCORD_TEMPLATE, 'slug': f'slug-{i}', 'title': f'T{i}'} for i in range(12)]
        with patch('framer_templates.http_post', return_value={}) as mock_post:
            ft.notify_discord_batch(templates)
        # 1 summary + 12 individual embeds = 13 calls
        self.assertEqual(mock_post.call_count, 13)
        # First call is summary embed
        summary_payload = mock_post.call_args_list[0][0][1]
        self.assertIn('embeds', summary_payload)
        self.assertEqual(summary_payload['embeds'][0]['title'], '12 new Framer templates')
        # Each subsequent call has exactly 1 embed
        for i in range(1, 13):
            payload = mock_post.call_args_list[i][0][1]
            self.assertEqual(len(payload['embeds']), 1)

    def test_empty_list_does_nothing(self):
        with patch('framer_templates.http_post', return_value={}) as mock_post:
            ft.notify_discord_batch([])
        mock_post.assert_not_called()

    def test_error_in_one_message_does_not_stop_remaining(self):
        templates = [{**_DISCORD_TEMPLATE, 'slug': f'slug-{i}', 'title': f'T{i}'} for i in range(3)]
        # 4 calls total: summary + 3 embeds; first fails
        effects = [Exception('fail')] + [{}] * 3
        with patch('framer_templates.http_post', side_effect=effects) as mock_post:
            ft.notify_discord_batch(templates)  # must not raise
        self.assertEqual(mock_post.call_count, 4)

    def test_error_log_includes_label_in_context(self):
        """Failed notifications log the template title (or 'summary') in the error context."""
        import error_log as el
        with patch('framer_templates.http_post', side_effect=Exception('fail')), \
             patch.object(el, 'log_error') as mock_log:
            ft.notify_discord_batch([_DISCORD_TEMPLATE])
        # Should be called at least once (for the summary or embed failure)
        self.assertTrue(mock_log.called)
        # Find any call where the context includes 'label'
        contexts = [call[0][3] for call in mock_log.call_args_list if len(call[0]) >= 4]
        self.assertTrue(
            any('label' in (ctx or {}) for ctx in contexts),
            f"Expected 'label' key in at least one log_error context, got: {contexts}",
        )

    def test_error_log_label_is_title_for_summary_embed(self):
        """The summary embed payload failure logs the summary embed title as label."""
        import error_log as el
        # Only the summary message fails; subsequent embed calls succeed.
        with patch('framer_templates.http_post', side_effect=[Exception('fail'), {}]), \
             patch.object(el, 'log_error') as mock_log:
            ft.notify_discord_batch([_DISCORD_TEMPLATE])
        contexts = [call[0][3] for call in mock_log.call_args_list if len(call[0]) >= 4]
        labels = [(ctx or {}).get('label') for ctx in contexts]
        self.assertTrue(any('new Framer template' in (l or '') for l in labels))

    def test_error_log_label_is_title_for_embed_message(self):
        """An embed payload failure logs the template title as label."""
        import error_log as el
        t = {**_DISCORD_TEMPLATE, 'title': 'My Special Template'}
        # Summary succeeds; embed fails.
        with patch('framer_templates.http_post', side_effect=[{}, Exception('fail')]), \
             patch.object(el, 'log_error') as mock_log:
            ft.notify_discord_batch([t])
        contexts = [call[0][3] for call in mock_log.call_args_list if len(call[0]) >= 4]
        labels = [(ctx or {}).get('label') for ctx in contexts]
        self.assertIn('My Special Template', labels)

    def test_notify_discord_wrapper_calls_batch(self):
        with patch('framer_templates.http_post', return_value={}) as mock_post:
            ft.notify_discord(_DISCORD_TEMPLATE)
        # 1 summary embed + 1 detail embed = 2 calls
        self.assertEqual(mock_post.call_count, 2)
        summary_payload = mock_post.call_args_list[0][0][1]
        self.assertIn('embeds', summary_payload)


# ---------------------------------------------------------------------------
# main (integration)
# ---------------------------------------------------------------------------

_TEMPLATES = [
    {
        'slug': 'template-a', 'title': 'Template A',
        'url': 'https://www.framer.com/marketplace/templates/template-a/',
        'author': 'Alice', 'author_slug': '', 'price': 'Free', 'thumbnail': '', 'published_at': '',
    },
    {
        'slug': 'template-b', 'title': 'Template B',
        'url': 'https://www.framer.com/marketplace/templates/template-b/',
        'author': 'Bob', 'author_slug': '', 'price': '$10', 'thumbnail': '', 'published_at': '',
    },
]


class TestMain(unittest.TestCase):

    def setUp(self):
        os.environ['NOTION_TOKEN'] = 'test_token'
        os.environ['NOTION_DATABASE_ID'] = 'test_db_id'
        os.environ['DISCORD_WEBHOOK_URL_TEMPLATES'] = 'https://discord.com/api/webhooks/test'

    def _run(self, templates, seen_slugs):
        """Run main() with all I/O mocked; return (save_mock, notify_mock, x_mock)."""
        save_mock = MagicMock()
        notify_mock = MagicMock()
        x_mock = MagicMock()
        with patch('framer_templates.fetch_framer_templates', return_value=templates), \
             patch('framer_templates.get_seen_slugs', return_value=seen_slugs), \
             patch('framer_templates.save_to_notion', save_mock), \
             patch('framer_templates.notify_discord_batch', notify_mock), \
             patch('framer_templates.post_to_x', x_mock), \
             patch('framer_templates._warn_discord'), \
             patch('builtins.open', side_effect=FileNotFoundError):
            ft.main()
        return save_mock, notify_mock, x_mock

    def test_missing_env_var_raises_system_exit(self):
        del os.environ['NOTION_TOKEN']
        with patch('builtins.open', side_effect=FileNotFoundError):
            with self.assertRaises(SystemExit):
                ft.main()
        os.environ['NOTION_TOKEN'] = 'test_token'

    def test_no_new_templates_skips_save_and_notify(self):
        save, notify, x = self._run(_TEMPLATES, {'template-a', 'template-b'})
        save.assert_not_called()
        notify.assert_not_called()
        x.assert_not_called()

    def test_first_run_seeds_db_without_discord(self):
        # Empty seen_slugs → first run
        save, notify, x = self._run(_TEMPLATES, set())
        self.assertEqual(save.call_count, 2)
        notify.assert_not_called()
        x.assert_not_called()

    def test_normal_run_saves_and_notifies_only_new_templates(self):
        # template-a already seen; template-b is new
        save, notify, x = self._run(_TEMPLATES, {'template-a'})
        save.assert_called_once()
        self.assertEqual(save.call_args[0][0]['slug'], 'template-b')
        notify.assert_called_once()
        x.assert_called_once()

    def test_save_failure_continues_processing_remaining_templates(self):
        save_mock = MagicMock(side_effect=[Exception('Notion error'), None])
        notify_mock = MagicMock()
        with patch('framer_templates.fetch_framer_templates', return_value=_TEMPLATES), \
             patch('framer_templates.get_seen_slugs', return_value=set()), \
             patch('framer_templates.save_to_notion', save_mock), \
             patch('framer_templates.notify_discord_batch', notify_mock), \
             patch('framer_templates.post_to_x'), \
             patch('framer_templates._warn_discord'), \
             patch('builtins.open', side_effect=FileNotFoundError):
            ft.main()  # must not raise
        self.assertEqual(save_mock.call_count, 2)

    def test_http_error_on_save_logs_notion_response_body(self):
        """When save_to_notion raises an HTTPError, notion_response must appear in the error log."""
        import error_log as el
        import io
        http_err = urllib.error.HTTPError(
            None, 400, 'Bad Request', {},
            io.BytesIO(b'{"object":"error","message":"Invalid property"}'),
        )
        save_mock = MagicMock(side_effect=http_err)
        with patch('framer_templates.fetch_framer_templates', return_value=[_TEMPLATES[0]]), \
             patch('framer_templates.get_seen_slugs', return_value=set()), \
             patch('framer_templates.save_to_notion', save_mock), \
             patch('framer_templates.notify_discord_batch'), \
             patch('framer_templates.post_to_x'), \
             patch('framer_templates._warn_discord'), \
             patch.object(el, 'log_error') as mock_log, \
             patch('builtins.open', side_effect=FileNotFoundError):
            ft.main()
        http_error_calls = [
            c for c in mock_log.call_args_list
            if len(c[0]) >= 4 and isinstance(c[0][3], dict) and 'notion_response' in c[0][3]
        ]
        self.assertTrue(http_error_calls, 'Expected log_error call with notion_response in context')
        ctx = http_error_calls[0][0][3]
        self.assertIn('Invalid property', ctx['notion_response'])

    def test_http_error_on_save_continues_to_next_template(self):
        """An HTTPError saving one template must not abort processing the remaining templates."""
        import io
        http_err = urllib.error.HTTPError(None, 400, 'Bad Request', {}, io.BytesIO(b'{}'))
        save_mock = MagicMock(side_effect=[http_err, None])
        notify_mock = MagicMock()
        with patch('framer_templates.fetch_framer_templates', return_value=_TEMPLATES), \
             patch('framer_templates.get_seen_slugs', return_value=set()), \
             patch('framer_templates.save_to_notion', save_mock), \
             patch('framer_templates.notify_discord_batch', notify_mock), \
             patch('framer_templates.post_to_x'), \
             patch('framer_templates._warn_discord'), \
             patch('builtins.open', side_effect=FileNotFoundError):
            ft.main()  # must not raise
        self.assertEqual(save_mock.call_count, 2)

    def test_warns_discord_when_fewer_than_five_templates(self):
        few = _TEMPLATES[:2]  # 2 templates < 5
        with patch('framer_templates.fetch_framer_templates', return_value=few), \
             patch('framer_templates.get_seen_slugs', return_value=set()), \
             patch('framer_templates.save_to_notion'), \
             patch('framer_templates.notify_discord_batch'), \
             patch('framer_templates.post_to_x'), \
             patch('framer_templates._warn_discord') as warn_mock, \
             patch('builtins.open', side_effect=FileNotFoundError):
            ft.main()
        warn_mock.assert_called_once()
        self.assertIn('WARNING', warn_mock.call_args[0][0])

    def test_no_discord_warning_when_five_or_more_templates(self):
        five = [
            {'slug': f'slug-{i}', 'title': f'T{i}', 'url': f'url{i}',
             'author': 'A', 'author_slug': '', 'price': 'Free', 'thumbnail': '', 'published_at': ''}
            for i in range(5)
        ]
        with patch('framer_templates.fetch_framer_templates', return_value=five), \
             patch('framer_templates.get_seen_slugs', return_value=set()), \
             patch('framer_templates.save_to_notion'), \
             patch('framer_templates.notify_discord_batch'), \
             patch('framer_templates.post_to_x'), \
             patch('framer_templates._warn_discord') as warn_mock, \
             patch('builtins.open', side_effect=FileNotFoundError):
            ft.main()
        warn_mock.assert_not_called()

    def test_fetch_failure_sends_discord_error_alert(self):
        """When fetch_framer_templates raises, a Discord error alert must be sent."""
        with patch('framer_templates.fetch_framer_templates',
                   side_effect=Exception('network error')), \
             patch('framer_templates._warn_discord') as warn_mock, \
             patch('builtins.open', side_effect=FileNotFoundError):
            with self.assertRaises(SystemExit):
                ft.main()
        warn_mock.assert_called_once()
        self.assertIn('ERROR', warn_mock.call_args[0][0])

    def test_fetch_failure_logs_error(self):
        """When fetch_framer_templates raises, an error must be written to the error log."""
        import error_log as el
        with patch('framer_templates.fetch_framer_templates',
                   side_effect=Exception('connection refused')), \
             patch('framer_templates._warn_discord'), \
             patch.object(el, 'log_error') as mock_log, \
             patch('builtins.open', side_effect=FileNotFoundError):
            with self.assertRaises(SystemExit):
                ft.main()
        self.assertTrue(mock_log.called)
        severity = mock_log.call_args[0][1]
        self.assertEqual(severity, 'error')

    def test_fetch_failure_exits_with_nonzero(self):
        """When fetch_framer_templates raises, main() must exit with a non-zero code."""
        with patch('framer_templates.fetch_framer_templates',
                   side_effect=urllib.error.URLError('connection refused')), \
             patch('framer_templates._warn_discord'), \
             patch('builtins.open', side_effect=FileNotFoundError):
            with self.assertRaises(SystemExit) as ctx:
                ft.main()
        self.assertNotEqual(ctx.exception.code, 0)

    def test_fetch_failure_does_not_call_get_seen_slugs(self):
        """When fetch_framer_templates raises, get_seen_slugs must not be called."""
        with patch('framer_templates.fetch_framer_templates',
                   side_effect=Exception('fetch error')), \
             patch('framer_templates._warn_discord'), \
             patch('framer_templates.get_seen_slugs') as mock_slugs, \
             patch('builtins.open', side_effect=FileNotFoundError):
            with self.assertRaises(SystemExit):
                ft.main()
        mock_slugs.assert_not_called()


# ---------------------------------------------------------------------------
# _warn_discord
# ---------------------------------------------------------------------------

class TestWarnDiscord(unittest.TestCase):

    def setUp(self):
        os.environ['DISCORD_ALERTS_WEBHOOK_URL'] = 'https://discord.com/api/webhooks/test-alerts'

    def tearDown(self):
        os.environ.pop('DISCORD_ALERTS_WEBHOOK_URL', None)

    def test_posts_content_message_to_alerts_webhook(self):
        with patch('framer_templates.http_post', return_value={}) as mock_post:
            ft._warn_discord('test warning message')
        mock_post.assert_called_once()
        url, payload = mock_post.call_args[0]
        self.assertIn('test-alerts', url)
        self.assertIn('content', payload)
        self.assertIn('test warning message', payload['content'])

    def test_uses_alerts_webhook_not_data_webhook(self):
        os.environ['DISCORD_WEBHOOK_URL_TEMPLATES'] = 'https://discord.com/api/webhooks/data'
        with patch('framer_templates.http_post', return_value={}) as mock_post:
            ft._warn_discord('alert')
        posted_url = mock_post.call_args[0][0]
        self.assertIn('test-alerts', posted_url)
        self.assertNotIn('data', posted_url)

    def test_exception_is_caught_and_does_not_propagate(self):
        with patch('framer_templates.http_post', side_effect=Exception('network error')):
            ft._warn_discord('msg')  # must not raise

    def test_no_op_when_env_var_missing(self):
        """_warn_discord must not raise when DISCORD_ALERTS_WEBHOOK_URL is unset."""
        os.environ.pop('DISCORD_ALERTS_WEBHOOK_URL', None)
        with patch('framer_templates.http_post') as mock_post:
            ft._warn_discord('msg')  # must not raise
        mock_post.assert_not_called()


# ---------------------------------------------------------------------------
# _write_summary
# ---------------------------------------------------------------------------

class TestWriteSummary(unittest.TestCase):

    def tearDown(self):
        os.environ.pop('GITHUB_STEP_SUMMARY', None)

    def test_writes_to_file_when_env_set(self):
        with patch.dict('os.environ', {'GITHUB_STEP_SUMMARY': '/tmp/summary.md'}), \
             patch('builtins.open', mock_open()) as m:
            ft._write_summary('## Framer Monitor\nhello')
        m.assert_called_once_with('/tmp/summary.md', 'a')
        m().write.assert_called_once_with('## Framer Monitor\nhello\n')

    def test_no_op_when_env_not_set(self):
        os.environ.pop('GITHUB_STEP_SUMMARY', None)
        with patch('builtins.open') as m:
            ft._write_summary('ignored')
        m.assert_not_called()

    def test_main_writes_summary_when_nothing_new(self):
        template = {'slug': 's1', 'title': 'T1', 'url': 'u1', 'author': 'A',
                    'author_slug': '', 'price': 'Free', 'thumbnail': '', 'published_at': ''}
        with patch.dict('os.environ', {'NOTION_TOKEN': 'ntn_x', 'NOTION_DATABASE_ID': 'db',
                                       'DISCORD_WEBHOOK_URL_TEMPLATES': 'https://h.com/w'}), \
             patch('framer_templates.fetch_framer_templates', return_value=[template]), \
             patch('framer_templates.get_seen_slugs', return_value={'s1'}), \
             patch('framer_templates._warn_discord'), \
             patch('framer_templates._write_summary') as mock_summary, \
             patch('builtins.open', side_effect=FileNotFoundError):
            ft.main()
        mock_summary.assert_called_once()
        self.assertIn('No new templates', mock_summary.call_args[0][0])

    def test_main_writes_summary_on_first_run(self):
        templates = [
            {'slug': f's{i}', 'title': f'T{i}', 'url': f'u{i}', 'author': 'A',
             'author_slug': '', 'price': 'Free', 'thumbnail': '', 'published_at': ''}
            for i in range(5)
        ]
        with patch.dict('os.environ', {'NOTION_TOKEN': 'ntn_x', 'NOTION_DATABASE_ID': 'db',
                                       'DISCORD_WEBHOOK_URL_TEMPLATES': 'https://h.com/w'}), \
             patch('framer_templates.fetch_framer_templates', return_value=templates), \
             patch('framer_templates.get_seen_slugs', return_value=set()), \
             patch('framer_templates._warn_discord'), \
             patch('framer_templates.save_to_notion'), \
             patch('framer_templates.notify_discord_batch'), \
             patch('framer_templates.post_to_x'), \
             patch('framer_templates._write_summary') as mock_summary, \
             patch('builtins.open', side_effect=FileNotFoundError):
            ft.main()
        mock_summary.assert_called_once()
        self.assertIn('First run', mock_summary.call_args[0][0])

    def test_main_writes_summary_on_new_templates(self):
        existing = {'slug': 'old', 'title': 'Old', 'url': 'u0', 'author': 'A',
                    'author_slug': '', 'price': 'Free', 'thumbnail': '', 'published_at': ''}
        new = {'slug': 'new', 'title': 'New', 'url': 'u1', 'author': 'A',
               'author_slug': '', 'price': 'Free', 'thumbnail': '', 'published_at': ''}
        with patch.dict('os.environ', {'NOTION_TOKEN': 'ntn_x', 'NOTION_DATABASE_ID': 'db',
                                       'DISCORD_WEBHOOK_URL_TEMPLATES': 'https://h.com/w'}), \
             patch('framer_templates.fetch_framer_templates', return_value=[existing, new]), \
             patch('framer_templates.get_seen_slugs', return_value={'old'}), \
             patch('framer_templates._warn_discord'), \
             patch('framer_templates.save_to_notion'), \
             patch('framer_templates.notify_discord_batch'), \
             patch('framer_templates.post_to_x'), \
             patch('framer_templates._write_summary') as mock_summary, \
             patch('builtins.open', side_effect=FileNotFoundError):
            ft.main()
        mock_summary.assert_called_once()
        summary_text = mock_summary.call_args[0][0]
        self.assertIn('1 new template', summary_text)
        self.assertIn('already tracked', summary_text)


# ---------------------------------------------------------------------------
# _build_tweet_text
# ---------------------------------------------------------------------------

class TestBuildTweetText(unittest.TestCase):

    def test_within_280_chars(self):
        templates = [_template(title=f'Template {i}', slug=f's{i}') for i in range(5)]
        text = ft._build_tweet_text(templates)
        self.assertLessEqual(len(text), 280)

    def test_contains_marketplace_link(self):
        text = ft._build_tweet_text([_template(title='Gym Pro')])
        self.assertIn('framer.com/marketplace', text)

    def test_contains_category_summary(self):
        text = ft._build_tweet_text([_template(title='Gym Pro')])
        self.assertIn('Health & Fitness', text)

    def test_singular_for_one_template(self):
        text = ft._build_tweet_text([_template()])
        self.assertIn('1 new Framer template ', text)
        # Intro line should use singular (not "templates")
        intro = text.split('\n')[0]
        self.assertNotIn('templates', intro)

    def test_plural_for_multiple_templates(self):
        text = ft._build_tweet_text([_template(slug='a'), _template(slug='b')])
        self.assertIn('templates', text)

    def test_truncation_with_many_templates(self):
        templates = [_template(title=f'A Very Long Template Name {i}', slug=f's{i}', price='$99')
                     for i in range(20)]
        text = ft._build_tweet_text(templates)
        self.assertLessEqual(len(text), 280)

    def test_fallback_when_no_category_match(self):
        text = ft._build_tweet_text([_template(title='Abstract Minimal')])
        self.assertIn('just dropped', text)


# ---------------------------------------------------------------------------
# _oauth1_header
# ---------------------------------------------------------------------------

class TestOAuth1Header(unittest.TestCase):

    def test_header_format(self):
        header = ft._oauth1_header(
            'POST', 'https://api.twitter.com/2/tweets', {},
            'key', 'secret', 'token', 'token_secret',
            nonce='testnonce', timestamp='1234567890',
        )
        self.assertTrue(header.startswith('OAuth '))
        self.assertIn('oauth_consumer_key="key"', header)
        self.assertIn('oauth_token="token"', header)
        self.assertIn('oauth_signature_method="HMAC-SHA1"', header)
        self.assertIn('oauth_signature=', header)

    def test_deterministic_with_fixed_nonce_and_timestamp(self):
        args = ('POST', 'https://api.twitter.com/2/tweets', {},
                'ck', 'cs', 'at', 'ats')
        kwargs = {'nonce': 'fixed', 'timestamp': '9999'}
        h1 = ft._oauth1_header(*args, **kwargs)
        h2 = ft._oauth1_header(*args, **kwargs)
        self.assertEqual(h1, h2)


# ---------------------------------------------------------------------------
# post_to_x
# ---------------------------------------------------------------------------

class TestPostToX(unittest.TestCase):

    _CRED_ENV = {
        'TWITTER_API_KEY': 'ck',
        'TWITTER_API_SECRET': 'cs',
        'TWITTER_ACCESS_TOKEN': 'at',
        'TWITTER_ACCESS_TOKEN_SECRET': 'ats',
    }

    def test_skips_silently_when_no_credentials(self):
        for k in self._CRED_ENV:
            os.environ.pop(k, None)
        with patch('framer_templates.http_post') as mock_post:
            ft.post_to_x([_template()])
        mock_post.assert_not_called()

    def test_skips_when_partial_credentials(self):
        os.environ['TWITTER_API_KEY'] = 'ck'
        for k in list(self._CRED_ENV)[1:]:
            os.environ.pop(k, None)
        with patch('framer_templates.http_post') as mock_post:
            ft.post_to_x([_template()])
        mock_post.assert_not_called()
        os.environ.pop('TWITTER_API_KEY', None)

    def test_calls_twitter_api_when_credentials_present(self):
        with patch.dict('os.environ', self._CRED_ENV), \
             patch('framer_templates.http_post', return_value={}) as mock_post:
            ft.post_to_x([_template(title='Gym Pro')])
        mock_post.assert_called_once()
        url = mock_post.call_args[0][0]
        self.assertIn('api.twitter.com', url)

    def test_error_does_not_propagate(self):
        with patch.dict('os.environ', self._CRED_ENV), \
             patch('framer_templates.http_post', side_effect=Exception('network')):
            ft.post_to_x([_template()])  # must not raise


class TestHttpRetry(unittest.TestCase):
    """_retry backs off and re-raises on transient HTTP errors."""

    def test_retries_on_502_then_succeeds(self):
        call_count = [0]

        def flaky():
            call_count[0] += 1
            if call_count[0] < 3:
                raise urllib.error.HTTPError(None, 502, 'Bad Gateway', {}, None)
            return 'ok'

        with patch('time.sleep'):
            result = ft._retry(flaky)
        self.assertEqual(result, 'ok')
        self.assertEqual(call_count[0], 3)

    def test_raises_after_max_attempts(self):
        err = urllib.error.HTTPError(None, 502, 'Bad Gateway', {}, None)
        with patch('time.sleep'), \
             self.assertRaises(urllib.error.HTTPError):
            ft._retry(lambda: (_ for _ in ()).throw(err))

    def test_does_not_retry_on_404(self):
        call_count = [0]

        def fn():
            call_count[0] += 1
            raise urllib.error.HTTPError(None, 404, 'Not Found', {}, None)

        with self.assertRaises(urllib.error.HTTPError):
            ft._retry(fn)
        self.assertEqual(call_count[0], 1)

    def test_retries_on_url_error(self):
        call_count = [0]

        def flaky():
            call_count[0] += 1
            if call_count[0] < 2:
                raise urllib.error.URLError('connection refused')
            return 'ok'

        with patch('time.sleep'):
            result = ft._retry(flaky)
        self.assertEqual(result, 'ok')
        self.assertEqual(call_count[0], 2)


if __name__ == '__main__':
    unittest.main()
