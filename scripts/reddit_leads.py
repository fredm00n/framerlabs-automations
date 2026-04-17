#!/usr/bin/env python3
"""
Monitors Reddit RSS feeds for potential Framer freelance leads.

Phase 1 (this script, runs every 15 min via GitHub Actions):
  Fetch RSS → light keyword filter → dedup against Notion → save as "pending".
  No Discord notifications, no LLM reasoning.

Phase 2 (hourly dedicated Claude session on Haiku, see REDDIT_LEADS_REVIEWER.md):
  Review pending leads with reasoning → approve/reject → notify Discord.
"""
import html as _html
import json
import os
import re
import ssl
import sys
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
import error_log

# Seconds to wait between individual subreddit RSS fetches to avoid Reddit
# rate-limiting (HTTP 429) when fetching 43 feeds in rapid succession.
_INTER_FEED_DELAY = 1.5

# Mimic a real browser to avoid Reddit blocking automated requests.
_REDDIT_USER_AGENT = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'


# ---------------------------------------------------------------------------
# Subreddit categories & feed URLs
# ---------------------------------------------------------------------------

_HIRING = frozenset({
    'forhire', 'hiring', 'DesignJobs', 'freelance', 'HungryArtists', 'jobbit',
})
_DESIGN_TECH = frozenset({
    'framer', 'figma', 'webdev', 'web_design', 'Frontend', 'UI_Design',
    'userexperience', 'graphic_design', 'design', 'css', 'reactjs',
    'javascript', 'webdesign', 'html',
})
_NOCODE = frozenset({
    'nocode', 'NoCodeSaaS', 'Webflow', 'Bubble',
})
_BUSINESS = frozenset({
    'startups', 'Entrepreneur', 'SaaS', 'solopreneur', 'smallbusiness', 'digitalnomad',
})
_MARKETING = frozenset({
    'marketing', 'digitalmarketing', 'PPC', 'SEO', 'ecommerce',
})
_INDUSTRY = frozenset({
    'restaurateur', 'RealEstate', 'fitness', 'personaltrainer',
    'legaladvice', 'medicine', 'IndieDev', 'gamedev',
})

REDDIT_FEEDS: dict[str, str] = {
    s: f'https://www.reddit.com/r/{s}/.rss'
    for s in (*_HIRING, *_DESIGN_TECH, *_NOCODE, *_BUSINESS, *_MARKETING, *_INDUSTRY)
}


# ---------------------------------------------------------------------------
# Light filter signal sets
# ---------------------------------------------------------------------------

_WEB_SIGNALS = frozenset({
    'website', 'web design', 'web designer', 'web developer',
    'landing page', 'framer', 'figma', 'portfolio', 'frontend',
    'ui/ux', 'web app', 'react', 'html', 'css',
})
_HIRE_SIGNALS = frozenset({
    'hire', 'hiring', 'looking for', 'need a ', 'need someone', 'seeking',
    'want to hire', 'want someone', 'need help with', 'looking to hire',
    'open to offers', 'taking on clients', '[hiring]',
})
_PAYMENT_SIGNALS = frozenset({
    'pay', 'paid', 'budget', 'rate', 'quote', 'per hour', 'hourly',
    'fixed price', 'compensation', '$', '£', '€',
})
_FRAMER_SIGNALS = frozenset({'framer'})
_BUSINESS_WEB = frozenset({
    'website', 'landing page', 'web design', 'web designer',
    'web developer', 'framer', 'figma',
})
_MARKETING_WEB = frozenset({'website', 'landing page', 'web design', 'web designer', 'framer', 'figma'})

# Posts matching these are almost always job seekers advertising, not clients
_JOB_SEEKER_SIGNALS = frozenset({
    '[for hire]', 'available for hire', "i'm available", 'i am available',
    'my portfolio', 'check out my work', 'dm me for work', 'hire me',
})
_ALWAYS_EXCLUDE = frozenset({
    'how to ', 'how do i ', 'tutorial', 'course', 'learning framer',
    'framer beginner', 'getting started with',
    'feedback on my', 'critique my', 'roast my', 'rate my',
    'what do you think', 'need feedback', 'honest feedback',
    'frustrated with', 'disappointed with', 'beware of', 'warning:',
    'framer pricing', 'framer cost', 'framer vs', 'how much does framer',
    'framer subscription',
})


def _has(text: str, signals: frozenset) -> bool:
    tl = text.lower()
    return any(s in tl for s in signals)


def passes_light_filter(title: str, content: str, subreddit: str) -> bool:
    """Return True if this post is worth saving for Claude to review."""
    text = f'{title} {content}'

    if _has(text, _ALWAYS_EXCLUDE) or _has(text, _JOB_SEEKER_SIGNALS):
        return False

    if subreddit in _HIRING:
        # Job boards: any post with a web/design tech signal is worth reviewing
        return _has(text, _WEB_SIGNALS)

    if subreddit in _DESIGN_TECH:
        # Design/tech communities: need framer + intent, or hiring + payment
        return (
            (_has(text, _FRAMER_SIGNALS) and _has(text, _HIRE_SIGNALS))
            or (_has(text, _HIRE_SIGNALS) and _has(text, _PAYMENT_SIGNALS))
        )

    if subreddit in _NOCODE:
        # No-code tools: hiring + web signal + payment (more specific to cut noise)
        return (
            _has(text, _HIRE_SIGNALS)
            and _has(text, _WEB_SIGNALS)
            and _has(text, _PAYMENT_SIGNALS)
        )

    if subreddit in _BUSINESS:
        return _has(text, _BUSINESS_WEB) and _has(text, _HIRE_SIGNALS)

    if subreddit in _MARKETING | _INDUSTRY:
        return _has(text, _MARKETING_WEB) and _has(text, _HIRE_SIGNALS)

    # Unknown subreddit: require all three signals
    return _has(text, _WEB_SIGNALS) and _has(text, _HIRE_SIGNALS) and _has(text, _PAYMENT_SIGNALS)


# ---------------------------------------------------------------------------
# Environment & HTTP helpers (same patterns as framer_templates.py)
# ---------------------------------------------------------------------------

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
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    def _do():
        req = urllib.request.Request(
            url,
            headers={'User-Agent': _REDDIT_USER_AGENT, **(headers or {})},
        )
        with urllib.request.urlopen(req, context=ctx, timeout=30) as r:
            return r.read().decode('utf-8')
    return _retry(_do)


def http_post(url: str, data: dict, headers: dict | None = None) -> dict:
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    body = json.dumps(data).encode('utf-8')
    def _do():
        req = urllib.request.Request(
            url,
            data=body,
            headers={'Content-Type': 'application/json', 'User-Agent': 'automation-bot/1.0', **(headers or {})},
            method='POST',
        )
        with urllib.request.urlopen(req, context=ctx, timeout=30) as r:
            raw = r.read()
            return json.loads(raw) if raw else {}
    return _retry(_do)


def http_patch(url: str, data: dict, headers: dict | None = None) -> dict:
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    body = json.dumps(data).encode('utf-8')
    def _do():
        req = urllib.request.Request(
            url,
            data=body,
            headers={'Content-Type': 'application/json', 'User-Agent': 'automation-bot/1.0', **(headers or {})},
            method='PATCH',
        )
        with urllib.request.urlopen(req, context=ctx, timeout=30) as r:
            raw = r.read()
            return json.loads(raw) if raw else {}
    return _retry(_do)


def notion_headers() -> dict:
    return {
        'Authorization': f'Bearer {os.environ["NOTION_TOKEN"]}',
        'Notion-Version': '2022-06-28',
    }


# ---------------------------------------------------------------------------
# RSS fetching & parsing
# ---------------------------------------------------------------------------

def _clean_html(text: str) -> str:
    """Strip HTML tags and decode entities from RSS content."""
    if not text:
        return ''
    text = _html.unescape(text)
    text = re.sub(r'<[^>]+>', ' ', text)
    return re.sub(r'\s+', ' ', text).strip()


_ATOM_NS = {'atom': 'http://www.w3.org/2005/Atom'}


def fetch_reddit_posts(subreddit: str, feed_url: str) -> list[dict] | None:
    """Fetch and parse an Atom RSS feed.

    Returns a list of post dicts on success (possibly empty for a feed with no
    entries), or None if the feed could not be fetched or parsed.
    """
    try:
        body = http_get(feed_url)
    except urllib.error.HTTPError as e:
        body_preview = ''
        try:
            body_preview = e.read().decode('utf-8', errors='replace')[:200]
        except Exception:
            pass
        print(f'HTTP {e.code} fetching r/{subreddit}: {e}')
        error_log.log_error(
            'reddit_leads', 'warning',
            f'Failed to fetch r/{subreddit}',
            {
                'feed_url': feed_url,
                'status': e.code,
                'body_preview': body_preview,
                'error': str(e),
            },
        )
        return None
    except Exception as e:
        print(f'Failed to fetch r/{subreddit}: {e}')
        error_log.log_error(
            'reddit_leads', 'warning',
            f'Failed to fetch r/{subreddit}',
            {'feed_url': feed_url, 'error': str(e)},
        )
        return None

    posts = []
    try:
        root = ET.fromstring(body)
        for entry in root.findall('atom:entry', _ATOM_NS):
            title_el = entry.find('atom:title', _ATOM_NS)
            link_el = entry.find('atom:link', _ATOM_NS)
            published_el = entry.find('atom:published', _ATOM_NS)
            updated_el = entry.find('atom:updated', _ATOM_NS)
            content_el = entry.find('atom:content', _ATOM_NS)

            title = _clean_html(title_el.text or '') if title_el is not None else ''
            link = link_el.get('href', '') if link_el is not None else ''
            # Prefer <published> (original post creation time) over <updated>
            # (which reflects the last comment or edit time and can be much later).
            published = (published_el.text or '') if published_el is not None else ''
            updated = (updated_el.text or '') if updated_el is not None else ''
            post_date = published or updated
            content = _clean_html(content_el.text or '') if content_el is not None else ''

            if title and link:
                posts.append({
                    'title': title,
                    'url': link,
                    'post_date': post_date,
                    'content': content,
                    'subreddit': subreddit,
                })
    except ET.ParseError as e:
        print(f'Failed to parse RSS for r/{subreddit}: {e}')
        error_log.log_error('reddit_leads', 'warning', f'Failed to parse RSS for r/{subreddit}', {'error': str(e)})
        return None

    return posts


# ---------------------------------------------------------------------------
# Notion state store
# ---------------------------------------------------------------------------

def url_exists_in_notion(url: str, db_id: str) -> bool:
    """Return True if this URL is already stored in the Notion DB."""
    data = http_post(
        f'https://api.notion.com/v1/databases/{db_id}/query',
        {
            'filter': {'property': 'URL', 'url': {'equals': url}},
            'page_size': 1,
        },
        headers=notion_headers(),
    )
    return len(data.get('results', [])) > 0


def _is_valid_iso8601_date(value: str) -> bool:
    """Return True if *value* is a non-empty string that Python can parse as an
    ISO 8601 datetime.  Notion's date API field requires a valid ISO 8601 string;
    a malformed value causes an HTTP 400 that will recur on every subsequent run
    because the page is never created and dedup never triggers."""
    if not value:
        return False
    try:
        datetime.fromisoformat(value)
        return True
    except (ValueError, TypeError):
        return False


def save_lead_to_notion(lead: dict, db_id: str) -> None:
    """Save a new lead to Notion with status 'pending'."""
    props: dict = {
        'Name': {'title': [{'text': {'content': lead['title'][:2000]}}]},
        'URL': {'url': lead['url']},
        'Subreddit': {'select': {'name': lead['subreddit']}},
        'Content': {'rich_text': [{'text': {'content': lead['content'][:2000]}}]},
        'Status': {'select': {'name': 'pending'}},
        'Discovered': {'date': {'start': datetime.now(timezone.utc).isoformat()}},
    }
    post_date = lead.get('post_date', '')
    if _is_valid_iso8601_date(post_date):
        props['Post Date'] = {'date': {'start': post_date}}
    elif post_date:
        # Date present but unparseable — log a warning and omit the field so
        # the lead is still saved rather than causing a recurring Notion 400.
        error_log.log_error(
            'reddit_leads', 'warning',
            'Skipping invalid post_date for lead',
            {'url': lead.get('url', ''), 'post_date': post_date},
        )
    http_post(
        'https://api.notion.com/v1/pages',
        {'parent': {'database_id': db_id}, 'properties': props},
        headers=notion_headers(),
    )


def get_pending_leads(db_id: str) -> list[dict]:
    """Return all leads with Status = 'pending' from Notion."""
    leads = []
    cursor = None
    while True:
        body: dict = {
            'filter': {'property': 'Status', 'select': {'equals': 'pending'}},
            'page_size': 100,
        }
        if cursor:
            body['start_cursor'] = cursor
        data = http_post(
            f'https://api.notion.com/v1/databases/{db_id}/query',
            body,
            headers=notion_headers(),
        )
        for page in data.get('results', []):
            props = page['properties']
            title_rt = props.get('Name', {}).get('title', [])
            subreddit_sel = props.get('Subreddit', {}).get('select') or {}
            content_rt = props.get('Content', {}).get('rich_text', [])
            leads.append({
                'page_id': page['id'],
                'title': title_rt[0]['plain_text'] if title_rt else '',
                'url': props.get('URL', {}).get('url', '') or '',
                'subreddit': subreddit_sel.get('name', ''),
                'content': content_rt[0]['plain_text'] if content_rt else '',
            })
        if not data.get('has_more'):
            break
        cursor = data.get('next_cursor')
    return leads


def get_lead_by_id(page_id: str) -> dict:
    """Fetch a single Notion page and return it as a lead dict."""
    raw = http_get(
        f'https://api.notion.com/v1/pages/{page_id}',
        headers=notion_headers(),
    )
    page = json.loads(raw)
    props = page.get('properties', {})
    title_rt = props.get('Name', {}).get('title', [])
    subreddit_sel = props.get('Subreddit', {}).get('select') or {}
    content_rt = props.get('Content', {}).get('rich_text', [])
    notes_rt = props.get('Review Notes', {}).get('rich_text', [])
    return {
        'page_id': page['id'],
        'title': title_rt[0]['plain_text'] if title_rt else '',
        'url': props.get('URL', {}).get('url', '') or '',
        'subreddit': subreddit_sel.get('name', ''),
        'content': content_rt[0]['plain_text'] if content_rt else '',
        'review_notes': notes_rt[0]['plain_text'] if notes_rt else '',
    }


def update_lead_status(page_id: str, status: str, notes: str) -> None:
    """Update the Status and Review Notes fields on a Notion page."""
    http_patch(
        f'https://api.notion.com/v1/pages/{page_id}',
        {
            'properties': {
                'Status': {'select': {'name': status}},
                'Review Notes': {'rich_text': [{'text': {'content': notes[:2000]}}]},
            }
        },
        headers=notion_headers(),
    )


def mark_notified(page_id: str) -> None:
    """Set the Notified checkbox to True on a Notion page."""
    http_patch(
        f'https://api.notion.com/v1/pages/{page_id}',
        {'properties': {'Notified': {'checkbox': True}}},
        headers=notion_headers(),
    )


# ---------------------------------------------------------------------------
# Discord
# ---------------------------------------------------------------------------

def notify_discord_lead(lead: dict) -> None:
    """Send a Discord embed for an approved lead."""
    review_notes = lead.get('review_notes', '')
    description = f"**Why this is a lead:** {review_notes}" if review_notes else ''
    embed = {
        'title': lead['title'][:256],
        'url': lead['url'],
        'description': description,
        'color': 0x00B0F4,
        'author': {'name': f"r/{lead['subreddit']}"},
    }
    try:
        http_post(os.environ['DISCORD_WEBHOOK_URL_LEADS'], {'embeds': [embed]})
    except Exception as e:
        print(f'Discord notification failed for "{lead["title"]}": {e}')
        error_log.log_error(
            'reddit_leads', 'warning',
            f'Discord notification failed for "{lead["title"]}"',
            {'error': str(e), 'url': lead.get('url', ''), 'subreddit': lead.get('subreddit', '')},
        )


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
        error_log.log_error('reddit_leads', 'warning', 'Failed to send Discord alert', {'error': str(e)})


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
# Main monitoring flow
# ---------------------------------------------------------------------------

def main() -> None:
    load_dotenv()

    missing = [k for k in ('NOTION_TOKEN', 'NOTION_REDDIT_LEADS_DB_ID') if not os.environ.get(k)]
    if missing:
        print(f'Missing required env vars: {", ".join(missing)}')
        raise SystemExit(1)

    db_id = os.environ['NOTION_REDDIT_LEADS_DB_ID']
    total_saved = 0
    fetch_errors = 0

    for i, (subreddit, feed_url) in enumerate(REDDIT_FEEDS.items()):
        if i > 0:
            time.sleep(_INTER_FEED_DELAY)
        posts = fetch_reddit_posts(subreddit, feed_url)
        if posts is None:
            fetch_errors += 1
            continue
        for post in posts:
            if not passes_light_filter(post['title'], post['content'], subreddit):
                continue
            try:
                if url_exists_in_notion(post['url'], db_id):
                    continue
                save_lead_to_notion(post, db_id)
                total_saved += 1
                print(f'Saved: [r/{subreddit}] {post["title"]}')
            except urllib.error.HTTPError as e:
                body_preview = ''
                try:
                    body_preview = e.read().decode('utf-8', errors='replace')[:500]
                except Exception:
                    pass
                print(f'Error saving lead from r/{subreddit}: {e}')
                error_log.log_error(
                    'reddit_leads', 'error',
                    f'Error saving lead from r/{subreddit}',
                    {'url': post['url'], 'error': str(e), 'notion_response': body_preview},
                )
            except Exception as e:
                print(f'Error saving lead from r/{subreddit}: {e}')
                error_log.log_error(
                    'reddit_leads', 'error',
                    f'Error saving lead from r/{subreddit}',
                    {'url': post['url'], 'error': str(e)},
                )

    if fetch_errors == len(REDDIT_FEEDS):
        _warn_discord(
            'WARNING: reddit_leads.py failed to fetch any subreddit feeds'
            ' — possible network issue. Check logs/errors.jsonl.'
        )
        error_log.log_error(
            'reddit_leads', 'error',
            'All subreddit feeds failed — possible network issue',
            {'feed_count': len(REDDIT_FEEDS)},
        )
    elif fetch_errors > len(REDDIT_FEEDS) // 2:
        _warn_discord(
            f'WARNING: reddit_leads.py failed to fetch {fetch_errors}/{len(REDDIT_FEEDS)} subreddit feeds'
            ' — possible partial network issue or Reddit rate-limiting. Check logs/errors.jsonl.'
        )
        error_log.log_error(
            'reddit_leads', 'warning',
            f'Majority of subreddit feeds failed ({fetch_errors}/{len(REDDIT_FEEDS)})',
            {'fetch_errors': fetch_errors, 'feed_count': len(REDDIT_FEEDS)},
        )

    print(f'Done. Saved {total_saved} new lead(s). ({fetch_errors} subreddit(s) unreachable)')
    _write_summary(
        f'## Reddit Leads Monitor\n'
        f'\U0001f4e5 {total_saved} new lead(s) saved · {fetch_errors}/{len(REDDIT_FEEDS)} subreddit(s) unreachable'
    )


# ---------------------------------------------------------------------------
# CLI interface for the daily reviewer session (REDDIT_LEADS_REVIEWER.md)
# ---------------------------------------------------------------------------

if __name__ == '__main__':
    args = sys.argv[1:]

    if not args:
        main()

    elif args[0] == '--list-pending':
        load_dotenv()
        leads = get_pending_leads(os.environ['NOTION_REDDIT_LEADS_DB_ID'])
        print(json.dumps(leads, indent=2))

    elif args[0] == '--update-status' and len(args) >= 4:
        load_dotenv()
        page_id, status = args[1], args[2]
        notes = ' '.join(args[3:])
        update_lead_status(page_id, status, notes)
        print(f'Updated {page_id} → {status}')

    elif args[0] == '--notify' and len(args) >= 2:
        load_dotenv()
        page_id = args[1]
        lead = get_lead_by_id(page_id)
        notify_discord_lead(lead)
        mark_notified(page_id)
        print(f'Notified: {lead["title"]}')

    else:
        print(f'Unknown arguments: {args}', file=sys.stderr)
        raise SystemExit(1)
