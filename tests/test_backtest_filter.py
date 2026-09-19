#!/usr/bin/env python3
"""Tests for scripts/backtest_filter.py"""
import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

sys.path.insert(0, '.')
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'scripts'))

from scripts.backtest_filter import (
    _plain,
    evaluate,
    fetch_corpus,
    load_cache,
    report,
    save_cache,
)


def _page(title, content, subreddit, status):
    """Build a Notion API page payload the way the Reddit Leads DB returns one."""
    return {
        'properties': {
            'Name': {'title': [{'plain_text': title}]},
            'Content': {'rich_text': [{'plain_text': content}]},
            'Subreddit': {'select': {'name': subreddit}},
            'Status': {'select': {'name': status}},
        }
    }


class TestPlain(unittest.TestCase):

    def test_joins_every_rich_text_block(self):
        """Notion splits long text across blocks; taking only the first
        truncates the post body and changes what the filter sees."""
        blocks = [{'plain_text': 'first '}, {'plain_text': 'second'}]
        self.assertEqual(_plain(blocks), 'first second')

    def test_empty_rich_text_is_empty_string(self):
        self.assertEqual(_plain([]), '')

    def test_missing_plain_text_key_is_skipped(self):
        self.assertEqual(_plain([{'plain_text': 'a'}, {}]), 'a')


class TestFetchCorpus(unittest.TestCase):

    @patch('scripts.backtest_filter.notion_headers', return_value={})
    @patch('scripts.backtest_filter.http_post')
    def test_returns_labelled_rows(self, mock_post, _headers):
        mock_post.return_value = {
            'results': [_page('Need a site', 'budget $500', 'framer', 'approved')],
            'has_more': False,
        }
        rows = fetch_corpus('db-id')
        self.assertEqual(rows, [{
            'title': 'Need a site',
            'content': 'budget $500',
            'subreddit': 'framer',
            'status': 'approved',
        }])

    @patch('scripts.backtest_filter.notion_headers', return_value={})
    @patch('scripts.backtest_filter.http_post')
    def test_follows_pagination_cursor(self, mock_post, _headers):
        mock_post.side_effect = [
            {'results': [_page('a', '', 'framer', 'approved')],
             'has_more': True, 'next_cursor': 'cur-1'},
            {'results': [_page('b', '', 'forhire', 'rejected')], 'has_more': False},
        ]
        rows = fetch_corpus('db-id')
        self.assertEqual(len(rows), 2)
        self.assertEqual(mock_post.call_args_list[1][0][1]['start_cursor'], 'cur-1')

    @patch('scripts.backtest_filter.notion_headers', return_value={})
    @patch('scripts.backtest_filter.http_post')
    def test_missing_select_properties_do_not_crash(self, mock_post, _headers):
        """A row whose Status or Subreddit was never set must not abort the run."""
        mock_post.return_value = {
            'results': [{'properties': {
                'Name': {'title': [{'plain_text': 'x'}]},
                'Content': {'rich_text': []},
                'Subreddit': {'select': None},
                'Status': {'select': None},
            }}],
            'has_more': False,
        }
        rows = fetch_corpus('db-id')
        self.assertEqual(rows[0]['status'], '')
        self.assertEqual(rows[0]['subreddit'], '')


class TestCacheRoundTrip(unittest.TestCase):

    def test_save_then_load_returns_same_rows(self):
        rows = [{'title': 't', 'content': 'c', 'subreddit': 'framer', 'status': 'approved'}]
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, 'sub', 'corpus.jsonl')
            save_cache(rows, path)
            self.assertEqual(load_cache(path), rows)

    def test_blank_lines_are_ignored(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, 'corpus.jsonl')
            with open(path, 'w') as f:
                f.write(json.dumps({'title': 'a'}) + '\n\n')
            self.assertEqual(len(load_cache(path)), 1)


class TestEvaluate(unittest.TestCase):

    def test_kept_lead_counts_as_ok_kept(self):
        rows = [{
            'title': '[Hiring] Need a Framer designer for our landing page',
            'content': 'Budget $2000, looking to hire someone.',
            'subreddit': 'framer', 'status': 'approved',
        }]
        self.assertEqual(evaluate(rows)['framer']['ok_kept'], 1)

    def test_cut_rejection_counts_as_bad_cut(self):
        """The art-commission case: hire signal and budget present, but the
        subject of the job is not web work and the subreddit has never
        produced a lead."""
        rows = [{
            'title': '[Hiring] artist for character illustrations',
            'content': 'Paying $300 a piece, send your portfolio.',
            'subreddit': 'HungryArtists', 'status': 'rejected',
        }]
        self.assertEqual(evaluate(rows)['HungryArtists']['bad_cut'], 1)

    def test_pending_and_failed_rows_are_skipped(self):
        """Only graded rows carry a label; anything else would skew the totals."""
        rows = [
            {'title': 't', 'content': '', 'subreddit': 'framer', 'status': 'pending'},
            {'title': 't', 'content': '', 'subreddit': 'framer', 'status': 'failed'},
        ]
        self.assertEqual(evaluate(rows), {})

    def test_blank_subreddit_is_bucketed_as_none(self):
        rows = [{'title': 't', 'content': '', 'subreddit': '', 'status': 'rejected'}]
        self.assertIn('(none)', evaluate(rows))


class TestReport(unittest.TestCase):

    def _run(self, stats):
        buf = io.StringIO()
        with redirect_stdout(buf):
            lost = report(stats)
        return lost, buf.getvalue()

    def test_returns_zero_when_no_lead_is_dropped(self):
        stats = {'framer': {'ok_kept': 2, 'ok_lost': 0, 'bad_kept': 1, 'bad_cut': 7}}
        lost, out = self._run(stats)
        self.assertEqual(lost, 0)
        self.assertNotIn('WARNING', out)

    def test_returns_and_flags_dropped_leads(self):
        """A non-zero return is what makes this usable as a CI gate."""
        stats = {'framer': {'ok_kept': 1, 'ok_lost': 3, 'bad_kept': 1, 'bad_cut': 5}}
        lost, out = self._run(stats)
        self.assertEqual(lost, 3)
        self.assertIn('WARNING', out)
        self.assertIn('LOST LEADS', out)

    def test_precision_improves_when_noise_is_cut(self):
        # 2 leads of 100 posts (2%) -> 2 leads of 12 kept (16.67%)
        stats = {'framer': {'ok_kept': 2, 'ok_lost': 0, 'bad_kept': 10, 'bad_cut': 88}}
        _lost, out = self._run(stats)
        self.assertIn('16.67%', out)
        self.assertIn('was 2.00%', out)


if __name__ == '__main__':
    unittest.main()
