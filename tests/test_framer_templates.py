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
from unittest.mock import MagicMock, mock_open, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'scripts'))
import framer_templates as ft


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
              thumbnail='https://cdn.example.com/t.jpg', published='$D2024-01-15'):
    return (
        f'"item":{{"id":"{id_}","slug":"{slug}","title":"{title}",'
        f'"price":"{price}","creator":{{"name":"{author}","slug":"{author_slug}"}},'
        f'"thumbnail":"{thumbnail}","publishedAt":"{published}"}}'
    )


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
        body = _rsc_item('cool-template', title='Cool Template', author='John Doe', author_slug='john-doe')
        templates = self._fetch(body)
        t = templates[0]
        self.assertEqual(t['title'], 'Cool Template')
        self.assertEqual(t['author'], 'John Doe')
        self.assertEqual(t['author_slug'], 'john-doe')
        self.assertEqual(t['price'], 'Free')  # no $$ prefix → unchanged
        self.assertEqual(t['url'], 'https://www.framer.com/marketplace/templates/cool-template/')
        self.assertEqual(t['thumbnail'], 'https://cdn.example.com/t.jpg')
        self.assertEqual(t['published_at'], '2024-01-15')  # $D prefix stripped

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
    'price': 'Free',
    'published_at': '2024-01-15',
    'thumbnail': '',
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


# ---------------------------------------------------------------------------
# notify_discord
# ---------------------------------------------------------------------------

_DISCORD_TEMPLATE = {
    'title': 'Test Template',
    'url': 'https://www.framer.com/marketplace/templates/test/',
    'author': 'Bob',
    'author_slug': 'bob-studio',
    'price': '$10',
    'thumbnail': '',
}


class TestNotifyDiscord(unittest.TestCase):

    def setUp(self):
        os.environ['DISCORD_WEBHOOK_URL'] = 'https://discord.com/api/webhooks/test'

    def test_posts_embed_to_webhook(self):
        with patch('framer_templates.http_post', return_value={}) as mock_post:
            ft.notify_discord(_DISCORD_TEMPLATE)
        mock_post.assert_called_once()
        payload = mock_post.call_args[0][1]
        self.assertIn('embeds', payload)
        embed = payload['embeds'][0]
        self.assertEqual(embed['title'], 'Test Template')
        self.assertEqual(embed['color'], 0x5865F2)

    def test_description_includes_author_link(self):
        with patch('framer_templates.http_post', return_value={}) as mock_post:
            ft.notify_discord(_DISCORD_TEMPLATE)
        embed = mock_post.call_args[0][1]['embeds'][0]
        self.assertIn('[Bob](https://www.framer.com/marketplace/profiles/bob-studio/)', embed['description'])
        self.assertIn('**$10**', embed['description'])

    def test_description_plain_author_when_no_slug(self):
        t = {**_DISCORD_TEMPLATE, 'author_slug': ''}
        with patch('framer_templates.http_post', return_value={}) as mock_post:
            ft.notify_discord(t)
        embed = mock_post.call_args[0][1]['embeds'][0]
        self.assertEqual(embed['description'], 'by Bob · **$10**')

    def test_includes_image_when_thumbnail_present(self):
        t = {**_DISCORD_TEMPLATE, 'thumbnail': 'https://cdn.example.com/img.jpg'}
        with patch('framer_templates.http_post', return_value={}) as mock_post:
            ft.notify_discord(t)
        embed = mock_post.call_args[0][1]['embeds'][0]
        self.assertIn('image', embed)
        self.assertEqual(embed['image']['url'], 'https://cdn.example.com/img.jpg')

    def test_no_image_key_when_thumbnail_absent(self):
        with patch('framer_templates.http_post', return_value={}) as mock_post:
            ft.notify_discord(_DISCORD_TEMPLATE)
        embed = mock_post.call_args[0][1]['embeds'][0]
        self.assertNotIn('image', embed)

    def test_exception_is_caught_and_does_not_propagate(self):
        with patch('framer_templates.http_post', side_effect=Exception('network error')):
            ft.notify_discord(_DISCORD_TEMPLATE)  # must not raise


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
        os.environ['DISCORD_WEBHOOK_URL'] = 'https://discord.com/api/webhooks/test'

    def _run(self, templates, seen_slugs):
        """Run main() with all I/O mocked; return (save_mock, notify_mock)."""
        save_mock = MagicMock()
        notify_mock = MagicMock()
        with patch('framer_templates.fetch_framer_templates', return_value=templates), \
             patch('framer_templates.get_seen_slugs', return_value=seen_slugs), \
             patch('framer_templates.save_to_notion', save_mock), \
             patch('framer_templates.notify_discord', notify_mock), \
             patch('builtins.open', side_effect=FileNotFoundError):
            ft.main()
        return save_mock, notify_mock

    def test_missing_env_var_raises_system_exit(self):
        del os.environ['NOTION_TOKEN']
        with patch('builtins.open', side_effect=FileNotFoundError):
            with self.assertRaises(SystemExit):
                ft.main()
        os.environ['NOTION_TOKEN'] = 'test_token'

    def test_no_new_templates_skips_save_and_notify(self):
        save, notify = self._run(_TEMPLATES, {'template-a', 'template-b'})
        save.assert_not_called()
        notify.assert_not_called()

    def test_first_run_seeds_db_without_discord(self):
        # Empty seen_slugs → first run
        save, notify = self._run(_TEMPLATES, set())
        self.assertEqual(save.call_count, 2)
        notify.assert_not_called()

    def test_normal_run_saves_and_notifies_only_new_templates(self):
        # template-a already seen; template-b is new
        save, notify = self._run(_TEMPLATES, {'template-a'})
        save.assert_called_once()
        self.assertEqual(save.call_args[0][0]['slug'], 'template-b')
        notify.assert_called_once()

    def test_save_failure_continues_processing_remaining_templates(self):
        save_mock = MagicMock(side_effect=[Exception('Notion error'), None])
        notify_mock = MagicMock()
        with patch('framer_templates.fetch_framer_templates', return_value=_TEMPLATES), \
             patch('framer_templates.get_seen_slugs', return_value=set()), \
             patch('framer_templates.save_to_notion', save_mock), \
             patch('framer_templates.notify_discord', notify_mock), \
             patch('builtins.open', side_effect=FileNotFoundError):
            ft.main()  # must not raise
        self.assertEqual(save_mock.call_count, 2)


if __name__ == '__main__':
    unittest.main()
