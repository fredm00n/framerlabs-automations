#!/usr/bin/env python3
"""
Replays ``passes_light_filter`` over every post the pipeline has ever saved and
reports what the current rules would keep and drop.

The Reddit Leads Notion DB is a labelled corpus: each row is a post that passed
the light filter at the time, carrying the verdict Phase 2 later gave it.  Since
every row already passed the *old* filter, replaying the *current* filter over
them measures exactly one thing — what a rule change would have cut:

  * an approved row the filter now drops is a lead that would have been lost
  * a rejected row the filter now drops is reviewer load that would be saved

That is the number to quote when changing the filter.  Guessing at keyword
precision without it is how the sets drifted to a 2.9% approval rate.

Usage:
    python3 scripts/backtest_filter.py --fetch     # pull corpus, then report
    python3 scripts/backtest_filter.py             # report from the cache

Needs NOTION_TOKEN and NOTION_REDDIT_LEADS_DB_ID (a .env at the repo root works).
The corpus is cached to .cache/lead_corpus.jsonl so repeat runs cost no API
calls; the cache is gitignored, as the corpus is other people's post content.
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from reddit_leads import passes_light_filter
from shared import http_post, load_dotenv, notion_headers

_CACHE = '.cache/lead_corpus.jsonl'


def _plain(rich: list) -> str:
    """Join every rich-text block, not just the first."""
    return ''.join(b.get('plain_text', '') for b in rich)


def fetch_corpus(db_id: str) -> list[dict]:
    """Page the whole Reddit Leads DB into a list of labelled posts."""
    rows: list[dict] = []
    cursor = None
    while True:
        body: dict = {'page_size': 100}
        if cursor:
            body['start_cursor'] = cursor
        data = http_post(
            f'https://api.notion.com/v1/databases/{db_id}/query',
            body,
            headers=notion_headers(),
        )
        for page in data.get('results', []):
            props = page['properties']
            status = (props.get('Status', {}).get('select') or {}).get('name', '')
            subreddit = (props.get('Subreddit', {}).get('select') or {}).get('name', '')
            rows.append({
                'title': _plain(props.get('Name', {}).get('title', [])),
                'content': _plain(props.get('Content', {}).get('rich_text', [])),
                'subreddit': subreddit,
                'status': status,
            })
        print(f'  fetched {len(rows)} rows', file=sys.stderr)
        if not data.get('has_more'):
            break
        cursor = data.get('next_cursor')
    return rows


def save_cache(rows: list[dict], path: str = _CACHE) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w') as f:
        for row in rows:
            f.write(json.dumps(row) + '\n')


def load_cache(path: str = _CACHE) -> list[dict]:
    with open(path) as f:
        return [json.loads(line) for line in f if line.strip()]


def evaluate(rows: list[dict]) -> dict:
    """Bucket every labelled row by whether the current filter still keeps it."""
    stats: dict = {}
    for row in rows:
        status = row['status']
        if status not in ('approved', 'rejected'):
            continue
        sub = row['subreddit'] or '(none)'
        kept = passes_light_filter(row['title'], row['content'], sub)
        bucket = stats.setdefault(
            sub, {'ok_kept': 0, 'ok_lost': 0, 'bad_kept': 0, 'bad_cut': 0}
        )
        if status == 'approved':
            bucket['ok_kept' if kept else 'ok_lost'] += 1
        else:
            bucket['bad_kept' if kept else 'bad_cut'] += 1
    return stats


def report(stats: dict) -> int:
    """Print the per-subreddit table and the totals.  Returns approved-lost."""
    totals = {'ok_kept': 0, 'ok_lost': 0, 'bad_kept': 0, 'bad_cut': 0}
    rows = sorted(
        stats.items(),
        key=lambda kv: kv[1]['bad_cut'] + kv[1]['bad_kept'],
        reverse=True,
    )
    print(f'{"subreddit":<20}{"seen":>7}{"leads":>7}{"lost":>7}{"noise cut":>11}')
    print('-' * 52)
    for sub, s in rows:
        for k in totals:
            totals[k] += s[k]
        seen = s['ok_kept'] + s['ok_lost'] + s['bad_kept'] + s['bad_cut']
        leads = s['ok_kept'] + s['ok_lost']
        flag = '  <-- LOST LEADS' if s['ok_lost'] else ''
        print(f'{sub:<20}{seen:>7}{leads:>7}{s["ok_lost"]:>7}{s["bad_cut"]:>11}{flag}')

    seen = sum(totals.values())
    leads = totals['ok_kept'] + totals['ok_lost']
    noise = totals['bad_kept'] + totals['bad_cut']
    print('-' * 52)
    print(f'{"TOTAL":<20}{seen:>7}{leads:>7}{totals["ok_lost"]:>7}{totals["bad_cut"]:>11}')
    print()
    if not seen:
        print('corpus           empty — nothing to measure')
        return 0
    print(f'corpus           {seen} reviewed posts, {leads} approved '
          f'({100.0 * leads / seen:.2f}% baseline)')
    if leads:
        print(f'leads kept       {totals["ok_kept"]}/{leads} '
              f'({100.0 * totals["ok_kept"] / leads:.1f}% recall)')
    if noise:
        print(f'reviewer load    {totals["bad_kept"]}/{noise} rejected posts still sent '
              f'({100.0 * totals["bad_cut"] / noise:.1f}% of the noise cut)')
    kept_total = totals['ok_kept'] + totals['bad_kept']
    if kept_total:
        print(f'precision        {100.0 * totals["ok_kept"] / kept_total:.2f}% '
              f'(was {100.0 * leads / seen:.2f}%)')
    if totals['ok_lost']:
        print()
        print(f'WARNING: {totals["ok_lost"]} approved lead(s) would have been dropped.')
    return totals['ok_lost']


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--fetch', action='store_true',
                        help='refresh the cached corpus from Notion first')
    parser.add_argument('--cache', default=_CACHE, help='corpus cache path')
    parser.add_argument('--show-lost', action='store_true',
                        help='print the title of every approved lead that is dropped')
    args = parser.parse_args()

    load_dotenv()

    if args.fetch:
        db_id = os.environ.get('NOTION_REDDIT_LEADS_DB_ID')
        if not db_id or not os.environ.get('NOTION_TOKEN'):
            sys.exit('NOTION_TOKEN and NOTION_REDDIT_LEADS_DB_ID must be set to --fetch')
        print('Fetching corpus from Notion...', file=sys.stderr)
        rows = fetch_corpus(db_id)
        save_cache(rows, args.cache)
        print(f'Cached {len(rows)} rows to {args.cache}', file=sys.stderr)
    else:
        try:
            rows = load_cache(args.cache)
        except FileNotFoundError:
            sys.exit(f'No corpus at {args.cache} — run with --fetch first.')

    if args.show_lost:
        for row in rows:
            if row['status'] == 'approved' and not passes_light_filter(
                row['title'], row['content'], row['subreddit'] or '(none)'
            ):
                print(f'  LOST [r/{row["subreddit"]}] {row["title"][:100]}')
        print()

    sys.exit(1 if report(evaluate(rows)) else 0)


if __name__ == '__main__':
    main()
