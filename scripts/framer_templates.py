#!/usr/bin/env python3
"""
Monitors Framer marketplace for new templates and notifies Discord.
State is persisted in a Notion database.
"""
import json
import os
import re
import urllib.parse
import urllib.request
import urllib.error
from datetime import date


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
        headers={'Content-Type': 'application/json', **(headers or {})},
        method='POST',
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


def notion_headers() -> dict:
    return {
        'Authorization': f'Bearer {os.environ["NOTION_TOKEN"]}',
        'Notion-Version': '2022-06-28',
    }


# ---------------------------------------------------------------------------
# Fetching & parsing
# ---------------------------------------------------------------------------

def fetch_framer_templates() -> list[dict]:
    print('Fetching Framer marketplace via defuddle...')
    # Framer loads templates client-side (no SSR data), so we use defuddle which
    # renders the page and returns clean Markdown with all template links.
    return fetch_from_defuddle()


def _decode_framer_cdn_url(cdn_url: str) -> str:
    """Extract the direct blob URL from a Framer Next.js image optimizer URL."""
    m = re.search(r'[?&]url=([^&]+)', cdn_url)
    if m:
        return urllib.parse.unquote(m.group(1))
    return cdn_url


def fetch_from_defuddle() -> list[dict]:
    md = http_get('https://defuddle.md/www.framer.com/marketplace/templates/?sort=recent')
    seen: set[str] = set()
    templates = []

    # Build slug → first thumbnail URL map from image-linked entries:
    #   [![Thumbnail 1 for <Title>...](CDN_URL) ![Thumbnail 2...](CDN_URL)](template_url)
    thumb_pattern = re.compile(
        r'\[!\[[^\]]*\]\(([^)]+)\)'           # first image: capture CDN URL
        r'(?:\s*!\[[^\]]*\]\([^)]+\))*'       # optional additional images
        r'\]\(https://www\.framer\.com/marketplace/templates/([a-z0-9][a-z0-9-]+)/\)'
    )
    thumbnails: dict[str, str] = {}
    for cdn_url, slug in thumb_pattern.findall(md):
        if slug not in thumbnails:
            thumbnails[slug] = _decode_framer_cdn_url(cdn_url)

    # Defuddle returns Markdown. Each entry looks like:
    #   [Title](https://www.framer.com/marketplace/templates/slug/)
    #   $99        ← or "Free"
    #   [Author Name](https://www.framer.com/@author-slug/)
    #
    # [^\[]* captures the gap between template link and author link (price + whitespace).
    # Negative lookbehind on ! excludes image links.
    pattern = re.compile(
        r'(?<!!)\[([^\]]+)\]\(https://www\.framer\.com/marketplace/templates/([a-z0-9][a-z0-9-]+)/\)'
        r'([^\[]*)'
        r'\[([^\]]+)\]\(https://www\.framer\.com/@[^)]+/\)'
    )
    for title, slug, gap, author in pattern.findall(md):
        if slug in seen:
            continue
        seen.add(slug)
        price_match = re.search(r'\$[\d,.]+|Free', gap)
        templates.append({
            'slug': slug,
            'title': title,
            'author': author,
            'price': price_match.group(0) if price_match else '',
            'url': f'https://www.framer.com/marketplace/templates/{slug}/',
            'thumbnail_url': thumbnails.get(slug, ''),
        })

    print(f'Parsed {len(templates)} templates from defuddle.')
    if len(templates) < 5:
        print(f'WARNING: only {len(templates)} templates parsed — defuddle output may be incomplete.')
    return templates


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
    http_post(
        'https://api.notion.com/v1/pages',
        {
            'parent': {'database_id': os.environ['NOTION_DATABASE_ID']},
            'properties': {
                'Name': {'title': [{'text': {'content': template['title']}}]},
                'Slug': {'rich_text': [{'text': {'content': template['slug']}}]},
                'URL': {'url': template['url']},
                'Author': {'rich_text': [{'text': {'content': template.get('author', '')}}]},
                'Price': {'rich_text': [{'text': {'content': template.get('price', '')}}]},
                'Discovered': {'date': {'start': date.today().isoformat()}},
            },
        },
        headers=notion_headers(),
    )


# ---------------------------------------------------------------------------
# Discord
# ---------------------------------------------------------------------------

def notify_discord(template: dict) -> None:
    try:
        embed: dict = {
            'title': template['title'],
            'url': template['url'],
            'description': f"By **{template.get('author', 'unknown')}** · {template.get('price', '?')}",
            'color': 0x0055FF,
        }
        if template.get('thumbnail_url'):
            embed['image'] = {'url': template['thumbnail_url']}
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
        if not is_first_run:
            notify_discord(template)
        try:
            save_to_notion(template)
            action = 'Seeded' if is_first_run else 'Notified + saved'
            print(f'{action}: {template["title"]}')
        except Exception as e:
            print(f'Failed to save "{template["title"]}" to Notion: {e}')

    verb = 'Seeded' if is_first_run else 'Notified'
    print(f'Done. {verb} {len(new_templates)} template(s).')


if __name__ == '__main__':
    main()
