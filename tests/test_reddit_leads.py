#!/usr/bin/env python3
"""Tests for scripts/reddit_leads.py"""
import json
import os
import sys
import unittest
from datetime import datetime
from unittest.mock import MagicMock, call, patch

sys.path.insert(0, '.')
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'scripts'))
from scripts.reddit_leads import (
    _clean_html,
    fetch_reddit_posts,
    get_lead_by_id,
    get_pending_leads,
    mark_notified,
    notify_discord_lead,
    passes_light_filter,
    save_lead_to_notion,
    update_lead_status,
    url_exists_in_notion,
)

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
    <updated>2024-03-01T10:00:00+00:00</updated>
    <content type="html">&lt;p&gt;Looking for a Framer developer. Budget $500.&lt;/p&gt;</content>
  </entry>
  <entry>
    <title>No title entry</title>
    <link href="https://www.reddit.com/r/forhire/comments/def456/no_title/"/>
    <updated>2024-03-01T09:00:00+00:00</updated>
    <content type="html">&lt;p&gt;Some content here.&lt;/p&gt;</content>
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
    def test_content_truncated_to_1000(self, mock_post):
        mock_post.return_value = {}
        lead = {
            'title': 'Test', 'url': 'https://reddit.com/1',
            'subreddit': 'framer', 'content': 'x' * 2000, 'post_date': '',
        }
        save_lead_to_notion(lead, 'db-id')
        props = mock_post.call_args[0][1]['properties']
        content_val = props['Content']['rich_text'][0]['text']['content']
        self.assertEqual(len(content_val), 1000)

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
        # Should not raise
        notify_discord_lead(lead)


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
# TestMain
# ---------------------------------------------------------------------------

class TestMain(unittest.TestCase):

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


# ---------------------------------------------------------------------------
# _write_summary
# ---------------------------------------------------------------------------

from unittest.mock import mock_open
import scripts.reddit_leads as rl


class TestWriteSummary(unittest.TestCase):

    def tearDown(self):
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


if __name__ == '__main__':
    unittest.main()
