#!/usr/bin/env python3
"""
Monitors Framer marketplace for new templates and notifies Discord.
State is persisted in a Notion database.
"""
import json
import os
import re
import urllib.request
import urllib.error
from datetime import datetime


def load_dotenv() -> None:
    try:
        with open('.env') as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith('#'):
                    continue
                key, _, val = line.partition('=')
                if key.strip() and key.strip() not in os.environ:
                    os.environ[key.strip()] = val.strip()
    except FileNotFoundError:
        pass


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

def http_get(url: str, headers: dict | None = None) -> str:
    req = urllib.request.Request(
        url,
        headers={'User-Agent': 'automation-bot/1.0', **(headers or {})},
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read().decode('utf-8')


def http_post(url: str, data: dict, headers: dict | None = None) -> dict:
    body = json.dumps(data).encode('utf-8')
    req = urllib.request.Request(
        url,
        data=body,
        headers={'Content-Type': 'application/json', 'User-Agent': 'automation-bot/1.0', **(headers or {})},
        method='POST',
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        raw = r.read()
        return json.loads(raw) if raw else {}


def notion_headers() -> dict:
    return {
        'Authorization': f'Bearer {os.environ["NOTION_TOKEN"]}',
        'Notion-Version': '2022-06-28',
    }


# ---------------------------------------------------------------------------
# Fetching & parsing
# ---------------------------------------------------------------------------

def fetch_framer_templates() -> list[dict]:
    return fetch_from_rsc()


_RSC_HEADERS = {
    'Accept': 'text/x-component',
    'Rsc': '1',
    'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
}


def fetch_from_rsc() -> list[dict]:
    # Framer uses Next.js RSC (React Server Components). Fetching the marketplace
    # URL with Rsc: 1 header returns a structured component stream that includes
    # all templates (including the newest) directly from the server — no JavaScript
    # execution needed. The defuddle approach missed the 1-2 newest templates because
    # they are hydrated after the initial render that defuddle captured.
    #
    # Pages are cumulative: page=2 returns items 1-40, page=3 returns items 1-60, etc.
    # We fetch up to 3 pages (60 templates) and stop early when a page adds fewer than
    # 20 new items, which means we've reached the last page.
    seen: set[str] = set()
    templates: list[dict] = []

    for page in range(1, 4):
        print(f'Fetching Framer marketplace via RSC (page {page})...')
        url = 'https://www.framer.com/marketplace/templates/?sort=recent'
        if page > 1:
            url += f'&page={page}'
        body = http_get(url, headers=_RSC_HEADERS)

        count_before = len(templates)
        _parse_rsc_body(body, seen, templates)
        new_this_page = len(templates) - count_before

        if new_this_page < 20:
            break

    print(f'Parsed {len(templates)} templates from RSC.')
    if len(templates) < 5:
        print(f'WARNING: only {len(templates)} templates parsed — RSC output may be incomplete.')
    return templates


def _parse_rsc_body(body: str, seen: set, templates: list) -> None:
    """Parse template items from an RSC body, appending new ones to templates in-place."""
    search = '"item":{"id":'
    pos = 0
    while True:
        idx = body.find(search, pos)
        if idx == -1:
            break
        try:
            item = _extract_json_object(body, idx + len('"item":'))
            slug = item.get('slug', '')
            if slug and slug not in seen:
                seen.add(slug)
                price_raw = item.get('price', '')
                # RSC encodes literal "$" as "$$" — strip the escape prefix
                price = price_raw[1:] if price_raw.startswith('$$') else price_raw
                creator = item.get('creator') or {}
                author = creator.get('name', '')
                # RSC encodes Date objects with a "$D" prefix — strip it
                published_raw = item.get('publishedAt', '')
                published_at = published_raw[2:] if published_raw.startswith('$D') else published_raw
                templates.append({
                    'slug': slug,
                    'title': item.get('title', ''),
                    'author': author,
                    'price': price,
                    'url': f'https://www.framer.com/marketplace/templates/{slug}/',
                    'thumbnail': item.get('thumbnail', ''),
                    'published_at': published_at,
                })
        except (ValueError, KeyError):
            pass
        pos = idx + 1


def _extract_json_object(s: str, start: int) -> dict:
    """Extract and parse a balanced JSON object from s starting at position start."""
    depth = 0
    i = start
    in_string = False
    escape = False
    while i < len(s):
        c = s[i]
        if escape:
            escape = False
        elif c == '\\' and in_string:
            escape = True
        elif c == '"':
            in_string = not in_string
        elif not in_string:
            if c == '{':
                depth += 1
            elif c == '}':
                depth -= 1
                if depth == 0:
                    return json.loads(s[start:i + 1])
        i += 1
    raise ValueError('Unclosed JSON object')


# ---------------------------------------------------------------------------
# Notion state store
# ---------------------------------------------------------------------------

def get_seen_slugs() -> set[str]:
    slugs: set[str] = set()
    cursor = None
    db_id = os.environ['NOTION_DATABASE_ID']
    while True:
        body: dict = {'page_size': 100}
        if cursor:
            body['start_cursor'] = cursor
        data = http_post(
            f'https://api.notion.com/v1/databases/{db_id}/query',
            body,
            headers=notion_headers(),
        )
        for page in data['results']:
            rt = page['properties'].get('Slug', {}).get('rich_text', [])
            if rt:
                slugs.add(rt[0]['plain_text'])
        if not data.get('has_more'):
            break
        cursor = data.get('next_cursor')
    return slugs


def save_to_notion(template: dict) -> None:
    props: dict = {
        'Name': {'title': [{'text': {'content': template['title']}}]},
        'Slug': {'rich_text': [{'text': {'content': template['slug']}}]},
        'URL': {'url': template['url']},
        'Author': {'rich_text': [{'text': {'content': template.get('author', '')}}]},
        'Price': {'rich_text': [{'text': {'content': template.get('price', '')}}]},
        'Discovered': {'date': {'start': datetime.now().isoformat()}},
    }
    if template.get('published_at'):
        props['Published'] = {'date': {'start': template['published_at']}}
    if template.get('thumbnail'):
        props['Thumbnail'] = {'url': template['thumbnail']}

    try:
        http_post(
            'https://api.notion.com/v1/pages',
            {'parent': {'database_id': os.environ['NOTION_DATABASE_ID']}, 'properties': props},
            headers=notion_headers(),
        )
    except urllib.error.HTTPError as e:
        if e.code == 400 and 'Thumbnail' in props:
            # Thumbnail property may not exist in DB schema yet; retry without it
            props.pop('Thumbnail')
            http_post(
                'https://api.notion.com/v1/pages',
                {'parent': {'database_id': os.environ['NOTION_DATABASE_ID']}, 'properties': props},
                headers=notion_headers(),
            )
        else:
            raise


# ---------------------------------------------------------------------------
# Discord
# ---------------------------------------------------------------------------

def notify_discord(template: dict) -> None:
    embed: dict = {
        'title': template['title'],
        'url': template['url'],
        'description': f"by {template.get('author', 'unknown')} • {template.get('price', '?')}",
    }
    if template.get('thumbnail'):
        embed['image'] = {'url': template['thumbnail']}
    try:
        http_post(os.environ['DISCORD_WEBHOOK_URL'], {'embeds': [embed]})
    except Exception as e:
        print(f'Discord notification failed for "{template["title"]}": {e}')


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    load_dotenv()

    missing = [
        k for k in ('NOTION_TOKEN', 'NOTION_DATABASE_ID', 'DISCORD_WEBHOOK_URL')
        if not os.environ.get(k)
    ]
    if missing:
        print(f'Missing required env vars: {", ".join(missing)}')
        raise SystemExit(1)

    templates = fetch_framer_templates()
    seen_slugs = get_seen_slugs()

    new_templates = [t for t in templates if t['slug'] not in seen_slugs]
    is_first_run = len(seen_slugs) == 0

    print(f'{len(templates)} templates fetched, {len(new_templates)} new.')

    if not new_templates:
        print('Nothing new. All done.')
        return

    if is_first_run:
        print('First run — seeding DB without Discord notifications to avoid spam.')

    for template in new_templates:
        try:
            save_to_notion(template)
        except Exception as e:
            print(f'Failed to save "{template["title"]}" to Notion: {e}')
            continue
        if not is_first_run:
            notify_discord(template)
        action = 'Seeded' if is_first_run else 'Notified + saved'
        print(f'{action}: {template["title"]}')

    verb = 'Seeded' if is_first_run else 'Notified'
    print(f'Done. {verb} {len(new_templates)} template(s).')


if __name__ == '__main__':
    main()
