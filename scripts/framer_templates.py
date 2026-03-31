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
import error_log


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

def _should_retry(exc: Exception) -> bool:
    if isinstance(exc, urllib.error.HTTPError):
        return exc.code in (429, 500, 502, 503, 504)
    if isinstance(exc, urllib.error.URLError):
        return True  # network/connection errors
    return False


def _retry(fn, max_attempts: int = 4):
    import time
    delay = 2
    last_exc = None
    for attempt in range(max_attempts):
        try:
            return fn()
        except Exception as exc:
            last_exc = exc
            if not _should_retry(exc) or attempt == max_attempts - 1:
                raise
            time.sleep(delay)
            delay *= 2
    raise last_exc  # unreachable but satisfies type checkers


def http_get(url: str, headers: dict | None = None) -> str:
    def _do():
        req = urllib.request.Request(
            url,
            headers={'User-Agent': 'automation-bot/1.0', **(headers or {})},
        )
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.read().decode('utf-8')
    return _retry(_do)


def http_post(url: str, data: dict, headers: dict | None = None) -> dict:
    body = json.dumps(data).encode('utf-8')
    def _do():
        req = urllib.request.Request(
            url,
            data=body,
            headers={'Content-Type': 'application/json', 'User-Agent': 'automation-bot/1.0', **(headers or {})},
            method='POST',
        )
        with urllib.request.urlopen(req, timeout=30) as r:
            raw = r.read()
            return json.loads(raw) if raw else {}
    return _retry(_do)


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
    # Pages are cumulative: page=2 returns items 1-40, etc.
    # We fetch up to 2 pages (40 templates) and stop early when a page adds fewer than
    # 20 new items, which means we've reached the last page.
    seen: set[str] = set()
    templates: list[dict] = []

    for page in range(1, 3):
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
        error_log.log_error(
            'framer_templates', 'warning',
            f'Only {len(templates)} templates parsed from RSC — format may have changed',
            {'count': len(templates)},
        )
    return templates


def _parse_rsc_body(body: str, seen: set, templates: list) -> None:
    """Parse template items from an RSC body, appending new ones to templates in-place.

    The RSC stream may emit ``"item":{"id":`` with or without whitespace between
    the colon and the opening brace (e.g. ``"item": {"id":``).  We search for the
    key prefix ``"item":`` and then skip any intervening whitespace before
    locating the ``{`` that starts the JSON object passed to
    ``_extract_json_object``.
    """
    search = '"item":'
    pos = 0
    while True:
        idx = body.find(search, pos)
        if idx == -1:
            break
        # Skip optional whitespace between "item": and the opening brace
        obj_start = idx + len(search)
        while obj_start < len(body) and body[obj_start] in ' \t\r\n':
            obj_start += 1
        if obj_start >= len(body) or body[obj_start] != '{':
            pos = idx + 1
            continue
        try:
            item = _extract_json_object(body, obj_start)
            if 'id' not in item:
                # Not a template item — advance and keep searching
                pos = idx + 1
                continue
            slug = item.get('slug', '')
            if slug and slug not in seen:
                seen.add(slug)
                price_raw = item.get('price', '')
                # RSC encodes literal "$" as "$$" — strip the escape prefix
                price = price_raw[1:] if price_raw.startswith('$$') else price_raw
                creator = item.get('creator') or {}
                author = creator.get('name', '')
                author_slug = creator.get('slug', '')
                # RSC encodes Date objects with a "$D" prefix — strip it
                published_raw = item.get('publishedAt', '')
                published_at = published_raw[2:] if published_raw.startswith('$D') else published_raw
                templates.append({
                    'slug': slug,
                    'title': item.get('title', ''),
                    'meta_title': item.get('metaTitle', ''),
                    'author': author,
                    'author_slug': author_slug,
                    'price': price,
                    'url': f'https://www.framer.com/marketplace/templates/{slug}/',
                    'demo_url': item.get('publishedUrl', ''),
                    'thumbnail': item.get('thumbnail', ''),
                    'published_at': published_at,
                    'remixes': item.get('remixes') or 0,
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
    if template.get('meta_title'):
        props['Meta Title'] = {'rich_text': [{'text': {'content': template['meta_title']}}]}
    if template.get('demo_url'):
        props['Demo URL'] = {'url': template['demo_url']}
    if template.get('remixes'):
        props['Remixes'] = {'number': template['remixes']}
    if template.get('published_at'):
        props['Published'] = {'date': {'start': template['published_at']}}
    if template.get('thumbnail'):
        props['Thumbnail'] = {'url': template['thumbnail']}
    if template.get('author_slug'):
        props['Author URL'] = {'url': f'https://www.framer.com/marketplace/profiles/{template["author_slug"]}/'}

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

def _build_embed(template: dict) -> dict:
    """Build a Discord embed dict for a single template."""
    author = template.get('author', 'unknown')
    author_slug = template.get('author_slug', '')
    price = template.get('price', '?')
    meta_title = template.get('meta_title', '')
    demo_url = template.get('demo_url', '')
    if author_slug:
        author_text = f"[{author}](https://www.framer.com/marketplace/profiles/{author_slug}/)"
    else:
        author_text = author
    description = f"by {author_text} · **{price}**"
    if meta_title:
        description += f"\n{meta_title}"
    if demo_url:
        description += f"\n[Live Demo]({demo_url})"
    embed: dict = {
        'title': template['title'],
        'url': template['url'],
        'description': description,
        'color': 0x5865F2,
    }
    if template.get('thumbnail'):
        embed['image'] = {'url': template['thumbnail']}
    return embed


def notify_discord_batch(templates: list[dict]) -> None:
    """Send a summary message with grouped embeds to Discord.

    Includes a summary line (e.g. "3 new Framer templates published on the
    marketplace:") and groups up to 10 embeds per webhook call (Discord's limit).
    """
    if not templates:
        return
    n = len(templates)
    noun = 'template' if n == 1 else 'templates'
    summary = f"{n} new Framer {noun} published on the marketplace:"
    embeds = [_build_embed(t) for t in templates]
    # Discord allows max 10 embeds per message
    for i in range(0, len(embeds), 10):
        chunk = embeds[i:i + 10]
        payload: dict = {'embeds': chunk}
        if i == 0:
            payload['content'] = summary
        try:
            http_post(os.environ['DISCORD_WEBHOOK_URL_TEMPLATES'], payload)
        except Exception as e:
            titles = ', '.join(t['title'] for t in chunk)
            print(f'Discord notification failed for batch [{titles}]: {e}')
            error_log.log_error(
                'framer_templates', 'warning',
                f'Discord batch notification failed',
                {'error': str(e), 'count': len(chunk)},
            )


def notify_discord(template: dict) -> None:
    """Send a Discord notification for a single template (convenience wrapper)."""
    notify_discord_batch([template])


def _warn_discord(message: str) -> None:
    """Send a system-level warning to the dedicated alerts webhook."""
    webhook_url = os.environ.get('DISCORD_ALERTS_WEBHOOK_URL')
    if not webhook_url:
        print('DISCORD_ALERTS_WEBHOOK_URL not set — skipping alert.')
        return
    try:
        http_post(webhook_url, {'content': f'[framerlabs-automations] {message}'})
    except Exception as e:
        print(f'Failed to send Discord alert: {e}')
        error_log.log_error('framer_templates', 'warning', 'Failed to send Discord alert', {'error': str(e)})


def _write_summary(text: str) -> None:
    """Append a markdown summary to the GitHub Actions job summary file, if running in CI."""
    path = os.environ.get('GITHUB_STEP_SUMMARY')
    if not path:
        return
    try:
        with open(path, 'a') as f:
            f.write(text + '\n')
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    load_dotenv()

    missing = [
        k for k in ('NOTION_TOKEN', 'NOTION_DATABASE_ID', 'DISCORD_WEBHOOK_URL_TEMPLATES')
        if not os.environ.get(k)
    ]
    if missing:
        print(f'Missing required env vars: {", ".join(missing)}')
        raise SystemExit(1)

    templates = fetch_framer_templates()
    if len(templates) < 5:
        _warn_discord(
            f'WARNING: only {len(templates)} template(s) parsed from Framer RSC'
            ' — format may have changed. Check GitHub Actions logs.'
        )
    seen_slugs = get_seen_slugs()

    new_templates = [t for t in templates if t['slug'] not in seen_slugs]
    is_first_run = len(seen_slugs) == 0

    print(f'{len(templates)} templates fetched, {len(new_templates)} new.')

    if not new_templates:
        print('Nothing new. All done.')
        _write_summary(f'## Framer Monitor\n✓ No new templates · {len(seen_slugs)} already tracked')
        return

    if is_first_run:
        print('First run — seeding DB without Discord notifications to avoid spam.')

    saved_templates: list[dict] = []
    for template in new_templates:
        try:
            save_to_notion(template)
            saved_templates.append(template)
        except Exception as e:
            print(f'Failed to save "{template["title"]}" to Notion: {e}')
            error_log.log_error(
                'framer_templates', 'error',
                f'Failed to save "{template["title"]}" to Notion',
                {'slug': template['slug'], 'error': str(e)},
            )
            continue
        action = 'Seeded' if is_first_run else 'Saved'
        print(f'{action}: {template["title"]}')

    if not is_first_run and saved_templates:
        notify_discord_batch(saved_templates)

    verb = 'Seeded' if is_first_run else 'Notified'
    print(f'Done. {verb} {len(new_templates)} template(s).')
    if is_first_run:
        _write_summary(f'## Framer Monitor\n🌱 First run — seeded {len(new_templates)} template(s) silently')
    else:
        _write_summary(f'## Framer Monitor\n✨ {len(new_templates)} new template(s) found · {len(seen_slugs)} already tracked')


if __name__ == '__main__':
    main()
