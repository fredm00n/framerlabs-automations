#!/usr/bin/env python3
"""
Monitors Framer marketplace for new templates and notifies Discord.
State is persisted in a Notion database.
"""
import base64
import hashlib
import hmac
import json
import os
import re
import time
import urllib.parse
import urllib.request
import urllib.error
import uuid
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

# Primary RSC search key followed by fallback alternatives.
# Framer's Next.js RSC stream embeds template objects under a JSON key that may
# change between Next.js or Framer releases.  If the primary key yields < 5
# results we try each fallback in order and use whichever produces the most
# templates.  The winning key is logged so a human can update the primary if
# needed.
_RSC_PRIMARY_KEY = '"item":'
_RSC_FALLBACK_KEYS = ('"templateItem":', '"marketplaceItem":')


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
    bodies: list[str] = []
    total_parse_errors = 0

    for page in range(1, 3):
        print(f'Fetching Framer marketplace via RSC (page {page})...')
        url = 'https://www.framer.com/marketplace/templates/?sort=recent'
        if page > 1:
            url += f'&page={page}'
        body = http_get(url, headers=_RSC_HEADERS)
        bodies.append(body)

        count_before = len(templates)
        total_parse_errors += _parse_rsc_body(body, seen, templates, _RSC_PRIMARY_KEY)
        new_this_page = len(templates) - count_before

        if new_this_page < 20:
            break

    # If the primary key produced fewer than 5 results, try fallback keys across
    # all fetched pages to recover from an RSC format change automatically.
    if len(templates) < 5:
        best_key = _RSC_PRIMARY_KEY
        best_templates: list[dict] = templates
        best_parse_errors = total_parse_errors
        for fallback_key in _RSC_FALLBACK_KEYS:
            candidate_seen: set[str] = set()
            candidate_templates: list[dict] = []
            candidate_errors = 0
            for body in bodies:
                candidate_errors += _parse_rsc_body(body, candidate_seen, candidate_templates, fallback_key)
            if len(candidate_templates) > len(best_templates):
                best_templates = candidate_templates
                best_key = fallback_key
                best_parse_errors = candidate_errors
        if best_key != _RSC_PRIMARY_KEY:
            print(f'Primary RSC key yielded {len(templates)} template(s); '
                  f'fallback key {best_key!r} yielded {len(best_templates)} — using fallback.')
            error_log.log_error(
                'framer_templates', 'warning',
                f'RSC primary key {_RSC_PRIMARY_KEY!r} yielded only {len(templates)} template(s); '
                f'fallback key {best_key!r} yielded {len(best_templates)}',
                {'primary_count': len(templates), 'fallback_key': best_key,
                 'fallback_count': len(best_templates)},
            )
            templates = best_templates
            total_parse_errors = best_parse_errors

    print(f'Parsed {len(templates)} templates from RSC.')
    if len(templates) < 5:
        print(f'WARNING: only {len(templates)} templates parsed — RSC output may be incomplete.')
        last_body = bodies[-1] if bodies else ''
        error_log.log_error(
            'framer_templates', 'warning',
            f'Only {len(templates)} templates parsed from RSC — format may have changed',
            {'count': len(templates), 'parse_errors': total_parse_errors,
             'body_preview': last_body[:500]},
        )
    return templates


def _parse_rsc_body(body: str, seen: set, templates: list,
                    search_key: str = '"item":') -> int:
    """Parse template items from an RSC body, appending new ones to templates in-place.

    The RSC stream may emit ``"item":{"id":`` with or without whitespace between
    the colon and the opening brace (e.g. ``"item": {"id":``).  We search for the
    key prefix (``search_key``) and then skip any intervening whitespace before
    locating the ``{`` that starts the JSON object passed to
    ``_extract_json_object``.

    ``search_key`` defaults to ``'"item":'`` (the primary Framer RSC key) but
    callers may supply an alternative (e.g. ``'"templateItem":'``) to probe
    fallback keys when the primary yields too few results.

    Returns the number of JSON parse failures (``ValueError`` from
    ``_extract_json_object``).  A non-zero value indicates that the RSC stream
    contained objects that looked like template items but could not be parsed —
    useful for diagnosing format changes without flooding the error log.
    """
    search = search_key
    pos = 0
    parse_errors = 0
    while True:
        idx = body.find(search, pos)
        if idx == -1:
            break
        # Skip optional whitespace between the key and the opening brace
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
        except ValueError:
            parse_errors += 1
        pos = idx + 1
    return parse_errors


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
# Category inference
# ---------------------------------------------------------------------------

CATEGORY_KEYWORDS: dict[str, list[str]] = {
    'Food & Dining': ['restaurant', 'cafe', 'coffee', 'food', 'recipe', 'bakery', 'menu', 'chef', 'dining', 'pizza', 'sushi', 'bar & grill'],
    'Health & Fitness': ['fitness', 'gym', 'health', 'yoga', 'wellness', 'medical', 'clinic', 'dental', 'therapy', 'sport', 'workout', 'physio'],
    'Portfolio & Creative': ['portfolio', 'photographer', 'artist', 'creative', 'studio', 'gallery', 'personal', 'resume', 'cv'],
    'SaaS & Tech': ['saas', 'startup', 'app', 'software', 'tech', 'dashboard', 'analytics', 'ai', 'platform', 'devtool'],
    'Agency': ['agency', 'marketing', 'consulting', 'firm', 'digital agency'],
    'E-commerce & Retail': ['shop', 'store', 'ecommerce', 'e-commerce', 'fashion', 'clothing', 'jewelry', 'boutique'],
    'Real Estate': ['real estate', 'property', 'realty', 'apartment', 'housing', 'rental'],
    'Education': ['education', 'course', 'school', 'university', 'learning', 'academy', 'tutor'],
    'Blog & Magazine': ['blog', 'magazine', 'news', 'journal', 'editorial'],
    'Landing Page': ['landing', 'waitlist', 'coming soon', 'launch'],
    'Non-profit & Community': ['charity', 'nonprofit', 'non-profit', 'church', 'community', 'volunteer'],
}


def infer_category(template: dict) -> str:
    """Infer a category from the template's title and meta_title via keyword matching."""
    text = (template.get('title', '') + ' ' + template.get('meta_title', '')).lower()
    for category, keywords in CATEGORY_KEYWORDS.items():
        for keyword in keywords:
            if keyword in text:
                return category
    return 'Other'


def group_by_category(templates: list[dict]) -> dict[str, list[dict]]:
    """Group templates by inferred category, preserving CATEGORY_KEYWORDS order."""
    groups: dict[str, list[dict]] = {}
    for t in templates:
        cat = infer_category(t)
        groups.setdefault(cat, []).append(t)
    ordered: dict[str, list[dict]] = {}
    for cat in CATEGORY_KEYWORDS:
        if cat in groups:
            ordered[cat] = groups[cat]
    if 'Other' in groups:
        ordered['Other'] = groups['Other']
    return ordered


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
        'Category': {'select': {'name': infer_category(template)}},
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
    remixes = template.get('remixes') or 0
    if author_slug:
        author_text = f"[{author}](https://www.framer.com/marketplace/profiles/{author_slug}/)"
    else:
        author_text = author
    description = f"by {author_text} · **{price}**"
    if meta_title:
        description += f"\n{meta_title}"
    if demo_url:
        description += f"\n[Live Demo]({demo_url})"
    if remixes:
        description += f"\n{remixes} remix{'es' if remixes != 1 else ''}"
    embed: dict = {
        'title': template['title'],
        'url': template['url'],
        'description': description,
        'color': 0x5865F2,
    }
    if template.get('thumbnail'):
        embed['image'] = {'url': template['thumbnail']}
    # Show when the template was published so Discord renders a human-readable date
    published_at = template.get('published_at', '')
    if published_at:
        embed['timestamp'] = published_at
    return embed


def _build_summary_embed(templates: list[dict]) -> dict:
    """Build a single Discord embed summarising new templates grouped by category."""
    n = len(templates)
    noun = 'template' if n == 1 else 'templates'
    grouped = group_by_category(templates)
    lines: list[str] = []
    included = 0
    for category, items in grouped.items():
        lines.append(f'**{category}**')
        for t in items:
            author = t.get('author', 'unknown')
            price = t.get('price', '?')
            line = f"- [{t['title']}]({t['url']}) by {author} -- {price}"
            if len('\n'.join(lines + [line])) > 3900:
                remaining = n - included
                lines.append(f'... and {remaining} more')
                break
            lines.append(line)
            included += 1
        else:
            lines.append('')  # blank line between categories
            continue
        break  # truncation triggered
    return {
        'title': f'{n} new Framer {noun}',
        'url': 'https://www.framer.com/marketplace/templates/?sort=recent',
        'description': '\n'.join(lines).rstrip(),
        'color': 0x5865F2,
    }


def notify_discord_batch(templates: list[dict]) -> None:
    """Send a grouped summary embed then one Discord message per template.

    First message is a rich embed summarising all templates grouped by
    category, followed by one message per template each containing a
    single embed with full details and thumbnail.
    """
    if not templates:
        return
    embeds = [_build_embed(t) for t in templates]
    # First message: grouped summary embed
    payloads: list[dict] = [{'embeds': [_build_summary_embed(templates)]}]
    # Then one message per template
    for embed in embeds:
        payloads.append({'embeds': [embed]})
    for payload in payloads:
        try:
            http_post(os.environ['DISCORD_WEBHOOK_URL_TEMPLATES'], payload)
        except Exception as e:
            titles = ', '.join(em['title'] for em in payload.get('embeds', []))
            label = titles or 'summary'
            print(f'Discord notification failed for [{label}]: {e}')
            error_log.log_error(
                'framer_templates', 'warning',
                'Discord batch notification failed',
                {'label': label, 'error': str(e)},
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


# ---------------------------------------------------------------------------
# X / Twitter
# ---------------------------------------------------------------------------

_TWITTER_CRED_KEYS = (
    'TWITTER_API_KEY', 'TWITTER_API_SECRET',
    'TWITTER_ACCESS_TOKEN', 'TWITTER_ACCESS_TOKEN_SECRET',
)

_TWITTER_POST_URL = 'https://api.twitter.com/2/tweets'


def _percent_encode(s: str) -> str:
    """RFC 5849 percent-encoding (unreserved chars stay unencoded)."""
    return urllib.parse.quote(s, safe='')


def _oauth1_header(method: str, url: str, body_params: dict,
                   consumer_key: str, consumer_secret: str,
                   token: str, token_secret: str,
                   nonce: str | None = None,
                   timestamp: str | None = None) -> str:
    """Build an OAuth 1.0a Authorization header (HMAC-SHA1)."""
    oauth_params = {
        'oauth_consumer_key': consumer_key,
        'oauth_nonce': nonce or uuid.uuid4().hex,
        'oauth_signature_method': 'HMAC-SHA1',
        'oauth_timestamp': timestamp or str(int(time.time())),
        'oauth_token': token,
        'oauth_version': '1.0',
    }
    all_params = {**oauth_params, **body_params}
    param_str = '&'.join(
        f'{_percent_encode(k)}={_percent_encode(v)}'
        for k, v in sorted(all_params.items())
    )
    base_string = f'{method.upper()}&{_percent_encode(url)}&{_percent_encode(param_str)}'
    signing_key = f'{_percent_encode(consumer_secret)}&{_percent_encode(token_secret)}'
    sig = base64.b64encode(
        hmac.new(signing_key.encode(), base_string.encode(), hashlib.sha1).digest()
    ).decode()
    oauth_params['oauth_signature'] = sig
    header_parts = ', '.join(
        f'{_percent_encode(k)}="{_percent_encode(v)}"'
        for k, v in sorted(oauth_params.items())
    )
    return f'OAuth {header_parts}'


def _build_tweet_text(templates: list[dict]) -> str:
    """Build a tweet summarising new templates (max 280 chars)."""
    n = len(templates)
    grouped = group_by_category(templates)
    cats = [cat for cat in grouped if cat != 'Other']
    if cats:
        cat_summary = ', '.join(cats[:3])
        if len(cats) > 3:
            cat_summary += f', and {len(cats) - 3} more'
        intro = f'{n} new Framer template{"s" if n != 1 else ""} -- spanning {cat_summary}.'
    else:
        intro = f'{n} new Framer template{"s" if n != 1 else ""} just dropped.'

    footer = '\nframer.com/marketplace/templates'
    # Build template lines, truncating to fit 280 chars
    lines: list[str] = []
    for t in templates:
        price = t.get('price', '?')
        lines.append(f'- {t["title"]} ({price})')

    # Assemble and truncate
    while lines:
        body = '\n'.join(lines)
        text = f'{intro}\n\n{body}\n{footer}'
        if len(text) <= 280:
            return text
        lines.pop()

    # Fallback: just intro + footer
    text = f'{intro}\n{footer}'
    if len(text) <= 280:
        return text
    # Ultra-fallback: truncate intro
    return text[:277] + '...'


def post_to_x(templates: list[dict]) -> None:
    """Post a tweet about new templates. No-op if Twitter credentials are not configured."""
    creds = {k: os.environ.get(k, '') for k in _TWITTER_CRED_KEYS}
    if not all(creds.values()):
        print('Twitter credentials not configured -- skipping X post.')
        return

    tweet_text = _build_tweet_text(templates)
    auth_header = _oauth1_header(
        'POST', _TWITTER_POST_URL, {},
        creds['TWITTER_API_KEY'], creds['TWITTER_API_SECRET'],
        creds['TWITTER_ACCESS_TOKEN'], creds['TWITTER_ACCESS_TOKEN_SECRET'],
    )
    try:
        http_post(
            _TWITTER_POST_URL,
            {'text': tweet_text},
            headers={'Authorization': auth_header},
        )
        print(f'Posted to X: {tweet_text[:80]}...')
    except Exception as e:
        print(f'Failed to post to X: {e}')
        error_log.log_error(
            'framer_templates', 'warning',
            'Failed to post to X/Twitter',
            {'error': str(e), 'tweet_length': len(tweet_text)},
        )


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
        post_to_x(saved_templates)

    verb = 'Seeded' if is_first_run else 'Notified'
    print(f'Done. {verb} {len(new_templates)} template(s).')
    if is_first_run:
        _write_summary(f'## Framer Monitor\n🌱 First run — seeded {len(new_templates)} template(s) silently')
    else:
        _write_summary(f'## Framer Monitor\n✨ {len(new_templates)} new template(s) found · {len(seen_slugs)} already tracked')


if __name__ == '__main__':
    main()
