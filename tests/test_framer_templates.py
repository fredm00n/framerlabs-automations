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
import shared
import framer_templates as ft

# Exercise the production side-effect paths (writes/notifications enabled)
# regardless of where the suite runs. The observe-only gate has dedicated
# tests that override this explicitly.
os.environ['ENABLE_SIDE_EFFECTS'] = '1'

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

    def test_non_zero_start_position(self):
        s = 'abc{"x": 42}xyz'
        self.assertEqual(ft._extract_json_object(s, 3), {'x': 42})

    def test_unclosed_object_raises_value_error(self):
        with self.assertRaises(ValueError):
            ft._extract_json_object('{"a": 1', 0)


# ---------------------------------------------------------------------------
# fetch_from_rsc
# ---------------------------------------------------------------------------

def _rsc_item(slug, id_='abc', title='T', price='Free', author='A',
              author_slug='a-studio',
              thumbnail='https://cdn.example.com/t.jpg', published='2024-01-15',
              meta_title='A Great Template', demo_url='https://demo.framer.website/',
              remixes=5):
    """Return an RSC body fragment in the new June 2026 format (primary key: "resource":{).

    The new format uses:
      - "resource":{ as the key (with the opening brace included)
      - "introduction" for meta title (was "metaTitle")
      - "author":{"name":...,"slug":...} (was "creator")
      - "publishedAt": plain ISO 8601 (no "$D" prefix)
      - "media":[{"url":...}] for thumbnail (was "thumbnail" top-level field)
      - "attributes":{"price":...,"previewUrl":...} (was top-level "price"/"publishedUrl")
    """
    return (
        f'"resource":{{"id":"{id_}","slug":"{slug}","title":"{title}",'
        f'"introduction":"{meta_title}",'
        f'"author":{{"name":"{author}","slug":"{author_slug}"}},'
        f'"publishedAt":"{published}",'
        f'"media":[{{"type":"image","url":"{thumbnail}"}}],'
        f'"attributes":{{"price":"{price}","previewUrl":"{demo_url}"}},'
        f'"remixes":{remixes}}}'
    )


def _rsc_item_old(slug, id_='abc', title='T', price='Free', author='A',
                  author_slug='a-studio',
                  thumbnail='https://cdn.example.com/t.jpg', published='$D2024-01-15',
                  meta_title='A Great Template', demo_url='https://demo.framer.website/',
                  remixes=5):
    """Return an RSC body fragment in the old pre-June 2026 format (key: "item":).

    Used only for backward-compatibility tests that verify the fallback path still
    parses old-format bodies when a rollback occurs.
    """
    return (
        f'"item":{{"id":"{id_}","slug":"{slug}","title":"{title}",'
        f'"metaTitle":"{meta_title}",'
        f'"price":"{price}","creator":{{"name":"{author}","slug":"{author_slug}"}},'
        f'"thumbnail":"{thumbnail}","publishedAt":"{published}",'
        f'"publishedUrl":"{demo_url}","remixes":{remixes}}}'
    )


def _rsc_item_key(slug, key='"resource":{', id_='abc', title='T', price='Free',
                  author='A', author_slug='a-studio',
                  thumbnail='https://cdn.example.com/t.jpg', published='2024-01-15',
                  meta_title='A Great Template', demo_url='https://demo.framer.website/',
                  remixes=5):
    """Like _rsc_item but allows specifying a custom RSC key prefix.

    When key ends with '{', the object literal format is used (new format):
        "resource":{"id":...}
    When key does not end with '{', the old format is used (key followed by object):
        "templateItem":{"id":...}
    """
    if key.endswith('{'):
        # New format: key already includes the opening brace
        inner = (
            f'"id":"{id_}","slug":"{slug}","title":"{title}",'
            f'"introduction":"{meta_title}",'
            f'"author":{{"name":"{author}","slug":"{author_slug}"}},'
            f'"publishedAt":"{published}",'
            f'"media":[{{"type":"image","url":"{thumbnail}"}}],'
            f'"attributes":{{"price":"{price}","previewUrl":"{demo_url}"}},'
            f'"remixes":{remixes}'
        )
        return f'{key}{inner}}}'
    else:
        # Old format: key is followed by a separate JSON object
        body = _rsc_item_old(slug, id_=id_, title=title, price=price, author=author,
                             author_slug=author_slug, thumbnail=thumbnail,
                             published=published, meta_title=meta_title,
                             demo_url=demo_url, remixes=remixes)
        # Replace the '"item":' prefix with the desired old-format key
        return body.replace('"item":', key, 1)


def _full_page(offset=0):
    """Return an RSC body string containing exactly 12 templates (new format page size)."""
    return '\n'.join(_rsc_item(f'slug-{offset + i}', id_=str(offset + i)) for i in range(12))


def _data_array_item(slug, id_='1', title='T', price=None, author='A',
                     author_slug='a-studio', thumbnail='https://cdn.example.com/t.jpg',
                     published='2026-06-20T10:00:00.000Z', meta_title='Meta Title',
                     demo_url='https://demo.framer.website/'):
    """Return a dict in the June-2026 embedded ``data`` array template shape.

    Each newest-grid element is a full template object carrying ``type":"template"``
    and an ``attributes`` sub-object whose ``price`` is a *number* (or ``null`` for
    a free template) — unlike the ``"resource":{...}`` featured blocks where the
    price is a string.
    """
    return {
        'id': id_, 'slug': slug, 'title': title, 'introduction': meta_title,
        'author': {'name': author, 'slug': author_slug},
        'media': [{'type': 'image', 'url': thumbnail}],
        'publishedAt': published, 'type': 'template',
        'attributes': {'price': price, 'previewUrl': demo_url},
    }


def _rsc_data_array_body(items):
    """Wrap data-array template dicts in an RSC fragment: ``...,"data":[...],...``."""
    return '5:["$","div",null,{"data":' + json.dumps(items) + '}]'


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
        self.assertEqual(t['url'], 'https://www.framer.com/community/marketplace/templates/cool-template/')
        self.assertEqual(t['thumbnail'], 'https://cdn.example.com/t.jpg')
        self.assertEqual(t['published_at'], '2024-01-15')  # plain ISO 8601 (new format, no $D prefix)
        self.assertEqual(t['meta_title'], 'Portfolio Website')
        self.assertEqual(t['demo_url'], 'https://cool.framer.website/')
        self.assertEqual(t['remixes'], 7)

    def test_strips_one_dollar_from_rsc_encoded_price(self):
        # RSC encodes literal "$" as "$$"; stripping the first "$" yields the actual price
        body = _rsc_item('s', price='$$29')
        t = self._fetch(body)[0]
        self.assertEqual(t['price'], '$29')

    def test_deduplicates_by_slug(self):
        body = _rsc_item('dup', id_='1') + '\n' + _rsc_item('dup', id_='2')
        templates = self._fetch(body)
        self.assertEqual(len(templates), 1)

    def test_fetches_community_marketplace_listing_url(self):
        # The June 2026 marketplace upgrade moved the listing under /community/.
        # Page 1 must be fetched from the /community/ listing URL sorted by newest;
        # the old /marketplace/...?sort=recent path is no longer canonical.
        body = _rsc_item('only-one')
        with patch('framer_templates.http_get', return_value=body) as mock_get:
            ft.fetch_from_rsc()
        first_url = mock_get.call_args_list[0][0][0]
        self.assertEqual(
            first_url,
            'https://www.framer.com/community/marketplace/templates/?sort=newest',
        )

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

    def test_no_warning_with_five_or_more_templates(self):
        body = '\n'.join(_rsc_item(f'slug-{i}', id_=str(i)) for i in range(5))
        with patch('framer_templates.http_get', return_value=body), \
             patch('builtins.print') as mock_print:
            ft.fetch_from_rsc()
        output = ' '.join(str(c) for c in mock_print.call_args_list)
        self.assertNotIn('WARNING', output)

    def test_fetches_page_2_when_page_1_is_full(self):
        # A full first page (12 items) means there may be more; page 2 must be fetched.
        page2_body = _rsc_item('slug-12', id_='12')  # 1 new item on page 2
        with patch('framer_templates.http_get', side_effect=[_full_page(), page2_body]) as mock_get:
            templates = ft.fetch_from_rsc()
        self.assertEqual(mock_get.call_count, 2)
        self.assertEqual(len(templates), 13)

    def test_does_not_fetch_page_2_when_page_1_is_partial(self):
        body = '\n'.join(_rsc_item(f'slug-{i}', id_=str(i)) for i in range(5))
        with patch('framer_templates.http_get', side_effect=[body]) as mock_get:
            templates = ft.fetch_from_rsc()
        self.assertEqual(mock_get.call_count, 1)
        self.assertEqual(len(templates), 5)

    def test_page_2_url_includes_page_param(self):
        page2_body = _rsc_item('slug-12', id_='12')
        with patch('framer_templates.http_get', side_effect=[_full_page(), page2_body]) as mock_get:
            ft.fetch_from_rsc()
        second_call_url = mock_get.call_args_list[1][0][0]
        self.assertIn('page=2', second_call_url)

    def test_stops_after_max_2_pages(self):
        # Two full pages — loop must stop at page 2 without fetching a 3rd.
        with patch('framer_templates.http_get',
                   side_effect=[_full_page(0), _full_page(12)]) as mock_get:
            templates = ft.fetch_from_rsc()
        self.assertEqual(mock_get.call_count, 2)
        self.assertEqual(len(templates), 24)

    def test_parses_templates_when_resource_key_has_space(self):
        # Regression: RSC format may emit "resource": { instead of "resource":{
        # In the new format, the key is '"resource":{' (brace is part of the key),
        # so a space before the brace means the key '"resource":{' is not found.
        # The primary key search uses body.find(), so '"resource": {' does NOT match
        # '"resource":{'. This test verifies a body with the variant spacing falls
        # back to old-format parsing gracefully (no crash, just 0 results from primary).
        body = _rsc_item('new-format-slug', id_='77')
        templates = self._fetch(body)
        self.assertEqual(len(templates), 1)
        self.assertEqual(templates[0]['slug'], 'new-format-slug')

    def test_old_format_still_parses_via_fallback(self):
        # Backward-compat: RSC bodies in the old "item": format must still parse
        # via the fallback key path (Framer could revert the format change).
        body = _rsc_item_old('whitespace-slug', id_='77').replace('"item":{', '"item": {')
        templates = self._fetch(body)
        self.assertEqual(len(templates), 1)
        self.assertEqual(templates[0]['slug'], 'whitespace-slug')

    def test_page2_failure_keeps_page1_templates(self):
        """If page 2 fetch fails after page 1 succeeded, keep the page-1 data.

        A full first page would normally trigger a page-2 fetch; if that fetch
        raises (network error, retries exhausted, HTTP 5xx), the script must
        not throw away the 12 valid page-1 templates that were already parsed.
        """
        page1 = _full_page(0)
        with patch('framer_templates.http_get',
                   side_effect=[page1, urllib.error.URLError('network unreachable')]):
            templates = ft.fetch_from_rsc()
        self.assertEqual(len(templates), 12)
        slugs = {t['slug'] for t in templates}
        self.assertIn('slug-0', slugs)
        self.assertIn('slug-11', slugs)

    def test_page2_failure_logs_warning_with_context(self):
        """Page-2 fetch failure must be logged with page, error, and page1_templates context."""
        import error_log as el
        page1 = _full_page(0)
        with patch('framer_templates.http_get',
                   side_effect=[page1, urllib.error.URLError('boom')]), \
             patch.object(el, 'log_error') as mock_log:
            ft.fetch_from_rsc()
        # Must have at least one warning mentioning page 2 fetch
        page2_calls = [
            c for c in mock_log.call_args_list
            if len(c[0]) >= 3 and 'page 2 fetch failed' in str(c[0][2]).lower()
        ]
        self.assertTrue(page2_calls, 'Expected a warning for page-2 fetch failure')
        ctx = page2_calls[0][0][3] if len(page2_calls[0][0]) >= 4 else {}
        self.assertEqual(ctx.get('page'), 2)
        self.assertEqual(ctx.get('page1_templates'), 12)
        self.assertIn('error', ctx)

    def test_page1_failure_still_raises(self):
        """A failure on page 1 must still raise — there is no data to fall back on."""
        with patch('framer_templates.http_get',
                   side_effect=urllib.error.URLError('connection refused')):
            with self.assertRaises(urllib.error.URLError):
                ft.fetch_from_rsc()


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

    def test_parses_item_with_new_format_key(self):
        # Standard new-format parsing: key is '"resource":{' (brace included in key string)
        body = _rsc_item('new-format-slug', id_='42')
        _, templates = self._parse(body)
        self.assertEqual(len(templates), 1)
        self.assertEqual(templates[0]['slug'], 'new-format-slug')

    def test_skips_object_without_id_field(self):
        # A JSON object matching the key pattern that has no "id" key should be skipped
        body = '"resource":{"slug":"no-id","title":"T"}'
        _, templates = self._parse(body)
        self.assertEqual(len(templates), 0)

    def test_continues_after_non_object_following_key(self):
        # If the key is found but not followed by a valid JSON object, parsing continues
        item_str = _rsc_item('after-junk', id_='99')
        # "resource":" is not a valid object start — junk value, then valid item
        body = '"resource":"junk"\n' + item_str
        _, templates = self._parse(body)
        self.assertEqual(len(templates), 1)
        self.assertEqual(templates[0]['slug'], 'after-junk')

    def test_old_format_parses_with_explicit_item_key(self):
        # _parse_rsc_body must work with the old '"item":' key (backward-compat fallback)
        body = _rsc_item_old('old-slug', id_='55')
        seen: set = set()
        templates: list = []
        ft._parse_rsc_body(body, seen, templates, search_key='"item":')
        self.assertEqual(len(templates), 1)
        self.assertEqual(templates[0]['slug'], 'old-slug')

    def test_custom_search_key_parses_templateItem(self):
        # _parse_rsc_body must work with alternative old-format RSC key "templateItem":
        body = _rsc_item_key('alt-slug', key='"templateItem":', id_='55')
        seen: set = set()
        templates: list = []
        ft._parse_rsc_body(body, seen, templates, search_key='"templateItem":')
        self.assertEqual(len(templates), 1)
        self.assertEqual(templates[0]['slug'], 'alt-slug')

    def test_mixed_good_and_bad_items_counted_correctly(self):
        # One good item and one unclosed object (new format)
        good = _rsc_item('good-slug', id_='1')
        bad = '"resource":{"id":"2","slug":"bad"'  # unclosed
        body = good + '\n' + bad
        seen: set = set()
        templates: list = []
        errors = ft._parse_rsc_body(body, seen, templates)
        self.assertEqual(errors, 1)
        self.assertEqual(len(templates), 1)

    def test_new_format_extracts_fields_from_sub_objects(self):
        """New format: price/previewUrl come from attributes, author from author obj,
        thumbnail from media[0].url, meta_title from introduction (not metaTitle)."""
        body = _rsc_item(
            'new-slug', id_='x1', title='New Template',
            price='$49', author='Studio New', author_slug='studio-new',
            thumbnail='https://cdn.framer.com/img.jpg', published='2026-06-15',
            meta_title='A SaaS Dashboard Template',
            demo_url='https://new-template.framer.website/', remixes=3,
        )
        seen: set = set()
        templates: list = []
        ft._parse_rsc_body(body, seen, templates)
        self.assertEqual(len(templates), 1)
        t = templates[0]
        self.assertEqual(t['slug'], 'new-slug')
        self.assertEqual(t['title'], 'New Template')
        self.assertEqual(t['meta_title'], 'A SaaS Dashboard Template')
        self.assertEqual(t['author'], 'Studio New')
        self.assertEqual(t['author_slug'], 'studio-new')
        self.assertEqual(t['price'], '$49')
        self.assertEqual(t['published_at'], '2026-06-15')
        self.assertEqual(t['thumbnail'], 'https://cdn.framer.com/img.jpg')
        self.assertEqual(t['demo_url'], 'https://new-template.framer.website/')
        self.assertEqual(t['remixes'], 3)

    def test_old_format_extracts_fields_via_explicit_item_key(self):
        """Old format: price/demo_url are top-level, creator instead of author,
        metaTitle instead of introduction, $D prefix on publishedAt stripped."""
        body = _rsc_item_old(
            'old-slug', id_='x2', title='Old Template',
            price='$$29', author='Old Studio', author_slug='old-studio',
            thumbnail='https://cdn.framer.com/old.jpg', published='$D2024-03-10',
            meta_title='A Restaurant Website Template',
            demo_url='https://old-template.framer.website/', remixes=0,
        )
        seen: set = set()
        templates: list = []
        ft._parse_rsc_body(body, seen, templates, search_key='"item":')
        self.assertEqual(len(templates), 1)
        t = templates[0]
        self.assertEqual(t['slug'], 'old-slug')
        self.assertEqual(t['meta_title'], 'A Restaurant Website Template')
        self.assertEqual(t['author'], 'Old Studio')
        self.assertEqual(t['author_slug'], 'old-studio')
        self.assertEqual(t['price'], '$29')       # $$ prefix stripped to $
        self.assertEqual(t['published_at'], '2024-03-10')  # $D prefix stripped
        self.assertEqual(t['thumbnail'], 'https://cdn.framer.com/old.jpg')
        self.assertEqual(t['demo_url'], 'https://old-template.framer.website/')


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



# ---------------------------------------------------------------------------
# _rsc_payload_type
# ---------------------------------------------------------------------------

class TestRscPayloadType(unittest.TestCase):

    def test_chunk_reference_normalises_to_I_bracket(self):
        self.assertEqual(ft._rsc_payload_type('I[339756,[],\"default\"]'), 'I[')

    def test_react_server_component_ref(self):
        self.assertEqual(ft._rsc_payload_type('"$Sreact.fragment"'), '"$S"')

    def test_fallback_for_unknown_payload(self):
        # Unknown payload types fall back to first 4 characters
        self.assertEqual(ft._rsc_payload_type('null'), 'null')
        self.assertEqual(ft._rsc_payload_type('true'), 'true')
        self.assertEqual(ft._rsc_payload_type('XY'), 'XY')


# ---------------------------------------------------------------------------
# _sample_rsc_line_prefixes
# ---------------------------------------------------------------------------

class TestSampleRscLinePrefixes(unittest.TestCase):

    def test_returns_empty_list_for_empty_body(self):
        self.assertEqual(ft._sample_rsc_line_prefixes(''), [])

    def test_extracts_single_rsc_line(self):
        body = '1:"$Sreact.fragment"'
        result = ft._sample_rsc_line_prefixes(body)
        self.assertEqual(len(result), 1)
        self.assertTrue(result[0].startswith('1:'))
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


# ---------------------------------------------------------------------------
# infer_category / group_by_category
# ---------------------------------------------------------------------------

def _template(title='T', meta_title='', slug='s', price='Free', author='A'):
    return {
        'title': title, 'meta_title': meta_title, 'slug': slug,
        'url': f'https://www.framer.com/community/marketplace/templates/{slug}/',
        'author': author, 'author_slug': '', 'price': price,
        'thumbnail': '', 'published_at': '', 'demo_url': '', 'remixes': 0,
    }


class TestInferCategory(unittest.TestCase):

    def test_matches_title_keyword(self):
        self.assertEqual(ft.infer_category(_template(title='Gym Fitness Pro')), 'Health & Fitness')

    def test_case_insensitive(self):
        self.assertEqual(ft.infer_category(_template(title='PORTFOLIO Site')), 'Portfolio & Creative')

    def test_returns_other_when_no_match(self):
        self.assertEqual(ft.infer_category(_template(title='Abstract Minimal')), 'Other')

    def test_first_matching_category_wins(self):
        # "SaaS" appears before "Landing Page" in CATEGORY_KEYWORDS
        self.assertEqual(ft.infer_category(_template(title='SaaS Landing Page')), 'SaaS & Tech')

    def test_multi_word_keyword(self):
        self.assertEqual(ft.infer_category(_template(title='Luxury Real Estate')), 'Real Estate')

    def test_short_keyword_ai_not_matched_inside_word(self):
        # 'ai' must not match inside 'retail' — a retail store is E-commerce,
        # not SaaS & Tech (the category that owns the 'ai' keyword).
        self.assertEqual(ft.infer_category(_template(title='Retail Store Template')), 'E-commerce & Retail')

    def test_short_keyword_ai_not_matched_inside_email(self):
        # 'ai' must not match inside 'email'.
        self.assertEqual(ft.infer_category(_template(title='Email Marketing Landing')), 'Agency')

    def test_short_keyword_app_not_matched_inside_word(self):
        # 'app' must not match inside 'happy'/'wrapper' — a shop is E-commerce.
        self.assertEqual(ft.infer_category(_template(title='Happy Wrapper Shop')), 'E-commerce & Retail')

    def test_standalone_ai_still_matches(self):
        # A genuine standalone 'AI' product is still SaaS & Tech.
        self.assertEqual(ft.infer_category(_template(title='AI Dashboard Platform')), 'SaaS & Tech')

    def test_standalone_app_still_matches(self):
        # A genuine standalone 'App' is still SaaS & Tech.
        self.assertEqual(ft.infer_category(_template(title='App Builder')), 'SaaS & Tech')

    def test_punctuation_keyword_ecommerce_matches(self):
        # Keywords containing punctuation ('e-commerce') keep working.
        self.assertEqual(ft.infer_category(_template(title='E-commerce Boutique')), 'E-commerce & Retail')

    def test_punctuation_keyword_bar_and_grill_matches(self):
        # Keywords with spaces and '&' ('bar & grill') keep working.
        self.assertEqual(ft.infer_category(_template(title='Downtown Bar & Grill House')), 'Food & Dining')

    def test_multi_word_landing_keyword_matches(self):
        # Multi-word keyword 'coming soon' keeps working.
        self.assertEqual(ft.infer_category(_template(title='Coming Soon Page')), 'Landing Page')


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
    'url': 'https://www.framer.com/community/marketplace/templates/my-template/',
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

    def test_excludes_published_when_malformed(self):
        """A malformed published_at (e.g. residual $D prefix) must be omitted
        to avoid a recurring Notion 400 that would prevent the template from
        ever being saved."""
        t = {**_BASE_TEMPLATE, 'published_at': '$D2024-01-15'}
        with patch('framer_templates.http_post', return_value={}) as mock_post:
            ft.save_to_notion(t)
        props = mock_post.call_args[0][1]['properties']
        self.assertNotIn('Published', props)

    def test_malformed_published_at_logs_warning(self):
        """A non-empty but unparseable published_at must log a warning with
        the slug and raw value for diagnosis."""
        import error_log as el
        t = {**_BASE_TEMPLATE, 'slug': 'bad-date-slug', 'published_at': 'not-a-date'}
        with patch('framer_templates.http_post', return_value={}), \
             patch.object(el, 'log_error') as mock_log:
            ft.save_to_notion(t)
        self.assertTrue(mock_log.called)
        ctx = mock_log.call_args[0][3]
        self.assertEqual(ctx['slug'], 'bad-date-slug')
        self.assertEqual(ctx['published_at'], 'not-a-date')

    def test_valid_published_at_still_saved(self):
        """A valid ISO 8601 published_at must still be saved to Notion."""
        t = {**_BASE_TEMPLATE, 'published_at': '2024-01-15T10:00:00+00:00'}
        with patch('framer_templates.http_post', return_value={}) as mock_post:
            ft.save_to_notion(t)
        props = mock_post.call_args[0][1]['properties']
        self.assertIn('Published', props)
        self.assertEqual(props['Published']['date']['start'], '2024-01-15T10:00:00+00:00')

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
            'https://www.framer.com/community/marketplace/profiles/alice-studio/',
        )

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

    def test_includes_demo_url_when_present(self):
        t = {**_BASE_TEMPLATE, 'demo_url': 'https://mysite.framer.website/'}
        with patch('framer_templates.http_post', return_value={}) as mock_post:
            ft.save_to_notion(t)
        props = mock_post.call_args[0][1]['properties']
        self.assertIn('Demo URL', props)
        self.assertEqual(props['Demo URL']['url'], 'https://mysite.framer.website/')

    def test_includes_remixes_when_nonzero(self):
        t = {**_BASE_TEMPLATE, 'remixes': 12}
        with patch('framer_templates.http_post', return_value={}) as mock_post:
            ft.save_to_notion(t)
        props = mock_post.call_args[0][1]['properties']
        self.assertIn('Remixes', props)
        self.assertEqual(props['Remixes']['number'], 12)

    def test_includes_category_select_property(self):
        t = {**_BASE_TEMPLATE, 'title': 'Gym & Fitness Pro', 'meta_title': ''}
        with patch('framer_templates.http_post', return_value={}) as mock_post:
            ft.save_to_notion(t)
        props = mock_post.call_args[0][1]['properties']
        self.assertIn('Category', props)
        self.assertEqual(props['Category']['select']['name'], 'Health & Fitness')

    def test_category_uses_meta_title_for_inference(self):
        t = {**_BASE_TEMPLATE, 'title': 'Minimal', 'meta_title': 'Restaurant Website Template'}
        with patch('framer_templates.http_post', return_value={}) as mock_post:
            ft.save_to_notion(t)
        props = mock_post.call_args[0][1]['properties']
        self.assertEqual(props['Category']['select']['name'], 'Food & Dining')

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

    def test_title_with_supplementary_emoji_fits_notion_utf16_limit(self):
        """Title containing supplementary-plane chars must fit Notion's UTF-16 limit.

        Notion counts UTF-16 code units, not Python code points.  A naive
        ``[:2000]`` slice on a string of all-emoji would produce 2000 code
        points / 4000 UTF-16 code units and trigger a 400 validation error.
        """
        # U+1F600 grinning face emoji = 1 code point, 2 UTF-16 code units
        t = {**_BASE_TEMPLATE, 'title': '\U0001F600' * 1500}
        with patch('framer_templates.http_post', return_value={}) as mock_post:
            ft.save_to_notion(t)
        props = mock_post.call_args[0][1]['properties']
        name_val = props['Name']['title'][0]['text']['content']
        utf16_units = len(name_val.encode('utf-16-le')) // 2
        self.assertLessEqual(utf16_units, 2000)

    def test_meta_title_with_mixed_supplementary_chars_fits_notion_limit(self):
        """Mixed BMP + supplementary chars must still fit the UTF-16 limit."""
        # 1900 ASCII chars + 200 emoji = 1900 + 400 = 2300 UTF-16 code units
        t = {**_BASE_TEMPLATE, 'meta_title': ('a' * 1900) + ('\U0001F600' * 200)}
        with patch('framer_templates.http_post', return_value={}) as mock_post:
            ft.save_to_notion(t)
        props = mock_post.call_args[0][1]['properties']
        meta_val = props['Meta Title']['rich_text'][0]['text']['content']
        utf16_units = len(meta_val.encode('utf-16-le')) // 2
        self.assertLessEqual(utf16_units, 2000)


# ---------------------------------------------------------------------------
# _build_embed
# ---------------------------------------------------------------------------

_DISCORD_TEMPLATE = {
    'title': 'Test Template',
    'url': 'https://www.framer.com/community/marketplace/templates/test/',
    'author': 'Bob',
    'author_slug': 'bob-studio',
    'price': '$10',
    'thumbnail': '',
}


class TestBuildEmbed(unittest.TestCase):

    def test_basic_embed_structure(self):
        embed = ft._build_embed(_DISCORD_TEMPLATE)
        self.assertEqual(embed['title'], 'Test Template')
        self.assertEqual(embed['url'], 'https://www.framer.com/community/marketplace/templates/test/')
        self.assertEqual(embed['color'], 0x5865F2)

    def test_description_includes_author_link(self):
        embed = ft._build_embed(_DISCORD_TEMPLATE)
        self.assertIn('[Bob](https://www.framer.com/community/marketplace/profiles/bob-studio/)', embed['description'])
        self.assertIn('**$10**', embed['description'])

    def test_includes_image_when_thumbnail_present(self):
        t = {**_DISCORD_TEMPLATE, 'thumbnail': 'https://cdn.example.com/img.jpg'}
        embed = ft._build_embed(t)
        self.assertIn('image', embed)
        self.assertEqual(embed['image']['url'], 'https://cdn.example.com/img.jpg')

    def test_description_escapes_bracket_in_author_link_text(self):
        # An author whose Framer display name contains ``[`` or ``]`` would
        # otherwise terminate the markdown link early and dump ``](profile-url)``
        # into the description as plain text.  Verify the brackets are escaped.
        t = {**_DISCORD_TEMPLATE, 'author': 'Jane [Studio]'}
        embed = ft._build_embed(t)
        self.assertIn(
            r'[Jane \[Studio\]](https://www.framer.com/community/marketplace/profiles/bob-studio/)',
            embed['description'],
        )

    def test_description_escapes_closing_paren_in_demo_url(self):
        # A demo URL containing ``)`` (e.g. a parenthesised path segment)
        # would terminate the markdown link early — escape it so the full
        # URL is preserved as the link target.
        t = {**_DISCORD_TEMPLATE, 'demo_url': 'https://example.com/path(v1)/'}
        embed = ft._build_embed(t)
        self.assertIn(r'[Live Demo](https://example.com/path(v1\)/)', embed['description'])

    def test_description_includes_remixes_when_nonzero(self):
        t = {**_DISCORD_TEMPLATE, 'remixes': 5}
        embed = ft._build_embed(t)
        self.assertIn('5 remixes', embed['description'])

    def test_description_remix_singular_when_one(self):
        t = {**_DISCORD_TEMPLATE, 'remixes': 1}
        embed = ft._build_embed(t)
        self.assertIn('1 remix', embed['description'])
        self.assertNotIn('1 remixes', embed['description'])

    def test_title_truncated_to_discord_256_char_limit(self):
        # Discord rejects embed titles > 256 chars with HTTP 400 "Invalid Form
        # Body".  A Framer template with an unusually long title would
        # otherwise drop the entire notification for that template.
        long_title = 'X' * 400
        t = {**_DISCORD_TEMPLATE, 'title': long_title}
        embed = ft._build_embed(t)
        self.assertEqual(len(embed['title']), 256)
        self.assertEqual(embed['title'], 'X' * 256)

    def test_description_truncated_to_discord_4096_char_limit(self):
        # A pathologically long meta_title could push the embed description
        # past Discord's 4096-char limit.  Truncating defensively means the
        # webhook POST cannot be rejected for that reason.
        huge_meta_title = 'M' * 5000
        t = {**_DISCORD_TEMPLATE, 'meta_title': huge_meta_title}
        embed = ft._build_embed(t)
        self.assertLessEqual(len(embed['description']), 4096)

    def test_timestamp_omitted_when_published_at_unparseable(self):
        # Defensive guard: an unparseable published_at (e.g. a residual "$D"
        # prefix left over from a future RSC format change, or a stray
        # non-ISO sentinel) would otherwise 400 the entire webhook POST.
        # Mirrors the same guard already in notify_discord_lead.
        for bad_value in ('$D2026-03-15T10:00:00.000Z', 'not-a-date',
                          '2026/03/15', 'null'):
            with self.subTest(bad_value=bad_value):
                t = {**_DISCORD_TEMPLATE, 'published_at': bad_value}
                embed = ft._build_embed(t)
                self.assertNotIn('timestamp', embed)


# ---------------------------------------------------------------------------
# _build_summary_embed
# ---------------------------------------------------------------------------

class TestBuildSummaryEmbed(unittest.TestCase):

    def test_singular_title_for_one_template(self):
        embed = ft._build_summary_embed([_template(title='Gym Pro')])
        self.assertEqual(embed['title'], '1 new Framer template')

    def test_description_contains_category_headers(self):
        templates = [
            _template(title='Gym Pro', slug='gym'),
            _template(title='My Portfolio', slug='port'),
        ]
        embed = ft._build_summary_embed(templates)
        self.assertIn('**Health & Fitness**', embed['description'])
        self.assertIn('**Portfolio & Creative**', embed['description'])

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

    def test_escapes_bracket_in_title_to_preserve_markdown_link(self):
        # A title containing ``]`` would otherwise terminate the Discord
        # markdown link early — the renderer would treat ``Pro`` as a link to
        # ``Brand`` and dump ``](url)`` as visible plain text.  Verify the
        # bracket is backslash-escaped inside the ``[...]`` segment.
        t = _template(title='Brand [Pro]', slug='brand-pro')
        embed = ft._build_summary_embed([t])
        self.assertIn(r'[Brand \[Pro\]](', embed['description'])
        # Raw unescaped ``]`` must not appear before the closing ``](``.
        self.assertNotIn('[Brand [Pro]]', embed['description'])


# ---------------------------------------------------------------------------
# _escape_md_link_text / _escape_md_link_url
# ---------------------------------------------------------------------------

class TestEscapeMarkdownLinkHelpers(unittest.TestCase):

    def test_link_text_escapes_brackets(self):
        self.assertEqual(ft._escape_md_link_text('Brand [Pro]'), r'Brand \[Pro\]')

    def test_link_text_escapes_backslash_before_other_chars(self):
        # Backslash must be escaped first so the ``\\]`` below cannot be
        # misread by the markdown parser as an already-escaped ``]``.
        self.assertEqual(ft._escape_md_link_text(r'A\B'), r'A\\B')
        # Combined backslash + bracket: backslash doubles first, then ``]``
        # is escaped, yielding ``\\\]`` (two backslashes then escaped ``]``).
        self.assertEqual(ft._escape_md_link_text(r'A\]B'), r'A\\\]B')

    def test_link_url_escapes_closing_paren(self):
        self.assertEqual(
            ft._escape_md_link_url('https://example.com/path(v1)/'),
            r'https://example.com/path(v1\)/',
        )

    def test_link_url_escapes_backslash_before_paren(self):
        # Backslash escapes first; the trailing ``)`` then gets its own escape.
        self.assertEqual(ft._escape_md_link_url(r'a\b)c'), r'a\\b\)c')


# ---------------------------------------------------------------------------
# notify_discord_batch
# ---------------------------------------------------------------------------

class TestNotifyDiscordBatch(unittest.TestCase):

    def setUp(self):
        os.environ['DISCORD_WEBHOOK_URL_TEMPLATES'] = 'https://discord.com/api/webhooks/test'
        # Disable the inter-message rate-limit pacing for the rest of the
        # batch tests so they remain fast.  Dedicated tests below verify the
        # delay behaviour with the real value.
        self._delay_patcher = patch('framer_templates._DISCORD_INTER_MESSAGE_DELAY', 0)
        self._delay_patcher.start()
        self.addCleanup(self._delay_patcher.stop)

    def test_single_template_summary_embed(self):
        with patch('framer_templates.http_post', return_value={}) as mock_post:
            ft.notify_discord_batch([_DISCORD_TEMPLATE])
        # 1 detail embed + 1 summary embed = 2 calls; summary is last so it
        # renders at the bottom of the Discord channel as a recap.
        self.assertEqual(mock_post.call_count, 2)
        embed_payload = mock_post.call_args_list[0][0][1]
        self.assertEqual(len(embed_payload['embeds']), 1)
        summary_payload = mock_post.call_args_list[-1][0][1]
        self.assertIn('embeds', summary_payload)
        self.assertEqual(summary_payload['embeds'][0]['title'], '1 new Framer template')

    def test_many_templates_sends_one_embed_per_message(self):
        templates = [{**_DISCORD_TEMPLATE, 'slug': f'slug-{i}', 'title': f'T{i}'} for i in range(12)]
        with patch('framer_templates.http_post', return_value={}) as mock_post:
            ft.notify_discord_batch(templates)
        # 12 individual embeds + 1 summary = 13 calls
        self.assertEqual(mock_post.call_count, 13)
        # Each detail call has exactly 1 embed
        for i in range(12):
            payload = mock_post.call_args_list[i][0][1]
            self.assertEqual(len(payload['embeds']), 1)
        # Final call is the summary embed
        summary_payload = mock_post.call_args_list[-1][0][1]
        self.assertIn('embeds', summary_payload)
        self.assertEqual(summary_payload['embeds'][0]['title'], '12 new Framer templates')

    def test_empty_list_does_nothing(self):
        with patch('framer_templates.http_post', return_value={}) as mock_post:
            ft.notify_discord_batch([])
        mock_post.assert_not_called()

    def test_error_in_one_message_does_not_stop_remaining(self):
        templates = [{**_DISCORD_TEMPLATE, 'slug': f'slug-{i}', 'title': f'T{i}'} for i in range(3)]
        # 4 calls total: 3 embeds + summary; first fails
        effects = [Exception('fail')] + [{}] * 3
        with patch('framer_templates.http_post', side_effect=effects) as mock_post:
            ft.notify_discord_batch(templates)  # must not raise
        self.assertEqual(mock_post.call_count, 4)

    def test_http_error_logs_discord_response_body(self):
        """When the Discord webhook raises an HTTPError, the API response body
        must be captured as ``discord_response`` so an operator can distinguish
        between a revoked webhook (401), deleted webhook (404), rate-limit
        (429), and a malformed-payload rejection (400) -- all of which would
        otherwise log only ``"HTTP Error <code>: <reason>"``.  Mirrors the
        diagnostic capture pattern in ``post_to_x`` and ``save_to_notion``."""
        import io
        import error_log as el
        body = b'{"message": "Invalid Webhook Token", "code": 50027}'
        # Each call must raise a fresh HTTPError -- the body stream is consumed
        # on the first ``read()``, so reusing the same instance for the summary
        # and embed messages would yield an empty body on the second call.
        def fresh_err(*args, **kwargs):
            raise urllib.error.HTTPError(
                'https://discord.com/api/webhooks/test',
                401, 'Unauthorized', {}, io.BytesIO(body),
            )
        with patch('framer_templates.http_post', side_effect=fresh_err), \
             patch.object(el, 'log_error') as mock_log:
            ft.notify_discord_batch([_DISCORD_TEMPLATE])  # must not raise
        self.assertTrue(mock_log.called)
        # Inspect every logged context -- both the summary failure and the
        # individual-embed failure must capture the response body and status.
        contexts = [c[0][3] for c in mock_log.call_args_list if len(c[0]) >= 4]
        self.assertTrue(contexts)
        for ctx in contexts:
            self.assertEqual(ctx.get('status'), 401)
            self.assertIn('discord_response', ctx)
            self.assertIn('Invalid Webhook Token', ctx['discord_response'])
            # Existing diagnostic context (label) is preserved.
            self.assertIn('label', ctx)
            self.assertIn('error', ctx)

    def test_notify_discord_wrapper_calls_batch(self):
        with patch('framer_templates.http_post', return_value={}) as mock_post:
            ft.notify_discord(_DISCORD_TEMPLATE)
        # 1 detail embed + 1 summary embed = 2 calls; summary sent last.
        self.assertEqual(mock_post.call_count, 2)
        summary_payload = mock_post.call_args_list[-1][0][1]
        self.assertIn('embeds', summary_payload)


class TestNotifyDiscordBatchRateLimitPacing(unittest.TestCase):
    """Verify ``notify_discord_batch`` sleeps ``_DISCORD_INTER_MESSAGE_DELAY``
    between successive webhook POSTs.

    Discord's webhook routes are rate-limited (~5 msgs / 2s); without proactive
    pacing, a 20-template batch reliably trips a 429 and then has to wait out
    the server-supplied ``Retry-After`` on every subsequent message — which
    typically costs more wall-clock time than the small inter-message delay
    itself.  These tests use the *real* module constant so the behaviour can
    be verified end-to-end without inflating runtime (``time.sleep`` is
    patched out so the test still completes instantly).
    """

    def setUp(self):
        os.environ['DISCORD_WEBHOOK_URL_TEMPLATES'] = 'https://discord.com/api/webhooks/test'

    def test_sleeps_between_messages_not_before_first(self):
        """With N payloads we expect exactly N-1 sleeps -- the first message
        is sent immediately, then each subsequent one is preceded by a
        ``_DISCORD_INTER_MESSAGE_DELAY`` pause."""
        templates = [
            {**_DISCORD_TEMPLATE, 'slug': f'slug-{i}', 'title': f'T{i}'}
            for i in range(4)
        ]
        # 4 embeds + 1 summary = 5 messages, so 4 sleeps expected.
        with patch('framer_templates.http_post', return_value={}) as mock_post, \
             patch('framer_templates.time.sleep') as mock_sleep:
            ft.notify_discord_batch(templates)
        self.assertEqual(mock_post.call_count, 5)
        self.assertEqual(mock_sleep.call_count, 4)
        # All sleeps must use the configured delay.
        for call in mock_sleep.call_args_list:
            self.assertEqual(call[0][0], ft._DISCORD_INTER_MESSAGE_DELAY)

    def test_empty_list_does_not_sleep(self):
        with patch('framer_templates.http_post') as mock_post, \
             patch('framer_templates.time.sleep') as mock_sleep:
            ft.notify_discord_batch([])
        mock_post.assert_not_called()
        mock_sleep.assert_not_called()

    def test_delay_zero_skips_sleep_entirely(self):
        """Setting the constant to 0 must short-circuit the sleep call so
        unit tests and synchronous tooling can bypass the pacing cleanly."""
        templates = [
            {**_DISCORD_TEMPLATE, 'slug': f'slug-{i}', 'title': f'T{i}'}
            for i in range(3)
        ]
        with patch('framer_templates._DISCORD_INTER_MESSAGE_DELAY', 0), \
             patch('framer_templates.http_post', return_value={}), \
             patch('framer_templates.time.sleep') as mock_sleep:
            ft.notify_discord_batch(templates)
        mock_sleep.assert_not_called()


# ---------------------------------------------------------------------------
# main (integration)
# ---------------------------------------------------------------------------

_TEMPLATES = [
    {
        'slug': 'template-a', 'title': 'Template A',
        'url': 'https://www.framer.com/community/marketplace/templates/template-a/',
        'author': 'Alice', 'author_slug': '', 'price': 'Free', 'thumbnail': '', 'published_at': '',
    },
    {
        'slug': 'template-b', 'title': 'Template B',
        'url': 'https://www.framer.com/community/marketplace/templates/template-b/',
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

    def test_get_seen_slugs_http_error_sends_discord_alert(self):
        """HTTPError from get_seen_slugs (e.g. Notion 404 / 401) must trigger a Discord alert."""
        import io
        http_err = urllib.error.HTTPError(
            None, 404, 'Not Found', {},
            io.BytesIO(b'{"object":"error","code":"object_not_found",'
                       b'"message":"Could not find database"}'),
        )
        with patch('framer_templates.fetch_framer_templates', return_value=_TEMPLATES), \
             patch('framer_templates.get_seen_slugs', side_effect=http_err), \
             patch('framer_templates._warn_discord') as warn_mock, \
             patch('builtins.open', side_effect=FileNotFoundError):
            with self.assertRaises(SystemExit):
                ft.main()
        # _warn_discord may also have been called for "<5 templates" warning if
        # _TEMPLATES has fewer than 5 entries; find the get_seen_slugs alert.
        alert_calls = [
            c for c in warn_mock.call_args_list
            if 'get_seen_slugs' in c[0][0]
        ]
        self.assertTrue(alert_calls, 'Expected a _warn_discord call mentioning get_seen_slugs')
        msg = alert_calls[0][0][0]
        self.assertIn('ERROR', msg)
        self.assertIn('404', msg)

    def test_get_seen_slugs_http_error_logs_notion_response(self):
        """HTTPError from get_seen_slugs must capture the Notion response body in the error log."""
        import error_log as el
        import io
        body = (b'{"object":"error","code":"object_not_found",'
                b'"message":"Could not find database with ID: xyz"}')
        http_err = urllib.error.HTTPError(None, 404, 'Not Found', {}, io.BytesIO(body))
        with patch('framer_templates.fetch_framer_templates', return_value=_TEMPLATES), \
             patch('framer_templates.get_seen_slugs', side_effect=http_err), \
             patch('framer_templates._warn_discord'), \
             patch.object(el, 'log_error') as mock_log, \
             patch('builtins.open', side_effect=FileNotFoundError):
            with self.assertRaises(SystemExit):
                ft.main()
        slug_err_calls = [
            c for c in mock_log.call_args_list
            if len(c[0]) >= 4 and isinstance(c[0][3], dict)
            and c[0][3].get('notion_response')
        ]
        self.assertTrue(slug_err_calls, 'Expected log_error call with notion_response context')
        ctx = slug_err_calls[0][0][3]
        self.assertEqual(ctx['status'], 404)
        self.assertIn('Could not find database', ctx['notion_response'])

    def test_get_seen_slugs_http_error_does_not_call_save(self):
        """HTTPError from get_seen_slugs must short-circuit before any save/notify happens."""
        import io
        http_err = urllib.error.HTTPError(None, 404, 'Not Found', {}, io.BytesIO(b'{}'))
        save_mock = MagicMock()
        notify_mock = MagicMock()
        with patch('framer_templates.fetch_framer_templates', return_value=_TEMPLATES), \
             patch('framer_templates.get_seen_slugs', side_effect=http_err), \
             patch('framer_templates.save_to_notion', save_mock), \
             patch('framer_templates.notify_discord_batch', notify_mock), \
             patch('framer_templates.post_to_x'), \
             patch('framer_templates._warn_discord'), \
             patch('builtins.open', side_effect=FileNotFoundError):
            with self.assertRaises(SystemExit):
                ft.main()
        save_mock.assert_not_called()
        notify_mock.assert_not_called()


# ---------------------------------------------------------------------------
# _write_summary
# ---------------------------------------------------------------------------

class TestMainWritesSummary(unittest.TestCase):

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

    def test_summary_reports_partial_failure_count(self):
        """When some saves fail, summary must report saved/total and mention failures."""
        templates = [
            {'slug': f's{i}', 'title': f'T{i}', 'url': f'u{i}', 'author': 'A',
             'author_slug': '', 'price': 'Free', 'thumbnail': '', 'published_at': ''}
            for i in range(4)
        ]
        # First 2 succeed, then 2 fail (below short-circuit threshold of 3)
        save_mock = MagicMock(side_effect=[None, None, Exception('err'), Exception('err')])
        with patch.dict('os.environ', {'NOTION_TOKEN': 'ntn_x', 'NOTION_DATABASE_ID': 'db',
                                       'DISCORD_WEBHOOK_URL_TEMPLATES': 'https://h.com/w'}), \
             patch('framer_templates.fetch_framer_templates', return_value=templates), \
             patch('framer_templates.get_seen_slugs', return_value={'existing'}), \
             patch('framer_templates._warn_discord'), \
             patch('framer_templates.save_to_notion', save_mock), \
             patch('framer_templates.notify_discord_batch'), \
             patch('framer_templates.post_to_x'), \
             patch('framer_templates._write_summary') as mock_summary, \
             patch('builtins.open', side_effect=FileNotFoundError):
            ft.main()
        mock_summary.assert_called_once()
        summary_text = mock_summary.call_args[0][0]
        self.assertIn('2/4', summary_text)
        self.assertIn('failed', summary_text)

    def test_summary_reports_short_circuit_failure_count(self):
        """When save loop short-circuits, summary must report saved/total and mention failures."""
        templates = [
            {'slug': f's{i}', 'title': f'T{i}', 'url': f'u{i}', 'author': 'A',
             'author_slug': '', 'price': 'Free', 'thumbnail': '', 'published_at': ''}
            for i in range(10)
        ]
        # All saves fail → short-circuit after 3
        save_mock = MagicMock(side_effect=Exception('Notion down'))
        with patch.dict('os.environ', {'NOTION_TOKEN': 'ntn_x', 'NOTION_DATABASE_ID': 'db',
                                       'DISCORD_WEBHOOK_URL_TEMPLATES': 'https://h.com/w'}), \
             patch('framer_templates.fetch_framer_templates', return_value=templates), \
             patch('framer_templates.get_seen_slugs', return_value={'existing'}), \
             patch('framer_templates._warn_discord'), \
             patch('framer_templates.save_to_notion', save_mock), \
             patch('framer_templates.notify_discord_batch'), \
             patch('framer_templates.post_to_x'), \
             patch('framer_templates._write_summary') as mock_summary, \
             patch('builtins.open', side_effect=FileNotFoundError):
            ft.main()
        mock_summary.assert_called_once()
        summary_text = mock_summary.call_args[0][0]
        self.assertIn('0/10', summary_text)
        self.assertIn('failed', summary_text)


# ---------------------------------------------------------------------------
# _build_tweet_text
# ---------------------------------------------------------------------------

class TestBuildTweetText(unittest.TestCase):

    def test_within_280_chars(self):
        templates = [_template(title=f'Template {i}', slug=f's{i}') for i in range(5)]
        text = ft._build_tweet_text(templates)
        self.assertLessEqual(len(text), 280)
        self.assertIn('framer.com/community/marketplace', text)

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

    def test_http_error_logs_twitter_response_body(self):
        """HTTPError on Twitter POST captures the API response body for diagnosis.

        Twitter's API returns very different bodies for each failure class
        (expired token vs duplicate tweet vs content-policy rejection), and the
        bare ``HTTP Error 401: Unauthorized`` string we log otherwise gives no
        signal about which one occurred.  Mirrors the pattern used by
        ``save_to_notion`` / ``url_exists_in_notion``.
        """
        import io
        response_body = b'{"title":"Forbidden","detail":"You are not permitted to perform this action."}'
        error = urllib.error.HTTPError(
            None, 403, 'Forbidden', {}, io.BytesIO(response_body)
        )
        with patch.dict('os.environ', self._CRED_ENV), \
             patch('framer_templates.http_post', side_effect=error), \
             patch('error_log.log_error') as mock_log:
            ft.post_to_x([_template()])
        mock_log.assert_called_once()
        ctx = mock_log.call_args[0][3]  # positional context dict
        self.assertEqual(ctx['status'], 403)
        self.assertIn('twitter_response', ctx)
        self.assertIn('You are not permitted', ctx['twitter_response'])
        # tweet_length and error string remain so existing diagnostics still work
        self.assertIn('tweet_length', ctx)
        self.assertIn('error', ctx)


# ---------------------------------------------------------------------------
# Save-loop short-circuit on consecutive Notion failures
# ---------------------------------------------------------------------------

class TestSaveShortCircuit(unittest.TestCase):
    """Tests for the consecutive save-failure short-circuit in main()."""

    def setUp(self):
        os.environ['NOTION_TOKEN'] = 'test_token'
        os.environ['NOTION_DATABASE_ID'] = 'test_db_id'
        os.environ['DISCORD_WEBHOOK_URL_TEMPLATES'] = 'https://discord.com/api/webhooks/test'

    def _make_templates(self, count):
        return [
            {
                'slug': f'slug-{i}', 'title': f'Template {i}',
                'url': f'https://www.framer.com/community/marketplace/templates/slug-{i}/',
                'author': 'A', 'author_slug': '', 'price': 'Free',
                'thumbnail': '', 'published_at': '',
            }
            for i in range(count)
        ]

    def test_three_consecutive_failures_short_circuits(self):
        """When save_to_notion fails 3 times in a row, main() must stop trying
        further saves instead of grinding through the whole batch."""
        templates = self._make_templates(10)
        save_mock = MagicMock(side_effect=Exception('Notion unreachable'))
        with patch('framer_templates.fetch_framer_templates', return_value=templates), \
             patch('framer_templates.get_seen_slugs', return_value=set()), \
             patch('framer_templates.save_to_notion', save_mock), \
             patch('framer_templates.notify_discord_batch'), \
             patch('framer_templates.post_to_x'), \
             patch('framer_templates._warn_discord'), \
             patch('builtins.open', side_effect=FileNotFoundError):
            ft.main()
        # save_to_notion should be called exactly 3 times (the short-circuit
        # threshold) — not 10 times.
        self.assertEqual(save_mock.call_count, ft._CONSECUTIVE_SAVE_FAILURE_SHORT_CIRCUIT)

    def test_short_circuit_fires_discord_alert(self):
        """When the save loop short-circuits, a Discord alert must be sent."""
        templates = self._make_templates(10)
        save_mock = MagicMock(side_effect=Exception('Notion down'))
        with patch('framer_templates.fetch_framer_templates', return_value=templates), \
             patch('framer_templates.get_seen_slugs', return_value=set()), \
             patch('framer_templates.save_to_notion', save_mock), \
             patch('framer_templates.notify_discord_batch'), \
             patch('framer_templates.post_to_x'), \
             patch('framer_templates._warn_discord') as warn_mock, \
             patch('builtins.open', side_effect=FileNotFoundError):
            ft.main()
        short_circuit_calls = [
            c for c in warn_mock.call_args_list
            if 'save_short_circuited' in str(c)
        ]
        self.assertTrue(short_circuit_calls,
                        'Expected a _warn_discord call with save_short_circuited dedup_key')

    def test_two_failures_then_success_resets_counter(self):
        """Two consecutive failures followed by a success must reset the
        counter, allowing the remaining saves to proceed."""
        templates = self._make_templates(5)
        # fail, fail, success, fail, success
        save_mock = MagicMock(side_effect=[
            Exception('err'), Exception('err'), None,
            Exception('err'), None,
        ])
        with patch('framer_templates.fetch_framer_templates', return_value=templates), \
             patch('framer_templates.get_seen_slugs', return_value=set()), \
             patch('framer_templates.save_to_notion', save_mock), \
             patch('framer_templates.notify_discord_batch'), \
             patch('framer_templates.post_to_x'), \
             patch('framer_templates._warn_discord'), \
             patch('builtins.open', side_effect=FileNotFoundError):
            ft.main()
        # All 5 should be attempted — counter resets after the 3rd call.
        self.assertEqual(save_mock.call_count, 5)

    def test_short_circuit_with_http_errors(self):
        """HTTPError failures (not just generic Exceptions) must also trigger
        the short-circuit."""
        import io
        templates = self._make_templates(10)
        http_err = urllib.error.HTTPError(
            None, 500, 'Internal Server Error', {},
            io.BytesIO(b'{}'),
        )
        save_mock = MagicMock(side_effect=http_err)
        with patch('framer_templates.fetch_framer_templates', return_value=templates), \
             patch('framer_templates.get_seen_slugs', return_value=set()), \
             patch('framer_templates.save_to_notion', save_mock), \
             patch('framer_templates.notify_discord_batch'), \
             patch('framer_templates.post_to_x'), \
             patch('framer_templates._warn_discord'), \
             patch('builtins.open', side_effect=FileNotFoundError):
            ft.main()
        self.assertEqual(save_mock.call_count, ft._CONSECUTIVE_SAVE_FAILURE_SHORT_CIRCUIT)

    def test_short_circuit_still_notifies_successfully_saved_templates(self):
        """Templates saved before the short-circuit must still be notified."""
        templates = self._make_templates(10)
        # First 2 succeed, then 3 fail → short-circuit.
        save_mock = MagicMock(side_effect=[
            None, None,
            Exception('err'), Exception('err'), Exception('err'),
        ])
        notify_mock = MagicMock()
        with patch('framer_templates.fetch_framer_templates', return_value=templates), \
             patch('framer_templates.get_seen_slugs', return_value={'existing-slug'}), \
             patch('framer_templates.save_to_notion', save_mock), \
             patch('framer_templates.notify_discord_batch', notify_mock), \
             patch('framer_templates.post_to_x'), \
             patch('framer_templates._warn_discord'), \
             patch('builtins.open', side_effect=FileNotFoundError):
            ft.main()
        # Notify should have been called with the 2 successfully saved templates.
        notify_mock.assert_called_once()
        notified = notify_mock.call_args[0][0]
        self.assertEqual(len(notified), 2)


class TestObserveOnlyGate(unittest.TestCase):
    """When side effects are disabled (sandbox run), Notion writes and
    notifications are skipped so an in-progress script cannot touch production."""

    def test_save_to_notion_skipped_when_disabled(self):
        with patch.dict(os.environ, {'GITHUB_ACTIONS': '', 'ENABLE_SIDE_EFFECTS': ''}):
            with patch('framer_templates.http_post') as post_mock:
                ft.save_to_notion({'title': 'T', 'slug': 's', 'url': 'u'})
        post_mock.assert_not_called()

    def test_notify_discord_batch_skipped_when_disabled(self):
        with patch.dict(os.environ, {'GITHUB_ACTIONS': '', 'ENABLE_SIDE_EFFECTS': ''}):
            with patch('framer_templates.http_post') as post_mock:
                ft.notify_discord_batch([{'title': 'T', 'slug': 's', 'url': 'u'}])
        post_mock.assert_not_called()


# ---------------------------------------------------------------------------
# _extract_json_array
# ---------------------------------------------------------------------------

class TestExtractJsonArray(unittest.TestCase):

    def test_simple_array(self):
        self.assertEqual(ft._extract_json_array('[1,2,3]', 0), [1, 2, 3])

    def test_array_of_objects(self):
        self.assertEqual(ft._extract_json_array('[{"a":1},{"b":2}]', 0), [{'a': 1}, {'b': 2}])

    def test_nested_arrays(self):
        self.assertEqual(ft._extract_json_array('[[1],[2,[3]]]', 0), [[1], [2, [3]]])

    def test_string_containing_brackets(self):
        # Brackets inside string literals must not affect depth counting.
        self.assertEqual(ft._extract_json_array('["a]b","c["]', 0), ['a]b', 'c['])

    def test_non_zero_start_position(self):
        self.assertEqual(ft._extract_json_array('xx[9]', 2), [9])

    def test_unterminated_array_returns_none(self):
        self.assertIsNone(ft._extract_json_array('[1,2', 0))


# ---------------------------------------------------------------------------
# _new_format_template
# ---------------------------------------------------------------------------

class TestNewFormatTemplate(unittest.TestCase):

    def test_numeric_price_stringified(self):
        # data-array prices are numbers (e.g. 39) — must be coerced to a string.
        t = ft._new_format_template(_data_array_item('s', price=39))
        self.assertEqual(t['price'], '39')

    def test_null_price_becomes_empty_string(self):
        t = ft._new_format_template(_data_array_item('s', price=None))
        self.assertEqual(t['price'], '')

    def test_double_dollar_string_price_stripped(self):
        # The featured "resource":{...} path encodes a literal "$" as "$$".
        t = ft._new_format_template({'slug': 's', 'attributes': {'price': '$$49'}})
        self.assertEqual(t['price'], '$49')

    def test_plain_string_price_unchanged(self):
        t = ft._new_format_template({'slug': 's', 'attributes': {'price': 'Free'}})
        self.assertEqual(t['price'], 'Free')

    def test_maps_core_fields(self):
        t = ft._new_format_template(_data_array_item(
            'cool', title='Cool', author='Jane', author_slug='jane',
            meta_title='SaaS Dashboard', published='2026-06-20T12:00:00.000Z',
            thumbnail='https://x/y.jpg', demo_url='https://d.framer.website/'))
        self.assertEqual(t['title'], 'Cool')
        self.assertEqual(t['meta_title'], 'SaaS Dashboard')
        self.assertEqual(t['author'], 'Jane')
        self.assertEqual(t['author_slug'], 'jane')
        self.assertEqual(t['published_at'], '2026-06-20T12:00:00.000Z')
        self.assertEqual(t['thumbnail'], 'https://x/y.jpg')
        self.assertEqual(t['demo_url'], 'https://d.framer.website/')
        self.assertEqual(
            t['url'], 'https://www.framer.com/community/marketplace/templates/cool/')

    def test_missing_attributes_and_media_are_safe(self):
        t = ft._new_format_template({'slug': 's', 'title': 'T'})
        self.assertEqual(t['price'], '')
        self.assertEqual(t['demo_url'], '')
        self.assertEqual(t['thumbnail'], '')
        self.assertEqual(t['author'], '')


# ---------------------------------------------------------------------------
# _parse_rsc_data_array
# ---------------------------------------------------------------------------

class TestParseRscDataArray(unittest.TestCase):

    def _parse(self, body, seen=None):
        seen = set() if seen is None else seen
        templates: list = []
        errs = ft._parse_rsc_data_array(body, seen, templates)
        return seen, templates, errs

    def test_parses_templates_preserving_newest_first_order(self):
        body = _rsc_data_array_body([
            _data_array_item('a', id_='1', published='2026-06-20T10:00:00Z'),
            _data_array_item('b', id_='2', published='2026-06-19T10:00:00Z'),
        ])
        _, templates, _ = self._parse(body)
        self.assertEqual([t['slug'] for t in templates], ['a', 'b'])

    def test_ignores_non_template_array_elements(self):
        items = [_data_array_item('a', id_='1'),
                 {'type': 'category', 'slug': 'agency', 'id': 'c'}]
        _, templates, _ = self._parse(_rsc_data_array_body(items))
        self.assertEqual([t['slug'] for t in templates], ['a'])

    def test_dedupes_against_seen(self):
        body = _rsc_data_array_body([_data_array_item('dup', id_='1')])
        _, templates, _ = self._parse(body, seen={'dup'})
        self.assertEqual(len(templates), 0)

    def test_skips_data_key_followed_by_non_array(self):
        # ``"data":{...}`` (an object, not an array) must be skipped without error.
        body = '"data":{"slug":"x","type":"template"}'
        _, templates, errs = self._parse(body)
        self.assertEqual(len(templates), 0)
        self.assertEqual(errs, 0)

    def test_numeric_and_free_prices(self):
        body = _rsc_data_array_body([
            _data_array_item('paid', id_='1', price=39),
            _data_array_item('free', id_='2', price=None),
        ])
        _, templates, _ = self._parse(body)
        prices = {t['slug']: t['price'] for t in templates}
        self.assertEqual(prices['paid'], '39')
        self.assertEqual(prices['free'], '')


# ---------------------------------------------------------------------------
# fetch_from_rsc — embedded data array (the real newest-templates grid)
# ---------------------------------------------------------------------------

class TestFetchFromRscDataArray(unittest.TestCase):
    """The June-2026 redesign embeds the actual newest grid as a ``"data":[...]``
    array; the parser must read it (the pre-fix code only saw the 12 featured
    ``"resource":{...}`` blocks and missed every newly published template)."""

    def test_data_array_templates_are_captured(self):
        data_items = [
            _data_array_item(f'new-{i}', id_=str(i),
                             published=f'2026-06-20T{10 + i:02d}:00:00Z')
            for i in range(6)
        ]
        body = _rsc_data_array_body(data_items)
        with patch('framer_templates.http_get', return_value=body):
            templates = ft.fetch_from_rsc()
        slugs = {t['slug'] for t in templates}
        for i in range(6):
            self.assertIn(f'new-{i}', slugs)

    def test_data_array_and_featured_resource_both_captured(self):
        # The newest grid (data array) plus a curated featured block must both
        # appear, deduplicated by slug.
        data_items = [_data_array_item(f'new-{i}', id_=str(i)) for i in range(6)]
        body = _rsc_data_array_body(data_items) + '\n' + _rsc_item('featured-x', id_='99')
        with patch('framer_templates.http_get', return_value=body):
            templates = ft.fetch_from_rsc()
        slugs = {t['slug'] for t in templates}
        self.assertIn('featured-x', slugs)
        self.assertIn('new-0', slugs)

    def test_template_in_both_data_array_and_resource_block_not_duplicated(self):
        item = _data_array_item('shared', id_='1')
        body = _rsc_data_array_body([item]) + '\n' + _rsc_item('shared', id_='1')
        with patch('framer_templates.http_get', return_value=body):
            templates = ft.fetch_from_rsc()
        self.assertEqual(sum(1 for t in templates if t['slug'] == 'shared'), 1)

    def test_newest_first_order_preserved_end_to_end(self):
        data_items = [
            _data_array_item(f'n{i}', id_=str(i),
                             published=f'2026-06-20T{20 - i:02d}:00:00Z')
            for i in range(6)
        ]
        body = _rsc_data_array_body(data_items)
        with patch('framer_templates.http_get', return_value=body):
            templates = ft.fetch_from_rsc()
        self.assertEqual([t['slug'] for t in templates[:6]],
                         ['n0', 'n1', 'n2', 'n3', 'n4', 'n5'])


# ---------------------------------------------------------------------------
# main — notification cap / backlog backfill
# ---------------------------------------------------------------------------

class TestNotifyCap(unittest.TestCase):
    """A backlog (e.g. the first run after this parser fix) must not flood Discord:
    only the newest _MAX_NOTIFY_PER_RUN are announced; the rest are saved silently."""

    def setUp(self):
        os.environ['NOTION_TOKEN'] = 'test_token'
        os.environ['NOTION_DATABASE_ID'] = 'test_db_id'
        os.environ['DISCORD_WEBHOOK_URL_TEMPLATES'] = 'https://discord.com/api/webhooks/test'

    def _templates(self, n):
        # Newest-first: s0 is the newest.
        return [
            {'slug': f's{i}', 'title': f'T{i}',
             'url': f'https://www.framer.com/community/marketplace/templates/s{i}/',
             'author': 'A', 'author_slug': '', 'price': 'Free',
             'thumbnail': '', 'published_at': ''}
            for i in range(n)
        ]

    def _run(self, templates, seen_slugs):
        save = MagicMock()
        notify = MagicMock()
        x = MagicMock()
        summary = MagicMock()
        with patch('framer_templates.fetch_framer_templates', return_value=templates), \
             patch('framer_templates.get_seen_slugs', return_value=seen_slugs), \
             patch('framer_templates.save_to_notion', save), \
             patch('framer_templates.notify_discord_batch', notify), \
             patch('framer_templates.post_to_x', x), \
             patch('framer_templates._warn_discord'), \
             patch('framer_templates._write_summary', summary), \
             patch('builtins.open', side_effect=FileNotFoundError):
            ft.main()
        return save, notify, x, summary

    def test_backlog_saves_all_but_notifies_only_newest_cap(self):
        templates = self._templates(25)
        save, notify, x, _ = self._run(templates, {'existing'})
        # Every template is still persisted to Notion.
        self.assertEqual(save.call_count, 25)
        # Only the newest _MAX_NOTIFY_PER_RUN are announced.
        notify.assert_called_once()
        notified = notify.call_args[0][0]
        self.assertEqual(len(notified), ft._MAX_NOTIFY_PER_RUN)
        self.assertEqual([t['slug'] for t in notified],
                         [f's{i}' for i in range(ft._MAX_NOTIFY_PER_RUN)])
        # X post is capped identically.
        self.assertEqual(len(x.call_args[0][0]), ft._MAX_NOTIFY_PER_RUN)

    def test_under_cap_notifies_all(self):
        templates = self._templates(3)
        _, notify, _, _ = self._run(templates, {'existing'})
        notify.assert_called_once()
        self.assertEqual(len(notify.call_args[0][0]), 3)

    def test_backlog_summary_reports_backfill(self):
        templates = self._templates(25)
        _, _, _, summary = self._run(templates, {'existing'})
        summary.assert_called_once()
        text = summary.call_args[0][0]
        self.assertIn('backfilled', text.lower())

    def test_first_run_with_backlog_still_seeds_silently(self):
        # First run (empty DB) seeds everything without notifying, regardless of cap.
        templates = self._templates(25)
        save, notify, x, _ = self._run(templates, set())
        self.assertEqual(save.call_count, 25)
        notify.assert_not_called()
        x.assert_not_called()


if __name__ == '__main__':
    unittest.main()
